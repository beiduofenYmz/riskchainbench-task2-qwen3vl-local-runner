#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ROOT="${1:?Usage: automation/pack_results.sh runs/<run-id>}"
[[ -d "$RUN_ROOT" ]] || { echo "Run directory not found: $RUN_ROOT" >&2; exit 2; }
command -v zstd >/dev/null 2>&1 || { echo "zstd is required." >&2; exit 2; }

OUT_DIR="$ROOT/artifacts"
mkdir -p "$OUT_DIR"
NAME="$(basename "$RUN_ROOT")"
ARCHIVE="$OUT_DIR/${NAME}.tar.zst"
tar --zstd -cf "$ARCHIVE" -C "$(dirname "$RUN_ROOT")" "$NAME"
sha256sum "$ARCHIVE" >"$ARCHIVE.sha256"
echo "Wrote $ARCHIVE and $ARCHIVE.sha256"
