param(
    [ValidateRange(0, 65535)]
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$CommunityHome = if ($env:AGENT_CHAT_HOME) { $env:AGENT_CHAT_HOME } else { Join-Path $env:LOCALAPPDATA "Agent Chat" }
$ConfigFile = Join-Path $CommunityHome "config.env"
$RunScript = Join-Path $CommunityHome "app\scripts\community\run_windows.ps1"
$LogsRoot = Join-Path $CommunityHome "logs"
$OutputLog = Join-Path $LogsRoot "agent-chat.log"
$ErrorLog = Join-Path $LogsRoot "agent-chat.error.log"

function Get-ConfiguredPort([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 0 }
    $Line = Get-Content -LiteralPath $Path | Where-Object {
        $_ -match '^ORCHESTRATOR_PUBLIC_URL=http://(?:127\.0\.0\.1|localhost):(\d{1,5})/?$'
    } | Select-Object -First 1
    if (-not $Line -or $Line -notmatch '^ORCHESTRATOR_PUBLIC_URL=http://(?:127\.0\.0\.1|localhost):(\d{1,5})/?$') { return 0 }
    $Parsed = 0
    if ([int]::TryParse($Matches[1], [ref]$Parsed) -and $Parsed -ge 1 -and $Parsed -le 65535) {
        return $Parsed
    }
    return 0
}

function Test-LocalPort([int]$PortNumber) {
    $Client = New-Object System.Net.Sockets.TcpClient
    try {
        $Result = $Client.BeginConnect("127.0.0.1", $PortNumber, $null, $null)
        if (-not $Result.AsyncWaitHandle.WaitOne(750)) { return $false }
        $Client.EndConnect($Result)
        return $true
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

if ($Port -eq 0) { $Port = Get-ConfiguredPort $ConfigFile }
if ($Port -lt 1) { throw "Agents Chat does not have a valid saved local address. Run the installer again." }
if (-not (Test-Path -LiteralPath $RunScript -PathType Leaf)) {
    throw "Agents Chat is not installed for this Windows user."
}

if (-not (Test-LocalPort $Port)) {
    $env:PORT = [string]$Port
    New-Item -ItemType Directory -Force -Path $LogsRoot | Out-Null
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $RunScript + '"')
    ) -RedirectStandardOutput $OutputLog -RedirectStandardError $ErrorLog
    $Deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 500
        if (Test-LocalPort $Port) { break }
    } while ((Get-Date) -lt $Deadline)
}

if (-not (Test-LocalPort $Port)) {
    $Detail = ""
    if (Test-Path -LiteralPath $ErrorLog -PathType Leaf) {
        $Detail = ((Get-Content -LiteralPath $ErrorLog -Tail 8) -join " ").Trim()
    }
    if (-not $Detail -and (Test-Path -LiteralPath $OutputLog -PathType Leaf)) {
        $Detail = ((Get-Content -LiteralPath $OutputLog -Tail 8) -join " ").Trim()
    }
    $Explanation = if ($Detail) { " Last diagnostic: $Detail" } else { " No diagnostic output was produced." }
    throw "Agents Chat did not start on its saved local address.$Explanation Diagnostic log: $ErrorLog. Run the installer again to repair it."
}

Start-Process "http://127.0.0.1:$Port/"
Write-Host "Opened Agents Chat at http://127.0.0.1:$Port/"
