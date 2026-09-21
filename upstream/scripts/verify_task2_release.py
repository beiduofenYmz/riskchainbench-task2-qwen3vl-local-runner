#!/usr/bin/env python3
"""Verify a frozen Task 2 release before runtime or model access."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from task2_frozen_protocol import (  # noqa: E402
    EXPECTED_TASK2_CONTRACT_SHA256,
    TASK2_CASE_COUNT,
    TASK2_TRAJECTORY_PROTOCOL,
    validate_task2_release,
)


SECRET_PATTERNS = {
    "github_token": re.compile(rb"(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"),
    "huggingface_token": re.compile(rb"hf_[A-Za-z0-9]{20,}"),
    "modelscope_token": re.compile(
        rb"ms-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    ),
    "private_key": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
}
TEXT_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".sha256",
    ".txt",
    ".yaml",
    ".yml",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def checked_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"unsafe release path: {value}")
    resolved_root = root.resolve()
    resolved = (root / relative).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"release path escapes root: {value}")
    return resolved


def validate_platform_manifest(
    root: Path,
    *,
    level: str,
) -> dict[str, Any]:
    file_manifest_path = root / "FILE_MANIFEST.jsonl"
    release_manifest_path = root / "RELEASE_MANIFEST.json"
    if not file_manifest_path.is_file() or not release_manifest_path.is_file():
        raise ValueError("platform release manifests are missing")
    rows = read_jsonl(file_manifest_path)
    release = read_json(release_manifest_path)
    if (
        release.get("schema_version")
        != "riskchainbench-platform-release/v0.1"
        or not str(release.get("status") or "").startswith("PASS_")
        or release.get("release_sha256")
        != embedded_hash(release, "release_sha256")
        or release.get("file_manifest_sha256")
        != sha256_file(file_manifest_path)
        or release.get("file_count") != len(rows)
        or release.get("total_bytes")
        != sum(int(row.get("bytes") or -1) for row in rows)
        or (release.get("credential_scan") or {}).get("status") != "PASS"
        or (release.get("credential_scan") or {}).get("secret_hits")
    ):
        raise ValueError("platform release manifest is invalid")
    paths = [str(row.get("path") or "") for row in rows]
    if not all(paths) or len(paths) != len(set(paths)):
        raise ValueError("platform file manifest paths are not unique")
    expected_paths = set(paths) | {
        "FILE_MANIFEST.jsonl",
        "RELEASE_MANIFEST.json",
    }
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        extras = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        raise ValueError(
            "platform release contains unmanifested or missing files: "
            f"extras={extras[:10]}, missing={missing[:10]}"
        )
    verified = 0
    deferred_archive_hashes = 0
    secret_hits: list[dict[str, str]] = []
    for row in rows:
        path = checked_path(root, str(row["path"]))
        if not path.is_file() or path.stat().st_size != int(row["bytes"]):
            raise ValueError(f"release payload is missing or truncated: {row['path']}")
        is_archive = str(row["path"]).endswith("/docker_image.tar.zst")
        if level == "full" or not is_archive:
            if sha256_file(path) != row.get("sha256"):
                raise ValueError(f"release payload hash mismatch: {row['path']}")
            verified += 1
        else:
            deferred_archive_hashes += 1
        if path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size <= 20_000_000:
            data = path.read_bytes()
            for name, pattern in SECRET_PATTERNS.items():
                if pattern.search(data):
                    secret_hits.append({"path": str(row["path"]), "kind": name})
    if secret_hits:
        raise ValueError(f"credential-like material detected: {secret_hits[:5]}")
    return {
        "release_sha256": release["release_sha256"],
        "visibility": release["visibility"],
        "file_count": len(rows),
        "total_bytes": release["total_bytes"],
        "verified_file_hash_count": verified,
        "deferred_archive_hash_count": deferred_archive_hashes,
    }


def validate_docker_archives(
    root: Path,
    *,
    contract: dict[str, Any],
    level: str,
) -> dict[str, Any]:
    manifest_path = root / "docker_images/manifest.jsonl"
    reference_path = root / "evaluator_only/docker_reference.jsonl"
    rows = read_jsonl(manifest_path)
    references = read_jsonl(reference_path)
    if len(rows) != TASK2_CASE_COUNT or len(references) != TASK2_CASE_COUNT:
        raise ValueError("Task 2 release must contain 600 Docker records")
    reference_by_case = {
        str(row.get("case_ref") or ""): row for row in references
    }
    case_refs: list[str] = []
    total_bytes = 0
    for ordinal, row in enumerate(rows, 1):
        case_ref = str(row.get("case_ref") or "")
        reference = reference_by_case.get(case_ref)
        archive = checked_path(root, str(row.get("archive") or ""))
        if (
            row.get("schema_version")
            != "riskchainbench-task2-docker-archive/v0.1"
            or row.get("ordinal") != ordinal
            or not case_ref
            or reference is None
            or row.get("archive_sha256") != reference.get("archive_sha256")
            or row.get("image_id") != reference.get("image_id")
            or not archive.is_file()
            or archive.stat().st_size != int(row.get("archive_bytes") or -1)
        ):
            raise ValueError(f"invalid Docker record: ordinal={ordinal}")
        if level == "full" and sha256_file(archive) != row["archive_sha256"]:
            raise ValueError(f"Docker archive hash mismatch: {case_ref}")
        case_refs.append(case_ref)
        total_bytes += archive.stat().st_size
    if (
        len(case_refs) != len(set(case_refs))
        or set(case_refs) != set(reference_by_case)
        or sha256_text("".join(f"{value}\n" for value in case_refs))
        != contract.get("ordered_case_refs_sha256")
    ):
        raise ValueError("Docker case set or order does not match the contract")
    return {
        "archive_count": len(rows),
        "total_archive_bytes": total_bytes,
        "archive_hash_level": level,
    }


def validate_runtime_supplement(
    root: Path,
    *,
    contract_sha256: str,
) -> dict[str, Any]:
    supplement_path = root / "runtime_supplement/runtime_supplement.json"
    if not supplement_path.is_file():
        raise ValueError("portable runtime supplement is missing")
    supplement = read_json(supplement_path)
    bundle = supplement.get("runtime_metadata_bundle") or {}
    bundle_path = checked_path(
        supplement_path.parent,
        str(bundle.get("path") or ""),
    )
    if (
        supplement.get("schema_version")
        != "riskchainbench-task2-runtime-supplement/v0.1"
        or supplement.get("task2_contract_sha256") != contract_sha256
        or supplement.get("case_count") != TASK2_CASE_COUNT
        or supplement.get("supplement_sha256")
        != embedded_hash(supplement, "supplement_sha256")
        or not bundle_path.is_file()
        or bundle_path.stat().st_size != int(bundle.get("bytes") or -1)
        or sha256_file(bundle_path) != bundle.get("sha256")
    ):
        raise ValueError("portable runtime supplement is invalid")
    return {
        "supplement_sha256": supplement["supplement_sha256"],
        "metadata_file_count": supplement.get("runtime_metadata_file_count"),
        "bundle_sha256": bundle["sha256"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task2-release", type=Path, required=True)
    parser.add_argument(
        "--level",
        choices=("metadata", "full"),
        default="metadata",
        help="full additionally hashes all 600 Docker archives.",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = utc_now()
    report: dict[str, Any] = {
        "schema_version": "riskchainbench-task2-release-verification/v0.1",
        "started_at": started_at,
        "finished_at": None,
        "status": "FAIL",
        "level": args.level,
        "task2_release": str(args.task2_release.resolve()),
        "expected_task2_contract_sha256": EXPECTED_TASK2_CONTRACT_SHA256,
        "task2_protocol_id": TASK2_TRAJECTORY_PROTOCOL["protocol_id"],
    }
    try:
        release = validate_task2_release(
            args.task2_release.resolve(),
            require_frozen_hash=True,
            verify_files=True,
        )
        report["contract_sha256"] = release["contract_sha256"]
        report["platform_manifest"] = validate_platform_manifest(
            args.task2_release,
            level=args.level,
        )
        report["docker_archives"] = validate_docker_archives(
            args.task2_release,
            contract=release["contract"],
            level=args.level,
        )
        report["runtime_supplement"] = validate_runtime_supplement(
            args.task2_release,
            contract_sha256=release["contract_sha256"],
        )
        report["status"] = "PASS"
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:2000]
    report["finished_at"] = utc_now()
    report["report_sha256"] = embedded_hash(report, "report_sha256")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
