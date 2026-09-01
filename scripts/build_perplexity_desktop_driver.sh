#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

SOURCE="${PWD}/scripts/perplexity_desktop_driver.swift"
INFO="${PWD}/scripts/perplexity_desktop_driver-Info.plist"
MODE="${1:-runtime}"
if [[ "${MODE}" == "--bundled" ]]; then
  APP="${PWD}/assets/macos/Agent Chat Perplexity Driver.app"
  EXECUTABLE_NAME="perplexity_desktop_driver.bin"
else
  APP="${PWD}/perplexity_bridge/Agent Chat Perplexity Driver.app"
  EXECUTABLE_NAME="perplexity_desktop_driver"
fi
MACOS="${APP}/Contents/MacOS"
EXECUTABLE="${MACOS}/${EXECUTABLE_NAME}"

if [[ -x "$EXECUTABLE" && "$EXECUTABLE" -nt "$SOURCE" && "$EXECUTABLE" -nt "$INFO" ]]; then
  printf '%s\n' "$EXECUTABLE"
  exit 0
fi

mkdir -p "$MACOS"
if [[ "${MODE}" == "--bundled" ]]; then
  PPLX_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-chat-perplexity.XXXXXX")"
  trap 'rm -rf "${PPLX_BUILD_DIR}"' EXIT
  xcrun swiftc "$SOURCE" -target arm64-apple-macosx13.0 \
    -o "${PPLX_BUILD_DIR}/driver-arm64" \
    -framework AppKit -framework ApplicationServices
  xcrun swiftc "$SOURCE" -target x86_64-apple-macosx13.0 \
    -o "${PPLX_BUILD_DIR}/driver-x86_64" \
    -framework AppKit -framework ApplicationServices
  lipo -create "${PPLX_BUILD_DIR}/driver-arm64" "${PPLX_BUILD_DIR}/driver-x86_64" \
    -output "$EXECUTABLE"
  cp "$INFO" "${APP}/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleExecutable ${EXECUTABLE_NAME}" \
    "${APP}/Contents/Info.plist"
else
  cp "$INFO" "${APP}/Contents/Info.plist"
  xcrun swiftc "$SOURCE" \
    -o "$EXECUTABLE" \
    -framework AppKit \
    -framework ApplicationServices
fi
codesign --force --sign - --identifier chat.agents.perplexity-driver "$APP" >/dev/null
printf '%s\n' "$EXECUTABLE"
