#!/usr/bin/env python3
"""Independently validate autonomous multimodal Task 2 artifacts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from task2_frozen_protocol import (  # noqa: E402
    EXPECTED_TASK2_CONTRACT_SHA256,
    TASK2_TRAJECTORY_PROTOCOL,
    validate_task2_release,
)

CASE_SCHEMA = "task2-autonomous-mllm-case/v0.4"
CONFIG_SCHEMA = "task2-autonomous-mllm-config/v0.4"
SUMMARY_SCHEMA = "task2-autonomous-mllm-summary/v0.4"
VALIDATION_SCHEMA = "task2-autonomous-mllm-validation/v0.4"
BROWSER_TRACK = "BROWSERGYM_MULTIMODAL_BROWSER"
BROWSER_BACKEND = "BROWSERGYM_PLAYWRIGHT"
BROWSER_HARNESS_ID = "riskchainbench-browsergym-playwright/v0.1"
BROWSERGYM_CORE_VERSION = "0.14.3"
PLAYWRIGHT_VERSION = "1.44.0"
EXECUTION_MODE = "TASK2_STANDALONE_SITE_ONLY"
DEFERRED_JUDGMENT_MODE = "DEFERRED_FIXED_EXTERNAL_JUDGE"
INLINE_JUDGMENT_MODE = "INLINE_SELF_JUDGE_DIAGNOSTIC"
DECISIONS = {"VIOLATION", "NON_VIOLATION", "INSUFFICIENT_EVIDENCE"}
CASE_STATUSES = {"PASS", "MODEL_FAILURE"}
MODEL_FAILURE_KINDS = {
    "REFUSAL",
    "INVALID_RESPONSE",
    "ABSTAIN",
    "MAX_STEPS",
}
REFUSAL_MARKERS = (
    "i'm sorry, but i cannot assist",
    "i’m sorry, but i cannot assist",
    "i cannot assist with that request",
    "i can't assist with that request",
    "i can’t assist with that request",
    "unable to assist with that request",
    "cannot help with that request",
    "can't help with that request",
    "can’t help with that request",
    "无法协助",
    "不能协助",
    "无法帮助",
    "不能帮助",
)
FORBIDDEN_MODEL_KEYS = {
    "selector",
    "scenario_id",
    "sampling_stratum",
    "site_id",
    "site_ref",
    "fixture_value",
    "fixture_values",
    "entry_value",
    "expected_entry",
    "hidden_label",
    "gold_label",
    "reconstructed_intent",
    "committed_restoration",
    "restored_message",
    "task1_message",
    "task1_intent",
    "entry_candidates",
}


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
    return sha256_text(
        canonical_json({key: child for key, child in value.items() if key != field})
    )


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def nested_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


def string_leaves(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from string_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from string_leaves(child)
    elif isinstance(value, str):
        yield value


def find_scenario(source: dict[str, Any], scenario_id: str) -> dict[str, Any] | None:
    for row in (source.get("verification") or {}).get("scenarios") or []:
        if row.get("id") == scenario_id:
            return row
    return None


def classify_failed_call(audit: dict[str, Any]) -> str:
    response_texts = [
        str(row.get("response_content") or "").strip()
        for row in audit.get("attempts") or []
        if str(row.get("response_content") or "").strip()
    ]
    if not response_texts:
        return "TRANSPORT_ERROR"
    normalized = "\n".join(response_texts).casefold()
    if any(marker in normalized for marker in REFUSAL_MARKERS):
        return "REFUSAL"
    return "INVALID_RESPONSE"


def expected_model_parameter_policy(
    model: str,
    transport: str = "libinfer-neo",
) -> dict[str, Any]:
    if transport == "openai-compatible":
        required_options = {} if model.startswith("gpt-5.") else {"temperature": 0}
        return {
            "temperature_policy": (
                "provider_default_omitted"
                if model.startswith("gpt-5.")
                else "explicit_zero"
            ),
            "reasoning_effort_policy": "provider_default_omitted",
            "thinking_policy": "provider_default",
            "json_mode_policy": "prompt_only",
            "required_options": required_options,
            "forbidden_options": {
                "response_format",
                "reasoning_effort",
                "enable_thinking",
                "thinking",
            },
        }
    if transport != "libinfer-neo":
        raise ValueError("unsupported model transport")
    if model.startswith("gpt-5."):
        return {
            "temperature_policy": "provider_default_omitted",
            "reasoning_effort_policy": "provider_default_omitted",
            "thinking_policy": "provider_default",
            "json_mode_policy": "response_format_json_object",
            "required_options": {"response_format": {"type": "json_object"}},
            "forbidden_options": {
                "temperature",
                "reasoning_effort",
                "enable_thinking",
                "thinking",
            },
        }
    required_options: dict[str, Any] = {
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "reasoning_effort": "minimal",
    }
    thinking_policy = "provider_default"
    if model.startswith("qwen"):
        thinking_policy = "enable_thinking_false"
        required_options["enable_thinking"] = False
    elif model.startswith("kimi-"):
        thinking_policy = "thinking_type_disabled"
        required_options["thinking"] = {"type": "disabled"}
    return {
        "temperature_policy": "explicit_zero",
        "reasoning_effort_policy": "explicit_minimal",
        "thinking_policy": thinking_policy,
        "json_mode_policy": "response_format_json_object",
        "required_options": required_options,
        "forbidden_options": (
            {"thinking"}
            if thinking_policy == "enable_thinking_false"
            else {"enable_thinking"}
            if thinking_policy == "thinking_type_disabled"
            else {"enable_thinking", "thinking"}
        ),
    }


def model_call_parameter_policy_matches(
    call: dict[str, Any],
    model: str,
    transport: str = "libinfer-neo",
) -> bool:
    expected = expected_model_parameter_policy(model, transport)
    options = call.get("model_request_options")
    if not isinstance(options, dict):
        return False
    return bool(
        call.get("temperature_policy") == expected["temperature_policy"]
        and call.get("reasoning_effort_policy")
        == expected["reasoning_effort_policy"]
        and call.get("thinking_policy") == expected["thinking_policy"]
        and call.get("json_mode_policy") == expected["json_mode_policy"]
        and all(
            options.get(key) == value
            for key, value in expected["required_options"].items()
        )
        and not (set(options) & expected["forbidden_options"])
    )


@dataclass
class CheckLedger:
    total: int = 0
    passed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)

    def require(
        self,
        condition: bool,
        code: str,
        *,
        case_ref: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.total += 1
        if condition:
            self.passed += 1
            return
        row: dict[str, Any] = {"code": code}
        if case_ref is not None:
            row["case_ref"] = case_ref
        if detail is not None:
            row["detail"] = detail[:1000]
        self.failures.append(row)


def config_fingerprint(config: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in config.items()
        if key not in {"created_at", "config_fingerprint", "source_snapshots"}
    }
    payload["source_snapshots"] = [
        {
            "source_path": row["source_path"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
        }
        for row in config.get("source_snapshots") or []
    ]
    return sha256_text(canonical_json(payload))


def validate_judgment(
    ledger: CheckLedger,
    *,
    value: Any,
    codebook: dict[str, Any],
    evidence_ids: set[str],
    case_ref: str,
) -> None:
    ledger.require(isinstance(value, dict), "JUDGMENT_OBJECT", case_ref=case_ref)
    if not isinstance(value, dict):
        return
    required = {
        "environment_status",
        "decision",
        "primary_violation_type",
        "secondary_violation_types",
        "evidence",
        "risk_reason",
        "counterevidence",
        "confidence",
        "evidence_sufficiency",
        "note",
    }
    ledger.require(set(value) == required, "JUDGMENT_KEYS", case_ref=case_ref)
    decision = value.get("decision")
    primary = value.get("primary_violation_type")
    ledger.require(decision in DECISIONS, "JUDGMENT_DECISION", case_ref=case_ref)
    concrete = {
        row["id"]
        for row in codebook.get("primary_violation_types") or []
        if row.get("id") not in {"NONE", "UNKNOWN"}
    }
    if decision == "VIOLATION":
        ledger.require(
            primary in concrete,
            "JUDGMENT_VIOLATION_PRIMARY",
            case_ref=case_ref,
        )
    elif decision == "NON_VIOLATION":
        ledger.require(
            primary == "NONE",
            "JUDGMENT_NON_VIOLATION_PRIMARY",
            case_ref=case_ref,
        )
    elif decision == "INSUFFICIENT_EVIDENCE":
        ledger.require(
            primary == "UNKNOWN",
            "JUDGMENT_INSUFFICIENT_PRIMARY",
            case_ref=case_ref,
        )
        ledger.require(
            value.get("evidence_sufficiency") == "INSUFFICIENT",
            "JUDGMENT_INSUFFICIENT_CONSISTENCY",
            case_ref=case_ref,
        )
    anchors = {
        row["id"] for row in codebook.get("evidence_anchor_types") or []
    }
    note = value.get("note")
    ledger.require(
        isinstance(note, str) and len(note) <= 1000,
        "JUDGMENT_NOTE",
        case_ref=case_ref,
    )
    if primary == "OTHER_PROHIBITED_ACTIVITY":
        ledger.require(
            isinstance(note, str) and len(note.strip()) >= 10,
            "JUDGMENT_OTHER_NOTE",
            case_ref=case_ref,
        )
    for field_name, require_nonempty in (("evidence", True), ("counterevidence", False)):
        rows = value.get(field_name)
        ledger.require(
            isinstance(rows, list) and (bool(rows) or not require_nonempty),
            f"JUDGMENT_{field_name.upper()}_ARRAY",
            case_ref=case_ref,
        )
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            detail = f"{field_name}[{index}]"
            ledger.require(
                isinstance(row, dict)
                and set(row) == {"evidence_id", "anchor_type", "observation"},
                "JUDGMENT_CITATION_SHAPE",
                case_ref=case_ref,
                detail=detail,
            )
            if not isinstance(row, dict):
                continue
            ledger.require(
                row.get("evidence_id") in evidence_ids,
                "JUDGMENT_CITATION_EVIDENCE_ID",
                case_ref=case_ref,
                detail=detail,
            )
            ledger.require(
                row.get("anchor_type") in anchors,
                "JUDGMENT_CITATION_ANCHOR",
                case_ref=case_ref,
                detail=detail,
            )
            ledger.require(
                isinstance(row.get("observation"), str)
                and bool(row["observation"].strip()),
                "JUDGMENT_CITATION_OBSERVATION",
                case_ref=case_ref,
                detail=detail,
            )


def validate_model_payload(
    ledger: CheckLedger,
    *,
    payload: dict[str, Any],
    source: dict[str, Any],
    scenario_id: str,
    case_ref: str,
    phase: str,
    public_fixture_values: set[str] | None = None,
) -> None:
    keys = {key.lower() for key in nested_keys(payload)}
    for key in FORBIDDEN_MODEL_KEYS:
        ledger.require(
            key not in keys,
            "MODEL_PAYLOAD_FORBIDDEN_KEY",
            case_ref=case_ref,
            detail=f"{phase}:{key}",
        )
    leaves = set(string_leaves(payload))
    ledger.require(
        scenario_id not in leaves,
        "MODEL_PAYLOAD_SCENARIO_ID_LEAK",
        case_ref=case_ref,
        detail=phase,
    )
    scenario = find_scenario(source, scenario_id)
    ledger.require(
        scenario is not None,
        "HIDDEN_SCENARIO_RESOLVES",
        case_ref=case_ref,
    )
    if scenario is None:
        return
    fixtures = {
        str(step["value"])
        for step in scenario.get("steps") or []
        if "value" in step
    }
    for fixture in fixtures:
        if fixture in (public_fixture_values or set()):
            continue
        ledger.require(
            fixture not in leaves,
            "MODEL_PAYLOAD_FIXTURE_LEAK",
            case_ref=case_ref,
            detail=phase,
        )
    selectors = {
        str(step["selector"])
        for step in scenario.get("steps") or []
        if "selector" in step and len(str(step["selector"])) >= 8
    }
    for selector in selectors:
        ledger.require(
            selector not in leaves,
            "MODEL_PAYLOAD_SELECTOR_LEAK",
            case_ref=case_ref,
            detail=phase,
        )


def validate_case(
    ledger: CheckLedger,
    *,
    result_path: Path,
    config: dict[str, Any],
    codebook: dict[str, Any],
) -> None:
    result = read_json(result_path)
    case_ref = str(result.get("case_ref") or result_path.parent.name)
    ledger.require(
        result.get("schema_version") == CASE_SCHEMA,
        "CASE_SCHEMA",
        case_ref=case_ref,
    )
    case_budget = int(config.get("max_case_seconds") or 0)
    ledger.require(
        result.get("case_wall_budget_seconds") == case_budget
        and isinstance(result.get("case_elapsed_seconds"), (int, float))
        and 0 <= float(result.get("case_elapsed_seconds") or -1)
        <= case_budget + 10,
        "CASE_WALL_BUDGET",
        case_ref=case_ref,
    )
    status = result.get("status")
    judgment_mode = str(config.get("judgment_mode") or "")
    ledger.require(
        result.get("execution_mode") == EXECUTION_MODE
        and result.get("evaluation_condition") == "task2_standalone"
        and result.get("judgment_mode") == judgment_mode
        and judgment_mode
        in {DEFERRED_JUDGMENT_MODE, INLINE_JUDGMENT_MODE},
        "CASE_STANDALONE_EXECUTION_MODE",
        case_ref=case_ref,
    )
    ledger.require(status in CASE_STATUSES, "CASE_STATUS", case_ref=case_ref)
    ledger.require(
        result.get("pipeline_evaluable") is True,
        "CASE_PIPELINE_EVALUABLE",
        case_ref=case_ref,
    )
    model_failures = result.get("model_failures") or []
    ledger.require(
        (status == "PASS" and not model_failures)
        or (status == "MODEL_FAILURE" and bool(model_failures)),
        "CASE_MODEL_FAILURE_STATUS_CONSISTENCY",
        case_ref=case_ref,
    )
    ledger.require(
        result.get("failure") is None,
        "CASE_SYSTEM_FAILURE_ABSENT",
        case_ref=case_ref,
    )
    for index, row in enumerate(model_failures):
        ledger.require(
            isinstance(row, dict)
            and row.get("kind") in MODEL_FAILURE_KINDS
            and row.get("phase") in {"web_action", "final_judge"},
            "CASE_MODEL_FAILURE_RECORD",
            case_ref=case_ref,
            detail=str(index),
        )
        if judgment_mode == DEFERRED_JUDGMENT_MODE:
            ledger.require(
                row.get("phase") == "web_action",
                "DEFERRED_MODE_NO_JUDGE_FAILURE",
                case_ref=case_ref,
                detail=str(index),
            )
    ledger.require(
        result.get("track") == BROWSER_TRACK,
        "CASE_TRACK",
        case_ref=case_ref,
    )
    model = config["model"]
    resolved_model_allowlist = set(
        config.get("resolved_model_allowlist") or [model]
    )
    ledger.require(
        result.get("requested_model") == model,
        "CASE_REQUESTED_MODEL",
        case_ref=case_ref,
    )
    ledger.require(
        set(result.get("resolved_models") or []) == resolved_model_allowlist,
        "CASE_RESOLVED_MODEL",
        case_ref=case_ref,
    )
    ledger.require(
        result.get("transport") == config.get("transport"),
        "CASE_TRANSPORT",
        case_ref=case_ref,
    )
    binding = result.get("standalone_binding") or {}
    ledger.require(
        binding.get("case_ref") == case_ref
        and binding.get("sample_id") == result.get("sample_id")
        and binding.get("controller_source") == "FROZEN_PRIVATE_RESOLVER"
        and binding.get("task1_prediction_required") is False
        and binding.get("model_visible") is False
        and "task1_handoff" not in result,
        "TASK2_STANDALONE_BINDING",
        case_ref=case_ref,
    )
    boundary = result.get("web_input_boundary") or {}
    ledger.require(
        boundary.get("policy") == "SITE_ONLY_EVIDENCE_INVESTIGATION"
        and boundary.get("task1_message_model_visible") is False
        and boundary.get("task1_intent_model_visible") is False
        and boundary.get("task1_entry_model_visible") is False
        and boundary.get("local_binding_model_visible") is False
        and boundary.get("paired_browser_conditions_executed") is False
        and boundary.get("task2_executed_once_per_model_site") is True
        and boundary.get("fixed_external_judge_deferred") is True,
        "TASK2_SITE_ONLY_WEB_INPUT_BOUNDARY",
        case_ref=case_ref,
    )
    browser = result.get("browser_execution") or {}
    ledger.require(
        browser.get("browser_backend") == BROWSER_BACKEND
        and browser.get("browser_harness_id") == BROWSER_HARNESS_ID
        and browser.get("browsergym_core_version") == BROWSERGYM_CORE_VERSION
        and browser.get("playwright_version") == PLAYWRIGHT_VERSION
        and browser.get("browsergym_action_execution") is True,
        "BROWSER_FROZEN_HARNESS",
        case_ref=case_ref,
    )
    for key in (
        "autonomous_action_selection",
        "unmarked_model_screenshots",
        "hidden_scenario_not_model_visible",
        "synthetic_fixture_values_not_model_visible",
    ):
        ledger.require(
            browser.get(key) is True,
            f"BROWSER_{key.upper()}",
            case_ref=case_ref,
        )
    if status == "PASS":
        ledger.require(
            browser.get("hidden_protocol_complete") is True,
            "BROWSER_HIDDEN_PROTOCOL_COMPLETE",
            case_ref=case_ref,
        )
        ledger.require(
            browser.get("model_stop_status") == "COMPLETE",
            "BROWSER_MODEL_STOP_COMPLETE",
            case_ref=case_ref,
        )
        ledger.require(
            browser.get("remaining_hidden_action_count") == 0,
            "BROWSER_HIDDEN_ACTIONS_COMPLETE",
            case_ref=case_ref,
        )
    else:
        ledger.require(
            browser.get("model_stop_status")
            in MODEL_FAILURE_KINDS | {"COMPLETE"},
            "BROWSER_MODEL_FAILURE_STOP_STATUS",
            case_ref=case_ref,
        )
    ledger.require(
        browser.get("external_request_attempt_count") == 0,
        "BROWSER_EXTERNAL_REQUESTS_ZERO",
        case_ref=case_ref,
    )
    actions = browser.get("actions") or []
    for index, action in enumerate(actions):
        ledger.require(
            action.get("decision_source") == "MODEL",
            "ACTION_DECISION_SOURCE",
            case_ref=case_ref,
            detail=str(index),
        )
        ledger.require(
            action.get("fixture_value_sent_to_model") is False,
            "ACTION_FIXTURE_HIDDEN",
            case_ref=case_ref,
            detail=str(index),
        )
        execution = action.get("controller_execution") or {}
        execution_status = execution.get("execution_status")
        ledger.require(
            execution_status in {"EXECUTED", "NO_STATE_CHANGE", "ACTION_ERROR"},
            "ACTION_EXECUTION_STATUS",
            case_ref=case_ref,
            detail=str(index),
        )
        if execution_status == "NO_STATE_CHANGE":
            ledger.require(
                action.get("page_changed") is False
                and execution.get("hidden_protocol_advanced") is False,
                "ACTION_NO_STATE_CHANGE_CONSISTENCY",
                case_ref=case_ref,
                detail=str(index),
            )
        if execution_status == "ACTION_ERROR":
            ledger.require(
                execution.get("hidden_protocol_advanced") is False
                and execution.get("browsergym_action_error")
                in {
                    "ELEMENT_INTERCEPTED",
                    "ELEMENT_NOT_VISIBLE",
                    "LOCATOR_AMBIGUOUS",
                    "ACTION_TIMEOUT",
                    "ELEMENT_DETACHED",
                    "ACTION_EXECUTION_ERROR",
                }
                and len(str(execution.get("browsergym_action_error_sha256") or ""))
                == 64,
                "ACTION_ERROR_CONSISTENCY",
                case_ref=case_ref,
                detail=str(index),
            )
    source_path = resolve_path(browser["hidden_protocol_source_path"])
    ledger.require(source_path.is_file(), "HIDDEN_SOURCE_EXISTS", case_ref=case_ref)
    if source_path.is_file():
        ledger.require(
            sha256_file(source_path) == browser["hidden_protocol_source_sha256"],
            "HIDDEN_SOURCE_HASH",
            case_ref=case_ref,
        )
        source = read_json(source_path)
    else:
        source = {}
    artifact_refs = result.get("artifact_refs") or {}
    artifacts: dict[str, dict[str, Any]] = {}
    for artifact_name in (
        "trajectory",
        "network_audit",
        "runtime_attestation",
        "fixed_judge_handoff",
        "evidence_package",
    ):
        reference = artifact_refs.get(artifact_name) or {}
        artifact_path = resolve_path(reference.get("path") or "")
        ledger.require(
            artifact_path.is_file(),
            "CASE_ARTIFACT_EXISTS",
            case_ref=case_ref,
            detail=artifact_name,
        )
        if artifact_path.is_file():
            ledger.require(
                sha256_file(artifact_path) == reference.get("sha256"),
                "CASE_ARTIFACT_HASH",
                case_ref=case_ref,
                detail=artifact_name,
            )
            artifacts[artifact_name] = read_json(artifact_path)

    trajectory = artifacts.get("trajectory") or {}
    ledger.require(
        trajectory.get("schema_version") == "riskchainbench-browser-trajectory/v0.1"
        and trajectory.get("case_ref") == case_ref
        and trajectory.get("trajectory_sha256")
        == embedded_hash(trajectory, "trajectory_sha256"),
        "TRAJECTORY_IDENTITY_AND_HASH",
        case_ref=case_ref,
    )
    ledger.require(
        trajectory.get("case_wall_budget_seconds") == case_budget
        and isinstance(trajectory.get("case_elapsed_seconds"), (int, float)),
        "TRAJECTORY_WALL_BUDGET",
        case_ref=case_ref,
    )
    trajectory_harness = trajectory.get("browser_harness") or {}
    ledger.require(
        trajectory_harness.get("backend") == BROWSER_BACKEND
        and trajectory_harness.get("harness_id") == BROWSER_HARNESS_ID
        and trajectory_harness.get("browsergym_core_version")
        == BROWSERGYM_CORE_VERSION
        and trajectory_harness.get("playwright_version") == PLAYWRIGHT_VERSION
        and trajectory.get("actions") == browser.get("actions"),
        "TRAJECTORY_BROWSER_BINDING",
        case_ref=case_ref,
    )

    network_audit = artifacts.get("network_audit") or {}
    ledger.require(
        network_audit.get("schema_version")
        == "riskchainbench-zero-egress-audit/v0.1"
        and network_audit.get("case_ref") == case_ref
        and network_audit.get("audit_sha256")
        == embedded_hash(network_audit, "audit_sha256"),
        "NETWORK_AUDIT_IDENTITY_AND_HASH",
        case_ref=case_ref,
    )
    ledger.require(
        network_audit.get("policy") == "LOCAL_REPLAY_ONLY"
        and network_audit.get("status") == "PASS"
        and network_audit.get("zero_external_request_attempts") is True
        and network_audit.get("external_request_attempt_count") == 0
        and not network_audit.get("external_request_attempt_sha256s"),
        "NETWORK_AUDIT_ZERO_EGRESS",
        case_ref=case_ref,
    )

    runtime_attestation = artifacts.get("runtime_attestation") or {}
    ledger.require(
        runtime_attestation.get("schema_version")
        == "riskchainbench-task2-runtime-attestation/v0.1"
        and runtime_attestation.get("case_ref") == case_ref
        and runtime_attestation.get("attestation_sha256")
        == embedded_hash(runtime_attestation, "attestation_sha256"),
        "RUNTIME_ATTESTATION_IDENTITY_AND_HASH",
        case_ref=case_ref,
    )
    ledger.require(
        runtime_attestation.get("browser_backend") == BROWSER_BACKEND
        and runtime_attestation.get("browser_harness_id") == BROWSER_HARNESS_ID
        and runtime_attestation.get("browsergym_core_version")
        == BROWSERGYM_CORE_VERSION
        and runtime_attestation.get("playwright_version") == PLAYWRIGHT_VERSION
        and runtime_attestation.get("expected_browsergym_core_version")
        == BROWSERGYM_CORE_VERSION
        and runtime_attestation.get("expected_playwright_version")
        == PLAYWRIGHT_VERSION,
        "RUNTIME_ATTESTATION_FROZEN_BROWSER",
        case_ref=case_ref,
    )
    ledger.require(
        runtime_attestation.get("case_wall_budget_seconds") == case_budget,
        "RUNTIME_ATTESTATION_WALL_BUDGET",
        case_ref=case_ref,
    )
    ledger.require(
        runtime_attestation.get("runtime_binary_sha256")
        == (result.get("runtime_integrity") or {}).get(
            "observed_runtime_binary_sha256"
        )
        and runtime_attestation.get("source_path_sha256")
        == (
            sha256_file(source_path)
            if source_path.is_file()
            else None
        )
        and runtime_attestation.get("prompt_sha256")
        == (config.get("inputs") or {}).get("prompts", {}).get("sha256")
        and runtime_attestation.get("codebook_sha256")
        == (config.get("inputs") or {}).get("codebook", {}).get("sha256"),
        "RUNTIME_ATTESTATION_INPUT_BINDING",
        case_ref=case_ref,
    )
    fixed_judge_handoff = artifacts.get("fixed_judge_handoff") or {}
    ledger.require(
        fixed_judge_handoff.get("schema_version")
        == "riskchainbench-fixed-judge-handoff/v0.1"
        and fixed_judge_handoff.get("case_ref") == case_ref
        and fixed_judge_handoff.get("sample_id") == result.get("sample_id")
        and fixed_judge_handoff.get("tested_model") == model
        and fixed_judge_handoff.get("transport") == config.get("transport")
        and fixed_judge_handoff.get("execution_mode") == EXECUTION_MODE
        and fixed_judge_handoff.get("judgment_mode") == judgment_mode
        and fixed_judge_handoff.get("handoff_sha256")
        == embedded_hash(fixed_judge_handoff, "handoff_sha256"),
        "FIXED_JUDGE_HANDOFF_IDENTITY_AND_HASH",
        case_ref=case_ref,
    )
    expected_handoff_status = (
        "PENDING_FIXED_EXTERNAL_JUDGE"
        if judgment_mode == DEFERRED_JUDGMENT_MODE
        else (
            "INLINE_DIAGNOSTIC_COMPLETE"
            if result.get("judgment") is not None
            else "PENDING_FIXED_EXTERNAL_JUDGE"
        )
    )
    ledger.require(
        fixed_judge_handoff.get("status") == expected_handoff_status,
        "FIXED_JUDGE_HANDOFF_STATUS",
        case_ref=case_ref,
    )
    for name, reference in (
        fixed_judge_handoff.get("artifact_refs") or {}
    ).items():
        path = resolve_path(reference.get("path") or "")
        ledger.require(
            path.is_file()
            and sha256_file(path) == reference.get("sha256")
            and path.stat().st_size == reference.get("size_bytes"),
            "FIXED_JUDGE_HANDOFF_ARTIFACT_HASH",
            case_ref=case_ref,
            detail=str(name),
        )
    evidence_package = artifacts.get("evidence_package") or {}
    ledger.require(
        evidence_package.get("schema_version")
        == "riskchainbench-evidence-package/v0.1"
        and evidence_package.get("case_ref") == case_ref
        and evidence_package.get("evidence_package_sha256")
        == embedded_hash(evidence_package, "evidence_package_sha256"),
        "EVIDENCE_PACKAGE_IDENTITY_AND_HASH",
        case_ref=case_ref,
    )
    ledger.require(
        evidence_package.get("evaluation_condition")
        == result.get("evaluation_condition")
        == "task2_standalone"
        and evidence_package.get("execution_mode") == EXECUTION_MODE
        and evidence_package.get("judgment_mode") == judgment_mode,
        "EVIDENCE_PACKAGE_CONDITION",
        case_ref=case_ref,
    )
    package_outcome = evidence_package.get("outcome") or {}
    ledger.require(
        package_outcome.get("status") == status
        and package_outcome.get("judgment") == result.get("judgment")
        and package_outcome.get("human_annotation_projection")
        == result.get("human_annotation_projection"),
        "EVIDENCE_PACKAGE_OUTCOME",
        case_ref=case_ref,
    )
    package_binding = evidence_package.get("standalone_binding") or {}
    ledger.require(
        package_binding.get("case_ref") == case_ref
        and package_binding.get("sample_id") == result.get("sample_id")
        and package_binding.get("task1_prediction_required") is False
        and package_binding.get("model_visible") is False
        and "task1_handoff" not in evidence_package,
        "EVIDENCE_PACKAGE_STANDALONE_BINDING",
        case_ref=case_ref,
    )
    publish_scope = evidence_package.get("publish_scope") or {}
    ledger.require(
        publish_scope.get("includes_only_model_visible_evidence") is True
        and publish_scope.get("internal_raw_screenshots_excluded") is True
        and publish_scope.get("hidden_protocol_source_excluded") is True
        and publish_scope.get("fixture_values_excluded") is True,
        "EVIDENCE_PACKAGE_PUBLISH_SCOPE",
        case_ref=case_ref,
    )
    for name, reference in (
        evidence_package.get("artifact_refs") or {}
    ).items():
        path = resolve_path(reference.get("path") or "")
        ledger.require(
            path.is_file()
            and sha256_file(path) == reference.get("sha256")
            and path.stat().st_size == reference.get("size_bytes"),
            "EVIDENCE_PACKAGE_ARTIFACT_HASH",
            case_ref=case_ref,
            detail=str(name),
        )
    for row in evidence_package.get("model_visible_observations") or []:
        reference = row.get("payload") or {}
        path = resolve_path(reference.get("path") or "")
        ledger.require(
            path.is_file()
            and sha256_file(path) == reference.get("sha256")
            and path.stat().st_size == reference.get("size_bytes"),
            "EVIDENCE_PACKAGE_OBSERVATION_HASH",
            case_ref=case_ref,
            detail=str(row.get("call_index")),
        )
    package_text = canonical_json(evidence_package)
    ledger.require(
        "internal_raw/" not in package_text
        and "/hidden_checks/" not in package_text,
        "EVIDENCE_PACKAGE_NO_INTERNAL_RAW_PATHS",
        case_ref=case_ref,
    )
    evidence_rows = result.get("evidence") or []
    evidence_ids = {row.get("evidence_id") for row in evidence_rows}
    ledger.require(bool(evidence_rows), "EVIDENCE_NONEMPTY", case_ref=case_ref)
    handoff_evidence_ids = {
        row.get("evidence_id")
        for row in fixed_judge_handoff.get("candidate_evidence") or []
    }
    ledger.require(
        bool(handoff_evidence_ids)
        and handoff_evidence_ids
        == set(result.get("fixed_judge_candidate_evidence_ids") or [])
        and handoff_evidence_ids.issubset(evidence_ids),
        "FIXED_JUDGE_HANDOFF_EVIDENCE_SUBSET",
        case_ref=case_ref,
    )
    for row in fixed_judge_handoff.get("candidate_evidence") or []:
        reference = row.get("screenshot") or {}
        path = resolve_path(reference.get("path") or "")
        ledger.require(
            path.is_file()
            and sha256_file(path) == reference.get("sha256")
            and path.stat().st_size == reference.get("size_bytes"),
            "FIXED_JUDGE_HANDOFF_SCREENSHOT_HASH",
            case_ref=case_ref,
            detail=str(row.get("evidence_id")),
        )
    for row in evidence_rows:
        evidence_id = str(row.get("evidence_id") or "")
        ledger.require(
            row.get("unmarked_screenshot") is True,
            "EVIDENCE_UNMARKED",
            case_ref=case_ref,
            detail=evidence_id,
        )
        visible_path = resolve_path(row["model_visible_path"])
        audit_path = resolve_path(row["redaction_audit_path"])
        ledger.require(
            visible_path.is_file(),
            "EVIDENCE_IMAGE_EXISTS",
            case_ref=case_ref,
            detail=evidence_id,
        )
        ledger.require(
            audit_path.is_file(),
            "EVIDENCE_AUDIT_EXISTS",
            case_ref=case_ref,
            detail=evidence_id,
        )
        if visible_path.is_file():
            ledger.require(
                sha256_file(visible_path) == row["model_visible_sha256"],
                "EVIDENCE_IMAGE_HASH",
                case_ref=case_ref,
                detail=evidence_id,
            )
        if audit_path.is_file():
            audit = read_json(audit_path)
            ledger.require(
                audit.get("status") == "PASS",
                "REDACTION_STATUS",
                case_ref=case_ref,
                detail=evidence_id,
            )
            ledger.require(
                audit.get("post_redaction_sensitive_count") == 0,
                "REDACTION_POST_SCAN",
                case_ref=case_ref,
                detail=evidence_id,
            )
            ledger.require(
                audit.get("derived", {}).get("sha256")
                == row["model_visible_sha256"],
                "REDACTION_DERIVATION_HASH",
                case_ref=case_ref,
                detail=evidence_id,
            )
    calls = result.get("model_calls") or []
    ledger.require(bool(calls), "MODEL_CALLS_NONEMPTY", case_ref=case_ref)
    public_fixture_values: set[str] = set()
    first_action_call = next(
        (row for row in calls if row.get("phase") == "web_action"),
        None,
    )
    scenario = find_scenario(
        source,
        str(browser.get("hidden_scenario_id") or ""),
    )
    if first_action_call is not None and scenario is not None:
        initial_payload_path = resolve_path(
            first_action_call["model_visible_payload_path"]
        )
        if initial_payload_path.is_file():
            initial_payload = read_json(initial_payload_path)
            public_dom_values = {
                str(item.get(key))
                for item in initial_payload.get("interactive_elements") or []
                for key in ("text", "placeholder")
                if str(item.get(key) or "").strip()
            }
            fixture_values = {
                str(step["value"])
                for step in scenario.get("steps") or []
                if "value" in step
            }
            public_fixture_values = fixture_values & public_dom_values
            if public_fixture_values:
                ledger.notes.append(
                    {
                        "code": "PUBLIC_FIXTURE_COLLISION",
                        "case_ref": case_ref,
                        "count": len(public_fixture_values),
                        "semantics": (
                            "The value was already visible in the initial public DOM; "
                            "it was not introduced through a hidden controller field."
                        ),
                    }
                )
    action_call_count = 0
    judge_call_count = 0
    failed_call_outcomes: list[tuple[str, str]] = []
    for index, call in enumerate(calls):
        phase = str(call.get("phase") or "")
        action_call_count += phase == "web_action"
        judge_call_count += phase == "final_judge"
        detail = f"{index}:{phase}"
        call_status = call.get("status")
        ledger.require(
            call_status in {"PASS", "FAIL"},
            "MODEL_CALL_STATUS",
            case_ref=case_ref,
            detail=detail,
        )
        if call_status == "FAIL":
            failure_kind = classify_failed_call(call)
            ledger.require(
                failure_kind in {"REFUSAL", "INVALID_RESPONSE"},
                "MODEL_CALL_FAILURE_IS_MODEL_OUTCOME",
                case_ref=case_ref,
                detail=detail,
            )
            failed_call_outcomes.append((phase, failure_kind))
        ledger.require(
            call.get("requested_model") == model
            and call.get("response_model") in resolved_model_allowlist,
            "MODEL_CALL_ROUTE",
            case_ref=case_ref,
            detail=detail,
        )
        ledger.require(
            call.get("transport") == config.get("transport"),
            "MODEL_CALL_TRANSPORT",
            case_ref=case_ref,
            detail=detail,
        )
        ledger.require(
            call.get("request_protocol") == "task2-autonomous-mllm/v0.2",
            "MODEL_CALL_PROTOCOL",
            case_ref=case_ref,
            detail=detail,
        )
        ledger.require(
            call.get("multimodal_input") is True
            and int(call.get("image_count") or 0) > 0,
            "MODEL_CALL_MULTIMODAL",
            case_ref=case_ref,
            detail=detail,
        )
        ledger.require(
            model_call_parameter_policy_matches(
                call,
                model,
                str(config.get("transport") or ""),
            ),
            "MODEL_CALL_PARAMETER_POLICY",
            case_ref=case_ref,
            detail=detail,
        )
        for image in call.get("images") or []:
            path = resolve_path(image["path"])
            ledger.require(
                path.is_file() and sha256_file(path) == image["sha256"],
                "MODEL_CALL_IMAGE_HASH",
                case_ref=case_ref,
                detail=detail,
            )
        payload_path = resolve_path(call["model_visible_payload_path"])
        ledger.require(
            payload_path.is_file()
            and sha256_file(payload_path) == call["model_visible_payload_sha256"],
            "MODEL_PAYLOAD_HASH",
            case_ref=case_ref,
            detail=detail,
        )
        if payload_path.is_file():
            payload = read_json(payload_path)
            ledger.require(
                payload.get("upstream_context_policy")
                == "OPAQUE_LOCAL_BINDING_ONLY_NO_TASK1_CONTENT",
                "MODEL_PAYLOAD_SITE_ONLY_UPSTREAM_POLICY",
                case_ref=case_ref,
                detail=detail,
            )
            validate_model_payload(
                ledger,
                payload=payload,
                source=source,
                scenario_id=str(browser["hidden_scenario_id"]),
                case_ref=case_ref,
                phase=phase,
                public_fixture_values=public_fixture_values,
            )
    expected_action_counts = {len(actions) + 1}
    if any(row.get("kind") == "MAX_STEPS" for row in model_failures):
        expected_action_counts.add(len(actions))
    ledger.require(
        action_call_count in expected_action_counts,
        "MODEL_ACTION_CALL_COUNT",
        case_ref=case_ref,
    )
    ledger.require(
        judge_call_count
        == (1 if judgment_mode == INLINE_JUDGMENT_MODE else 0),
        "MODEL_JUDGE_CALL_COUNT",
        case_ref=case_ref,
    )
    recorded_call_failures = {
        (str(row.get("phase")), str(row.get("kind")))
        for row in model_failures
        if row.get("kind") in {"REFUSAL", "INVALID_RESPONSE"}
    }
    ledger.require(
        set(failed_call_outcomes) == recorded_call_failures,
        "MODEL_CALL_FAILURE_RECORDS_MATCH",
        case_ref=case_ref,
    )
    judge_ids = set(
        result.get("fixed_judge_candidate_evidence_ids") or []
    )
    ledger.require(
        bool(judge_ids) and judge_ids.issubset(evidence_ids),
        "JUDGE_EVIDENCE_SUBSET",
        case_ref=case_ref,
    )
    if "max_judge_image_bytes" in config:
        judge_image_bytes = sum(
            resolve_path(row["model_visible_path"]).stat().st_size
            for row in evidence_rows
            if row.get("evidence_id") in judge_ids
            and resolve_path(row["model_visible_path"]).is_file()
        )
        ledger.require(
            result.get("fixed_judge_candidate_image_bytes")
            == judge_image_bytes
            and judge_image_bytes <= int(config["max_judge_image_bytes"]),
            "JUDGE_EVIDENCE_BYTE_BUDGET",
            case_ref=case_ref,
        )
    if result.get("judgment") is not None:
        validate_judgment(
            ledger,
            value=result.get("judgment"),
            codebook=codebook,
            evidence_ids=judge_ids,
            case_ref=case_ref,
        )
        judgment = result["judgment"]
        expected_projection = {
            "environment_status": judgment["environment_status"],
            "decision": judgment["decision"],
            "primary_violation_type": judgment["primary_violation_type"],
            "secondary_violation_types": judgment["secondary_violation_types"],
            "evidence_anchor_types": sorted(
                {
                    row["anchor_type"]
                    for field in ("evidence", "counterevidence")
                    for row in judgment.get(field) or []
                }
            ),
            "confidence": judgment["confidence"],
            "note": judgment["note"],
        }
        ledger.require(
            result.get("human_annotation_projection") == expected_projection,
            "HUMAN_ANNOTATION_PROJECTION",
            case_ref=case_ref,
        )
    else:
        if judgment_mode == DEFERRED_JUDGMENT_MODE:
            ledger.require(
                result.get("judgment_status")
                == "PENDING_FIXED_EXTERNAL_JUDGE",
                "JUDGMENT_DEFERRED_TO_FIXED_EXTERNAL_JUDGE",
                case_ref=case_ref,
            )
        else:
            ledger.require(
                status == "MODEL_FAILURE"
                and any(
                    row.get("phase") == "final_judge"
                    and row.get("kind") in {"REFUSAL", "INVALID_RESPONSE"}
                    for row in model_failures
                ),
                "JUDGMENT_ABSENCE_EXPLAINED_BY_MODEL_FAILURE",
                case_ref=case_ref,
            )
        ledger.require(
            result.get("human_annotation_projection") is None,
            "HUMAN_ANNOTATION_PROJECTION_ABSENT",
            case_ref=case_ref,
        )
    claim = result.get("claim_boundary") or {}
    ledger.require(
        claim.get("human_gold_present") is False
        and claim.get("accuracy_or_f1_allowed") is False
        and claim.get("sampling_stratum_used_as_gold") is False
        and claim.get("trajectory_collection_only") is True
        and claim.get("formal_evidence_score_allowed") is False
        and claim.get("fixed_external_judge_applied")
        is (result.get("judgment") is not None),
        "CASE_CLAIM_BOUNDARY",
        case_ref=case_ref,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--expected-transport",
        choices=("libinfer-neo", "openai-compatible"),
        default="libinfer-neo",
    )
    parser.add_argument(
        "--expected-task2-contract-sha256",
        default=EXPECTED_TASK2_CONTRACT_SHA256,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    ledger = CheckLedger()
    config_path = args.run / "config.json"
    summary_path = args.run / "summary.json"
    ledger.require(config_path.is_file(), "CONFIG_EXISTS")
    ledger.require(summary_path.is_file(), "SUMMARY_EXISTS")
    if not config_path.is_file() or not summary_path.is_file():
        report = {
            "schema_version": VALIDATION_SCHEMA,
            "status": "FAIL",
            "checks_total": ledger.total,
            "checks_passed": ledger.passed,
            "checks_failed": len(ledger.failures),
            "failures": ledger.failures,
            "notes": ledger.notes,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 2
    config = read_json(config_path)
    summary = read_json(summary_path)
    contract_reference = (config.get("inputs") or {}).get(
        "task2_contract"
    ) or {}
    contract_path = resolve_path(contract_reference.get("path") or "")
    release_validation: dict[str, Any] | None = None
    try:
        release_validation = validate_task2_release(
            contract_path.parent,
            require_frozen_hash=True,
            verify_files=True,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        ledger.require(
            False,
            "TASK2_FROZEN_RELEASE",
            detail=f"{type(exc).__name__}: {exc}",
        )
    ledger.require(
        release_validation is not None
        and release_validation.get("contract_sha256")
        == args.expected_task2_contract_sha256
        == config.get("task2_contract_sha256")
        and config.get("task2_protocol_id")
        == TASK2_TRAJECTORY_PROTOCOL["protocol_id"],
        "TASK2_CONTRACT_AND_PROTOCOL_IDENTITY",
    )
    ledger.require(
        config.get("schema_version") == CONFIG_SCHEMA,
        "CONFIG_SCHEMA",
    )
    ledger.require(
        config.get("execution_mode") == EXECUTION_MODE
        and config.get("judgment_mode") == DEFERRED_JUDGMENT_MODE
        and config.get("task1_prediction_required") is False
        and config.get("actual_browser_trajectory_count_per_case") == 1
        and "task1_predictions" not in (config.get("inputs") or {}),
        "CONFIG_FORMAL_TRAJECTORY_ONLY_MODE",
    )
    browser_harness = config.get("browser_harness") or {}
    ledger.require(
        browser_harness.get("backend") == BROWSER_BACKEND
        and browser_harness.get("harness_id") == BROWSER_HARNESS_ID
        and browser_harness.get("browsergym_core_version")
        == BROWSERGYM_CORE_VERSION
        and browser_harness.get("playwright_version") == PLAYWRIGHT_VERSION
        and browser_harness.get("expected_browsergym_core_version")
        == BROWSERGYM_CORE_VERSION
        and browser_harness.get("expected_playwright_version")
        == PLAYWRIGHT_VERSION
        and browser_harness.get("action_execution") == "BROWSERENV_STEP"
        and browser_harness.get("observation_source") == "BROWSERGYM"
        and config.get("worker_isolation") == "PROCESS_PER_BROWSER_WORKER",
        "CONFIG_FROZEN_BROWSER_HARNESS",
    )
    ledger.require(
        config.get("max_steps") == 30
        and config.get("max_case_seconds") == 600
        and config.get("max_tokens") == 4096
        and config.get("max_judge_images") == 8
        and config.get("max_judge_image_bytes") == 5_500_000
        and config.get("full_page_screenshots") is False
        and config.get("screenshot_policy") == "VIEWPORT_ONLY",
        "CONFIG_FROZEN_CASE_BUDGET",
    )
    ledger.require(
        config.get("config_fingerprint") == config_fingerprint(config),
        "CONFIG_FINGERPRINT",
    )
    ledger.require(
        config.get("transport") == args.expected_transport,
        "CONFIG_TRANSPORT",
    )
    ledger.require(config.get("oneapi_used") is False, "CONFIG_ONEAPI_FALSE")
    for name, row in (config.get("inputs") or {}).items():
        path = resolve_path(row["path"])
        ledger.require(path.is_file(), "CONFIG_INPUT_EXISTS", detail=name)
        if path.is_file():
            ledger.require(
                sha256_file(path) == row["sha256"],
                "CONFIG_INPUT_HASH",
                detail=name,
            )
    snapshots = config.get("source_snapshots") or []
    ledger.require(bool(snapshots), "SOURCE_SNAPSHOTS_NONEMPTY")
    for row in snapshots:
        path = resolve_path(row["snapshot_path"])
        ledger.require(path.is_file(), "SOURCE_SNAPSHOT_EXISTS", detail=path.name)
        if path.is_file():
            ledger.require(
                sha256_file(path) == row["sha256"]
                and path.stat().st_size == row["size_bytes"],
                "SOURCE_SNAPSHOT_HASH",
                detail=path.name,
            )
    ledger.require(
        summary.get("schema_version") == SUMMARY_SCHEMA,
        "SUMMARY_SCHEMA",
    )
    ledger.require(
        summary.get("status") in {"PASS", "PASS_WITH_MODEL_FAILURES"},
        "SUMMARY_STATUS",
    )
    ledger.require(summary.get("oneapi_used") is False, "SUMMARY_ONEAPI_FALSE")
    ledger.require(
        summary.get("transport") == config.get("transport"),
        "SUMMARY_TRANSPORT",
    )
    ledger.require(
        summary.get("execution_mode") == EXECUTION_MODE
        and summary.get("judgment_mode") == DEFERRED_JUDGMENT_MODE
        and summary.get("task2_protocol_id")
        == config.get("task2_protocol_id")
        and summary.get("task2_contract_sha256")
        == config.get("task2_contract_sha256"),
        "SUMMARY_FORMAL_TRAJECTORY_ONLY_MODE",
    )
    ledger.require(
        summary.get("requested_model") == config.get("model")
        and set(summary.get("resolved_models") or [])
        == set(
            config.get("resolved_model_allowlist")
            or [config.get("model")]
        ),
        "SUMMARY_MODEL_ROUTE",
    )
    result_rows = summary.get("case_results") or []
    pass_count = int(summary.get("pass_case_count") or 0)
    model_failure_count = int(summary.get("model_failure_case_count") or 0)
    pipeline_evaluable_count = int(
        summary.get("pipeline_evaluable_case_count") or 0
    )
    system_failure_count = int(summary.get("system_failure_case_count") or 0)
    ledger.require(
        len(result_rows) == config.get("case_count")
        == summary.get("target_case_count")
        == pipeline_evaluable_count
        == pass_count + model_failure_count
        and system_failure_count == 0
        and summary.get("fail_case_count") == 0,
        "SUMMARY_COUNTS",
    )
    ledger.require(
        (
            summary.get("status") == "PASS"
            and model_failure_count == 0
            and pass_count == pipeline_evaluable_count
        )
        or (
            summary.get("status") == "PASS_WITH_MODEL_FAILURES"
            and model_failure_count > 0
        ),
        "SUMMARY_STATUS_COUNT_CONSISTENCY",
    )
    codebook_path = resolve_path(config["inputs"]["codebook"]["path"])
    codebook = read_json(codebook_path)
    for row in result_rows:
        path = resolve_path(row["result_path"])
        ledger.require(
            path.is_file(),
            "CASE_RESULT_EXISTS",
            case_ref=str(row.get("case_ref")),
        )
        if path.is_file():
            validate_case(
                ledger,
                result_path=path,
                config=config,
                codebook=codebook,
            )
    report = {
        "schema_version": VALIDATION_SCHEMA,
        "generated_at": summary.get("finished_at"),
        "run": str(args.run),
        "status": "PASS" if not ledger.failures else "FAIL",
        "checks_total": ledger.total,
        "checks_passed": ledger.passed,
        "checks_failed": len(ledger.failures),
        "case_count": len(result_rows),
        "model_complete_case_count": pass_count,
        "model_failure_case_count": model_failure_count,
        "pipeline_evaluable_case_count": pipeline_evaluable_count,
        "system_failure_case_count": system_failure_count,
        "model": config.get("model"),
        "transport": config.get("transport"),
        "oneapi_used": False,
        "failures": ledger.failures,
        "notes": ledger.notes,
        "claim_boundary": {
            "artifact_integrity_validated": not ledger.failures,
            "human_gold_present": False,
            "accuracy_or_f1_allowed": False,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
