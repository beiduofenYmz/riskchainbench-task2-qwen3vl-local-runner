#!/usr/bin/env python3
"""Build fail-closed Task 1 gates for offline end-to-end composition.

The standalone Task 2 browser runner does not consume these predictions.  The
gate manifest is applied after trajectory collection so Task 1 failures remain
non-investigable without duplicating Task 2 browser runs.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESOLVER = (
    PROJECT_ROOT
    / "outputs/riskchainbench_balanced600_v0.3/scale_600/private/resolver.json"
)


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


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object at {path}:{line_number}")
        rows.append(value)
    return rows


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


def entry_sha256(value: Any) -> str:
    normalized = str(value or "").strip().lower().rstrip("/")
    return sha256_text(normalized) if normalized else ""


def resolve_artifact_path(value: Any) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else PROJECT_ROOT / path


def missing_prediction_status(task1_run: Path, sample_id: str) -> tuple[str, str]:
    state_path = task1_run / "tasks" / sample_id / "state.json"
    if not state_path.is_file():
        return "TASK1_NOT_RUN", "state_missing"
    state = read_json(state_path)
    attempt_path = resolve_artifact_path(state.get("attempt_path"))
    call_path = attempt_path / "model_call.json"
    if not call_path.is_file():
        return "TASK1_SYSTEM_FAILURE", str(state.get("error_type") or "call_missing")
    call = read_json(call_path)
    attempts = [row for row in call.get("attempts") or [] if isinstance(row, dict)]
    provider_observation = any(
        any(
            key in row and row.get(key) is not None
            for key in (
                "response_content",
                "response_id",
                "response_model",
                "finish_reason",
                "usage",
            )
        )
        for row in attempts
    )
    if provider_observation:
        return "TASK1_MODEL_FAILURE", str(state.get("error_type") or "model_output")
    return "TASK1_SYSTEM_FAILURE", str(state.get("error_type") or "transport")


def build_handoff(
    *,
    resolver: dict[str, Any],
    predictions: list[dict[str, Any]],
    task1_run: Path,
    model: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bindings = resolver.get("bindings") or []
    if resolver.get("schema_version") != "benchmark-private-resolver/v0.2":
        raise ValueError("unsupported resolver schema")
    if int(resolver.get("binding_count") or -1) != len(bindings):
        raise ValueError("resolver binding count mismatch")
    prediction_by_id: dict[str, dict[str, Any]] = {}
    for prediction in predictions:
        sample_id = str(prediction.get("sample_id") or "")
        if not sample_id or sample_id in prediction_by_id:
            raise ValueError(f"duplicate or missing prediction sample_id: {sample_id}")
        prediction_by_id[sample_id] = prediction

    authorized: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for ordinal, binding in enumerate(bindings, 1):
        sample_ids = binding.get("sample_ids") or []
        if not sample_ids:
            raise ValueError(f"resolver binding lacks sample_ids: {ordinal}")
        sample_id = str(sample_ids[0])
        prediction = prediction_by_id.get(sample_id)
        detail = ""
        predicted_hash = ""
        if prediction is None:
            status, detail = missing_prediction_status(task1_run, sample_id)
        elif prediction.get("model_id") != model:
            status = "TASK1_MODEL_ID_MISMATCH"
            detail = str(prediction.get("model_id") or "")
        elif prediction.get("abstain") is True:
            status = "TASK1_MODEL_ABSTAIN"
            detail = "abstain_true"
        else:
            candidates = prediction.get("entry_candidates") or []
            top1 = candidates[0].get("value") if candidates else None
            predicted_hash = entry_sha256(top1)
            if not predicted_hash:
                status = "TASK1_NO_TOP1_ENTRY"
                detail = "entry_candidates_empty"
            elif predicted_hash != str(binding.get("entry_value_sha256") or ""):
                status = "TASK1_WRONG_TOP1_ENTRY"
                detail = "top1_hash_mismatch"
            else:
                status = "AUTHORIZED"
                authorized.append(prediction)
        rows.append(
            {
                "ordinal": ordinal,
                "case_ref": binding["case_ref"],
                "site_id": binding["site_id"],
                "sample_id": sample_id,
                "status": status,
                "detail": detail,
                "predicted_top1_sha256": predicted_hash or None,
                "expected_entry_sha256": binding.get("entry_value_sha256"),
            }
        )
    counts = Counter(row["status"] for row in rows)
    manifest = {
        "schema_version": "task1-task2-handoff/v0.1",
        "created_at": utc_now(),
        "status": (
            "PASS"
            if len(authorized) == len(bindings)
            else "PASS_WITH_TASK1_GATES"
        ),
        "model": model,
        "transport": "libinfer-neo",
        "oneapi_used": False,
        "source_count": len(bindings),
        "authorized_case_count": len(authorized),
        "gated_case_count": len(bindings) - len(authorized),
        "status_counts": dict(sorted(counts.items())),
        "rows": rows,
        "claim_boundary": {
            "task2_standalone_runner_consumes_predictions": False,
            "offline_composition_reuses_task2_only_for_authorized_cases": True,
            "resolver_entry_value_exposed_to_model": False,
            "wrong_task1_predictions_replaced_with_gold": False,
            "task1_model_failures_preserved": True,
        },
    }
    return authorized, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolver", type=Path, default=DEFAULT_RESOLVER)
    parser.add_argument("--task1-run", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = read_json(args.task1_run / "config.json")
        model = args.model or str(config.get("requested_model") or "")
        if not model:
            raise ValueError("Task 1 model is missing")
        if config.get("requested_model") != model:
            raise ValueError("requested model differs from Task 1 frozen config")
        predictions_path = args.task1_run / "predictions.jsonl"
        resolver = read_json(args.resolver)
        predictions = read_jsonl(predictions_path)
        authorized, manifest = build_handoff(
            resolver=resolver,
            predictions=predictions,
            task1_run=args.task1_run,
            model=model,
        )
        authorized_path = args.out_dir / "authorized_primary_predictions.jsonl"
        atomic_jsonl(authorized_path, authorized)
        manifest["inputs"] = {
            "resolver": {
                "path": str(args.resolver),
                "sha256": sha256_file(args.resolver),
            },
            "task1_config": {
                "path": str(args.task1_run / "config.json"),
                "sha256": sha256_file(args.task1_run / "config.json"),
            },
            "task1_predictions": {
                "path": str(predictions_path),
                "sha256": sha256_file(predictions_path),
            },
        }
        manifest["authorized_predictions"] = {
            "path": str(authorized_path),
            "sha256": sha256_file(authorized_path),
            "count": len(authorized),
        }
        manifest["manifest_sha256"] = sha256_text(canonical_json(manifest))
        atomic_json(args.out_dir / "handoff_manifest.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
