@echo off
setlocal
title Uninstall Agents Chat
set "UNINSTALLER=%LOCALAPPDATA%\Agent Chat\app\scripts\community\uninstall_windows.ps1"
if not exist "%UNINSTALLER%" (
  echo Agents Chat is not installed for this Windows account.
  echo.
  pause
  exit /b 1
)
echo Agents Chat will be removed. Your settings and chats will be preserved.
echo Removed program files are kept in the recoverable Agent Chat Removed folder.
echo.
powershell.exe -NoExit -NoProfile -ExecutionPolicy Bypass -File "%UNINSTALLER%"
