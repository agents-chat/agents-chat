import asyncio
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import sys as _sys
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
import _bridge_common as common  # shared bridge core (single source of truth)


APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent.parent
STATE_DIR = APP_DIR / "codex_bridge"
STATE_DIR.mkdir(exist_ok=True)
CONTEXT_STORE = STATE_DIR / "contexts.json"


_ENV_FILES = (
    APP_DIR / ".env",
    REPO_DIR / ".env",
    APP_DIR / "agents" / "shared.env",
    APP_DIR / "agents" / "codex.env",
)
common.load_env_files(_ENV_FILES)

CODEX_BIN = os.environ.get(
    "CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex"
)
# Fallback workspace when the orchestrator doesn't pass a per-chat `workspace`
# (a direct/legacy caller). Deliberately NOT the Agent-Chat repo — agents must
# never do real work inside the chat app folder. The orchestrator normally
# sends a per-chat workspace that overrides this.
CODEX_WORKDIR = os.environ.get(
    "CODEX_BRIDGE_WORKDIR", str(Path.home() / "AGENTS" / "workspaces" / "codex")
)
CODEX_MODEL = os.environ.get("CODEX_BRIDGE_MODEL", "gpt-5.5")
CODEX_REASONING = os.environ.get("CODEX_BRIDGE_REASONING", "high")
CODEX_SANDBOX = os.environ.get("CODEX_BRIDGE_SANDBOX", "workspace-write")
# Ordinary chat turns stay workspace-scoped.  Agents Chat may request this
# separate profile only for its owner-authenticated first-run desktop-setup
# endpoint, where Docker and other local runtimes are outside the workspace.
CODEX_HOST_SETUP_SANDBOX = os.environ.get(
    "CODEX_BRIDGE_HOST_SETUP_SANDBOX", "danger-full-access"
)
CODEX_TIMEOUT_HARD_CAP_S = float(os.environ.get("CODEX_BRIDGE_TIMEOUT_HARD_CAP_S", "600"))
CODEX_TIMEOUT_S = min(
    float(os.environ.get("CODEX_BRIDGE_TIMEOUT_S", "600")), CODEX_TIMEOUT_HARD_CAP_S,
)
MAX_CONTEXT_MESSAGES = int(os.environ.get("CODEX_BRIDGE_MAX_CONTEXT_MESSAGES", "12"))
# Hard ceiling on the total prompt character count sent to the Codex CLI.
# Codex raises "Separator is not found, and chunk exceed the limit" when the
# stdin payload is too large. Trim oldest history lines until we're under budget.
MAX_PROMPT_CHARS = int(os.environ.get("CODEX_BRIDGE_MAX_PROMPT_CHARS", "40000"))
# Rolling summarization (mirror of the Claude bridge): once a context exceeds
# SUMMARY_TRIGGER messages, fold all but the last SUMMARY_KEEP into a persistent
# running summary via a cheap model, so @codex keeps long-term chat memory
# without an ever-growing prompt. SUMMARY_HARD_CAP bounds it if summarization is
# unavailable (fail-open).
SUMMARY_TRIGGER = int(os.environ.get("CODEX_BRIDGE_SUMMARY_TRIGGER", "16"))
SUMMARY_KEEP = int(os.environ.get("CODEX_BRIDGE_SUMMARY_KEEP", "8"))
SUMMARY_HARD_CAP = int(os.environ.get("CODEX_BRIDGE_SUMMARY_HARD_CAP", "24"))
SUMMARY_MODEL = os.environ.get("CODEX_BRIDGE_SUMMARY_MODEL", "gpt-5.4-mini")
SUMMARY_TIMEOUT_S = float(os.environ.get("CODEX_BRIDGE_SUMMARY_TIMEOUT_S", "120"))
# Hard ceiling on the stored running summary, so even a model that ignores the
# ~200-word instruction can't slowly bloat the prompt over many compactions.
SUMMARY_MAX_CHARS = int(os.environ.get("CODEX_BRIDGE_SUMMARY_MAX_CHARS", "2000"))
BRIDGE_TOKEN = os.environ.get("AGENT_TOKEN_CODEX", "").strip()
# Shared Chrome DevTools MCP on this Mac (launchd com.agentchat.chrome-devtools).
# HTTP client only — never spawn npx chrome-devtools-mcp from this bridge.
# Loopback-only: refuse any non-local URL so this cannot be pointed off-box.
CHROME_DEVTOOLS_MCP_URL = os.environ.get(
    "CHROME_DEVTOOLS_MCP_URL", "http://127.0.0.1:55022/mcp"
).strip()
CHROME_DEVTOOLS_MCP_ENABLED = os.environ.get(
    "CHROME_DEVTOOLS_MCP_ENABLED", "1"
).strip().lower() not in ("0", "false", "no", "off")
CHROME_DEVTOOLS_MCP_SERVER_NAME = (
    os.environ.get("CHROME_DEVTOOLS_MCP_SERVER_NAME", "chrome-devtools").strip()
    or "chrome-devtools"
)
CHROME_DEVTOOLS_PROMPT = (
    "If chrome-devtools MCP tools are present they attach to the user's "
    "already-open Chrome through the local loopback permission server "
    "(http://127.0.0.1:55022/mcp). Use them only for the asked task. Do "
    "not open, click, screenshot, or switch to unrelated tabs (mail, "
    "banking, cPanel, insurance). "
)


