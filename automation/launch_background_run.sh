#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="${RUN_ID:-qwen3-vl-8b-instruct_$(date +%Y%m%d_%H%M%S)}"
UNIT="task2-qwen3vl-${RUN_ID//[^A-Za-z0-9_.-]/-}"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

if command -v systemd-run >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
  systemd-run --user --unit "$UNIT" --collect --same-dir \
    bash -lc "cd '$ROOT' && RUN_ID='$RUN_ID' ./automation/run_qwen3vl_task2.sh" \
    >/dev/null
  echo "Started systemd user service: $UNIT.service"
  echo "Follow it with: journalctl --user -fu $UNIT"
else
  nohup bash -lc "cd '$ROOT' && RUN_ID='$RUN_ID' ./automation/run_qwen3vl_task2.sh" \
    >"$LOG_DIR/${RUN_ID}.log" 2>&1 < /dev/null &
  echo "Started PID $!"
  echo "Follow it with: tail -f '$LOG_DIR/${RUN_ID}.log'"
fi
echo "Run output root: $ROOT/runs/$RUN_ID"
