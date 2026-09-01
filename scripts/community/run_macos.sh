#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMMUNITY_HOME="${AGENT_CHAT_HOME:-${HOME}/Library/Application Support/Agent Chat}"
CONFIG_FILE="${AGENT_CHAT_CONFIG_FILE:-${COMMUNITY_HOME}/config.env}"
COMMUNITY_DATA_DIR="${AGENT_CHAT_DATA_DIR:-${COMMUNITY_HOME}/data}"
PYTHON_BIN="${AGENT_CHAT_PYTHON:-${APP_ROOT}/.venv/bin/python}"
PORT="${PORT:-}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Agent Chat's current Community Edition launcher supports macOS only." >&2
  exit 1
fi
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Missing Community Edition configuration: ${CONFIG_FILE}" >&2
  echo "Run the macOS installer or scripts/community/create_config.py first." >&2
  exit 1
fi
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Missing Agent Chat Python runtime: ${PYTHON_BIN}" >&2
  exit 1
fi
# Prefer the port saved in the private configuration (the same address the
# launchd plist and browser opener use); PORT in the environment still wins.
if [[ -z "${PORT}" ]]; then
  PORT="$(/usr/bin/sed -nE 's#^ORCHESTRATOR_PUBLIC_URL=http://(127\.0\.0\.1|localhost):([0-9]{1,5})/?$#\2#p' \
    "${CONFIG_FILE}" | /usr/bin/head -n 1)"
fi
PORT="${PORT:-8086}"
if [[ ! "${PORT}" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "PORT must be an integer between 1 and 65535." >&2
  exit 1
fi

mkdir -p "${COMMUNITY_DATA_DIR}"
chmod 700 "${COMMUNITY_HOME}" "${COMMUNITY_DATA_DIR}" 2>/dev/null || true

export AGENT_CHAT_EDITION=community
export AGENT_CHAT_ENV_FILE="${CONFIG_FILE}"
export DATA_DIR="${COMMUNITY_DATA_DIR}"
export AGENT_CHAT_DB="${COMMUNITY_DATA_DIR}/neuroblend_v5_15.sqlite3"
export CALENDARS_STORE="${COMMUNITY_DATA_DIR}/calendars.json"
export JARVIS_FOREMAN_CONFIG="${COMMUNITY_DATA_DIR}/jarvis_foreman.json"
export AGENT_WORKSPACES_ROOT="${COMMUNITY_DATA_DIR}/workspaces"
export PROJECTS_FOLDER_ROOT="${COMMUNITY_DATA_DIR}/projects"
export WORKSPACE_DISCOVER_ROOTS="${COMMUNITY_DATA_DIR}/projects"
unset STORAGE_DB

"${PYTHON_BIN}" "${APP_ROOT}/scripts/community/doctor.py" \
  --config "${CONFIG_FILE}" --data-dir "${COMMUNITY_DATA_DIR}"

echo "Starting Agent Chat Community Edition on http://127.0.0.1:${PORT}"
exec "${PYTHON_BIN}" -m uvicorn app:app --app-dir "${APP_ROOT}" \
  --host 127.0.0.1 --port "${PORT}" --timeout-graceful-shutdown 10 --no-access-log
