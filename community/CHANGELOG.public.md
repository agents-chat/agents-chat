# Changelog

All notable user-facing changes to Agents Chat Community Edition are documented
here. This file is also displayed inside the app through **What's new**.

The first public release is intentionally described in detail so new users can
see the complete local product they are downloading. Future entries will focus
only on what changed in each version.

## [0.1.0-beta.3] — 2026-09-01

### Privacy and security correction

- Rebuilt the public source and both installers from a stricter allowlisted
  export that removes private infrastructure names, personal paths and names,
  internal operating personas, business-specific vocabulary, trading-desk
  residue, and the former private database product name.
- Replaced the Community starter registry structurally with generic public
  agents instead of relying on cosmetic label substitutions.
- Revoked the installer bootstrap token as soon as the owner completes account
  setup, and changed Telegram sending to an authenticated signed `POST` with its
  message in the request body.
- Added public-address validation for credentialed IMAP, SMTP, and CalDAV
  connections, including redirect protection. Private-network servers require
  an explicit owner opt-in.
- Enforced loopback-only binding in every bundled bridge launcher and removed
  unused internal bridge components from the public package.
- Renamed Community databases, logs, and backups to the neutral `agent-chat`
  identity, with a recoverable migration for existing beta installs.
- Temporarily removed the macOS Perplexity Accessibility helper until it can be
  distributed with Developer ID signing and notarization.
- Updated and relocked the Python web and cryptography dependencies used by new
  installs.

## [0.1.0-beta.2] — 2026-09-01

### Launch readiness

- Added a guided getting-started path from first sign-in through a completed,
  verified project and responsible-use guidance for consequential workflows.
- Replaced the missing maintainer-only npm targets with shipped cross-platform
  wrappers, so `npm start` and `npm setup` now launch the public Mac or Windows
  workflow instead of pointing at files that are not in the release.
- Added weekly Python dependency monitoring alongside npm and GitHub Actions.
- Added structured GitHub questions for setup and how-to help, with public-safe
  reporting guidance and direct routing from the website and repository.
- Documented the bundled macOS Perplexity helper, its shipped Swift source, its
  build path, and the exact-byte integrity guarantee used for releases.
- Hardened Community CI so both the private source checkout and the public
  history-free export run the checks appropriate to the files they actually
  contain.
- Replaced personal attribution with project-only contributor and trademark
  language throughout the public release.
- Expanded the release privacy gate and artifact checks for personal paths,
  credentials, private integrations, generated caches, and unexpected files.

## [0.1.0-beta.1] — 2026-08-31

### Models & Integrations
- **Orchestrator-owned onboarding.** Once the first orchestrator enters the
  setup chat, that agent now runs the process end to end: it performs and
  verifies each setup step, stays accountable for any internal delegation, and
  keeps leading until the owner explicitly finishes. Setup requests are no
  longer intercepted by a separate desktop handoff card.
- **Truthful first-agent recommendation.** Setup now prefers a detected, usable
  desktop agent such as Codex over an unconfigured OpenRouter connector, uses
  platform-neutral scan copy, and lets users go Back without losing their
  onboarding answers.
- **OpenRouter is now a first-class free-model agent.** Add a key through the
  write-only setup, verify it through the localhost bridge, and choose from the
  current free text-model catalog. Paid, custom, or ambiguously priced routes
  fail closed to the Free Models Router.
- **Every connected agent can use one guarded Crawl4AI reader.** Dynamic-page
  requests flow through signed, chat-scoped tool URLs with loopback-only
  defaults, strict public-URL checks, and bounded use of the local Docker worker.
- **One-click Perplexity desktop agent on macOS.** Detect an installed Perplexity
  app, generate a private localhost-only connection key, start the local bridge,
  open the narrowly scoped **Agent Chat Perplexity Driver** Accessibility grant,
  and verify the real connection from the setup card. The release carries a
  universal Apple-silicon/Intel helper, so users do not need Xcode, Terminal, or
  a Perplexity API key; generic `python3` access stays off.
- **Self-healing bridge reconnect.** Reconnecting a degraded bridge can reload an
  outdated model configuration instead of reporting an
  already-running process as repaired.
