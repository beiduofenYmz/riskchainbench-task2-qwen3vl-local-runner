#!/usr/bin/env python3
"""Compose Task 1 gates with one standalone Task 2 trajectory per website.

This script never launches a browser and never repairs Task 1 predictions with
Gold entries.  It exposes two logical analysis columns while reusing the same
Task 2 artifact:

* reference_restoration always points to the standalone Task 2 result;
* model_restoration points to that result only when the frozen Task 1 v000
  handoff is AUTHORIZED, otherwise it is marked non-investigable.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def embedded_hash(value: dict[str, Any], field: str) -> str:
    unhashed = copy.deepcopy(value)
    unhashed.pop(field, None)
    return sha256_text(canonical_json(unhashed))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(canonical_json(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def artifact_ref(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def compose(
    *,
    task1_handoff_path: Path,
    task2_run: Path,
    out_dir: Path,
) -> dict[str, Any]:
    handoff = read_json(task1_handoff_path)
    task2_config = read_json(task2_run / "config.json")
    task2_summary = read_json(task2_run / "summary.json")
    if handoff.get("schema_version") != "task1-task2-handoff/v0.1":
        raise ValueError("unsupported Task 1 handoff schema")
    if task2_config.get("execution_mode") != "TASK2_STANDALONE_SITE_ONLY":
        raise ValueError("Task 2 run is not standalone")
    if task2_config.get("judgment_mode") != "DEFERRED_FIXED_EXTERNAL_JUDGE":
        raise ValueError("Task 2 run must defer judgment")
    model = str(task2_config.get("model") or "")
    if not model or handoff.get("model") != model:
        raise ValueError("Task 1 and Task 2 model IDs differ")

    handoff_rows = {
        str(row["case_ref"]): row for row in handoff.get("rows") or []
    }
    result_rows = {
        str(row["case_ref"]): row for row in task2_summary.get("case_results") or []
    }
    expected_case_refs = [str(value) for value in task2_config.get("case_refs") or []]
    if (
        not expected_case_refs
        or len(expected_case_refs) != len(set(expected_case_refs))
        or set(expected_case_refs) != set(handoff_rows)
        or set(expected_case_refs) != set(result_rows)
    ):
        raise ValueError("Task 1 handoff and Task 2 case sets differ")

    composed_rows: list[dict[str, Any]] = []
    reused_count = 0
    non_investigable_count = 0
    for case_ref in expected_case_refs:
        summary_row = result_rows[case_ref]
        result_path = Path(str(summary_row["result_path"]))
        if not result_path.is_absolute():
            result_path = PROJECT_ROOT / result_path
        result = read_json(result_path)
        if (
            result.get("case_ref") != case_ref
            or result.get("requested_model") != model
            or result.get("evaluation_condition") != "task2_standalone"
            or result.get("judgment_mode") != "DEFERRED_FIXED_EXTERNAL_JUDGE"
        ):
            raise ValueError(f"invalid standalone Task 2 result: {case_ref}")
        evidence_package = result.get("artifact_refs", {}).get("evidence_package") or {}
        package_path = Path(str(evidence_package.get("path") or ""))
        if not package_path.is_absolute():
            package_path = PROJECT_ROOT / package_path
        if (
            not package_path.is_file()
            or sha256_file(package_path) != evidence_package.get("sha256")
        ):
            raise ValueError(f"invalid evidence package: {case_ref}")

        shared_result = {
            "status": "TASK2_RESULT_AVAILABLE",
            "investigable": True,
            "task2_status": result.get("status"),
            "task2_result": artifact_ref(result_path),
            "evidence_package": artifact_ref(package_path),
            "trajectory_reused": True,
        }
        gate = handoff_rows[case_ref]
        authorized = gate.get("status") == "AUTHORIZED"
        if authorized:
            model_column = copy.deepcopy(shared_result)
            model_column["task1_gate_status"] = "AUTHORIZED"
            reused_count += 1
        else:
            model_column = {
                "status": "NON_INVESTIGABLE",
                "investigable": False,
                "task1_gate_status": gate.get("status"),
                "task1_gate_detail": gate.get("detail"),
                "task2_result": None,
                "evidence_package": None,
                "trajectory_reused": False,
                "gold_repair_applied": False,
            }
            non_investigable_count += 1
        composed_rows.append(
            {
                "schema_version": "riskchainbench-end-to-end-composition/v0.1",
                "case_ref": case_ref,
                "sample_id": gate.get("sample_id"),
                "model": model,
                "actual_task2_browser_run_count": 1,
                "reference_restoration": copy.deepcopy(shared_result),
                "model_restoration": model_column,
            }
        )

    rows_path = out_dir / "composed_conditions.jsonl"
    atomic_jsonl(rows_path, composed_rows)
    manifest = {
        "schema_version": "riskchainbench-end-to-end-composition-manifest/v0.1",
        "status": "PASS",
        "model": model,
        "case_count": len(composed_rows),
        "actual_task2_browser_trajectory_count": len(composed_rows),
        "logical_condition_record_count": len(composed_rows) * 2,
        "reference_reused_trajectory_count": len(composed_rows),
        "model_restoration_reused_trajectory_count": reused_count,
        "model_restoration_non_investigable_count": non_investigable_count,
        "browser_runs_duplicated_for_conditions": False,
        "gold_repair_applied": False,
        "inputs": {
            "task1_handoff": artifact_ref(task1_handoff_path),
            "task2_config": artifact_ref(task2_run / "config.json"),
            "task2_summary": artifact_ref(task2_run / "summary.json"),
        },
        "outputs": {
            "composed_conditions": artifact_ref(rows_path),
        },
        "claim_boundary": {
            "reference_and_model_are_logical_analysis_columns": True,
            "task2_browser_trajectory_collected_once_per_model_site": True,
            "fixed_external_judge_applied": False,
            "human_gold_present": False,
            "accuracy_or_f1_allowed": False,
        },
    }
    manifest["manifest_sha256"] = embedded_hash(manifest, "manifest_sha256")
    atomic_json(out_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task1-handoff", type=Path, required=True)
    parser.add_argument("--task2-run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = compose(
            task1_handoff_path=args.task1_handoff.resolve(),
            task2_run=args.task2_run.resolve(),
            out_dir=args.out.resolve(),
        )
    except Exception as exc:
        failure = {
            "schema_version": "riskchainbench-end-to-end-composition-manifest/v0.1",
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1600],
        }
        atomic_json(args.out / "manifest.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
