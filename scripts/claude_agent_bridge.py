import asyncio
import json
import os
import re
import shutil
import signal
import time
import traceback
from datetime import datetime, timedelta
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
STATE_DIR = APP_DIR / "claude_bridge"
STATE_DIR.mkdir(exist_ok=True)
CONTEXT_STORE = STATE_DIR / "contexts.json"


_ENV_FILES = (
    APP_DIR / ".env",
    REPO_DIR / ".env",
    APP_DIR / "agents" / "shared.env",
    APP_DIR / "agents" / "claude.env",
)
common.load_env_files(_ENV_FILES)

# The Claude Code CLI bundled inside the desktop app or installed on PATH.
# The path is pinned to a version dir (…/claude-code/<ver>/…) that the desktop
# app rewrites on every auto-update — which is exactly what silently broke
# @claude before. _resolve_claude_bin() falls back to the newest installed
# version whenever the configured path is gone, so updates don't take @claude
# down. Override with CLAUDE_BIN to force a specific binary.
def _version_key(name: str) -> tuple:
    parts: list[tuple] = []
    for chunk in re.split(r"[.\-]", name):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)


def _resolve_claude_bin(configured: str) -> str:
    if configured and Path(configured).is_file():
        return configured
    try:
        base = Path(configured)
        while base.name and base.name != "claude-code":
            base = base.parent
        if base.name == "claude-code" and base.is_dir():
            candidates = []
            for child in base.iterdir():
                cand = child / "claude.app" / "Contents" / "MacOS" / "claude"
                if cand.is_file():
                    candidates.append((_version_key(child.name), str(cand)))
            if candidates:
                candidates.sort()
                return candidates[-1][1]
    except Exception:
        pass
    bundled = sorted(
        (Path.home() / "Library" / "Application Support" / "Claude" / "claude-code").glob(
            "*/claude.app/Contents/MacOS/claude"
        ), reverse=True,
    )
    for candidate in [*bundled, Path.home() / ".local" / "bin" / "claude"]:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("claude") or shutil.which("claude.exe") or configured


CLAUDE_BIN_CONFIGURED = os.environ.get(
    "CLAUDE_BIN", "",
)
# Resolved once at import, then re-resolved on the fly whenever the cached path
# vanishes. The bridge is long-lived, so a path resolved at startup can be
# deleted out from under us when the desktop app auto-updates mid-run (it
# rewrites …/claude-code/<ver>/…). Always call current_claude_bin() — never a
# stale module-level constant — so updates never take @claude down.
_CLAUDE_BIN_CACHE = _resolve_claude_bin(CLAUDE_BIN_CONFIGURED)


def current_claude_bin() -> str:
    global _CLAUDE_BIN_CACHE
    if Path(_CLAUDE_BIN_CACHE).is_file():
        return _CLAUDE_BIN_CACHE
    _CLAUDE_BIN_CACHE = _resolve_claude_bin(CLAUDE_BIN_CONFIGURED)
    return _CLAUDE_BIN_CACHE
# Fallback workspace when the orchestrator doesn't pass a per-chat `workspace`
# (a direct/legacy caller). Deliberately NOT the Agent-Chat repo — agents must
# never do real work inside the chat app folder. The orchestrator normally
# sends a per-chat workspace that overrides this.
CLAUDE_WORKDIR = os.environ.get(
    "CLAUDE_BRIDGE_WORKDIR", str(Path.home() / "AGENTS" / "workspaces" / "claude")
)
CLAUDE_MODEL = os.environ.get("CLAUDE_BRIDGE_MODEL", "claude-opus-5")
CLAUDE_EFFORT = os.environ.get("CLAUDE_BRIDGE_EFFORT", "high")
CLAUDE_TIMEOUT_HARD_CAP_S = float(
    os.environ.get("CLAUDE_BRIDGE_TIMEOUT_HARD_CAP_S", "600")
)
CLAUDE_TIMEOUT_S = min(
    float(os.environ.get("CLAUDE_BRIDGE_TIMEOUT_S", "600")),
    CLAUDE_TIMEOUT_HARD_CAP_S,
)
CLAUDE_TOOL_IDLE_TIMEOUT_S = float(
    os.environ.get("CLAUDE_BRIDGE_TOOL_IDLE_TIMEOUT_S", "240")
)
MAX_CONTEXT_MESSAGES = int(os.environ.get("CLAUDE_BRIDGE_MAX_CONTEXT_MESSAGES", "12"))
# Rolling summarization: once a context exceeds SUMMARY_TRIGGER messages, fold
# everything but the last SUMMARY_KEEP into a persistent running summary (via a
# cheap model) so the agent keeps long-term memory of a chat without the prompt
# growing forever. SUMMARY_HARD_CAP bounds messages if summarization is
# unavailable (fail-open: same memory loss as before the feature, never a
# runaway prompt).
SUMMARY_TRIGGER = int(os.environ.get("CLAUDE_BRIDGE_SUMMARY_TRIGGER", "16"))
SUMMARY_KEEP = int(os.environ.get("CLAUDE_BRIDGE_SUMMARY_KEEP", "8"))
SUMMARY_HARD_CAP = int(os.environ.get("CLAUDE_BRIDGE_SUMMARY_HARD_CAP", "24"))
SUMMARY_MODEL = os.environ.get("CLAUDE_BRIDGE_SUMMARY_MODEL", "claude-haiku-4-5")
SUMMARY_TIMEOUT_S = float(os.environ.get("CLAUDE_BRIDGE_SUMMARY_TIMEOUT_S", "120"))
# Hard ceiling on the stored running summary, so even a model that ignores the
# ~200-word instruction can't slowly bloat the prompt over many compactions.
SUMMARY_MAX_CHARS = int(os.environ.get("CLAUDE_BRIDGE_SUMMARY_MAX_CHARS", "2000"))
BRIDGE_TOKEN = os.environ.get("AGENT_TOKEN_CLAUDE", "").strip()
# The exact `--model` values the Claude CLI accepts, verified against the
# installed binary rather than copied from a docs page: an unrecognised name is
# taken at face value at init and only fails at the API layer, which surfaces as
# an empty "<synthetic>" turn rather than a clean error.
MODEL_OPTIONS = {
    "claude-fable-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
}
EFFORT_OPTIONS = {"low", "medium", "high", "xhigh", "max"}

