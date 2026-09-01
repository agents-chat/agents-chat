param([switch]$PurgeData)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") { throw "This uninstaller must run on Windows." }

# Step outside the installation before touching it. Windows holds a process's
# current directory open, so if this script were launched with its working
# directory inside "Agent Chat\app" (a shortcut, or a terminal cd'd into the
# tree), moving that directory below would fail with access-denied and abort
# the uninstall. Both spellings are needed: PowerShell's location stack and the
# Win32 current directory are tracked separately in Windows PowerShell 5.1.
Set-Location -LiteralPath $env:SystemRoot
[Environment]::CurrentDirectory = $env:SystemRoot

$CommunityHome = if ($env:AGENT_CHAT_HOME) { $env:AGENT_CHAT_HOME } else { Join-Path $env:LOCALAPPDATA "Agent Chat" }
$DefaultStartupRoot = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$StartupFile = if ($env:AGENT_CHAT_STARTUP_FILE) { $env:AGENT_CHAT_STARTUP_FILE } else { Join-Path $DefaultStartupRoot "Agent Chat Community.cmd" }
$StartMenuRoot = if ($env:AGENT_CHAT_START_MENU_ROOT) { $env:AGENT_CHAT_START_MENU_ROOT } else { Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Agents Chat" }
$DesktopRoot = [Environment]::GetFolderPath("Desktop")
$DesktopShortcut = if ($env:AGENT_CHAT_DESKTOP_SHORTCUT) { $env:AGENT_CHAT_DESKTOP_SHORTCUT } elseif ($DesktopRoot) { Join-Path $DesktopRoot "Agents Chat.lnk" } else { "" }
$RemovedRoot = Join-Path $env:LOCALAPPDATA ("Agent Chat Removed\" + (Get-Date -Format "yyyyMMdd-HHmmss"))

New-Item -ItemType Directory -Force -Path $RemovedRoot | Out-Null
$ConfigFile = Join-Path $CommunityHome "config.env"
$PortNumber = 8086
if (Test-Path -LiteralPath $ConfigFile) {
    $PublicUrlLine = Get-Content -LiteralPath $ConfigFile | Where-Object { $_ -match '^ORCHESTRATOR_PUBLIC_URL=' } | Select-Object -First 1
    if ($PublicUrlLine -and $PublicUrlLine -match ':(\d{1,5})/?$') {
        $ParsedPort = 0
        if ([int]::TryParse($Matches[1], [ref]$ParsedPort) -and $ParsedPort -ge 1 -and $ParsedPort -le 65535) {
            $PortNumber = $ParsedPort
        }
    }
}

$CandidatePids = @()
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like "*$CommunityHome*" -and (
        $_.CommandLine -like "*run_windows.ps1*" -or $_.CommandLine -like "*uvicorn*app:app*"
    )
} | ForEach-Object { $CandidatePids += [int]$_.ProcessId }

try {
    Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $PortNumber -State Listen -ErrorAction Stop | ForEach-Object {
        $Process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $_.OwningProcess) -ErrorAction SilentlyContinue
        if ($Process -and $Process.CommandLine -and $Process.CommandLine -like "*$CommunityHome*") {
            $CandidatePids += [int]$_.OwningProcess
        }
    }
} catch {
    # Older PowerShell installations may not provide Get-NetTCPConnection; the
    # command-line ownership check above remains authoritative.
}

$CandidatePids | Sort-Object -Unique | ForEach-Object {
    $TargetPid = [int]$_
    if (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue) {
        $Killer = Start-Process -FilePath taskkill.exe `
            -ArgumentList @("/PID", [string]$TargetPid, "/T", "/F") `
            -WindowStyle Hidden -Wait -PassThru
        if ($Killer.ExitCode -ne 0 -and (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue)) {
            throw "Could not stop Agents Chat process $TargetPid."
        }
    }
}
Start-Sleep -Milliseconds 500

$StillRunning = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like "*$CommunityHome*" -and (
        $_.CommandLine -like "*run_windows.ps1*" -or $_.CommandLine -like "*uvicorn*app:app*"
    )
}
if ($StillRunning) {
    throw "Agents Chat is still running. Close it and run the uninstaller again."
}
# Remove the installed tree BEFORE the launchers. If anything here fails, the
# user is left with a working app they can still open and still uninstall from
# the Start menu; stripping the shortcuts first would strand them with an
# installed app and no way to reach it.
$Items = @("app", "runtime", "tools", "logs", "backups", "autostart.ps1")
if ($PurgeData) { $Items += @("config.env", "data") }
foreach ($Item in $Items) {
    $Source = Join-Path $CommunityHome $Item
    if (Test-Path -LiteralPath $Source) {
        Move-Item -LiteralPath $Source -Destination $RemovedRoot
    }
}

if (Test-Path -LiteralPath $StartupFile) {
    Move-Item -LiteralPath $StartupFile -Destination $RemovedRoot
}
if (Test-Path -LiteralPath $StartMenuRoot) {
    Move-Item -LiteralPath $StartMenuRoot -Destination $RemovedRoot
}
if ($DesktopShortcut -and (Test-Path -LiteralPath $DesktopShortcut)) {
    Move-Item -LiteralPath $DesktopShortcut -Destination $RemovedRoot
}

if ($PurgeData) {
    # All user data and installed files above were moved to the recoverable
    # removal folder. Remove the now-empty installation home as well so the
    # next install is indistinguishable from a first install. Never recurse
    # here: an unexpected file must remain visible instead of being destroyed.
    if (Test-Path -LiteralPath $CommunityHome) {
        $RemainingItems = @(Get-ChildItem -LiteralPath $CommunityHome -Force)
        if ($RemainingItems.Count -eq 0) {
            Remove-Item -LiteralPath $CommunityHome -Force
        } else {
            $RemainingNames = ($RemainingItems | ForEach-Object { $_.Name }) -join ", "
            throw "Agents Chat stopped safely, but unexpected files remain in $CommunityHome`: $RemainingNames"
        }
    }
    Write-Host "Agent Chat and its local data were moved to: $RemovedRoot"
} else {
    Write-Host "Agent Chat was removed. Configuration and chat data remain in: $CommunityHome"
    Write-Host "Removed files are recoverable from: $RemovedRoot"
}
