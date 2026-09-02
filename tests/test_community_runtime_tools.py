import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

from scripts import _bridge_common


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def test_bridge_context_store_recreates_missing_parent_after_reinstall(tmp_path):
    store = tmp_path / "codex_bridge" / "contexts.json"

    _bridge_common.save_contexts(store, {"existing": {"messages": []}})
    context_id, context = _bridge_common.get_or_create_context(store, "fresh123")

    assert context_id == "fresh123"
    assert context["messages"] == []
    assert store.is_file()
    assert set(json.loads(store.read_text(encoding="utf-8"))) == {"existing", "fresh123"}


def test_config_creator_generates_private_non_placeholder_config(tmp_path):
    destination = tmp_path / "config.env"
    result = subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts" / "community" / "create_config.py"),
            "--output",
            str(destination),
            "--display-name",
            "Test Owner",
            "--port",
            "18086",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    text = destination.read_text(encoding="utf-8")

    assert "Created private configuration" in result.stdout
    assert "your name, a username or email, and password" in result.stdout
    assert "admin@localhost" not in result.stdout
    assert "Temporary password:" not in result.stdout
    assert "ORCHESTRATOR_ACCESS_TOKEN=GENERATED_BY_INSTALLER" not in text
    password_line = next(
        line for line in text.splitlines()
        if line.startswith("ORCHESTRATOR_ADMIN_PASSWORD=")
    )
    assert len(password_line.split("=", 1)[1]) >= 20
    assert "USER_DISPLAY_NAME=Test Owner" in text
    assert "COMMUNITY_FIRST_RUN_SETUP=1" in text
    assert "ORCHESTRATOR_PUBLIC_URL=http://127.0.0.1:18086" in text
    assert "ALLOWED_ORIGINS=http://127.0.0.1:18086,http://localhost:18086" in text
    # Windows does not expose POSIX owner/group/other permission bits. The
    # Windows installer applies its native ACLs; keep this chmod assertion on
    # platforms where chmod has the semantics the helper is implementing.
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_doctor_passes_for_generated_config_and_temp_data(tmp_path):
    config = tmp_path / "config.env"
    data_dir = tmp_path / "data"
    subprocess.run(
        [str(PYTHON), str(ROOT / "scripts/community/create_config.py"), "--output", str(config)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts/community/doctor.py"),
            "--config",
            str(config),
            "--data-dir",
            str(data_dir),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert report["checks"]
    assert {item["status"] for item in report["checks"]} == {"pass"}


def test_macos_launcher_is_loopback_only_and_uses_external_state():
    launcher = (ROOT / "scripts/community/run_macos.sh").read_text(encoding="utf-8")

    assert "--host 127.0.0.1" in launcher
    assert "AGENT_CHAT_ENV_FILE" in launcher
    assert "Application Support/Agent Chat" in launcher
    assert 'export DATA_DIR="${COMMUNITY_DATA_DIR}"' in launcher
    assert 'export AGENT_CHAT_DB="${COMMUNITY_DATA_DIR}/agent-chat.sqlite3"' in launcher
    assert "migrate_community_db.py" in launcher
    assert 'export CALENDARS_STORE="${COMMUNITY_DATA_DIR}/calendars.json"' in launcher
    assert 'export JARVIS_FOREMAN_CONFIG="${COMMUNITY_DATA_DIR}/jarvis_foreman.json"' in launcher
    assert 'export AGENT_WORKSPACES_ROOT="${COMMUNITY_DATA_DIR}/workspaces"' in launcher
    assert 'export PROJECTS_FOLDER_ROOT="${COMMUNITY_DATA_DIR}/projects"' in launcher
    assert 'export WORKSPACE_DISCOVER_ROOTS="${COMMUNITY_DATA_DIR}/projects"' in launcher
    assert "0.0.0.0" not in launcher


def test_legacy_community_database_is_migrated_with_sidecars(tmp_path):
    legacy_name = "".join(("neuro", "blend", "_v5_15.sqlite3"))
    old = tmp_path / legacy_name
    old.write_bytes(b"database")
    (tmp_path / f"{legacy_name}-wal").write_bytes(b"wal")
    (tmp_path / f"{legacy_name}-shm").write_bytes(b"shm")
    subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts/community/migrate_community_db.py"),
            "--data-dir", str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not old.exists()
    assert (tmp_path / "agent-chat.sqlite3").read_bytes() == b"database"
    assert (tmp_path / "agent-chat.sqlite3-wal").read_bytes() == b"wal"
    assert (tmp_path / "agent-chat.sqlite3-shm").read_bytes() == b"shm"


def test_shipped_bridge_launchers_refuse_non_loopback_hosts():
    launchers = {
        "Codex": "run_codex_bridge.sh",
        "Claude": "run_claude_bridge.sh",
        "Grok": "run_grok_bridge.sh",
        "MiniMax": "run_minimax_bridge.sh",
        "Antigravity": "run_antigravity_bridge.sh",
        "OpenRouter": "run_openrouter_bridge.sh",
    }
    for label, filename in launchers.items():
        text = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
        assert '"127.0.0.1"' in text, label
        assert '"localhost"' in text, label
        assert '"::1"' in text, label
        assert "Refusing to expose" in text, label


def test_macos_app_creator_builds_a_user_launcher(tmp_path):
    opener = tmp_path / "open_macos.sh"
    opener.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    opener.chmod(0o755)
    output = tmp_path / "Agents Chat.app"
    subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts/community/create_macos_app.py"),
            "--output", str(output),
            "--opener", str(opener),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with (output / "Contents/Info.plist").open("rb") as stream:
        payload = plistlib.load(stream)
    executable = output / "Contents/MacOS/Agents Chat"
    assert payload["CFBundleIdentifier"] == "chat.agents.community"
    assert str(opener) in executable.read_text(encoding="utf-8")
    # Windows can still validate the generated bundle contents, but it cannot
    # represent the Unix executable bit used by the macOS launcher.
    if os.name != "nt":
        assert executable.stat().st_mode & stat.S_IXUSR


def test_launchd_plist_is_user_scoped_and_loopback_launcher_backed(tmp_path):
    output = tmp_path / "com.agentchat.community.plist"
    community_home = tmp_path / "Agent Chat"
    subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts/community/create_launchd_plist.py"),
            "--output",
            str(output),
            "--app-root",
            str(ROOT),
            "--community-home",
            str(community_home),
            "--python",
            str(PYTHON),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with output.open("rb") as stream:
        payload = plistlib.load(stream)

    assert payload["Label"] == "com.agentchat.community"
    assert payload["ProgramArguments"] == [str(ROOT / "scripts/community/run_macos.sh")]
    assert payload["EnvironmentVariables"]["AGENT_CHAT_HOME"] == str(community_home)
    assert payload["EnvironmentVariables"]["AGENT_CHAT_PYTHON"] == str(PYTHON.absolute())
    assert payload["EnvironmentVariables"]["PORT"] == "8086"
    assert payload["RunAtLoad"] is True


def test_launchd_plist_accepts_isolated_label_and_port(tmp_path):
    output = tmp_path / "acceptance.plist"
    subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts/community/create_launchd_plist.py"),
            "--output", str(output),
            "--app-root", str(ROOT),
            "--community-home", str(tmp_path / "Agent Chat Acceptance"),
            "--python", str(PYTHON),
            "--label", "com.agentchat.community.acceptance",
            "--port", "18086",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with output.open("rb") as stream:
        payload = plistlib.load(stream)
    assert payload["Label"] == "com.agentchat.community.acceptance"
    assert payload["EnvironmentVariables"]["PORT"] == "18086"


def test_launchd_plist_preserves_virtualenv_python_symlink(tmp_path):
    real_python = tmp_path / "python-real"
    real_python.write_text("placeholder", encoding="utf-8")
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(real_python)
    output = tmp_path / "venv.plist"
    subprocess.run(
        [
            str(PYTHON),
            str(ROOT / "scripts/community/create_launchd_plist.py"),
            "--output", str(output),
            "--app-root", str(ROOT),
            "--community-home", str(tmp_path / "Agent Chat"),
            "--python", str(venv_python),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with output.open("rb") as stream:
        payload = plistlib.load(stream)
    assert payload["EnvironmentVariables"]["AGENT_CHAT_PYTHON"] == str(venv_python)


def test_installer_requires_release_marker_and_uses_recoverable_updates():
    installer = (ROOT / "scripts/community/install_macos.sh").read_text(encoding="utf-8")
    uninstaller = (ROOT / "scripts/community/uninstall_macos.sh").read_text(encoding="utf-8")
    opener = (ROOT / "scripts/community/open_macos.sh").read_text(encoding="utf-8")

    assert ".community-release.json" in installer
    assert "AGENT_CHAT_ALLOW_DEV_INSTALL" in installer
    assert "--require-hashes" in installer
    assert "requirements.sha256" in installer
    assert "existing private Python runtime matches the locked dependencies" in installer
    assert "SAVED_REQUIREMENTS_HASH" in installer
    assert "RUNTIME_READY" in installer
    assert "import fastapi, uvicorn, httpx" in installer
    assert 'UV_TARGET="aarch64-apple-darwin"' in installer
    assert 'UV_TARGET="x86_64-apple-darwin"' in installer
    assert "ed336d0ba49db8ef89b2b41fffa372ce63bd032f22a56f001c265891aec32829" in installer
    assert "77f5ca26c0de20e992a3677a174fe1121ee25c36f9b1434a863f75bf077a05eb" in installer
    assert "UV_BINARY_SHA256" in installer
    assert "releases.astral.sh/github/uv/releases/download" in installer
    assert "command -v uv" not in installer
    assert "install.sh" not in installer
    assert "--host 127.0.0.1" in (ROOT / "scripts/community/run_macos.sh").read_text(encoding="utf-8")
    assert "mv \"${INSTALL_ROOT}\"" in installer
    assert 'mv "${RUNTIME_VENV}" "${BACKUP_ROOT}/venv-${TIMESTAMP}"' in installer
    assert "find_available_port" in installer
    assert "automatically selected private local port" in installer
    assert "create_macos_app.py" in installer
    assert "Agents Chat.app" in installer
    assert "PERPLEXITY_HELPER_APP" not in installer
    assert "com.apple.quarantine" not in installer
    assert "127.0.0.1" in opener
    assert "launchctl kickstart" in opener
    assert "Agents Chat.app" in uninstaller
    assert "stop_installed_processes" in installer
    assert "stop_installed_processes" in uninstaller
    assert "/bin/ps -axo pid=,command=" in installer
    assert "/bin/ps -axo pid=,command=" in uninstaller
    assert 'index($0, home)' in installer
    assert 'index($0, home)' in uninstaller
    assert "rm -rf" not in installer
    assert "rm -rf" not in uninstaller
    assert "${HOME}/.Trash" in uninstaller
    assert 'rmdir "${COMMUNITY_HOME}"' in uninstaller


def test_windows_launcher_is_loopback_only_and_uses_external_state():
    launcher = (ROOT / "scripts/community/run_windows.ps1").read_text(encoding="utf-8")

    assert "--host 127.0.0.1" in launcher
    assert "AGENT_CHAT_ENV_FILE" in launcher
    assert "LOCALAPPDATA" in launcher
    assert "AGENT_CHAT_DB" in launcher
    assert "CALENDARS_STORE" in launcher
    assert "JARVIS_FOREMAN_CONFIG" in launcher
    assert "AGENT_WORKSPACES_ROOT" in launcher
    assert "PROJECTS_FOLDER_ROOT" in launcher
    assert "WORKSPACE_DISCOVER_ROOTS" in launcher
    assert "0.0.0.0" not in launcher


def test_windows_installer_is_user_scoped_locked_and_recoverable():
    installer = (ROOT / "scripts/community/install_windows.ps1").read_text(encoding="utf-8")
    uninstaller = (ROOT / "scripts/community/uninstall_windows.ps1").read_text(encoding="utf-8")
    opener = (ROOT / "scripts/community/open_windows.ps1").read_text(encoding="utf-8")

    assert ".community-release.json" in installer
    assert "--require-hashes" in installer
    assert "LOCALAPPDATA" in installer
    assert "current-user Startup folder available" in installer
    assert "Agent Chat Community.cmd" in installer
    assert '[int]$Port = 0' in installer
    assert "Find-AvailablePort" in installer
    assert "automatically selected private local port" in installer
    assert '--port $Port' in installer
    # The Startup entry routes through a generated autostart.ps1 wrapper so the
    # detached server's output is actually captured (cmd's `start` builtin
    # cannot redirect its child).
    #
    # The wrapper body is a SINGLE-quoted here-string, so no install-time value is
    # ever interpolated into generated code: a profile path containing an
    # apostrophe (C:\Users\O'Brien), a $ or a backtick would otherwise break the
    # generated script's syntax and silently kill login autostart. Everything the
    # wrapper needs is recomputed at runtime instead -- paths from $PSScriptRoot,
    # the port from config.env -- so there is one source of truth with the other
    # launchers.
    assert "$AutostartBody = @'" in installer
    assert "$CommunityHome = $PSScriptRoot" in installer
    assert "$Port = Get-ConfiguredPort $ConfigFile" in installer
    # Autostart must not fight the shortcut launcher, but a bare TCP connect only
    # proves *something* is listening: an unrelated squatter on the saved port
    # used to make autostart skip forever and silently. The skip now has to prove
    # the port really is Agents Chat (/api/health answers 401 when auth is on),
    # and anything short of that starts the server anyway.
    assert "function Test-AgentChatOnPort([int]$PortNumber)" in installer
    assert "/api/health" in installer
    assert "if ($PortIsBusy -and (Test-AgentChatOnPort $Port)) {" in installer
    # The probe must not be able to hang the login.
    assert "$Request.Timeout = 2000" in installer
    assert "$Request.ReadWriteTimeout = 2000" in installer
    # The wrapper runs hidden, so its own stdout/stderr are discarded: every path
    # that ends without starting the server has to leave a trace in a dedicated
    # append-only status log that is never a Start-Process redirect target (a
    # redirect target gets truncated by a concurrent launcher).
    assert "agent-chat.autostart.status.log" in installer
    assert "function Write-AutostartStatus([string]$Message)" in installer
    assert "Add-Content -LiteralPath $StatusLog" in installer
    assert 'Write-AutostartStatus "SKIPPED: Agents Chat already answering on port $Port."' in installer
    assert 'Write-AutostartStatus "FAILED: config.env holds no valid saved local address' in installer
    assert 'Write-AutostartStatus "FAILED: not installed for this Windows user' in installer
    # Separate log FILENAMES are what actually stop the two launchers from
    # truncating each other; the port guard is only best effort.
    assert "agent-chat.autostart.log" in installer
    assert "agent-chat.autostart.error.log" in installer
    assert "-RedirectStandardOutput" in installer
    assert "-RedirectStandardError" in installer
    assert "autostart.ps1" in installer
    assert "& $OpenScript -Port $Port" in installer
    assert "Agents Chat.lnk" in installer
    assert "Uninstall Agents Chat.lnk" in installer
    assert "WScript.Shell" in installer
    assert "AGENT_CHAT_STARTUP_FILE" in installer
    assert "AGENT_CHAT_START_MENU_ROOT" in installer
    assert "AGENT_CHAT_DESKTOP_SHORTCUT" in installer
    assert "UV_PYTHON_INSTALL_DIR" in installer
    assert 'Join-Path $ToolsRoot "python"' in installer
    assert "UV_CACHE_DIR" in installer
    # only-managed is uv's real knob for ignoring Microsoft Store aliases and
    # PEP-514 registry interpreters (UV_PYTHON_NO_REGISTRY was never a real
    # uv setting and silently did nothing).
    assert '$env:UV_PYTHON_PREFERENCE = "only-managed"' in installer
    assert "requirements.sha256" in installer
    assert "provider.txt" in installer
    assert "existing private Python runtime matches the locked dependencies" in installer
    assert "SavedRequirementsHash" in installer
    assert "RuntimeCheck = Test-PythonLaunch" in installer
    assert "ToBase64String" in installer
    assert "b64decode" in installer
    assert "System.Diagnostics.ProcessStartInfo" in installer
    assert "$StartInfo.UseShellExecute = $false" in installer
    assert "if (-not $Process.Start())" in installer
    assert "Start-Process -FilePath $Path" not in installer
    assert '$UvBin = Join-Path $ToolsRoot "uv.exe"' in installer
    assert "uv-x86_64-pc-windows-msvc.zip" in installer
    assert "acfde570451cfdb8689fa159a138ee805ba4e241c466432750302c86254b0984" in installer
    assert "23cf0f8194ff576562646a1a2950c6826249c8806cd1547debd24db77eb68f58" in installer
    assert "ActualUvArchiveSha256" in installer
    assert "StagedUvHash" in installer
    assert "Invoke-Expression" not in installer
    assert "function Test-PinnedUv" in installer
    assert 'Get-RecoveryPath "uv-tools"' in installer
    assert '$Attempt -le 3' in installer
    assert "Python download was interrupted. Retrying automatically" in installer
    assert "python find 3.12" in installer
    assert "ManagedPythonPolicyBlocked" in installer
    direct_probe = "Get-ChildItem -LiteralPath $env:UV_PYTHON_INSTALL_DIR"
    assert direct_probe in installer
    assert installer.index(direct_probe) < installer.index("python find 3.12")
    assert r"^cpython-3\.12\.\d+-windows-x86_64-none$" in installer
    assert "[System.IO.FileAttributes]::ReparsePoint" in installer
    assert "sys.version_info[:2]==(3,12)" in installer
    assert "platform.architecture()[0]=='64bit'" in installer
    assert "--python $PrivatePythonBin" in installer
    assert "floating" in installer and "junction" in installer
    assert "Python environment setup was interrupted. Retrying automatically" in installer
    assert "A dependency download was interrupted. Retrying automatically" in installer
    assert 'ErrorActionPreference = "Continue"' in installer
    assert "$RuntimeExitCode = $LASTEXITCODE" in installer
    assert "$DependenciesExitCode = $LASTEXITCODE" in installer
    assert "Python runtime creation failed after 3 attempts" in installer
    assert "Dependency installation failed after 3 attempts" in installer
    assert "incomplete-venv-" in installer
    # Enterprise Application Control can reject uv's unsigned managed CPython.
    # Fall back only for the exact Win32 policy error, then verify both a pinned
    # Python.org hash and the PSF Authenticode publisher before execution.
    assert "NativeErrorCode -eq 4551" in installer
    assert "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" in installer
    assert "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb" in installer
    assert "Get-AuthenticodeSignature" in installer
    assert '"Python Software Foundation"' in installer
    assert "Test-OfficialPython" in installer
    assert "function Get-RegisteredPython312Roots" in installer
    assert "function Test-PathInsideCommunityHome" in installer
    assert "HKCU:\\Software\\Python\\PythonCore" in installer
    assert "CurrentVersion\\Uninstall" in installer
    assert "will not modify it automatically" in installer
    assert "-m venv --copies" in installer
    assert "--only-binary=:all:" in installer
    assert '"python.org-3.12.10"' in installer
    assert r"Programs\Python\Python312" in installer
    assert "shared current-user Python runtime remains available" in installer
    assert "Unblock-File" not in installer
    assert "Set-MpPreference" not in installer
    # The app swap and runtime rebuild are recoverable. A failed candidate is
    # retired, and only a previously verified runnable app/runtime pair is
    # restored to the active paths.
    assert "function Get-RecoveryPath([string]$Prefix)" in installer
    assert "function Test-PriorCoreRunnable" in installer
    assert 'Get-RecoveryPath "failed-install"' in installer
    assert 'Get-RecoveryPath "failed-runtime"' in installer
    assert 'Get-RecoveryPath "failed-config"' in installer
    assert "$PriorCoreRunnable" in installer
    assert "$PriorRuntimeBackup" in installer
    assert "the previous runnable Agents Chat core was restored" in installer
    assert "incomplete-prior-app" in installer
    assert "Get-ConfiguredPort" in opener
    assert 'Start-Process "http://127.0.0.1:$Port/"' in opener
    assert "RedirectStandardOutput" in opener
    assert "RedirectStandardError" in opener
    assert "Last diagnostic" in opener
    assert "agent-chat.error.log" in opener
    assert "schtasks.exe" not in installer
    assert "RunAsAdministrator" not in installer
    assert "Remove-Item -Recurse" not in installer
    assert "Agent Chat Removed" in uninstaller
    assert "[string]$Home" not in installer
    assert "[string]$InstallHome" in installer
    assert "Agent Chat Community.cmd" in uninstaller
    assert "Get-NetTCPConnection" in uninstaller
    assert "taskkill.exe" in uninstaller
    assert "Start-Process -FilePath taskkill.exe" in uninstaller
    assert "$Killer.ExitCode -ne 0" in uninstaller
    assert "/T" in uninstaller
    assert "/F" in uninstaller
    assert "StillRunning" in uninstaller
    assert "StartMenuRoot" in uninstaller
    assert "DesktopShortcut" in uninstaller
    assert "AGENT_CHAT_START_MENU_ROOT" in uninstaller
    assert "Get-ChildItem -LiteralPath $CommunityHome -Force" in uninstaller
    assert "Remove-Item -LiteralPath $CommunityHome -Force" in uninstaller
    assert "unexpected files remain" in uninstaller
    assert "Remove-Item -Recurse" not in uninstaller
    # The generated autostart wrapper must be purged with everything else, or
    # -PurgeData trips its own "unexpected files remain" guard. This regressed
    # once already, so pin it.
    assert '"autostart.ps1"' in uninstaller
    assert '"logs"' in uninstaller
    # Windows keeps a process's current directory open, so the uninstaller has
    # to step outside the tree it is about to move, and the shortcut that
    # launches it must not point back into that tree either.
    assert "Set-Location -LiteralPath $env:SystemRoot" in uninstaller
    assert "[Environment]::CurrentDirectory = $env:SystemRoot" in uninstaller
    assert "-Arguments $UninstallArguments -WorkingDirectory $env:SystemRoot" in installer
    # The .cmd encoding fallbacks and the wrapper's BOM are load-bearing for
    # non-ASCII profile paths; a silent drop would break login autostart.
    assert "Get-CmdSafePath" in installer
    assert "Get-OemEncoding" in installer
    assert "New-Object System.Text.UTF8Encoding($true)" in installer
    # cmd.exe eats a literal "%" even inside quotes, and resolves a bare
    # powershell.exe against the working directory first.
    assert "-replace '%', '%%'" in installer
    assert "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" in installer


def test_doctor_default_home_matches_the_installers():
    doctor = (ROOT / "scripts/community/doctor.py").read_text(encoding="utf-8")
    assert '/ "Agent Chat"' in doctor
    assert '/ "Agents Chat"' not in doctor


def test_double_click_installers_send_first_login_to_the_browser():
    mac = (ROOT / "Install Agents Chat on Mac.command").read_text(encoding="utf-8")
    windows = (ROOT / "Install Agents Chat on Windows.cmd").read_text(encoding="utf-8")

    assert "install_macos.sh" in mac
    assert "create your own password" in mac
    assert "Save the email and temporary password" not in mac
    assert "Press any key to close" in mac
    assert "install_windows.ps1" in windows
    assert "create your own password" in windows
    assert "Save the email and temporary password" not in windows
    assert "pause" in windows.lower()