# A running bridge process is not necessarily a usable Claude provider. Record
# the last real user-facing CLI outcome so /health can stop reporting a false
# green while Claude Code is refusing every turn for an account/session cap.
_CLAUDE_CAP_RE = re.compile(
    r"usage limit|session limit|hit your (?:usage|rate|session) limit|"
    r"purchase more credits|upgrade to pro|\bquota\b|out of credits|"
    r"insufficient.*credit|payment required|\bbilling\b",
    re.IGNORECASE,
)
_CLAUDE_RESET_RE = re.compile(
    # A clock needs :MM or an am/pm suffix — a bare number ("again 30 minutes
    # later") must not parse as an hour-of-day.
    r"(?:try again|reset[s]?|again)\s+(?:at\s+)?"
    r"(\d{1,2})(?::(\d{2})\s*([ap]\.?m\.?)?|\s*([ap]\.?m\.?))",
    re.IGNORECASE,
)
_LAST_UPSTREAM: dict = {
    "ok": None,
    "kind": None,
    "error": "",
    "ts": 0,
    "reset_ts": 0,
}
UPSTREAM_FAIL_TTL_MS = int(
    os.environ.get("CLAUDE_BRIDGE_UPSTREAM_FAIL_TTL_MS", str(15 * 60 * 1000))
)
CAP_DEFAULT_COOLDOWN_MS = int(
    os.environ.get("CLAUDE_BRIDGE_CAP_DEFAULT_COOLDOWN_MS", str(60 * 60 * 1000))
)


