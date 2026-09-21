#!/usr/bin/env python3
"""Probe an OpenAI-compatible endpoint with locally generated benign images."""

from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

from PIL import Image, ImageDraw, ImageFont


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from model_request_boundary import prepare_model_request_body


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_export_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"invalid env line {path}:{line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid env key {path}:{line_number}")
        parsed = shlex.split(raw_value, comments=True, posix=True)
        values[key] = parsed[0] if parsed else ""
    return values


def chat_completions_url(value: str) -> str:
    base = value.rstrip("/")
    lowered = base.lower()
    if "oneapi" in lowered or "not-for-automation" in lowered:
        raise ValueError("endpoint is forbidden by the benchmark transport policy")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def libinfer_chat_url(value: str) -> str:
    """Compatibility alias for older internal callers."""

    return chat_completions_url(value)


def font(size: int) -> ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def probe_code(seed: str, model: str, index: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    digest = hashlib.sha256(f"{seed}|{model}|{index}".encode("utf-8")).digest()
    return "".join(alphabet[value % len(alphabet)] for value in digest[:6])


def render_code_png(code: str) -> bytes:
    image = Image.new("RGB", (480, 180), color=(248, 248, 248))
    draw = ImageDraw.Draw(image)
    selected_font = font(92)
    bbox = draw.textbbox((0, 0), code, font=selected_font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.rectangle((8, 8, 471, 171), outline=(30, 30, 30), width=4)
    draw.text(
        ((480 - width) / 2, (180 - height) / 2 - bbox[1]),
        code,
        fill=(16, 16, 16),
        font=selected_font,
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def multimodal_body(
    model: str,
    png: bytes,
    *,
    run_id: str,
    probe_index: int,
    max_tokens: int = 512,
    transport: str = "libinfer-neo",
) -> dict[str, Any]:
    image_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Read the six-character code printed in the image. "
                            "Return only those six uppercase letters or digits, with no explanation."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
                ],
            }
        ],
        # Current libinfer vision routes can spend tens of tokens on hidden
        # reasoning before emitting the six-character answer.  A 24-token
        # cap produced a valid image request with an empty final content and
        # therefore confused truncation with lack of image capability.
        "max_tokens": max_tokens,
    }
    if transport == "libinfer-neo":
        body.update(
            {
                "libinfer-notes": {
                    "project": "riskchainbench",
                    "task": f"multimodal-route-probe-{probe_index}",
                    "runId": run_id,
                    "extra": "local-benign-image-capability-probe-v0.1",
                },
                "libinfer-metadata": {
                    "protocol": "riskchainbench_multimodal_route_probe_v0.1",
                    "probe_index": probe_index,
                    "image_sha256": sha256_bytes(png),
                },
                "libinfer-retries": 1,
            }
        )
    elif transport != "openai-compatible":
        raise ValueError("unsupported model transport")
    # The shared boundary correctly masks arbitrary image data because it
    # cannot prove pixel privacy.  This capability probe is narrower: the
    # raster was generated in this process from a six-character random code.
    # Validate the exact local bytes, run the complete non-image request
    # through the shared boundary with a typed placeholder, then restore only
    # the attested image leaf.  Production webpage screenshots must not use
    # this exception.
    projected = copy.deepcopy(body)
    projected["messages"][0]["content"][1]["image_url"]["url"] = "[MASKED_IMAGE]"
    safe = prepare_model_request_body(projected)
    if safe["messages"][0]["content"][1]["image_url"]["url"] != "[MASKED_IMAGE]":
        raise ValueError("local benign image placeholder did not survive model boundary")
    decoded = base64.b64decode(image_url.split(",", 1)[1], validate=True)
    if decoded != png or not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("local benign image attestation failed")
    safe["messages"][0]["content"][1]["image_url"]["url"] = image_url
    return safe


