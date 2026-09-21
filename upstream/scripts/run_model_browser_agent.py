#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import time
from collections.abc import Mapping
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid

from jsonschema import Draft202012Validator, FormatChecker
from playwright.sync_api import Frame, Locator, Page, sync_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_obfuscated_session_dataset as privacy_scanner  # noqa: E402

try:
    from build_human_trajectory_workbench import CASES
except ModuleNotFoundError:
    from scripts.build_human_trajectory_workbench import CASES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "configs" / "model_browser_agent_v0.1.md"
DEFAULT_SCHEMA = PROJECT_ROOT / "schemas" / "model_browser_trajectory_v0.1.schema.json"
INTERACTIVE_SELECTOR = "a,button,input,select,textarea,[role=button]"
URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://\S+|\bwww\.\S+")
IPV4_RE = re.compile(r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}(?![0-9])")
DOMAIN_RE = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|net|org|cn|io|app|top|xyz|info|me|co|in|test)\b")
MASKED_BOUNDARY_VALUE_RE = re.compile(r"^\[MASKED_[A-Z0-9_-]+\]$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _model_boundary_findings(value: Any) -> list[dict[str, Any]]:
    """Return shared-scan findings without ever echoing untrusted input.

    ``scan_public_boundary_findings`` is the single source of truth for model
    boundaries.  A malformed value or a scanner failure is treated as a
    finding (rather than falling back to the older regex-only redactor), so a
    caller cannot accidentally send an unsanitized leaf to the LLM.
    """

    text = str(value or "")
    try:
        findings = privacy_scanner.scan_public_boundary_findings(text)
    except Exception:
        return [{"kind": "SCANNER_ERROR"}]
    if not isinstance(findings, list):
        return [{"kind": "SCANNER_ERROR"}]
    return findings


def sanitize_model_text(value: Any) -> str:
    """Project one text leaf into the model-visible boundary.

    Any shared-scanner finding masks the *complete* leaf.  This is deliberate:
    replacing only the matched prefix (for example ``邀请码:ABCD/EF``) can leave
    a suffix in the request.  Legacy URL/IP/domain replacements are retained
    only for reserved/synthetic values which the shared scanner considers safe.
    """

    text = str(value or "")
    if _model_boundary_findings(text):
        return "[MASKED_TEXT]"
    text = URL_RE.sub("[MASKED_URL]", text)
    text = IPV4_RE.sub("[MASKED_IP]", text)
    text = DOMAIN_RE.sub("[MASKED_DOMAIN]", text)
    return text


def sanitize_model_value(value: Any) -> Any:
    """Recursively sanitize a JSON-like value before it reaches the model.

    Dynamic DOM text is not the only possible leak: task metadata, previous
    action summaries, and dictionary keys can carry the same short contextual
    identifiers.  Keys are replaced by deterministic opaque names and string
    values use the fail-closed whole-leaf policy above.
    """

    if isinstance(value, Mapping):
        sanitized: dict[Any, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            key_findings = _model_boundary_findings(key_text)
            if key_findings:
                key_text = f"masked-key-{sha256_text(key_text)[:16]}"
            projected_child = sanitize_model_value(child)
            # A JSON object commonly stores a cue and its identifier in
            # separate leaves (``{"邀请码": "ABCD"}``).  Scanning each leaf
            # independently would miss that relationship, so inspect the
            # key/value pair as one bounded text before retaining the child.
            if not key_findings:
                # ``key`` may itself be only a cue (for example ``账号``)
                # while the actual value is nested.  Probe it with a fixed
                # synthetic token; if that establishes contextual semantics,
                # hide the complete child subtree rather than trusting its
                # individual leaves.
                contextual_key = bool(_model_boundary_findings(f"{key}:X"))
                # Inspect the already projected child.  Looking at the raw
                # ``repr`` here would let an incidental ``index.html`` inside
                # a safe page-reference object mask the entire observation.
                pair_text = f"{key}:{projected_child}"
                if contextual_key or _model_boundary_findings(pair_text):
                    projected_child = "[MASKED_TEXT]"
            sanitized[key_text] = projected_child
        return sanitized
    if isinstance(value, list):
        return [sanitize_model_value(child) for child in value]
    if isinstance(value, tuple):
        return [sanitize_model_value(child) for child in value]
    if isinstance(value, str):
        return sanitize_model_text(value)
    return value


def assert_model_boundary_safe(value: Any) -> None:
    """Fail closed if any textual key/leaf remains unsafe after projection."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if _model_boundary_findings(key):
                raise ValueError("MODEL_BOUNDARY_UNSAFE")
            if (
                child is not None
                and not isinstance(child, (Mapping, list, tuple))
                and not (
                    isinstance(child, str)
                    and MASKED_BOUNDARY_VALUE_RE.fullmatch(child)
                )
                and _model_boundary_findings(f"{key}:{child}")
            ):
                raise ValueError("MODEL_BOUNDARY_UNSAFE")
            assert_model_boundary_safe(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            assert_model_boundary_safe(child)
        return
    if isinstance(value, str) and _model_boundary_findings(value):
        raise ValueError("MODEL_BOUNDARY_UNSAFE")


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG screenshot: {path}")
    return struct.unpack(">II", header[16:24])


def libinfer_chat_url(value: str) -> str:
    base = value.rstrip("/")
    lowered = base.lower()
    if "oneapi" in lowered or "not-for-automation" in lowered:
        raise ValueError(f"refusing non-libinfer endpoint: {base}")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def call_libinfer(base_url: str, api_key: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    # Keep a final guard at the transport boundary.  Callers normally pass a
    # projected observation, but retry/error paths and metadata are still
    # untrusted strings; no request should bypass the shared scanner.
    safe_body = sanitize_model_value(body)
    assert_model_boundary_safe(safe_body)
    request = urllib.request.Request(
        libinfer_chat_url(base_url),
        data=json.dumps(safe_body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:4000]
        raise RuntimeError(f"libinfer HTTP {error.code}: {detail}") from error
    if not isinstance(payload, dict) or payload.get("error"):
        raise RuntimeError(f"invalid libinfer response: {payload}")
    return payload


def response_content(payload: dict[str, Any]) -> str:
    content = payload["choices"][0]["message"]["content"]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(row.get("text") or "")
            for row in content
            if isinstance(row, dict) and row.get("type") == "text"
        )
    raise ValueError("unsupported libinfer response content")


def extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("model response contains no JSON object")
    value, consumed = json.JSONDecoder().raw_decode(text[start:])
    if text[start + consumed :].strip():
        raise ValueError("model response contains trailing content")
    if not isinstance(value, dict):
        raise ValueError("model response JSON must be an object")
    return value


def validate_decision(
    decision: dict[str, Any], inventory: list[dict[str, Any]], fixtures: dict[str, str], all_complete: bool
) -> list[str]:
    errors: list[str] = []
    action = decision.get("action")
    if action not in {"fill", "click", "reload", "stop"}:
        errors.append("action must be fill, click, reload, or stop")
    reason = decision.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("reason must be a non-empty string")
    by_id = {row["element_id"]: row for row in inventory}
    if action in {"fill", "click"}:
        element_id = decision.get("element_id")
        if element_id not in by_id:
            errors.append("element_id is not present in the current observation")
        elif action == "fill":
            element = by_id[element_id]
            fixture = decision.get("fixture")
            if fixture not in fixtures:
                errors.append("fixture is not present in the current observation")
            if element.get("name") and fixture != element.get("name"):
                errors.append("fixture must match the selected field name")
            if element.get("has_value"):
                errors.append("selected field is already filled; choose an incomplete field")
            if element.get("tag") not in {"input", "textarea", "select"}:
                errors.append("fill target must be an input, textarea, or select")
        elif by_id[element_id].get("tag") not in {"a", "button", "input"} and by_id[element_id].get("role") != "button":
            errors.append("click target is not an actionable control")
    if action == "stop":
        if decision.get("status") not in {"SUCCESS", "FAILED", "ABSTAIN"}:
            errors.append("stop requires status SUCCESS, FAILED, or ABSTAIN")
        if decision.get("status") == "SUCCESS" and not all_complete:
            errors.append("SUCCESS is premature because required checkpoints remain incomplete")
    return errors


def get_frame(page: Page) -> Frame:
    element = page.locator("#mirrorFrame").element_handle()
    if element is None:
        raise RuntimeError("mirror iframe element is unavailable")
    frame = element.content_frame()
    if frame is None:
        raise RuntimeError("mirror iframe content is unavailable")
    frame.wait_for_selector("body", timeout=20_000)
    return frame


def page_ref(page: Page) -> str:
    value = page.locator("#fakeAddress").inner_text().strip()
    return value if value.startswith("case://") else "case://unavailable"


def observe_frame(page: Page) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    frame = get_frame(page)
    raw = frame.evaluate(
        r"""
        selector => {
          const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
          const all = Array.from(document.querySelectorAll(selector));
          const viewportWidth = window.innerWidth;
          const viewportHeight = window.innerHeight;
          const elements = all.map((element, domIndex) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            const label = element.labels && element.labels.length
              ? normalize(Array.from(element.labels).map(row => row.innerText).join(' '))
              : '';
            const text = /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName)
              ? normalize(label || element.getAttribute('aria-label') || element.getAttribute('placeholder') || element.name)
              : normalize(element.innerText || element.getAttribute('aria-label') || element.getAttribute('title'));
            const rendered = style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
            if (!rendered) return null;
            return {
              dom_index: domIndex,
              tag: element.tagName.toLowerCase(),
              role: element.getAttribute('role') || '',
              name: element.getAttribute('name') || '',
              type: element.getAttribute('type') || '',
              text,
              placeholder: element.getAttribute('placeholder') || '',
              disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
              has_value: /^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName) && String(element.value || '').length > 0,
              checked: typeof element.checked === 'boolean' ? element.checked : null,
              viewport_status: rect.bottom < 0 || rect.top > viewportHeight || rect.right < 0 || rect.left > viewportWidth ? 'OFFSCREEN' : 'ONSCREEN',
              bbox: {x: rect.left, y: rect.top, width: rect.width, height: rect.height}
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
            source_document_width: Math.max(root ? root.scrollWidth : 0, body ? body.scrollWidth : 0),
            source_document_height: Math.max(root ? root.scrollHeight : 0, body ? body.scrollHeight : 0)
          };
        }
        """,
        INTERACTIVE_SELECTOR,
    )
    text = str(raw.get("body_text") or "")
    inventory: list[dict[str, Any]] = []
    for index, row in enumerate(raw.get("elements") or [], 1):
        inventory.append(
            {
                "element_id": f"E{index:03d}",
                "dom_index": int(row["dom_index"]),
                "tag": sanitize_model_text(row.get("tag")),
                "role": sanitize_model_text(row.get("role")),
                "name": sanitize_model_text(row.get("name")),
                "type": sanitize_model_text(row.get("type")),
                "text": sanitize_model_text(row.get("text"))[:160],
                "placeholder": sanitize_model_text(row.get("placeholder"))[:120],
                "disabled": bool(row.get("disabled")),
                "has_value": bool(row.get("has_value")),
                "checked": row.get("checked"),
                "viewport_status": sanitize_model_text(row.get("viewport_status")),
                "bbox": row.get("bbox"),
            }
        )
    state = {
        "title": sanitize_model_text(raw.get("title")),
        "visible_text_sha256": sha256_text(text),
        "visible_text_excerpt": sanitize_model_text(text)[:5000],
        "interactive_count": len(inventory),
        "scroll_x": float(raw.get("scroll_x") or 0),
        "scroll_y": float(raw.get("scroll_y") or 0),
        "viewport_width": int(raw.get("viewport_width") or 0),
        "viewport_height": int(raw.get("viewport_height") or 0),
        "source_document_width": float(raw.get("source_document_width") or 0),
        "source_document_height": float(raw.get("source_document_height") or 0),
    }
    return state, inventory


def locator_for(page: Page, item: dict[str, Any]) -> Locator:
    return get_frame(page).locator(INTERACTIVE_SELECTOR).nth(int(item["dom_index"]))


def refresh_item_geometry(page: Page, item: dict[str, Any]) -> dict[str, Any]:
    state, inventory = observe_frame(page)
    match = next((row for row in inventory if row["dom_index"] == item["dom_index"]), item)
    return {**match, "capture_state": state}


def take_frame_screenshot(page: Page, path: Path) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.locator("#mirrorFrame").screenshot(path=str(path), animations="disabled")
    return png_dimensions(path)


def checkpoint_map(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        row["id"]: {
            "checkpoint_id": row["id"],
            "description": row["description"],
            "passed": False,
            "evidence_event_ids": [],
        }
        for row in case["checkpoints"]
    }


def pass_checkpoint(checkpoints: dict[str, dict[str, Any]], checkpoint_id: str, event_id: str) -> None:
    item = checkpoints.get(checkpoint_id)
    if item and not item["passed"]:
        item["passed"] = True
        item["evidence_event_ids"].append(event_id)


def update_checkpoints(
    case: dict[str, Any], checkpoints: dict[str, dict[str, Any]], event: dict[str, Any], after_ref: str
) -> None:
    decision = event["model_decision"]
    action = decision["action"]
    target = event.get("target") or {}
    event_id = event["event_id"]
    if "wp-reader" in case["task_id"]:
        if action == "fill" and target.get("name") == "log":
            pass_checkpoint(checkpoints, "CPwp_user", event_id)
        if action == "fill" and target.get("name") == "pwd":
            pass_checkpoint(checkpoints, "CPwp_pass", event_id)
        if action == "click" and "wp-reader-session.html" in after_ref:
            pass_checkpoint(checkpoints, "CPwp_submit", event_id)
            pass_checkpoint(checkpoints, "CPwp_state", event_id)
        if action == "reload" and "wp-reader-session.html" in event["page_ref"] and "wp-reader-session.html" in after_ref:
            pass_checkpoint(checkpoints, "CPwp_reload", event_id)
        if action == "click" and event["page_ref"].endswith("wp-reader-session.html") and after_ref.endswith("/index.html"):
            pass_checkpoint(checkpoints, "CPwp_logout", event_id)
    else:
        filled = {
            row.get("target", {}).get("name")
            for row in event.get("history", [])
            if row.get("model_decision", {}).get("action") == "fill"
        }
        if action == "fill":
            filled.add(target.get("name"))
        if {"invite", "name", "display_name", "email", "pin", "pin2"}.issubset(filled):
            pass_checkpoint(checkpoints, "CPmember_fields", event_id)
        if action == "click" and "membership-request-pending.html" in after_ref:
            pass_checkpoint(checkpoints, "CPmember_submit", event_id)
            pass_checkpoint(checkpoints, "CPmember_pending", event_id)


def checkpoint_projection(checkpoints: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"checkpoint_id": row["checkpoint_id"], "description": row["description"], "passed": row["passed"]}
        for row in checkpoints.values()
    ]


def model_observation(
    case: dict[str, Any], state: dict[str, Any], inventory: list[dict[str, Any]], checkpoints: dict[str, dict[str, Any]], history: list[dict[str, Any]], validation_feedback: list[str]
) -> dict[str, Any]:
    completed_fields = {
        str((row.get("target") or {}).get("name") or "")
        for row in history
        if (row.get("model_decision") or {}).get("action") == "fill"
    }
    observation = {
        "protocol": "model_browser_action_v0.1",
        "task": {
            "task_id": case["task_id"],
            "case_ref": case["case_ref"],
            "objective": case["objective"],
            "completion_rule": "All listed checkpoints must be passed before stop/SUCCESS.",
        },
        "current_page": {"page_ref": history[-1]["result"]["after_page_ref"] if history else f"case://{case['case_ref']}/{case['start_path']}", **state},
        "fixtures": [
            {
                "fixture": row["field"],
                "label": row["label"],
                "assigned_field": row["field"],
                "completed": row["field"] in completed_fields,
            }
            for row in case["fixtures"]
        ],
        "field_progress": {
            "completed_fields": sorted(completed_fields),
            "remaining_fields": [row["field"] for row in case["fixtures"] if row["field"] not in completed_fields],
        },
        "interactive_elements": [
            {key: value for key, value in row.items() if key not in {"dom_index", "bbox"}}
            for row in inventory
        ],
        "checkpoints": checkpoint_projection(checkpoints),
        "recent_actions": [
            {
                "sequence": row["sequence"],
                "action": row["model_decision"]["action"],
                "target": (row.get("target") or {}).get("text") or (row.get("target") or {}).get("name"),
                "reason": row["model_decision"]["reason"],
                "after_page_ref": row["result"]["after_page_ref"],
                "status": row["result"]["status"],
            }
            for row in history[-8:]
        ],
        "previous_response_errors": validation_feedback,
    }
    # Project every textual leaf, not only the DOM excerpts.  This covers
    # imported case metadata, page references, action reasons and validation
    # feedback if a fixture or an upstream adapter is unexpectedly tainted.
    projected = sanitize_model_value(observation)
    assert_model_boundary_safe(projected)
    return projected


def choose_action(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    observation: dict[str, Any],
    inventory: list[dict[str, Any]],
    fixtures: dict[str, str],
    all_complete: bool,
    timeout: float,
    libinfer_retries: int,
    decision_retries: int,
    task_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    feedback: list[str] = []
    safe_prompt = sanitize_model_text(prompt)
    assert_model_boundary_safe(safe_prompt)
    for attempt in range(1, decision_retries + 2):
        payload = sanitize_model_value(
            {**observation, "previous_response_errors": feedback}
        )
        assert_model_boundary_safe(payload)
        request_hash = sha256_text(canonical_json(payload))
        run_id = str(uuid.uuid4())
        response = call_libinfer(
            base_url,
            api_key,
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": safe_prompt},
                    {"role": "user", "content": canonical_json(payload)},
                ],
                "temperature": 0,
                "max_tokens": 700,
                "response_format": {"type": "json_object"},
                "libinfer-notes": {
                    "project": "obfuscated-imwalker",
                    "task": f"model-browser-{task_id}-{attempt}"[:160],
                    "runId": run_id,
                    "extra": "model-browser-agent-v0.1",
                },
                "libinfer-metadata": {
                    "protocol": "model_browser_action_v0.1",
                    "task_id": task_id,
                    "request_sha256": request_hash,
                },
                "libinfer-retries": libinfer_retries,
            },
            timeout,
        )
        content = response_content(response)
        try:
            decision = extract_json_object(content)
            errors = validate_decision(decision, inventory, fixtures, all_complete)
        except Exception as error:
            decision = {}
            errors = [f"{type(error).__name__}: {error}"]
        if not errors:
            return decision, {
                "request_sha256": request_hash,
                "response_sha256": sha256_text(content),
                "libinfer_session_id": run_id,
                "response_id": response.get("id"),
                "response_model": response.get("model"),
                "usage": response.get("usage"),
                "attempt": attempt,
            }
        feedback = errors
    raise RuntimeError("model failed to produce a valid action: " + "; ".join(feedback))


def prepare_action_target(
    page: Page, decision: dict[str, Any], inventory: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    action = decision["action"]
    if action in {"reload", "stop"}:
        state, _ = observe_frame(page)
        return None, state
    item = next(row for row in inventory if row["element_id"] == decision["element_id"])
    locator_for(page, item).scroll_into_view_if_needed(timeout=15_000)
    page.wait_for_timeout(250)
    target = refresh_item_geometry(page, item)
    state = target.pop("capture_state")
    return target, state


def execute_action(
    page: Page,
    decision: dict[str, Any],
    target: dict[str, Any] | None,
    fixture_values: dict[str, str],
) -> bool:
    action = decision["action"]
    if action == "fill":
        value = fixture_values[decision["fixture"]]
        if target is None:
            raise RuntimeError("fill action has no prepared target")
        locator = locator_for(page, target)
        if target.get("tag") == "select":
            locator.select_option(value=value)
        else:
            locator.fill(value)
        page.wait_for_timeout(350)
    elif action == "click":
        if target is None:
            raise RuntimeError("click action has no prepared target")
        before = page_ref(page)
        locator_for(page, target).click(timeout=15_000)
        try:
            page.wait_for_function("before => document.querySelector('#fakeAddress').textContent.trim() !== before", arg=before, timeout=5_000)
        except Exception:
            pass
        page.wait_for_timeout(700)
    elif action == "reload":
        page.locator("#browserReload").click()
        page.wait_for_timeout(900)
    return action == "stop"


def event_type_for(decision: dict[str, Any]) -> str:
    if decision["action"] == "fill":
        return "INPUT_COMMIT"
    if decision["action"] == "click":
        return "CLICK"
    if decision["action"] == "reload":
        return "TOOL_RELOAD"
    return "COMPLETE" if decision.get("status") == "SUCCESS" else "MODEL_ABORT"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a libinfer-neo model as a local browser action policy.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--case-ref", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workbench-url", default="http://127.0.0.1:6006/human_trajectory_workbench_v0.1/index.html")
    parser.add_argument("--base-url", default=os.environ.get("LIBINFER_NEO_URL", "http://127.0.0.1:8101"))
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--max-steps", type=int, default=14)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--libinfer-retries", type=int, default=2)
    parser.add_argument("--decision-retries", type=int, default=2)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case = next((row for row in CASES if row["case_ref"] == args.case_ref), None)
    if case is None:
        raise ValueError(f"unknown case-ref: {args.case_ref}")
    if args.output_dir.exists():
        if not args.replace:
            raise FileExistsError(f"output exists: {args.output_dir}")
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True)
    screenshots = args.output_dir / "screenshots"
    prompt = args.prompt.read_text(encoding="utf-8")
    prompt_hash = sha256_text(prompt)
    api_key = os.environ.get("LIBINFER_SK")
    if not api_key:
        raise ValueError("missing LIBINFER_SK")
    case_index = CASES.index(case)
    fixture_values = {row["field"]: row["value"] for row in case["fixtures"]}
    checkpoints = checkpoint_map(case)
    events: list[dict[str, Any]] = []
    libinfer_sessions: list[str] = []
    external_requests: list[str] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    started_at = utc_now()
    started_perf = time.monotonic()
    trajectory_id = "MTR" + sha256_text(f"{case['case_ref']}:{args.model}:{started_at}")[:20]
    completion_status = "FAILED"
    model_stop_status = None
    failure = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)

        def route_handler(route) -> None:
            host = urlparse(route.request.url).hostname
            if host in {"127.0.0.1", "localhost"}:
                route.continue_()
            else:
                external_requests.append(route.request.url)
                route.abort()

        context.route("**/*", route_handler)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        try:
            page.goto(args.workbench_url, wait_until="networkidle", timeout=40_000)
            page.locator("#caseSelect").select_option(str(case_index))
            page.wait_for_timeout(800)
            get_frame(page)
            for sequence in range(1, args.max_steps + 1):
                state, inventory = observe_frame(page)
                # Keep the raw synthetic case reference for local checkpoint
                # bookkeeping and the trajectory artifact; only the projected
                # observation is sent across the model boundary.
                raw_before_ref = page_ref(page)
                all_complete = all(row["passed"] for row in checkpoints.values())
                observation = model_observation(case, state, inventory, checkpoints, events, [])
                decision, inference = choose_action(
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.model,
                    prompt=prompt,
                    observation=observation,
                    inventory=inventory,
                    fixtures=fixture_values,
                    all_complete=all_complete,
                    timeout=args.timeout_seconds,
                    libinfer_retries=args.libinfer_retries,
                    decision_retries=args.decision_retries,
                    task_id=case["task_id"],
                )
                libinfer_sessions.append(inference["libinfer_session_id"])
                target, capture_state = prepare_action_target(page, decision, inventory)
                screenshot_ref = f"screenshots/step_{sequence:02d}_{decision['action']}.png"
                shot_width, shot_height = take_frame_screenshot(page, args.output_dir / screenshot_ref)
                capture_state["document_width"] = shot_width
                capture_state["document_height"] = shot_height
                before_ref = raw_before_ref
                should_stop = execute_action(page, decision, target, fixture_values)
                after_ref = page_ref(page)
                event_id = f"ME{sequence:04d}"
                event = {
                    "event_id": event_id,
                    "sequence": sequence,
                    "elapsed_ms": max(0, round((time.monotonic() - started_perf) * 1000)),
                    "event_type": event_type_for(decision),
                    "page_ref": before_ref,
                    "state": capture_state,
                    "model_decision": {
                        **decision,
                        "request_sha256": inference["request_sha256"],
                        "response_sha256": inference["response_sha256"],
                        "libinfer_session_id": inference["libinfer_session_id"],
                        "response_id": inference["response_id"],
                        "response_model": inference["response_model"],
                        "attempt": inference["attempt"],
                    },
                    "target": target,
                    "operation": {"op": decision["action"], "fixture": decision.get("fixture")},
                    "result": {
                        "status": "STOPPED" if should_stop else "EXECUTED",
                        "before_page_ref": before_ref,
                        "after_page_ref": after_ref,
                        "page_changed": before_ref != after_ref,
                    },
                    "screenshot_ref": screenshot_ref,
                    "screenshot_width": shot_width,
                    "screenshot_height": shot_height,
                    "screenshot_coordinate_space": "FRAME_VIEWPORT_BEFORE_ACTION",
                    "history": events,
                }
                update_checkpoints(case, checkpoints, event, after_ref)
                event.pop("history", None)
                events.append(event)
                write_json(args.output_dir / "trajectory.partial.json", {"trajectory_id": trajectory_id, "events": events})
                if should_stop:
                    model_stop_status = decision.get("status")
                    completion_status = "COMPLETE" if model_stop_status == "SUCCESS" and all(row["passed"] for row in checkpoints.values()) else "PARTIAL"
                    break
            else:
                failure = f"max steps reached: {args.max_steps}"
                completion_status = "PARTIAL"
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            completion_status = "FAILED"
        finally:
            context.close()
            browser.close()

    trajectory = {
        "schema_version": "0.1",
        "trajectory_id": trajectory_id,
        "task_id": case["task_id"],
        "case_ref": case["case_ref"],
        "actor": {"actor_type": "MODEL_BROWSER_AGENT", "model": args.model, "transport": "libinfer-neo"},
        "collection_mode": "MODEL_DRIVEN_LOCAL_BROWSER",
        "privacy": {
            "raw_input_values_sent_to_model": False,
            "input_source": "SYNTHETIC_FIXTURE_REFERENCES",
            "external_navigation_allowed": False,
        },
        "viewport": {"width": 1600, "height": 1000, "device_pixel_ratio": 1},
        "started_at": started_at,
        "completed_at": utc_now(),
        "completion_status": completion_status,
        "events": events,
        "checkpoints": list(checkpoints.values()),
        "event_chain_sha256": sha256_text(canonical_json(events)),
        "libinfer_sessions": libinfer_sessions,
        "prompt_sha256": prompt_hash,
        "limitations": [
            "The model receives sanitized DOM observations, not screenshot pixels.",
            "All browser actions run in a copied local mirror with synthetic fixtures and blocked external navigation.",
            "A COMPLETE run proves task execution against the local protocol mock, not fidelity of unobserved private backend content.",
        ],
        "runtime_diagnostics": {
            "workbench_url": args.workbench_url,
            "model_stop_status": model_stop_status,
            "failure": failure,
            "external_request_count": len(external_requests),
            "external_requests": external_requests,
            "page_errors": page_errors,
            "console_errors": console_errors,
        },
    }
    write_json(args.output_dir / "trajectory.json", trajectory)
    (args.output_dir / "trajectory.partial.json").unlink(missing_ok=True)
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    schema_errors = [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(trajectory),
            key=lambda item: list(item.absolute_path),
        )
    ]
    summary = {
        "status": "PASS" if completion_status == "COMPLETE" and not schema_errors and not external_requests else "FAIL",
        "trajectory": str(args.output_dir / "trajectory.json"),
        "trajectory_id": trajectory_id,
        "model": args.model,
        "case_ref": case["case_ref"],
        "completion_status": completion_status,
        "events": len(events),
        "screenshots": len(list(screenshots.glob("*.png"))),
        "checkpoints_passed": sum(row["passed"] for row in checkpoints.values()),
        "checkpoints_total": len(checkpoints),
        "schema_errors": schema_errors,
        "failure": failure,
        "external_request_count": len(external_requests),
    }
    write_json(args.output_dir / "run_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
