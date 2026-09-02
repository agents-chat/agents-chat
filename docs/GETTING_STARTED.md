# Your first Agents Chat project

This walkthrough takes you from a fresh Community Edition install to a
finished, verified coding project. Everything runs on the computer where you
installed Agents Chat.

## Before you begin

- Install Agents Chat using the guide for [macOS](MAC_INSTALL.md) or
  [Windows](WINDOWS_INSTALL.md).
- Bring an AI agent you already use. Community Edition does not include an AI
  provider subscription, usage credits, API keys, or a signed-in provider
  account.
- Keep passwords, API keys, app passwords, bot tokens, and recovery codes out
  of chat and GitHub issues. Enter a credential only in the secure field that
  names it.

## 1. Create your local account

Open Agents Chat and create the first owner account. This account protects the
local workspace; it is not an Agents Chat cloud account. Use a strong password
that you can store safely.

## 2. Connect and verify one agent

Open the agent setup screen and choose a provider or local agent you already
have. Follow that provider's setup instructions, then choose **Verify
connection**. Do not continue to a real project until at least one agent is
shown as ready.

If you explore without an agent, you can organize projects, upload files, and
review settings, but chats and coding work cannot run yet.

## 3. Know where the work goes

These parts have different jobs:

- **Project** — the container that keeps related chats and a coding workspace
  together.
- **Workspace folder** — the real folder on this computer where the source
  code and project files live.
- **Chat Files** — uploads and saved deliverables from the current
  conversation. This is not a file browser for the whole workspace folder.
- **Canvas** — the shared visual board for previews, pages, images, and other
  visual work in the current chat.
- **Mission Studio** — the control surface for a coding mission: its plan,
  progress, verification, review, and decisions that need you.

## 4. Start your first coding project

Choose **Start coding project**, give the project a clear name, and describe a
small outcome someone can verify. A good first brief is:

> Build a one-page personal website with a short introduction, three project
> cards, and a contact link. Make it work on phones and computers, and add a
> repeatable check that proves the build succeeds.

Use a new folder or a folder that you are comfortable letting the agent edit.
Do not begin in a folder containing irreplaceable files without a backup.

## 5. Follow the mission stages

The normal path is:

1. **Plan** — confirm that the agent understood the outcome, boundaries, and
   proof of completion. Ask for changes before approving a plan that is wrong
   or too broad.
2. **Build** — the implementer changes files in the project's workspace
   folder. You can follow progress in Mission Studio.
3. **Verify** — Agents Chat runs the project's real test, build, lint, or other
   repeatable check. A conversational claim that something works is not a
   substitute for this step.
4. **Review** — inspect the result, the verification evidence, and any known
   limits. Open the project locally when visual or interactive behavior needs
   to be checked.
5. **Owner accept or merge** — you decide whether the reviewed result is
   accepted. For a Git-backed project, merge only after the result and its
   evidence are satisfactory. Agents do not make this final decision for you.

When Agents Chat asks for a decision or reports a blocked check, resolve that
item before treating the project as complete.

## What a finished project looks like

Before you call the first project done, confirm all of these:

- The requested outcome is present in the workspace folder.
- At least one meaningful, repeatable verification check passes.
- You inspected any important visual or interactive behavior yourself.
- Mission Studio shows no unresolved error or owner decision.
- The completion receipt describes what was verified and where the result
  lives.
- You accepted the result, or merged the reviewed branch when Git is in use.
- Any limitation or remaining manual step is written down.

## Get help safely

If the problem is repeatable, open a GitHub issue and include:

- your operating system and Agents Chat version;
- the exact action you took and what you expected;
- the error message with secrets and personal information removed;
- the smallest set of steps that reproduces the problem; and
- screenshots only after checking every visible name, path, message, and
  browser tab.

Never attach configuration files, databases, chat exports, private project
files, access tokens, cookies, recovery codes, or full logs you have not
reviewed. Use GitHub's private security reporting path for a vulnerability;
do not post exploit details in a public issue.