def _is_loopback_http_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _chrome_devtools_mcp_wired() -> bool:
    return bool(
        CHROME_DEVTOOLS_MCP_ENABLED
        and CHROME_DEVTOOLS_MCP_URL
        and _is_loopback_http_url(CHROME_DEVTOOLS_MCP_URL)
    )


def _chrome_devtools_codex_override() -> Optional[str]:
    if not _chrome_devtools_mcp_wired():
        return None
    return (
        f"mcp_servers.{CHROME_DEVTOOLS_MCP_SERVER_NAME}="
        f'{{url="{CHROME_DEVTOOLS_MCP_URL}",startup_timeout_sec=30}}'
    )


_CLI_STATUS_CACHE: dict = {"ts": 0.0, "ready": False, "error": "Not checked yet"}
_CLI_STATUS_TTL_S = 5.0
MODEL_OPTIONS = {
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
}
REASONING_OPTIONS = {"low", "medium", "high", "xhigh"}

# Native Codex runs on the same Mac as Agent Chat, so local files are the most
# reliable way to hand it attachments. Docker/Tailscale URLs can be unreachable
# from CLI file tools, but the bytes are already on this disk.
ATTACHMENTS_DIR = Path(os.environ.get("DATA_DIR", APP_DIR)) / "attachments"
LOCAL_FILES_DIR = STATE_DIR / "files"
LOCAL_FILES_DIR.mkdir(exist_ok=True)
_ATTACHMENT_URL_RE = re.compile(r"https?://[^/\s]+/attachments/(\d+)/([a-f0-9]{32})")

app = FastAPI(title="Codex Agent Chat Bridge")


# Live "current step" text per context_id, surfaced to Agent Chat via
# api_log_get so @codex shows a thinking line while it works — the same
# mechanism the Agent Zero agents (Lead/Sales) use. Updated as the streamed
# `codex exec --json` events arrive in _run_codex. In-memory only (single
# uvicorn worker), bounded below to avoid unbounded growth.
PROGRESS: dict[str, dict] = {}


def _set_progress(context_id: str, text: str, active: bool = True, **kwargs) -> None:
    common.set_progress(PROGRESS, context_id, text, active, **kwargs)


def _end_progress(context_id: str) -> None:
    common.end_progress(PROGRESS, context_id)


def _maybe_set_partial_from_event(context_id: str, evt: dict) -> None:
    """Publish growing agent_message text for the chat bubble. Progress stays separate."""
    if evt.get("type") not in ("item.started", "item.updated", "item.completed"):
        return
    item = evt.get("item") or {}
    if item.get("type") != "agent_message":
        return
    text = str(item.get("text") or "").strip()
    if text:
        common.set_partial_response(PROGRESS, context_id, text)


