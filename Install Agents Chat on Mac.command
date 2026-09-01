#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALLER="${SCRIPT_DIR}/scripts/community/install_macos.sh"

if [[ "${1:-}" == "--check-only" ]]; then
  exec "${INSTALLER}" --check-only
fi

clear
echo "============================================================"
echo "                   Install Agents Chat"
echo "============================================================"
echo
echo "This installs Agents Chat only for your Mac account."
echo "It does not need an administrator password or sudo access."
echo "When the browser opens, you will create your own password there."
echo

if [[ ! -x "${INSTALLER}" ]]; then
  echo "[ERROR] The installer is missing or not executable."
  echo "Extract the entire ZIP, then try again."
  echo
  read -r -n 1 -p "Press any key to close..."
  echo
  exit 1
fi

"${INSTALLER}"
INSTALL_EXIT=$?
echo
if (( INSTALL_EXIT != 0 )); then
  echo "Agents Chat was not installed. Read the error above, then try again."
else
  echo "Installation complete."
  echo "Finish the one-minute setup in your browser. No temporary password is needed."
  echo "You can reopen Agents Chat later from your Applications folder."
fi
echo
read -r -n 1 -p "Press any key to close..."
echo
exit "${INSTALL_EXIT}"
