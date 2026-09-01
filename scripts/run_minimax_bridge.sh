#!/usr/bin/env bash
# Launches the minimax Agent Chat bridge (FastAPI + uvicorn) on the configured
# host/port. Mirrors run_claude_bridge.sh / run_hermes_bridge.sh — env-overridable
# host/port, single uvicorn process, runs the bridge module via `python3 -m`.
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON_BIN="${AGENT_CHAT_PYTHON:-${PWD}/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || { echo "Missing Agent Chat runtime: $PYTHON_BIN (run scripts/setup_v5.sh)" >&2; exit 1; }

HOST="${MINIMAX_BRIDGE_HOST:-127.0.0.1}"
PORT="${MINIMAX_BRIDGE_PORT:-55012}"

echo "Starting minimax Agent Chat bridge on http://${HOST}:${PORT}"
exec "$PYTHON_BIN" -m uvicorn scripts.minimax_agent_bridge:app --host "$HOST" --port "$PORT" --no-access-log
