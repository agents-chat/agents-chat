param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$CommunityHome = if ($env:AGENT_CHAT_HOME) { $env:AGENT_CHAT_HOME } else { Join-Path $env:LOCALAPPDATA "Agent Chat" }
$ConfigFile = if ($env:AGENT_CHAT_CONFIG_FILE) { $env:AGENT_CHAT_CONFIG_FILE } else { Join-Path $CommunityHome "config.env" }
$DataDir = if ($env:AGENT_CHAT_DATA_DIR) { $env:AGENT_CHAT_DATA_DIR } else { Join-Path $CommunityHome "data" }
$PythonBin = if ($env:AGENT_CHAT_PYTHON) { $env:AGENT_CHAT_PYTHON } else { Join-Path $CommunityHome "runtime\venv\Scripts\python.exe" }
$Port = if ($env:PORT) { $env:PORT } else { "8086" }

if ($env:OS -ne "Windows_NT") {
    throw "Agent Chat's Windows launcher must run on Windows."
}
if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
    throw "Missing Community Edition configuration: $ConfigFile"
}
if (-not (Test-Path -LiteralPath $PythonBin -PathType Leaf)) {
    throw "Missing Agent Chat Python runtime: $PythonBin"
}
$PortNumber = 0
if (-not [int]::TryParse($Port, [ref]$PortNumber) -or $PortNumber -lt 1 -or $PortNumber -gt 65535) {
    throw "PORT must be an integer between 1 and 65535."
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
& $PythonBin (Join-Path $AppRoot "scripts\community\migrate_community_db.py") --data-dir $DataDir
if ($LASTEXITCODE -ne 0) { throw "Agent Chat database migration failed." }
$env:AGENT_CHAT_EDITION = "community"
$env:AGENT_CHAT_ENV_FILE = $ConfigFile
$env:DATA_DIR = $DataDir
$env:AGENT_CHAT_DB = Join-Path $DataDir "agent-chat.sqlite3"
$env:CALENDARS_STORE = Join-Path $DataDir "calendars.json"
$env:JARVIS_FOREMAN_CONFIG = Join-Path $DataDir "jarvis_foreman.json"
$env:AGENT_WORKSPACES_ROOT = Join-Path $DataDir "workspaces"
$env:PROJECTS_FOLDER_ROOT = Join-Path $DataDir "projects"
$env:WORKSPACE_DISCOVER_ROOTS = Join-Path $DataDir "projects"
Remove-Item Env:STORAGE_DB -ErrorAction SilentlyContinue

& $PythonBin (Join-Path $AppRoot "scripts\community\doctor.py") --platform windows --config $ConfigFile --data-dir $DataDir
if ($LASTEXITCODE -ne 0) { throw "Agent Chat preflight checks failed." }

Write-Host "Starting Agent Chat Community Edition on http://127.0.0.1:$PortNumber"
& $PythonBin -m uvicorn app:app --app-dir $AppRoot --host 127.0.0.1 --port $PortNumber --timeout-graceful-shutdown 10 --no-access-log
exit $LASTEXITCODE
