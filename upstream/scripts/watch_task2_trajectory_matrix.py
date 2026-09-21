#!/usr/bin/env python3
"""Print live progress for a Task 2 trajectory matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "model"


def snapshot(root: Path) -> dict[str, Any]:
    status = read_json(root / "matrix_status.json")
    rows = []
    for model, state in (status.get("models") or {}).items():
        run_dir = root / "models" / safe_slug(model) / "run"
        case_paths = list((run_dir / "cases").glob("*/case_result.json"))
        counts = {"PASS": 0, "MODEL_FAILURE": 0, "FAIL": 0}
        for path in case_paths:
            result_status = str(read_json(path).get("status") or "")
            if result_status in counts:
                counts[result_status] += 1
        terminal = counts["PASS"] + counts["MODEL_FAILURE"]
        rows.append(
            {
                "model": model,
                "state": state.get("state"),
                "round": state.get("round"),
                "terminal": terminal,
                "pass": counts["PASS"],
                "model_failure": counts["MODEL_FAILURE"],
                "system_failure_pending_retry": counts["FAIL"],
                "remaining": max(0, 600 - terminal),
                "percent": round(terminal / 6, 2),
            }
        )
    return {
        "schema_version": "riskchainbench-task2-trajectory-progress/v0.1",
        "matrix_root": str(root),
        "updated_at": status.get("updated_at"),
        "rows": rows,
        "total_terminal": sum(row["terminal"] for row in rows),
        "total_target": len(rows) * 600,
        "fixed_external_judge_status": "PENDING",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--watch", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    while True:
        print(
            json.dumps(
                snapshot(args.run),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
        if not args.watch:
            return 0
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
