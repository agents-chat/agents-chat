#!/usr/bin/env bash
set -u

OPENER="${HOME}/Library/Application Support/Agent Chat/app/scripts/community/open_macos.sh"
if [[ ! -x "${OPENER}" ]]; then
  echo "Agents Chat is not installed for this Mac account."
  echo "Run 'Install Agents Chat on Mac.command' from the downloaded folder first."
  echo
  read -r -n 1 -p "Press any key to close..."
  echo
  exit 1
fi

"${OPENER}"
OPEN_EXIT=$?
if (( OPEN_EXIT != 0 )); then
  echo
  read -r -n 1 -p "Press any key to close..."
  echo
fi
exit "${OPEN_EXIT}"
