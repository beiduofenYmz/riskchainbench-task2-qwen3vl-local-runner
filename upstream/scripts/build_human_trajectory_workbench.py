#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
from typing import Any
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "scripts" / "human_trajectory_workbench_template.html"
SCHEMA = PROJECT_ROOT / "schemas" / "human_browser_trajectory_v0.1.schema.json"
MODEL_BROWSER_SCHEMA = PROJECT_ROOT / "schemas" / "model_browser_trajectory_v0.1.schema.json"
MODEL_BROWSER_RUNS = PROJECT_ROOT / "outputs" / "model_browser_trajectories" / "pilot_20260717"
WP_EXPANDED_SITE = (
    PROJECT_ROOT
    / "outputs/go_site_mirrors/fofa-00056-scam-124.156.117.43-expanded/site"
)
WP_OFFLINE_TRANSLATION = (
    PROJECT_ROOT
    / "outputs/offline_translation_profiles/fofa-00056-scam-124.156.117.43.json"
)
HTML2CANVAS = (
    PROJECT_ROOT
    / "outputs/go_site_mirrors/fofa-02776-scam-www.infulator.in/site/assets/mirror/"
    / "www.infulator.in/static/js/html2canvas.min.js"
)
DEMO_BUNDLE = (
    PROJECT_ROOT
    / "outputs/evidence_cases/human_replay_demo_20260717/wp_session_full_v0.1"
)

