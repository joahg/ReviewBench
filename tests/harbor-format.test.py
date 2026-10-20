import asyncio
import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from harbor.models.dataset.manifest import DatasetManifest, DatasetInfo, DatasetTaskRef, DatasetFileRef
from harbor.publisher.packager import Packager

spec = importlib.util.spec_from_file_location(
    "check_format", Path(__file__).resolve().parents[1] / "scripts" / "harbor" / "check-format.py"
)
checker = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"shortuuid": None}):
    spec.loader.exec_module(checker)


class HubManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.task = self.directory / "one"
        self.task.mkdir()
        (self.task / "task.toml").write_text(
            'schema_version = "1.3"\n[task]\nname = "example/one"\n',
            encoding="utf-8",
        )
        self.metric = self.directory / "metric.py"
        self.metric.write_bytes(b"EXPECTED_TASKS = {}\n")
        self.manifest = DatasetManifest(
            dataset=DatasetInfo(name="example/benchmark"),
            tasks=[DatasetTaskRef(name="example/one", digest=f"sha256:{Packager.compute_content_hash(self.task)[0]}")],
            files=[DatasetFileRef(path="metric.py", digest=f"sha256:{hashlib.sha256(self.metric.read_bytes()).hexdigest()}")],
        )

    def restore(self):
        with patch.object(sys, "argv", ["check-format.py", str(self.directory), "--hub-ref", "example/benchmark@v1"]):
            with patch.object(checker, "hub_manifest", AsyncMock(return_value=self.manifest)):
                checker.main()

    def test_restoration_writes_only_a_validated_manifest(self):
        self.restore()
        restored = DatasetManifest.from_toml_file(self.directory / "dataset.toml")
        self.assertEqual(restored.tasks, self.manifest.tasks)
        self.assertEqual(restored.files, self.manifest.files)
        checker.validate(restored, self.directory)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.restore()

    def test_tampered_download_does_not_write_manifest(self):
        self.metric.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ValueError, "file digest"):
            self.restore()
        self.assertFalse((self.directory / "dataset.toml").exists())

    def test_tampered_task_does_not_write_manifest(self):
        task_config = self.task / "task.toml"
        task_config.write_text(task_config.read_text(encoding="utf-8") + 'description = "changed"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "task digest"):
            self.restore()
        self.assertFalse((self.directory / "dataset.toml").exists())

    def test_missing_and_extra_tasks_fail_coverage(self):
        self.manifest.tasks = []
        with self.assertRaisesRegex(ValueError, "no tasks"):
            self.restore()
        self.manifest.tasks = [DatasetTaskRef(name="example/missing", digest="sha256:" + "a" * 64)]
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.restore()
        self.assertFalse((self.directory / "dataset.toml").exists())

    def test_extra_task_fails_coverage(self):
        extra = self.directory / "extra"
        extra.mkdir()
        (extra / "task.toml").write_text('[task]\nname = "example/extra"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.restore()
        self.assertFalse((self.directory / "dataset.toml").exists())

    def test_unpinned_hub_references_are_rejected_before_access(self):
        for reference in ("example/benchmark", "example/benchmark@latest", "example/benchmark@head"):
            with self.assertRaisesRegex(ValueError, "pinned"):
                asyncio.run(checker.hub_manifest(reference))


if __name__ == "__main__":
    unittest.main()