- **Truthful health button states.** Reconnect is red for offline, amber for
  degraded/model drift, and green only when the bridge is online.

### Conversations & navigation
- The compact desktop navigation now shows every destination icon, including
  items inside collapsed groups, and its labelled pop-out opens reliably with
  a mouse on hybrid and remote Windows desktops.
- Project conversations now keep an explicit **Chat** or **Code** intent. Chat
  stays ordinary project discussion; Code opens the connected workspace and
  coding-aware routing, and the choice survives reloads and navigation.
- Activity is now a responsive master-detail command center with separate
  **Needs you**, **Updates**, and **History** workflows, grouped signal threads,
  and dedicated mobile detail actions.
- Desktop navigation stays compact until opened, while project switching,
  Settings, Mode & agents, and agent setup use purpose-built responsive panels.
- Move a conversation to an owner-only 30-day Trash, then Undo, Restore, or
  permanently delete it with a private deletion receipt.
- Open Mission Map on desktop or mobile to see what needs you, what is working,
  recent outputs, and the chat's real chronological handoffs.
- Edit the future roster of an ordinary Counsel, Collaborate, Debate, Delegate,
  Pipeline, or Poll turn while the current agent finishes safely. Auto plans,
  mentions, skills, and schedules remain fixed for their run.

### Coding jobs
- Code conversations now open as a persistent multi-agent **Mission Studio**.
  The crew, shared build canvas, transcript, and durable Plan, Build, Verify,
  Review, and Integrate graph stay synchronized across reloads and reconnects.
- Live steering reaches the active coding job without creating a second goal.
  Verification and review seats use bridge-enforced read-only workspace access,
  while deterministic host checks remain authoritative.
- Repeated identical verification failures stop after two matching results and
  surface their evidence to the owner instead of looping indefinitely.
- Start a verify-gated coding job from a project workspace. The app staffs
  separate implementer, reviewer, and verifier seats, runs deterministic host
  checks before and after review, and leaves merge to you. An agent cannot mark
  the job done.
- **Rebuild** clones Agents Chat into an isolated folder and lets the orchestrator
  loop the crew until the copy ships. Mid-build seat changes affect future stages,
  nested working directories are honored, and the live app is never overwritten.

### Voice & live collaboration
- Voice Chat is now a full conversation workspace with one selected agent or the
  room orchestrator, a saved live transcript, typed follow-ups, and a visualizer
  driven by real session state. Optional provider-backed speech remains off until
  the owner supplies the required key.
- FREE-WILL follows an explicit ordered question plan, keeps guest turns bounded
  per question, and preserves skill identity through handoffs and reconnects.

Agents Chat Community Edition is a local-first workspace for bringing multiple AI
agents into one organized interface. This first public preview includes the
complete local product rather than a limited trial: there are no product-imposed
caps on chats, connected agents, projects, skills, or files.

### Multi-agent chat

- Talk privately with one agent or bring several agents into the same room.
- Choose **Solo**, **Counsel**, **Collaborate**, **Debate**, **Delegate**,
  **Pipeline**, or **Poll** depending on how the team should work.
- Counsel gathers analysis and has a selected lead synthesize the answer.
- Collaborate lets agents build toward a shared result over multiple turns.
- Debate pressure-tests a decision from opposing viewpoints before a neutral
  wrap-up.
- Delegate gives different tasks to several agents in parallel.
- Pipeline hands work from one specialist to the next in a defined order.
- Poll asks agents the same question independently and tallies the result.
- Address agents by name, choose participating agents, change the lead, and see
  the planned order before sending.
- Receive live progress, agent presence, working states, and honest connection
  errors instead of a silent or falsely successful interface.
- Stop active work, nudge an agent, answer follow-up questions, and resume work
  that needs human input.
- Keep drafts, uploads, failed-send recovery, and in-progress activity attached
  to the conversation where they began.
- Search a conversation, quote earlier messages, copy results, and start a clean
  conversation without losing the old one.

### Agents and connections

- Discover supported agent tools already installed on the computer during
  first-run setup.
- Recognize the Microsoft Store Codex desktop app on Windows and show an honest
  **Codex found · one quick step** state with an exact, automatically generated
  Codex prompt directly on its card, instead of sending users into another menu
  or pretending its protected desktop executable is a usable command-line bridge.