_clip = common.clip


def _clean_command(cmd: str) -> str:
    """Unwrap a shell-wrapped command (`/bin/zsh -lc 'find …'`) to its inner
    command so the progress line reads `find …` rather than the wrapper."""
    cmd = str(cmd or "").strip()
    m = re.search(r"-l?c\s+(.+)$", cmd)
    if m:
        inner = m.group(1).strip()
        if len(inner) >= 2 and inner[0] in "'\"" and inner[-1] == inner[0]:
            inner = inner[1:-1]
        cmd = inner.strip()
    return cmd


def _progress_line_from_codex_event(evt: dict) -> Optional[str]:
    """Map one `codex exec --json` event to a short 'current step' line, or None."""
    etype = evt.get("type")
    if etype == "thread.started":
        return "Starting up…"
    if etype == "turn.started":
        return "Thinking…"
    if etype in ("item.started", "item.updated", "item.completed"):
        item = evt.get("item") or {}
        itype = item.get("type")
        if itype == "agent_message":
            return _clip(item.get("text") or "")
        if itype == "reasoning":
            return _clip(item.get("text") or "Thinking…")
        if itype == "command_execution":
            return _clip("Running: " + _clean_command(item.get("command") or ""), 90)
        if itype in ("file_change", "patch", "patch_apply"):
            return "Editing files"
        if itype == "mcp_tool_call":
            return _clip("Using " + str(item.get("tool") or item.get("name") or "a tool"), 90)
        if itype == "web_search":
            return _clip("Searching the web: " + str(item.get("query") or "").strip(), 90)
        if itype in ("todo_list", "plan"):
            return "Updating plan"
        return None
    return None


def _load_contexts() -> dict:
    return common.load_contexts(CONTEXT_STORE)


def _save_contexts(contexts: dict) -> None:
    common.save_contexts(CONTEXT_STORE, contexts)


def _context(context_id: Optional[str]) -> tuple[str, dict]:
    return common.get_or_create_context(CONTEXT_STORE, context_id)


def _authorized(request: Request) -> bool:
    return common.authorized(request, BRIDGE_TOKEN)


# Room-header stripping lives in _bridge_common (single source of truth shared
# with the Claude bridge). Alias keeps existing call sites unchanged.
_strip_system_header = common.strip_system_header


def _localize_attachments(message: str) -> str:
    """Annotate chat attachment URLs with local paths Codex can read directly."""
    seen: dict[str, str] = {}

    def _local_path_for(cid: str, file_id: str) -> Optional[str]:
        src = ATTACHMENTS_DIR / cid / file_id
        if not src.is_file():
            return None
        name = file_id
        meta = ATTACHMENTS_DIR / cid / f"{file_id}.json"
        if meta.is_file():
            try:
                name = json.loads(meta.read_text()).get("name") or file_id
            except Exception:
                pass
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name)) or file_id
        dest = LOCAL_FILES_DIR / f"{file_id}-{safe}"
        if not dest.exists():
            try:
                shutil.copy2(src, dest)
            except Exception:
                return str(src)
        return str(dest)

    def _annotate(match: re.Match) -> str:
        cid, file_id = match.group(1), match.group(2)
        key = f"{cid}/{file_id}"
        if key not in seen:
            local = _local_path_for(cid, file_id)
            seen[key] = f"{match.group(0)} (local file: {local})" if local else match.group(0)
        return seen[key]

    return _ATTACHMENT_URL_RE.sub(_annotate, message)


