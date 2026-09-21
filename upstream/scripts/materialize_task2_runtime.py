#!/usr/bin/env python3
"""Materialize signed Task 2 OCI archives into a portable local replay root."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from task2_frozen_protocol import EXPECTED_TASK2_CONTRACT_SHA256  # noqa: E402

RUNTIME_ATTESTATION_NAME = ".riskchainbench-runtime-attestation.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def checked_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe archive path: {value}")
    return path


def verify_runtime_metadata(
    task2_release: Path,
    supplement_path: Path,
) -> dict[str, Any]:
    contract = read_json(task2_release / "task2_contract.json")
    if (
        contract.get("contract_sha256") != EXPECTED_TASK2_CONTRACT_SHA256
        or embedded_hash(contract, "contract_sha256")
        != EXPECTED_TASK2_CONTRACT_SHA256
    ):
        raise ValueError("Task 2 release is not the frozen Balanced-600 contract")
    supplement = read_json(supplement_path)
    if (
        supplement.get("schema_version")
        != "riskchainbench-task2-runtime-supplement/v0.1"
        or supplement.get("task2_contract_sha256")
        != EXPECTED_TASK2_CONTRACT_SHA256
        or supplement.get("case_count") != 600
        or supplement.get("supplement_sha256")
        != embedded_hash(supplement, "supplement_sha256")
    ):
        raise ValueError("invalid Task 2 runtime supplement")
    supplement_root = supplement_path.parent
    bundle_ref = supplement.get("runtime_metadata_bundle") or {}
    bundle = supplement_root / checked_relative_path(str(bundle_ref.get("path")))
    if (
        not bundle.is_file()
        or bundle.stat().st_size != int(bundle_ref.get("bytes") or -1)
        or sha256_file(bundle) != bundle_ref.get("sha256")
    ):
        raise ValueError("runtime metadata bundle is missing or invalid")
    present = [
        (
            supplement_root
            / checked_relative_path(str(row["runtime_path"]))
        ).is_file()
        for row in supplement.get("files") or []
    ]
    if any(present) and not all(present):
        raise ValueError("runtime metadata source is only partially present")
    if all(present):
        verify_runtime_metadata_files(
            source_root=supplement_root,
            supplement=supplement,
        )
    archive_manifest = task2_release / supplement["docker_archive_manifest"]["path"]
    if sha256_file(archive_manifest) != supplement["docker_archive_manifest"]["sha256"]:
        raise ValueError("Docker archive manifest hash mismatch")
    return supplement


def verify_runtime_metadata_files(
    *,
    source_root: Path,
    supplement: dict[str, Any],
) -> None:
    for row in supplement.get("files") or []:
        path = source_root / checked_relative_path(str(row["runtime_path"]))
        if (
            not path.is_file()
            or path.stat().st_size != int(row["bytes"])
            or sha256_file(path) != row["sha256"]
        ):
            raise ValueError(f"runtime metadata mismatch: {row['runtime_path']}")


def stage_runtime_metadata_from_source(
    *,
    source_root: Path,
    supplement: dict[str, Any],
    runtime_root: Path,
) -> int:
    verify_runtime_metadata_files(
        source_root=source_root,
        supplement=supplement,
    )
    copied = 0
    for row in supplement.get("files") or []:
        source = source_root / checked_relative_path(str(row["runtime_path"]))
        destination = runtime_root / checked_relative_path(str(row["source_path"]))
        if (
            destination.is_file()
            and destination.stat().st_size == int(row["bytes"])
            and sha256_file(destination) == row["sha256"]
        ):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if sha256_file(destination) != row["sha256"]:
            raise ValueError(f"failed to stage runtime metadata: {row['source_path']}")
        copied += 1
    return copied


def stage_runtime_metadata(
    *,
    supplement_path: Path,
    supplement: dict[str, Any],
    runtime_root: Path,
) -> int:
    supplement_root = supplement_path.parent
    source_files = [
        supplement_root / checked_relative_path(str(row["runtime_path"]))
        for row in supplement.get("files") or []
    ]
    if source_files and all(path.is_file() for path in source_files):
        return stage_runtime_metadata_from_source(
            source_root=supplement_root,
            supplement=supplement,
            runtime_root=runtime_root,
        )

    bundle_ref = supplement.get("runtime_metadata_bundle") or {}
    bundle = supplement_root / checked_relative_path(str(bundle_ref.get("path")))
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".riskchainbench-runtime-metadata-",
        dir=str(runtime_root.parent),
    ) as temporary_root_value:
        temporary_root = Path(temporary_root_value)
        extracted_root = temporary_root / "extracted"
        extract_zstd_tar(bundle, extracted_root)
        if not (extracted_root / "files").is_dir():
            raise ValueError("runtime metadata bundle has no files root")
        return stage_runtime_metadata_from_source(
            source_root=extracted_root,
            supplement=supplement,
            runtime_root=runtime_root,
        )


def extract_zstd_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    decompressor = subprocess.Popen(
        ["zstd", "-dc", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert decompressor.stdout is not None
    extractor = subprocess.run(
        ["tar", "-xf", "-", "-C", str(destination)],
        stdin=decompressor.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    decompressor.stdout.close()
    _, decompress_error = decompressor.communicate()
    if decompressor.returncode != 0 or extractor.returncode != 0:
        detail = (
            decompress_error.decode("utf-8", errors="replace")
            + extractor.stderr.decode("utf-8", errors="replace")
        )[:2000]
        raise ValueError(f"failed to extract zstd tar archive: {detail}")


def safe_member_target(root: Path, name: str) -> Path:
    relative = checked_relative_path(name)
    target = root / relative
    resolved_root = root.resolve()
    resolved_parent = target.parent.resolve()
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise ValueError(f"layer member escapes root: {name}")
    return target


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def extract_layer(blob: Path, root: Path) -> None:
    with tarfile.open(blob, mode="r:*") as archive:
        for member in archive.getmembers():
            target = safe_member_target(root, member.name)
            basename = target.name
            if basename == ".wh..wh..opq":
                if target.parent.is_dir():
                    for child in target.parent.iterdir():
                        remove_path(child)
                continue
            if basename.startswith(".wh."):
                remove_path(target.with_name(basename[4:]))
                continue
            if member.issym() or member.islnk():
                link = Path(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise ValueError(f"unsafe layer link: {member.name}")
            archive.extract(member, path=root, set_attrs=True)


def materialize_case(
    *,
    task2_release: Path,
    runtime_root: Path,
    binding: dict[str, Any],
    archive_row: dict[str, Any],
    replace: bool,
) -> dict[str, Any]:
    case_ref = str(binding["case_ref"])
    archive = task2_release / checked_relative_path(str(archive_row["archive"]))
    if (
        not archive.is_file()
        or archive.stat().st_size != int(archive_row["archive_bytes"])
        or sha256_file(archive) != archive_row["archive_sha256"]
    ):
        raise ValueError(f"Docker archive mismatch: {case_ref}")
    destination = runtime_root / checked_relative_path(str(binding["site_output"]))
    binary = destination / "mirrorserve"
    site = destination / "site"
    source_binary_sha256 = str(binding["runtime_binary_sha256"])
    attestation_path = destination / RUNTIME_ATTESTATION_NAME
    if destination.exists() and not replace:
        if binary.is_file() and site.is_dir() and attestation_path.is_file():
            attestation = read_json(attestation_path)
            archive_binary_sha256 = sha256_file(binary)
            content_valid = (
                attestation.get("schema_version")
                == "riskchainbench-runtime-attestation/v0.1"
                and attestation.get("case_ref") == case_ref
                and attestation.get("archive_sha256")
                == archive_row["archive_sha256"]
                and attestation.get("image_id") == archive_row["image_id"]
                and attestation.get("source_runtime_binary_sha256")
                == source_binary_sha256
                and attestation.get("archive_runtime_binary_sha256")
                == archive_binary_sha256
                and attestation.get("attestation_sha256")
                == embedded_hash(attestation, "attestation_sha256")
            )
            if content_valid:
                contract_attestation_rebound = (
                    attestation.get("task2_contract_sha256")
                    != EXPECTED_TASK2_CONTRACT_SHA256
                )
                if contract_attestation_rebound:
                    attestation["task2_contract_sha256"] = (
                        EXPECTED_TASK2_CONTRACT_SHA256
                    )
                    attestation["attestation_sha256"] = embedded_hash(
                        attestation,
                        "attestation_sha256",
                    )
                    atomic_json(attestation_path, attestation)
                return {
                    "case_ref": case_ref,
                    "status": "PASS_ALREADY_MATERIALIZED",
                    "contract_attestation_rebound": (
                        contract_attestation_rebound
                    ),
                    "site_output": str(destination),
                    "archive_sha256": archive_row["archive_sha256"],
                    "source_runtime_binary_sha256": source_binary_sha256,
                    "archive_runtime_binary_sha256": archive_binary_sha256,
                    "runtime_binary_sha256": archive_binary_sha256,
                    "source_runtime_binary_match": (
                        archive_binary_sha256 == source_binary_sha256
                    ),
                    "runtime_attestation_path": (
                        attestation_path.relative_to(runtime_root).as_posix()
                    ),
                    "runtime_attestation_sha256": sha256_file(attestation_path),
                }
        raise ValueError(f"existing runtime is incomplete or stale: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.materializing-{uuid.uuid4().hex}"
    )
    if temporary.exists():
        shutil.rmtree(temporary)
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"oci-{case_ref}-",
            dir=str(destination.parent),
        ) as oci_dir_value:
            oci_dir = Path(oci_dir_value)
            extract_zstd_tar(archive, oci_dir / "layout")
            layout = oci_dir / "layout"
            index = read_json(layout / "index.json")
            descriptors = index.get("manifests") or []
            if len(descriptors) != 1:
                raise ValueError(f"OCI archive must contain one image: {case_ref}")
            manifest_digest = str(descriptors[0]["digest"]).removeprefix("sha256:")
            if manifest_digest != str(archive_row["image_id"]).removeprefix("sha256:"):
                raise ValueError(f"OCI image ID mismatch: {case_ref}")
            manifest_blob = layout / "blobs/sha256" / manifest_digest
            if sha256_file(manifest_blob) != manifest_digest:
                raise ValueError(f"OCI manifest digest mismatch: {case_ref}")
            manifest = read_json(manifest_blob)
            temporary.mkdir(parents=True)
            for layer in manifest.get("layers") or []:
                layer_digest = str(layer["digest"]).removeprefix("sha256:")
                layer_blob = layout / "blobs/sha256" / layer_digest
                if sha256_file(layer_blob) != layer_digest:
                    raise ValueError(f"OCI layer digest mismatch: {case_ref}")
                extract_layer(layer_blob, temporary)
        extracted_binary = temporary / "mirrorserve"
        if not extracted_binary.is_file() or not (temporary / "site").is_dir():
            raise ValueError(f"materialized runtime hash mismatch: {case_ref}")
        archive_binary_sha256 = sha256_file(extracted_binary)
        attestation = {
            "schema_version": "riskchainbench-runtime-attestation/v0.1",
            "case_ref": case_ref,
            "task2_contract_sha256": EXPECTED_TASK2_CONTRACT_SHA256,
            "archive_sha256": archive_row["archive_sha256"],
            "image_id": archive_row["image_id"],
            "source_runtime_binary_sha256": source_binary_sha256,
            "archive_runtime_binary_sha256": archive_binary_sha256,
            "source_runtime_binary_match": (
                archive_binary_sha256 == source_binary_sha256
            ),
        }
        attestation["attestation_sha256"] = embedded_hash(
            attestation,
            "attestation_sha256",
        )
        atomic_json(temporary / RUNTIME_ATTESTATION_NAME, attestation)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {
        "case_ref": case_ref,
        "status": "PASS_MATERIALIZED",
        "contract_attestation_rebound": False,
        "site_output": str(destination),
        "archive_sha256": archive_row["archive_sha256"],
        "source_runtime_binary_sha256": source_binary_sha256,
        "archive_runtime_binary_sha256": archive_binary_sha256,
        "runtime_binary_sha256": archive_binary_sha256,
        "source_runtime_binary_match": (
            archive_binary_sha256 == source_binary_sha256
        ),
        "runtime_attestation_path": (
            (destination / RUNTIME_ATTESTATION_NAME)
            .relative_to(runtime_root)
            .as_posix()
        ),
        "runtime_attestation_sha256": sha256_file(
            destination / RUNTIME_ATTESTATION_NAME
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task2-release", type=Path, required=True)
    parser.add_argument("--supplement", type=Path)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--case-ref", action="append", default=[])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    task2_release = args.task2_release.resolve()
    supplement_path = (
        args.supplement.resolve()
        if args.supplement
        else task2_release / "runtime_supplement/runtime_supplement.json"
    )
    runtime_root = args.runtime_root.resolve()
    report_path = args.report or runtime_root / "materialization_report.json"
    started_at = utc_now()
    try:
        if not 1 <= args.workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        supplement = verify_runtime_metadata(task2_release, supplement_path)
        staged_metadata_count = stage_runtime_metadata(
            supplement_path=supplement_path,
            supplement=supplement,
            runtime_root=runtime_root,
        )
        resolver = read_json(task2_release / supplement["resolver"]["path"])
        bindings = {
            str(row["case_ref"]): row for row in resolver.get("bindings") or []
        }
        archives = {
            str(row["case_ref"]): row
            for row in read_jsonl(
                task2_release / supplement["docker_archive_manifest"]["path"]
            )
        }
        selected = args.case_ref or sorted(bindings)
        if len(selected) != len(set(selected)):
            raise ValueError("case-ref values must be unique")
        unknown = sorted(set(selected) - set(bindings))
        if unknown:
            raise ValueError("unknown case-ref values: " + ",".join(unknown))
        rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        if not args.metadata_only:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(
                        materialize_case,
                        task2_release=task2_release,
                        runtime_root=runtime_root,
                        binding=bindings[case_ref],
                        archive_row=archives[case_ref],
                        replace=args.replace,
                    ): case_ref
                    for case_ref in selected
                }
                for future in as_completed(futures):
                    case_ref = futures[future]
                    try:
                        rows.append(future.result())
                    except Exception as exc:  # noqa: BLE001
                        failures.append(
                            {
                                "case_ref": case_ref,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            }
                        )
        source_runtime_binary_mismatch_count = sum(
            row.get("source_runtime_binary_match") is False for row in rows
        )
        contract_attestation_rebound_count = sum(
            row.get("contract_attestation_rebound") is True for row in rows
        )
        report: dict[str, Any] = {
            "schema_version": "riskchainbench-task2-materialization/v0.2",
            "status": "PASS" if not failures else "FAIL",
            "started_at": started_at,
            "finished_at": utc_now(),
            "task2_contract_sha256": EXPECTED_TASK2_CONTRACT_SHA256,
            "runtime_supplement_sha256": supplement["supplement_sha256"],
            "runtime_root": str(runtime_root),
            "staged_runtime_metadata_file_count": staged_metadata_count,
            "requested_case_count": len(selected),
            "materialized_case_count": len(rows),
            "failure_count": len(failures),
            "source_runtime_binary_mismatch_count": (
                source_runtime_binary_mismatch_count
            ),
            "contract_attestation_rebound_count": (
                contract_attestation_rebound_count
            ),
            "rows": sorted(rows, key=lambda row: row["case_ref"]),
            "failures": sorted(failures, key=lambda row: row["case_ref"]),
        }
        report["report_sha256"] = embedded_hash(report, "report_sha256")
        atomic_json(report_path, report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(canonical_json({"status": "FAIL", "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "requested_case_count": report["requested_case_count"],
                "materialized_case_count": report["materialized_case_count"],
                "failure_count": report["failure_count"],
                "runtime_root": report["runtime_root"],
                "report": str(report_path),
                "report_sha256": report["report_sha256"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
