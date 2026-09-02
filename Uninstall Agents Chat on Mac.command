#!/usr/bin/env bash
set -u

UNINSTALLER="${HOME}/Library/Application Support/Agent Chat/app/scripts/community/uninstall_macos.sh"
if [[ ! -x "${UNINSTALLER}" ]]; then
  echo "Agents Chat is not installed for this Mac account."
  echo
  read -r -n 1 -p "Press any key to close..."
  echo
  exit 1
fi

clear
echo "============================================================"
echo "                  Uninstall Agents Chat"
echo "============================================================"
echo
echo "Press Return to remove the app but KEEP your settings and chats."
echo "Type DELETE to also move settings and chats to the recoverable Trash."
echo
read -r -p "Your choice: " CHOICE
if [[ "${CHOICE}" == "DELETE" ]]; then
  "${UNINSTALLER}" --purge-data
else
  "${UNINSTALLER}"
fi
UNINSTALL_EXIT=$?
echo
read -r -n 1 -p "Press any key to close..."
echo
exit "${UNINSTALL_EXIT}"