def _parse_cap_reset_ms(message: str, now_ms: Optional[int] = None) -> int:
    """Best-effort reset clock for Claude's `resets 7:40pm` limit message."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        match = _CLAUDE_RESET_RE.search(message or "")
        if not match:
            return now_ms + CAP_DEFAULT_COOLDOWN_MS
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        ampm = ((match.group(3) or match.group(4)) or "").lower().replace(".", "")
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return now_ms + CAP_DEFAULT_COOLDOWN_MS
        now = datetime.fromtimestamp(now_ms / 1000)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return int(target.timestamp() * 1000)
    except Exception:
        return now_ms + CAP_DEFAULT_COOLDOWN_MS


def _record_upstream(ok: bool, error: str = "") -> None:
    now_ms = int(time.time() * 1000)
    is_cap = (not ok) and bool(_CLAUDE_CAP_RE.search(error or ""))
    _LAST_UPSTREAM.update({
        "ok": bool(ok),
        "kind": None if ok else ("quota" if is_cap else "error"),
        "error": "" if ok else str(error or "")[:500],
        "ts": now_ms,
        "reset_ts": _parse_cap_reset_ms(error, now_ms) if is_cap else 0,
    })


def _health_status() -> tuple[str, Optional[str]]:
    if not BRIDGE_TOKEN:
        return "missing-token", "No inbound bridge token (AGENT_TOKEN_CLAUDE)."
    if not Path(current_claude_bin()).is_file():
        return "degraded", "Claude Code binary is missing."
    now_ms = int(time.time() * 1000)
    if _LAST_UPSTREAM["ok"] is False:
        if _LAST_UPSTREAM["kind"] == "quota":
            if int(_LAST_UPSTREAM.get("reset_ts") or 0) > now_ms:
                return "degraded", _LAST_UPSTREAM["error"] or "Claude session limit reached."
        elif now_ms - int(_LAST_UPSTREAM["ts"] or 0) < UPSTREAM_FAIL_TTL_MS:
            return "degraded", _LAST_UPSTREAM["error"] or "The last Claude call failed."
    return "ok", None


class ClaudeRunError(RuntimeError):
    """A CLI failure with retry semantics the app can honor."""

    def __init__(self, message: str):
        super().__init__(message)
        self.retryable = not bool(_CLAUDE_CAP_RE.search(message or ""))
        self.http_status = 500 if self.retryable else 429


class ClaudeGuardError(ClaudeRunError):
    """A local safety stop with exact partial usage for Agent Chat to meter."""

    def __init__(self, message: str, *, usage: Optional[dict] = None,
                 partial_response: str = ""):
        super().__init__(message)
        self.retryable = False
        self.http_status = 422
        self.usage = usage if isinstance(usage, dict) else None
        self.partial_response = str(partial_response or "").strip()

# Tool access. By default the agent can see and edit files in its workspace and
# run commands (parity with the Codex bridge's workspace-write sandbox). Tighten
# by overriding these env vars — e.g. drop "Bash" or set PERMISSION_MODE=default.
PERMISSION_MODE = os.environ.get("CLAUDE_BRIDGE_PERMISSION_MODE", "acceptEdits").strip()
ALLOWED_TOOLS = os.environ.get(
    "CLAUDE_BRIDGE_ALLOWED_TOOLS",
    "Read,Edit,Write,MultiEdit,Glob,Grep,Bash,WebFetch,WebSearch,TodoWrite,NotebookEdit",
).strip()
READ_ONLY_ALLOWED_TOOLS = "Read,Glob,Grep"
READ_ONLY_DISALLOWED_TOOLS = "Edit,Write,MultiEdit,Bash,NotebookEdit"
ADD_DIRS = [
    d.strip()
    for d in re.split(r"[,\n]", os.environ.get("CLAUDE_BRIDGE_ADD_DIRS", ""))
    if d.strip()
]

# --- Zapier MCP wiring ------------------------------------------------------
# Optional Zapier MCP server ("connect to your own MCP client" in the Zapier UI)
# is a standard streamable-HTTP MCP endpoint. The Claude Code CLI speaks MCP
# natively, so we don't need a REST translation layer: we just hand the CLI a
# generated --mcp-config pointing at the endpoint, and add the Zapier tools to
# the allowlist. Credentials come from the host env layer (agents/shared.env or
# agents/claude.env) — NOT the Agent Zero container secrets.env, which this
# native bridge can't read.
#   ZAPIER_MCP_URL    = https://mcp.zapier.com/api/mcp/s/<id>/mcp
#   ZAPIER_MCP_TOKEN  = <bearer token from the same screen>  (optional if the
#                       URL already embeds the secret; sent as Authorization)
# Tool scoping is deliberate. Default to the whole server only if the user hasn't
# pinned a list; for the shared room we recommend keeping send/delete OFF by
# setting ZAPIER_MCP_ALLOWED_TOOLS to just the read/draft tools you enabled in
# Zapier (e.g. mcp__zapier__gmail_create_draft,mcp__zapier__gmail_find_email).
ZAPIER_MCP_URL = os.environ.get("ZAPIER_MCP_URL", "").strip()
ZAPIER_MCP_TOKEN = os.environ.get("ZAPIER_MCP_TOKEN", "").strip()
ZAPIER_MCP_SERVER_NAME = os.environ.get("ZAPIER_MCP_SERVER_NAME", "zapier").strip() or "zapier"
ZAPIER_MCP_ALLOWED_TOOLS = os.environ.get(
    "ZAPIER_MCP_ALLOWED_TOOLS", f"mcp__{ZAPIER_MCP_SERVER_NAME}"
).strip()
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
CHROME_DEVTOOLS_MCP_ALLOWED_TOOLS = os.environ.get(
    "CHROME_DEVTOOLS_MCP_ALLOWED_TOOLS",
    f"mcp__{CHROME_DEVTOOLS_MCP_SERVER_NAME}",
).strip()
MCP_CONFIG_PATH: Optional[str] = None
CHROME_DEVTOOLS_MCP_WIRED = False
ZAPIER_MCP_WIRED = False

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


def _chrome_devtools_mcp_server() -> Optional[dict]:
    if not CHROME_DEVTOOLS_MCP_ENABLED or not CHROME_DEVTOOLS_MCP_URL:
        return None
    if not _is_loopback_http_url(CHROME_DEVTOOLS_MCP_URL):
        return None
    return {"type": "http", "url": CHROME_DEVTOOLS_MCP_URL}


def _build_mcp_config() -> Optional[str]:
    """Write a Claude-CLI mcp-config JSON for Zapier and/or the shared
    chrome-devtools HTTP server. Regenerated at import so a rotated token in
    the env layer takes effect on the next bridge restart."""
    servers: dict = {}
    chrome = _chrome_devtools_mcp_server()
    if chrome:
        servers[CHROME_DEVTOOLS_MCP_SERVER_NAME] = chrome
    if ZAPIER_MCP_URL:
        server: dict = {"type": "http", "url": ZAPIER_MCP_URL}
        if ZAPIER_MCP_TOKEN:
            server["headers"] = {"Authorization": f"Bearer {ZAPIER_MCP_TOKEN}"}
        servers[ZAPIER_MCP_SERVER_NAME] = server
    if not servers:
        return None
    dest = STATE_DIR / "mcp_config.json"
    try:
        dest.write_text(json.dumps({"mcpServers": servers}, indent=2))
        os.chmod(dest, 0o600)  # token at rest — keep it owner-only
        return str(dest)
    except OSError:
        return None


MCP_CONFIG_PATH = _build_mcp_config()
CHROME_DEVTOOLS_MCP_WIRED = bool(
    MCP_CONFIG_PATH and _chrome_devtools_mcp_server()
)
ZAPIER_MCP_WIRED = bool(MCP_CONFIG_PATH and ZAPIER_MCP_URL)
# Fold optional MCP servers into the CLI allowlist only when they are wired.
if ZAPIER_MCP_WIRED and ZAPIER_MCP_ALLOWED_TOOLS:
    ALLOWED_TOOLS = ",".join(t for t in (ALLOWED_TOOLS, ZAPIER_MCP_ALLOWED_TOOLS) if t)
if CHROME_DEVTOOLS_MCP_WIRED and CHROME_DEVTOOLS_MCP_ALLOWED_TOOLS:
    ALLOWED_TOOLS = ",".join(
        t for t in (ALLOWED_TOOLS, CHROME_DEVTOOLS_MCP_ALLOWED_TOOLS) if t
    )

# Attachments shared in chat are served to Dockerized agents at a host URL this
# native bridge can't reach. The files live on this same disk, so we mirror them
# (with their real filename/extension) and point Claude at the local copy so it
# can Read them — including images/screenshots.
ATTACHMENTS_DIR = Path(os.environ.get("DATA_DIR", APP_DIR)) / "attachments"
LOCAL_FILES_DIR = STATE_DIR / "files"
LOCAL_FILES_DIR.mkdir(exist_ok=True)
_ATTACHMENT_URL_RE = re.compile(r"https?://[^/\s]+/attachments/(\d+)/([a-f0-9]{32})")

app = FastAPI(title="Claude Agent Chat Bridge")


# Live "current step" text per context_id, surfaced to Agent Chat via
# api_log_get so @claude shows a thinking line while it works — the same
# mechanism the Agent Zero agents (Lead/Sales) use. Updated as the streamed
# claude-cli events arrive in _run_claude. In-memory only (single uvicorn
# worker), bounded below to avoid unbounded growth on a long-lived process.
PROGRESS: dict[str, dict] = {}


def _set_progress(context_id: str, text: str, active: bool = True, **kwargs) -> None:
    common.set_progress(PROGRESS, context_id, text, active, **kwargs)


def _end_progress(context_id: str) -> None:
    common.end_progress(PROGRESS, context_id)


_clip = common.clip


def _tool_use_line(block: dict) -> Optional[str]:
    """Human one-liner for a tool_use content block (Read/Edit/Bash/…)."""
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    base = lambda p: os.path.basename(str(p)) if p else ""  # noqa: E731
    if name == "Bash":
        desc = str(inp.get("description") or "").strip()
        return _clip(desc or ("Running: " + str(inp.get("command") or "").strip()), 90)
    if name == "Read":
        return _clip("Reading " + base(inp.get("file_path")), 90)
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return _clip("Editing " + base(inp.get("file_path") or inp.get("notebook_path")), 90)
    if name == "Glob":
        return _clip("Finding files: " + str(inp.get("pattern") or "").strip(), 90)
    if name == "Grep":
        return _clip("Searching: " + str(inp.get("pattern") or "").strip(), 90)
    if name == "WebFetch":
        return _clip("Fetching " + str(inp.get("url") or "").strip(), 90)
    if name == "WebSearch":
        return _clip("Searching the web: " + str(inp.get("query") or "").strip(), 90)
    if name == "TodoWrite":
        return "Updating plan"
    if name == "Task":
        return "Delegating to a subagent"
    return _clip("Using " + str(name), 90)


def _progress_line_from_event(evt: dict) -> Optional[str]:
    """Map one stream-json event to a short 'current step' line, or None."""
    etype = evt.get("type")
    if etype == "system":
        sub = evt.get("subtype")
        if sub == "init":
            return "Starting up…"
        if sub == "thinking_tokens":
            return "Thinking…"
        if sub == "post_turn_summary":
            detail = str(evt.get("status_detail") or "").strip()
            return _clip(detail) if detail else None
        return None
    if etype == "assistant":
        line: Optional[str] = None
        for block in (evt.get("message") or {}).get("content") or []:
            btype = block.get("type")
            if btype == "thinking":
                thought = str(block.get("thinking") or "").strip()
                if thought:
                    line = _clip(thought)
            elif btype == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    line = _clip(text)
            elif btype == "tool_use":
                line = _tool_use_line(block)
        return line
    return None


def _child_env() -> dict:
    """Environment for the claude subprocess.

    The bridge is meant to authenticate with the standalone CLI's own
    subscription login (run once via `claude /login`). If this process was
    started from the desktop harness it may carry an empty ANTHROPIC_API_KEY
    or a staging base URL that would shadow that login, so strip those.
    """
    # Re-read the env files first: a Secrets-panel grant lands in
    # agents/claude.env while this process is long-lived, and must reach the
    # NEXT spawned agent without waiting for a bridge restart.
    common.load_env_files(_ENV_FILES, overwrite=True)
    env = dict(os.environ)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        if not env.get(key, "").strip():
            env.pop(key, None)
    for key in ("USE_STAGING_OAUTH", "USE_LOCAL_OAUTH"):
        env.pop(key, None)
    return env


def _load_contexts() -> dict:
    return common.load_contexts(CONTEXT_STORE)


def _save_contexts(contexts: dict) -> None:
    common.save_contexts(CONTEXT_STORE, contexts)


def _context(context_id: Optional[str]) -> tuple[str, dict]:
    return common.get_or_create_context(CONTEXT_STORE, context_id)


def _authorized(request: Request) -> bool:
    return common.authorized(request, BRIDGE_TOKEN)


SYSTEM_PROMPT = (
    "You are Claude inside the user's local Agent Chat roster as @claude, running "
    "through Claude Code on this computer. Be warm, precise, and honest about what "
    "you can verify. "
    "You have real tool access in your per-chat workspace (its path is stated below): "
    "you can read, "
    "edit, and create files and run commands. Use them when they help answer — read "
    "files before describing them, and make the edits the user asks for. Prefer "
    "reversible changes, stay inside the assigned workspace, and ask before destructive "
    "or system-wide actions. "
    "Attachments the user shares in chat are mirrored to local disk; each is listed with "
    "a 'local file:' path in the message — Read that path directly (it works for "
    "images/screenshots too) instead of trying to fetch the http URL. "
    "The Agent Chat orchestrator has already included the current room state and "
    "collaboration instructions in the user message."
)
if CHROME_DEVTOOLS_MCP_WIRED:
    SYSTEM_PROMPT += " " + CHROME_DEVTOOLS_PROMPT


def _localize_attachments(message: str) -> str:
    """Mirror any chat attachment referenced by URL to a locally readable copy
    (with its real filename) and annotate the URL with that local path, so the
    native bridge agent can Read it without fetching the unreachable host URL."""
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

    def _annotate(m: re.Match) -> str:
        cid, file_id = m.group(1), m.group(2)
        key = f"{cid}/{file_id}"
        if key not in seen:
            local = _local_path_for(cid, file_id)
            seen[key] = f"{m.group(0)} (local file: {local})" if local else m.group(0)
        return seen[key]

    return _ATTACHMENT_URL_RE.sub(_annotate, message)


# Room-header stripping lives in _bridge_common (single source of truth shared
# with the Codex bridge). Alias keeps existing call sites unchanged.
_strip_system_header = common.strip_system_header


def _prompt(message: str, ctx: dict) -> str:
    history = ctx.get("messages", [])[-MAX_CONTEXT_MESSAGES:]
    history_lines = common.format_history_lines(history)
    history_block = "\n\n".join(history_lines) if history_lines else "No prior bridge-local context."
    blocks = []
    summary = str(ctx.get("summary") or "").strip()
    if summary:
        blocks.append("[Summary of earlier conversation]\n" + summary)
    blocks.append("[Bridge-local recent context]\n" + history_block)
    blocks.append("[New Agent Chat prompt]\n" + message)
    return "\n\n".join(blocks)


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
    cmd = common.executable_command(
        current_claude_bin(), "-p", "--model", SUMMARY_MODEL,
        "--output-format", "text",
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=CLAUDE_WORKDIR,
        env=_child_env(),
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
    return stdout.decode("utf-8", errors="replace").strip() or old_summary


async def _maybe_summarize(ctx: dict) -> None:
    """When a context outgrows the window, compact the overflow into ctx['summary'].
    Fail-open: on any error, hard-cap the message list instead (never raises).
    Policy lives in _bridge_common; _summarize is the Claude-specific model call."""
    await common.maybe_summarize(
        ctx, _summarize,
        trigger=SUMMARY_TRIGGER, keep=SUMMARY_KEEP,
        hard_cap=SUMMARY_HARD_CAP, max_chars=SUMMARY_MAX_CHARS,
    )


def _claude_options(body: dict) -> tuple[str, str]:
    model = str(body.get("model") or CLAUDE_MODEL).strip()
    effort = str(body.get("reasoning") or body.get("effort") or CLAUDE_EFFORT).strip()
    if model not in MODEL_OPTIONS:
        model = CLAUDE_MODEL if CLAUDE_MODEL in MODEL_OPTIONS else "claude-opus-5"
    if effort not in EFFORT_OPTIONS:
        effort = CLAUDE_EFFORT if CLAUDE_EFFORT in EFFORT_OPTIONS else "high"
    return model, effort


_USAGE_INT_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens",
)


def _aggregate_assistant_usage(by_message: dict[str, dict]) -> Optional[dict]:
    """Sum one usage object per Claude model message, ignoring streamed repeats."""
    if not by_message:
        return None
    total = {key: 0 for key in _USAGE_INT_FIELDS}
    for usage in by_message.values():
        for key in _USAGE_INT_FIELDS:
            try:
                total[key] += max(0, int(usage.get(key) or 0))
            except (TypeError, ValueError):
                continue
    total["model_calls"] = len(by_message)
    return total


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Stop Claude and any Bash/MCP children it launched for this turn."""
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



