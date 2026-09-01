# Agents Chat Community Edition Scope

## Product boundary

The Community Edition is the local-first Agents Chat application plus generic,
user-configurable agent adapters. It must install without access to Owner's
machines, networks, accounts, companies, data, or private agent personas.

## Included in the Community Preview

- Core FastAPI application and responsive web interface
- Local SQLite storage and per-chat artifact storage
- Authentication enabled by default
- Generic Codex, Claude, Hermes, MiniMax, Antigravity, Grok, Perplexity, and
  free-only OpenRouter adapters where their local tools or owner-supplied
  credentials are available
- User-created agent definitions and provider configuration
- Project folders, files, canvas, responsive Activity workspace, Voice Chat,
  durable Mission Studio coding graphs, audit trail, and approval-oriented
  workflows
- macOS and Windows installers, launchers, doctor checks, uninstallers, and
  release archives
- Documentation, tests, CI, issue templates, and security policy

No provider credential is bundled. Optional services must fail closed or degrade
honestly when they are not configured.

## Community product promise

Community Edition is the complete local product, not a crippled trial. It does
not impose arbitrary limits on chats, connected agents, projects, skills, or
files. Local features stay available when the owner supplies the required local
tool or provider access.

Future paid products may charge for services that cannot be delivered merely by
downloading this repository: hosted cloud accounts, secure remote access,
Companion device pairing, multi-computer synchronization, team administration,
managed backups and updates, hosted routing, premium support, custom business
integrations, and extended compliance or audit retention. Those services must
remain outside the Community repository and must not be required for normal
local use.

## Excluded from the public repository

- `.env` files, API keys, cookies, certificates, tokens, and credentials
- SQLite databases, chats, attachments, artifacts, logs, caches, and backups
- Absolute paths, usernames, hostnames, private IPs, and Tailscale details from
  Owner's environment
- Private/client-specific agent personas, configurations, bridges, mission files,
  branding, and memories
- Personal financial and brokerage integrations
- Private support mailboxes, Telegram bots, calendars, email accounts, and
  operational secret fanout
- Client rollout scripts, launchd definitions, and production-only recovery
  scripts tied to Owner's machines
- Assets without confirmed redistribution rights

## Release architecture

The first public repository will be created from an allowlisted, sanitized export
with fresh Git history. The private production repository remains the upstream
working system until the public boundary is proven. Public changes should be
portable back into the private repository without importing private data in the
opposite direction.

## Platform sequence

1. macOS Community Preview
2. macOS beta on clean machines
3. Windows package
4. Windows hardware acceptance test
5. Supported macOS and Windows release

Windows work must reuse the same application configuration and tests. Only the
installer, service management, paths, permissions, and OS integrations should
diverge.
