param(
    [switch]$CheckOnly,
    [switch]$NoStart,
    [string]$DisplayName = "Owner",
    [string]$AdminEmail = "admin@localhost",
    [ValidateRange(0, 65535)]
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$CommunityHome = if ($env:AGENT_CHAT_HOME) { $env:AGENT_CHAT_HOME } else { Join-Path $env:LOCALAPPDATA "Agent Chat" }
$InstallRoot = Join-Path $CommunityHome "app"
$RuntimeRoot = Join-Path $CommunityHome "runtime"
$ToolsRoot = Join-Path $CommunityHome "tools"
$LogsRoot = Join-Path $CommunityHome "logs"
$ConfigFile = Join-Path $CommunityHome "config.env"
$DefaultStartupRoot = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$StartupFile = if ($env:AGENT_CHAT_STARTUP_FILE) { $env:AGENT_CHAT_STARTUP_FILE } else { Join-Path $DefaultStartupRoot "Agent Chat Community.cmd" }
$StartupRoot = Split-Path -Parent $StartupFile
$StartMenuRoot = if ($env:AGENT_CHAT_START_MENU_ROOT) { $env:AGENT_CHAT_START_MENU_ROOT } else { Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Agents Chat" }
$DesktopRoot = [Environment]::GetFolderPath("Desktop")
$DesktopShortcut = if ($env:AGENT_CHAT_DESKTOP_SHORTCUT) { $env:AGENT_CHAT_DESKTOP_SHORTCUT } elseif ($DesktopRoot) { Join-Path $DesktopRoot "Agents Chat.lnk" } else { "" }
$UvVersion = "0.11.32"
$UvArchiveUrl = "https://releases.astral.sh/github/uv/releases/download/0.11.32/uv-x86_64-pc-windows-msvc.zip"
$UvArchiveSha256 = "acfde570451cfdb8689fa159a138ee805ba4e241c466432750302c86254b0984"
$UvBinarySha256 = "23cf0f8194ff576562646a1a2950c6826249c8806cd1547debd24db77eb68f58"
$BackupsRoot = Join-Path $CommunityHome "backups"
$VenvRoot = Join-Path $RuntimeRoot "venv"
$PythonBin = Join-Path $VenvRoot "Scripts\python.exe"
$RuntimeStamp = Join-Path $RuntimeRoot "requirements.sha256"
$RuntimeProviderStamp = Join-Path $RuntimeRoot "provider.txt"
$OfficialPythonVersion = "3.12.10"
$OfficialPythonUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$OfficialPythonSha256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
$OfficialPythonRoot = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"
$OfficialPythonBin = Join-Path $OfficialPythonRoot "python.exe"
$PortWasProvided = $PSBoundParameters.ContainsKey("Port")

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

function Test-LocalPortAvailable([int]$PortNumber) {
    $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $PortNumber)
    try {
        $Listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        $Listener.Stop()
    }
}

function Find-AvailablePort {
    $Candidates = @(8086..8096) + @(18086..18096)
    foreach ($Candidate in $Candidates) {
        if (Test-LocalPortAvailable $Candidate) { return $Candidate }
    }
    throw "Agents Chat could not find a free private local port. Close another local server and run the installer again."
}

function Stop-InstalledProcesses([string]$InstallHome) {
    $CandidatePids = @()
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and $_.CommandLine -like "*$InstallHome*" -and (
            $_.CommandLine -like "*run_windows.ps1*" -or $_.CommandLine -like "*uvicorn*app:app*"
        )
    } | ForEach-Object { $CandidatePids += [int]$_.ProcessId }
    $CandidatePids | Sort-Object -Unique | ForEach-Object {
        $TargetPid = [int]$_
        if (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue) {
            $Killer = Start-Process -FilePath taskkill.exe -ArgumentList @(
                "/PID", [string]$TargetPid, "/T", "/F"
            ) -WindowStyle Hidden -Wait -PassThru
            if ($Killer.ExitCode -ne 0 -and (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue)) {
                throw "Could not stop the existing Agents Chat process $TargetPid for upgrade."
            }
        }
    }
    if ($CandidatePids.Count -gt 0) { Start-Sleep -Milliseconds 500 }
}

function New-AgentChatShortcut(
    [string]$Path,
    [string]$Target,
    [string]$Arguments,
    [string]$WorkingDirectory,
    [string]$IconPath,
    [int]$WindowStyle = 1
) {
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($Path)
    $Shortcut.TargetPath = $Target
    $Shortcut.Arguments = $Arguments
    $Shortcut.WorkingDirectory = $WorkingDirectory
    $Shortcut.WindowStyle = $WindowStyle
    if (Test-Path -LiteralPath $IconPath -PathType Leaf) { $Shortcut.IconLocation = "$IconPath,0" }
    $Shortcut.Save()
}

