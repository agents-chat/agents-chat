#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMMUNITY_HOME="${AGENT_CHAT_HOME:-${HOME}/Library/Application Support/Agent Chat}"
INSTALL_ROOT="${COMMUNITY_HOME}/app"
RUNTIME_ROOT="${COMMUNITY_HOME}/runtime"
RUNTIME_VENV="${RUNTIME_ROOT}/venv"
RUNTIME_STAMP="${RUNTIME_ROOT}/requirements.sha256"
TOOLS_ROOT="${COMMUNITY_HOME}/tools"
BACKUP_ROOT="${COMMUNITY_HOME}/backups"
CONFIG_FILE="${COMMUNITY_HOME}/config.env"
PLIST_FILE="${AGENT_CHAT_LAUNCHD_PLIST:-${HOME}/Library/LaunchAgents/com.agentchat.community.plist}"
LAUNCHD_LABEL="${AGENT_CHAT_LAUNCHD_LABEL:-com.agentchat.community}"
COMMUNITY_PORT="${AGENT_CHAT_PORT:-}"
USER_APP="${AGENT_CHAT_USER_APP:-${HOME}/Applications/Agents Chat.app}"
OPEN_BROWSER="${AGENT_CHAT_OPEN_BROWSER:-1}"
UV_VERSION="0.11.32"
UV_ARM64_ARCHIVE_SHA256="ed336d0ba49db8ef89b2b41fffa372ce63bd032f22a56f001c265891aec32829"
UV_ARM64_BINARY_SHA256="3736babdf838efb1c04ca690dd6ff3458a23cdf98e0e08b1f721eac4779e272d"
UV_X86_64_ARCHIVE_SHA256="77f5ca26c0de20e992a3677a174fe1121ee25c36f9b1434a863f75bf077a05eb"
UV_X86_64_BINARY_SHA256="07653d52bc37cecadab5f968aebc27dd5cd0a485c20d9adbff147ba268698bae"
DISPLAY_NAME="Owner"
ADMIN_EMAIL="admin@localhost"
CHECK_ONLY=0
NO_START=0
PORT_WAS_PROVIDED=0
[[ -n "${COMMUNITY_PORT}" ]] && PORT_WAS_PROVIDED=1

usage() {
  echo "Usage: install_macos.sh [--check-only] [--no-start] [--display-name NAME] [--admin-email EMAIL] [--port PORT]"
}