async def _run_claude(
    prompt: str, model: str, effort: str, context_id: str, workspace: str,
    *, workspace_access: str = "writable",
) -> "tuple[str, Optional[dict], Optional[float]]":
    # Tell the agent the exact model + effort Agent Chat dialed in, so when the user
    # asks "what are you set to?" it reports these instead of guessing. These are
    # the values actually passed below via --model/--effort, so they're accurate.
    system_prompt = (
        f"{SYSTEM_PROMPT} Your working directory for this chat is {workspace}; "
        + (
            "this turn is enforced read-only. Inspect files and report findings; "
            "do not edit, create, delete, rename, or execute project files. "
            if workspace_access == "read-only" else
            "read, edit, and create files there. "
        )
        +
        f"Agent Chat is running you with model '{model}' and "
        f"reasoning effort '{effort}', set on your card in the Agent Chat UI. If "
        "asked which model or reasoning level you're using, state these values "
        "directly rather than guessing from your own context. "
        "Your memory of this chat is managed for you by the bridge: each turn you "
        "receive a running '[Summary of earlier conversation]' (older turns "
        "auto-condensed by a small model) plus the most recent messages "
        "verbatim, so you keep the gist of long chats without the full "
        "transcript. If asked how your memory or context window works, explain "
        "this rather than guessing."
    )
    # stream-json (instead of plain text) lets us watch each step — thinking,
    # tool calls, narration — as it happens and publish a live "current step"
    # line via _set_progress, which api_log_get hands back to Agent Chat.
    cmd = common.executable_command(
        current_claude_bin(),
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--append-system-prompt",
        system_prompt,
        "--output-format",
        "stream-json",
        "--verbose",
    )
    if MCP_CONFIG_PATH and workspace_access != "read-only":
        cmd += ["--mcp-config", MCP_CONFIG_PATH, "--strict-mcp-config"]
    permission_mode = "plan" if workspace_access == "read-only" else PERMISSION_MODE
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    allowed = READ_ONLY_ALLOWED_TOOLS if workspace_access == "read-only" else ALLOWED_TOOLS
    if allowed:
        cmd += ["--allowedTools", allowed]
    if workspace_access == "read-only":
        cmd += ["--disallowedTools", READ_ONLY_DISALLOWED_TOOLS]
    for d in ADD_DIRS if workspace_access != "read-only" else []:
        cmd += ["--add-dir", d]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
        env=_child_env(),
        start_new_session=True,
    )
    _set_progress(context_id, "Starting up…", True)

    final_text: Optional[str] = None
    err_detail: Optional[str] = None
    text_chunks: list[str] = []
    usage_obj: Optional[dict] = None
    cost_usd: Optional[float] = None
    assistant_usage: dict[str, dict] = {}
    idle_limit_hit = False

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

    def _handle_line(raw: bytes) -> bool:
        nonlocal final_text, err_detail, usage_obj, cost_usd
        raw = raw.strip()
        if not raw:
            return False
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            return False
        etype = evt.get("type")
        if etype == "result":
            # Claude's CLI attaches exact token usage + total cost to the final
            # result event — capture both so the turn is metered exactly.
            if isinstance(evt.get("usage"), dict):
                usage_obj = dict(evt["usage"])
                usage_obj["model_calls"] = max(
                    1, len(assistant_usage), int(usage_obj.get("model_calls") or 0)
                )
            if isinstance(evt.get("total_cost_usd"), (int, float)):
                cost_usd = float(evt["total_cost_usd"])
            if evt.get("is_error"):
                err_detail = str(evt.get("result") or "").strip() or None
            else:
                final_text = str(evt.get("result") or "").strip() or None
        elif etype == "assistant":
            message = evt.get("message") or {}
            message_id = str(message.get("id") or "").strip()
            message_usage = message.get("usage")
            if message_id and isinstance(message_usage, dict):
                # stream-json can emit several content blocks carrying the same
                # message id. Keep one usage object per actual model call.
                assistant_usage[message_id] = dict(message_usage)
            blocks = message.get("content") or []
            for block in blocks:
                if block.get("type") == "text" and block.get("text"):
                    text_chunks.append(str(block.get("text")))
                    common.set_partial_response(PROGRESS, context_id, "".join(text_chunks))
        line = _progress_line_from_event(evt)
        if line:
            _set_progress(context_id, line, True)
        return False

    async def _read_events() -> None:
        nonlocal idle_limit_hit
        # Read raw chunks and split on newlines ourselves rather than using
        # readline(). stream-json emits one JSON event per line, and a single
        # line (a big tool_result, a long final `result`, etc.) can exceed
        # StreamReader.readline()'s 64 KiB limit — which raised
        # "Separator is not found, and chunk exceed the limit" → HTTP 500.
        # read() has no such per-line cap.
        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(65536), timeout=CLAUDE_TOOL_IDLE_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                idle_limit_hit = True
                await _terminate_process_tree(proc)
                return
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
        await asyncio.wait_for(_read_events(), timeout=CLAUDE_TIMEOUT_S)
        await proc.wait()
    except asyncio.TimeoutError:
        await _terminate_process_tree(proc)
        _end_progress(context_id)
        raise ClaudeGuardError(
            f"Claude stopped after the {CLAUDE_TIMEOUT_S:g}s turn safety timeout.",
            usage=_aggregate_assistant_usage(assistant_usage),
            partial_response="".join(text_chunks),
        )
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

    partial_usage = _aggregate_assistant_usage(assistant_usage)
    if idle_limit_hit:
        raise ClaudeGuardError(
            f"Claude stopped after {CLAUDE_TOOL_IDLE_TIMEOUT_S:g}s without tool or model progress.",
            usage=partial_usage,
            partial_response="".join(text_chunks),
        )

    if proc.returncode not in (0, None):
        detail = err_detail or stderr_data.decode("utf-8", errors="replace").strip()
        raise ClaudeRunError(detail[-1200:] or f"Claude exited with {proc.returncode}.")
    if final_text is None and err_detail:
        raise ClaudeRunError(err_detail[-1200:])
    if isinstance(usage_obj, dict):
        usage_obj["model_calls"] = max(
            1, len(assistant_usage), int(usage_obj.get("model_calls") or 0)
        )
    reply = final_text or " ".join(c for c in text_chunks if c).strip()
    return reply or "(Claude returned an empty response.)", usage_obj, cost_usd


