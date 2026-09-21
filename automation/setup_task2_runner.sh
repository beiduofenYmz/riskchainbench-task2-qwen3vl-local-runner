#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.10}"
VENV_DIR="${VENV_DIR:-$ROOT/.venv-task2}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  echo "Missing $PYTHON_BIN. Task 2 is pinned to Python 3.10." >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || {
  echo "Docker is required to restore the 600 local mirrors." >&2
  exit 2
}
docker info >/dev/null 2>&1 || {
  echo "Docker is installed but not usable by the current user." >&2
  exit 2
}
if ! command -v tesseract >/dev/null 2>&1 && [[ -z "${TESSERACT_ROOT:-}" ]]; then
  echo "Tesseract is required. Install tesseract-ocr or set TESSERACT_ROOT later." >&2
  exit 2
fi
command -v zstd >/dev/null 2>&1 || {
  echo "zstd is required to restore Docker archives." >&2
  exit 2
}

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip wheel
"$VENV_DIR/bin/python" -m pip install -r "$ROOT/upstream/requirements-task2.txt" modelscope
"$VENV_DIR/bin/python" -m playwright install chromium

echo "Runner environment ready: $VENV_DIR"
