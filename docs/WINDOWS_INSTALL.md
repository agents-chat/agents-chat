# Install Agents Chat Community Edition on Windows

Agents Chat installs for the current Windows user. It does not require an
administrator account, open a firewall port, or expose the app beyond the
local PC.

1. Download the Windows ZIP from the GitHub release.
2. Right-click the ZIP, choose **Extract All**, and open the extracted folder.
3. Double-click **Install Agents Chat on Windows.cmd**.
4. Keep the installer window open until it finishes. Your browser then opens a
   one-time setup page where you create your own password. There is no
   temporary password to save.

The installer automatically chooses the first available private loopback port,
saves that address, and reuses it during upgrades. It also creates:

- **Agents Chat** on the Desktop
- **Agents Chat** and **Uninstall Agents Chat** in the Start menu
- a current-user Startup entry so the local server is ready after sign-in

For an advanced PowerShell installation, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\community\install_windows.ps1
```

To deliberately choose a specific free loopback port for the first install:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\community\install_windows.ps1 -Port 18086
```

The installer opens the correct saved address; users do not need to remember or
type the port number. A reinstall keeps that address, private configuration, and
existing password.

The browser opens into a one-time setup where you create your own password.
There is no temporary credential to copy, lose, or hunt down. Reinstalling
keeps the existing private configuration and existing password.

The installer creates a private, hash-locked Python 3.12 environment under
`%LOCALAPPDATA%\Agent Chat`. Existing configuration and chat data are preserved
during upgrades. It uses Agent Chat's pinned private copy of `uv`, never a
different copy found on `PATH`.

On a managed PC, Windows Enterprise Application Control may reject uv's
unsigned managed interpreter. Only when Windows returns the specific policy
error does the installer use its compatibility fallback: it downloads the
pinned Python.org 3.12.10 installer, verifies both SHA-256 and the Python
Software Foundation Authenticode publisher, and installs it for the current
user without changing `PATH`, file associations, shortcuts, or the Python
launcher. The app's dependency environment remains private under `Agent Chat`.
The signed Python base is a shared current-user prerequisite under
`%LOCALAPPDATA%\Programs\Python\Python312` and remains available if Agents Chat
is later removed.

If an install or upgrade fails before Doctor passes, the failed app and runtime
are moved into the recoverable `backups` folder. A previous app is restored only
when its configuration, dependency stamp, and runtime imports were verified
before the update began.

Use **Uninstall Agents Chat** from the Start menu to remove the application while
retaining settings and chats. For an advanced PowerShell uninstall:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Agent Chat\app\scripts\community\uninstall_windows.ps1"
```

Add `-PurgeData` only when you also want configuration and chat data moved into
the recoverable `Agent Chat Removed` folder.

These folder names are legacy compatibility names. Keeping them prevents an
upgrade from losing an existing installation; the product shown in the
interface is **Agents Chat**.