@app.get("/health")
async def health():
    status, reason = _health_status()
    data = {
        "status": status,
        "claude_bin": current_claude_bin(),
        "claude_bin_present": Path(current_claude_bin()).is_file(),
        "workdir": CLAUDE_WORKDIR,
        "model": CLAUDE_MODEL,
        "effort": CLAUDE_EFFORT,
        "token_present": bool(BRIDGE_TOKEN),
        "model_options": sorted(MODEL_OPTIONS),
        "reasoning_options": sorted(EFFORT_OPTIONS),
        "tool_idle_timeout_seconds": CLAUDE_TOOL_IDLE_TIMEOUT_S,
        "turn_timeout_seconds": CLAUDE_TIMEOUT_S,
        "permission_mode": PERMISSION_MODE,
        "allowed_tools": ALLOWED_TOOLS,
        "add_dirs": ADD_DIRS,
        # MCP wiring — confirms endpoints are configured without echoing tokens.
        "zapier_mcp_enabled": ZAPIER_MCP_WIRED,
        "zapier_mcp_server": ZAPIER_MCP_SERVER_NAME if ZAPIER_MCP_WIRED else None,
        "zapier_mcp_allowed_tools": ZAPIER_MCP_ALLOWED_TOOLS if ZAPIER_MCP_WIRED else None,
        "chrome_devtools_mcp_enabled": CHROME_DEVTOOLS_MCP_WIRED,
        "chrome_devtools_mcp_server": (
            CHROME_DEVTOOLS_MCP_SERVER_NAME if CHROME_DEVTOOLS_MCP_WIRED else None
        ),
    }
    if reason:
        data["reason"] = reason
    if _LAST_UPSTREAM["ok"] is not None:
        data["last_upstream_ok"] = bool(_LAST_UPSTREAM["ok"])
        data["last_upstream_kind"] = _LAST_UPSTREAM["kind"]
        data["last_upstream_ts"] = _LAST_UPSTREAM["ts"]
        if _LAST_UPSTREAM.get("reset_ts"):
            data["reset_ts"] = _LAST_UPSTREAM["reset_ts"]
    return data


