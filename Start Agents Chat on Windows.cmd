@echo off
setlocal
title Start Agents Chat
set "OPENER=%LOCALAPPDATA%\Agent Chat\app\scripts\community\open_windows.ps1"
if not exist "%OPENER%" (
  echo Agents Chat is not installed for this Windows account.
  echo Run "Install Agents Chat on Windows.cmd" from the downloaded folder first.
  echo.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%OPENER%"
if errorlevel 1 (
  echo.
  echo Agents Chat could not start. The error above explains what needs attention.
  pause
  exit /b 1
)