while (( $# )); do
  case "$1" in
    --check-only) CHECK_ONLY=1 ;;
    --no-start) NO_START=1 ;;
    --display-name)
      shift
      [[ $# -gt 0 ]] || { echo "--display-name requires a value" >&2; exit 2; }
      DISPLAY_NAME="$1"
      ;;
    --admin-email)
      shift
      [[ $# -gt 0 ]] || { echo "--admin-email requires a value" >&2; exit 2; }
      ADMIN_EMAIL="$1"
      ;;
    --port)
      shift
      [[ $# -gt 0 ]] || { echo "--port requires a value" >&2; exit 2; }
      COMMUNITY_PORT="$1"
      PORT_WAS_PROVIDED=1
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

failures=0
pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1" >&2; failures=$((failures + 1)); }

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

configured_port() {
  [[ -f "${CONFIG_FILE}" ]] || return 0
  /usr/bin/sed -nE 's#^ORCHESTRATOR_PUBLIC_URL=http://(127\.0\.0\.1|localhost):([0-9]{1,5})/?$#\2#p' \
    "${CONFIG_FILE}" | /usr/bin/head -n 1
}

port_available() {
  ! /usr/bin/nc -z -G 1 127.0.0.1 "$1" >/dev/null 2>&1
}

find_available_port() {
  local candidate
  for candidate in {8086..8096} {18086..18096}; do
    if port_available "${candidate}"; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

[[ "$(uname -s)" == "Darwin" ]] && pass "macOS detected" || fail "this installer currently supports macOS only"
# Installing is only ever allowed from a cleared release package. A development
# checkout may run the read-only preflight (AGENT_CHAT_ALLOW_DEV_INSTALL=1 with
# --check-only) so contributors and CI can validate this script, but it must
# never copy an unreviewed source tree into the install root: only the release
# build's allow-list decides what a package contains.
if [[ -f "${SOURCE_ROOT}/.community-release.json" ]]; then
  pass "release package marker present"
elif (( CHECK_ONLY )) && [[ "${AGENT_CHAT_ALLOW_DEV_INSTALL:-0}" == "1" ]]; then
  pass "development tree: preflight only, installing from it is refused"
else
  fail "not a cleared Community Edition release package"
fi
[[ -f "${SOURCE_ROOT}/requirements.lock" ]] && pass "locked Python dependencies present" || fail "requirements.lock is missing"
[[ -f "${SOURCE_ROOT}/config.community.env" ]] && pass "portable configuration template present" || fail "config.community.env is missing"
[[ -x /usr/bin/rsync || -x "$(command -v rsync 2>/dev/null || true)" ]] && pass "rsync available" || fail "rsync is required"
[[ -x /usr/bin/curl || -x "$(command -v curl 2>/dev/null || true)" ]] && pass "curl available" || fail "curl is required"
[[ -x /usr/bin/nc ]] && pass "local port detector available" || fail "nc is required"
[[ "${LAUNCHD_LABEL}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{2,127}$ ]] && pass "launchd label is valid" || fail "AGENT_CHAT_LAUNCHD_LABEL is invalid"

CONFIGURED_PORT="$(configured_port)"
if [[ -n "${CONFIGURED_PORT}" ]]; then
  if [[ ! "${CONFIGURED_PORT}" =~ ^[0-9]+$ ]] || (( CONFIGURED_PORT < 1 || CONFIGURED_PORT > 65535 )); then
    fail "the existing configuration has an invalid local address"
  elif (( PORT_WAS_PROVIDED )) && [[ "${COMMUNITY_PORT}" != "${CONFIGURED_PORT}" ]]; then
    fail "this installation already uses local port ${CONFIGURED_PORT}; reinstall without --port"
  else
    COMMUNITY_PORT="${CONFIGURED_PORT}"
    pass "existing installation will keep local port ${COMMUNITY_PORT}"
  fi
elif (( PORT_WAS_PROVIDED )); then
  if [[ "${COMMUNITY_PORT}" =~ ^[0-9]+$ ]] && (( COMMUNITY_PORT >= 1 && COMMUNITY_PORT <= 65535 )); then
    if port_available "${COMMUNITY_PORT}"; then
      pass "requested private local port ${COMMUNITY_PORT} is available"
    else
      fail "requested local port ${COMMUNITY_PORT} is already in use"
    fi
  else
    fail "--port and AGENT_CHAT_PORT must be between 1 and 65535"
  fi
else
  COMMUNITY_PORT="$(find_available_port || true)"
  if [[ -n "${COMMUNITY_PORT}" ]]; then
    pass "automatically selected private local port ${COMMUNITY_PORT}"
  else
    fail "could not find a free private local port"
  fi
fi

if (( failures )); then
  exit 1
fi
if (( CHECK_ONLY )); then
  echo "macOS installer preflight passed; no files were changed."
  exit 0
fi

mkdir -p "${COMMUNITY_HOME}" "${RUNTIME_ROOT}" "${TOOLS_ROOT}" "${BACKUP_ROOT}"
chmod 700 "${COMMUNITY_HOME}"

# Second-resolution names collide when the installer is run twice in the same
# second, and a colliding "mv" silently nests one backup inside the other, so
# every backup name gets process entropy plus a uniqueness check.
backup_stamp() {
  local prefix="$1" suffix="${2:-}" base stamp counter=1
  base="$(date +%Y%m%d-%H%M%S)-$$"
  stamp="${base}"
  while [[ -e "${BACKUP_ROOT}/${prefix}-${stamp}${suffix}" ]]; do
    stamp="${base}-${counter}"
    counter=$((counter + 1))
  done
  printf '%s' "${stamp}"
}

STAGING_ROOT="${COMMUNITY_HOME}/.app-staging-$$"
if [[ -e "${STAGING_ROOT}" ]]; then
  fail "staging path already exists: ${STAGING_ROOT}"
  exit 1
fi
mkdir -p "${STAGING_ROOT}"
cleanup_staging() {
  if [[ -d "${STAGING_ROOT}" ]]; then
    mv "${STAGING_ROOT}" "${BACKUP_ROOT}/incomplete-install-$(backup_stamp incomplete-install)" 2>/dev/null || true
  fi
}
trap cleanup_staging EXIT

# The source is always a cleared release package (the marker gate above refuses
# anything else), so these excludes are not a private-content filter -- what a
# package may contain is decided by the release build's allow-list manifest.
# They keep the *user's own* runtime state out of the copy when someone runs the
# app from the unpacked folder and then reinstalls over it: caches, virtualenvs,
# databases, threads, attachments, credentials and other local-only state.
RSYNC_BIN="$(command -v rsync)"
"${RSYNC_BIN}" -a \
  --exclude '.git/' --exclude '.env' --exclude '.env.*' --exclude '.venv/' \
  --exclude 'node_modules/' --exclude 'data/' --exclude 'tests/' --exclude '__pycache__/' \
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude '.claude/' \
  --exclude '*.pyc' --exclude '*.sqlite3' --exclude '*.sqlite3-*' --exclude '*.sqlite3.*' \
  --exclude 'sessions.json' --exclude 'chats_index.json' --exclude 'custom_agents.json' \
  --exclude 'workspace_bindings.json' --exclude 'calendars.json' --exclude 'threads/' \
  --exclude 'attachments/' --exclude 'artifacts_store/' --exclude 'support_tickets/' \
  --exclude 'voice_cache/' --exclude 'storage_backups/' --exclude 'cutover_backups/' \
  --exclude 'backup/' --exclude '*_bridge/' --exclude 'agents/' --exclude 'episodes/' \
  --exclude 'freewill_briefs/' --exclude 'push_state/' --exclude 'custom_voices/' \
  --exclude 'config.example.env' --exclude 'CHANGELOG.md' \
  "${SOURCE_ROOT}/" "${STAGING_ROOT}/"

if find "${STAGING_ROOT}" \( -name '.env' -o -name '*.sqlite3' -o -name '*.sqlite3-*' -o -name '*.sqlite3.*' \) -print | grep -q .; then
  fail "staging unexpectedly contains runtime configuration or databases"
  exit 1
fi

# The owner already approved this cleared release by opening its installer, but
# browsers attach quarantine recursively to every file in the ZIP. Verify the
# exact bundled helper before removing quarantine from that helper alone, so
# LaunchServices can open its scoped Accessibility identity without presenting
# a second unrelated Gatekeeper block. Never clear quarantine broadly.
PERPLEXITY_HELPER_APP="${STAGING_ROOT}/assets/macos/Agent Chat Perplexity Driver.app"
if [[ -d "${PERPLEXITY_HELPER_APP}" ]]; then
  if /usr/bin/codesign --verify --deep --strict "${PERPLEXITY_HELPER_APP}"; then
    /usr/bin/xattr -dr com.apple.quarantine "${PERPLEXITY_HELPER_APP}" 2>/dev/null || true
    pass "bundled Perplexity helper signature verified"
  else
    fail "bundled Perplexity helper signature is invalid"
    exit 1
  fi
fi

# From here until the runtime, config, and launchers are fully built, a failure
# must not strand the user between versions: keep the failed tree for
# diagnosis, and put the previous working app back so an interrupted upgrade
# (offline uv download, hash mismatch, transient error) stays recoverable.
# Every value the rollback needs is declared before the trap is armed, and the
# trap is armed before the running service is stopped and the app directory is
# swapped, so there is no unprotected window in which the previous app can be
# lost or left stopped.
PREVIOUS_APP_BACKUP=""
PREVIOUS_VENV_BACKUP=""
PREVIOUS_STAMP_BACKUP=""
PREVIOUS_USER_APP_BACKUP=""
PREVIOUS_PLIST_BACKUP=""
RUNTIME_REBUILT=0
CONFIG_CREATED_THIS_RUN=0
PLIST_CREATED_THIS_RUN=0
INSTALL_COMPLETE=0
INSTALL_INTERRUPTED=0

rollback_install() {
  local status=$?
  local restored_app=0
  local restored_venv=0
  # Fail safe: roll back unless the install explicitly finished. Do NOT trust
  # $? here -- on macOS bash the EXIT trap sees 0 after SIGHUP (which is what a
  # Finder-launched .command gets when its Terminal window is closed) and after
  # SIGTERM, so a guard keyed on the status would silently abandon a half-
  # upgraded tree. Past this point the only zero-exit path sets INSTALL_COMPLETE;
  # --check-only returns long before the trap is armed.
  (( INSTALL_COMPLETE )) && return 0
  cleanup_staging
  if [[ -n "${PREVIOUS_APP_BACKUP}" ]]; then
    echo "Installation failed; restoring the previous Agents Chat app files..." >&2
  else
    echo "Installation failed; cleaning up the partial first-time installation..." >&2
  fi

  if [[ -d "${INSTALL_ROOT}" ]]; then
    mv "${INSTALL_ROOT}" "${BACKUP_ROOT}/failed-install-$(backup_stamp failed-install)" 2>/dev/null || true
  fi
  if [[ -n "${PREVIOUS_APP_BACKUP}" && -d "${PREVIOUS_APP_BACKUP}" && ! -e "${INSTALL_ROOT}" ]]; then
    if mv "${PREVIOUS_APP_BACKUP}" "${INSTALL_ROOT}" 2>/dev/null; then
      restored_app=1
    fi
  fi

  # A restored app tree cannot start without the private runtime it was built
  # against, so the virtual environment and its dependency stamp roll back with
  # it; otherwise the "restored" app fails to import uvicorn and the opener
  # simply times out.
  if (( RUNTIME_REBUILT )); then
    if [[ -n "${PREVIOUS_VENV_BACKUP}" && -d "${PREVIOUS_VENV_BACKUP}" ]]; then
      if [[ -d "${RUNTIME_VENV}" ]]; then
        mv "${RUNTIME_VENV}" "${BACKUP_ROOT}/failed-venv-$(backup_stamp failed-venv)" 2>/dev/null || true
      fi
      if [[ ! -e "${RUNTIME_VENV}" ]] && mv "${PREVIOUS_VENV_BACKUP}" "${RUNTIME_VENV}" 2>/dev/null; then
        restored_venv=1
      fi
    fi
    if (( restored_venv )) && [[ -n "${PREVIOUS_STAMP_BACKUP}" && -f "${PREVIOUS_STAMP_BACKUP}" ]]; then
      cp "${PREVIOUS_STAMP_BACKUP}" "${RUNTIME_STAMP}" 2>/dev/null || true
    else
      # No matching stamp to go back to: drop it so the next run rebuilds
      # instead of trusting a dependency set that was never fully installed.
      rm -f "${RUNTIME_STAMP}" 2>/dev/null || true
    fi
  fi

  # Without the launcher app the restored version has no obvious way to start.
  if [[ -n "${PREVIOUS_USER_APP_BACKUP}" && -e "${PREVIOUS_USER_APP_BACKUP}" ]]; then
    if [[ -e "${USER_APP}" ]]; then
      mv "${USER_APP}" "${BACKUP_ROOT}/failed-launcher-$(backup_stamp failed-launcher .app).app" 2>/dev/null || true
    fi
    if [[ ! -e "${USER_APP}" ]]; then
      mv "${PREVIOUS_USER_APP_BACKUP}" "${USER_APP}" 2>/dev/null || true
    fi
  fi

  # The failed run may already have written a launchd job description for a
  # version that is no longer installed, so the previous one goes back before
  # anything is bootstrapped. Today's generated payload only depends on stable
  # paths, but a release that renames the launcher script or adds an
  # environment variable would otherwise start the restored app with the new
  # version's job definition.
  if [[ -n "${PREVIOUS_PLIST_BACKUP}" && -f "${PREVIOUS_PLIST_BACKUP}" ]]; then
    cp "${PREVIOUS_PLIST_BACKUP}" "${PLIST_FILE}" 2>/dev/null || true
  fi

  # A failed first-time install must not leave the machine half-claimed: a
  # config.env makes the next run refuse a different --port ("this installation
  # already uses local port ..."), and a startup entry pointing at an app root
  # that does not exist would be loaded at every login. Only what this run
  # created is retired, so an existing user's configuration is never touched,
  # and nothing is deleted -- the copies stay in the backups directory.
  if [[ -z "${PREVIOUS_APP_BACKUP}" ]]; then
    if (( PLIST_CREATED_THIS_RUN )) && [[ -f "${PLIST_FILE}" ]]; then
      launchctl bootout "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
      mv "${PLIST_FILE}" "${BACKUP_ROOT}/failed-plist-$(backup_stamp failed-plist .plist).plist" 2>/dev/null || true
    fi
    if (( CONFIG_CREATED_THIS_RUN )) && [[ -f "${CONFIG_FILE}" ]]; then
      mv "${CONFIG_FILE}" "${BACKUP_ROOT}/failed-config-$(backup_stamp failed-config .env).env" 2>/dev/null || true
    fi
  fi

  if (( restored_app )); then
    echo "The previous app files were restored. Run the installer again to retry the upgrade." >&2
    # The upgrade booted the service out; bring the restored version back up
    # from the restored plist -- but only if it can actually run. The plist
    # carries KeepAlive with SuccessfulExit=false and a 10s ThrottleInterval, so
    # bootstrapping a job whose runtime could not be restored would respawn a
    # failing server every ten seconds forever instead of leaving it cleanly
    # stopped.
    if [[ -f "${PLIST_FILE}" ]] \
      && [[ -x "${RUNTIME_VENV}/bin/python" ]] \
      && "${RUNTIME_VENV}/bin/python" -c 'import uvicorn' >/dev/null 2>&1; then
      launchctl bootstrap "gui/$(id -u)" "${PLIST_FILE}" >/dev/null 2>&1 || true
    else
      echo "The runtime could not be restored, so Agents Chat was left stopped." >&2
    fi
  elif [[ -z "${PREVIOUS_APP_BACKUP}" ]]; then
    echo "This was a first-time installation, so there was no previous version to restore." >&2
    echo "Fix the problem reported above and run the installer again." >&2
  else
    echo "The previous app files were left in ${PREVIOUS_APP_BACKUP} and could not be restored automatically." >&2
  fi
  return 0
}

# rollback_install is fail-safe on its own (it rolls back unless INSTALL_COMPLETE
# is set), but these traps still give each signal a conventional exit status
# instead of the misleading 0 macOS bash would otherwise report. HUP matters
# most: the shipped launcher is a .command, so closing the Terminal window
# during an upgrade sends SIGHUP to the whole process group.
trap 'INSTALL_INTERRUPTED=1; exit 129' HUP
trap 'INSTALL_INTERRUPTED=1; exit 131' QUIT
trap 'INSTALL_INTERRUPTED=1; exit 143' TERM
trap 'INSTALL_INTERRUPTED=1; exit 130' INT
trap rollback_install EXIT

# The running service is stopped only now, inside the window the rollback trap
# protects: a failure while the new tree was still being staged (a failing
# rsync, the leak scan, the staging-path check) therefore leaves the previous
# version running untouched instead of stopping it with nothing to restore.
# PREVIOUS_APP_BACKUP is recorded only after the move actually succeeded, so
# the rollback never chases a backup path that was never created.
if [[ -d "${INSTALL_ROOT}" ]]; then
  echo "Stopping the existing Agents Chat process for a safe upgrade..."
  launchctl bootout "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1 || \
    launchctl bootout "gui/$(id -u)" "${PLIST_FILE}" >/dev/null 2>&1 || true
  stop_installed_processes
  APP_BACKUP_TARGET="${BACKUP_ROOT}/app-$(backup_stamp app)"
  mv "${INSTALL_ROOT}" "${APP_BACKUP_TARGET}"
  PREVIOUS_APP_BACKUP="${APP_BACKUP_TARGET}"
fi
mv "${STAGING_ROOT}" "${INSTALL_ROOT}"

case "$(uname -m)" in
  arm64)
    UV_TARGET="aarch64-apple-darwin"
    UV_ARCHIVE_SHA256="${UV_ARM64_ARCHIVE_SHA256}"
    UV_BINARY_SHA256="${UV_ARM64_BINARY_SHA256}"
    ;;
  x86_64)
    UV_TARGET="x86_64-apple-darwin"
    UV_ARCHIVE_SHA256="${UV_X86_64_ARCHIVE_SHA256}"
    UV_BINARY_SHA256="${UV_X86_64_BINARY_SHA256}"
    ;;
  *)
    echo "uv has no pinned Agent Chat build for this Mac architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

UV_BIN="${TOOLS_ROOT}/uv"
UV_BIN_HASH=""
UV_VERSION_OUTPUT=""
if [[ -x "${UV_BIN}" ]]; then
  UV_BIN_HASH="$(/usr/bin/shasum -a 256 "${UV_BIN}" | /usr/bin/awk '{print $1}')"
  UV_VERSION_OUTPUT="$("${UV_BIN}" --version 2>/dev/null || true)"
fi

if [[ "${UV_BIN_HASH}" != "${UV_BINARY_SHA256}" ]] || \
   [[ "${UV_VERSION_OUTPUT}" != "uv ${UV_VERSION}" && "${UV_VERSION_OUTPUT}" != "uv ${UV_VERSION} "* ]]; then
  echo "Installing verified uv ${UV_VERSION} into Agent Chat's private tools directory..."
  UV_ARCHIVE="${TOOLS_ROOT}/uv-${UV_TARGET}-$$.download.tar.gz"
  UV_STAGING="${TOOLS_ROOT}/.uv-staging-$$"
  UV_EXTRACTED="${UV_STAGING}/uv-${UV_TARGET}"
  if [[ -e "${UV_STAGING}" ]]; then
    echo "uv staging path already exists: ${UV_STAGING}" >&2
    exit 1
  fi

  UV_INSTALL_OK=0
  if /usr/bin/curl --proto '=https' --tlsv1.2 --fail --location --retry 3 \
      --output "${UV_ARCHIVE}" \
      "https://releases.astral.sh/github/uv/releases/download/${UV_VERSION}/uv-${UV_TARGET}.tar.gz" && \
     [[ "$(/usr/bin/shasum -a 256 "${UV_ARCHIVE}" | /usr/bin/awk '{print $1}')" == "${UV_ARCHIVE_SHA256}" ]] && \
     mkdir "${UV_STAGING}" && \
     /usr/bin/tar -xzf "${UV_ARCHIVE}" -C "${UV_STAGING}" && \
     [[ -x "${UV_EXTRACTED}/uv" && -x "${UV_EXTRACTED}/uvx" ]] && \
     [[ "$(/usr/bin/shasum -a 256 "${UV_EXTRACTED}/uv" | /usr/bin/awk '{print $1}')" == "${UV_BINARY_SHA256}" ]]; then
    UV_INSTALL_OK=1
  fi

  /bin/rm -f "${UV_ARCHIVE}" 2>/dev/null || true
  if (( ! UV_INSTALL_OK )); then
    if [[ -d "${UV_STAGING}" ]]; then
      mv "${UV_STAGING}" "${BACKUP_ROOT}/incomplete-uv-$(backup_stamp incomplete-uv)" 2>/dev/null || true
    fi
    echo "uv download, archive hash, extraction, or binary hash verification failed" >&2
    exit 1
  fi

  if [[ -e "${TOOLS_ROOT}/uvx" ]]; then
    mv "${TOOLS_ROOT}/uvx" "${BACKUP_ROOT}/uvx-$(backup_stamp uvx)"
  fi
  if [[ -e "${UV_BIN}" ]]; then
    mv "${UV_BIN}" "${BACKUP_ROOT}/uv-$(backup_stamp uv)"
  fi
  mv "${UV_EXTRACTED}/uvx" "${TOOLS_ROOT}/uvx"
  mv "${UV_EXTRACTED}/uv" "${UV_BIN}"
  rmdir "${UV_EXTRACTED}" "${UV_STAGING}"
fi

[[ -x "${UV_BIN}" ]] || { echo "uv installation failed" >&2; exit 1; }
[[ "$(/usr/bin/shasum -a 256 "${UV_BIN}" | /usr/bin/awk '{print $1}')" == "${UV_BINARY_SHA256}" ]] || {
  echo "uv binary failed its pinned SHA-256 check" >&2
  exit 1
}
UV_VERSION_OUTPUT="$("${UV_BIN}" --version 2>/dev/null || true)"
[[ "${UV_VERSION_OUTPUT}" == "uv ${UV_VERSION}" || "${UV_VERSION_OUTPUT}" == "uv ${UV_VERSION} "* ]] || {
  echo "uv binary failed its pinned version check" >&2
  exit 1
}

REQUIREMENTS_FILE="${INSTALL_ROOT}/requirements.lock"
REQUIREMENTS_HASH="$(/usr/bin/shasum -a 256 "${REQUIREMENTS_FILE}" | /usr/bin/awk '{print $1}')"
RUNTIME_READY=0

# Reuse the private Python runtime only when it matches the exact locked
# dependency set and can import every direct runtime dependency. This keeps a
# normal app update quick and small, while a stale or damaged environment still
# follows the safe backup-and-rebuild path.
if [[ -x "${RUNTIME_VENV}/bin/python" && -f "${RUNTIME_STAMP}" ]]; then
  SAVED_REQUIREMENTS_HASH="$(/usr/bin/tr -d '[:space:]' < "${RUNTIME_STAMP}")"
  if [[ "${SAVED_REQUIREMENTS_HASH}" == "${REQUIREMENTS_HASH}" ]] && \
      "${RUNTIME_VENV}/bin/python" -c \
        'import fastapi, uvicorn, httpx, cryptography, pypdf, imageio_ffmpeg, jinja2, multipart, yaml' \
        >/dev/null 2>&1; then
    RUNTIME_READY=1
    pass "existing private Python runtime matches the locked dependencies"
  fi
fi

if (( ! RUNTIME_READY )); then
  # Remember what is being replaced so a failed rebuild can hand the previous
  # working runtime (and its matching stamp) back to the restored app tree.
  RUNTIME_REBUILT=1
  if [[ -d "${RUNTIME_VENV}" ]]; then
    TIMESTAMP="$(backup_stamp venv)"
    PREVIOUS_VENV_BACKUP="${BACKUP_ROOT}/venv-${TIMESTAMP}"
    mv "${RUNTIME_VENV}" "${BACKUP_ROOT}/venv-${TIMESTAMP}"
  fi
  if [[ -f "${RUNTIME_STAMP}" ]]; then
    PREVIOUS_STAMP_BACKUP="${BACKUP_ROOT}/requirements-stamp-$(backup_stamp requirements-stamp)"
    cp "${RUNTIME_STAMP}" "${PREVIOUS_STAMP_BACKUP}"
  fi
  "${UV_BIN}" venv --python 3.12 "${RUNTIME_VENV}"
  "${UV_BIN}" pip install --python "${RUNTIME_VENV}/bin/python" \
    --require-hashes -r "${REQUIREMENTS_FILE}"
  /usr/bin/printf '%s\n' "${REQUIREMENTS_HASH}" > "${RUNTIME_STAMP}"
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
  # Flagged before the generator runs, so a rollback also retires a config file
  # that was only partially written.
  CONFIG_CREATED_THIS_RUN=1
  "${RUNTIME_VENV}/bin/python" "${INSTALL_ROOT}/scripts/community/create_config.py" \
    --output "${CONFIG_FILE}" --display-name "${DISPLAY_NAME}" --admin-email "${ADMIN_EMAIL}" \
    --port "${COMMUNITY_PORT}"
else
  echo "Keeping existing private configuration at ${CONFIG_FILE}"
fi

"${RUNTIME_VENV}/bin/python" "${INSTALL_ROOT}/scripts/community/doctor.py" \
  --config "${CONFIG_FILE}" --data-dir "${COMMUNITY_HOME}/data"

# Keep a copy of the job description that is about to be regenerated: the
# rollback has to hand the restored app its own launchd job, not the one the
# failed upgrade wrote.
if [[ -f "${PLIST_FILE}" ]]; then
  PREVIOUS_PLIST_BACKUP="${BACKUP_ROOT}/launchd-plist-$(backup_stamp launchd-plist .plist).plist"
  cp "${PLIST_FILE}" "${PREVIOUS_PLIST_BACKUP}"
else
  PLIST_CREATED_THIS_RUN=1
fi
"${RUNTIME_VENV}/bin/python" "${INSTALL_ROOT}/scripts/community/create_launchd_plist.py" \
  --output "${PLIST_FILE}" --app-root "${INSTALL_ROOT}" \
  --community-home "${COMMUNITY_HOME}" --python "${RUNTIME_VENV}/bin/python" \
  --label "${LAUNCHD_LABEL}" --port "${COMMUNITY_PORT}"

if [[ -e "${USER_APP}" ]]; then
  EXISTING_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${USER_APP}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "${EXISTING_BUNDLE_ID}" != "chat.agents.community" ]]; then
    echo "Refusing to replace a different application at ${USER_APP}" >&2
    exit 1
  fi
  PREVIOUS_USER_APP_BACKUP="${BACKUP_ROOT}/Agents Chat app-$(backup_stamp 'Agents Chat app' .app).app"
  mv "${USER_APP}" "${PREVIOUS_USER_APP_BACKUP}"
fi
"${RUNTIME_VENV}/bin/python" "${INSTALL_ROOT}/scripts/community/create_macos_app.py" \
  --output "${USER_APP}" --opener "${INSTALL_ROOT}/scripts/community/open_macos.sh"

# The app tree, runtime, config, launchd plist, and launcher app are all in
# place — a failure past this point (opening the browser) must not roll back.
INSTALL_COMPLETE=1
trap - EXIT INT TERM HUP QUIT

# The new runtime is proven good, so the copies this run set aside are no longer
# a recovery path -- only dead weight. A venv is >100 MB, so keeping one per
# upgrade quietly grows Application Support without bound.
#
# Nothing here is deleted: like the uninstaller, superseded backups are moved to
# ~/.Trash, so the user can still recover them and can reclaim the space by
# emptying the Trash. Installers in this project never run a recursive delete.
prune_backups() {
  local prefix candidate keep=2 total drop index trash
  [[ -d "${BACKUP_ROOT}" ]] || return 0
  trash="${HOME}/.Trash/Agents Chat superseded backups $(date +%Y%m%d-%H%M%S)-$$"
  for prefix in app venv "Agents Chat app" requirements-stamp launchd-plist \
                failed-venv failed-launcher failed-plist failed-config incomplete-install; do
    # backup_stamp names embed a zero-padded timestamp, so the glob expands
    # oldest-first and the leading entries are the ones to retire. Collected into
    # an array rather than piped, so names containing spaces stay intact and no
    # GNU-only NUL-delimited flags are needed (BSD tail has no -z).
    local entries=()
    for candidate in "${BACKUP_ROOT}/${prefix}"-*; do
      [[ -e "${candidate}" ]] && entries+=("${candidate}")
    done
    total=${#entries[@]}
    drop=$((total - keep))
    (( drop > 0 )) || continue
    mkdir -p "${trash}" 2>/dev/null || return 0
    for (( index = 0; index < drop; index++ )); do
      mv "${entries[$index]}" "${trash}/" 2>/dev/null || true
    done
  done
}
prune_backups

if (( NO_START )); then
  echo "Agents Chat is installed. Open it from ${USER_APP}"
  exit 0
fi

if [[ "${OPEN_BROWSER}" == "1" ]]; then
  "${INSTALL_ROOT}/scripts/community/open_macos.sh"
else
  launchctl bootstrap "gui/$(id -u)" "${PLIST_FILE}"
  launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}"
fi
echo "Agents Chat Community Edition is installed and ready at http://127.0.0.1:${COMMUNITY_PORT}/"
echo "You can reopen it later from ${USER_APP}"
