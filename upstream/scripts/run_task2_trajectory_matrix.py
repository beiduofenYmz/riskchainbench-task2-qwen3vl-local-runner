#!/usr/bin/env python3
"""Run one standalone Balanced-600 Task 2 trajectory pass per model."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from task2_frozen_protocol import (  # noqa: E402
    EXPECTED_TASK2_CONTRACT_SHA256,
    TASK2_CASE_COUNT,
    TASK2_TRAJECTORY_PROTOCOL,
    validate_task2_release,
)

PYTHON = Path(sys.executable)
RUNNER = PROJECT_ROOT / "scripts/run_task2_autonomous_mllm_batch.py"
VALIDATOR = PROJECT_ROOT / "scripts/validate_task2_autonomous_mllm_batch.py"
DEFAULT_TASK2_DATA = PROJECT_ROOT / "data/task2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "model"


def parse_models(value: str) -> list[str]:
    models = [item.strip() for item in value.split(",") if item.strip()]
    if not models or len(models) != len(set(models)):
        raise ValueError("models must be a unique non-empty list")
    return models


def passing_models(route_probe: dict[str, Any]) -> set[str]:
    return {
        str(row["model"])
        for row in route_probe.get("results") or []
        if row.get("status") == "PASS_MULTIMODAL_ROUTE"
    }


class ProgressLedger:
    def __init__(
        self,
        path: Path,
        models: list[str],
        case_count: int,
    ) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.payload = {
            "schema_version": "riskchainbench-task2-trajectory-matrix-status/v0.1",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "models": {
                model: {
                    "state": "PENDING",
                    "round": 0,
                    "target_case_count": case_count,
                    "pipeline_evaluable_case_count": 0,
                    "system_failure_case_count": case_count,
                }
                for model in models
            },
        }
        atomic_json(self.path, self.payload)

    def update(self, model: str, **values: Any) -> None:
        with self.lock:
            self.payload["models"][model].update(values)
            self.payload["updated_at"] = utc_now()
            atomic_json(self.path, self.payload)


def runner_command(
    *,
    args: argparse.Namespace,
    model: str,
    run_dir: Path,
    resume: bool,
) -> list[str]:
    command = [
        str(PYTHON),
        str(RUNNER),
        "--task2-release",
        str(args.task2_release),
        "--route-probe",
        str(args.route_probe),
        "--transport",
        args.transport,
        "--runtime-root",
        str(args.runtime_root),
        "--runtime-materialization-report",
        str(args.runtime_materialization_report),
        "--model",
        model,
        "--max-steps",
        "30",
        "--max-case-seconds",
        "600",
        "--max-tokens",
        "4096",
        "--max-judge-images",
        "8",
        "--max-judge-image-bytes",
        "5500000",
        "--workers",
        str(args.case_workers),
        "--viewport-only",
        "--out",
        str(run_dir),
    ]
    if args.base_url_env:
        command.extend(["--base-url-env", args.base_url_env])
    if args.api_key_env:
        command.extend(["--api-key-env", args.api_key_env])
    if args.env_file is not None:
        command.extend(["--env-file", str(args.env_file)])
    if args.tesseract_root is not None:
        command.extend(["--tesseract-root", str(args.tesseract_root)])
    if resume:
        command.append("--resume")
    return command


def run_logged(command: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    local_browser_cache = PROJECT_ROOT / ".tools/ms-playwright-1.44"
    if (
        "PLAYWRIGHT_BROWSERS_PATH" not in environment
        and local_browser_cache.is_dir()
    ):
        environment["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_cache)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{utc_now()}] COMMAND_SHA256={sha256_text(canonical_json(command))}\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.write(f"[{utc_now()}] EXIT={completed.returncode}\n")
        return completed.returncode


def run_model(
    *,
    args: argparse.Namespace,
    model: str,
    ledger: ProgressLedger,
) -> dict[str, Any]:
    model_root = args.out / "models" / safe_slug(model)
    run_dir = model_root / "run"
    log_path = model_root / "runner.log"
    ledger.update(model, state="RUNNING")
    for round_index in range(1, args.max_system_retry_rounds + 2):
        ledger.update(model, state="RUNNING", round=round_index)
        resume = run_dir.exists()
        run_logged(
            runner_command(
                args=args,
                model=model,
                run_dir=run_dir,
                resume=resume,
            ),
            log_path,
        )
        summary_path = run_dir / "summary.json"
        if not summary_path.is_file():
            continue
        summary = read_json(summary_path)
        ledger.update(
            model,
            target_case_count=int(summary.get("target_case_count") or 0),
            pipeline_evaluable_case_count=int(
                summary.get("pipeline_evaluable_case_count") or 0
            ),
            system_failure_case_count=int(
                summary.get("system_failure_case_count") or 0
            ),
            pass_case_count=int(summary.get("pass_case_count") or 0),
            model_failure_case_count=int(
                summary.get("model_failure_case_count") or 0
            ),
        )
        if (
            int(summary.get("target_case_count") or 0) == args.case_count
            and int(summary.get("system_failure_case_count") or 0) == 0
            and int(summary.get("pipeline_evaluable_case_count") or 0)
            == args.case_count
        ):
            validation_path = model_root / "validation.json"
            validation_command = [
                str(PYTHON),
                str(VALIDATOR),
                "--run",
                str(run_dir),
                "--out",
                str(validation_path),
                "--expected-transport",
                args.transport,
                "--expected-task2-contract-sha256",
                args.task2_contract_sha256,
            ]
            validation_rc = run_logged(
                validation_command,
                model_root / "validator.log",
            )
            validation = (
                read_json(validation_path)
                if validation_path.is_file()
                else {"status": "MISSING"}
            )
            state = (
                "COMPLETE"
                if validation_rc == 0 and validation.get("status") == "PASS"
                else "VALIDATION_FAILED"
            )
            ledger.update(model, state=state)
            return {
                "model": model,
                "state": state,
                "rounds_used": round_index,
                "summary": {
                    key: summary.get(key)
                    for key in (
                        "target_case_count",
                        "pass_case_count",
                        "model_failure_case_count",
                        "pipeline_evaluable_case_count",
                        "system_failure_case_count",
                    )
                },
                "run_dir": str(run_dir),
                "validation": str(validation_path),
            }
    ledger.update(model, state="SYSTEM_FAILURES_UNRESOLVED")
    summary = (
        read_json(run_dir / "summary.json")
        if (run_dir / "summary.json").is_file()
        else {}
    )
    return {
        "model": model,
        "state": "SYSTEM_FAILURES_UNRESOLVED",
        "rounds_used": args.max_system_retry_rounds + 1,
        "summary": summary,
        "run_dir": str(run_dir),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", required=True)
    parser.add_argument(
        "--transport",
        choices=("libinfer-neo", "openai-compatible"),
        required=True,
    )
    parser.add_argument("--base-url-env")
    parser.add_argument("--api-key-env")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--tesseract-root",
        type=Path,
        help="Optional portable Tesseract root; otherwise use tesseract from PATH.",
    )
    parser.add_argument(
        "--task2-release",
        type=Path,
        default=DEFAULT_TASK2_DATA,
    )
    parser.add_argument("--route-probe", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--runtime-materialization-report",
        type=Path,
        required=True,
    )
    parser.add_argument("--case-workers", type=int, default=2)
    parser.add_argument("--parallel-models", type=int, default=2)
    parser.add_argument("--max-system-retry-rounds", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    models = parse_models(args.models)
    release = validate_task2_release(
        args.task2_release.resolve(),
        require_frozen_hash=True,
        verify_files=True,
    )
    args.task2_release = args.task2_release.resolve()
    args.task2_contract_sha256 = release["contract_sha256"]
    args.case_count = int(release["contract"]["case_count"])
    release_paths = release["paths"]
    args.resolver = release_paths["resolver"]
    args.observation_manifest = release_paths["observation_manifest"]
    args.prompts = release_paths["prompts"]
    args.codebook = release_paths["codebook"]
    args.docker_reference = release_paths["docker_reference"]
    if (
        args.task2_contract_sha256 != EXPECTED_TASK2_CONTRACT_SHA256
        or args.case_count != TASK2_CASE_COUNT
    ):
        raise SystemExit("Task 2 release is not the frozen Balanced-600")
    if not 1 <= args.case_workers <= 8:
        raise SystemExit("case-workers must be between 1 and 8")
    if not 1 <= args.parallel_models <= len(models):
        raise SystemExit("parallel-models must be between 1 and model count")
    if not 0 <= args.max_system_retry_rounds <= 5:
        raise SystemExit("max-system-retry-rounds must be between 0 and 5")
    materialization = read_json(args.runtime_materialization_report)
    if (
        materialization.get("schema_version")
        != "riskchainbench-task2-materialization/v0.2"
        or materialization.get("status") != "PASS"
        or materialization.get("task2_contract_sha256")
        != args.task2_contract_sha256
        or materialization.get("report_sha256")
        != embedded_hash(materialization, "report_sha256")
        or Path(str(materialization.get("runtime_root") or "")).resolve()
        != args.runtime_root.resolve()
        or int(materialization.get("materialized_case_count") or -1)
        != args.case_count
        or int(materialization.get("failure_count", -1)) != 0
    ):
        raise SystemExit("portable runtime report does not match Task 2 release")
    route_probe = read_json(args.route_probe)
    if (
        route_probe.get("status") != "PASS_FIXED_MLLM_SELECTED"
        or route_probe.get("transport") != args.transport
        or route_probe.get("oneapi_used") is not False
        or not set(models).issubset(passing_models(route_probe))
    ):
        raise SystemExit("requested models did not pass the matching route probe")
    args.out.mkdir(parents=True, exist_ok=True)
    platform_release_path = args.task2_release / "RELEASE_MANIFEST.json"
    platform_release = read_json(platform_release_path)
    if (
        platform_release.get("release_sha256")
        != embedded_hash(platform_release, "release_sha256")
    ):
        raise SystemExit("Task 2 platform release manifest is invalid")
    alignment = {
        "schema_version": "riskchainbench-task2-alignment-preflight/v0.1",
        "created_at": utc_now(),
        "status": "PASS",
        "task2_protocol_id": TASK2_TRAJECTORY_PROTOCOL["protocol_id"],
        "task2_contract_sha256": args.task2_contract_sha256,
        "platform_release_sha256": platform_release["release_sha256"],
        "ordered_case_refs_sha256": release["contract"][
            "ordered_case_refs_sha256"
        ],
        "case_count": args.case_count,
        "models": models,
        "transport": args.transport,
        "route_probe_sha256": sha256_file(args.route_probe),
        "runtime_materialization_report_sha256": sha256_file(
            args.runtime_materialization_report
        ),
        "browser_harness_id": TASK2_TRAJECTORY_PROTOCOL[
            "browser_harness_id"
        ],
        "browsergym_core_version": TASK2_TRAJECTORY_PROTOCOL[
            "browsergym_core_version"
        ],
        "playwright_version": TASK2_TRAJECTORY_PROTOCOL[
            "playwright_version"
        ],
        "max_steps": TASK2_TRAJECTORY_PROTOCOL["max_steps"],
        "max_case_seconds": TASK2_TRAJECTORY_PROTOCOL[
            "max_case_seconds"
        ],
        "max_tokens_per_call": TASK2_TRAJECTORY_PROTOCOL[
            "max_tokens_per_call"
        ],
        "judgment_mode": TASK2_TRAJECTORY_PROTOCOL["judgment_mode"],
        "actual_browser_trajectory_count_per_model_site": 1,
    }
    alignment["attestation_sha256"] = embedded_hash(
        alignment,
        "attestation_sha256",
    )
    atomic_json(args.out / "alignment_preflight.json", alignment)
    ledger = ProgressLedger(
        args.out / "matrix_status.json",
        models,
        args.case_count,
    )
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.parallel_models) as executor:
        futures = {
            executor.submit(run_model, args=args, model=model, ledger=ledger): model
            for model in models
        }
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: models.index(str(row["model"])))
    complete = sum(row["state"] == "COMPLETE" for row in rows)
    manifest = {
        "schema_version": "riskchainbench-task2-trajectory-matrix/v0.1",
        "created_at": utc_now(),
        "status": "PASS" if complete == len(models) else "FAIL",
        "transport": args.transport,
        "models": models,
        "model_count": len(models),
        "complete_model_count": complete,
        "task2_protocol_id": TASK2_TRAJECTORY_PROTOCOL["protocol_id"],
        "task2_contract_sha256": args.task2_contract_sha256,
        "case_count_per_model": args.case_count,
        "expected_actual_browser_trajectory_count": (
            len(models) * args.case_count
        ),
        "actual_browser_trajectory_count": sum(
            int(row.get("summary", {}).get("pipeline_evaluable_case_count") or 0)
            for row in rows
        ),
        "inline_judge_call_count": 0,
        "fixed_external_judge_status": "PENDING",
        "alignment_preflight": {
            "path": str(args.out / "alignment_preflight.json"),
            "sha256": sha256_file(args.out / "alignment_preflight.json"),
        },
        "rows": rows,
        "inputs": {
            name: {
                "path": str(path),
                "sha256": sha256_file(path),
            }
            for name, path in (
                ("task2_contract", release["contract_path"]),
                ("route_probe", args.route_probe),
                ("resolver", args.resolver),
                ("observation_manifest", args.observation_manifest),
                ("prompts", args.prompts),
                ("codebook", args.codebook),
                ("runtime_materialization_report", args.runtime_materialization_report),
                ("docker_reference", args.docker_reference),
            )
        },
        "claim_boundary": {
            "task2_only": True,
            "task1_predictions_consumed": False,
            "human_gold_present": False,
            "accuracy_or_f1_allowed": False,
            "formal_evidence_score_allowed": False,
        },
    }
    manifest["manifest_sha256"] = embedded_hash(manifest, "manifest_sha256")
    atomic_json(args.out / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
