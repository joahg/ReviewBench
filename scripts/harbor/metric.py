"""Strict ReviewBench metrics for single runs and explicitly planned rounds."""

import argparse
import hashlib
import json
import math
import runpy
import sys
from pathlib import Path

EXPECTED_TASKS = {}  # Replaced by the exporter with task_id: evaluation_id.
COUNTS = (
    "candidate_count", "golden_tp_count", "matched_count", "matched_tp_count",
    "unmatched_count", "unmatched_tp_count", "unmatched_fp_count", "golden_covered_count",
)
RATIOS = (
    "grounded_precision", "grounded_recall", "augmented_precision",
    "augmented_recall", "novel_tp_yield",
)


def load_profile(job_dir, metric_path):
    plan = json.loads((job_dir / "reviewbench-plan.json").read_text(encoding="utf-8"))
    snapshot = job_dir / "reviewbench-metric.py"
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    if plan.get("schema_version") != 1:
        raise ValueError("Unsupported ReviewBench run plan")
    if digest(snapshot) != plan["metric_sha256"]:
        raise ValueError("Run metric snapshot digest mismatch")
    if digest(metric_path) not in [plan["metric_sha256"], *plan["source_metric_sha256"]]:
        raise ValueError("Metric does not match this run's frozen scoring profile")
    metric = runpy.run_path(str(snapshot))
    expected = plan["expected_tasks"]
    if not isinstance(expected, dict) or not expected or metric["EXPECTED_TASKS"] != expected:
        raise ValueError("Run metric task versions do not match the frozen plan")
    attempts = plan["n_attempts"]
    if type(attempts) is not int or attempts < 1 or len(plan["trials"]) != len(expected) * attempts:
        raise ValueError("Invalid run plan trial coverage")
    pairs = set()
    for trial in plan["trials"].values():
        task_id = str(trial["task_id"])
        number = trial["round"]
        if task_id not in expected or trial["evaluation_id"] != expected[task_id] or (
            type(number) is not int or not 1 <= number <= attempts or (task_id, number) in pairs
        ):
            raise ValueError("Invalid or duplicate run plan task/round")
        pairs.add((task_id, number))
    return plan, metric


def ratios(counts):
    def divide(numerator, denominator):
        return numerator / denominator if denominator else None

    return {
        "grounded_precision": divide(counts["matched_tp_count"], counts["matched_count"]),
        "grounded_recall": divide(counts["golden_covered_count"], counts["golden_tp_count"]),
        "augmented_precision": divide(
            counts["matched_tp_count"] + counts["unmatched_tp_count"], counts["candidate_count"]
        ),
        "augmented_recall": divide(
            counts["golden_covered_count"] + counts["unmatched_tp_count"],
            counts["golden_tp_count"] + counts["unmatched_tp_count"],
        ),
        "novel_tp_yield": divide(counts["unmatched_tp_count"], counts["candidate_count"]),
    }


def read_counts(reward):
    groups = {}
    for key, value in reward.items():
        if key in ("rb_schema", "rb_task", "rb_evaluation"):
            continue
        prefix, separator, field = key.rpartition(".")
        if not separator or field not in COUNTS or not (
            prefix == "overall" or prefix.startswith(("by_severity.", "by_category."))
        ):
            raise ValueError(f"Unknown reward field: {key}")
        if type(value) not in (int, float) or not 0 <= value <= 2**53 - 1 or value != int(value):
            raise ValueError(f"Invalid count: {key}={value!r}")
        groups.setdefault(prefix, {})[field] = value
    if "overall" not in groups:
        raise ValueError("Missing overall counts")
    for prefix, counts in groups.items():
        if set(counts) != set(COUNTS):
            raise ValueError(f"Incomplete counts: {prefix}")
        if (
            counts["candidate_count"] != counts["matched_count"] + counts["unmatched_count"]
            or counts["unmatched_count"] != counts["unmatched_tp_count"] + counts["unmatched_fp_count"]
            or counts["matched_tp_count"] > counts["matched_count"]
            or counts["golden_covered_count"] > counts["golden_tp_count"]
        ):
            raise ValueError(f"Inconsistent counts: {prefix}")
    return groups