function Get-AbsolutePath([string]$Path) {
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Get-CmdSafePath([string]$Path) {
    # cmd.exe reads a .cmd file in the OEM codepage, so a path outside plain
    # ASCII cannot be written as ASCII without silently becoming "?" and
    # breaking every login. The 8.3 short name is always ASCII, so prefer it
    # whenever the real path leaves ASCII behind.
    if ($Path -notmatch '[^\x20-\x7E]') { return $Path }
    try {
        $FileSystem = New-Object -ComObject Scripting.FileSystemObject
        $ShortPath = $FileSystem.GetFile($Path).ShortPath
        if ($ShortPath -and $ShortPath -notmatch '[^\x20-\x7E]') { return $ShortPath }
    } catch {
        # 8.3 name creation can be disabled on the volume; fall through to the
        # OEM-encoded write below.
    }
    return ""
}

function Get-OemEncoding {
    try { [System.Text.Encoding]::RegisterProvider([System.Text.CodePagesEncodingProvider]::Instance) } catch { }
    try {
        return [System.Text.Encoding]::GetEncoding([System.Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
    } catch {
        return $null
    }
}

function Assert-Preflight([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    Write-Host "[PASS] $Message"
}

function Get-RecoveryPath([string]$Prefix) {
    $Stem = Join-Path $BackupsRoot ("$Prefix-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-$PID")
    $Candidate = $Stem
    $Counter = 0
    while (Test-Path -LiteralPath $Candidate) {
        $Counter++
        $Candidate = "$Stem-$Counter"
    }
    return $Candidate
}

function Get-Win32ErrorCode([Exception]$Exception) {
    $Current = $Exception
    while ($Current) {
        if ($Current -is [System.ComponentModel.Win32Exception]) {
            return $Current.NativeErrorCode
        }
        $Current = $Current.InnerException
    }
    return 0
}

function Test-PinnedUv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $BinaryHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($BinaryHash -ne $UvBinarySha256) { return $false }
        $VersionOutput = (& $Path --version | Select-Object -Last 1)
        $VersionExitCode = $LASTEXITCODE
    } catch {
        return $false
    }
    $VersionPattern = '^uv ' + [Regex]::Escape($UvVersion) + '(?:\s|$)'
    return $VersionExitCode -eq 0 -and $VersionOutput -and ([string]$VersionOutput).Trim() -match $VersionPattern
}

function Test-PythonLaunch([string]$Path, [string]$Code = "pass") {
    # Windows PowerShell 5.1 joins ArgumentList arrays with spaces without
    # preserving element quotes. Encode the Python snippet so the -c value is a
    # single whitespace-free argument even when the original code contains
    # imports or other spaces.
    $Encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $EncodedCode = "exec(__import__('base64').b64decode('$Encoded').decode())"
    $Process = $null
    try {
        # Start-Process on Windows PowerShell 5.1 wraps App Control failures in
        # InvalidOperationException and drops the native error code. Calling
        # System.Diagnostics.Process directly preserves the inner
        # Win32Exception, so Get-Win32ErrorCode can classify exact error 4551.
        $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
        $StartInfo.FileName = $Path
        $StartInfo.Arguments = "-I -c $EncodedCode"
        $StartInfo.UseShellExecute = $false
        $StartInfo.CreateNoWindow = $true
        $Process = New-Object System.Diagnostics.Process
        $Process.StartInfo = $StartInfo
        if (-not $Process.Start()) { throw "Process.Start returned false." }
        $Process.WaitForExit()
        return [pscustomobject]@{
            Success = ($Process.ExitCode -eq 0)
            ExitCode = $Process.ExitCode
            NativeErrorCode = 0
        }
    } catch {
        return [pscustomobject]@{
            Success = $false
            ExitCode = -1
            NativeErrorCode = (Get-Win32ErrorCode $_.Exception)
        }
    } finally {
        if ($Process) { $Process.Dispose() }
    }
}

function Test-OfficialPython([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Path
    $Publisher = if ($Signature.SignerCertificate) {
        $Signature.SignerCertificate.GetNameInfo(
            [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
            $false
        )
    } else { "" }
    if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or $Publisher -ne "Python Software Foundation") {
        return $false
    }
    $Probe = Test-PythonLaunch $Path
    if (-not $Probe.Success) { return $false }
    try {
        $Details = (& $Path -I -c "import platform;print(platform.python_version()+'|'+platform.architecture()[0])" | Select-Object -Last 1)
    } catch {
        return $false
    }
    return $Details -and ([string]$Details).Trim() -eq "$OfficialPythonVersion|64bit"
}

function Get-RegisteredPython312Roots {
    $Seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($RegistryRoot in @(
        "HKCU:\Software\Python\PythonCore",
        "HKLM:\Software\Python\PythonCore",
        "HKLM:\Software\WOW6432Node\Python\PythonCore"
    )) {
        if (-not (Test-Path -LiteralPath $RegistryRoot)) { continue }
        Get-ChildItem -LiteralPath $RegistryRoot -ErrorAction SilentlyContinue | Where-Object {
            $_.PSChildName -like "3.12*"
        } | ForEach-Object {
            $InstallPathKey = Join-Path $_.PSPath "InstallPath"
            if (Test-Path -LiteralPath $InstallPathKey) {
                $RegisteredRoot = (Get-Item -LiteralPath $InstallPathKey).GetValue("")
                if ($RegisteredRoot -and $Seen.Add([string]$RegisteredRoot)) {
                    Write-Output ([string]$RegisteredRoot)
                }
            }
        }
    }
    foreach ($UninstallRoot in @(
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
    )) {
        if (-not (Test-Path -LiteralPath $UninstallRoot)) { continue }
        Get-ChildItem -LiteralPath $UninstallRoot -ErrorAction SilentlyContinue | ForEach-Object {
            $Entry = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            $DisplayName = if ($Entry -and $Entry.PSObject.Properties["DisplayName"]) { [string]$Entry.DisplayName } else { "" }
            $InstallLocation = if ($Entry -and $Entry.PSObject.Properties["InstallLocation"]) { [string]$Entry.InstallLocation } else { "" }
            if ($DisplayName -like "Python 3.12*" -and $InstallLocation) {
                $RegisteredRoot = $InstallLocation
                if ($Seen.Add($RegisteredRoot)) { Write-Output $RegisteredRoot }
            }
        }
    }
}

function Test-PathInsideCommunityHome([string]$Path) {
    try {
        $CanonicalHome = [System.IO.Path]::GetFullPath($CommunityHome).TrimEnd('\') + '\'
        $CanonicalPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
        return $CanonicalPath.StartsWith($CanonicalHome, [System.StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $true
    }
}

function Install-OfficialPythonFallback {
    $RegisteredPythonRoots = @(Get-RegisteredPython312Roots)
    foreach ($RegisteredPythonRoot in $RegisteredPythonRoots) {
        # A registered Python installation may not live under the recoverable
        # Agent Chat tools tree: the app uninstaller moves that tree, which
        # would otherwise strand the product's Programs registration.
        if (Test-PathInsideCommunityHome $RegisteredPythonRoot) { continue }
        $RegisteredPythonBin = Join-Path $RegisteredPythonRoot "python.exe"
        if (Test-OfficialPython $RegisteredPythonBin) {
            Write-Host "[PASS] reusing registered signed Python.org $OfficialPythonVersion runtime"
            return $RegisteredPythonBin
        }
    }
    if (Test-OfficialPython $OfficialPythonBin) {
        Write-Host "[PASS] reusing the signed Python.org $OfficialPythonVersion runtime"
        return $OfficialPythonBin
    }
    if ($RegisteredPythonRoots.Count -gt 0) {
        throw "A different or unverifiable Python 3.12 installation is already registered. Agent Chat will not modify it automatically."
    }
    if (Test-Path -LiteralPath $OfficialPythonBin -PathType Leaf) {
        throw "A Python runtime already exists at $OfficialPythonBin but failed the version or publisher check. Agent Chat will not overwrite it."
    }

    Write-Host "Enterprise Application Control blocked uv's managed interpreter (Windows error 4551)."
    Write-Host "Installing the signed Python.org $OfficialPythonVersion fallback for this Windows user..."
    New-Item -ItemType Directory -Force -Path $ToolsRoot | Out-Null
    $InstallerPath = Join-Path $ToolsRoot ("python-$OfficialPythonVersion-amd64-$PID.download.exe")
    try {
        $DownloadReady = $false
        for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -UseBasicParsing -Uri $OfficialPythonUrl -OutFile $InstallerPath
                $DownloadReady = $true
                break
            } catch {
                if (Test-Path -LiteralPath $InstallerPath -PathType Leaf) {
                    Remove-Item -LiteralPath $InstallerPath -Force
                }
                if ($Attempt -lt 3) {
                    Write-Host "Signed Python download was interrupted. Retrying automatically ($Attempt of 3)..."
                    Start-Sleep -Seconds (2 * $Attempt)
                }
            }
        }
        if (-not $DownloadReady) { throw "The signed Python.org runtime download failed after 3 attempts." }

        $ActualSha256 = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ActualSha256 -ne $OfficialPythonSha256) {
            throw "The signed Python.org runtime failed its pinned SHA-256 check."
        }
        $InstallerSignature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
        $InstallerPublisher = if ($InstallerSignature.SignerCertificate) {
            $InstallerSignature.SignerCertificate.GetNameInfo(
                [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
                $false
            )
        } else { "" }
        if ($InstallerSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or $InstallerPublisher -ne "Python Software Foundation") {
            throw "The Python.org runtime installer failed its Authenticode publisher check."
        }

        $InstallArguments = '/quiet InstallAllUsers=0 TargetDir="{0}" Include_pip=1 Include_launcher=0 InstallLauncherAllUsers=0 AssociateFiles=0 PrependPath=0 Shortcuts=0 Include_doc=0 Include_test=0 Include_tcltk=0' -f $OfficialPythonRoot
        $InstallerProcess = Start-Process -FilePath $InstallerPath -ArgumentList $InstallArguments -Wait -PassThru
        if ($InstallerProcess.ExitCode -notin @(0, 3010)) {
            throw "The signed Python.org runtime installer exited $($InstallerProcess.ExitCode)."
        }
    } finally {
        if (Test-Path -LiteralPath $InstallerPath -PathType Leaf) {
            Remove-Item -LiteralPath $InstallerPath -Force
        }
    }

    if (-not (Test-OfficialPython $OfficialPythonBin)) {
        throw "The signed Python.org runtime was installed but failed its post-install launch, version, architecture, or publisher check."
    }
    Write-Host "[PASS] signed Python.org $OfficialPythonVersion fallback is policy-compatible"
    Write-Host "Note: this shared current-user Python runtime remains available if Agent Chat is later removed."
    return $OfficialPythonBin
}

function Test-PriorCoreRunnable {
    $PriorRequirements = Join-Path $InstallRoot "requirements.lock"
    foreach ($RequiredPath in @($PriorRequirements, $ConfigFile, $PythonBin, $RuntimeStamp)) {
        if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) { return $false }
    }
    try {
        $PriorRequirementsHash = (Get-FileHash -LiteralPath $PriorRequirements -Algorithm SHA256).Hash.ToLowerInvariant()
        $SavedRequirementsHash = (Get-Content -LiteralPath $RuntimeStamp -Raw).Trim().ToLowerInvariant()
    } catch {
        return $false
    }
    if ($PriorRequirementsHash -ne $SavedRequirementsHash) { return $false }
    $Probe = Test-PythonLaunch $PythonBin "import fastapi,uvicorn,httpx,cryptography,pypdf,imageio_ffmpeg,jinja2,multipart,yaml"
    return $Probe.Success
}

Assert-Preflight ($env:OS -eq "Windows_NT") "Windows detected"
# Installing is only ever allowed from a cleared release package. A development
# checkout may run the read-only preflight (AGENT_CHAT_ALLOW_DEV_INSTALL=1 with
# -CheckOnly) so contributors and CI can validate this script, but it must never
# copy an unreviewed source tree into the install root: only the release build's
# allow-list decides what a package contains. This mirrors install_macos.sh --
# accepting the flag for a real install would let an unscrubbed tree through.
if (Test-Path (Join-Path $SourceRoot ".community-release.json")) {
    Assert-Preflight $true "cleared release marker present"
} elseif ($CheckOnly -and $env:AGENT_CHAT_ALLOW_DEV_INSTALL -eq "1") {
    Assert-Preflight $true "development tree: preflight only, installing from it is refused"
} else {
    Assert-Preflight $false "not a cleared Community Edition release package"
}
Assert-Preflight (Test-Path (Join-Path $SourceRoot "requirements.lock")) "locked Python dependencies present"
Assert-Preflight (Test-Path (Join-Path $SourceRoot "config.community.env")) "portable configuration template present"
Assert-Preflight ($PSVersionTable.PSVersion.Major -ge 5) "PowerShell 5 or newer available"
Assert-Preflight ([bool]$env:APPDATA) "current-user Startup folder available"
Assert-Preflight ($null -ne (Get-Command robocopy.exe -ErrorAction SilentlyContinue)) "Robocopy available"

$ConfiguredPort = Get-ConfiguredPort $ConfigFile
if ($ConfiguredPort -gt 0) {
    if ($PortWasProvided -and $Port -ne 0 -and $Port -ne $ConfiguredPort) {
        throw "This installation already uses local port $ConfiguredPort. Reinstall without -Port to keep existing chats and settings connected correctly."
    }
    $Port = $ConfiguredPort
    Write-Host "[PASS] existing installation will keep local port $Port"
} elseif ($PortWasProvided -and $Port -gt 0) {
    Assert-Preflight (Test-LocalPortAvailable $Port) "requested private local port $Port is available"
} else {
    $Port = Find-AvailablePort
    Write-Host "[PASS] automatically selected private local port $Port"
}

if ($CheckOnly) {
    Write-Host "Windows installer preflight passed; no files were changed."
    exit 0
}

$PriorCoreRunnable = Test-PriorCoreRunnable
$PriorAppBackup = ""
$PriorRuntimeBackup = ""
$ConfigCreatedThisRun = $false

if (Test-Path -LiteralPath $InstallRoot) {
    Write-Host "Stopping the existing Agents Chat process for a safe upgrade..."
    Stop-InstalledProcesses $CommunityHome
}

New-Item -ItemType Directory -Force -Path $CommunityHome, $RuntimeRoot, $ToolsRoot, $LogsRoot, $BackupsRoot | Out-Null
$StagingRoot = Join-Path $CommunityHome ".app-staging-$PID"
if (Test-Path -LiteralPath $StagingRoot) { throw "Staging path already exists: $StagingRoot" }
New-Item -ItemType Directory -Path $StagingRoot | Out-Null

try {
    & robocopy.exe $SourceRoot $StagingRoot /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP `
        /XD .git .venv node_modules data tests __pycache__ .pytest_cache .ruff_cache .claude `
        threads attachments artifacts_store support_tickets voice_cache storage_backups cutover_backups `
        backup agents episodes freewill_briefs push_state custom_voices `
        /XF .env .env.* *.pyc *.sqlite3 *.sqlite3-* sessions.json chats_index.json custom_agents.json workspace_bindings.json calendars.json | Out-Null
    if ($LASTEXITCODE -gt 7) { throw "Robocopy failed with exit code $LASTEXITCODE" }

    $Unexpected = Get-ChildItem -LiteralPath $StagingRoot -Recurse -Force -File | Where-Object {
        $_.Name -eq ".env" -or $_.Name -like "*.sqlite3" -or $_.Name -like "*.sqlite3-*"
    }
    if ($Unexpected) { throw "Staging unexpectedly contains runtime configuration or databases." }

    if (Test-Path -LiteralPath $InstallRoot) {
        $PriorBackupPrefix = if ($PriorCoreRunnable) { "app" } else { "incomplete-prior-app" }
        $PriorAppBackup = Get-RecoveryPath $PriorBackupPrefix
        Move-Item -LiteralPath $InstallRoot -Destination $PriorAppBackup
    }
    Move-Item -LiteralPath $StagingRoot -Destination $InstallRoot
} catch {
    if (Test-Path -LiteralPath $StagingRoot) {
        $Incomplete = Get-RecoveryPath "incomplete-install"
        Move-Item -LiteralPath $StagingRoot -Destination $Incomplete
    }
    if ($PriorCoreRunnable -and $PriorAppBackup -and -not (Test-Path -LiteralPath $InstallRoot) -and (Test-Path -LiteralPath $PriorAppBackup)) {
        Move-Item -LiteralPath $PriorAppBackup -Destination $InstallRoot
    }
    throw
}

$CoreInstallComplete = $false
try {
$UvBin = Join-Path $ToolsRoot "uv.exe"
if (-not (Test-PinnedUv $UvBin)) {
    Write-Host "Installing uv $UvVersion into Agent Chat's private tools directory..."
    $UvArchive = Join-Path $ToolsRoot ("uv-$UvVersion-$PID.download.zip")
    $UvStaging = Join-Path $ToolsRoot (".uv-staging-$PID")
    if (Test-Path -LiteralPath $UvStaging) { throw "uv staging path already exists: $UvStaging" }
    try {
        $UvDownloadReady = $false
        for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -UseBasicParsing -Uri $UvArchiveUrl -OutFile $UvArchive
                $UvDownloadReady = $true
                break
            } catch {
                if (Test-Path -LiteralPath $UvArchive -PathType Leaf) {
                    Remove-Item -LiteralPath $UvArchive -Force
                }
                if ($Attempt -lt 3) {
                    Write-Host "uv download was interrupted. Retrying automatically ($Attempt of 3)..."
                    Start-Sleep -Seconds (2 * $Attempt)
                }
            }
        }
        if (-not $UvDownloadReady) { throw "uv download failed after 3 attempts." }
        $ActualUvArchiveSha256 = (Get-FileHash -LiteralPath $UvArchive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ActualUvArchiveSha256 -ne $UvArchiveSha256) {
            throw "uv failed its pinned SHA-256 check."
        }

        New-Item -ItemType Directory -Path $UvStaging | Out-Null
        Expand-Archive -LiteralPath $UvArchive -DestinationPath $UvStaging
        foreach ($UvFileName in @("uv.exe", "uvw.exe", "uvx.exe")) {
            if (-not (Test-Path -LiteralPath (Join-Path $UvStaging $UvFileName) -PathType Leaf)) {
                throw "The verified uv archive is missing $UvFileName."
            }
        }
        $StagedUvHash = (Get-FileHash -LiteralPath (Join-Path $UvStaging "uv.exe") -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($StagedUvHash -ne $UvBinarySha256) {
            throw "The extracted uv executable failed its pinned SHA-256 check."
        }
        $ExistingUvFiles = @(@("uv.exe", "uvw.exe", "uvx.exe") | Where-Object {
            Test-Path -LiteralPath (Join-Path $ToolsRoot $_)
        })
        if ($ExistingUvFiles.Count -gt 0) {
            $RetiredUvRoot = Get-RecoveryPath "uv-tools"
            New-Item -ItemType Directory -Path $RetiredUvRoot | Out-Null
            foreach ($UvFileName in $ExistingUvFiles) {
                Move-Item -LiteralPath (Join-Path $ToolsRoot $UvFileName) -Destination (Join-Path $RetiredUvRoot $UvFileName)
            }
        }
        # Move uv.exe last so an interrupted extraction can never make the next
        # run mistake a partial tool install for a complete one.
        foreach ($UvFileName in @("uvw.exe", "uvx.exe", "uv.exe")) {
            Move-Item -LiteralPath (Join-Path $UvStaging $UvFileName) -Destination (Join-Path $ToolsRoot $UvFileName)
        }
    } finally {
        if (Test-Path -LiteralPath $UvArchive -PathType Leaf) {
            Remove-Item -LiteralPath $UvArchive -Force
        }
        if (Test-Path -LiteralPath $UvStaging) {
            $RemainingUvFiles = @(Get-ChildItem -LiteralPath $UvStaging -Force)
            if ($RemainingUvFiles.Count -eq 0) {
                Remove-Item -LiteralPath $UvStaging -Force
            } else {
                Move-Item -LiteralPath $UvStaging -Destination (Get-RecoveryPath "incomplete-uv")
            }
        }
    }
}
if (-not (Test-PinnedUv $UvBin)) { throw "The private uv executable failed its pinned binary or version check." }

# Keep uv's Python runtime and cache inside Agent Chat. A user's global uv
# installation can contain stale junctions, Microsoft Store aliases, or other
# machine-specific state that should never decide whether Agent Chat installs.
# UV_PYTHON_PREFERENCE=only-managed makes `uv python find` and `uv venv` ignore
# Microsoft Store aliases and PEP-514 registry interpreters entirely.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $ToolsRoot "python"
$env:UV_CACHE_DIR = Join-Path $ToolsRoot "cache"
$env:UV_PYTHON_PREFERENCE = "only-managed"
New-Item -ItemType Directory -Force -Path $env:UV_PYTHON_INSTALL_DIR, $env:UV_CACHE_DIR | Out-Null

$RequirementsFile = Join-Path $InstallRoot "requirements.lock"
$RequirementsHash = (Get-FileHash -LiteralPath $RequirementsFile -Algorithm SHA256).Hash.ToLowerInvariant()
$RuntimeReady = $false
$RuntimeProvider = ""

# A normal app update should not redownload Python and every dependency when
# the exact locked dependency set is unchanged. Verify the reusable runtime
# before trusting it; a damaged or stale environment still follows the safe
# backup-and-rebuild path below.
if ((Test-Path -LiteralPath $PythonBin -PathType Leaf) -and
    (Test-Path -LiteralPath $RuntimeStamp -PathType Leaf) -and
    (Test-Path -LiteralPath $RuntimeProviderStamp -PathType Leaf)) {
    $SavedRequirementsHash = (Get-Content -LiteralPath $RuntimeStamp -Raw).Trim().ToLowerInvariant()
    $RuntimeProvider = (Get-Content -LiteralPath $RuntimeProviderStamp -Raw).Trim()
    if ($SavedRequirementsHash -eq $RequirementsHash -and $RuntimeProvider -in @("uv-managed-3.12", "python.org-3.12.10")) {
        $RuntimeCheck = Test-PythonLaunch $PythonBin "import fastapi,uvicorn,httpx,cryptography,cffi,pypdf,imageio_ffmpeg,jinja2,multipart,yaml"
        if ($RuntimeCheck.Success) {
            $RuntimeReady = $true
            Write-Host "[PASS] existing private Python runtime matches the locked dependencies"
        }
    }
}

if (-not $RuntimeReady) {
    if (Test-Path -LiteralPath $VenvRoot) {
        $RuntimeBackupPrefix = if ($PriorCoreRunnable) { "runtime" } else { "incomplete-prior-runtime" }
        $PriorRuntimeBackup = Get-RecoveryPath $RuntimeBackupPrefix
        New-Item -ItemType Directory -Path $PriorRuntimeBackup | Out-Null
        Move-Item -LiteralPath $VenvRoot -Destination (Join-Path $PriorRuntimeBackup "venv")
        if (Test-Path -LiteralPath $RuntimeStamp -PathType Leaf) {
            Move-Item -LiteralPath $RuntimeStamp -Destination (Join-Path $PriorRuntimeBackup "requirements.sha256")
        }
        if (Test-Path -LiteralPath $RuntimeProviderStamp -PathType Leaf) {
            Move-Item -LiteralPath $RuntimeProviderStamp -Destination (Join-Path $PriorRuntimeBackup "provider.txt")
        }
    }
    # Install/resolve the concrete managed interpreter before creating the venv.
    # Passing only "3.12" makes uv write a launcher that follows its floating
    # `cpython-3.12-...` junction. Windows can later reject that junction as an
    # untrusted mount point (observed on a real restart), even though the exact
    # versioned interpreter beside it is healthy. Pinning the resolved executable
    # keeps restarts independent of that alias.
    $PythonInstallReady = $false
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $UvBin python install 3.12
            $PythonInstallExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($PythonInstallExitCode -eq 0) {
            $PythonInstallReady = $true
            break
        }
        if ($Attempt -lt 3) {
            Write-Host "Python download was interrupted. Retrying automatically ($Attempt of 3)..."
            Start-Sleep -Seconds (2 * $Attempt)
        }
    }
    if (-not $PythonInstallReady) { throw "Python runtime download failed after 3 attempts. Check the internet connection and run the installer again." }

    $PrivatePythonBin = ""
    $UseOfficialPythonFallback = $false
    $ManagedPythonPolicyBlocked = $false
    $ManagedPythonValidationCode = "import platform,sys;assert sys.version_info[:2]==(3,12);assert platform.architecture()[0]=='64bit'"

    # Probe uv's concrete managed installations before asking `uv python find`.
    # On an App Control-protected PC, `find` launches the unsigned interpreter
    # while inspecting it and fails with error 4551 before it can print a path.
    # Direct enumeration lets us classify that exact native policy error and
    # move to the pinned, PSF-signed fallback without weakening the policy.
    $ManagedPythonRoots = @(Get-ChildItem -LiteralPath $env:UV_PYTHON_INSTALL_DIR -Directory -ErrorAction SilentlyContinue)
    foreach ($ManagedPythonRoot in $ManagedPythonRoots) {
        # Require a concrete patch-version directory. The patchless directory
        # is uv's floating junction, and other managed Python versions may live
        # beside it in this private tools root.
        if ($ManagedPythonRoot.Name -notmatch '^cpython-3\.12\.\d+-windows-x86_64-none$') { continue }
        if (($ManagedPythonRoot.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $Candidate = Join-Path $ManagedPythonRoot.FullName "python.exe"
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }
        $CandidateProbe = Test-PythonLaunch $Candidate $ManagedPythonValidationCode
        if ($CandidateProbe.Success) {
            $PrivatePythonBin = $Candidate
            break
        }
        if ($CandidateProbe.NativeErrorCode -eq 4551) {
            $ManagedPythonPolicyBlocked = $true
        }
    }

    if (-not $PrivatePythonBin -and $ManagedPythonPolicyBlocked) {
        $PrivatePythonBin = Install-OfficialPythonFallback
        $UseOfficialPythonFallback = $true
        $RuntimeProvider = "python.org-3.12.10"
    }

    # Retain uv's normal resolver for installations whose directory layout is
    # unfamiliar but whose interpreter can run under the current policy.
    if (-not $PrivatePythonBin) {
        $PythonFindOutput = @()
        $PythonFindExitCode = -1
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $PythonFindOutput = @(& $UvBin python find 3.12)
            $PythonFindExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($PythonFindExitCode -eq 0) {
            foreach ($Line in $PythonFindOutput) {
                $Candidate = ([string]$Line).Trim()
                if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
                    $CandidateParent = Get-Item -LiteralPath (Split-Path -Parent $Candidate)
                    if (($CandidateParent.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
                    $PrivatePythonBin = $Candidate
                }
            }
        }
    }
    if (-not $PrivatePythonBin -or -not (Test-Path -LiteralPath $PrivatePythonBin -PathType Leaf)) {
        throw "Agent Chat could not resolve its private Python interpreter."
    }

    if (-not $UseOfficialPythonFallback) {
        $ManagedPythonProbe = Test-PythonLaunch $PrivatePythonBin $ManagedPythonValidationCode
        if ($ManagedPythonProbe.NativeErrorCode -eq 4551) {
            $PrivatePythonBin = Install-OfficialPythonFallback
            $UseOfficialPythonFallback = $true
            $RuntimeProvider = "python.org-3.12.10"
        } elseif (-not $ManagedPythonProbe.Success) {
            throw "The managed Python runtime could not start (exit $($ManagedPythonProbe.ExitCode)); this was not an Application Control policy block."
        } else {
            $RuntimeProvider = "uv-managed-3.12"
        }
    }

    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        if (Test-Path -LiteralPath $VenvRoot) {
            $IncompleteRuntime = Get-RecoveryPath ("incomplete-venv-$Attempt")
            Move-Item -LiteralPath $VenvRoot -Destination $IncompleteRuntime
        }
        # Windows PowerShell can promote a native program's stderr into a
        # terminating NativeCommandError when ErrorActionPreference is Stop. uv
        # writes download diagnostics to stderr, so temporarily keep those errors
        # non-terminating and decide success from the real process exit code.
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $RuntimeExitCode = -1
        try {
            if ($UseOfficialPythonFallback) {
                & $PrivatePythonBin -I -m venv --copies $VenvRoot
            } else {
                & $UvBin venv --python $PrivatePythonBin $VenvRoot
            }
            $RuntimeExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($RuntimeExitCode -eq 0) { break }
        if ($Attempt -lt 3) {
            Write-Host "Python environment setup was interrupted. Retrying automatically ($Attempt of 3)..."
            Start-Sleep -Seconds (2 * $Attempt)
        }
    }
    if ($RuntimeExitCode -ne 0) { throw "Python runtime creation failed after 3 attempts. Check the internet connection and run the installer again." }
    if ($UseOfficialPythonFallback -and -not (Test-OfficialPython $PythonBin)) {
        throw "The Python.org fallback created a private environment that failed its signed executable or launch check."
    }

    $DependenciesReady = $false
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $DependenciesExitCode = -1
        try {
            if ($UseOfficialPythonFallback) {
                & $PythonBin -I -m pip install --disable-pip-version-check --only-binary=:all: --require-hashes -r $RequirementsFile
            } else {
                & $UvBin pip install --python $PythonBin --require-hashes -r $RequirementsFile
            }
            $DependenciesExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($DependenciesExitCode -eq 0) {
            $DependenciesReady = $true
            break
        }
        if ($Attempt -lt 3) {
            Write-Host "A dependency download was interrupted. Retrying automatically ($Attempt of 3)..."
            Start-Sleep -Seconds (2 * $Attempt)
        }
    }
    if (-not $DependenciesReady) { throw "Dependency installation failed after 3 attempts. Check the internet connection and run the installer again." }
    $InstalledRuntimeProbe = Test-PythonLaunch $PythonBin "import fastapi,uvicorn,httpx,cryptography,cffi,pypdf,imageio_ffmpeg,jinja2,multipart,yaml"
    if (-not $InstalledRuntimeProbe.Success) {
        throw "The installed Python runtime failed its compiled dependency launch check (exit $($InstalledRuntimeProbe.ExitCode), Windows error $($InstalledRuntimeProbe.NativeErrorCode))."
    }
    Set-Content -LiteralPath $RuntimeStamp -Value $RequirementsHash -Encoding ASCII
    Set-Content -LiteralPath $RuntimeProviderStamp -Value $RuntimeProvider -Encoding ASCII
}

if (-not (Test-Path -LiteralPath $ConfigFile)) {
    $ConfigCreatedThisRun = $true
    & $PythonBin (Join-Path $InstallRoot "scripts\community\create_config.py") --output $ConfigFile --display-name $DisplayName --admin-email $AdminEmail --port $Port
    if ($LASTEXITCODE -ne 0) { throw "Configuration creation failed." }
} else {
    Write-Host "Keeping the existing private configuration."
}

& $PythonBin (Join-Path $InstallRoot "scripts\community\doctor.py") --platform windows --config $ConfigFile --data-dir (Join-Path $CommunityHome "data")
if ($LASTEXITCODE -ne 0) { throw "Agent Chat preflight checks failed." }
$CoreInstallComplete = $true
} catch {
    $InstallFailure = $_
    try {
        if (Test-Path -LiteralPath $InstallRoot) {
            $FailedAppBackup = Get-RecoveryPath "failed-install"
            Move-Item -LiteralPath $InstallRoot -Destination $FailedAppBackup
        }

        $ShouldRetireCurrentRuntime = (-not $PriorCoreRunnable) -or [bool]$PriorRuntimeBackup
        if ($ShouldRetireCurrentRuntime -and (
            (Test-Path -LiteralPath $VenvRoot) -or
            (Test-Path -LiteralPath $RuntimeStamp -PathType Leaf) -or
            (Test-Path -LiteralPath $RuntimeProviderStamp -PathType Leaf)
        )) {
            $FailedRuntimeBackup = Get-RecoveryPath "failed-runtime"
            New-Item -ItemType Directory -Path $FailedRuntimeBackup | Out-Null
            if (Test-Path -LiteralPath $VenvRoot) {
                Move-Item -LiteralPath $VenvRoot -Destination (Join-Path $FailedRuntimeBackup "venv")
            }
            if (Test-Path -LiteralPath $RuntimeStamp -PathType Leaf) {
                Move-Item -LiteralPath $RuntimeStamp -Destination (Join-Path $FailedRuntimeBackup "requirements.sha256")
            }
            if (Test-Path -LiteralPath $RuntimeProviderStamp -PathType Leaf) {
                Move-Item -LiteralPath $RuntimeProviderStamp -Destination (Join-Path $FailedRuntimeBackup "provider.txt")
            }
        }

        if ($ConfigCreatedThisRun -and (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
            $FailedConfigBackup = Get-RecoveryPath "failed-config"
            Move-Item -LiteralPath $ConfigFile -Destination $FailedConfigBackup
        }

        if ($PriorCoreRunnable) {
            if (-not $PriorAppBackup -or -not (Test-Path -LiteralPath $PriorAppBackup)) {
                throw "The prior runnable app backup is unavailable."
            }
            Move-Item -LiteralPath $PriorAppBackup -Destination $InstallRoot
            if ($PriorRuntimeBackup) {
                $SavedVenv = Join-Path $PriorRuntimeBackup "venv"
                $SavedRuntimeStamp = Join-Path $PriorRuntimeBackup "requirements.sha256"
                if (-not (Test-Path -LiteralPath $SavedVenv) -or -not (Test-Path -LiteralPath $SavedRuntimeStamp -PathType Leaf)) {
                    throw "The prior runnable runtime backup is incomplete."
                }
                Move-Item -LiteralPath $SavedVenv -Destination $VenvRoot
                Move-Item -LiteralPath $SavedRuntimeStamp -Destination $RuntimeStamp
                $SavedProviderStamp = Join-Path $PriorRuntimeBackup "provider.txt"
                if (Test-Path -LiteralPath $SavedProviderStamp -PathType Leaf) {
                    Move-Item -LiteralPath $SavedProviderStamp -Destination $RuntimeProviderStamp
                }
            }
            Write-Warning "The update failed, so the previous runnable Agents Chat core was restored."
        } else {
            Write-Warning "The install failed before a runnable core was ready. Incomplete files were moved to the recovery backups folder."
        }
    } catch {
        $RollbackFailure = $_
        throw "Agents Chat installation failed: $($InstallFailure.Exception.Message) Recovery also failed: $($RollbackFailure.Exception.Message)"
    }
    throw $InstallFailure
}

$OpenScript = Join-Path $InstallRoot "scripts\community\open_windows.ps1"
$UninstallScript = Join-Path $InstallRoot "scripts\community\uninstall_windows.ps1"
$IconPath = Join-Path $InstallRoot "static\favicon.ico"
$PowerShellTarget = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
New-Item -ItemType Directory -Force -Path $StartupRoot, $StartMenuRoot | Out-Null
# The Startup entry launches through a generated PowerShell wrapper because
# cmd's `start` builtin cannot redirect the detached child's output - a bare
# `start ... 1>>log` binds the redirect to `start` itself and captures nothing.
# Start-Process with -RedirectStandardOutput/-RedirectStandardError (the same
# pattern open_windows.ps1 uses) makes login-time startup failures diagnosable.
#
# The wrapper is emitted as fixed, path-free source held in a literal here-string:
# it recomputes the install home from its own $PSScriptRoot and the port from
# config.env at login. No install-time value is ever pasted into executable
# syntax, so a legal profile path containing an apostrophe, a dollar sign, or a
# backtick cannot break the generated script, and the saved port keeps exactly
# one source of truth. It also carries its own redirect log filenames: the
# separate FILENAMES are what stop the two launchers from reopening (and
# truncating) each other's live redirect targets. The already-running guard is
# only best effort on top of that - the port is not bound until tens of seconds
# after launch (doctor.py, then importing the app), so two launches inside that
# window still both spawn.
#
# Because the wrapper itself runs with -WindowStyle Hidden, its own stdout and
# stderr go nowhere, so every path that ends without starting the server also
# appends a line to a dedicated append-only status log. That log is never a
# Start-Process redirect target, so a concurrent launcher cannot truncate it.
$AutostartScript = Join-Path $CommunityHome "autostart.ps1"
$AutostartBody = @'
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Generated by the Agent Chat Community Edition installer. Do not edit: every
# path is recomputed from this script's own location at login.
$CommunityHome = $PSScriptRoot
$env:AGENT_CHAT_HOME = $CommunityHome
$ConfigFile = Join-Path $CommunityHome "config.env"
$RunScript = Join-Path $CommunityHome "app\scripts\community\run_windows.ps1"
$LogsRoot = Join-Path $CommunityHome "logs"
$OutputLog = Join-Path $LogsRoot "agent-chat.autostart.log"
$ErrorLog = Join-Path $LogsRoot "agent-chat.autostart.error.log"
# Append-only, and deliberately never a Start-Process redirect target, so a
# concurrent launcher cannot truncate it. This wrapper runs hidden, so this file
# is the only evidence a user ever gets that autostart ran and gave up.
$StatusLog = Join-Path $LogsRoot "agent-chat.autostart.status.log"

function Write-AutostartStatus([string]$Message) {
    try {
        New-Item -ItemType Directory -Force -Path $LogsRoot | Out-Null
        $Stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        Add-Content -LiteralPath $StatusLog -Value "$Stamp $Message" -Encoding UTF8
    } catch {
        # Diagnostics must never be the reason login autostart fails.
    }
}

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

# A bare TCP connect only proves *something* is listening. Skipping on that
# alone means any unrelated process that later squats on the saved port makes
# autostart skip forever. Agents Chat answers /api/health with 401 when it is
# healthy and auth is on (and with its own JSON payload when auth is off), so a
# real HTTP answer of that shape is good evidence. Anything else - no answer, a
# timeout, a foreign listener - is treated as "not Agents Chat", because
# starting a second server that fails to bind is self-evident and logged,
# whereas never starting at all is invisible.
function Test-AgentChatOnPort([int]$PortNumber) {
    $Code = 0
    $Body = ""
    $Response = $null
    try {
        $Request = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create("http://127.0.0.1:$PortNumber/api/health")
        $Request.Method = "GET"
        $Request.Proxy = $null
        $Request.Timeout = 2000
        $Request.ReadWriteTimeout = 2000
        $Request.AllowAutoRedirect = $false
        $Response = $Request.GetResponse()
    } catch [System.Net.WebException] {
        $Response = $_.Exception.Response
    } catch {
        return $false
    }
    if ($null -eq $Response) { return $false }
    try {
        $Code = [int]$Response.StatusCode
        if ($Code -eq 200) {
            $Reader = New-Object System.IO.StreamReader($Response.GetResponseStream())
            try { $Body = $Reader.ReadToEnd() } finally { $Reader.Close() }
        }
    } catch {
        return $false
    } finally {
        $Response.Close()
    }
    if ($Code -eq 401) { return $true }
    return ($Code -eq 200 -and $Body -like '*auth_required*')
}

$Port = Get-ConfiguredPort $ConfigFile
if ($Port -lt 1) {
    Write-AutostartStatus "FAILED: config.env holds no valid saved local address; nothing was started."
    throw "Agents Chat does not have a valid saved local address. Run the installer again."
}
if (-not (Test-Path -LiteralPath $RunScript -PathType Leaf)) {
    Write-AutostartStatus "FAILED: not installed for this Windows user (missing $RunScript); nothing was started."
    throw "Agents Chat is not installed for this Windows user."
}

# The Desktop or Start menu shortcut may already have started the server during
# the login window. Do not spawn a second one when the port demonstrably belongs
# to Agents Chat. When it does not, start anyway and say so.
$PortIsBusy = Test-LocalPort $Port
if ($PortIsBusy -and (Test-AgentChatOnPort $Port)) {
    Write-AutostartStatus "SKIPPED: Agents Chat already answering on port $Port."
    exit 0
}
if ($PortIsBusy) {
    Write-AutostartStatus "WARNING: port $Port is held by something that did not answer as Agents Chat; starting anyway."
} else {
    Write-AutostartStatus "STARTING: nothing is listening on port $Port."
}

$env:PORT = [string]$Port
New-Item -ItemType Directory -Force -Path $LogsRoot | Out-Null
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
Start-Process $PowerShellExe -WindowStyle Hidden -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $RunScript + '"')
) -RedirectStandardOutput $OutputLog -RedirectStandardError $ErrorLog
'@
# Emit the BOM explicitly so the wrapper decodes correctly no matter which
# PowerShell reads it, rather than relying on the shipped .cmd forcing
# powershell.exe.
$Utf8WithBom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllLines((Get-AbsolutePath $AutostartScript), ($AutostartBody -split "`r?`n"), $Utf8WithBom)

$StartupTarget = Get-CmdSafePath $AutostartScript
$StartupCommandPath = if ($StartupTarget) { $StartupTarget } else { $AutostartScript }
# cmd.exe expands "%" before it considers quoting, so a literal "%" in the
# profile path (a legal account-name character) would be swallowed and the
# wrapper would be launched from a path that does not exist. Doubling it is the
# batch-file escape. Resolve powershell.exe through %SystemRoot% as well, so the
# Startup entry can never pick up a powershell.exe planted in the working
# directory that cmd searches first.
$StartupCommandPathForCmd = $StartupCommandPath -replace '%', '%%'
$StartupLines = @(
    "@echo off",
    "start `"`" /min `"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartupCommandPathForCmd`""
)
if ($StartupTarget) {
    Set-Content -LiteralPath $StartupFile -Value $StartupLines -Encoding ASCII
} else {
    # No ASCII-safe spelling of the install path exists. Write the batch file in
    # the codepage cmd.exe actually reads instead of letting ASCII replace every
    # accented character with "?", and say so loudly if even that cannot hold it.
    $OemEncoding = Get-OemEncoding
    if ($OemEncoding -and $OemEncoding.GetString($OemEncoding.GetBytes($AutostartScript)) -eq $AutostartScript) {
        [System.IO.File]::WriteAllLines((Get-AbsolutePath $StartupFile), $StartupLines, $OemEncoding)
    } else {
        [System.IO.File]::WriteAllLines((Get-AbsolutePath $StartupFile), $StartupLines, (New-Object System.Text.UTF8Encoding($false)))
        Write-Warning "Agents Chat cannot write a reliable Startup entry for this install path: $AutostartScript"
        Write-Warning "Automatic start at login may not work. Open Agents Chat from the Desktop or Start menu shortcut instead."
    }
}

$OpenArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$OpenScript`""
$UninstallArguments = "-NoExit -NoProfile -ExecutionPolicy Bypass -File `"$UninstallScript`""
New-AgentChatShortcut -Path (Join-Path $StartMenuRoot "Agents Chat.lnk") `
    -Target $PowerShellTarget -Arguments $OpenArguments -WorkingDirectory $InstallRoot `
    -IconPath $IconPath -WindowStyle 7
# The uninstaller's working directory must NOT be inside the tree it removes:
# Windows keeps a process's current directory open, so Move-Item on "app" would
# fail with access-denied and (with $ErrorActionPreference = "Stop") abort the
# uninstall after the shortcuts had already been removed.
New-AgentChatShortcut -Path (Join-Path $StartMenuRoot "Uninstall Agents Chat.lnk") `
    -Target $PowerShellTarget -Arguments $UninstallArguments -WorkingDirectory $env:SystemRoot `
    -IconPath $IconPath
if ($DesktopShortcut) {
    New-AgentChatShortcut -Path $DesktopShortcut -Target $PowerShellTarget `
        -Arguments $OpenArguments -WorkingDirectory $InstallRoot -IconPath $IconPath -WindowStyle 7
}

if ($NoStart) {
    Write-Host "Agents Chat is installed. Open it from the Desktop or Start menu shortcut."
    exit 0
}

$env:PORT = [string]$Port
& $OpenScript -Port $Port
Write-Host "Agents Chat Community Edition is installed and ready at http://127.0.0.1:$Port/"
Write-Host "You can reopen it later from the Desktop or Start menu."