- Require Codex to pass both a launch check and a signed-in-session check before
  it can be labeled connected; a listening bridge alone is no longer enough.
- Show only agents that are genuinely connected in the Room roster and agent
  pickers. Offline or not-yet-configured tools remain in **Set up agents** until
  their complete readiness check succeeds.
- Explain connection scope before setup: agents receive only the messages, files,
  and workspace the user chooses, retain the permissions of their desktop app,
  and gain no new whole-computer or Agents Chat source-code access.
- Start with generic adapters for **Codex, Claude, Hermes, MiniMax, Antigravity,
  Grok, and Perplexity**.
- Connect supported desktop command-line agents through local bridges.
- Connect Docker agents such as Agent Zero through a guided localhost setup.
- Connect OpenAI-compatible services and custom local HTTP agent endpoints.
- Give each connected agent a clear name, role, description, endpoint, and
  private local connection key.
- Generate a self-connect prompt for an agent that is not yet listed so the agent
  can create and explain its own compatible local adapter.
- Re-scan the computer at any time and add, remove, or reconfigure agents later.
- Keep agents honestly marked offline until their required local tool, bridge,
  model, and credentials are actually available.
- Start managed Community bridges from setup and automatically select an unused
  loopback port when another Agents Chat installation already owns the default.
- Keep the Windows dependency environment and package cache inside Agents Chat,
  always use the pinned private `uv`, and admit a signed shared Python.org base
  only for the exact Enterprise Application Control compatibility fallback.
- Use the built-in **True Free Will Orchestration** skill to draw a random
  floor-holder, route questions by `@handle`, pass the room agent to agent, and
  turn the bounded session into capability dossiers that improve future routing.
- Include newly connected agents in a True Free Will run by entering their ids,
  display names, or handles in the skill's agent-pool field.

### Conversations, folders, and search

- Organize conversations into persistent folders with clear **Unfiled** and
  **Archived** areas.
- Create, rename, move, archive, restore, and delete conversations without
  flattening the rest of the workspace.
- Switch conversations in place without reloading the entire application.
- Use readable conversation titles generated from the work instead of relying on
  numeric chat identifiers.
- Browse Chat Atlas views for folders, activity rhythm, agent contribution, and
  follow-up relationships.
- Use Constellation and timeline views to understand how conversations relate
  without inventing relationships that are not in the underlying data.

### Projects and real computer folders

- Create projects that group conversations, files, calendar items, and open work.
- Attach a real folder from the Mac or Windows computer to a project.
- Choose folders with the native system folder picker instead of manually typing
  long filesystem paths.
- Let every conversation linked to a project inherit the same working folder.
- Override a single conversation with its own folder when the work needs a
  separate location.
- Protect application, reference, and other unsafe directories from accidental
  use as agent workspaces.
- Seed new project folders with durable project, decision, task, and notes files
  so important context can live outside chat history.
- Archive completed projects into a collapsible archive and restore them later.
- Add project calendar items, attach folders, and link or unlink conversations
  from the Projects page.
- Start an isolated project preview, inspect the rendered result, and point to a
  page element that needs work.
- Capture preview errors as precise context for the next agent turn.
- Run a detected project test suite and return the result to the conversation.
- Review branch status, commits, changed files, uncommitted work, diffs, and draft
  release notes without automatically merging or publishing anything.

### Files and Canvas

- Upload files directly into a conversation and keep them associated with that
  chat and owner.
- Store generated reports, documents, images, and web artifacts locally.
- Capture files created by supported agents into the conversation's Files area.
- Preview supported output in the visual Canvas without losing the conversation.
- Browse Canvas history, compare artifacts, hand an artifact to another agent,
  and open or download the original file.
- Extract readable text from supported PDFs and use local macOS OCR when enabled.
- Isolate HTML and SVG previews so previewed content cannot use the owner's Agent
  Chat session to access the main application API.

### Knowledge and decisions

- Record settled team decisions in a shared Decisions ledger.
- Inject relevant decisions into agent context so the team can reuse what was
  already decided.
