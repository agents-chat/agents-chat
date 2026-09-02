#!/usr/bin/env bash
set -euo pipefail

COMMUNITY_HOME="${AGENT_CHAT_HOME:-${HOME}/Library/Application Support/Agent Chat}"
PLIST_FILE="${AGENT_CHAT_LAUNCHD_PLIST:-${HOME}/Library/LaunchAgents/com.agentchat.community.plist}"
LAUNCHD_LABEL="${AGENT_CHAT_LAUNCHD_LABEL:-com.agentchat.community}"
USER_APP="${AGENT_CHAT_USER_APP:-${HOME}/Applications/Agents Chat.app}"
PURGE_DATA=0

if [[ "${1:-}" == "--purge-data" ]]; then
  PURGE_DATA=1
elif [[ $# -gt 0 ]]; then
  echo "Usage: uninstall_macos.sh [--purge-data]" >&2
  exit 2
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This uninstaller currently supports macOS only." >&2
  exit 1
fi

installed_process_ids() {
  /bin/ps -axo pid=,command= | /usr/bin/awk -v home="${COMMUNITY_HOME}" '
    index($0, home) && ($0 ~ /uvicorn/ || $0 ~ /run_macos\.sh/) { print $1 }
  '
}

stop_installed_processes() {
  local pid remaining=()
  while IFS= read -r pid; do
    [[ "${pid}" =~ ^[0-9]+$ ]] || continue
    kill -TERM "${pid}" >/dev/null 2>&1 || true
    remaining+=("${pid}")
  done < <(installed_process_ids)
  if (( ${#remaining[@]} == 0 )); then
    return 0
  fi
  local deadline=$((SECONDS + 5))
  while (( SECONDS < deadline )); do
    local alive=0
    for pid in "${remaining[@]}"; do
      if kill -0 "${pid}" >/dev/null 2>&1; then alive=1; break; fi
    done
    (( alive == 0 )) && return 0
    sleep 0.2
  done
  for pid in "${remaining[@]}"; do
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill -KILL "${pid}" >/dev/null 2>&1 || true
    fi
  done
}

launchctl bootout "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1 || \
  launchctl bootout "gui/$(id -u)" "${PLIST_FILE}" >/dev/null 2>&1 || true
stop_installed_processes
TRASH_ROOT="${HOME}/.Trash/Agent Chat uninstall $(date +%Y%m%d-%H%M%S)"
mkdir -p "${TRASH_ROOT}"

if [[ -f "${PLIST_FILE}" ]]; then
  mv "${PLIST_FILE}" "${TRASH_ROOT}/"
fi
if [[ -d "${USER_APP}" ]]; then
  EXISTING_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${USER_APP}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "${EXISTING_BUNDLE_ID}" == "chat.agents.community" ]]; then
    mv "${USER_APP}" "${TRASH_ROOT}/"
  else
    echo "Leaving a different application untouched at: ${USER_APP}" >&2
  fi
fi
for item in app runtime tools logs backups; do
  if [[ -e "${COMMUNITY_HOME}/${item}" ]]; then
    mv "${COMMUNITY_HOME}/${item}" "${TRASH_ROOT}/"
  fi
done
if (( PURGE_DATA )); then
  for item in config.env data; do
    if [[ -e "${COMMUNITY_HOME}/${item}" ]]; then
      mv "${COMMUNITY_HOME}/${item}" "${TRASH_ROOT}/"
    fi
  done
  rmdir "${COMMUNITY_HOME}" 2>/dev/null || true
  echo "Agent Chat and its local data were moved to: ${TRASH_ROOT}"
else
  echo "Agent Chat was removed; configuration and chat data were preserved in: ${COMMUNITY_HOME}"
  echo "Removed files are recoverable from: ${TRASH_ROOT}"
fi
