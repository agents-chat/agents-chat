# macOS Community Edition Installation

Status: public beta `0.1.0-beta.3`.

## Supported target

- Apple silicon and Intel Macs supported by Python 3.12 dependencies
- Local loopback access on an automatically selected private port
- Authentication enabled by default
- No administrator or `sudo` access required

## Release installation

Download and expand the macOS release ZIP, then open the installer:

> **Install Agents Chat on Mac.command**

Because this preview is not yet notarized by Apple, macOS blocks a plain
double-click on a downloaded installer the first time. Open it like this
instead:

1. **Control-click (or right-click)** `Install Agents Chat on Mac.command` and
   choose **Open**, then click **Open** in the confirmation dialog.
2. On **macOS Sequoia**, if no Open button is offered: double-click the file
   once, open **System Settings → Privacy & Security**, scroll down and click
   **Open Anyway**, then confirm.

This is needed only once per file. Advanced users can instead clear the
download quarantine for the whole extracted folder, after which plain
double-clicks work:

```bash
xattr -dr com.apple.quarantine "/path/to/the/extracted/Agent-Chat-Community folder"
```

Keep the installer window open until it finishes. When your browser opens,
create your own password on the one-time setup screen. There is no temporary
password to copy or save.

For an advanced terminal installation, run:

```bash
./scripts/community/install_macos.sh
```

The installer:

1. Refuses to install an archive without the Community release-clearance marker.
2. Copies only application files into `~/Library/Application Support/Agent Chat`.
3. Verifies an architecture-specific, SHA-pinned `uv` executable and installs an
   isolated Python 3.12 runtime with locked dependency hashes. It does not trust
   a different `uv` found on `PATH` or execute a downloaded installer script.
4. Generates a private configuration and opens a one-time create-your-password screen.
5. Selects the first available private loopback port, while preserving the saved
   port during upgrades.
6. Creates a user-scoped launchd service and **Agents Chat.app** launcher in the
   user's Applications folder.
7. Runs the doctor checks, starts Agents Chat, and opens the correct local page.

The prior Perplexity Accessibility helper is intentionally not bundled in this
public beta. It will return only after Developer ID signing and notarization are
available.

When installation finishes, the browser asks you to create your own password.
There is no temporary credential to copy, lose, or hunt down. An upgrade keeps
the existing private configuration and existing password.

The `Agent Chat` application-support folder is a legacy compatibility name.
Keeping it prevents upgrades from losing existing settings and conversations;
the product shown in the interface is **Agents Chat**.

If `uv` is not already available, the installer downloads the pinned standalone
installer from Astral over HTTPS into Agents Chat's private tools directory. The
current pinned version is documented in the installer.

## Preflight without changes

```bash
./scripts/community/install_macos.sh --check-only
```

## Uninstall

Double-click **Uninstall Agents Chat on Mac.command** in the downloaded folder.
Press Return to preserve settings and chats, or type `DELETE` when you explicitly
want them moved to the recoverable Trash too.

For an advanced terminal uninstall, the default command preserves the private
configuration and chat data:

```bash
./scripts/community/uninstall_macos.sh
```

To remove the data too:

```bash
./scripts/community/uninstall_macos.sh --purge-data
```

Removed files are moved to the Trash for recovery rather than permanently
deleted.
