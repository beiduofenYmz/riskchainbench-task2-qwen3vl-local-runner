#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${TASK2_PYTHON:-$ROOT/.venv-task2/bin/python}"
RUN_ROOT="${1:?Usage: automation/watch_progress.sh runs/<run-id>}"

exec "$PYTHON_BIN" "$ROOT/upstream/scripts/watch_task2_trajectory_matrix.py" \
  --run "$RUN_ROOT/matrix" --watch --interval "${WATCH_INTERVAL:-60}"