def call_chat_completions(
    *, base_url: str, api_key: str, body: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    request = urllib.request.Request(
        chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(
            f"endpoint HTTP {error.code}: {route_error_reason(detail)}"
        ) from error
    if not isinstance(payload, dict) or payload.get("error"):
        raise RuntimeError("endpoint returned an invalid or error response")
    return payload


def call_libinfer(
    *, base_url: str, api_key: str, body: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    """Compatibility alias for older internal callers."""

    return call_chat_completions(
        base_url=base_url,
        api_key=api_key,
        body=body,
        timeout_seconds=timeout_seconds,
    )


def route_error_reason(detail: str) -> str:
    """Map provider details to a shareable capability reason code."""

    lowered = detail.lower()
    if "unknown variant" in lowered and "image_url" in lowered:
        return "IMAGE_INPUT_SCHEMA_TEXT_ONLY"
    if "unexpected item type in content" in lowered:
        return "IMAGE_CONTENT_ITEM_REJECTED"
    if "bad_response_status_code" in lowered or "openai_error" in lowered:
        return "UPSTREAM_IMAGE_REQUEST_REJECTED"
    if "image" in lowered or "vision" in lowered or "multimodal" in lowered:
        return "IMAGE_INPUT_REJECTED"
    return "ROUTE_REQUEST_REJECTED_DETAIL_REDACTED"


def response_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("endpoint response has no message content") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(row.get("text") or "")
            for row in content
            if isinstance(row, dict) and row.get("type") == "text"
        )
    raise ValueError("unsupported endpoint response content")


def normalized_code(value: str) -> str:
    matches = re.findall(r"(?i)(?<![A-Z0-9])[A-Z0-9]{6}(?![A-Z0-9])", value.upper())
    return matches[0] if len(set(matches)) == 1 else ""


def evaluate_probe(expected: str, content: str) -> bool:
    return normalized_code(content) == expected


def probe_model(
    *,
    model: str,
    seed: str,
    probe_count: int,
    base_url: str,
    api_key: str,
    timeout_seconds: float,
    max_tokens: int,
    transport: str,
) -> dict[str, Any]:
    rows = []
    for index in range(probe_count):
        code = probe_code(seed, model, index)
        png = render_code_png(code)
        run_id = str(uuid.uuid4())
        body = multimodal_body(
            model,
            png,
            run_id=run_id,
            probe_index=index,
            max_tokens=max_tokens,
            transport=transport,
        )
        started = time.monotonic()
        row: dict[str, Any] = {
            "probe_index": index,
            "expected_code": code,
            "image_sha256": sha256_bytes(png),
            "image_size_bytes": len(png),
            "request_sha256": sha256_text(canonical_json(body)),
            "started_at": utc_now(),
        }
        try:
            response = call_chat_completions(
                base_url=base_url,
                api_key=api_key,
                body=body,
                timeout_seconds=timeout_seconds,
            )
            content = response_content(response)
            row.update(
                {
                    "status": "PASS" if evaluate_probe(code, content) else "FAIL_WRONG_CONTENT",
                    "response_content": content[:1000],
                    "response_sha256": sha256_text(content),
                    "response_model": response.get("model"),
                    "usage": response.get("usage"),
                }
            )
        except Exception as exc:
            row.update(
                {
                    "status": "FAIL_TRANSPORT_OR_ROUTE",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:2000],
                }
            )
        row["duration_seconds"] = round(time.monotonic() - started, 3)
        row["finished_at"] = utc_now()
        rows.append(row)
    passed = sum(row["status"] == "PASS" for row in rows)
    return {
        "model": model,
        "status": "PASS_MULTIMODAL_ROUTE" if passed == probe_count else "FAIL_MULTIMODAL_ROUTE",
        "probe_count": probe_count,
        "pass_count": passed,
        "probes": rows,
    }


def run_probe(
    *,
    models: list[str],
    priority: list[str],
    seed: str,
    probe_count: int,
    base_url: str,
    api_key: str,
    timeout_seconds: float,
    max_tokens: int,
    transport: str = "libinfer-neo",
) -> dict[str, Any]:
    if probe_count < 2:
        raise ValueError("at least two image probes are required")
    if not 64 <= max_tokens <= 4096:
        raise ValueError("max_tokens must be between 64 and 4096")
    if set(priority) != set(models) or len(priority) != len(models):
        raise ValueError("priority must contain every model exactly once")
    started_at = utc_now()
    results = [
        probe_model(
            model=model,
            seed=seed,
            probe_count=probe_count,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            transport=transport,
        )
        for model in models
    ]
    by_model = {row["model"]: row for row in results}
    selected = next(
        (model for model in priority if by_model[model]["status"] == "PASS_MULTIMODAL_ROUTE"),
        None,
    )
    payload: dict[str, Any] = {
        "schema_version": "openai-compatible-multimodal-route-probe/v0.2",
        "transport": transport,
        "oneapi_used": False,
        "started_at": started_at,
        "finished_at": utc_now(),
        "probe_seed_sha256": sha256_text(seed),
        "probe_count_per_model": probe_count,
        "max_tokens_per_probe": max_tokens,
        "temperature_policy": "provider_default_omitted",
        "models": models,
        "selection_priority": priority,
        "results": results,
        "passing_model_count": sum(
            row["status"] == "PASS_MULTIMODAL_ROUTE" for row in results
        ),
        "selected_fixed_mllm": selected,
        "status": "PASS_FIXED_MLLM_SELECTED" if selected else "BLOCKED_NO_MULTIMODAL_ROUTE",
        "blockers": [] if selected else ["NO_TESTED_MULTIMODAL_ROUTE"],
    }
    payload["report_sha256"] = sha256_text(canonical_json(payload))
    return payload


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--transport",
        choices=("libinfer-neo", "openai-compatible"),
        default="libinfer-neo",
    )
    parser.add_argument("--base-url-env")
    parser.add_argument("--api-key-env")
    parser.add_argument("--models", required=True)
    parser.add_argument(
        "--priority",
        help="Comma-separated selection priority; defaults to --models order.",
    )
    parser.add_argument("--seed", default="riskchainbench-multimodal-route-v0.1")
    parser.add_argument("--probe-count", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.out.exists():
        print(json.dumps({"status": "FAIL", "error": "refusing to overwrite existing probe report"}))
        return 1
    try:
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
        base_url = environment.get(base_url_env) or os.environ.get(base_url_env)
        api_key = environment.get(api_key_env) or os.environ.get(api_key_env)
        if not base_url or not api_key:
            raise ValueError(
                f"missing endpoint credentials in {base_url_env}/{api_key_env}"
            )
        chat_completions_url(base_url)
        models = parse_csv(args.models)
        report = run_probe(
            models=models,
            priority=parse_csv(args.priority) if args.priority else models,
            seed=args.seed,
            probe_count=args.probe_count,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=args.timeout_seconds,
            max_tokens=args.max_tokens,
            transport=args.transport,
        )
        atomic_json(args.out, report)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "transport": report["transport"],
                "oneapi_used": report["oneapi_used"],
                "passing_model_count": report["passing_model_count"],
                "selected_fixed_mllm": report["selected_fixed_mllm"],
                "results": [
                    {
                        "model": row["model"],
                        "status": row["status"],
                        "pass_count": row["pass_count"],
                        "probe_count": row["probe_count"],
                    }
                    for row in report["results"]
                ],
                "out": str(args.out),
                "report_sha256": report["report_sha256"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
