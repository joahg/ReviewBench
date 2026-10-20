"""Check an export against Harbor's installed schemas and actual task packager."""

import argparse
import asyncio
import hashlib
from pathlib import Path

from harbor.models.dataset.manifest import DatasetManifest, DatasetInfo, DatasetTaskRef, DatasetFileRef
from harbor.models.task.config import TaskConfig
from harbor.publisher.packager import Packager


def validate(dataset, directory):
    if not dataset.tasks:
        raise ValueError("Dataset contains no tasks")
    expected = {reference.short_name for reference in dataset.tasks}
    actual = {path.name for path in directory.iterdir() if path.is_dir() and (path / "task.toml").exists()}
    if len(expected) != len(dataset.tasks) or actual != expected:
        raise ValueError("Downloaded task coverage does not match the dataset manifest")
    for reference in dataset.tasks:
        task_directory = directory / reference.short_name
        task = TaskConfig.model_validate_toml((task_directory / "task.toml").read_text(encoding="utf-8"))
        if task.task.name != reference.name:
            raise ValueError(f"Wrong task name: {reference.name}")
        digest, _ = Packager.compute_content_hash(task_directory)
        if reference.digest != f"sha256:{digest}":
            raise ValueError(f"Wrong task digest: {reference.name}")
    for reference in dataset.files:
        digest = hashlib.sha256((directory / reference.path).read_bytes()).hexdigest()
        if reference.digest != f"sha256:{digest}":
            raise ValueError(f"Wrong dataset file digest: {reference.path}")


async def hub_manifest(reference):
    if "@" not in reference or reference.rsplit("@", 1)[1] in ("", "latest", "head"):
        raise ValueError("--hub-ref requires a pinned org/dataset@version, tag, or digest")
    from harbor.models.task.id import PackageTaskId
    from harbor.registry.client.package import PackageDatasetClient

    metadata = await PackageDatasetClient().get_dataset_metadata(reference)
    if any(not isinstance(task, PackageTaskId) for task in metadata.task_ids):
        raise ValueError("Only content-addressed Hub task packages are supported")
    return DatasetManifest(
        dataset=DatasetInfo(name=metadata.name, description=metadata.description),
        tasks=[DatasetTaskRef(name=f"{task.org}/{task.name}", digest=task.ref)
               for task in metadata.task_ids],
        files=[DatasetFileRef(path=file.path, digest=f"sha256:{file.content_hash}")
               for file in metadata.files],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--hub-ref", help="Restore a missing manifest from pinned Hub metadata after checking all digests")
    args = parser.parse_args()
    manifest = args.directory / "dataset.toml"
    if args.hub_ref:
        if manifest.exists():
            raise ValueError("Manifest already exists; omit --hub-ref to validate it without changes")
        dataset = asyncio.run(hub_manifest(args.hub_ref))
    else:
        dataset = DatasetManifest.from_toml_file(manifest)
    validate(dataset, args.directory)
    if args.hub_ref:
        with manifest.open("x", encoding="utf-8", newline="\n") as file:
            file.write(dataset.to_toml())
    print(f"Harbor schemas and content digests validated for {len(dataset.tasks)} tasks")


if __name__ == "__main__":
    main()
