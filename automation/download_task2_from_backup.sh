#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${TASK2_PYTHON:-$ROOT/.venv-task2/bin/python}"
MODELSCOPE_BIN="${MODELSCOPE_BIN:-$ROOT/.venv-task2/bin/modelscope}"
DATASET_ID="${TASK2_BACKUP_DATASET_ID:-beiduofen/riskchainbench-task2-controlled-web-replay}"
DEST="${TASK2_RELEASE_DIR:-$ROOT/data/task2}"

[[ -x "$PYTHON_BIN" && -x "$MODELSCOPE_BIN" ]] || {
  echo "Run automation/setup_task2_runner.sh first." >&2
  exit 2
}
if [[ -d "$DEST" ]] && [[ -n "$(find "$DEST" -mindepth 1 -print -quit)" ]]; then
  echo "Refusing to merge into existing data: $DEST" >&2
  echo "Use a fresh directory or remove the old incomplete download first." >&2
  exit 2
fi

"$MODELSCOPE_BIN" whoami >/dev/null
mkdir -p "$(dirname "$DEST")"
"$MODELSCOPE_BIN" download "$DATASET_ID" \
  --repo-type dataset \
  --local-dir "$DEST" \
  --max-workers "${DOWNLOAD_WORKERS:-4}"

# ModelScope creates these three repository starter files for new datasets.
# They are not part of the frozen Task 2 payload and would fail its verifier.
rm -f "$DEST/.gitattributes" "$DEST/README.md" "$DEST/dataset_infos.json" "$DEST/.ms_upload_cache"

mkdir -p "$ROOT/runs/preflight"
"$PYTHON_BIN" "$ROOT/upstream/scripts/verify_task2_release.py" \
  --task2-release "$DEST" \
  --level metadata \
  --out "$ROOT/runs/preflight/release-metadata-after-download.json"

echo "Downloaded and metadata-verified Task 2 release: $DEST"
