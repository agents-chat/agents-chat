@echo off
setlocal
title Install Agents Chat

echo ============================================================
echo                  Install Agents Chat
echo ============================================================
echo.
echo This installs Agents Chat only for your Windows account.
echo It does not need administrator access or open a firewall port.
echo When the browser opens, you will create your own password there.
echo.

set "INSTALLER=%~dp0scripts\community\install_windows.ps1"
if not exist "%INSTALLER%" (
  echo [ERROR] The installer is missing. Extract the entire ZIP, then try again.
  echo.
  pause
  exit /b 1
)

if /I "%~1"=="--check-only" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" -CheckOnly
  if errorlevel 1 (exit /b 1) else (exit /b 0)
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%"
set "INSTALL_EXIT=%ERRORLEVEL%"
echo.
if not "%INSTALL_EXIT%"=="0" (
  echo Agents Chat was not installed. Read the error above, then try again.
) else (
  echo Installation complete.
  echo Finish the one-minute setup in your browser. No temporary password is needed.
  echo You can reopen Agents Chat later from your Desktop or Start menu.
)
echo.
pause
exit /b %INSTALL_EXIT%
