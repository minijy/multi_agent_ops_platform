#!/usr/bin/env bash
set -euo pipefail

for name in intent-gateway intent-vllm; do
  screen -S "${name}" -X quit >/dev/null 2>&1 || true
done
redis-cli -h 127.0.0.1 -p 6379 shutdown save >/dev/null 2>&1 || true

echo "intent serving processes stopped"
