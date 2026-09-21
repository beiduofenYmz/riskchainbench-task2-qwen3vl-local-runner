#!/usr/bin/env python3
"""Frozen, fail-closed protocol contract for RiskChainBench Task 2."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


TASK2_BENCHMARK_ID = "riskchainbench-task2-controlled-web-investigation-v0.4"
TASK2_CONTRACT_SCHEMA = "riskchainbench-task2-contract/v0.2"
TASK2_PROTOCOL_ID = "riskchainbench-task2-trajectory-v0.4"
TASK2_CASE_INPUT_SCHEMA = "riskchainbench-task2-case-input/v0.2"
TASK2_CASE_COUNT = 600

EXPECTED_TASK2_CONTRACT_SHA256 = (
    "52b2e3e4bbca46fe6f007bdc7b553fa0b2f1f923c82a5b775faef09350e9ff42"
)

TASK2_TRAJECTORY_PROTOCOL: dict[str, Any] = {
    "protocol_id": TASK2_PROTOCOL_ID,
    "execution_mode": "TASK2_STANDALONE_SITE_ONLY",
    "actual_browser_trajectory_count_per_model_site": 1,
    "task1_content_model_visible": False,
    "task1_predictions_consumed": False,
    "model_output_scope": "TRAJECTORY_AND_EVIDENCE_PACKAGE_ONLY",
    "judgment_mode": "DEFERRED_FIXED_EXTERNAL_JUDGE",
    "inline_judge_allowed": False,
    "browser_backend": "BROWSERGYM_PLAYWRIGHT",
    "browser_harness_id": "riskchainbench-browsergym-playwright/v0.1",
    "browsergym_core_version": "0.14.3",
    "playwright_version": "1.44.0",
    "network_policy": "LOCAL_REPLAY_ONLY_ZERO_EGRESS",
    "screenshot_policy": "VIEWPORT_ONLY",
    "max_steps": 30,
    "max_case_seconds": 600,
    "max_tokens_per_call": 4096,
    "max_evidence_handoff_images": 8,
    "max_evidence_handoff_image_bytes": 5_500_000,
    "ordered_case_execution": True,
    "route_probe_required": True,
    "retry_policy": {
        "system_failure_max_retry_rounds": 2,
        "model_failure_retry": False,
    },
    "transport_policy": {
        "allowed": ["libinfer-neo", "openai-compatible"],
        "oneapi_allowed": False,
        "openai_compatible_wire_format": "CHAT_COMPLETIONS",
        "model_allowlist": False,
    },
    "decoding_policy_id": "riskchainbench-task2-strict-json-v0.4",
}

RELEASE_RELATIVE_PATHS = {
    "contract": Path("task2_contract.json"),
    "case_inputs": Path("model_visible/task2_case_inputs.jsonl"),
    "resolver": Path("evaluator_only/resolver.json"),
    "observation_manifest": Path(
        "evaluator_only/observation_plan_manifest.json"
    ),
    "docker_reference": Path("evaluator_only/docker_reference.jsonl"),
    "prompts": Path("spec/task2_trajectory_prompt.json"),
    "codebook": Path("spec/violation_codebook.json"),
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(
                f"expected JSON object at {path}:{line_number}"
            )
        rows.append(value)
    return rows


def checked_release_path(release_root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe Task 2 release path: {relative}")
    resolved_root = release_root.resolve()
    resolved = (release_root / path).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"Task 2 release path escapes root: {relative}")
    return resolved


def release_paths(release_root: Path) -> dict[str, Path]:
    return {
        name: (release_root / relative).resolve()
        for name, relative in RELEASE_RELATIVE_PATHS.items()
    }


def _validate_file_refs(
    contract: dict[str, Any],
    release_root: Path,
) -> None:
    files = contract.get("files")
    if not isinstance(files, dict):
        raise ValueError("Task 2 contract files map is missing")
    required_roles = {
        "case_inputs",
        "resolver",
        "observation_manifest",
        "spec_task2_trajectory_prompt.json",
        "spec_violation_codebook.json",
        "docker_reference",
    }
    missing = required_roles - set(files)
    if missing:
        raise ValueError(
            f"Task 2 contract is missing file roles: {sorted(missing)}"
        )
    for role, reference in files.items():
        if not isinstance(reference, dict):
            raise ValueError(f"invalid Task 2 file reference: {role}")
        path = checked_release_path(
            release_root,
            str(reference.get("path") or ""),
        )
        if (
            not path.is_file()
            or path.stat().st_size != int(reference.get("bytes") or -1)
            or sha256_file(path) != reference.get("sha256")
        ):
            raise ValueError(f"Task 2 file reference mismatch: {role}")


def _validate_case_order(
    contract: dict[str, Any],
    release_root: Path,
) -> None:
    paths = release_paths(release_root)
    resolver = read_json(paths["resolver"])
    bindings = resolver.get("bindings")
    if not isinstance(bindings, list) or len(bindings) != TASK2_CASE_COUNT:
        raise ValueError("Task 2 resolver must contain exactly 600 bindings")
    case_refs = [str(row.get("case_ref") or "") for row in bindings]
    if (
        not all(case_refs)
        or len(case_refs) != len(set(case_refs))
        or sha256_text("".join(f"{value}\n" for value in case_refs))
        != contract.get("ordered_case_refs_sha256")
    ):
        raise ValueError("Task 2 resolver order does not match the contract")

    case_inputs = read_jsonl(paths["case_inputs"])
    if len(case_inputs) != TASK2_CASE_COUNT:
        raise ValueError("Task 2 model-visible input must contain 600 rows")
    forbidden = {
        "entry_value",
        "entry_value_sha256",
        "label",
        "risk_type",
        "site_id",
        "violation_type",
    }
    for ordinal, (row, case_ref) in enumerate(
        zip(case_inputs, case_refs, strict=True),
        1,
    ):
        if (
            row.get("schema_version") != TASK2_CASE_INPUT_SCHEMA
            or row.get("ordinal") != ordinal
            or row.get("case_ref") != case_ref
            or row.get("execution_mode")
            != TASK2_TRAJECTORY_PROTOCOL["execution_mode"]
            or row.get("judgment_mode")
            != TASK2_TRAJECTORY_PROTOCOL["judgment_mode"]
            or row.get("actual_browser_trajectory_count") != 1
            or row.get("task1_content_model_visible") is not False
            or row.get("case_input_sha256")
            != embedded_hash(row, "case_input_sha256")
            or forbidden.intersection(row)
        ):
            raise ValueError(
                f"Task 2 model-visible row is not frozen: ordinal={ordinal}"
            )


def validate_task2_release(
    release_root: Path,
    *,
    require_frozen_hash: bool = True,
    verify_files: bool = True,
) -> dict[str, Any]:
    """Validate the exact Task 2 contract before any endpoint is accessed."""

    paths = release_paths(release_root)
    contract_path = paths["contract"]
    if not contract_path.is_file():
        raise ValueError(f"Task 2 contract is missing: {contract_path}")
    contract = read_json(contract_path)
    observed_hash = str(contract.get("contract_sha256") or "")
    if (
        contract.get("schema_version") != TASK2_CONTRACT_SCHEMA
        or contract.get("benchmark_id") != TASK2_BENCHMARK_ID
        or contract.get("case_count") != TASK2_CASE_COUNT
        or observed_hash != embedded_hash(contract, "contract_sha256")
        or contract.get("trajectory_protocol") != TASK2_TRAJECTORY_PROTOCOL
        or contract.get("human_gold_status")
        != "PENDING_REAL_ANNOTATOR_SUBMISSIONS"
        or contract.get("label_claim_allowed") is not False
    ):
        raise ValueError("Task 2 contract identity or protocol mismatch")
    if require_frozen_hash:
        if EXPECTED_TASK2_CONTRACT_SHA256 == "__TO_BE_FROZEN__":
            raise ValueError("Task 2 contract hash has not been frozen")
        if observed_hash != EXPECTED_TASK2_CONTRACT_SHA256:
            raise ValueError("Task 2 contract hash mismatch")
    if verify_files:
        _validate_file_refs(contract, release_root)
        _validate_case_order(contract, release_root)
    return {
        "contract": contract,
        "contract_path": contract_path,
        "contract_sha256": observed_hash,
        "paths": paths,
    }


def assert_protocol_runtime(
    *,
    contract: dict[str, Any],
    browser_harness_id: str,
    browsergym_core_version: str,
    playwright_version: str,
    max_steps: int,
    max_case_seconds: int,
    max_tokens: int,
    max_evidence_images: int,
    max_evidence_image_bytes: int,
    viewport_only: bool,
    run_inline_judge: bool,
) -> None:
    protocol = contract.get("trajectory_protocol") or {}
    observed = {
        "browser_harness_id": browser_harness_id,
        "browsergym_core_version": browsergym_core_version,
        "playwright_version": playwright_version,
        "max_steps": max_steps,
        "max_case_seconds": max_case_seconds,
        "max_tokens_per_call": max_tokens,
        "max_evidence_handoff_images": max_evidence_images,
        "max_evidence_handoff_image_bytes": max_evidence_image_bytes,
        "screenshot_policy": "VIEWPORT_ONLY" if viewport_only else "FULL_PAGE",
        "inline_judge_allowed": run_inline_judge,
    }
    expected = {key: protocol.get(key) for key in observed}
    if observed != expected:
        raise ValueError(
            "Task 2 runtime does not match the frozen trajectory protocol: "
            f"expected={expected!r}, observed={observed!r}"
        )