# Self-reported capability card. The orchestrator's concierge reads this (cached)
# to route work to the right agent instead of guessing from a static table —
# keep `best_for`/`strengths` honest and current as this agent's role evolves.
@app.get("/capabilities")
async def capabilities():
    return {
        "id": "claude",
        "model": CLAUDE_MODEL,
        "best_for": (
            "Reasoning, writing, and careful coding — analysis, drafting and "
            "editing, research synthesis, architecture and trade-off discussion, "
            "and reviewing work for soundness."
        ),
        "strengths": [
            "analysis & structured reasoning",
            "writing & editing",
            "research synthesis",
            "architecture & trade-off discussion",
            "code review",
            "explaining clearly and flagging uncertainty",
        ],
        "avoid": "Long, fully-autonomous headless jobs — prefer a contained agent.",
        "voice": (
            "Thinks out loud, weighs trade-offs, and flags uncertainty honestly; "
            "a careful reasoning partner who writes clearly and won't bluff."
        ),
        "blurb": "Reasoning partner and writer; thinks out loud and flags uncertainty honestly.",
        "supports": {"read_only_workspace": True},
    }


@app.post("/api/api_log_get")
async def api_log_get(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)
    message = _localize_attachments(message)
    model, effort = _claude_options(body)
    workspace = common.resolve_workspace(body, CLAUDE_WORKDIR)
    workspace_access = str(body.get("workspace_access") or "writable").strip().lower()
    if workspace_access not in {"writable", "read-only"}:
        return JSONResponse(
            {"error": "workspace_access must be writable or read-only"},
            status_code=400,
        )

    context_id, ctx = _context(body.get("context_id"))
    # Mark the new turn active immediately so a poll landing before the CLI
    # spawns sees "Starting up…" rather than the previous turn's final line.
    # keep_partial=False drops last turn's streamed answer so the live bubble
    # does not replay it while this follow-up is still thinking.
    _set_progress(context_id, "Starting up…", True, keep_partial=False)
    contexts = _load_contexts()
    ctx = contexts.setdefault(context_id, ctx)
    # Store only the real content for replay; the live room header is injected
    # fresh each turn by the orchestrator, so keeping copies here just bloats
    # every future prompt. The current turn below still uses the full `message`.
    ctx.setdefault("messages", []).append({"role": "user", "text": _strip_system_header(message)})
    ctx["updated_ts"] = int(time.time() * 1000)
    _save_contexts(contexts)

    try:
        reply, usage, cost_usd = await _run_claude(
            _prompt(message, ctx), model, effort, context_id, workspace,
            workspace_access=workspace_access,
        )
    except Exception as e:
        _end_progress(context_id)
        # A local Agent Chat step/idle/turn guard says nothing about Anthropic's
        # health. Keep the provider signal truthful instead of turning our own
        # protective stop into a false degraded-provider alert.
        if not isinstance(e, ClaudeGuardError):
            _record_upstream(False, str(e))
        # str(e) used to go out only in the HTTP body (nothing logs it), leaving
        # a bare uvicorn "500". Log the full traceback to stderr (-> the bridge
        # log) with the context_id so 500s on orchestrator-resumed turns are
        # self-diagnosing.
        status = int(getattr(e, "http_status", 500))
        retryable = bool(getattr(e, "retryable", status in (500, 502, 503, 504)))
        print(
            f"[claude] api_message {status} context_id={context_id}: {e!r}",
            file=_sys.stderr,
            flush=True,
        )
        traceback.print_exc()
        error_payload = {"error": str(e), "retryable": retryable}
        usage = getattr(e, "usage", None)
        if isinstance(usage, dict):
            error_payload["usage"] = usage
        partial = str(getattr(e, "partial_response", "") or "").strip()
        if partial:
            error_payload["partial_response"] = partial
        error_payload["model"] = model
        error_payload["reasoning"] = effort
        return JSONResponse(error_payload, status_code=status)

    _record_upstream(True)

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
        "reasoning": effort,
    }
    # Exact token usage + cost straight from the Claude CLI's result event, so
    # the usage dashboard meters this turn exactly instead of estimating it.
    if isinstance(usage, dict):
        out["usage"] = usage
    if cost_usd is not None:
        out["cost_usd"] = cost_usd
    return out
