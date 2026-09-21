#!/usr/bin/env python3
"""Shared fail-closed projection for text sent to external model services.

This module is intentionally transport-agnostic.  Callers first build their
least-privilege request, then pass the complete provider body through
``prepare_model_request_body`` immediately before JSON serialization.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import re
from typing import Any

import generate_obfuscated_session_dataset as privacy_scanner


URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://|\bwww\.")
PRIVATE_REF_RE = re.compile(r"(?i)\bprivate://")
IPV4_RE = re.compile(
    r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}(?![0-9])"
)
SCHEMA_TRANSPORT_METADATA_KEYS = frozenset({"$schema", "$id"})
MASKED_BOUNDARY_VALUE_RE = re.compile(r"^\[MASKED_[A-Z0-9_-]+\]$")


class ModelBoundaryError(ValueError):
    """A fixed-code failure which never includes the unsafe source value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def text_has_boundary_finding(value: Any) -> bool:
    """Return true for shared-scanner findings or legacy shortcut forms.

    The shared scanner is the source of truth for contextual short identifiers,
    email/contact data, domains and long numbers.  The explicit URL/private/IP
    checks retain the stricter evidence-agent rule that even reserved URLs and
    loopback addresses are not model-visible.  Scanner failure is unsafe.
    """

    text = str(value or "")
    try:
        findings = privacy_scanner.scan_public_boundary_findings(text)
    except Exception:
        return True
    if not isinstance(findings, list) or findings:
        return True
    return bool(URL_RE.search(text) or PRIVATE_REF_RE.search(text) or IPV4_RE.search(text))


def sanitize_model_text(value: Any) -> str:
    text = str(value or "")
    return "[MASKED_TEXT]" if text_has_boundary_finding(text) else text


def sanitize_model_tree(value: Any) -> Any:
    """Recursively project keys and values, masking a complete unsafe leaf."""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            key_is_unsafe = text_has_boundary_finding(key_text)
            if key_is_unsafe:
                key_text = f"masked-key-{sha256_text(key_text)[:16]}"
            if key_text in sanitized:
                raise ModelBoundaryError("MODEL_BOUNDARY_KEY_COLLISION")
            projected_child = sanitize_model_tree(child)
            # Structured JSON frequently separates the cue and identifier
            # into distinct leaves, e.g. ``{"邀请码": "ABCD"}``.  Neither
            # leaf is sensitive in isolation, so inspect scalar key/value
            # pairs together before retaining the value.  Containers are
            # handled recursively and schema property definitions therefore
            # remain usable.
            if (
                not key_is_unsafe
                and child is not None
                and not isinstance(child, (Mapping, list, tuple))
                and text_has_boundary_finding(f"{key}:{child}")
            ):
                projected_child = "[MASKED_TEXT]"
            sanitized[key_text] = projected_child
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_model_tree(child) for child in value]
    if isinstance(value, str):
        return sanitize_model_text(value)
    return value


def assert_model_tree_safe(value: Any) -> None:
    """Fail closed if any textual key or value survives projection unsafely."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if text_has_boundary_finding(key):
                raise ModelBoundaryError("MODEL_BOUNDARY_UNSAFE")
            if (
                child is not None
                and not isinstance(child, (Mapping, list, tuple))
                and not (
                    isinstance(child, str)
                    and MASKED_BOUNDARY_VALUE_RE.fullmatch(child)
                )
                and text_has_boundary_finding(f"{key}:{child}")
            ):
                raise ModelBoundaryError("MODEL_BOUNDARY_UNSAFE")
            assert_model_tree_safe(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            assert_model_tree_safe(child)
        return
    if isinstance(value, str) and text_has_boundary_finding(value):
        raise ModelBoundaryError("MODEL_BOUNDARY_UNSAFE")


def project_model_visible_schema(value: Any) -> Any:
    """Remove transport-only schema identifiers, then sanitize the public view.

    ``$schema`` and ``$id`` commonly contain public specification URLs.  They
    are useful to the local validator but not to the model, so the provider view
    omits them while callers retain the original schema for local validation.
    """

    def strip_metadata(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {
                str(key): strip_metadata(child)
                for key, child in node.items()
                if str(key) not in SCHEMA_TRANSPORT_METADATA_KEYS
            }
        if isinstance(node, (list, tuple)):
            return [strip_metadata(child) for child in node]
        return node

    projected = sanitize_model_tree(strip_metadata(value))
    assert_model_tree_safe(projected)
    return projected


def prepare_model_request_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return the only body permitted to cross the provider POST boundary."""

    if not isinstance(body, Mapping):
        raise ModelBoundaryError("MODEL_BODY_NOT_OBJECT")
    original_model = body.get("model")
    if not isinstance(original_model, str) or not original_model.strip():
        raise ModelBoundaryError("MODEL_ROUTE_MISSING")
    if text_has_boundary_finding(original_model):
        raise ModelBoundaryError("MODEL_ROUTE_UNSAFE")
    projected = sanitize_model_tree(body)
    if projected.get("model") != original_model:
        raise ModelBoundaryError("MODEL_ROUTE_UNSAFE")
    assert_model_tree_safe(projected)
    return projected
