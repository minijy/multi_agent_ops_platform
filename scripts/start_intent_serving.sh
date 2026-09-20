#!/usr/bin/env bash
set -euo pipefail

SERVING_ROOT="${INTENT_SERVING_ROOT:-/root/autodl-tmp/intent-serving}"
SERVING_ENV="${INTENT_SERVING_ENV:-/root/autodl-tmp/envs/intent-serving}"
MODEL_PATH="${INTENT_MODEL_PATH:-/root/autodl-tmp/models/qwen3-1.7b-intent-router-cp100-merged}"
LITELLM_CONFIG="${LITELLM_CONFIG_PATH:-${SERVING_ROOT}/litellm.yaml}"

: "${INTENT_VLLM_API_KEY:?INTENT_VLLM_API_KEY is required}"
: "${INFERENCE_GATEWAY_API_KEY:?INFERENCE_GATEWAY_API_KEY is required}"

mkdir -p "${SERVING_ROOT}/logs" "${SERVING_ROOT}/redis"

if ! command -v screen >/dev/null 2>&1; then
  echo "screen is required" >&2
  exit 1
fi
if ! command -v redis-server >/dev/null 2>&1; then
  echo "redis-server is required" >&2
  exit 1
fi
if [ ! -x "${SERVING_ENV}/bin/python" ]; then
  echo "serving environment not found: ${SERVING_ENV}" >&2
  exit 1
fi
if [ ! -d "${MODEL_PATH}" ]; then
  echo "model not found: ${MODEL_PATH}" >&2
  exit 1
fi
if [ ! -f "${LITELLM_CONFIG}" ]; then
  echo "LiteLLM config not found: ${LITELLM_CONFIG}" >&2
  exit 1
fi

screen -S intent-vllm -X quit >/dev/null 2>&1 || true
screen -S intent-gateway -X quit >/dev/null 2>&1 || true

if redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
  redis-cli -h 127.0.0.1 -p 6379 shutdown save >/dev/null
fi
redis-server \
  --bind 127.0.0.1 --port 6379 --dir "${SERVING_ROOT}/redis" \
  --appendonly yes --maxmemory-policy noeviction --daemonize yes \
  --pidfile "${SERVING_ROOT}/redis/redis.pid" \
  --logfile "${SERVING_ROOT}/logs/redis.log"

screen -dmS intent-vllm bash -lc \
  "exec '${SERVING_ENV}/bin/python' -m vllm.entrypoints.openai.api_server \
    --model '${MODEL_PATH}' \
    --served-model-name qwen3-1.7b-intent-router \
    --host 127.0.0.1 --port 8001 --dtype half \
    --gpu-memory-utilization 0.85 --max-model-len 8192 --max-num-seqs 16 \
    --enable-prefix-caching \
    >'${SERVING_ROOT}/logs/vllm.log' 2>&1"

screen -dmS intent-gateway bash -lc \
  "export LITELLM_MASTER_KEY='${INFERENCE_GATEWAY_API_KEY}' INTENT_VLLM_API_KEY='${INTENT_VLLM_API_KEY}'; \
   exec '${SERVING_ENV}/bin/litellm' --config '${LITELLM_CONFIG}' --host 0.0.0.0 --port 8200 \
   >'${SERVING_ROOT}/logs/litellm.log' 2>&1"

echo "started Redis daemon and screen sessions: intent-vllm, intent-gateway"