- Track repeated unanswered decision searches as **Open Questions**.
- Answer an open question by recording the missing decision, or dismiss it when
  it no longer matters.
- View branches, handoffs, active work, approvals, and unresolved questions in
  the Mission Map.
- Keep project instructions and durable notes in the project's own local files.
- Preserve pending approvals and agent questions across a clean restart.

### Skills, workflows, and automation

- Create reusable skills for common work instead of rebuilding the same prompt
  and team every time.
- Define skill inputs, participating agents, ordered steps, APIs, and the
  multi-agent mode used to run the workflow.
- Open the skill's own conversation immediately after creating it.
- Run skills manually from the Skills panel or turn supported work into a
  scheduled automation.
- Use **LOOPS** as an automation mission-control view for schedules, recent runs,
  health, and items that need attention.
- Keep automation notifications configurable so routine work can stay quiet while
  important failures or approvals reach the owner.

### Control Room and operations

- Follow a five-step Control Room setup guide: connect an agent, choose and test
  the orchestrator, choose a Senior, create a private alert room, and enable
  monitoring.
- See agent, bridge, Docker, automation, notification, and application health in
  one operator view.
- Assign a Senior agent and a private control conversation for operational work.
- Hand a Control Room finding to a connected desktop agent for investigation.
- Open calendar, automation, notification, mailbox, and secret setup directly
  from the relevant Control Room card.
- View truthful service state and recovery guidance rather than treating an
  unconfigured integration as healthy.
- Review local usage and estimated model cost by agent and conversation.
- Keep an audit trail for security-sensitive and administrative actions.

### Calendars, mailboxes, notifications, and secrets

- Add multiple read-only iCalendar feeds for personal, work, or shared schedules.
- View upcoming events in Agents Chat and associate internal calendar items with
  projects.
- Configure agent mailboxes and see which mail, calendar, and contact capabilities
  are actually connected.
- Store integration credentials locally through the Secrets interface and grant
  them only to the agents that need them.
- Apply supported secret grants and revocations to desktop bridges without
  requiring a full Agents Chat restart.
- Configure browser notifications and optional advanced notification routes.
- Keep optional integrations visibly unconfigured until the owner supplies the
  required account and credentials.

### Voice, accessibility, and responsive use

- Dictate messages from the composer using the computer or phone microphone.
- Play agent replies aloud with configurable voices, speed, pause, and resume.
- Use local speech options where supported, with honest fallback when a voice
  service is unavailable.
- Follow sequential multi-agent work turn by turn with spoken replies when voice
  playback is enabled.
- Use touch-friendly navigation, composer controls, sheets, and conversation
  switching on phone-sized screens.
- Choose light and dark visual themes with visible keyboard focus, readable form
  states, high-contrast button text, and reduced-motion support.
- Use a responsive interface designed around clear labels and practical
  fifth-grader-friendly setup guidance.

### Installation and platform support

- Install from clear double-click launchers at the top of the downloaded Mac or
  Windows folder without manually typing terminal commands.
- Automatically select a free private loopback port on first install and preserve
  the saved address during upgrades.
- Reopen Agents Chat from a user Applications launcher on macOS or Desktop and
  Start menu shortcuts on Windows.
- Use plainly labeled uninstall launchers that preserve chats by default and keep
  removed files recoverable.
- Open a one-minute first-launch screen where the owner creates a password directly;
  there is no temporary credential to copy, lose, or hunt down.
- Install the macOS Community Preview without administrator or sudo access.
- Install the tested Windows Preview for the current Windows user without opening
  a firewall port or requiring administrator rights.
- Run Agents Chat in an isolated, hash-locked Python 3.12 environment.
- Bootstrap the private `uv` tool from architecture-specific release archives
  whose archive and executable SHA-256 values are pinned for Apple silicon,
  Intel Mac, and x64 Windows; PATH tools and remote install-script execution are
  no longer trusted.
- On enterprise-managed Windows PCs, recover from the exact Application Control
  block by verifying a pinned Python.org SHA-256 and Python Software Foundation
  signature, then creating the private environment from that signed runtime.
- Roll back a failed Windows core update to the previously verified app and
  runtime, or retire incomplete fresh-install files into collision-safe recovery
  backups instead of leaving a release marker that looks runnable.
