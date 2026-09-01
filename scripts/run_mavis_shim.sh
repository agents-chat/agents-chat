#!/usr/bin/env bash
# Launches the mavis OpenAI shim (FastAPI + uvicorn) on the configured
# host/port. Mirrors run_minimax_bridge.sh — env-overridable host/port,
# single uvicorn process, runs the shim module via `python3 -m`.
#
# This is the OpenAI-shaped front door for Agent Zero containers (Lead,
# Sales) that need to talk to the Mavis daemon through the same minimax
# bridge the Agent Chat `@minimax` channel uses. Host/port default to
# 127.0.0.1:55016; Docker Desktop's host gateway can reach the host loopback
# without exposing this compatibility API to the LAN. Override with
# MAVIS_SHIM_HOST / MAVIS_SHIM_PORT only for a deliberate non-Desktop topology.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${AGENT_CHAT_PYTHON:-${PWD}/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || { echo "Missing Agent Chat runtime: $PYTHON_BIN (run scripts/setup_v5.sh)" >&2; exit 1; }

HOST="${MAVIS_SHIM_HOST:-127.0.0.1}"
PORT="${MAVIS_SHIM_PORT:-55016}"

echo "Starting mavis OpenAI shim on http://${HOST}:${PORT}"
exec "$PYTHON_BIN" -m uvicorn scripts.mavis_openai_shim:app --host "$HOST" --port "$PORT" --no-access-log