def _prompt(
    message: str,
    ctx: dict,
    model: str,
    reasoning: str,
    workspace: str,
    sandbox: str = CODEX_SANDBOX,
) -> str:
    history = ctx.get("messages", [])[-MAX_CONTEXT_MESSAGES:]
    history_lines = common.format_history_lines(history)
    summary = str(ctx.get("summary") or "").strip()
    summary_block = (
        f"[Summary of earlier conversation]\n{summary}\n\n" if summary else ""
    )
    # Trim history lines from the oldest end until the assembled prompt will
    # fit within MAX_PROMPT_CHARS (guards against the Codex CLI chunk-size error).
    preamble_len = 2600  # rough char count of the static system preamble below
    summary_len = len(summary_block)
    budget = MAX_PROMPT_CHARS - preamble_len - summary_len - len(message) - 200
    if budget > 0:
        while history_lines and sum(len(l) for l in history_lines) > budget:
            history_lines.pop(0)
    history_block = "\n\n".join(history_lines) if history_lines else "No prior bridge-local context."

    return (
        "You are Codex inside the user's local Agent Chat roster as @codex. "
        "Answer as Codex running on this computer: warm, concise, "
        "implementation-minded, and honest about what you can verify. "
        f"You have real workspace access at {workspace}. "
        + (
            "This turn is enforced read-only: inspect files and report findings, "
            "but do not edit, create, delete, rename, or execute project files. "
            if sandbox == "read-only" else
            "You can read, edit, and create files there, and should use those "
            "tools when the user asks for file or app work. Prefer reversible "
            "changes, stay inside the assigned workspace, and ask before "
            "destructive or system-wide actions. "
        )
        + f"The Codex subprocess runs with sandbox={sandbox!r}. "
        "Prefer local file paths over remote fetches. "
        f"{CHROME_DEVTOOLS_PROMPT if sandbox != 'read-only' and _chrome_devtools_mcp_wired() else ''}"
        f"This exact turn was invoked through the bridge with model={model!r} "
        f"and reasoning={reasoning!r}. You may report those values when the user "
        "asks what model or reasoning setting you are using, while being clear "
        "they are bridge invocation settings rather than independently queried "
        "runtime metadata from inside the model. "
        "Your memory of this chat is managed for you by the bridge: each turn "
        "you receive a running '[Summary of earlier conversation]' (older turns "
        "auto-condensed by a small model) plus the most recent messages "
        "verbatim, so you keep the gist of long chats without the full "
        "transcript. If asked how your memory or context window works, explain "
        "this rather than guessing. "
        "Attachments the user shares in chat are mirrored to local disk; each is "
        "listed with a 'local file:' path in the message. Read that path "
        "directly instead of trying to fetch a Docker host or Tailscale URL. "
        "The Agent Chat orchestrator has already included the current room "
        "state, attachments protocol, and collaboration instructions in the "
        "new message below.\n\n"
        "Start EVERY reply with its bottom line as a separate first paragraph: "
        "`BOTTOM LINE: <one or two direct sentences>`. It must contain the decisive "
        "answer, finding, number, fix, recommendation, decision, or actual question "
        "and stand alone without teaser, preamble, praise, recap, or fluff. Then a "
        "blank line, then the normal conversational opener and useful detail; do not "
        "merely repeat the bottom line. Even a short reply gets the BOTTOM LINE "
        "paragraph, with no body when that is the complete answer.\n\n"
        f"{summary_block}"
        "[Bridge-local recent context]\n"
        f"{history_block}\n\n"
        "[New Agent Chat prompt]\n"
        f"{message}"
    )


