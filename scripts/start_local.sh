#!/usr/bin/env bash
# 启动本地多智能体平台（控制台 + Function Calling Runtime）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

usage() {
  cat <<'EOF'
用法: scripts/start_local.sh [--restart] [--help]

  默认在前台启动 API（.env 里的 APP_HOST / APP_PORT，缺省 127.0.0.1:8100）。
  若控制面或 Session 使用 PostgreSQL，会先尝试拉起本机 Postgres 并等待就绪。

  --restart   先结束占用端口的旧进程再启动
EOF
}

RESTART=0
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    --restart)
      RESTART=1
      ;;
    *)
      echo "未知参数: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

env_get() {
  local key="$1"
  local default="${2:-}"
  local line=""
  if [[ -f "$ROOT/.env" ]]; then
    line="$(grep -E "^${key}=" "$ROOT/.env" | tail -n 1 || true)"
  fi
  if [[ -z "$line" ]]; then
    printf '%s' "$default"
    return
  fi
  local value="${line#*=}"
  value="${value%$'\r'}"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "未找到 $PYTHON" >&2
  echo "请先创建环境: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  if [[ -f "$ROOT/.env.example" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "已从 .env.example 复制 .env；模型与业务数据连接请在管理页面配置。"
  else
    echo "缺少 .env" >&2
    exit 1
  fi
fi

HOST="$(env_get APP_HOST 127.0.0.1)"
PORT="$(env_get APP_PORT 8100)"
CONTROL_BACKEND="$(env_get CONTROL_PLANE_BACKEND sqlite)"
SESSION_BACKEND="$(env_get SESSION_EVENT_BACKEND sqlite)"
POSTGRES_DSN="$(env_get POSTGRES_DSN)"

needs_postgres=0
if [[ "$CONTROL_BACKEND" == postgres || "$SESSION_BACKEND" == postgres ]]; then
  needs_postgres=1
fi

pg_target() {
  local dsn="$1"
  "$PYTHON" - "$dsn" <<'PY'
from urllib.parse import urlparse
import sys
raw = sys.argv[1].strip()
if not raw:
    print("127.0.0.1 5432")
    raise SystemExit
parsed = urlparse(raw)
host = parsed.hostname or "127.0.0.1"
port = parsed.port or 5432
print(f"{host} {port}")
PY
}

wait_postgres() {
  local dsn="$1"
  local label="$2"
  local host port
  read -r host port <<<"$(pg_target "$dsn")"
  if command -v pg_isready >/dev/null 2>&1; then
    if pg_isready -h "$host" -p "$port" >/dev/null 2>&1; then
      return 0
    fi
  else
    if "$PYTHON" - "$host" "$port" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=1):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
    then
      return 0
    fi
  fi

  echo "PostgreSQL 未就绪（$label → $host:$port），尝试拉起本机服务…"
  if [[ -d "/Applications/Postgres.app" ]]; then
    open -a Postgres >/dev/null 2>&1 || true
  elif command -v docker >/dev/null 2>&1 && [[ -f "$ROOT/docker-compose.postgres.yml" ]]; then
    docker compose -f "$ROOT/docker-compose.postgres.yml" up -d
  fi

  local i
  for i in $(seq 1 30); do
    if command -v pg_isready >/dev/null 2>&1; then
      if pg_isready -h "$host" -p "$port" >/dev/null 2>&1; then
        echo "PostgreSQL 已就绪：$host:$port"
        return 0
      fi
    else
      if "$PYTHON" - "$host" "$port" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=1):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
      then
        echo "PostgreSQL 已就绪：$host:$port"
        return 0
      fi
    fi
    sleep 1
  done
  echo "PostgreSQL 仍未监听 $host:$port，无法启动（$label）。" >&2
  echo "请打开 Postgres.app，或运行: docker compose -f docker-compose.postgres.yml up -d" >&2
  exit 1
}

if [[ "$needs_postgres" -eq 1 ]]; then
  wait_postgres "${POSTGRES_DSN:-postgresql://127.0.0.1:5432/postgres}" "POSTGRES_DSN"
fi

health_url="http://${HOST}:${PORT}/health"
openapi_url="http://${HOST}:${PORT}/openapi.json"
ui_url="http://${HOST}:${PORT}/ui"

required_routes_ready() {
  curl -sf "$openapi_url" 2>/dev/null | "$PYTHON" -c '
import json, sys
try:
    paths = json.load(sys.stdin).get("paths", {})
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)
required = {"/v1/memories", "/v1/auth/login", "/v1/access-control"}
raise SystemExit(0 if required <= set(paths) else 1)
'
}

port_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
  fi
}

if curl -sf "$health_url" >/dev/null 2>&1; then
  if required_routes_ready && [[ "$RESTART" -eq 0 ]]; then
    echo "本地多智能体已在运行。"
    echo "控制台: $ui_url"
    echo "健康检查: $health_url"
    echo "若要换新进程: scripts/start_local.sh --restart"
    exit 0
  elif ! required_routes_ready && [[ "$RESTART" -eq 0 ]]; then
    echo "端口 $PORT 上是旧版服务：健康检查可用，但缺少当前必要 API。" >&2
    echo "请运行: scripts/start_local.sh --restart" >&2
    exit 1
  fi
fi

pids="$(port_pids)"
if [[ -n "$pids" ]]; then
  if [[ "$RESTART" -eq 1 ]]; then
    echo "结束端口 $PORT 上的旧进程: $pids"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
  else
    echo "端口 $PORT 已被占用（PID: $pids），但 /health 不可用。" >&2
    echo "请检查占用进程，或使用: scripts/start_local.sh --restart" >&2
    exit 1
  fi
fi

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
echo "启动本地多智能体: $ui_url"
exec "$PYTHON" -m uvicorn ops_agent.api.app:app --host "$HOST" --port "$PORT"