def compute(rewards, expected, *, report_incomplete=True):
    if not expected:
        raise ValueError("Metric has no expected tasks; use the generated dataset metric.py")
    seen = set()
    rows = []
    failed = duplicates = 0
    for reward in rewards:
        if reward is None:
            failed += 1
            continue
        if not isinstance(reward, dict) or reward.get("rb_schema") != 1:
            raise ValueError("Unsupported ReviewBench reward schema")
        task_id = str(reward.get("rb_task"))
        if task_id not in expected or reward.get("rb_evaluation") != expected[task_id]:
            raise ValueError("Reward belongs to another task or evaluation version")
        if task_id in seen:
            duplicates += 1
        seen.add(task_id)
        rows.append(read_counts(reward))

    complete = not failed and not duplicates and len(seen) == len(expected)
    output = {
        "complete": int(complete), "expected_prs": len(expected), "scored_prs": len(seen),
        "failed_trials": failed, "duplicate_trials": duplicates,
    }
    if not complete:
        if report_incomplete:
            print(f"ReviewBench incomplete: {output}; aggregate scores withheld", file=sys.stderr)
        return output

    for prefix in sorted({key for row in rows for key in row}):
        strata = [row[prefix] for row in rows if prefix in row]
        pooled = {key: sum(row[key] for row in strata) for key in COUNTS}
        per_pr = [ratios(row) for row in strata]
        micro = ratios(pooled)
        for mode in ("macro", "micro"):
            for field, value in {**pooled, "novel_tp_count": pooled["unmatched_tp_count"]}.items():
                output[f"{mode}.{prefix}.{field}"] = value
            for field in RATIOS:
                defined = [row[field] for row in per_pr if row[field] is not None]
                value = (sum(defined) / len(defined) if defined else None) if mode == "macro" else micro[field]
                key = f"{mode}.{prefix}.{field}"
                output[f"{key}.defined"] = int(value is not None)
                if value is not None:
                    output[key] = value
    return output


def compute_rounds(rewards, expected, attempts, source_prs):
    if type(attempts) is not int or attempts < 1:
        raise ValueError("Number of rounds must be a positive integer")
    if type(source_prs) is not int or source_prs < len(expected):
        raise ValueError("Invalid source dataset size")
    rounds = [[] for _ in range(attempts)]
    scored = set()
    unassigned = failures = 0
    for reward in rewards:
        if reward is None:
            failures += 1
            continue
        if not isinstance(reward, dict):
            raise ValueError("Invalid round reward")
        task_id = str(reward.get("rb_task"))
        if reward.get("rb_schema") != 1 or task_id not in expected or reward.get("rb_evaluation") != expected[task_id]:
            raise ValueError("Reward belongs to another task or evaluation version")
        clean = {key: value for key, value in reward.items() if key not in ("rb_round", "rb_failed")}
        read_counts(clean)
        scored.add(task_id)
        if "rb_round" not in reward:
            # Harbor's internal END hook computes once before the plugin's END hook.
            unassigned += 1
            continue
        number = reward["rb_round"]
        if type(number) is not int or not 1 <= number <= attempts:
            raise ValueError("Invalid planned round number")
        failed = reward.get("rb_failed")
        if type(failed) is not int or failed not in (0, 1):
            raise ValueError("Missing or invalid trial failure marker")
        failures += failed
        rounds[number - 1].append(None if failed else clean)

    results = [compute(rows, expected, report_incomplete=False) for rows in rounds]
    complete = not failures and not unassigned and all(row["complete"] for row in results)
    output = {
        "complete": int(complete),
        "source_prs": source_prs, "expected_prs": len(expected),
        "full_dataset": int(len(expected) == source_prs),
        "n_attempts": attempts, "expected_trials": len(expected) * attempts,
        "scored_prs": len(scored),
        "scored_trials": sum(row["scored_prs"] for row in results),
        "completed_rounds": sum(row["complete"] for row in results),
        "failed_trials": failures, "unassigned_trials": unassigned,
        "duplicate_trials": sum(row["duplicate_trials"] for row in results),
    }
    for number, result in enumerate(results, 1):
        for key, value in result.items():
            output[f"round.{number}.{key}"] = value
    if not complete:
        return output

    keys = sorted({key for result in results for key in result if key.startswith(("macro.", "micro."))})
    for key in keys:
        if key.endswith(".defined"):
            continue
        is_count = key.rsplit(".", 1)[-1] in (*COUNTS, "novel_tp_count")
        values = [result.get(key, 0) for result in results] if is_count else [
            result[key] for result in results if key in result
        ]
        mean = sum(values) / len(values)
        # Report how many rounds define a ratio; null is never treated as zero.
        output[f"summary.{key}.defined_rounds"] = len(values)
        output[f"summary.{key}.mean"] = mean
        output[f"summary.{key}.stddev"] = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    for key in sorted({key.removesuffix(".defined") for result in results for key in result if key.endswith(".defined")}):
        output.setdefault(f"summary.{key}.defined_rounds", 0)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", type=Path, required=True)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    rewards = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = compute(rewards, EXPECTED_TASKS)
    if args.require_complete and not result["complete"]:
        raise ValueError("Refusing to publish an incomplete ReviewBench evaluation")
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