async def _summarize(old_summary: str, messages: list) -> str:
    """Fold older messages into a compact running summary via a cheap model."""
    convo = "\n".join(
        f"{m.get('role', 'message')}: {str(m.get('text', '')).strip()}"
        for m in messages
        if str(m.get("text", "")).strip()
    )
    if not convo.strip():
        return old_summary
    instruction = (
        "You maintain a running summary of a chat so an assistant can recall it "
        "later. Merge the existing summary and the new messages into one updated "
        "summary under ~200 words. Preserve concrete facts, decisions, names, "
        "numbers, file paths, and open questions/todos. Be terse and factual — "
        "output ONLY the summary, no preamble.\n\n"
        f"[Existing summary]\n{old_summary.strip() or '(none yet)'}\n\n"
        f"[New messages]\n{convo}\n\n"
        "[Updated summary]"
    )
    with tempfile.NamedTemporaryFile(prefix="codex-summary-", suffix=".txt", delete=False) as f:
        out_path = Path(f.name)
    try:
        cmd = _codex_command(
            "exec", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "-C", CODEX_WORKDIR, "-m", SUMMARY_MODEL,
            "--output-last-message", str(out_path), "-",
        )
        common.load_env_files(_ENV_FILES, overwrite=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(instruction.encode("utf-8")), timeout=SUMMARY_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError("summary timed out")
        if proc.returncode != 0:
            raise RuntimeError((stderr or stdout).decode("utf-8", errors="replace")[-400:])
        reply = out_path.read_text(errors="replace").strip()
        if not reply:
            reply = stdout.decode("utf-8", errors="replace").strip()
        return reply or old_summary
    finally:
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass


async def _maybe_summarize(ctx: dict) -> None:
    """When a context outgrows the window, compact the overflow into ctx['summary'].
    Fail-open: on any error, hard-cap the message list instead (never raises).
    Policy lives in _bridge_common; _summarize is the Codex-specific model call."""
    await common.maybe_summarize(
        ctx, _summarize,
        trigger=SUMMARY_TRIGGER, keep=SUMMARY_KEEP,
        hard_cap=SUMMARY_HARD_CAP, max_chars=SUMMARY_MAX_CHARS,
    )


def _codex_options(body: dict) -> tuple[str, str]:
    model = str(body.get("model") or CODEX_MODEL).strip()
    reasoning = str(body.get("reasoning") or CODEX_REASONING).strip()
    if model not in MODEL_OPTIONS:
        model = CODEX_MODEL if CODEX_MODEL in MODEL_OPTIONS else "gpt-5.5"
    if reasoning not in REASONING_OPTIONS:
        reasoning = CODEX_REASONING if CODEX_REASONING in REASONING_OPTIONS else "high"
    return model, reasoning


def _codex_command(*args: str) -> list[str]:
    """Build one executable command for native binaries and Windows npm shims.

    npm installs Codex as ``codex.cmd`` on Windows. A batch shim cannot be
    launched directly by ``asyncio.create_subprocess_exec``; doing so produces
    a misleading native launch error even after discovery and login succeeded.
    """
    return common.executable_command(CODEX_BIN, *args)


def _codex_cli_status() -> tuple[bool, str]:
    """Prove the configured CLI can start and has an authenticated session.

    A protected Microsoft Store executable can exist on disk while Windows
    refuses to launch it. The log endpoint is the orchestrator's readiness
    probe, so it must fail closed instead of advertising a false green bridge.
    """
    now = time.monotonic()
    if now - float(_CLI_STATUS_CACHE["ts"]) < _CLI_STATUS_TTL_S:
        return bool(_CLI_STATUS_CACHE["ready"]), str(_CLI_STATUS_CACHE["error"])
    path = Path(CODEX_BIN).expanduser()
    ready = False
    error = "Codex CLI is not installed at the configured location."
    if path.is_file():
        try:
            for args in (("--version",), ("login", "status")):
                command = _codex_command(*args)
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=5,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode != 0:
                    error = (
                        "Codex is installed but not signed in. Open Codex or run "
                        "‘codex login’, then reconnect."
                    )
                    break
            else:
                ready, error = True, ""
        except PermissionError:
            error = (
                "Windows blocked the Codex command from starting. Use a normal "
                "Codex CLI installation, then reconnect it from Agents Chat."
            )
        except (OSError, subprocess.SubprocessError):
            error = "Codex could not start. Open Codex, sign in, then reconnect."
    _CLI_STATUS_CACHE.update({"ts": now, "ready": ready, "error": error})
    return ready, error


async def _require_ready_codex() -> Optional[JSONResponse]:
    ready, error = await asyncio.to_thread(_codex_cli_status)
    if ready:
        return None
    return JSONResponse(
        {"error": error, "retryable": False, "state": "setup-required"},
        status_code=503,
    )


# Markers in a Codex failure that mean "this won't clear on a quick auto-retry"
# (account usage/quota caps, billing). Surfaced as non-retryable so the app raises
# a clear hold immediately instead of burning its short 2s/4s retry budget on a
# daily limit that resets hours later.
_CODEX_NON_RETRYABLE_RE = re.compile(
    r"usage limit|hit your (?:usage|rate) limit|purchase more credits|upgrade to pro|"
    r"\bquota\b|out of credits|insufficient.*credit|payment required|\bbilling\b",
    re.IGNORECASE,
)


class CodexRunError(RuntimeError):
    """A Codex turn failed with a real reason (from the JSONL stream or stderr).
    `retryable` is False for usage/quota caps that a quick retry can't fix; the app
    honors it (see _agent_http_error) to skip pointless retries. `http_status` is
    what the bridge returns so the app classifies it the same way (429 = limit)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.retryable = not bool(_CODEX_NON_RETRYABLE_RE.search(message or ""))
        self.http_status = 500 if self.retryable else 429


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


def _read_codex_output(path: Path) -> str:
    """Read Codex's final-message file using its documented UTF-8 encoding.

    ``Path.read_text()`` otherwise uses the host locale.  On Windows that can
    be a legacy code page, turning text such as ``Account → Security`` into
    mojibake even though the Codex output file itself is valid UTF-8.
    """
    return path.read_text(encoding="utf-8", errors="replace").strip()


async def _run_codex(
    prompt: str, model: str, reasoning: str, context_id: str, workspace: str,
    *, sandbox: str = CODEX_SANDBOX,
) -> "tuple[str, Optional[dict]]":
    with tempfile.NamedTemporaryFile(prefix="codex-agent-", suffix=".txt", delete=False) as f:
        output_path = Path(f.name)
    try:
        # --json streams JSONL events on stdout so we can publish a live
        # "current step" line via _set_progress (handed back by api_log_get).
        # --output-last-message still writes the final reply to a file, which
        # we read for the actual answer.
        cmd = _codex_command(
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "--sandbox",
            sandbox,
            "-C",
            workspace,
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
            "--output-last-message",
            str(output_path),
            "-",
        )
        chrome_override = (
            _chrome_devtools_codex_override() if sandbox != "read-only" else None
        )
        if chrome_override:
            # Insert before the stdin "-" prompt so it is a config flag, not input.
            cmd[-1:-1] = ["-c", chrome_override]
        # Re-read env files so a Secrets-panel grant reaches this spawn (the
        # codex child inherits os.environ) without a bridge restart.
        common.load_env_files(_ENV_FILES, overwrite=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        _set_progress(context_id, "Starting up…", True)

        last_agent_message = ""
        usage_obj: Optional[dict] = None
        # Codex reports failures (usage/rate limits, auth, model errors) as JSONL
        # `error` / `turn.failed` events on STDOUT and exits non-zero with EMPTY
        # stderr — so reading only stderr collapsed every such failure to a useless
        # "Codex exited with 1". Capture the real message here so we can surface it.
        last_error = ""
        async def _feed_stdin() -> None:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        async def _drain_stderr() -> bytes:
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except Exception:
                pass
            return b"".join(chunks)

        def _handle_line(raw: bytes) -> None:
            nonlocal last_agent_message, usage_obj, last_error
            raw = raw.strip()
            if not raw:
                return
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                return
            etype = evt.get("type")
            if etype == "error":
                last_error = str(evt.get("message") or "").strip() or last_error
            elif etype == "turn.failed":
                ferr = evt.get("error") or {}
                last_error = str(ferr.get("message") or "").strip() or last_error
            _maybe_set_partial_from_event(context_id, evt)
            if evt.get("type") == "item.completed":
                item = evt.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    last_agent_message = str(item.get("text"))
            elif evt.get("type") == "turn.completed" and isinstance(evt.get("usage"), dict):
                # Codex attaches exact token usage to turn.completed — capture it
                # so the turn is metered exactly instead of estimated.
                usage_obj = evt["usage"]
            line = _progress_line_from_codex_event(evt)
            if line:
                _set_progress(context_id, line, True)

        async def _read_events() -> None:
            # Read raw chunks and split on newlines ourselves rather than using
            # readline(), whose 64 KiB per-line limit raises "Separator is not
            # found, and chunk exceed the limit" on a big JSONL event. read()
            # has no such cap. (See claude_agent_bridge for the same fix.)
            buf = b""
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    _handle_line(buf[:nl])
                    buf = buf[nl + 1 :]
            if buf:
                _handle_line(buf)

        feeder = asyncio.create_task(_feed_stdin())
        stderr_task = asyncio.create_task(_drain_stderr())
        try:
            await asyncio.wait_for(_read_events(), timeout=CODEX_TIMEOUT_S)
            await proc.wait()
        except asyncio.TimeoutError:
            await _terminate_process_tree(proc)
            _end_progress(context_id)
            raise RuntimeError("Codex timed out while answering.")
        except asyncio.CancelledError:
            await _terminate_process_tree(proc)
            _end_progress(context_id)
            raise
        finally:
            if not feeder.done():
                feeder.cancel()
            try:
                await feeder
            except Exception:
                pass

        stderr_data = b""
        try:
            stderr_data = await stderr_task
        except Exception:
            pass
        _end_progress(context_id)

        if proc.returncode not in (0, None):
            stderr_detail = stderr_data.decode("utf-8", errors="replace").strip()
            # Prefer the real failure reason from the JSON event stream; fall back
            # to stderr, then to the bare exit code.
            detail = last_error or stderr_detail or f"Codex exited with {proc.returncode}."
            raise CodexRunError(detail[-1200:])
        reply = _read_codex_output(output_path)
        if not reply:
            reply = last_agent_message.strip()
        return reply or "(Codex returned an empty response.)", usage_obj
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass


@app.get("/health")
async def health():
    cli_ready, cli_error = await asyncio.to_thread(_codex_cli_status)
    return {
        "status": "ok" if BRIDGE_TOKEN and cli_ready else (
            "missing-token" if not BRIDGE_TOKEN else "setup-required"
        ),
        "codex_bin": CODEX_BIN,
        "codex_bin_present": Path(CODEX_BIN).is_file(),
        "codex_cli_ready": cli_ready,
        "codex_cli_error": cli_error,
        "workdir": CODEX_WORKDIR,
        "workdir_present": Path(CODEX_WORKDIR).is_dir(),
        "model": CODEX_MODEL,
        "reasoning": CODEX_REASONING,
        "sandbox": CODEX_SANDBOX,
        "token_present": bool(BRIDGE_TOKEN),
        "model_options": sorted(MODEL_OPTIONS),
        "reasoning_options": sorted(REASONING_OPTIONS),
        "turn_timeout_seconds": CODEX_TIMEOUT_S,
        "attachment_local_files_dir": str(LOCAL_FILES_DIR),
        "chrome_devtools_mcp_enabled": _chrome_devtools_mcp_wired(),
        "chrome_devtools_mcp_server": (
            CHROME_DEVTOOLS_MCP_SERVER_NAME if _chrome_devtools_mcp_wired() else None
        ),
    }


# Self-reported capability card. The orchestrator's concierge reads this (cached)
# to route work to the right agent instead of guessing from a static table —
# keep `best_for`/`strengths` honest and current as this agent's role evolves.
@app.get("/capabilities")
async def capabilities():
    return {
        "id": "codex",
        "model": CODEX_MODEL,
        "best_for": (
            "Software engineering — code review, debugging, repo navigation, "
            "tests, and tooling, turning a vague task into a concrete "
            "implementation plan with verified diffs and checked commands."
        ),
        "strengths": [
            "implementation & concrete diffs",
            "debugging & root-cause analysis",
            "reading and navigating a codebase",
            "tests, tooling & automation",
            "code review",
            "running and verifying commands locally",
        ],
        "avoid": "Open-ended strategy or long-form prose — prefer a writer/strategist.",
        "voice": (
            "Warm, concise, and implementation-minded; shows checked diffs and "
            "commands instead of describing them, and is honest about what it "
            "has and hasn't verified."
        ),
        "blurb": "Hands-on software engineer; prefers verified diffs over description.",
        "supports": {"read_only_workspace": True},
    }


@app.post("/api/api_log_get")
async def api_log_get(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    blocked = await _require_ready_codex()
    if blocked:
        return blocked
    try:
        body = await request.json()
    except Exception:
        body = {}
    context_id = body.get("context_id")
    if not context_id:
        return JSONResponse({"error": "context_id is required"}, status_code=400)
    entry = PROGRESS.get(context_id) or {}
    return {
        "log": {
            "progress": entry.get("text", ""),
            "progress_active": bool(entry.get("active", False)),
            "partial_response": entry.get("partial_response", ""),
        }
    }


@app.post("/api/api_message")
async def api_message(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    blocked = await _require_ready_codex()
    if blocked:
        return blocked
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    message = _localize_attachments(message)
    model, reasoning = _codex_options(body)
    workspace = common.resolve_workspace(body, CODEX_WORKDIR)
    workspace_access = str(body.get("workspace_access") or "writable").strip().lower()
    if workspace_access not in {"writable", "read-only"}:
        return JSONResponse(
            {"error": "workspace_access must be writable or read-only"},
            status_code=400,
        )
    execution_profile = str(body.get("execution_profile") or "").strip()
    sandbox = (
        "read-only"
        if workspace_access == "read-only"
        else CODEX_HOST_SETUP_SANDBOX
        if execution_profile == "host_setup"
        else CODEX_SANDBOX
    )

    context_id, ctx = _context(body.get("context_id"))
    # Mark the new turn active immediately so a poll landing before the CLI
    # spawns sees "Starting up…" rather than the previous turn's final line.
    # keep_partial=False drops last turn's streamed answer so the live bubble
    # does not replay it while this follow-up is still thinking.
    _set_progress(context_id, "Starting up…", True, keep_partial=False)
    contexts = _load_contexts()
    ctx = contexts.setdefault(context_id, ctx)
    ctx.setdefault("messages", []).append({"role": "user", "text": _strip_system_header(message)})
    ctx["updated_ts"] = int(time.time() * 1000)
    _save_contexts(contexts)

    try:
        reply, usage = await _run_codex(
            _prompt(message, ctx, model, reasoning, workspace, sandbox),
            model, reasoning, context_id, workspace, sandbox=sandbox,
        )
    except Exception as e:
        _end_progress(context_id)
        # Surface the real reason + whether it's worth a quick retry. CodexRunError
        # carries both; a plain error (e.g. a timeout) defaults to a retryable 500.
        status = int(getattr(e, "http_status", 500))
        retryable = bool(getattr(e, "retryable", status in (500, 502, 503, 504)))
        return JSONResponse({"error": str(e), "retryable": retryable}, status_code=status)

    contexts = _load_contexts()
    ctx = contexts.setdefault(context_id, {"messages": []})
    ctx.setdefault("messages", []).append({"role": "assistant", "text": reply})
    ctx["updated_ts"] = int(time.time() * 1000)
    # Compact older turns into a running summary instead of dropping them.
    await _maybe_summarize(ctx)
    _save_contexts(contexts)
    out = {
        "response": reply,
        "context_id": context_id,
        "model": model,
        # Say so when the caller's model pick was coerced, rather than reporting
        # the substitute as if it had been the choice.
        **common.model_fallback_meta(body.get("model"), model),
        "reasoning": reasoning,
    }
    # Exact token usage from Codex's turn.completed event, so the usage
    # dashboard meters this turn exactly instead of estimating it.
    if isinstance(usage, dict):
        out["usage"] = usage
    return out
