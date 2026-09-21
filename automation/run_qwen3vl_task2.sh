#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${TASK2_PYTHON:-$ROOT/.venv-task2/bin/python}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
DATASET_DIR="${TASK2_RELEASE_DIR:-$ROOT/data/task2}"
RUNTIME_ROOT="${TASK2_RUNTIME_ROOT:-$ROOT/runtime}"
MODEL="${QWEN_MODEL:-Qwen/Qwen3-VL-8B-Instruct}"
RUN_ID="${RUN_ID:-qwen3-vl-8b-instruct_$(date +%Y%m%d_%H%M%S)}"
RUN_ROOT="${RUN_ROOT:-$ROOT/runs/$RUN_ID}"
TRANSPORT="openai-compatible"
BASE_URL_ENV="LOCAL_OPENAI_BASE_URL"
API_KEY_ENV="LOCAL_OPENAI_API_KEY"
CONTRACT_SHA="52b2e3e4bbca46fe6f007bdc7b553fa0b2f1f923c82a5b775faef09350e9ff42"
SMOKE_CASE="CASEd150dbe314a6c11bc1fa"

[[ -x "$PYTHON_BIN" ]] || { echo "Run automation/setup_task2_runner.sh first." >&2; exit 2; }
[[ -f "$ENV_FILE" ]] || { echo "Create a 0600 .env from .env.example first." >&2; exit 2; }
if [[ "$(stat -c '%a' "$ENV_FILE")" != "600" ]]; then
  echo "Refusing to load a non-0600 environment file: $ENV_FILE" >&2
  exit 2
fi
set -a
source "$ENV_FILE"
set +a
: "${LOCAL_OPENAI_BASE_URL:?Set LOCAL_OPENAI_BASE_URL in .env}"
: "${LOCAL_OPENAI_API_KEY:?Set LOCAL_OPENAI_API_KEY in .env}"
[[ -f "$DATASET_DIR/task2_contract.json" ]] || { echo "Task 2 data is missing. Run automation/download_task2_from_backup.sh." >&2; exit 2; }

TESSERACT_ARGS=()
if [[ -n "${TESSERACT_ROOT:-}" ]]; then
  TESSERACT_ARGS+=(--tesseract-root "$TESSERACT_ROOT")
fi
mkdir -p "$RUN_ROOT/preflight"

"$PYTHON_BIN" - "$DATASET_DIR" "$RUN_ROOT/preflight/data-source.json" <<'PY'
import json
import sys
from pathlib import Path

release = Path(sys.argv[1])
out = Path(sys.argv[2])
contract = json.loads((release / "task2_contract.json").read_text(encoding="utf-8"))
manifest = json.loads((release / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
out.write_text(
    json.dumps(
        {
            "source_dataset": "beiduofen/riskchainbench-task2-controlled-web-replay",
            "source_kind": "private_byte_verified_backup",
            "task2_contract_sha256": contract.get("contract_sha256"),
            "release_sha256": manifest.get("release_sha256"),
            "release_file_count": manifest.get("file_count"),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

"$PYTHON_BIN" "$ROOT/upstream/scripts/verify_task2_release.py" \
  --task2-release "$DATASET_DIR" --level metadata \
  --out "$RUN_ROOT/preflight/release-metadata.json"
"$PYTHON_BIN" "$ROOT/upstream/scripts/verify_task2_release.py" \
  --task2-release "$DATASET_DIR" --level full \
  --out "$RUN_ROOT/preflight/release-full.json"

"$PYTHON_BIN" "$ROOT/upstream/scripts/materialize_task2_runtime.py" \
  --task2-release "$DATASET_DIR" --runtime-root "$RUNTIME_ROOT" \
  --case-ref "$SMOKE_CASE" --workers 1 \
  --report "$RUN_ROOT/preflight/materialize-smoke.json"
"$PYTHON_BIN" "$ROOT/upstream/scripts/materialize_task2_runtime.py" \
  --task2-release "$DATASET_DIR" --runtime-root "$RUNTIME_ROOT" \
  --workers "${MATERIALIZE_WORKERS:-4}" --replace \
  --report "$RUN_ROOT/preflight/materialize-600.json"

"$PYTHON_BIN" "$ROOT/upstream/scripts/probe_multimodal_routes.py" \
  --transport "$TRANSPORT" --base-url-env "$BASE_URL_ENV" --api-key-env "$API_KEY_ENV" \
  --models "$MODEL" --priority "$MODEL" --probe-count 2 \
  --out "$RUN_ROOT/preflight/routes.json"

run_and_validate() {
  local name="$1"; shift
  "$PYTHON_BIN" "$ROOT/upstream/scripts/run_task2_autonomous_mllm_batch.py" \
    --task2-release "$DATASET_DIR" --route-probe "$RUN_ROOT/preflight/routes.json" \
    --env-file "$ENV_FILE" --transport "$TRANSPORT" \
    --base-url-env "$BASE_URL_ENV" --api-key-env "$API_KEY_ENV" \
    "${TESSERACT_ARGS[@]}" --runtime-root "$RUNTIME_ROOT" \
    --runtime-materialization-report "$RUN_ROOT/preflight/materialize-600.json" \
    --model "$MODEL" --workers "${CASE_WORKERS:-1}" --out "$RUN_ROOT/$name" "$@"
  "$PYTHON_BIN" "$ROOT/upstream/scripts/validate_task2_autonomous_mllm_batch.py" \
    --run "$RUN_ROOT/$name" --expected-transport "$TRANSPORT" \
    --expected-task2-contract-sha256 "$CONTRACT_SHA" \
    --out "$RUN_ROOT/$name/validation.json"
}

run_and_validate smoke-1 --case-ref "$SMOKE_CASE"
run_and_validate smoke-10 --limit 10

"$PYTHON_BIN" "$ROOT/upstream/scripts/run_task2_trajectory_matrix.py" \
  --models "$MODEL" --transport "$TRANSPORT" \
  --base-url-env "$BASE_URL_ENV" --api-key-env "$API_KEY_ENV" \
  --env-file "$ENV_FILE" "${TESSERACT_ARGS[@]}" \
  --task2-release "$DATASET_DIR" --route-probe "$RUN_ROOT/preflight/routes.json" \
  --runtime-root "$RUNTIME_ROOT" \
  --runtime-materialization-report "$RUN_ROOT/preflight/materialize-600.json" \
  --parallel-models 1 --case-workers "${CASE_WORKERS:-1}" \
  --max-system-retry-rounds 2 --out "$RUN_ROOT/matrix"

echo "Task 2 Qwen3-VL run completed: $RUN_ROOT"
