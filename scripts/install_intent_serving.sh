#!/usr/bin/env bash
set -euo pipefail

SERVING_ROOT="${INTENT_SERVING_ROOT:-/root/autodl-tmp/intent-serving}"
SERVING_ENV="${INTENT_SERVING_ENV:-/root/autodl-tmp/envs/intent-serving}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-/root/autodl-tmp/pip-cache}"

if ! command -v redis-server >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y redis-server
fi

mkdir -p "${SERVING_ROOT}/logs" "${SERVING_ROOT}/redis" "$(dirname "${SERVING_ENV}")"
if [ ! -x "${SERVING_ENV}/bin/python" ]; then
  python -m venv --copies "${SERVING_ENV}"
fi

PIP_CACHE_DIR="${PIP_CACHE_DIR}" "${SERVING_ENV}/bin/python" -m pip install \
  --index-url "${PIP_INDEX_URL}" \
  "transformers==4.56.2" \
  "vllm==0.10.2" \
  "litellm[proxy]>=1.70,<2"

install -m 0600 \
  "${PROJECT_ROOT}/config/litellm.intent.example.yaml" \
  "${SERVING_ROOT}/litellm.yaml"

"${SERVING_ENV}/bin/python" -m pip check
"${SERVING_ENV}/bin/python" - <<'PY'
import litellm
import torch
import transformers
import vllm

print("torch", torch.__version__)
print("transformers", transformers.__version__)
print("vllm", vllm.__version__)
print("litellm", getattr(litellm, "__version__", "installed"))
PY
