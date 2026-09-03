# Agents Chat Community Edition

Agents Chat is an independent, open-source project. It is a local-first
workspace for bringing multiple AI agents into one organized chat interface,
where conversations, project folders, uploaded material, generated files,
approvals, and agent handoffs stay connected.

## Public beta

Version `0.1.0-beta.3` is an early public beta for macOS and Windows. Expect
rough edges, especially around first-time installation and connecting external
agent tools. Both launchers bind to `127.0.0.1`, enable
authentication by default, keep mutable state in the current user's private
application-data directory, and do not bundle AI-provider credentials.

Start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) for the path
from first sign-in through a verified project and its completion receipt.

## Downloads

- [macOS beta ZIP](https://github.com/agents-chat/agents-chat/releases/download/v0.1.0-beta.3/agent-chat-community-macos-0.1.0-beta.3.zip)
  ([SHA-256](https://github.com/agents-chat/agents-chat/releases/download/v0.1.0-beta.3/agent-chat-community-macos-0.1.0-beta.3.zip.sha256))
- [Windows beta ZIP](https://github.com/agents-chat/agents-chat/releases/download/v0.1.0-beta.3/agent-chat-community-windows-0.1.0-beta.3.zip)
  ([SHA-256](https://github.com/agents-chat/agents-chat/releases/download/v0.1.0-beta.3/agent-chat-community-windows-0.1.0-beta.3.zip.sha256))

The two downloads are intentionally byte-for-byte identical universal source
bundles. Each contains both platform installers; the separate filenames make
the correct starting installer clear. They are not different native binaries.

### Verify the download

Keep the ZIP and its `.sha256` file in the same folder. On macOS, run:

```bash
shasum -a 256 -c agent-chat-community-macos-0.1.0-beta.3.zip.sha256
```

On Windows, open PowerShell in the download folder and run:

```powershell
$expected = (Get-Content .\agent-chat-community-windows-0.1.0-beta.3.zip.sha256).Split()[0].ToLower()
$actual = (Get-FileHash .\agent-chat-community-windows-0.1.0-beta.3.zip -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw "Checksum mismatch: do not install this download." }
"Checksum OK: $actual"
```

Only continue when macOS reports `OK` or Windows reports `Checksum OK`.

## Install on macOS

Download and expand the macOS ZIP. Because this preview is not yet notarized
by Apple, **Control-click (right-click) the installer and choose Open** the
first time instead of double-clicking:

> **Install Agents Chat on Mac.command** → right-click → **Open** → **Open**

(macOS Sequoia users without an Open button: double-click once, then
**System Settings → Privacy & Security → Open Anyway**.)

The installer uses a locked Python 3.12 environment, automatically selects a
free private local address, installs a user-scoped launchd job and Applications
launcher, runs health checks, and opens a one-time setup page where you create
your own password. No administrator password or `sudo` is required.

See [`docs/MAC_INSTALL.md`](docs/MAC_INSTALL.md) for preflight and uninstall
instructions.

## Install on Windows

Download and extract the Windows ZIP, then double-click **Install Agents Chat on
Windows.cmd**. The current-user installer creates Desktop, Start menu, and
sign-in launchers, keeps the server on a private loopback address, and opens the
same one-time password setup. No administrator account or firewall rule is
required.

The app normally uses a completely private Python 3.12 runtime. If Enterprise
Application Control blocks that unsigned interpreter, the installer accepts
only the exact Windows policy error, verifies a pinned Python.org download and
Python Software Foundation signature, and uses that signed current-user runtime
without changing `PATH` or file associations. See
[`docs/WINDOWS_INSTALL.md`](docs/WINDOWS_INSTALL.md) for details and uninstall
behavior.

## Included starter agents

The generic starter roster supports local adapters for Codex, Claude, Hermes,
MiniMax, Antigravity, Grok, and OpenRouter. OpenRouter setup is write-only and
restricted to verified free text models; no paid or ambiguously priced route is
selected by the Community app. An agent remains honestly offline until its
local tool, bridge, permission, and token are configured by the owner.

The previous ad-hoc-signed Perplexity Accessibility helper is not included in
this release. It will return only after Developer ID signing and notarization
are available.

## Current workspace highlights

- Code conversations open into a persistent multi-agent Mission Studio with a
  durable Plan, Build, Verify, Review, and Integrate graph.
- Owners can steer an active coding mission while protected verification and
  review seats remain read-only and host checks stay authoritative.
- Voice Chat provides a saved live transcript, agent or orchestrator routing,
  and optional server speech when the owner supplies the required provider key.
- Activity, Projects, agent setup, and navigation use dedicated responsive
  desktop and mobile workspaces instead of compressed popups.

## Learn what each agent is best at

Community Edition includes **True Free Will Orchestration**, a bounded
self-routing skill that lets a mixed agent room ask one another focused
questions, route replies by `@handle`, and pass the floor from agent to agent. When
the session ends, Agents Chat distills what it learned into capability dossiers
that help the system orchestrator choose the right teammate later.

The skill accepts agents added after installation, not only the starter roster.
See [`docs/TRUE_FREE_WILL.md`](docs/TRUE_FREE_WILL.md) for the best first-run
prompt, recommended controls, and the human-review limits.

## A complete local product

Community Edition is not a limited trial. It does not cap chats, connected
agents, projects, skills, or files. If a feature can run safely on the owner's
computer, it belongs in the local product.

## Privacy and security

- The application listens on loopback only by default.
- Authentication is enabled by default.
- Chats, files, configuration, and credentials are stored locally.
- Remote access is an advanced deployment and should use HTTPS with deliberate
  origin and network configuration.
- No telemetry or outbound support email is enabled by default.

Do not post credentials or private chat material in issues. See
[`SECURITY.md`](SECURITY.md) for vulnerability reporting.

## Help improve the beta

- Open a structured [question](https://github.com/agents-chat/agents-chat/issues/new?template=question.yml)
  for setup, connection, and how-to help.
- Open a structured [feature request](https://github.com/agents-chat/agents-chat/issues/new?template=feature_request.yml)
  for an idea or workflow you would like to see.
- Open a structured [GitHub Issue](https://github.com/agents-chat/agents-chat/issues/new/choose)
  for a reproducible bug or documentation problem.
- Report suspected vulnerabilities privately as described in
  [`SECURITY.md`](SECURITY.md).

Agents Chat is a personal open-source beta, not a commercial support service.
Read [`SUPPORT.md`](SUPPORT.md) for response expectations and
[`docs/RESPONSIBLE_USE.md`](docs/RESPONSIBLE_USE.md) before consequential use.

## License

Agents Chat Community Edition is licensed under the [MIT License](LICENSE).
The code license does not grant rights to the project name or logo; see the
[Agents Chat Trademark Policy](TRADEMARKS.md).
Bundled third-party notices are in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
