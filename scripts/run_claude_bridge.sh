#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON_BIN="${AGENT_CHAT_PYTHON:-${PWD}/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || { echo "Missing Agent Chat runtime: $PYTHON_BIN (run scripts/setup_v5.sh)" >&2; exit 1; }

HOST="${CLAUDE_BRIDGE_HOST:-127.0.0.1}"
PORT="${CLAUDE_BRIDGE_PORT:-55010}"
if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]]; then
  echo "Refusing to expose the Claude bridge beyond loopback: $HOST" >&2
  exit 1
fi

echo "Starting Claude Agent Chat bridge on http://${HOST}:${PORT}"
exec "$PYTHON_BIN" -m uvicorn scripts.claude_agent_bridge:app --host "$HOST" --port "$PORT" --no-access-log
