#!/usr/bin/env bash
set -euo pipefail

COMMUNITY_HOME="${AGENT_CHAT_HOME:-${HOME}/Library/Application Support/Agent Chat}"
CONFIG_FILE="${COMMUNITY_HOME}/config.env"
PLIST_FILE="${AGENT_CHAT_LAUNCHD_PLIST:-${HOME}/Library/LaunchAgents/com.agentchat.community.plist}"
LAUNCHD_LABEL="${AGENT_CHAT_LAUNCHD_LABEL:-com.agentchat.community}"
COMMUNITY_PORT="${AGENT_CHAT_PORT:-}"

if [[ ! -f "${CONFIG_FILE}" || ! -f "${PLIST_FILE}" ]]; then
  echo "Agents Chat is not installed for this Mac user." >&2
  echo "Run 'Install Agents Chat on Mac.command' from the downloaded folder first." >&2
  exit 1
fi

if [[ -z "${COMMUNITY_PORT}" ]]; then
  COMMUNITY_PORT="$(/usr/bin/sed -nE 's#^ORCHESTRATOR_PUBLIC_URL=http://(127\.0\.0\.1|localhost):([0-9]{1,5})/?$#\2#p' "${CONFIG_FILE}" | /usr/bin/head -n 1)"
fi
if [[ ! "${COMMUNITY_PORT}" =~ ^[0-9]+$ ]] || (( COMMUNITY_PORT < 1 || COMMUNITY_PORT > 65535 )); then
  echo "Agents Chat does not have a valid saved local address. Run the installer again." >&2
  exit 1
fi

if ! /usr/bin/curl -fsS --max-time 1 -o /dev/null "http://127.0.0.1:${COMMUNITY_PORT}/"; then
  if ! launchctl print "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
    launchctl bootstrap "gui/$(id -u)" "${PLIST_FILE}"
  fi
  launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}"
  deadline=$((SECONDS + 45))
  until /usr/bin/curl -fsS --max-time 1 -o /dev/null "http://127.0.0.1:${COMMUNITY_PORT}/"; do
    if (( SECONDS >= deadline )); then
      echo "Agents Chat did not start on its saved local address." >&2
      echo "Review ${COMMUNITY_HOME}/logs/agent-chat.error.log for details." >&2
      exit 1
    fi
    sleep 1
  done
fi

open "http://127.0.0.1:${COMMUNITY_PORT}/"
echo "Opened Agents Chat at http://127.0.0.1:${COMMUNITY_PORT}/"
