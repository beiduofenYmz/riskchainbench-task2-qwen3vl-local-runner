#!/usr/bin/env bash
set -Eeuo pipefail

# Run this script in a dedicated vLLM/CUDA environment, not .venv-task2.
MODEL_ID="${QWEN_MODEL_ID:-Qwen/Qwen3-VL-8B-Instruct}"
SERVED_MODEL_NAME="${QWEN_SERVED_MODEL_NAME:-$MODEL_ID}"
HOST="${VLLM_HOST:-127.0.0.1}"
PORT="${VLLM_PORT:-8000}"
VLLM_BIN="${VLLM_BIN:-vllm}"

command -v "$VLLM_BIN" >/dev/null 2>&1 || {
  echo "vLLM is not installed in this shell. Activate the server's vLLM environment first." >&2
  exit 2
}

ARGS=(serve "$MODEL_ID" --served-model-name "$SERVED_MODEL_NAME" --host "$HOST" --port "$PORT")
if [[ -n "${VLLM_API_KEY:-}" ]]; then
  ARGS+=(--api-key "$VLLM_API_KEY")
fi
if [[ -n "${TENSOR_PARALLEL_SIZE:-}" ]]; then
  ARGS+=(--tensor-parallel-size "$TENSOR_PARALLEL_SIZE")
fi
if [[ -n "${VLLM_EXTRA_ARGS:-}" ]]; then
  # Deliberately explicit: this is recorded in the shell history/service unit.
  read -r -a EXTRA <<<"$VLLM_EXTRA_ARGS"
  ARGS+=("${EXTRA[@]}")
fi

exec "$VLLM_BIN" "${ARGS[@]}"