CASES = [
    {
        "task_id": "HUMTASKwp-reader-session-v0.1",
        "case_ref": "CASEdd9cd1ede910e29019e5",
        "source_site": WP_EXPANDED_SITE,
        "offline_translation_profile": WP_OFFLINE_TRANSLATION,
        "start_path": "index.html",
        "title": "登录、刷新并退出本地读者会话",
        "objective": "在镜像页面中完成合成登录，刷新确认会话仍在，再退出登录。",
        "fidelity": "UNOBSERVED_ORIGINAL_STATE",
        "fixtures": [
            {"label": "用户名", "field": "log", "value": "mirror-reader@example.test"},
            {"label": "密码", "field": "pwd", "value": "4242"},
        ],
        "transitions": [
            {
                "id": "wp_login",
                "form_selector": "#loginform",
                "success_path": "__mirror/states/wp-reader-session.html",
                "validation_path": "__mirror/states/wp-login-validation.html",
                "rejected_path": "__mirror/states/wp-login-rejected.html",
                "required_values": {"log": "mirror-reader@example.test", "pwd": "4242"},
            },
            {
                "id": "wp_logout",
                "form_selector": ".sitemirror-state-controls form",
                "success_path": "index.html",
                "required_values": {},
            },
        ],
        "checkpoints": [
            {"id": "CPwp_user", "description": "填写合成用户名"},
            {"id": "CPwp_pass", "description": "填写合成密码"},
            {"id": "CPwp_submit", "description": "提交登录表单"},
            {"id": "CPwp_state", "description": "到达本地已登录状态"},
            {"id": "CPwp_reload", "description": "在已登录状态执行刷新"},
            {"id": "CPwp_logout", "description": "退出并返回公开页面"},
        ],
    },
    {
        "task_id": "HUMTASKmembership-request-v0.1",
        "case_ref": "CASEf7e8d17ade0284b6b0d5",
        "source_site": PROJECT_ROOT / "outputs/go_site_mirrors/fofa-02822-gambling-bettorhabitsapp.com/site",
        "start_path": "request/index.html",
        "title": "提交本地会员申请",
        "objective": "使用提供的合成资料提交会员申请，并确认页面进入待审核状态。",
        "fidelity": "PROTOCOL_EQUIVALENT_MOCK",
        "fixtures": [
            {"label": "邀请码", "field": "invite", "value": "MIRROR-DEMO"},
            {"label": "用户名", "field": "name", "value": "mirror-member"},
            {"label": "显示名", "field": "display_name", "value": "Mirror Member"},
            {"label": "邮箱", "field": "email", "value": "member@example.test"},
            {"label": "PIN", "field": "pin", "value": "4242"},
            {"label": "确认 PIN", "field": "pin2", "value": "4242"},
        ],
        "transitions": [
            {
                "id": "membership_request",
                "form_selector": "form.stack",
                "success_path": "__mirror/states/membership-request-pending.html",
                "validation_path": "__mirror/states/membership-request-validation.html",
                "rejected_path": "__mirror/states/membership-request-rejected.html",
                "required_values": {
                    "invite": "MIRROR-DEMO",
                    "name": "mirror-member",
                    "display_name": "Mirror Member",
                    "email": "member@example.test",
                    "pin": "4242",
                    "pin2": "4242",
                },
            }
        ],
        "checkpoints": [
            {"id": "CPmember_fields", "description": "填写六个合成字段"},
            {"id": "CPmember_submit", "description": "提交会员申请"},
            {"id": "CPmember_pending", "description": "到达本地待审核状态"},
        ],
    },
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rewrite_root_asset_refs(source: str, asset_prefix: str) -> str:
    return re.sub(r"([\"'(])\/assets\/", lambda match: match.group(1) + asset_prefix, source)


def make_embedded_snapshots_static_gateway_safe(site_root: Path) -> tuple[int, int]:
    """Rewrite root asset paths in live HTML and Base64 viewport-snapshot bodies."""
    changed_files = 0
    changed_snapshots = 0
    template_pattern = re.compile(
        r'(<template\b[^>]*data-sitemirror-viewport-snapshot[^>]*>)([A-Za-z0-9+/=\s]+)(</template>)',
        flags=re.IGNORECASE,
    )
    for path in sorted(site_root.rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        base_match = re.search(r'<base\s+href=["\'](/[^"\']*)["\']\s*/?>', source, flags=re.IGNORECASE)
        if base_match:
            route_depth = len([part for part in base_match.group(1).split("/") if part])
        else:
            route_depth = len(path.parent.relative_to(site_root).parts)
        asset_prefix = "../" * route_depth + "assets/"
        updated = rewrite_root_asset_refs(source, asset_prefix)

        def patch_snapshot(match: re.Match[str]) -> str:
            nonlocal changed_snapshots
            compact = "".join(match.group(2).split())
            try:
                decoded = base64.b64decode(compact, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return match.group(0)
            rewritten = rewrite_root_asset_refs(decoded, asset_prefix)
            if rewritten == decoded:
                return match.group(0)
            changed_snapshots += 1
            encoded = base64.b64encode(rewritten.encode("utf-8")).decode("ascii")
            return match.group(1) + encoded + match.group(3)

        updated = template_pattern.sub(patch_snapshot, updated)
        if updated != source:
            path.write_text(updated, encoding="utf-8")
            changed_files += 1
    return changed_files, changed_snapshots


def make_state_pages_static_gateway_safe(site_root: Path) -> int:
    """Keep state-page relative assets rooted at the copied site in a static file gateway."""
    changed = 0
    for path in sorted((site_root / "__mirror" / "states").glob("*.html")):
        source = path.read_text(encoding="utf-8")
        base_match = re.search(r'<base\s+href=["\']([^"\']*)["\']\s*/?>', source, flags=re.IGNORECASE)
        if base_match:
            original_href = base_match.group(1)
            if original_href.startswith("/") and not original_href.startswith("//"):
                replacement = f'<base href="../../{original_href.lstrip("/")}">'
                updated = source[:base_match.start()] + replacement + source[base_match.end():]
                route_depth = len([part for part in original_href.split("/") if part])
                asset_prefix = "../" * route_depth + "assets/"
                updated = re.sub(r"([\"'(])\/assets\/", rf"\1{asset_prefix}", updated)
                replacements = 1
            else:
                updated, replacements = source, 0
        else:
            updated, replacements = re.subn(
                r"(<head(?:\s[^>]*)?>)",
                r'\1<base href="../../">',
                source,
                count=1,
                flags=re.IGNORECASE,
            )
        if replacements:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    return changed


def make_percent_encoded_asset_aliases(site_root: Path) -> int:
    """Static servers URL-decode requests; provide decoded aliases for encoded filenames."""
    aliases = 0
    sources = [path for path in site_root.rglob("*") if path.is_file() and "%" in path.as_posix()]
    for source in sources:
        relative = source.relative_to(site_root)
        decoded_parts: list[str] = []
        valid = True
        for part in relative.parts:
            decoded = unquote(part)
            if decoded in {"", ".", ".."} or "/" in decoded or "\\" in decoded:
                valid = False
                break
            decoded_parts.append(decoded)
        if not valid or tuple(decoded_parts) == relative.parts:
            continue
        destination = site_root.joinpath(*decoded_parts)
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        aliases += 1
    return aliases


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        signature = handle.read(24)
    if len(signature) < 24 or signature[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return struct.unpack(">II", signature[16:24])


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_demo(out: Path) -> dict[str, Any]:
    scenario = DEMO_BUNDLE / "scenarios/wp_reader_session_and_logout"
    steps = read_jsonl(scenario / "steps.jsonl")
    demo_dir = out / "demo" / "automated_wp_session"
    demo_dir.mkdir(parents=True, exist_ok=True)
    started = steps[0]["started_at"] if steps else ""
    start_time = datetime.fromisoformat(started) if started else datetime.now(timezone.utc)
    events: list[dict[str, Any]] = []
    for sequence, step in enumerate(steps, 1):
        state_id = step["after_state_id"]
        state_json = json.loads((scenario / "states" / f"{state_id}.json").read_text(encoding="utf-8"))
        screenshot_source = scenario / "states" / f"{state_id}.png"
        screenshot_ref = None
        screenshot_width = screenshot_height = 0
        if screenshot_source.exists():
            destination = demo_dir / f"{state_id}.png"
            shutil.copy2(screenshot_source, destination)
            screenshot_ref = destination.relative_to(out).as_posix()
            screenshot_width, screenshot_height = png_dimensions(destination)
        operation = step.get("operation") or {}
        op = str(operation.get("op") or "ACTION")
        target = step.get("target_before")
        started_at = datetime.fromisoformat(step["started_at"])
        events.append(
            {
                "event_id": f"AE{sequence:03d}",
                "sequence": sequence,
                "elapsed_ms": int((started_at - start_time).total_seconds() * 1000),
                "event_type": {
                    "goto": "NAVIGATE",
                    "fill": "INPUT_COMMIT",
                    "click": "CLICK",
                    "reload": "TOOL_RELOAD",
                }.get(op, "ASSERTION"),
                "page_ref": "case://CASEdd9cd1ede910e29019e5/" + state_id,
                "operation": operation,
                "target": target,
                "state_diff": step.get("state_diff") or {},
                "safety_class": step.get("safety_class"),
                "status": step.get("status"),
                "screenshot_ref": screenshot_ref,
                "screenshot_width": screenshot_width,
                "screenshot_height": screenshot_height,
                "state": {
                    "title": state_json.get("title") or "",
                    "scroll_x": 0,
                    "scroll_y": 0,
                    "document_width": screenshot_width,
                    "document_height": screenshot_height,
                    "mirror_state_ids": state_json.get("mirror_state_ids") or [],
                },
            }
        )
    return {
        "trajectory_id": "AUTOwp-reader-full-v0.1",
        "actor_type": "AUTOMATED_PLAYWRIGHT_PROFILE",
        "label": "自动浏览器示例：登录、刷新、退出",
        "case_ref": "CASEdd9cd1ede910e29019e5",
        "task_id": "HUMTASKwp-reader-session-v0.1",
        "capture_mode": "FULL",
        "overall": "PASS",
        "events": events,
        "limitations": [
            "This is a deterministic Playwright profile replay, not an LLM browser-agent run.",
            "It demonstrates the dynamic action player and local protocol states.",
        ],
    }


def load_text_model_runs() -> list[dict[str, Any]]:
    root = PROJECT_ROOT / "outputs/annotation_tasks/web_only_pilot_20260717_v0.1/trajectories"
    runs: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        runs.append(
            {
                "trajectory_id": value.get("trajectory_id"),
                "case_ref": value.get("case_ref"),
                "model": value.get("model"),
                "task_setting": value.get("task_setting"),
                "web_risk_signal": value.get("web_risk_signal"),
                "chain_answer": value.get("chain_answer"),
                "evidence_status": value.get("evidence_status"),
                "confidence": value.get("confidence"),
                "risk_reason": value.get("risk_reason"),
                "audit_steps": value.get("audit_steps") or [],
                "claims": value.get("claims") or [],
            }
        )
    return runs


def load_model_browser_replays(out: Path) -> list[dict[str, Any]]:
    replays: list[dict[str, Any]] = []
    if not MODEL_BROWSER_RUNS.exists():
        return replays
    destination_root = out / "model_browser_replays"
    for path in sorted(MODEL_BROWSER_RUNS.rglob("trajectory.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        actor = value.get("actor") if isinstance(value.get("actor"), dict) else {}
        if actor.get("actor_type") != "MODEL_BROWSER_AGENT":
            continue
        trajectory_id = str(value.get("trajectory_id") or path.parent.name)
        safe_id = re.sub(r"[^0-9A-Za-z._-]+", "_", trajectory_id)
        destination = destination_root / safe_id
        shutil.copytree(path.parent, destination)
        copied = json.loads((destination / "trajectory.json").read_text(encoding="utf-8"))
        for event in copied.get("events") or []:
            screenshot_ref = event.get("screenshot_ref")
            if screenshot_ref:
                event["screenshot_ref"] = (
                    Path("model_browser_replays") / safe_id / str(screenshot_ref)
                ).as_posix()
        model = str(actor.get("model") or "unknown-model")
        status = str(copied.get("completion_status") or "UNKNOWN")
        case_ref = str(copied.get("case_ref") or "")
        task = next((row for row in CASES if row["case_ref"] == case_ref), None)
        task_label = str(task["title"] if task else copied.get("task_id") or case_ref or path.parent.name)
        copied["task_title"] = task_label
        copied["label"] = f"模型浏览器 · {model} · {task_label} · {status}"
        try:
            source_ref = path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            source_ref = path.as_posix()
        copied["source_trajectory_ref"] = source_ref
        replays.append(copied)
    return replays


def write_hashes(out: Path) -> int:
    rows: list[str] = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "sha256sums.txt":
            rows.append(f"{sha256_file(path)}  {path.relative_to(out).as_posix()}")
    (out / "sha256sums.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a direct human browser-trajectory recorder and visual player.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.out.exists():
        if not args.replace:
            raise FileExistsError(f"output exists: {args.out}")
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)
    public_cases: list[dict[str, Any]] = []
    patched_state_page_count = 0
    percent_encoded_alias_count = 0
    static_gateway_html_count = 0
    static_gateway_snapshot_count = 0
    for case in CASES:
        destination = args.out / "sites" / case["case_ref"]
        shutil.copytree(case["source_site"], destination)
        html_count, snapshot_count = make_embedded_snapshots_static_gateway_safe(destination)
        static_gateway_html_count += html_count
        static_gateway_snapshot_count += snapshot_count
        patched_state_page_count += make_state_pages_static_gateway_safe(destination)
        percent_encoded_alias_count += make_percent_encoded_asset_aliases(destination)
        public_case = {
            key: value
            for key, value in case.items()
            if key not in {"source_site", "offline_translation_profile"}
        }
        translation_path = case.get("offline_translation_profile")
        if translation_path:
            if not translation_path.is_file():
                raise FileNotFoundError(translation_path)
            public_case["offline_translation"] = json.loads(
                translation_path.read_text(encoding="utf-8")
            )
        public_cases.append(public_case)
    vendor = args.out / "vendor"
    vendor.mkdir()
    shutil.copy2(HTML2CANVAS, vendor / "html2canvas.min.js")
    schema_dir = args.out / "schemas"
    schema_dir.mkdir()
    shutil.copy2(SCHEMA, schema_dir / SCHEMA.name)
    shutil.copy2(MODEL_BROWSER_SCHEMA, schema_dir / MODEL_BROWSER_SCHEMA.name)
    demo = build_demo(args.out)
    model_browser_replays = load_model_browser_replays(args.out)
    text_runs = load_text_model_runs()
    manifest = {
        "schema_version": "0.1",
        "workbench": "human_browser_trajectory",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task_count": len(public_cases),
        "cases": public_cases,
        "model_browser_replays": model_browser_replays,
        "automated_replays": [demo],
        "text_model_runs": text_runs,
        "boundaries": {
            "human_collection": "Direct interaction with copied local mirrors; raw input values are never exported.",
            "model_browser_replay": "libinfer-neo chooses each action from sanitized DOM observations; Playwright executes it in the isolated local mirror and captures provenance screenshots.",
            "automated_replay": "Deterministic Playwright browser actions with screenshots.",
            "text_model_runs": "Offline evidence-review outputs; they are not browser-action trajectories and are not animated as such.",
        },
    }
    write_json(args.out / "manifest.json", manifest)
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    template = TEMPLATE.read_text(encoding="utf-8")
    (args.out / "index.html").write_text(template.replace("__WORKBENCH_DATA__", encoded), encoding="utf-8")
    file_count = write_hashes(args.out)
    print(json.dumps({
        "status": "PASS",
        "out": str(args.out),
        "task_count": len(public_cases),
        "automated_replay_count": 1,
        "model_browser_replay_count": len(model_browser_replays),
        "text_model_run_count": len(text_runs),
        "patched_state_page_count": patched_state_page_count,
        "percent_encoded_alias_count": percent_encoded_alias_count,
        "static_gateway_html_count": static_gateway_html_count,
        "static_gateway_snapshot_count": static_gateway_snapshot_count,
        "hashed_file_count": file_count,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