- Start automatically at user sign-in through launchd on macOS or the current-user
  Startup entry on Windows.
- Preserve configuration and chat data across normal upgrades.
- Run preflight and doctor checks for platform, files, authentication,
  configuration permissions, data storage, and Python runtime health.
- Uninstall while preserving data by default, or explicitly remove data into a
  recoverable Trash or removal folder.
- Build cleared release archives from an explicit allowlist and publish checksum
  files alongside the downloadable packages.
- Include contributor guidance, issue templates, pull-request checks, dependency
  updates, security reporting instructions, and an MIT license.

### Privacy and security

- Bind to **127.0.0.1** by default so a normal installation is not exposed to the
  local network or internet.
- Enable authentication by default and generate a private bootstrap credential
  during installation.
- Keep chats, projects, files, artifacts, configuration, and credentials in the
  current user's protected application-data directory.
- Ship no AI-provider API keys, account credentials, personal data, private
  agents, private business integrations, or machine-specific configuration.
- Perform origin checks, request limits, owner isolation, protected-path checks,
  and scoped authorization for administrative actions.
- Keep preview authentication scoped to the preview so a project under
  development cannot act as the signed-in owner.
- Build public archives through secret scanning and checks for private paths,
  hostnames, networks, identities, databases, logs, and unsupported assets.
- Send no telemetry and no automatic support email by default.
- Make **Report a problem** explain that Community reports become public GitHub
  issues, exclude chat contents by default, and let the user review the report
  before opening GitHub.

### Improved for the Community preview

- Replace temporary-password handoffs with a secure, local first-launch screen
  where the owner creates a memorable password directly.
- Let the owner choose their display name and either a local username or email
  during first launch, then sign in with that choice instead of a developer-style
  `admin@localhost` address. Existing email sign-ins remain compatible.
- Added the missing OpenAI-compatible local connector to the distributed tree.
- Separated private release-clearing rules from the public verifier so the public
  repository does not disclose a catalog of maintainer or client identifiers.
- Removed embedded authoring metadata from distributed persona images and added
  byte-level metadata checks plus an independent Gitleaks configuration.
- Made contributor CI scan version-controlled source without treating its own
  virtual environment or ordinary documentation edits as a broken release.
- Added a Windows `-Port` option so Community Edition can safely run beside an
  existing local Agents Chat installation.
- Made the Windows uninstaller stop its complete server process tree, tolerate an
  already-stopped child safely, verify shutdown, and preserve data by default.
- Made macOS upgrades replace the private virtual environment recoverably instead
  of failing when the environment already exists.
- Replaced developer- and company-specific onboarding examples with generic Agent
  Chat language.
- Replaced sanitizer-generated and developer-only field examples with natural,
  neutral language that makes sense to a first-time user.
- Replaced the internal term “scratch” with plain-language labels such as **Chat
  files**, **project folder**, and **private folder**.
- Strengthened completed-input, focus, and primary-button contrast throughout
  first-run setup.
- Kept saved and autofilled login text readable on Windows by preventing Chrome
  from replacing the dark credential fields with a low-contrast pale surface.
- Prevented a fresh installation from showing a stale **Session expired** notice
  before its owner has even created the first local sign-in.
- Kept agent-connection progress visible while bridges start and reported the
  actual connection result instead of appearing unresponsive.
- Made Docker and custom-agent setup a guided sequence with explanations and
  sensible localhost examples.
- Added a native macOS and Windows folder chooser for chat and project workspaces.
- Made a newly created skill open directly into its skill conversation.
- Prevented a full macOS uninstall from leaving an empty installation directory.
- Preserved the Community virtual environment during background startup.

### Preview boundaries

- This is a macOS-first developer preview with a tested Windows preview; a cleared
  public download has not been published yet.
- AI models, provider subscriptions, command-line tools, Docker runtimes, email
  accounts, calendars, and their credentials are supplied by the user.
- Secure access from outside the computer, cloud synchronization, managed
  backups, team administration, hosted routing, SSO, premium support, and
  compliance services are not part of this local Community release.
- Advanced remote access requires deliberate HTTPS, origin, authentication, and
  network configuration; the default installation remains local-only.
