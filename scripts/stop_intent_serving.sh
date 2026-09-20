#!/usr/bin/env bash
set -euo pipefail

for name in intent-gateway intent-vllm intent-redis; do
  screen -S "${name}" -X quit >/dev/null 2>&1 || true
done

echo "intent serving processes stopped"
