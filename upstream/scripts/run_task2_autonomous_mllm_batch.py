#!/usr/bin/env python3
"""Collect autonomous Task 2 browser trajectories on local site mirrors.

Task 2 is a standalone site-only investigation.  A private controller binds each
case to its local mirror without exposing Task 1 content, the resolver, source
stratum, hidden verification scenario, selectors, or synthetic fixture values
to the tested model.  At each step the model sees only an unmarked, redacted
screenshot plus a sanitized opaque control inventory.

The formal protocol defers classification and evidence scoring to one separately
frozen external judge. Inline self-judging is not accepted by this runner.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import Locator, Page


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_model_browser_agent import (  # noqa: E402
    assert_model_boundary_safe,
    sanitize_model_value,
)
from riskchainbench_browsergym import (  # noqa: E402
    BROWSER_HARNESS_ID,
    BROWSERGYM_CORE_VERSION,
    EXPECTED_BROWSERGYM_CORE_VERSION,
    EXPECTED_PLAYWRIGHT_VERSION,
    PLAYWRIGHT_VERSION,
    BrowserGymSession,
    assert_frozen_browser_runtime,
    browsergym_action,
    locator_for_bid,
)
from run_unified_mllm_smoke import (  # noqa: E402
    DOMAIN_RE,
    EMAIL_RE,
    IPV4_RE,
    MODEL_ID_RE,
    URL_RE,
    ModelCallError,
    atomic_json,
    call_model_json,
    canonical_json,
    load_export_env,
    pinned_response_models,
    provider_output_observed,
    read_json,
    read_jsonl,
    redact_screenshot,
    safe_text,
    sha256_file,
    sha256_text,
    utc_now,
)
from task2_frozen_protocol import (  # noqa: E402
    EXPECTED_TASK2_CONTRACT_SHA256,
    TASK2_CASE_COUNT,
    TASK2_TRAJECTORY_PROTOCOL,
    assert_protocol_runtime,
    validate_task2_release,
)
from verify_stateful_profile import (  # noqa: E402
    execute_step,
    free_port,
    start_local_runtime_process,
    stop_process,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK2_RELEASE = PROJECT_ROOT / "data/task2"
DEFAULT_PROMPTS = DEFAULT_TASK2_RELEASE / "spec/task2_trajectory_prompt.json"
DEFAULT_CODEBOOK = PROJECT_ROOT / "configs/riskchainbench_violation_codebook_v0.2.json"
DEFAULT_ENV: Path | None = None
DEFAULT_TESSERACT_ROOT: Path | None = None
INTERACTIVE_SELECTOR = "a,button,input,select,textarea,form,[role=button]"
MUTATING_OPS = {"fill", "check", "click", "direct_submit", "reload"}
MODEL_ACTIONS = {
    "fill",
    "check",
    "click",
    "submit",
    "reload",
    "scroll_down",
    "scroll_up",
    "stop",
}
MODEL_TO_PROFILE_OP = {
    "fill": "fill",
    "check": "check",
    "click": "click",
    "submit": "direct_submit",
    "reload": "reload",
}
DECISIONS = {"VIOLATION", "NON_VIOLATION", "INSUFFICIENT_EVIDENCE"}
ENVIRONMENT_STATUSES = {"COMPLETE", "PARTIAL", "INACCESSIBLE", "INVALID"}
EVIDENCE_SUFFICIENCY = {"SUFFICIENT", "INSUFFICIENT"}
UPSTREAM_CONTEXT_POLICY = "OPAQUE_LOCAL_BINDING_ONLY_NO_TASK1_CONTENT"
BROWSER_BACKEND = "BROWSERGYM_PLAYWRIGHT"
EXECUTION_MODE = "TASK2_STANDALONE_SITE_ONLY"
DEFERRED_JUDGMENT_MODE = "DEFERRED_FIXED_EXTERNAL_JUDGE"
INLINE_JUDGMENT_MODE = "INLINE_SELF_JUDGE_DIAGNOSTIC"
MODEL_FAILURE_KINDS = {
    "REFUSAL",
    "INVALID_RESPONSE",
    "ABSTAIN",
    "MAX_STEPS",
}


class CaseWallTimeout(RuntimeError):
    """Operational watchdog expiry; this is retryable system failure."""


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


def embedded_hash(value: dict[str, Any], field: str) -> str:
    unhashed = copy.deepcopy(value)
    unhashed.pop(field, None)
    return sha256_text(canonical_json(unhashed))


def artifact_file_ref(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def primary_sample_id(binding: dict[str, Any]) -> str:
    sample_ids = [
        str(value)
        for value in binding.get("sample_ids") or []
        if str(value).endswith("--v000")
    ]
    if len(sample_ids) != 1:
        raise ValueError(
            f"binding must contain exactly one v000 sample: {binding.get('case_ref')}"
        )
    return sample_ids[0]


def human_annotation_projection(
    judgment: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if judgment is None:
        return None
    anchors = {
        str(row["anchor_type"])
        for field in ("evidence", "counterevidence")
        for row in judgment.get(field) or []
        if isinstance(row, dict) and row.get("anchor_type")
    }
    return {
        "environment_status": judgment["environment_status"],
        "decision": judgment["decision"],
        "primary_violation_type": judgment["primary_violation_type"],
        "secondary_violation_types": judgment["secondary_violation_types"],
        "evidence_anchor_types": sorted(anchors),
        "confidence": judgment["confidence"],
        "note": judgment["note"],
    }


def load_runtime_materialization(
    *,
    report_path: Path | None,
    docker_reference_path: Path | None,
    expected_docker_reference_sha256: str,
    expected_task2_contract_sha256: str,
    runtime_root: Path,
    resolver: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    if report_path is None:
        return {}
    if (
        docker_reference_path is None
        or not docker_reference_path.is_file()
        or sha256_file(docker_reference_path)
        != expected_docker_reference_sha256
    ):
        raise ValueError("frozen Docker reference is required for portable runtime")
    archive_references = {
        str(row["case_ref"]): row for row in read_jsonl(docker_reference_path)
    }
    report = read_json(report_path)
    bindings = {
        str(row["case_ref"]): row for row in resolver.get("bindings") or []
    }
    rows = {
        str(row["case_ref"]): row for row in report.get("rows") or []
    }
    if (
        report.get("schema_version")
        != "riskchainbench-task2-materialization/v0.2"
        or report.get("status") != "PASS"
        or report.get("task2_contract_sha256")
        != expected_task2_contract_sha256
        or report.get("report_sha256") != embedded_hash(report, "report_sha256")
        or Path(str(report.get("runtime_root") or "")).resolve() != runtime_root
        or int(report.get("requested_case_count") or -1) != 600
        or int(report.get("materialized_case_count") or -1) != 600
        or int(report.get("failure_count", -1)) != 0
        or set(rows) != set(bindings)
        or set(archive_references) != set(bindings)
    ):
        raise ValueError("portable runtime materialization report is invalid")
    verified: dict[str, dict[str, Any]] = {}
    for case_ref, row in rows.items():
        binding = bindings[case_ref]
        archive_reference = archive_references[case_ref]
        attestation_path = runtime_root / str(
            row.get("runtime_attestation_path") or ""
        )
        if not attestation_path.is_file():
            raise ValueError(f"runtime attestation is missing: {case_ref}")
        attestation = read_json(attestation_path)
        archive_binary_sha256 = str(
            row.get("archive_runtime_binary_sha256") or ""
        )
        if (
            row.get("status")
            not in {"PASS_MATERIALIZED", "PASS_ALREADY_MATERIALIZED"}
            or row.get("source_runtime_binary_sha256")
            != binding.get("runtime_binary_sha256")
            or row.get("archive_sha256")
            != archive_reference.get("archive_sha256")
            or row.get("runtime_binary_sha256") != archive_binary_sha256
            or row.get("runtime_attestation_sha256")
            != sha256_file(attestation_path)
            or attestation.get("schema_version")
            != "riskchainbench-runtime-attestation/v0.1"
            or attestation.get("case_ref") != case_ref
            or attestation.get("task2_contract_sha256")
            != expected_task2_contract_sha256
            or attestation.get("archive_sha256")
            != archive_reference.get("archive_sha256")
            or attestation.get("image_id")
            != archive_reference.get("image_id")
            or attestation.get("source_runtime_binary_sha256")
            != binding.get("runtime_binary_sha256")
            or attestation.get("archive_runtime_binary_sha256")
            != archive_binary_sha256
            or attestation.get("attestation_sha256")
            != embedded_hash(attestation, "attestation_sha256")
        ):
            raise ValueError(f"runtime attestation mismatch: {case_ref}")
        verified[case_ref] = {
            "verification_source": "FROZEN_OCI_MATERIALIZATION_REPORT",
            "expected_runtime_binary_sha256": archive_binary_sha256,
            "source_runtime_binary_sha256": binding["runtime_binary_sha256"],
            "source_runtime_binary_match": (
                archive_binary_sha256 == binding["runtime_binary_sha256"]
            ),
            "archive_sha256": row["archive_sha256"],
            "image_id": archive_reference["image_id"],
            "docker_reference_path": str(docker_reference_path),
            "docker_reference_sha256": expected_docker_reference_sha256,
            "runtime_attestation_path": str(attestation_path),
            "runtime_attestation_sha256": row[
                "runtime_attestation_sha256"
            ],
            "materialization_report_path": str(report_path),
            "materialization_report_sha256": sha256_file(report_path),
        }
    return verified


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")
    os.replace(temporary, path)


def snapshot_sources(output_dir: Path, paths: list[Path]) -> list[dict[str, Any]]:
    snapshot_root = output_dir / "source_snapshot"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    records = []
    used_names: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        name = resolved.name
        if name in used_names:
            name = f"{resolved.parent.name}__{name}"
        used_names.add(name)
        destination = snapshot_root / name
        shutil.copy2(resolved, destination)
        records.append(
            {
                "source_path": str(resolved),
                "snapshot_path": str(destination),
                "sha256": sha256_file(destination),
                "size_bytes": destination.stat().st_size,
            }
        )
    return records


def normalize_predicted_entry(value: Any) -> str:
    return str(value or "").strip().lower().rstrip("/")


def entry_sha256(value: Any) -> str:
    normalized = normalize_predicted_entry(value)
    if not normalized:
        return ""
    return sha256_text(normalized)


def classify_model_call_failure(audit: dict[str, Any]) -> str:
    """Separate model output failures from provider/transport failures."""

    attempts = list(audit.get("attempts") or [])
    response_texts = [
        str(row.get("response_content") or "").strip()
        for row in attempts
        if str(row.get("response_content") or "").strip()
    ]
    model_owned = any(row.get("failure_owner") == "MODEL" for row in attempts)
    if not model_owned and not any(provider_output_observed(row) for row in attempts):
        return "TRANSPORT_ERROR"
    normalized = "\n".join(response_texts).casefold()
    if any(marker in normalized for marker in REFUSAL_MARKERS):
        return "REFUSAL"
    return "INVALID_RESPONSE"


def qualification_status(
    *,
    target_count: int,
    pass_count: int,
    model_failure_count: int,
    system_failure_count: int,
) -> str:
    if (
        system_failure_count == 0
        and pass_count + model_failure_count == target_count
    ):
        return "PASS" if model_failure_count == 0 else "PASS_WITH_MODEL_FAILURES"
    return "FAIL"


def codebook_projection(codebook: dict[str, Any]) -> dict[str, Any]:
    """Return only the policy needed by the judge, never source/session labels."""

    return {
        "schema_version": codebook["schema_version"],
        "benchmark_scope": {
            "decision_target": codebook["benchmark_scope"]["decision_target"],
            "legal_status": codebook["benchmark_scope"]["legal_status"],
            "statement_zh": codebook["benchmark_scope"]["statement_zh"],
        },
        "decisions": [
            {
                "id": row["id"],
                "label_zh": row["label_zh"],
                "definition_zh": row["definition_zh"],
            }
            for row in codebook["decisions"]
        ],
        "primary_violation_types": [
            {
                key: row[key]
                for key in (
                    "id",
                    "label_zh",
                    "include_zh",
                    "exclude_zh",
                    "minimum_evidence_zh",
                )
                if key in row
            }
            for row in codebook["primary_violation_types"]
        ],
        "evidence_anchor_types": [
            {"id": row["id"], "label_zh": row["label_zh"]}
            for row in codebook["evidence_anchor_types"]
        ],
        "decision_rules": codebook["decision_rules"],
    }


def select_profile_scenario(profile: dict[str, Any]) -> dict[str, Any]:
    scenarios = list((profile.get("verification") or {}).get("scenarios") or [])
    if not scenarios:
        raise ValueError("stateful profile has no verification scenario")
    preferred = {
        "wp_reader_session_and_logout": 1_000_000,
        "party_join_and_resume": 900_000,
        "auto-contact_accepted": 800_000,
    }

    def score(row: dict[str, Any]) -> tuple[int, str]:
        mutations = sum(
            step.get("op") in MUTATING_OPS for step in row.get("steps") or []
        )
        states = len(row.get("covers_states") or [])
        return (
            preferred.get(str(row.get("id")), 0) + states * 1_000 + mutations,
            str(row.get("id")),
        )

    selected = max(scenarios, key=score)
    if not selected.get("depends_on_scenarios"):
        return selected

    # Task 2 starts every case from a clean state and does not execute hidden
    # prerequisite scenarios. Fall back to the deepest independently runnable
    # path instead of leaking or silently materializing prerequisite Gold state.
    independent = [
        row for row in scenarios if not row.get("depends_on_scenarios")
    ]
    if not independent:
        raise ValueError(
            "stateful profile has no independently runnable verification scenario"
        )

    def independent_score(row: dict[str, Any]) -> tuple[int, int, int, str]:
        mutations = sum(
            step.get("op") in MUTATING_OPS for step in row.get("steps") or []
        )
        states = len(row.get("covers_states") or [])
        return (
            preferred.get(str(row.get("id")), 0),
            mutations,
            states,
            str(row.get("id")),
        )

    return max(independent, key=independent_score)


def build_phases(scenario: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prelude: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for step in scenario.get("steps") or []:
        if step.get("op") in MUTATING_OPS:
            current = {"action": step, "checks": []}
            phases.append(current)
        elif current is None:
            prelude.append(step)
        else:
            current["checks"].append(step)
    return prelude, phases


@dataclass
class HiddenProtocol:
    track: str
    scenario: dict[str, Any]
    prelude: list[dict[str, Any]]
    phases: list[dict[str, Any]]
    completed_actions: list[dict[str, Any]]
    assertion_count: int = 0

    @classmethod
    def from_scenario(cls, track: str, scenario: dict[str, Any]) -> "HiddenProtocol":
        prelude, phases = build_phases(scenario)
        return cls(
            track=track,
            scenario=scenario,
            prelude=prelude,
            phases=phases,
            completed_actions=[],
        )

    @property
    def complete(self) -> bool:
        return not self.phases

    def allowed_phase_indices(self) -> list[int]:
        if not self.phases:
            return []
        first = self.phases[0]
        if first["action"]["op"] not in {"fill", "check"} or first["checks"]:
            return [0]
        result = []
        for index, phase in enumerate(self.phases):
            if phase["action"]["op"] not in {"fill", "check"} or phase["checks"]:
                break
            result.append(index)
        return result or [0]


def coverage_objectives(protocol: HiddenProtocol) -> list[str]:
    """Expose operation classes needed for a fair task, never hidden targets."""

    if protocol.complete:
        return ["STOP_COMPLETE_AFTER_REVIEWING_THE_COLLECTED_LOCAL_STATES"]
    operations = [phase["action"]["op"] for phase in protocol.phases]
    objectives: list[str] = []
    if protocol.track == "L2_OBSERVATION":
        objectives.append("FOLLOW_ONE_SAFE_LOCAL_CONTROL_TO_A_CHANGED_PUBLIC_STATE")
    if any(operation in {"fill", "check"} for operation in operations):
        objectives.append(
            "COMPLETE_THE_RELEVANT_VISIBLE_FORM_WITH_CONTROLLER_SYNTHETIC_VALUES"
        )
    if any(operation in {"click", "direct_submit"} for operation in operations):
        objectives.append("REACH_AND_INSPECT_A_DETERMINISTIC_LOCAL_POST_ACTION_STATE")
    if "reload" in operations:
        objectives.append("VERIFY_WHETHER_THE_LOCAL_STATE_PERSISTS_AFTER_ONE_RELOAD")
    reload_index = operations.index("reload") if "reload" in operations else -1
    if reload_index >= 0 and any(
        operation == "click" for operation in operations[reload_index + 1 :]
    ):
        objectives.append("EXERCISE_A_VISIBLE_REVERSIBLE_EXIT_AFTER_PERSISTENCE")
    return objectives


def scenario_source(
    *,
    binding: dict[str, Any],
    observation_paths: dict[str, Path],
    runtime_root: Path,
) -> tuple[str, Path, dict[str, Any], dict[str, Any]]:
    profile_ref = binding.get("profile")
    if isinstance(profile_ref, dict) and profile_ref.get("path"):
        path = runtime_root / str(profile_ref["path"])
        if sha256_file(path) != profile_ref.get("sha256"):
            raise ValueError("stateful profile hash mismatch")
        payload = read_json(path)
        return "L3_STATEFUL", path, payload, select_profile_scenario(payload)
    path = observation_paths.get(str(binding["case_ref"]))
    if path is None:
        raise ValueError("case has neither an L3 profile nor an L2 observation plan")
    payload = read_json(path)
    scenarios = list((payload.get("verification") or {}).get("scenarios") or [])
    if len(scenarios) != 1:
        raise ValueError("observation plan must contain exactly one scenario")
    return "L2_OBSERVATION", path, payload, scenarios[0]


def observe_page(page: Page) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = page.evaluate(
        r"""
        selector => {
          const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
          const viewportWidth = window.innerWidth;
          const viewportHeight = window.innerHeight;
          const elements = Array.from(document.querySelectorAll(selector))
            .map((element, domIndex) => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              const visible = style.display !== 'none'
                && style.visibility !== 'hidden'
                && Number(style.opacity) !== 0
                && rect.width > 0
                && rect.height > 0;
              if (!visible) return null;
              const className = normalize(
                typeof element.className === 'string' ? element.className : ''
              ).toLowerCase();
              const clipped = (
                style.clipPath && style.clipPath !== 'none'
              ) || (
                style.clip && style.clip !== 'auto'
                && style.clip !== 'rect(auto, auto, auto, auto)'
              );
              const assistiveOnly = element.hidden
                || element.closest('[aria-hidden="true"],[inert]') !== null
                || /(^|\s)(screen-reader-text|sr-only|visually-hidden)(\s|$)/.test(className)
                || (clipped && rect.width <= 2 && rect.height <= 2);
              if (assistiveOnly) return null;
              const label = element.labels && element.labels.length
                ? normalize(Array.from(element.labels).map(row =>
                    row.innerText || row.textContent || '').join(' '))
                : '';
              const text = element.tagName === 'FORM'
                ? normalize(element.innerText || element.textContent).slice(0, 240)
                : /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName)
                  ? normalize(label || element.getAttribute('aria-label')
                      || element.getAttribute('placeholder') || element.name)
                  : normalize(element.innerText || element.textContent
                      || element.getAttribute('aria-label')
                      || element.getAttribute('title'));
              const tag = element.tagName.toLowerCase();
              const type = String(element.getAttribute('type') || '').toLowerCase();
              return {
                dom_index: domIndex,
                browsergym_id: element.getAttribute('bid') || '',
                tag,
                role: element.getAttribute('role') || '',
                name: element.getAttribute('name') || '',
                type,
                text,
                placeholder: element.getAttribute('placeholder') || '',
                disabled: Boolean(element.disabled
                  || element.getAttribute('aria-disabled') === 'true'),
                has_value: /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName)
                  && String(element.value || '').length > 0,
                checked: typeof element.checked === 'boolean'
                  ? Boolean(element.checked) : null,
                viewport_status: rect.bottom < 0
                  ? 'ABOVE_VIEWPORT'
                  : rect.top > viewportHeight
                    ? 'BELOW_VIEWPORT'
                    : rect.right < 0
                      ? 'LEFT_OF_VIEWPORT'
                      : rect.left > viewportWidth
                        ? 'RIGHT_OF_VIEWPORT'
                        : 'ONSCREEN',
                capabilities: {
                  fill: !element.disabled && (
                    tag === 'textarea' || tag === 'select'
                    || (tag === 'input' && ![
                      'button','submit','reset','checkbox','radio','hidden','file'
                    ].includes(type))
                  ),
                  check: !element.disabled && tag === 'input'
                    && ['checkbox','radio'].includes(type),
                  click: !element.disabled && (
                    tag === 'a' || tag === 'button'
                    || (tag === 'input' && ['button','submit','reset'].includes(type))
                    || element.getAttribute('role') === 'button'
                  ),
                  submit: tag === 'form'
                }
              };
            }).filter(Boolean);
          const bodyText = normalize(document.body ? document.body.innerText : '');
          const root = document.documentElement;
          const body = document.body;
          return {
            title: document.title || '',
            body_text: bodyText,
            elements,
            scroll_x: window.scrollX || 0,
            scroll_y: window.scrollY || 0,
            viewport_width: viewportWidth,
            viewport_height: viewportHeight,
            document_width: Math.max(root ? root.scrollWidth : 0, body ? body.scrollWidth : 0),
            document_height: Math.max(root ? root.scrollHeight : 0, body ? body.scrollHeight : 0)
          };
        }
        """,
        INTERACTIVE_SELECTOR,
    )
    inventory: list[dict[str, Any]] = []
    for row in raw.get("elements") or []:
        browsergym_id = safe_text(row.get("browsergym_id"), 80)
        if not browsergym_id:
            continue
        inventory.append(
            {
                "element_id": f"BID-{browsergym_id}",
                "browsergym_id": browsergym_id,
                "dom_index": int(row["dom_index"]),
                "tag": safe_text(row.get("tag"), 24),
                "role": safe_text(row.get("role"), 32),
                "name": safe_text(row.get("name"), 80),
                "type": safe_text(row.get("type"), 32),
                "text": safe_text(row.get("text"), 240),
                "placeholder": safe_text(row.get("placeholder"), 120),
                "disabled": bool(row.get("disabled")),
                "has_value": bool(row.get("has_value")),
                "checked": row.get("checked"),
                "viewport_status": row.get("viewport_status"),
                "capabilities": row.get("capabilities"),
            }
        )
    visible_text = str(raw.get("body_text") or "")
    state = {
        "title": safe_text(raw.get("title"), 240),
        "visible_text_excerpt": safe_text(visible_text, 5000),
        "visible_text_sha256": sha256_text(visible_text),
        "scroll_x": float(raw.get("scroll_x") or 0),
        "scroll_y": float(raw.get("scroll_y") or 0),
        "viewport_width": int(raw.get("viewport_width") or 0),
        "viewport_height": int(raw.get("viewport_height") or 0),
        "document_width": int(raw.get("document_width") or 0),
        "document_height": int(raw.get("document_height") or 0),
        "interactive_count": len(inventory),
    }
    projected = sanitize_model_value(
        {
            "state": state,
            "interactive_elements": [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"dom_index", "browsergym_id"}
                }
                for row in inventory
            ],
        }
    )
    assert_model_boundary_safe(projected)
    return projected["state"], [
        {
            **projected_row,
            "dom_index": raw_row["dom_index"],
            "browsergym_id": raw_row["browsergym_id"],
        }
        for projected_row, raw_row in zip(
            projected["interactive_elements"], inventory, strict=True
        )
    ]


def page_state_fingerprint(page: Page) -> str:
    """Hash observable local state without retaining raw URLs or field values."""

    state, inventory = observe_page(page)
    parsed = urlparse(page.url)
    payload = {
        "path_sha256": sha256_text(parsed.path),
        "fragment_sha256": sha256_text(parsed.fragment),
        "state": state,
        "interactive_elements": [
            {
                key: value
                for key, value in row.items()
                if key not in {"dom_index", "browsergym_id"}
            }
            for row in inventory
        ],
    }
    return sha256_text(canonical_json(payload))


def locator_for(page: Page, item: dict[str, Any]) -> Locator:
    return locator_for_bid(page, str(item["browsergym_id"]))


def target_matches_selector(page: Page, item: dict[str, Any], selector: str) -> bool:
    marker = "rcb-" + sha256_text(
        f"{time.monotonic_ns()}:{item['dom_index']}:{selector}"
    )[:20]
    target = locator_for(page, item)
    target.evaluate(
        "(element, value) => element.setAttribute('data-rcb-hidden-match', value)",
        marker,
    )
    try:
        return bool(
            page.locator(selector).evaluate_all(
                """(elements, value) => elements.some(element =>
                  element.getAttribute('data-rcb-hidden-match') === value)""",
                marker,
            )
        )
    finally:
        target.evaluate(
            "element => element.removeAttribute('data-rcb-hidden-match')"
        )


def target_form_matches_selector(
    page: Page,
    item: dict[str, Any],
    selector: str,
    *,
    selector_is_form: bool,
) -> bool:
    marker = "rcb-form-" + sha256_text(
        f"{time.monotonic_ns()}:{item['dom_index']}:{selector}"
    )[:20]
    target = locator_for(page, item)
    marked = target.evaluate(
        """(element, value) => {
          const form = element.closest('form');
          if (!form) return false;
          form.setAttribute('data-rcb-hidden-form-match', value);
          return true;
        }""",
        marker,
    )
    if not marked:
        return False
    try:
        candidate = page.locator(selector)
        script = (
            """(elements, value) => elements.some(element =>
              element.getAttribute('data-rcb-hidden-form-match') === value)"""
            if selector_is_form
            else """(elements, value) => elements.some(element =>
              element.closest('form')?.getAttribute(
                'data-rcb-hidden-form-match') === value)"""
        )
        return bool(candidate.evaluate_all(script, marker))
    finally:
        page.locator(
            f"form[data-rcb-hidden-form-match='{marker}']"
        ).evaluate_all(
            "elements => elements.forEach(element => "
            "element.removeAttribute('data-rcb-hidden-form-match'))"
        )


def safe_exploratory_action(
    protocol: HiddenProtocol,
    decision: dict[str, Any],
    inventory: list[dict[str, Any]],
    page: Page,
) -> bool:
    """Allow optional fields in the same form without advancing hidden Gold."""

    if decision.get("action") not in {"fill", "check"}:
        return False
    item = next(
        (row for row in inventory if row["element_id"] == decision.get("element_id")),
        None,
    )
    if item is None or item.get("disabled"):
        return False
    if decision["action"] == "fill" and item.get("has_value"):
        return False
    if decision["action"] == "check" and item.get("checked"):
        return False
    for phase in protocol.phases:
        step = phase["action"]
        selector = step.get("selector")
        if not selector:
            continue
        if step.get("op") == "direct_submit" and target_form_matches_selector(
            page, item, selector, selector_is_form=True
        ):
            return True
        if step.get("op") in {"fill", "check"} and target_form_matches_selector(
            page, item, selector, selector_is_form=False
        ):
            return True
    return False


def match_hidden_phase(
    protocol: HiddenProtocol,
    decision: dict[str, Any],
    inventory: list[dict[str, Any]],
    page: Page,
) -> int | None:
    action = decision.get("action")
    if action not in MODEL_TO_PROFILE_OP:
        return None
    expected_op = MODEL_TO_PROFILE_OP[action]
    item = next(
        (row for row in inventory if row["element_id"] == decision.get("element_id")),
        None,
    )
    for index in protocol.allowed_phase_indices():
        step = protocol.phases[index]["action"]
        step_op = step.get("op")
        submit_button_equivalent = (
            step_op == "direct_submit"
            and action == "click"
            and item is not None
            and bool((item.get("capabilities") or {}).get("click"))
            and target_form_matches_selector(
                page,
                item,
                str(step.get("selector") or ""),
                selector_is_form=True,
            )
        )
        form_submit_equivalent = (
            step_op == "click"
            and action == "submit"
            and item is not None
            and bool((item.get("capabilities") or {}).get("submit"))
            and target_form_matches_selector(
                page,
                item,
                str(step.get("selector") or ""),
                selector_is_form=False,
            )
        )
        if (
            step_op != expected_op
            and not submit_button_equivalent
            and not form_submit_equivalent
        ):
            continue
        if expected_op == "reload":
            return index
        if (
            protocol.track == "L2_OBSERVATION"
            and expected_op == "click"
            and item is not None
        ):
            # L2 certifies public observation depth, not one privileged target.
            # Any model-selected click is eligible here; execution still has to
            # prove that it stayed local and changed the rendered page.
            return index
        selector = step.get("selector")
        if submit_button_equivalent or form_submit_equivalent:
            return index
        if item is not None and selector and target_matches_selector(page, item, selector):
            return index
    return None


def validate_action(
    value: dict[str, Any],
    *,
    inventory: list[dict[str, Any]],
    protocol: HiddenProtocol,
    page: Page,
    ineffective_element_ids: set[str] | None = None,
    ineffective_actions: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    required = {
        "action",
        "element_id",
        "reason",
        "visual_observations",
        "stop_status",
    }
    if set(value) != required:
        errors.append("ACTION_KEYS_INVALID")
    action = value.get("action")
    if action not in MODEL_ACTIONS:
        errors.append("ACTION_INVALID")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        errors.append("ACTION_REASON_INVALID")
    observations = value.get("visual_observations")
    if not isinstance(observations, list) or not all(
        isinstance(row, str) and row.strip() for row in observations
    ):
        errors.append("ACTION_VISUAL_OBSERVATIONS_INVALID")
    by_id = {row["element_id"]: row for row in inventory}
    if action in {"fill", "check", "click", "submit"}:
        item = by_id.get(value.get("element_id"))
        if item is None:
            errors.append("ACTION_ELEMENT_ID_INVALID")
        elif value.get("element_id") in (ineffective_element_ids or set()):
            errors.append("ACTION_ELEMENT_PREVIOUSLY_INEFFECTIVE")
        elif not bool((item.get("capabilities") or {}).get(action)):
            errors.append("ACTION_ELEMENT_CAPABILITY_INVALID")
        if value.get("stop_status") is not None:
            errors.append("ACTION_STOP_STATUS_MUST_BE_NULL")
    elif action in {"reload", "scroll_down", "scroll_up"}:
        if value.get("element_id") is not None:
            errors.append("ACTION_ELEMENT_ID_MUST_BE_NULL")
        if value.get("stop_status") is not None:
            errors.append("ACTION_STOP_STATUS_MUST_BE_NULL")
        if action in (ineffective_actions or set()):
            errors.append("ACTION_PREVIOUSLY_INEFFECTIVE")
    elif action == "stop":
        if value.get("element_id") is not None:
            errors.append("ACTION_ELEMENT_ID_MUST_BE_NULL")
        if value.get("stop_status") not in {"COMPLETE", "ABSTAIN"}:
            errors.append("ACTION_STOP_STATUS_INVALID")
        if value.get("stop_status") == "COMPLETE" and not protocol.complete:
            errors.append("ACTION_STOP_PREMATURE")
    return sorted(set(errors))


def generic_fixture_value(item: dict[str, Any]) -> str:
    field_type = str(item.get("type") or "").lower()
    if field_type == "email":
        return "mirror@example.test"
    if field_type in {"number", "tel"}:
        return "4242"
    if field_type == "url":
        return "https://case-00000000000000000000.test/start"
    if item.get("tag") == "textarea":
        return "Controlled local observation."
    return "mirror-value"


def browsergym_action_error_code(error: str) -> str:
    """Project a raw browser error to a stable, non-sensitive outcome code."""

    normalized = error.lower()
    if "intercepts pointer events" in normalized:
        return "ELEMENT_INTERCEPTED"
    if "not visible" in normalized or "hidden" in normalized:
        return "ELEMENT_NOT_VISIBLE"
    if "strict mode violation" in normalized:
        return "LOCATOR_AMBIGUOUS"
    if "timeouterror" in normalized or "timeout" in normalized:
        return "ACTION_TIMEOUT"
    if "detached" in normalized:
        return "ELEMENT_DETACHED"
    return "ACTION_EXECUTION_ERROR"


def failed_browsergym_execution(
    *,
    profile_operation: str,
    started: float,
    fixture_value: str | None,
    model_selected_l2_target: bool,
    action: str,
    step: Any,
) -> dict[str, Any]:
    """Keep an ordinary web-action failure inside the model trajectory."""

    error = str(step.action_error)
    return {
        "profile_operation": profile_operation,
        "assertion_count": 0,
        "duration_seconds": round(time.monotonic() - started, 3),
        "fixture_value_sha256": (
            sha256_text(fixture_value) if fixture_value is not None else None
        ),
        "model_selected_l2_target": model_selected_l2_target,
        "hidden_protocol_advanced": False,
        "execution_status": "ACTION_ERROR",
        "browser_harness_id": BROWSER_HARNESS_ID,
        "browsergym_action_sha256": sha256_text(action),
        "browsergym_action_error": browsergym_action_error_code(error),
        "browsergym_action_error_sha256": sha256_text(error),
        "browsergym_terminated": step.terminated,
        "browsergym_truncated": step.truncated,
    }


def execute_exploratory_action(
    *,
    decision: dict[str, Any],
    item: dict[str, Any] | None,
    session: BrowserGymSession,
) -> dict[str, Any]:
    started = time.monotonic()
    value: str | None = None
    if decision["action"] == "fill":
        if item is None:
            raise AssertionError("exploratory fill lost its target")
        value = generic_fixture_value(item)
    if decision["action"] not in {
        "fill",
        "check",
        "click",
        "submit",
        "reload",
        "scroll_down",
        "scroll_up",
    }:
        raise AssertionError("unsupported BrowserGym exploratory action")
    action = browsergym_action(
        action=decision["action"],
        bid=str(item["browsergym_id"]) if item is not None else None,
        value=value,
        select_first=item is not None and item.get("tag") == "select",
    )
    step = session.step(action)
    if step.action_error:
        return failed_browsergym_execution(
            profile_operation=f"browsergym_exploratory_{decision['action']}",
            started=started,
            fixture_value=value,
            model_selected_l2_target=False,
            action=action,
            step=step,
        )
    return {
        "profile_operation": f"browsergym_exploratory_{decision['action']}",
        "assertion_count": 0,
        "duration_seconds": round(time.monotonic() - started, 3),
        "fixture_value_sha256": sha256_text(value) if value is not None else None,
        "model_selected_l2_target": False,
        "hidden_protocol_advanced": False,
        "execution_status": "EXECUTED",
        "browser_harness_id": BROWSER_HARNESS_ID,
        "browsergym_action_sha256": sha256_text(action),
        "browsergym_action_error": None,
        "browsergym_terminated": step.terminated,
        "browsergym_truncated": step.truncated,
    }


def validate_judgment(
    value: dict[str, Any],
    *,
    evidence_ids: set[str],
    codebook: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
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
    if set(value) != required:
        errors.append("JUDGMENT_KEYS_INVALID")
    if value.get("environment_status") not in ENVIRONMENT_STATUSES:
        errors.append("JUDGMENT_ENVIRONMENT_STATUS_INVALID")
    decision = value.get("decision")
    if decision not in DECISIONS:
        errors.append("JUDGMENT_DECISION_INVALID")
    concrete_types = {
        row["id"]
        for row in codebook["primary_violation_types"]
        if row["id"] not in {"NONE", "UNKNOWN"}
    }
    primary = value.get("primary_violation_type")
    if decision == "VIOLATION" and primary not in concrete_types:
        errors.append("JUDGMENT_PRIMARY_TYPE_INVALID_FOR_VIOLATION")
    if decision == "NON_VIOLATION" and primary != "NONE":
        errors.append("JUDGMENT_PRIMARY_TYPE_INVALID_FOR_NON_VIOLATION")
    if decision == "INSUFFICIENT_EVIDENCE" and primary != "UNKNOWN":
        errors.append("JUDGMENT_PRIMARY_TYPE_INVALID_FOR_INSUFFICIENT")
    secondaries = value.get("secondary_violation_types")
    if not isinstance(secondaries, list) or any(
        row not in concrete_types or row == primary for row in secondaries or []
    ):
        errors.append("JUDGMENT_SECONDARY_TYPES_INVALID")
    allowed_anchors = {
        row["id"] for row in codebook["evidence_anchor_types"]
    }
    def validate_citations(
        rows: Any, *, field: str, require_nonempty: bool
    ) -> None:
        if not isinstance(rows, list) or (require_nonempty and not rows):
            errors.append(f"JUDGMENT_{field}_INVALID")
            return
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "evidence_id",
                "anchor_type",
                "observation",
            }:
                errors.append(f"JUDGMENT_{field}_ITEM_INVALID")
                continue
            if row.get("evidence_id") not in evidence_ids:
                errors.append(f"JUDGMENT_{field}_ID_UNKNOWN")
            if row.get("anchor_type") not in allowed_anchors:
                errors.append(f"JUDGMENT_{field}_ANCHOR_TYPE_INVALID")
            if not isinstance(row.get("observation"), str) or not row[
                "observation"
            ].strip():
                errors.append(f"JUDGMENT_{field}_OBSERVATION_INVALID")

    validate_citations(value.get("evidence"), field="EVIDENCE", require_nonempty=True)
    if not isinstance(value.get("risk_reason"), str) or not value[
        "risk_reason"
    ].strip():
        errors.append("JUDGMENT_RISK_REASON_INVALID")
    validate_citations(
        value.get("counterevidence"),
        field="COUNTEREVIDENCE",
        require_nonempty=False,
    )
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int) or not (
        1 <= confidence <= 5
    ):
        errors.append("JUDGMENT_CONFIDENCE_INVALID")
    sufficiency = value.get("evidence_sufficiency")
    if sufficiency not in EVIDENCE_SUFFICIENCY:
        errors.append("JUDGMENT_SUFFICIENCY_INVALID")
    if decision == "INSUFFICIENT_EVIDENCE" and sufficiency != "INSUFFICIENT":
        errors.append("JUDGMENT_INSUFFICIENT_CONSISTENCY_INVALID")
    note = value.get("note")
    if not isinstance(note, str) or len(note) > 1000:
        errors.append("JUDGMENT_NOTE_INVALID")
    if primary == "OTHER_PROHIBITED_ACTIVITY" and (
        not isinstance(note, str) or len(note.strip()) < 10
    ):
        errors.append("JUDGMENT_OTHER_NOTE_REQUIRED")
    serialized = canonical_json(value)
    if (
        URL_RE.search(serialized)
        or DOMAIN_RE.search(serialized)
        or IPV4_RE.search(serialized)
        or EMAIL_RE.search(serialized)
    ):
        errors.append("JUDGMENT_FORBIDDEN_IDENTIFIER")
    return sorted(set(errors))


def capture_evidence(
    *,
    page: Page,
    browsergym_observation: dict[str, Any] | None,
    case_root: Path,
    evidence_index: int,
    phase: str,
    tesseract_root: Path | None,
    full_page: bool,
) -> dict[str, Any]:
    evidence_id = f"EVID{evidence_index:04d}"
    raw_root = case_root / "internal_raw"
    visible_root = case_root / "model_visible/evidence"
    audit_root = case_root / "redaction_audits"
    for path in (raw_root, visible_root, audit_root):
        path.mkdir(parents=True, exist_ok=True)
    raw_path = raw_root / f"{evidence_id}.png"
    visible_path = visible_root / f"{evidence_id}.png"
    capture_scope = "FULL_PAGE" if full_page else "VIEWPORT"
    capture_source = "PLAYWRIGHT_FULL_PAGE_EXTENSION"
    screenshot = (
        browsergym_observation.get("screenshot")
        if isinstance(browsergym_observation, dict)
        else None
    )
    if not full_page and screenshot is not None:
        Image.fromarray(screenshot).save(raw_path, format="PNG")
        capture_source = "BROWSERGYM_OBSERVATION"
    else:
        try:
            page.screenshot(
                path=str(raw_path), full_page=full_page, animations="disabled"
            )
        except Exception:
            page.screenshot(
                path=str(raw_path), full_page=False, animations="disabled"
            )
            capture_scope = "VIEWPORT_FALLBACK"
            capture_source = "PLAYWRIGHT_VIEWPORT_FALLBACK"
    audit_path = audit_root / f"{evidence_id}.json"
    audit = redact_screenshot(
        raw_path,
        visible_path,
        audit_path,
        tesseract_root=tesseract_root,
    )
    return {
        "evidence_id": evidence_id,
        "phase": phase,
        "capture_scope": capture_scope,
        "capture_source": capture_source,
        "browser_harness_id": BROWSER_HARNESS_ID,
        "unmarked_screenshot": True,
        "model_visible_path": str(visible_path),
        "model_visible_sha256": audit["derived"]["sha256"],
        "parent_sha256": audit["parent"]["sha256"],
        "redaction_audit_path": str(audit_path),
        "redaction_status": audit["status"],
    }


def sampled_evidence(
    evidence: list[dict[str, Any]],
    max_images: int,
    max_image_bytes: int,
) -> list[dict[str, Any]]:
    """Evenly sample evidence while keeping the multimodal request bounded."""

    def sample_count(count: int) -> list[dict[str, Any]]:
        if len(evidence) <= count:
            return evidence
        if count < 2:
            return [evidence[-1]]
        selected = {0, len(evidence) - 1}
        slots = count - 2
        for index in range(1, slots + 1):
            selected.add(round(index * (len(evidence) - 1) / (slots + 1)))
        return [evidence[index] for index in sorted(selected)][:count]

    for count in range(min(len(evidence), max_images), 1, -1):
        rows = sample_count(count)
        total = sum(
            Path(row["model_visible_path"]).stat().st_size for row in rows
        )
        if total <= max_image_bytes:
            return rows
    return sample_count(1)


def should_capture_full_page(
    *,
    full_page_enabled: bool,
    evidence: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    protocol_complete: bool,
) -> bool:
    if not full_page_enabled:
        return False
    if not evidence or protocol_complete:
        return True
    if not actions:
        return False
    last = actions[-1]
    execution = last.get("controller_execution") or {}
    return bool(
        (
            last.get("page_changed")
            and last.get("action") not in {"fill", "check"}
        )
        or last.get("action") == "reload"
        or int(execution.get("assertion_count") or 0) > 0
    )


def run_hidden_checks(
    *,
    page: Page,
    context: Any,
    steps: list[dict[str, Any]],
    base_url: str,
    responses: list[dict[str, Any]],
    screenshots_dir: Path,
    scenario_id: str,
) -> int:
    assertions = 0
    for step in steps:
        execute_step(
            page,
            context,
            step,
            base_url=base_url,
            responses=responses,
            screenshots_dir=screenshots_dir,
            scenario_id=scenario_id,
        )
        if str(step.get("op", "")).startswith("expect_"):
            assertions += 1
    return assertions


def execute_matched_phase(
    *,
    protocol: HiddenProtocol,
    phase_index: int,
    session: BrowserGymSession,
    page: Page,
    context: Any,
    base_url: str,
    responses: list[dict[str, Any]],
    screenshots_dir: Path,
    decision: dict[str, Any],
    item: dict[str, Any] | None,
) -> dict[str, Any]:
    phase = protocol.phases[phase_index]
    hidden_step = phase["action"]
    started = time.monotonic()
    model_selected_l2_click = (
        protocol.track == "L2_OBSERVATION"
        and hidden_step.get("op") == "click"
        and decision.get("action") == "click"
    )
    if decision.get("action") != "reload" and item is None:
        raise AssertionError("BrowserGym action has no model-selected target")
    before_state = page_state_fingerprint(page)
    fixture_value = (
        str(hidden_step["value"])
        if decision.get("action") == "fill" and "value" in hidden_step
        else (
            generic_fixture_value(item)
            if decision.get("action") == "fill" and item is not None
            else None
        )
    )
    action = browsergym_action(
        action=str(decision["action"]),
        bid=(
            str(item["browsergym_id"])
            if item is not None
            else None
        ),
        value=fixture_value,
        select_first=(
            decision.get("action") == "fill"
            and item is not None
            and item.get("tag") == "select"
        ),
    )
    step_result = session.step(action)
    if step_result.action_error:
        return failed_browsergym_execution(
            profile_operation=str(hidden_step["op"]),
            started=started,
            fixture_value=fixture_value,
            model_selected_l2_target=model_selected_l2_click,
            action=action,
            step=step_result,
        )
    page.wait_for_timeout(int(hidden_step.get("settle_ms", 500)))
    session.refresh()
    if model_selected_l2_click:
        after_url = urlparse(page.url)
        if (
            after_url.hostname not in {"127.0.0.1", "localhost"}
            or after_url.port != urlparse(base_url).port
        ):
            raise AssertionError("L2 model-selected click left local replay")
        state_changed = before_state != page_state_fingerprint(page)
        if not state_changed:
            return {
                "profile_operation": hidden_step["op"],
                "assertion_count": 0,
                "duration_seconds": round(time.monotonic() - started, 3),
                "fixture_value_sha256": None,
                "model_selected_l2_target": True,
                "hidden_protocol_advanced": False,
                "execution_status": "NO_STATE_CHANGE",
                "browser_harness_id": BROWSER_HARNESS_ID,
                "browsergym_action_sha256": sha256_text(action),
                "browsergym_action_error": None,
                "browsergym_terminated": step_result.terminated,
                "browsergym_truncated": step_result.truncated,
            }
    assertions = run_hidden_checks(
        page=page,
        context=context,
        steps=phase["checks"],
        base_url=base_url,
        responses=responses,
        screenshots_dir=screenshots_dir,
        scenario_id=str(protocol.scenario["id"]),
    )
    protocol.assertion_count += assertions
    completed = {
        "profile_operation": hidden_step["op"],
        "assertion_count": assertions,
        "duration_seconds": round(time.monotonic() - started, 3),
        "fixture_value_sha256": (
            sha256_text(fixture_value)
            if fixture_value is not None
            else None
        ),
        "model_selected_l2_target": model_selected_l2_click,
        "hidden_protocol_advanced": True,
        "execution_status": "EXECUTED",
        "browser_harness_id": BROWSER_HARNESS_ID,
        "browsergym_action_sha256": sha256_text(action),
        "browsergym_action_error": None,
        "browsergym_terminated": step_result.terminated,
        "browsergym_truncated": step_result.truncated,
    }
    protocol.completed_actions.append(completed)
    del protocol.phases[phase_index]
    return completed


def action_target_projection(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        key: value
        for key, value in item.items()
        if key not in {"dom_index", "browsergym_id"}
    }


def run_case(
    *,
    binding: dict[str, Any],
    track: str,
    source_path: Path,
    scenario: dict[str, Any],
    model: str,
    prompts: dict[str, str],
    codebook: dict[str, Any],
    base_url_model: str,
    api_key: str,
    transport: str,
    output_dir: Path,
    tesseract_root: Path | None,
    max_steps: int,
    max_case_seconds: int,
    max_tokens: int,
    max_judge_images: int,
    max_judge_image_bytes: int,
    full_page: bool,
    run_inline_judge: bool,
    prompts_path: Path,
    codebook_path: Path,
    resolved_model_allowlist: set[str],
    runtime_root: Path,
    runtime_integrity: dict[str, Any],
) -> dict[str, Any]:
    case_ref = str(binding["case_ref"])
    sample_id = primary_sample_id(binding)
    case_root = output_dir / "cases" / case_ref
    case_root.mkdir(parents=True, exist_ok=True)
    call_root = case_root / "model_calls"
    observation_root = case_root / "model_visible/observations"
    case_runtime_state = case_root / "runtime_state"
    hidden_screenshots = case_root / "internal_raw/hidden_checks"
    for path in (
        call_root,
        observation_root,
        case_runtime_state,
        hidden_screenshots,
    ):
        path.mkdir(parents=True, exist_ok=True)

    protocol = HiddenProtocol.from_scenario(track, scenario)
    calls: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    external_attempts: list[str] = []
    responses: list[dict[str, Any]] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    started_at = utc_now()
    started_monotonic = time.monotonic()
    case_deadline_monotonic = started_monotonic + max_case_seconds
    model_stop_status: str | None = None
    system_failure: str | None = None
    model_failures: list[dict[str, Any]] = []
    ineffective_element_ids: set[str] = set()
    ineffective_actions: set[str] = set()
    system_failure_kind: str | None = None

    def assert_case_budget(phase: str) -> None:
        if time.monotonic() >= case_deadline_monotonic:
            raise CaseWallTimeout(
                f"case exceeded {max_case_seconds}s wall-clock budget "
                f"before {phase}"
            )

    def invoke(
        *,
        system_prompt: str,
        payload: dict[str, Any],
        images: list[tuple[str, Path]],
        phase: str,
        validator: Callable[[dict[str, Any]], list[str]],
    ) -> dict[str, Any]:
        assert_case_budget(phase)
        call_index = len(calls) + 1
        payload_path = observation_root / f"{call_index:03d}_{phase}.json"
        atomic_json(payload_path, payload)
        try:
            parsed, audit = call_model_json(
                base_url=base_url_model,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_payload=payload,
                images=images,
                phase=phase,
                case_ref=case_ref,
                max_tokens=max_tokens if phase == "web_action" else max(max_tokens, 1800),
                call_index=call_index,
                validator=validator,
                audit_path=call_root / f"{call_index:03d}_{phase}.json",
                request_protocol="task2-autonomous-mllm/v0.2",
                notes_task_prefix="task2-autonomous-mllm",
                notes_extra="fixed-mllm-autonomous-local-browser-v0.2",
                deadline_monotonic=case_deadline_monotonic,
                transport=transport,
            )
        except ModelCallError as exc:
            exc.audit["model_visible_payload_path"] = str(payload_path)
            exc.audit["model_visible_payload_sha256"] = sha256_file(payload_path)
            atomic_json(call_root / f"{call_index:03d}_{phase}.json", exc.audit)
            calls.append(exc.audit)
            atomic_json(case_root / "model_calls.json", calls)
            if exc.audit.get("deadline_exhausted"):
                raise CaseWallTimeout(
                    f"case exceeded {max_case_seconds}s wall-clock budget "
                    f"during {phase}"
                ) from exc
            raise
        audit["model_visible_payload_path"] = str(payload_path)
        audit["model_visible_payload_sha256"] = sha256_file(payload_path)
        atomic_json(call_root / f"{call_index:03d}_{phase}.json", audit)
        calls.append(audit)
        atomic_json(case_root / "model_calls.json", calls)
        return parsed

    def record_model_call_failure(exc: ModelCallError) -> bool:
        kind = classify_model_call_failure(exc.audit)
        if kind == "TRANSPORT_ERROR":
            return False
        model_failures.append(
            {
                "phase": exc.phase,
                "kind": kind,
                "call_index": exc.audit.get("call_index"),
                "attempt_count": len(exc.audit.get("attempts") or []),
                "audit_status": exc.audit.get("status"),
            }
        )
        return True

    site_out = runtime_root / str(binding["site_output"])
    binary = site_out / "mirrorserve"
    site_dir = site_out / "site"
    observed_runtime_binary_sha256 = sha256_file(binary)
    if (
        observed_runtime_binary_sha256
        != runtime_integrity["expected_runtime_binary_sha256"]
    ):
        raise ValueError("runtime binary hash mismatch")
    log_handle = (case_root / "mirrorserve.log").open("w", encoding="utf-8")
    process = None
    session: BrowserGymSession | None = None
    try:
        process, port = start_local_runtime_process(
            binary,
            site_dir,
            case_runtime_state,
            port=free_port(),
            dynamic_port=True,
            log=log_handle,
        )
        local_base = f"http://127.0.0.1:{port}"
        session = BrowserGymSession(
            start_url=local_base,
            allowed_port=port,
            viewport={
                "width": int(scenario.get("viewport_width", 1280)),
                "height": int(scenario.get("viewport_height", 900)),
            },
            timeout_ms=30_000,
            external_attempts=external_attempts,
            responses=responses,
            console_errors=console_errors,
            page_errors=page_errors,
        )
        session.reset(seed=int(sha256_text(case_ref)[:8], 16))
        page = session.page
        context = session.context
        try:
                protocol.assertion_count += run_hidden_checks(
                    page=page,
                    context=context,
                    steps=protocol.prelude,
                    base_url=local_base,
                    responses=responses,
                    screenshots_dir=hidden_screenshots,
                    scenario_id=str(scenario["id"]),
                )
                session.refresh()
                for sequence in range(1, max_steps + 1):
                    assert_case_budget("web_action")
                    state, inventory = observe_page(page)
                    evidence_row = capture_evidence(
                        page=page,
                        browsergym_observation=session.observation,
                        case_root=case_root,
                        evidence_index=len(evidence) + 1,
                        phase="FINAL_STATE" if protocol.complete else "BEFORE_ACTION",
                        tesseract_root=tesseract_root,
                        full_page=should_capture_full_page(
                            full_page_enabled=full_page,
                            evidence=evidence,
                            actions=actions,
                            protocol_complete=protocol.complete,
                        ),
                    )
                    evidence.append(evidence_row)
                    payload = {
                        "schema_version": "task2-autonomous-action-input/v0.2",
                        "phase": "LOCAL_BROWSER_EVIDENCE_SEEKING",
                        "upstream_context_policy": UPSTREAM_CONTEXT_POLICY,
                        "task": (
                            "Investigate whether this isolated local website violates the "
                            "supplied policy and collect directly observable evidence for a "
                            "later final decision and violation-type classification."
                        ),
                        "network_policy": "LOCAL_REPLAY_ONLY",
                        "policy_codebook": codebook_projection(codebook),
                        "case_budget": {
                            "max_browser_actions": max_steps,
                            "remaining_browser_actions": max_steps - len(actions),
                            "wall_clock_limit_seconds": max_case_seconds,
                            "finish_early_when_sufficient": True,
                        },
                        "synthetic_values": (
                            "Controller-injected when an eligible field is selected; never "
                            "shown to the model."
                        ),
                        "coverage_objectives": coverage_objectives(protocol),
                        "current_state": state,
                        "interactive_elements": [
                            {
                                key: value
                                for key, value in row.items()
                                if key not in {"dom_index", "browsergym_id"}
                            }
                            for row in inventory
                        ],
                        "progress": {
                            "executed_action_count": len(actions),
                            "observed_state_count": len(evidence),
                            "verification_depth_complete": protocol.complete,
                        },
                        "ineffective_element_ids": sorted(ineffective_element_ids),
                        "ineffective_actions": sorted(ineffective_actions),
                        "recent_actions": [
                            {
                                "sequence": row["sequence"],
                                "action": row["action"],
                                "target": row.get("target"),
                                "page_changed": row["page_changed"],
                                "execution_status": (
                                    row.get("controller_execution") or {}
                                ).get("execution_status"),
                                "before_page_ref": row.get("before_page_ref"),
                                "after_page_ref": row.get("after_page_ref"),
                            }
                            for row in actions[-6:]
                        ],
                        "available_evidence_id": evidence_row["evidence_id"],
                        "forbidden_inputs": [
                            "URL",
                            "DOMAIN",
                            "IP",
                            "REPUTATION",
                            "SOURCE_OR_SEED_LABEL",
                            "HIDDEN_SCENARIO",
                            "CSS_SELECTOR",
                            "FIXTURE_VALUE",
                        ],
                    }
                    projected_payload = sanitize_model_value(payload)
                    assert_model_boundary_safe(projected_payload)
                    try:
                        decision = invoke(
                            system_prompt=prompts["action_system"],
                            payload=projected_payload,
                            images=[
                                (
                                    evidence_row["evidence_id"],
                                    Path(evidence_row["model_visible_path"]),
                                )
                            ],
                            phase="web_action",
                            validator=lambda value: validate_action(
                                value,
                                inventory=inventory,
                                protocol=protocol,
                                page=page,
                                ineffective_element_ids=ineffective_element_ids,
                                ineffective_actions=ineffective_actions,
                            ),
                        )
                    except ModelCallError as exc:
                        if not record_model_call_failure(exc):
                            raise
                        model_stop_status = model_failures[-1]["kind"]
                        break
                    if decision["action"] == "stop":
                        model_stop_status = decision["stop_status"]
                        if model_stop_status != "COMPLETE":
                            model_failures.append(
                                {
                                    "phase": "web_action",
                                    "kind": "ABSTAIN",
                                    "call_index": calls[-1].get("call_index"),
                                    "attempt_count": len(
                                        calls[-1].get("attempts") or []
                                    ),
                                    "audit_status": calls[-1].get("status"),
                                }
                            )
                        break
                    phase_index = match_hidden_phase(
                        protocol, decision, inventory, page
                    )
                    item = next(
                        (
                            row
                            for row in inventory
                            if row["element_id"] == decision.get("element_id")
                        ),
                        None,
                    )
                    if item is None and decision["action"] not in {
                        "reload",
                        "scroll_down",
                        "scroll_up",
                    }:
                        raise AssertionError("validated action lost its target")
                    before_state = page_state_fingerprint(page)
                    if phase_index is None:
                        execution = execute_exploratory_action(
                            decision=decision,
                            item=item,
                            session=session,
                        )
                    else:
                        execution = execute_matched_phase(
                            protocol=protocol,
                            phase_index=phase_index,
                            session=session,
                            page=page,
                            context=context,
                            base_url=local_base,
                            responses=responses,
                            screenshots_dir=hidden_screenshots,
                            decision=decision,
                            item=item,
                        )
                    after_state = page_state_fingerprint(page)
                    page_changed = before_state != after_state
                    if (
                        phase_index is None
                        and not page_changed
                        and execution.get("execution_status") == "EXECUTED"
                    ):
                        execution["execution_status"] = "NO_STATE_CHANGE"
                    if execution.get("execution_status") in {
                        "NO_STATE_CHANGE",
                        "ACTION_ERROR",
                    }:
                        if decision.get("element_id") is not None:
                            ineffective_element_ids.add(
                                str(decision["element_id"])
                            )
                        else:
                            ineffective_actions.add(str(decision["action"]))
                    actions.append(
                        {
                            "sequence": sequence,
                            "action": decision["action"],
                            "target": action_target_projection(item),
                            "reason": safe_text(decision["reason"], 600),
                            "visual_observations": [
                                safe_text(row, 400)
                                for row in decision["visual_observations"]
                            ],
                            "evidence_id": evidence_row["evidence_id"],
                            "decision_source": "MODEL",
                            "fixture_value_sent_to_model": False,
                            "controller_execution": execution,
                            "before_page_ref": "STATE-" + before_state[:16],
                            "after_page_ref": "STATE-" + after_state[:16],
                            "page_changed": page_changed,
                        }
                    )
                    atomic_json(case_root / "trajectory.partial.json", actions)
                else:
                    model_stop_status = "MAX_STEPS"
                    model_failures.append(
                        {
                            "phase": "web_action",
                            "kind": "MAX_STEPS",
                            "call_index": calls[-1].get("call_index") if calls else None,
                            "attempt_count": 0,
                            "audit_status": "PASS",
                        }
                    )
        finally:
            session.close()
            session = None
    except CaseWallTimeout as exc:
        system_failure_kind = "CASE_WALL_TIMEOUT"
        system_failure = system_failure or str(exc)[:1000]
    except Exception as exc:
        system_failure_kind = system_failure_kind or type(exc).__name__
        system_failure = system_failure or f"{type(exc).__name__}: {str(exc)[:1000]}"
    finally:
        if session is not None:
            session.close()
        stop_process(process)
        log_handle.close()

    if not evidence:
        raise RuntimeError(system_failure or "no model-visible evidence was captured")

    judge_rows = sampled_evidence(
        evidence,
        max_judge_images,
        max_judge_image_bytes,
    )
    judge_payload = {
        "schema_version": "task2-evidence-grounded-judgment-input/v0.1",
        "phase": "EVIDENCE_GROUNDED_POLICY_JUDGMENT",
        "upstream_context_policy": UPSTREAM_CONTEXT_POLICY,
        "inspection_summary": {
            "local_only": True,
            "verification_depth_complete": protocol.complete,
            "executed_action_count": len(actions),
            "observed_state_count": len(evidence),
            "external_request_attempt_count": len(external_attempts),
        },
        "action_ledger": [
            {
                "sequence": row["sequence"],
                "action": row["action"],
                "target": row["target"],
                "evidence_id": row["evidence_id"],
                "page_changed": row["page_changed"],
                "execution_status": (
                    row.get("controller_execution") or {}
                ).get("execution_status"),
            }
            for row in actions
        ],
        "available_evidence_ids": [row["evidence_id"] for row in judge_rows],
        "policy_codebook": codebook_projection(codebook),
        "forbidden_shortcuts": [
            "URL",
            "DOMAIN",
            "IP",
            "REPUTATION",
            "SOURCE_OR_SEED_LABEL",
            "HIDDEN_SCENARIO",
        ],
    }
    judgment: dict[str, Any] | None = None
    if run_inline_judge and system_failure is None:
        try:
            judgment = invoke(
                system_prompt=prompts["judge_system"],
                payload=judge_payload,
                images=[
                    (row["evidence_id"], Path(row["model_visible_path"]))
                    for row in judge_rows
                ],
                phase="final_judge",
                validator=lambda value: validate_judgment(
                    value,
                    evidence_ids={row["evidence_id"] for row in judge_rows},
                    codebook=codebook,
                ),
            )
        except ModelCallError as exc:
            if not record_model_call_failure(exc):
                system_failure_kind = "MODEL_TRANSPORT_ERROR"
                system_failure = (
                    f"ModelCallError: transport failure in phase {exc.phase}"
                )
        except CaseWallTimeout as exc:
            system_failure_kind = "CASE_WALL_TIMEOUT"
            system_failure = str(exc)[:1000]
        except Exception as exc:
            system_failure_kind = type(exc).__name__
            system_failure = f"{type(exc).__name__}: {str(exc)[:1000]}"

    resolved_models = sorted(
        {
            str(call.get("response_model"))
            for call in calls
            if call.get("response_model")
        }
    )
    if external_attempts and system_failure is None:
        system_failure_kind = "ZERO_EGRESS_VIOLATION"
        system_failure = "blocked external request attempts were observed"
    if set(resolved_models) != resolved_model_allowlist and system_failure is None:
        system_failure_kind = "MODEL_ROUTE_MISMATCH"
        system_failure = (
            "resolved model route mismatch: "
            + canonical_json(
                {
                    "expected": sorted(resolved_model_allowlist),
                    "actual": resolved_models,
                }
            )
        )
    if (
        run_inline_judge
        and judgment is None
        and not any(row["phase"] == "final_judge" for row in model_failures)
        and system_failure is None
    ):
        system_failure_kind = "FINAL_JUDGMENT_MISSING"
        system_failure = "final judgment missing without a recorded model failure"
    if (
        not protocol.complete
        and not model_failures
        and system_failure is None
    ):
        model_failures.append(
            {
                "phase": "web_action",
                "kind": "INVALID_RESPONSE",
                "call_index": calls[-1].get("call_index") if calls else None,
                "attempt_count": 0,
                "audit_status": "INCOMPLETE_PROTOCOL",
            }
        )
        model_stop_status = model_stop_status or "INVALID_RESPONSE"

    passed = (
        system_failure is None
        and not model_failures
        and protocol.complete
        and model_stop_status == "COMPLETE"
        and ((run_inline_judge and judgment is not None) or not run_inline_judge)
    )
    if system_failure is not None:
        case_status = "FAIL"
    elif model_failures:
        case_status = "MODEL_FAILURE"
    elif passed:
        case_status = "PASS"
    else:
        case_status = "FAIL"
        system_failure_kind = "UNCLASSIFIED_COMPLETION_INVARIANT"
        system_failure = "unclassified case completion invariant failure"
    trajectory_path = case_root / "trajectory.json"
    trajectory = {
        "schema_version": "riskchainbench-browser-trajectory/v0.1",
        "case_ref": case_ref,
        "browser_harness": {
            "backend": BROWSER_BACKEND,
            "harness_id": BROWSER_HARNESS_ID,
            "browsergym_core_version": BROWSERGYM_CORE_VERSION,
            "playwright_version": PLAYWRIGHT_VERSION,
        },
        "started_at": started_at,
        "finished_at": utc_now(),
        "case_wall_budget_seconds": max_case_seconds,
        "case_elapsed_seconds": round(
            time.monotonic() - started_monotonic,
            3,
        ),
        "model_stop_status": model_stop_status,
        "hidden_protocol_complete": protocol.complete,
        "actions": actions,
        "evidence_sequence": [
            {
                "observation_index": index,
                "evidence_id": row["evidence_id"],
                "phase": row["phase"],
                "capture_scope": row["capture_scope"],
                "capture_source": row["capture_source"],
                "model_visible_sha256": row["model_visible_sha256"],
            }
            for index, row in enumerate(evidence, 1)
        ],
    }
    trajectory["trajectory_sha256"] = embedded_hash(
        trajectory, "trajectory_sha256"
    )
    atomic_json(trajectory_path, trajectory)
    network_audit_path = case_root / "network_audit.json"
    network_audit = {
        "schema_version": "riskchainbench-zero-egress-audit/v0.1",
        "case_ref": case_ref,
        "policy": "LOCAL_REPLAY_ONLY",
        "allowed_origin_port": port,
        "external_request_attempt_count": len(external_attempts),
        "external_request_attempt_sha256s": external_attempts,
        "local_response_count": len(responses),
        "zero_external_request_attempts": not external_attempts,
        "status": "PASS" if not external_attempts else "FAIL",
    }
    network_audit["audit_sha256"] = embedded_hash(
        network_audit, "audit_sha256"
    )
    atomic_json(network_audit_path, network_audit)
    runtime_attestation_path = case_root / "runtime_attestation.json"
    runtime_attestation = {
        "schema_version": "riskchainbench-task2-runtime-attestation/v0.1",
        "case_ref": case_ref,
        "browser_backend": BROWSER_BACKEND,
        "browser_harness_id": BROWSER_HARNESS_ID,
        "browsergym_core_version": BROWSERGYM_CORE_VERSION,
        "playwright_version": PLAYWRIGHT_VERSION,
        "expected_browsergym_core_version": EXPECTED_BROWSERGYM_CORE_VERSION,
        "expected_playwright_version": EXPECTED_PLAYWRIGHT_VERSION,
        "runtime_binary_sha256": observed_runtime_binary_sha256,
        "case_wall_budget_seconds": max_case_seconds,
        "runtime_integrity": runtime_integrity,
        "source_path_sha256": sha256_file(source_path),
        "prompt_sha256": sha256_file(prompts_path),
        "codebook_sha256": sha256_file(codebook_path),
    }
    runtime_attestation["attestation_sha256"] = embedded_hash(
        runtime_attestation, "attestation_sha256"
    )
    atomic_json(runtime_attestation_path, runtime_attestation)
    model_calls_path = case_root / "model_calls.json"
    condition = "task2_standalone"
    judgment_mode = (
        INLINE_JUDGMENT_MODE if run_inline_judge else DEFERRED_JUDGMENT_MODE
    )
    judge_handoff_path = case_root / "judge_handoff.json"
    judge_handoff = {
        "schema_version": "riskchainbench-fixed-judge-handoff/v0.1",
        "case_ref": case_ref,
        "sample_id": sample_id,
        "tested_model": model,
        "transport": transport,
        "execution_mode": EXECUTION_MODE,
        "judgment_mode": judgment_mode,
        "status": (
            "INLINE_DIAGNOSTIC_COMPLETE"
            if judgment is not None
            else "PENDING_FIXED_EXTERNAL_JUDGE"
        ),
        "judge_input": judge_payload,
        "candidate_evidence": [
            {
                "evidence_id": row["evidence_id"],
                "phase": row["phase"],
                "capture_scope": row["capture_scope"],
                "screenshot": artifact_file_ref(
                    Path(row["model_visible_path"])
                ),
            }
            for row in judge_rows
        ],
        "artifact_refs": {
            "trajectory": artifact_file_ref(trajectory_path),
            "model_calls": artifact_file_ref(model_calls_path),
            "network_audit": artifact_file_ref(network_audit_path),
            "runtime_attestation": artifact_file_ref(runtime_attestation_path),
            "prompt_bundle": artifact_file_ref(prompts_path),
            "codebook": artifact_file_ref(codebook_path),
        },
        "expected_output": {
            "classification": [
                "environment_status",
                "decision",
                "primary_violation_type",
                "secondary_violation_types",
                "evidence",
                "counterevidence",
                "confidence",
                "note",
            ],
            "evidence_quality_dimensions": [
                "evidence_faithfulness",
                "evidence_sufficiency",
                "investigation_completeness",
                "reasoning_consistency",
            ],
        },
        "claim_boundary": {
            "human_gold_present": False,
            "fixed_judge_applied": judgment is not None,
            "accuracy_or_f1_allowed": False,
            "formal_evidence_score_allowed": False,
        },
    }
    judge_handoff["handoff_sha256"] = embedded_hash(
        judge_handoff, "handoff_sha256"
    )
    atomic_json(judge_handoff_path, judge_handoff)
    evidence_package_path = case_root / "evidence_package.json"
    evidence_package = {
        "schema_version": "riskchainbench-evidence-package/v0.1",
        "case_ref": case_ref,
        "sample_id": sample_id,
        "evaluation_condition": condition,
        "execution_mode": EXECUTION_MODE,
        "judgment_mode": judgment_mode,
        "requested_model": model,
        "resolved_models": resolved_models,
        "transport": transport,
        "created_at": utc_now(),
        "outcome": {
            "status": case_status,
            "pipeline_evaluable": case_status in {"PASS", "MODEL_FAILURE"},
            "model_stop_status": model_stop_status,
            "hidden_protocol_complete": protocol.complete,
            "model_failures": model_failures,
            "system_failure": system_failure,
            "system_failure_kind": system_failure_kind,
            "judgment": judgment,
            "human_annotation_projection": human_annotation_projection(judgment),
        },
        "standalone_binding": {
            "case_ref": case_ref,
            "sample_id": sample_id,
            "controller_source": "FROZEN_PRIVATE_RESOLVER",
            "task1_prediction_required": False,
            "model_visible": False,
        },
        "browser_harness": {
            "backend": BROWSER_BACKEND,
            "harness_id": BROWSER_HARNESS_ID,
            "browsergym_core_version": BROWSERGYM_CORE_VERSION,
            "playwright_version": PLAYWRIGHT_VERSION,
            "max_browser_actions": max_steps,
            "wall_clock_limit_seconds": max_case_seconds,
            "executed_action_count": len(actions),
        },
        "evidence_items": [
            {
                "evidence_id": row["evidence_id"],
                "phase": row["phase"],
                "capture_scope": row["capture_scope"],
                "capture_source": row["capture_source"],
                "selected_for_fixed_judge": row["evidence_id"]
                in {selected["evidence_id"] for selected in judge_rows},
                "model_visible_screenshot": artifact_file_ref(
                    Path(row["model_visible_path"])
                ),
                "redaction_audit": artifact_file_ref(
                    Path(row["redaction_audit_path"])
                ),
            }
            for row in evidence
        ],
        "model_visible_observations": [
            {
                "call_index": call["call_index"],
                "phase": call["phase"],
                "payload": artifact_file_ref(
                    Path(call["model_visible_payload_path"])
                ),
            }
            for call in calls
            if call.get("model_visible_payload_path")
        ],
        "model_call_audits": [
            {
                "call_index": call["call_index"],
                "phase": call["phase"],
                "status": call["status"],
                "audit": artifact_file_ref(
                    call_root
                    / f"{int(call['call_index']):03d}_{call['phase']}.json"
                ),
            }
            for call in calls
        ],
        "artifact_refs": {
            "trajectory": artifact_file_ref(trajectory_path),
            "model_calls": artifact_file_ref(model_calls_path),
            "network_audit": artifact_file_ref(network_audit_path),
            "runtime_attestation": artifact_file_ref(runtime_attestation_path),
            "fixed_judge_handoff": artifact_file_ref(judge_handoff_path),
            "prompt": artifact_file_ref(prompts_path),
            "codebook": artifact_file_ref(codebook_path),
        },
        "publish_scope": {
            "includes_only_model_visible_evidence": True,
            "internal_raw_screenshots_excluded": True,
            "hidden_protocol_source_excluded": True,
            "fixture_values_excluded": True,
        },
        "claim_boundary": {
            "human_gold_present": False,
            "accuracy_or_f1_allowed": False,
            "fixed_external_evidence_judge_applied": judgment is not None,
            "formal_evidence_score_allowed": False,
        },
    }
    evidence_package["evidence_package_sha256"] = embedded_hash(
        evidence_package, "evidence_package_sha256"
    )
    atomic_json(evidence_package_path, evidence_package)
    result = {
        "schema_version": "task2-autonomous-mllm-case/v0.4",
        "case_ref": case_ref,
        "sample_id": sample_id,
        "evaluation_condition": condition,
        "execution_mode": EXECUTION_MODE,
        "judgment_mode": judgment_mode,
        "requested_model": model,
        "resolved_models": resolved_models,
        "transport": transport,
        "runtime_integrity": {
            **runtime_integrity,
            "observed_runtime_binary_sha256": (
                observed_runtime_binary_sha256
            ),
        },
        "track": "BROWSERGYM_MULTIMODAL_BROWSER",
        "mirror_track": track,
        "started_at": started_at,
        "finished_at": utc_now(),
        "case_wall_budget_seconds": max_case_seconds,
        "case_elapsed_seconds": round(
            time.monotonic() - started_monotonic,
            3,
        ),
        "status": case_status,
        "pipeline_evaluable": case_status in {"PASS", "MODEL_FAILURE"},
        "model_failures": model_failures,
        "standalone_binding": {
            "case_ref": case_ref,
            "sample_id": sample_id,
            "controller_source": "FROZEN_PRIVATE_RESOLVER",
            "task1_prediction_required": False,
            "model_visible": False,
        },
        "web_input_boundary": {
            "policy": "SITE_ONLY_EVIDENCE_INVESTIGATION",
            "task1_message_model_visible": False,
            "task1_intent_model_visible": False,
            "task1_entry_model_visible": False,
            "local_binding_model_visible": False,
            "paired_browser_conditions_executed": False,
            "task2_executed_once_per_model_site": True,
            "fixed_external_judge_deferred": True,
        },
        "browser_execution": {
            "browser_backend": BROWSER_BACKEND,
            "browser_harness_id": BROWSER_HARNESS_ID,
            "browsergym_core_version": BROWSERGYM_CORE_VERSION,
            "playwright_version": PLAYWRIGHT_VERSION,
            "browsergym_action_execution": True,
            "autonomous_action_selection": True,
            "unmarked_model_screenshots": True,
            "hidden_scenario_not_model_visible": True,
            "synthetic_fixture_values_not_model_visible": True,
            "hidden_protocol_source_path": str(source_path),
            "hidden_protocol_source_sha256": sha256_file(source_path),
            "hidden_scenario_id": scenario["id"],
            "hidden_protocol_complete": protocol.complete,
            "model_stop_status": model_stop_status,
            "executed_action_count": len(actions),
            "hidden_assertion_count": protocol.assertion_count,
            "remaining_hidden_action_count": len(protocol.phases),
            "actions": actions,
            "external_request_attempt_count": len(external_attempts),
            "external_request_attempt_sha256s": external_attempts,
            "local_responses": [
                {
                    "path": row["path"],
                    "method": row["method"],
                    "status": row["status"],
                    "resource_type": row["resource_type"],
                }
                for row in responses
            ],
            "console_errors": console_errors,
            "page_errors": page_errors,
        },
        "evidence": evidence,
        "artifact_refs": {
            "trajectory": artifact_file_ref(trajectory_path),
            "network_audit": artifact_file_ref(network_audit_path),
            "runtime_attestation": artifact_file_ref(runtime_attestation_path),
            "fixed_judge_handoff": artifact_file_ref(judge_handoff_path),
            "evidence_package": artifact_file_ref(evidence_package_path),
        },
        "fixed_judge_candidate_evidence_ids": [
            row["evidence_id"] for row in judge_rows
        ],
        "fixed_judge_candidate_image_bytes": sum(
            Path(row["model_visible_path"]).stat().st_size for row in judge_rows
        ),
        "judgment": judgment,
        "human_annotation_projection": human_annotation_projection(judgment),
        "judgment_status": (
            "PENDING_FIXED_EXTERNAL_JUDGE"
            if not run_inline_judge
            else (
                "INLINE_DIAGNOSTIC_UNSCORED_NO_HUMAN_GOLD"
                if judgment is not None
                else (
                    "MODEL_JUDGMENT_FAILURE"
                    if any(
                        row["phase"] == "final_judge"
                        for row in model_failures
                    )
                    else "SYSTEM_JUDGMENT_FAILURE"
                )
            )
        ),
        "model_calls": calls,
        "failure": system_failure,
        "failure_kind": system_failure_kind,
        "claim_boundary": {
            "human_gold_present": False,
            "accuracy_or_f1_allowed": False,
            "sampling_stratum_used_as_gold": False,
            "trajectory_collection_only": True,
            "fixed_external_judge_applied": judgment is not None,
            "formal_evidence_score_allowed": False,
        },
    }
    atomic_json(case_root / "case_result.json", result)
    (case_root / "trajectory.partial.json").unlink(missing_ok=True)
    return result


def execute_case_job(
    job: dict[str, Any],
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    """Execute one case in a process-isolated BrowserGym worker."""

    ordinal = int(job["ordinal"])
    case_ref = str(job["case_ref"])
    output_dir = Path(job["run_kwargs"]["output_dir"])
    try:
        result = run_case(**job["run_kwargs"])
        failure = None
        if result["status"] == "FAIL":
            failure = {
                "case_ref": case_ref,
                "error_type": "CASE_SYSTEM_FAILURE",
                "error": result.get("failure"),
            }
        return ordinal, result, failure
    except Exception as exc:
        failure = {
            "case_ref": case_ref,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1200],
        }
        atomic_json(
            output_dir / "cases" / case_ref / "failure.json",
            failure,
        )
        return ordinal, None, failure


def reusable_terminal_result(path: Path, model: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        result = read_json(path)
        if (
            result.get("schema_version") != "task2-autonomous-mllm-case/v0.4"
            or result.get("requested_model") != model
            or result.get("execution_mode") != EXECUTION_MODE
            or result.get("evaluation_condition") != "task2_standalone"
            or result.get("judgment_mode") != DEFERRED_JUDGMENT_MODE
            or result.get("status") not in {"PASS", "MODEL_FAILURE"}
            or result.get("pipeline_evaluable") is not True
            or result.get("failure") is not None
        ):
            return None
        for name in (
            "trajectory",
            "network_audit",
            "runtime_attestation",
            "fixed_judge_handoff",
            "evidence_package",
        ):
            reference = (result.get("artifact_refs") or {}).get(name) or {}
            artifact_path = Path(str(reference.get("path") or ""))
            if not artifact_path.is_absolute():
                artifact_path = PROJECT_ROOT / artifact_path
            if (
                not artifact_path.is_file()
                or sha256_file(artifact_path) != reference.get("sha256")
            ):
                return None
        return result
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def observation_plan_paths(path: Path, runtime_root: Path) -> dict[str, Path]:
    manifest = read_json(path)
    result = {}
    for row in manifest.get("plans") or []:
        plan_path = runtime_root / row["path"]
        if sha256_file(plan_path) != row["sha256"]:
            raise ValueError(f"observation plan hash mismatch: {row['case_ref']}")
        result[str(row["case_ref"])] = plan_path
    return result


def select_cases(
    *,
    resolver: dict[str, Any],
    observation_paths: dict[str, Path],
    requested_case_refs: list[str],
    limit: int,
    runtime_root: Path,
) -> list[tuple[dict[str, Any], str, Path, dict[str, Any]]]:
    binding_by_case = {
        str(row["case_ref"]): row for row in resolver.get("bindings") or []
    }
    if requested_case_refs:
        ordered_case_refs = list(requested_case_refs)
    else:
        ordered_case_refs = list(binding_by_case)
    if len(ordered_case_refs) != len(set(ordered_case_refs)):
        raise ValueError("duplicate case_ref requested")
    selected = []
    for case_ref in ordered_case_refs:
        binding = binding_by_case.get(case_ref)
        if binding is None:
            raise ValueError(f"unknown case_ref: {case_ref}")
        primary_sample_id(binding)
        track, source_path, _, scenario = scenario_source(
            binding=binding,
            observation_paths=observation_paths,
            runtime_root=runtime_root,
        )
        selected.append((binding, track, source_path, scenario))
    if limit:
        selected = selected[:limit]
    if not selected:
        raise ValueError("no Task 2 cases selected")
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task2-release",
        type=Path,
        default=DEFAULT_TASK2_RELEASE,
        help=(
            "Exact gated Task 2 release root. Resolver, prompts, codebook, "
            "case order, and Docker references are derived from its contract."
        ),
    )
    parser.add_argument("--route-probe", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument(
        "--transport",
        choices=("libinfer-neo", "openai-compatible"),
        required=True,
    )
    parser.add_argument(
        "--base-url-env",
        help=(
            "Environment variable containing the endpoint base URL. Defaults "
            "to LIBINFER_NEO_URL or OPENAI_BASE_URL by transport."
        ),
    )
    parser.add_argument(
        "--api-key-env",
        help=(
            "Environment variable containing the API key. Defaults to "
            "LIBINFER_SK or OPENAI_API_KEY by transport."
        ),
    )
    parser.add_argument("--tesseract-root", type=Path, default=DEFAULT_TESSERACT_ROOT)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=PROJECT_ROOT,
        help=(
            "Root containing resolver-relative site outputs, stateful profiles, "
            "and observation plans."
        ),
    )
    parser.add_argument(
        "--runtime-materialization-report",
        type=Path,
        required=True,
        help=(
            "Required for portable OCI materializations whose archive runtime "
            "binary may differ from the historical source-worktree fingerprint."
        ),
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--case-ref", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-case-seconds", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-judge-images", type=int, default=8)
    parser.add_argument("--max-judge-image-bytes", type=int, default=5_500_000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--viewport-only",
        action="store_true",
        default=True,
        help="Frozen protocol; retained as an explicit attestation flag.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse PASS/MODEL_FAILURE cases only when the complete frozen "
            "configuration fingerprint is unchanged."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.replace and args.resume:
        print(
            canonical_json(
                {"status": "FAIL", "error": "--replace and --resume conflict"}
            )
        )
        return 1
    resuming = args.resume and args.out.exists()
    if args.out.exists():
        if not args.replace and not args.resume:
            print(canonical_json({"status": "FAIL", "error": "output exists"}))
            return 1
        if args.replace:
            shutil.rmtree(args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    failures: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    try:
        release = validate_task2_release(
            args.task2_release.resolve(),
            require_frozen_hash=True,
            verify_files=True,
        )
        contract = release["contract"]
        release_paths = release["paths"]
        args.resolver = release_paths["resolver"]
        args.observation_manifest = release_paths["observation_manifest"]
        args.prompts = release_paths["prompts"]
        args.codebook = release_paths["codebook"]
        args.docker_reference = release_paths["docker_reference"]
        run_inline_judge = False
        assert_frozen_browser_runtime()
        assert_protocol_runtime(
            contract=contract,
            browser_harness_id=BROWSER_HARNESS_ID,
            browsergym_core_version=BROWSERGYM_CORE_VERSION,
            playwright_version=PLAYWRIGHT_VERSION,
            max_steps=args.max_steps,
            max_case_seconds=args.max_case_seconds,
            max_tokens=args.max_tokens,
            max_evidence_images=args.max_judge_images,
            max_evidence_image_bytes=args.max_judge_image_bytes,
            viewport_only=args.viewport_only,
            run_inline_judge=run_inline_judge,
        )
        if not 1 <= args.workers <= 8:
            raise ValueError("workers must be between 1 and 8")
        route_probe = read_json(args.route_probe)
        if route_probe.get("status") != "PASS_FIXED_MLLM_SELECTED":
            raise ValueError("fixed multimodal route probe has not passed")
        if (
            route_probe.get("transport") != args.transport
            or route_probe.get("oneapi_used") is not False
        ):
            raise ValueError("route probe transport does not match this run")
        model = args.model
        if not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model):
            raise ValueError("invalid fixed model id")
        passing = {
            row.get("model")
            for row in route_probe.get("results") or []
            if row.get("status") == "PASS_MULTIMODAL_ROUTE"
        }
        if model not in passing:
            raise ValueError("model did not pass the frozen multimodal route probe")
        resolved_model_allowlist = pinned_response_models(route_probe, model)
        environment = (
            load_export_env(args.env_file)
            if args.env_file is not None and args.env_file.is_file()
            else {}
        )
        base_url_env = args.base_url_env or (
            "LIBINFER_NEO_URL"
            if args.transport == "libinfer-neo"
            else "OPENAI_BASE_URL"
        )
        api_key_env = args.api_key_env or (
            "LIBINFER_SK"
            if args.transport == "libinfer-neo"
            else "OPENAI_API_KEY"
        )
        if not base_url_env.isidentifier() or not api_key_env.isidentifier():
            raise ValueError("invalid endpoint environment variable name")
        base_url_model = environment.get(base_url_env) or os.environ.get(
            base_url_env
        )
        api_key = environment.get(api_key_env) or os.environ.get(api_key_env)
        if not base_url_model or not api_key:
            raise ValueError(
                f"missing endpoint credentials in {base_url_env}/{api_key_env}"
            )
        if "oneapi" in base_url_model.lower():
            raise ValueError("OneAPI is forbidden")
        prompts_payload = read_json(args.prompts)
        if (
            prompts_payload.get("schema_version")
            != "riskchainbench-task2-trajectory-prompt/v0.1"
            or prompts_payload.get("judgment_mode")
            != DEFERRED_JUDGMENT_MODE
            or prompts_payload.get("browser_harness") != BROWSER_HARNESS_ID
            or "judge_system" in prompts_payload
        ):
            raise ValueError("unsupported Task 2 prompt bundle")
        prompts = {"action_system": str(prompts_payload["action_system"])}
        codebook = read_json(args.codebook)
        resolver = read_json(args.resolver)
        if resolver.get("schema_version") != "benchmark-private-resolver/v0.2":
            raise ValueError("Task 2 requires resolver v0.2")
        runtime_root = args.runtime_root.resolve()
        runtime_materialization = load_runtime_materialization(
            report_path=args.runtime_materialization_report.resolve(),
            docker_reference_path=args.docker_reference.resolve(),
            expected_docker_reference_sha256=sha256_file(
                args.docker_reference
            ),
            expected_task2_contract_sha256=EXPECTED_TASK2_CONTRACT_SHA256,
            runtime_root=runtime_root,
            resolver=resolver,
        )
        selected = select_cases(
            resolver=resolver,
            observation_paths=observation_plan_paths(
                args.observation_manifest,
                runtime_root,
            ),
            requested_case_refs=args.case_ref,
            limit=args.limit,
            runtime_root=runtime_root,
        )
        config = {
            "schema_version": "task2-autonomous-mllm-config/v0.4",
            "created_at": started_at,
            "model": model,
            "resolved_model_allowlist": sorted(resolved_model_allowlist),
            "transport": args.transport,
            "credential_environment": {
                "base_url_env": base_url_env,
                "api_key_env": api_key_env,
                "values_persisted": False,
            },
            "oneapi_used": False,
            "execution_mode": EXECUTION_MODE,
            "judgment_mode": DEFERRED_JUDGMENT_MODE,
            "task2_protocol_id": TASK2_TRAJECTORY_PROTOCOL["protocol_id"],
            "task2_contract_sha256": release["contract_sha256"],
            "task1_prediction_required": False,
            "actual_browser_trajectory_count_per_case": 1,
            "browser_harness": {
                "backend": BROWSER_BACKEND,
                "harness_id": BROWSER_HARNESS_ID,
                "browsergym_core_version": BROWSERGYM_CORE_VERSION,
                "playwright_version": PLAYWRIGHT_VERSION,
                "expected_browsergym_core_version": (
                    EXPECTED_BROWSERGYM_CORE_VERSION
                ),
                "expected_playwright_version": EXPECTED_PLAYWRIGHT_VERSION,
                "action_execution": "BROWSERENV_STEP",
                "observation_source": "BROWSERGYM",
            },
            "case_refs": [row[0]["case_ref"] for row in selected],
            "case_count": len(selected),
            "max_steps": args.max_steps,
            "max_case_seconds": args.max_case_seconds,
            "max_tokens": args.max_tokens,
            "max_judge_images": args.max_judge_images,
            "max_judge_image_bytes": args.max_judge_image_bytes,
            "workers": args.workers,
            "worker_isolation": "PROCESS_PER_BROWSER_WORKER",
            "full_page_screenshots": not args.viewport_only,
            "runtime_root": str(runtime_root),
            "runtime_materialization_report": (
                {
                    "path": str(args.runtime_materialization_report.resolve()),
                    "sha256": sha256_file(
                        args.runtime_materialization_report.resolve()
                    ),
                }
            ),
            "docker_reference": {
                "path": str(args.docker_reference.resolve()),
                "sha256": sha256_file(args.docker_reference.resolve()),
            },
            "screenshot_policy": (
                "ADAPTIVE_FULL_PAGE_STATE_BOUNDARIES"
                if not args.viewport_only
                else "VIEWPORT_ONLY"
            ),
            "inputs": {
                "task2_contract": {
                    "path": str(release["contract_path"]),
                    "sha256": sha256_file(release["contract_path"]),
                    "embedded_contract_sha256": release[
                        "contract_sha256"
                    ],
                },
                "resolver": {
                    "path": str(args.resolver),
                    "sha256": sha256_file(args.resolver),
                },
                "observation_manifest": {
                    "path": str(args.observation_manifest),
                    "sha256": sha256_file(args.observation_manifest),
                },
                "prompts": {
                    "path": str(args.prompts),
                    "sha256": sha256_file(args.prompts),
                },
                "codebook": {
                    "path": str(args.codebook),
                    "sha256": sha256_file(args.codebook),
                },
                "route_probe": {
                    "path": str(args.route_probe),
                    "sha256": sha256_file(args.route_probe),
                },
                "runner": {
                    "path": str(Path(__file__).resolve()),
                    "sha256": sha256_file(Path(__file__).resolve()),
                },
                "shared_model_client": {
                    "path": str(SCRIPT_DIR / "run_unified_mllm_smoke.py"),
                    "sha256": sha256_file(
                        SCRIPT_DIR / "run_unified_mllm_smoke.py"
                    ),
                },
                "model_boundary_sanitizer": {
                    "path": str(SCRIPT_DIR / "run_model_browser_agent.py"),
                    "sha256": sha256_file(
                        SCRIPT_DIR / "run_model_browser_agent.py"
                    ),
                },
                "local_protocol_verifier": {
                    "path": str(SCRIPT_DIR / "verify_stateful_profile.py"),
                    "sha256": sha256_file(
                        SCRIPT_DIR / "verify_stateful_profile.py"
                    ),
                },
                "browsergym_adapter": {
                    "path": str(SCRIPT_DIR / "riskchainbench_browsergym.py"),
                    "sha256": sha256_file(
                        SCRIPT_DIR / "riskchainbench_browsergym.py"
                    ),
                },
                "frozen_protocol": {
                    "path": str(SCRIPT_DIR / "task2_frozen_protocol.py"),
                    "sha256": sha256_file(
                        SCRIPT_DIR / "task2_frozen_protocol.py"
                    ),
                },
            },
        }
        config["source_snapshots"] = snapshot_sources(
            args.out,
            [
                Path(__file__).resolve(),
                SCRIPT_DIR / "run_unified_mllm_smoke.py",
                SCRIPT_DIR / "run_model_browser_agent.py",
                SCRIPT_DIR / "verify_stateful_profile.py",
                SCRIPT_DIR / "riskchainbench_browsergym.py",
                SCRIPT_DIR / "task2_frozen_protocol.py",
                args.prompts,
                args.codebook,
                release["contract_path"],
            ],
        )
        fingerprint_payload = {
            key: value
            for key, value in config.items()
            if key not in {"created_at", "config_fingerprint", "source_snapshots"}
        }
        fingerprint_payload["source_snapshots"] = [
            {
                "source_path": row["source_path"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
            }
            for row in config["source_snapshots"]
        ]
        config["config_fingerprint"] = sha256_text(
            canonical_json(fingerprint_payload)
        )
        existing_config_path = args.out / "config.json"
        if resuming:
            if not existing_config_path.is_file():
                raise ValueError("resume requested but config.json is missing")
            existing_config = read_json(existing_config_path)
            if (
                existing_config.get("schema_version")
                != config["schema_version"]
                or existing_config.get("config_fingerprint")
                != config["config_fingerprint"]
            ):
                raise ValueError(
                    "resume configuration fingerprint mismatch; use a new "
                    "output directory or --replace"
                )
            config["created_at"] = existing_config.get(
                "created_at", config["created_at"]
            )
        atomic_json(args.out / "config.json", config)
        progress_path = args.out / "progress.jsonl"
        progress_lock = threading.Lock()

        def record_progress(
            *,
            case_ref: str,
            status: str,
            ordinal: int,
            detail: str | None = None,
        ) -> None:
            event = {
                "timestamp": utc_now(),
                "case_ref": case_ref,
                "status": status,
                "ordinal": ordinal,
                "target_case_count": len(selected),
            }
            if detail:
                event["detail"] = detail[:600]
            with progress_lock:
                with progress_path.open("a", encoding="utf-8") as handle:
                    handle.write(canonical_json(event) + "\n")
                print(canonical_json(event), flush=True)

        jobs: list[dict[str, Any]] = []
        for ordinal, row in enumerate(selected, 1):
            binding, track, source_path, scenario = row
            case_ref = str(binding["case_ref"])
            case_root = args.out / "cases" / case_ref
            if resuming:
                existing_result = reusable_terminal_result(
                    case_root / "case_result.json",
                    model,
                )
                if existing_result is not None:
                    results.append(existing_result)
                    record_progress(
                        case_ref=case_ref,
                        status="RESUMED_TERMINAL",
                        ordinal=ordinal,
                    )
                    continue
                if case_root.exists():
                    shutil.rmtree(case_root)
            runtime_integrity = runtime_materialization.get(
                case_ref,
                {
                    "verification_source": "FROZEN_RESOLVER_SOURCE_WORKTREE",
                    "expected_runtime_binary_sha256": binding[
                        "runtime_binary_sha256"
                    ],
                    "source_runtime_binary_sha256": binding[
                        "runtime_binary_sha256"
                    ],
                    "source_runtime_binary_match": True,
                },
            )
            jobs.append(
                {
                    "ordinal": ordinal,
                    "case_ref": case_ref,
                    "run_kwargs": {
                        "binding": binding,
                        "track": track,
                        "source_path": source_path,
                        "scenario": scenario,
                        "model": model,
                        "prompts": prompts,
                        "codebook": codebook,
                        "base_url_model": base_url_model,
                        "api_key": api_key,
                        "transport": args.transport,
                        "output_dir": args.out,
                        "tesseract_root": args.tesseract_root,
                        "max_steps": args.max_steps,
                        "max_case_seconds": args.max_case_seconds,
                        "max_tokens": args.max_tokens,
                        "max_judge_images": args.max_judge_images,
                        "max_judge_image_bytes": args.max_judge_image_bytes,
                        "full_page": not args.viewport_only,
                        "run_inline_judge": run_inline_judge,
                        "prompts_path": args.prompts,
                        "codebook_path": args.codebook,
                        "resolved_model_allowlist": resolved_model_allowlist,
                        "runtime_root": runtime_root,
                        "runtime_integrity": runtime_integrity,
                    },
                }
            )
            record_progress(
                case_ref=case_ref,
                status="STARTED",
                ordinal=ordinal,
            )

        completed_rows: list[
            tuple[int, dict[str, Any] | None, dict[str, Any] | None]
        ] = []

        def record_completed(
            row: tuple[int, dict[str, Any] | None, dict[str, Any] | None],
        ) -> None:
            ordinal, result, failure = row
            case_ref = (
                str(result["case_ref"])
                if result is not None
                else str(failure["case_ref"])
            )
            record_progress(
                case_ref=case_ref,
                status=(
                    str(result["status"])
                    if result is not None
                    else "FAIL"
                ),
                ordinal=ordinal,
                detail=(
                    result.get("failure")
                    if result is not None
                    else f"{failure['error_type']}: {failure['error']}"
                ),
            )

        if args.workers == 1:
            for job in jobs:
                row = execute_case_job(job)
                completed_rows.append(row)
                record_completed(row)
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(execute_case_job, job): job
                    for job in jobs
                }
                for future in as_completed(futures):
                    row = future.result()
                    completed_rows.append(row)
                    record_completed(row)
        for _, result, failure in sorted(completed_rows, key=lambda row: row[0]):
            if result is not None:
                results.append(result)
            if failure is not None:
                failures.append(failure)
        case_order = {
            str(row[0]["case_ref"]): ordinal
            for ordinal, row in enumerate(selected)
        }
        results.sort(key=lambda row: case_order[str(row["case_ref"])])
        pass_count = sum(row["status"] == "PASS" for row in results)
        model_failure_count = sum(
            row["status"] == "MODEL_FAILURE" for row in results
        )
        pipeline_evaluable_count = pass_count + model_failure_count
        system_failure_count = len(selected) - pipeline_evaluable_count
        summary = {
            "schema_version": "task2-autonomous-mllm-summary/v0.4",
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": qualification_status(
                target_count=len(selected),
                pass_count=pass_count,
                model_failure_count=model_failure_count,
                system_failure_count=system_failure_count,
            ),
            "requested_model": model,
            "resolved_models": sorted(
                {
                    resolved
                    for row in results
                    for resolved in row.get("resolved_models") or []
                }
            ),
            "transport": args.transport,
            "oneapi_used": False,
            "track": "BROWSERGYM_MULTIMODAL_BROWSER",
            "execution_mode": EXECUTION_MODE,
            "judgment_mode": config["judgment_mode"],
            "task2_protocol_id": config["task2_protocol_id"],
            "task2_contract_sha256": config["task2_contract_sha256"],
            "target_case_count": len(selected),
            "pass_case_count": pass_count,
            "model_failure_case_count": model_failure_count,
            "pipeline_evaluable_case_count": pipeline_evaluable_count,
            "system_failure_case_count": system_failure_count,
            "fail_case_count": system_failure_count,
            "mirror_track_counts": {
                track_name: sum(row["mirror_track"] == track_name for row in results)
                for track_name in ("L2_OBSERVATION", "L3_STATEFUL")
            },
            "case_results": [
                {
                    "case_ref": row["case_ref"],
                    "status": row["status"],
                    "mirror_track": row["mirror_track"],
                    "result_path": str(
                        args.out / "cases" / row["case_ref"] / "case_result.json"
                    ),
                }
                for row in results
            ],
            "failures": failures,
            "config_fingerprint": config["config_fingerprint"],
            "claim_boundary": {
                "scope": "TASK2_TRAJECTORY_COLLECTION",
                "human_gold_present": False,
                "accuracy_or_f1_allowed": False,
                "full_scale_600_evaluation": len(selected) == 600,
                "fixed_external_judge_status": "PENDING",
            },
        }
        atomic_json(args.out / "summary.json", summary)
        write_jsonl(args.out / "failures.jsonl", failures)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["status"] != "FAIL" else 2
    except Exception as exc:
        summary = {
            "schema_version": "task2-autonomous-mllm-summary/v0.4",
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1600],
        }
        atomic_json(args.out / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
