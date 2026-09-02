"""minimax Agent Chat bridge — opencode CLI with cloud-direct fallback.

Historically this bridge drove a local **Mavis daemon** (the MiniMax Code
desktop app's backend) over HTTP on 127.0.0.1:15321, turning each Agent Chat
turn into one Mavis session turn. The daemon kept the conversation history
server-side, so the bridge was effectively stateless across turns.

That local daemon is **gone**: the MiniMax Code app update (v3.0.48, 2026-07-12)
removed the standalone :15321 HTTP daemon entirely — both the running process
and the shipped binary — and now serves its runtime in-process over an Electron
`app://` protocol that exposes no TCP port. There is nothing on :15321 to talk
to anymore, and no way to run the daemon standalone.

So this bridge now calls the **MiniMax cloud directly**. The MiniMax "agent"
gateway exposes an Anthropic-Messages-shaped endpoint —
`https://agent.minimax.io/mavis/api/v1/llm/v1/messages` — authenticated with the
JWT the desktop app maintains in `~/.minimax/local-runtime.auth.json`. This is
the same MiniMax-M3 / M2.7 catalog the daemon used, just reached over the public
API instead of a localhost hop.

The tradeoff of dropping the daemon: @minimax is now a **plain chat/reasoning
LLM**, not an opencode coding agent — it has no tools, no shell, and no shared
workspace. To preserve multi-turn memory without a server-side session, the
bridge keeps its own per-context history and rolling summary locally, exactly
like the @claude / @codex bridges (`_bridge_common`).

Per-turn flow:
  1. Resolve (or mint) the local context for `context_id`; store the new user
     turn (room header stripped) so future turns can replay it.
  2. Build one prompt: [running summary] + [bridge-local recent context] +
     [the current room turn]. The system preamble goes in the Anthropic
     `system` field.
  3. POST it to the cloud `/messages` endpoint with the JWT bearer.
  4. Store the assistant reply, fold overflow into the running summary, and
     return { response, context_id, model, thinking, usage } to Agent Chat.

Auth to Agent Chat is x-api-key (AGENT_TOKEN_MINIMAX), exactly as the other
bridges. Auth to MiniMax cloud is the JWT bearer, read fresh from the auth file
each turn so the desktop app's token refresh is picked up automatically.

**opencode CLI mode (2026-08-07).** The MiniMax Hub desktop app (v2.x,
internally "Hilo") bundles a complete opencode CLI. Spawning our OWN instance of
that binary — with our own generated config, isolated state dirs, and the same
cloud JWT — gives @minimax REAL tools (file read/write, bash, glob/grep,
webfetch) in the per-chat workspace, restoring the agentic capability lost when
the Code app removed its daemon. The bridge runs `opencode run` per turn as a
subprocess (mirroring the claude bridge's `claude -p` pattern): no extra
listening port, so the local attack surface is strictly smaller than the old
unauthenticated :15321 daemon ever was. Per-chat memory rides opencode's own
persistent sessions (`-s <id>`, stored per context). When the binary is missing
— e.g. a Hub auto-update moved it, the same failure class that killed the
daemon — the bridge falls back to the cloud-direct chat path above and /health
says so honestly instead of green-lying. `MINIMAX_BRIDGE_MODE` pins the
behavior: `auto` (default, opencode when available), `opencode` (never fall
back), `cloud` (legacy chat-only).
"""
import asyncio
import json
import os
import re
import signal
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import _bridge_common as common  # shared bridge core (single source of truth)


APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent.parent
# Per-bridge state lives here (context store), same shape as the other bridges'
# state dirs (claude_bridge/, codex_bridge/) so the set of folders stays uniform.
STATE_DIR = APP_DIR / "minimax_bridge"
STATE_DIR.mkdir(exist_ok=True)
CONTEXT_STORE = STATE_DIR / "contexts.json"


SHARED_ENV_FILE = APP_DIR / "agents" / "shared.env"
_ENV_FILES = (
    APP_DIR / ".env",
    REPO_DIR / ".env",
    SHARED_ENV_FILE,
    APP_DIR / "agents" / "minimax.env",
)
common.load_env_files(_ENV_FILES)


# --- MiniMax cloud endpoint --------------------------------------------------
# Anthropic-Messages-shaped gateway. The path already ends in `/v1`; we POST to
# `{base}/messages`. Confirmed working with the desktop app's JWT bearer.
MINIMAX_API_BASE = os.environ.get(
    "MINIMAX_API_BASE", "https://agent.minimax.io/mavis/api/v1/llm/v1"
).rstrip("/")
# The JWT the MiniMax Code desktop app maintains. We read it fresh each turn so
# the app's periodic token refresh is picked up without a bridge restart. An
# explicit MINIMAX_API_KEY / MINIMAX_JWT env value overrides the file, for a
# headless box where the desktop app never runs.
MINIMAX_AUTH_FILE = Path(
    os.environ.get(
        "MINIMAX_AUTH_FILE", str(Path.home() / ".minimax" / "local-runtime.auth.json")
    )
).expanduser()
ANTHROPIC_VERSION = os.environ.get("MINIMAX_ANTHROPIC_VERSION", "2023-06-01")

# Canonical model list (provider/model form — what the app's model picker sends
# and stores). The cloud endpoint wants the bare model id, so we strip the
# `minimax/` prefix at call time. The M2.7 family forces thinking on at the
# provider, so the thinking toggle only meaningfully applies to M3.
MINIMAX_MODEL_OPTIONS = (
    "minimax/MiniMax-M3",
    "minimax/MiniMax-M2.7",
    "minimax/MiniMax-M2.7-highspeed",
)
MINIMAX_DEFAULT_MODEL = os.environ.get("MINIMAX_BRIDGE_MODEL", "minimax/MiniMax-M3")
if MINIMAX_DEFAULT_MODEL not in MINIMAX_MODEL_OPTIONS:
    MINIMAX_DEFAULT_MODEL = "minimax/MiniMax-M3"
MINIMAX_DEFAULT_THINKING = os.environ.get("MINIMAX_BRIDGE_THINKING", "true").lower() in (
    "1", "true", "yes", "on",
)
# Upper bound on a single reply. A cap, not a target — the model stops at
# end_turn well below this for normal chat. Generous so long answers aren't
# truncated mid-thought.
MINIMAX_MAX_TOKENS = int(os.environ.get("MINIMAX_BRIDGE_MAX_TOKENS", "8192"))
# Read timeout for one cloud call. Thinking-on M3 replies can run a while, so
# keep this comfortably large; the connect timeout stays short.
MINIMAX_TIMEOUT_HARD_CAP_S = float(os.environ.get("MINIMAX_BRIDGE_TIMEOUT_HARD_CAP_S", "600"))
MINIMAX_TIMEOUT_S = min(
    float(os.environ.get("MINIMAX_BRIDGE_TIMEOUT_S", "600")), MINIMAX_TIMEOUT_HARD_CAP_S,
)
MINIMAX_CONNECT_TIMEOUT_S = float(os.environ.get("MINIMAX_BRIDGE_CONNECT_TIMEOUT_S", "15"))
BRIDGE_TOKEN = os.environ.get("AGENT_TOKEN_MINIMAX", "").strip()


# --- opencode CLI mode -------------------------------------------------------
# `auto` uses the opencode binary when present and falls back to cloud-direct
# chat when it isn't; `opencode` forces the CLI (errors surface, no fallback);
# `cloud` forces the legacy chat-only path.
MINIMAX_BRIDGE_MODE = os.environ.get("MINIMAX_BRIDGE_MODE", "auto").strip().lower()
if MINIMAX_BRIDGE_MODE not in ("auto", "opencode", "cloud"):
    MINIMAX_BRIDGE_MODE = "auto"
# The opencode binary bundled inside the MiniMax Hub desktop app. Existence is
# re-checked on every turn (cheap stat) because the Hub auto-updates and can
# move or remove the binary mid-run — a vanished binary must flip us to cloud
# fallback on the NEXT turn, not 500 forever.
MINIMAX_OPENCODE_BIN = os.environ.get(
    "MINIMAX_OPENCODE_BIN",
    "/Applications/MiniMax Hub.app/Contents/Resources/opencode/opencode",
)
# Fallback workspace when the orchestrator doesn't pass a per-chat `workspace`.
# Deliberately NOT the Agent-Chat repo — agents must never do real work inside
# the chat app folder (parity with the claude/codex bridges).
MINIMAX_WORKDIR = os.environ.get(
    "MINIMAX_BRIDGE_WORKDIR", str(Path.home() / "AGENTS" / "workspaces" / "minimax")
)
# Isolated opencode home: config/cache/data all live under the bridge's own
# state dir so our instance never touches ~/.minimax (the desktop apps' state)
# or the Hub's own opencode server state.
OC_HOME = STATE_DIR / "opencode"
OC_CONFIG_FILE = STATE_DIR / "opencode.json"
OC_READ_ONLY_CONFIG_FILE = STATE_DIR / "opencode-read-only.json"


def current_opencode_bin() -> str:
    """Path to a usable opencode binary, or ""."""
    override = os.environ.get("MINIMAX_OPENCODE_BIN", "").strip()
    p = Path(override or MINIMAX_OPENCODE_BIN)
    return str(p) if p.is_file() and os.access(p, os.X_OK) else ""


# --- Rolling summarization (mirrors the claude/codex bridges) ----------------
# With no server-side session, the bridge keeps history locally and compacts
# overflow into ctx['summary'] once it outgrows the window, so a long chat never
# balloons the per-turn prompt or blows the model's context window.
MAX_CONTEXT_MESSAGES = int(os.environ.get("MINIMAX_BRIDGE_MAX_CONTEXT_MESSAGES", "20"))
SUMMARY_TRIGGER = int(os.environ.get("MINIMAX_BRIDGE_SUMMARY_TRIGGER", "16"))
SUMMARY_KEEP = int(os.environ.get("MINIMAX_BRIDGE_SUMMARY_KEEP_RECENT", "4"))
SUMMARY_HARD_CAP = int(os.environ.get("MINIMAX_BRIDGE_SUMMARY_HARD_CAP", "30"))
SUMMARY_MAX_CHARS = int(os.environ.get("MINIMAX_BRIDGE_SUMMARY_MAX_CHARS", "2000"))
# Cheap, fast model for the summary fold. M2.7-highspeed forces thinking on at
# the provider (harmless — we just read the text block back).
SUMMARY_MODEL = os.environ.get(
    "MINIMAX_BRIDGE_SUMMARY_MODEL", "minimax/MiniMax-M2.7-highspeed"
)
SUMMARY_TIMEOUT_S = float(os.environ.get("MINIMAX_BRIDGE_SUMMARY_TIMEOUT_S", "120"))


# System preamble. @minimax is now a cloud LLM with NO tools/shell/workspace —
# keep the persona honest so it doesn't promise file edits it can't make.
SYSTEM_PROMPT = (
    "You are @minimax, a MiniMax-M3 assistant in a shared multi-agent Agent Chat "
    "room. You answer directly and concisely, reason carefully when a problem is "
    "hard, and say plainly when you are unsure. You are a chat and reasoning agent "
    "reached over MiniMax's cloud API: you have no shell, no tools, and no shared "
    "file workspace, so never claim to have run commands, edited files, or taken "
    "any action outside this conversation. When code is useful, write it inline. "
    "The room header on each turn tells you the current roster and context. "
    "You cannot open URLs, links, or attachments — NEVER emit tool-call markup "
    "(e.g. <function_calls>, <invoke>, JSON tool calls); when a link or file "
    "appears in the room, work from what is quoted in the conversation and say "
    "plainly when you cannot inspect something. Never reply with only an "
    "announcement of what you are about to do (\"Let me read…\") — deliver the "
    "substance in the same turn."
)

# MiniMax-M3 is tool-trained, so with no tools wired it sometimes hallucinates
# Anthropic-style tool-call XML into its TEXT reply (seen live: it "fetched" an
# attachment URL twice and the raw <function_calls> block was posted to the room
# as its whole message). Strip any such markup; if nothing substantive remains
# the turn gets one corrective retry, then an honest no-tools fallback.
_TOOL_MARKUP_RE = re.compile(
    r"<(?:antml:)?(function_calls|invoke|tool_call|tool_use)\b[^>]*>"
    r".*?(?:</(?:antml:)?\1>|\Z)",
    re.S | re.I,
)


def _strip_tool_markup(text: str) -> "tuple[str, bool]":
    """Remove hallucinated tool-call blocks from a reply. Returns
    (cleaned_text, found_any)."""
    if not text or "<" not in text:
        return (text or "").strip(), False
    cleaned, n = _TOOL_MARKUP_RE.subn("", text)
    return cleaned.strip(), n > 0


NO_TOOLS_FALLBACK = (
    "I tried to open a link/attachment, but I don't have tools — I can only "
    "reason over what's pasted into the chat. Share the relevant text and I'll "
    "take it from there."
)


class CloudAuthError(Exception):
    """The MiniMax cloud rejected our JWT (401/403) — expired or missing. The
    desktop app refreshes the token in the auth file, so the remedy is to make
    sure MiniMax Code is running/signed in; we surface a clear message."""


class CloudError(Exception):
    """Any other non-2xx from the cloud endpoint, body attached for the log."""


# Last real upstream (cloud) outcome, so /health can report an HONEST status
# instead of just "the proxy process is up". This is what stops the silent-
# degradation trap: before, /health always said "ok" while the daemon underneath
# was dead, so the UI dot stayed green, Reconnect no-op'd, and every turn errored.
# Now a failed cloud call (auth/unreachable) flips /health to "degraded" with a
# reason, which the app turns into an amber dot + a real Reconnect message.
# kind: "auth" (token expired/rejected) | "net" (unreachable/timeout) | None.
_LAST_CLOUD: dict = {"ok": None, "kind": None, "error": "", "ts": 0}
# How long a past failure keeps /health "degraded" before we optimistically show
# "ok" again (so a single stale blip from hours ago doesn't linger as amber).
# An EXPIRED token degrades independently of this, via the exp check below.
UPSTREAM_FAIL_TTL_MS = int(os.environ.get("MINIMAX_BRIDGE_UPSTREAM_FAIL_TTL_MS", str(15 * 60 * 1000)))


def _record_cloud(ok: bool, kind: Optional[str] = None, error: str = "") -> None:
    _LAST_CLOUD["ok"] = bool(ok)
    _LAST_CLOUD["kind"] = None if ok else kind
    _LAST_CLOUD["error"] = "" if ok else str(error or "")[:300]
    _LAST_CLOUD["ts"] = int(time.time() * 1000)


def _jwt_is_expired(jwt: str) -> bool:
    """Best-effort: decode the JWT payload (no signature check) and compare its
    `exp` to now. Returns True only when we can read a past exp — an unreadable
    token is left to the live call to reject, so we never FALSE-degrade a token
    we simply couldn't parse."""
    try:
        import base64
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return bool(exp) and int(exp) <= int(time.time())
    except Exception:
        return False


def _health_status() -> tuple[str, Optional[str]]:
    """Compute an honest (status, reason) for /health from what we can know
    cheaply — inbound token, cloud-token presence + expiry, and the last real
    cloud outcome — WITHOUT burning a token on a live ping every poll."""
    if not BRIDGE_TOKEN:
        return "missing-token", "No inbound bridge token (AGENT_TOKEN_MINIMAX)."
    if MINIMAX_BRIDGE_MODE == "opencode" and not current_opencode_bin():
        return "degraded", (
            f"opencode mode is forced but no binary at {MINIMAX_OPENCODE_BIN} — "
            "install/update the MiniMax Hub app or set MINIMAX_OPENCODE_BIN."
        )
    jwt = _load_jwt()
    if not jwt:
        return "degraded", (
            "No MiniMax cloud token — sign in to the MiniMax Code app "
            "or set MINIMAX_API_KEY."
        )
    if _jwt_is_expired(jwt):
        return "degraded", "MiniMax cloud token expired — open the MiniMax Code app to refresh it."
    if _LAST_CLOUD["ok"] is False and (
        int(time.time() * 1000) - int(_LAST_CLOUD["ts"] or 0) < UPSTREAM_FAIL_TTL_MS
    ):
        return "degraded", (
            _LAST_CLOUD["error"] or "The last MiniMax cloud call failed."
        )
    return "ok", None


def _authorized(request: Request) -> bool:
    return common.authorized(request, BRIDGE_TOKEN)


app = FastAPI(title="minimax Agent Chat Bridge (cloud-direct)")

# Live "current step" text per context_id, surfaced to Agent Chat via
# api_log_get so @minimax shows a "Working on it…" line during the cloud call.
PROGRESS: dict[str, dict] = {}


def _set_progress(context_id: str, text: str, active: bool = True, **kwargs) -> None:
    common.set_progress(PROGRESS, context_id, text, active, **kwargs)


def _end_progress(context_id: str) -> None:
    common.end_progress(PROGRESS, context_id)


def _load_contexts() -> dict:
    return common.load_contexts(CONTEXT_STORE)


def _save_contexts(contexts: dict) -> None:
    common.save_contexts(CONTEXT_STORE, contexts)


def _context(context_id: Optional[str]) -> tuple[str, dict]:
    return common.get_or_create_context(CONTEXT_STORE, context_id)


_strip_system_header = common.strip_system_header


def _load_jwt() -> str:
    """Return the MiniMax cloud bearer token. Explicit env override wins;
    otherwise read it fresh from the desktop app's auth file so a token refresh
    is picked up without restarting the bridge."""
    env_tok = (os.environ.get("MINIMAX_JWT") or os.environ.get("MINIMAX_API_KEY") or "").strip()
    if env_tok:
        return env_tok
    try:
        data = json.loads(MINIMAX_AUTH_FILE.read_text())
        tok = str(((data or {}).get("auth") or {}).get("accessToken") or "").strip()
        return tok
    except Exception:
        return ""


def _minimax_options(body: dict) -> tuple[str, bool]:
    """Validate the model + thinking fields the orchestrator sent. Anything
    out-of-range falls back to defaults — never trust the caller's allowlist,
    never let a malformed payload kill the room."""
    model = str(body.get("model") or MINIMAX_DEFAULT_MODEL).strip()
    if model not in MINIMAX_MODEL_OPTIONS:
        model = MINIMAX_DEFAULT_MODEL
    # Accept `thinking` (our field) OR `reasoning` (codex/claude's field) so the
    # UI can send whichever reads best.
    raw_thinking = body.get("thinking")
    if raw_thinking is None:
        raw_thinking = body.get("reasoning")
    if isinstance(raw_thinking, bool):
        thinking = raw_thinking
    else:
        thinking = str(raw_thinking or str(MINIMAX_DEFAULT_THINKING)).strip().lower() in (
            "1", "true", "yes", "on", "thinking", "enabled",
        )
    return model, thinking


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


def _model_id(model: str) -> str:
    """`minimax/MiniMax-M3` → `MiniMax-M3`. The cloud endpoint wants the bare id."""
    return model.split("/", 1)[1].strip() if "/" in model else model.strip()


def _extract_text(content) -> str:
    """Join the `text` blocks of an Anthropic-shaped content array, skipping
    `thinking` blocks (present when thinking is on — chain-of-thought we don't
    surface as the reply)."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = str(block.get("text") or "")
            if t:
                parts.append(t)
    return "".join(parts).strip()


def _content_has_tool_use(content) -> bool:
    """True when the cloud reply carried `tool_use` blocks — the model tried to
    call a tool it doesn't have; _extract_text silently drops those, which is how
    a turn ends up as just 'Let me read what X wrote…' with no substance."""
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )


def _map_usage(usage) -> Optional[dict]:
    """Normalize the cloud's token usage into the shape the app's usage
    dashboard meters. The endpoint gives a real input/output split, so we pass
    it through plus a derived total — no estimation needed."""
    if not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    result: dict = {}
    if inp is not None:
        result["input_tokens"] = int(inp)
    if out is not None:
        result["output_tokens"] = int(out)
    if inp is not None and out is not None:
        result["total_tokens"] = int(inp) + int(out)
    for k in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if usage.get(k) is not None:
            result[k] = int(usage[k])
    return result or None


# --- opencode CLI machinery --------------------------------------------------
class OpencodeError(Exception):
    """The opencode subprocess failed (spawn, non-zero exit, no output, or
    timeout). In `auto` mode this triggers the cloud-direct fallback."""


# Chat attachments arrive as host URLs the CLI can't always fetch; mirror each
# referenced file to a locally readable copy and annotate the URL with that
# path so the agent can Read it (same pattern as the claude bridge).
ATTACHMENTS_DIR = Path(os.environ.get("DATA_DIR", str(APP_DIR))) / "attachments"
LOCAL_FILES_DIR = STATE_DIR / "files"
_ATTACHMENT_URL_RE = re.compile(r"https?://[^/\s]+/attachments/(\d+)/([a-f0-9]{32})")


def _localize_attachments(message: str) -> str:
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
                LOCAL_FILES_DIR.mkdir(parents=True, exist_ok=True)
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


def _ensure_oc_config(workspace_access: str = "writable") -> str:
    """(Re)write the generated opencode config. The JWT is NEVER written to
    disk: the config references `{env:MINIMAX_JWT}`, which opencode resolves
    from the subprocess environment at spawn time. Both `apiKey` and an explicit
    Authorization header are set because @ai-sdk/anthropic sends `x-api-key` by
    default while the mavis gateway wants a Bearer (verified: apiKey alone gets
    'Unauthorized: token is required')."""
    models = {}
    for opt in MINIMAX_MODEL_OPTIONS:
        mid = _model_id(opt)
        models[mid] = {
            "name": mid,
            "limit": {
                "context": 200000 if "M2.7" in mid else 450000,
                "output": 128000,
            },
        }
    cfg = {
        "$schema": "https://opencode.ai/schema.json",
        "provider": {
            "minimax": {
                "name": "MiniMax",
                "npm": "@ai-sdk/anthropic",
                "options": {
                    "baseURL": MINIMAX_API_BASE,
                    "apiKey": "{env:MINIMAX_JWT}",
                    "headers": {"Authorization": "Bearer {env:MINIMAX_JWT}"},
                },
                "models": models,
            }
        },
        "enabled_providers": ["minimax"],
        "model": MINIMAX_DEFAULT_MODEL,
        # Workspace-scoped autonomy, parity with the claude/codex bridges'
        # accept-edits posture. This is an unattended endpoint: localhost + the
        # x-api-key gate are the perimeter, same accepted trade as @claude.
        "permission": (
            {
                "edit": "deny",
                "bash": "deny",
                "external_directory": "deny",
                "webfetch": "allow",
            }
            if workspace_access == "read-only" else
            {"edit": "allow", "bash": "allow", "webfetch": "allow"}
        ),
    }
    body = json.dumps(cfg, indent=1)
    config_file = (
        OC_READ_ONLY_CONFIG_FILE
        if workspace_access == "read-only" else OC_CONFIG_FILE
    )
    try:
        if config_file.is_file() and config_file.read_text() == body:
            return str(config_file)
    except Exception:
        pass
    STATE_DIR.mkdir(exist_ok=True)
    tmp = config_file.with_suffix(".tmp")
    tmp.write_text(body)
    tmp.replace(config_file)
    return str(config_file)


def _oc_env(jwt: str, workspace_access: str = "writable") -> dict:
    """Environment for the opencode subprocess: the parent env (PATH etc. — the
    agent's bash tool should see the user's normal toolchain) minus any
    OPENCODE_*/Anthropic vars that could shadow our config, plus our isolated
    state dirs and the fresh JWT."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("OPENCODE_") or k in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
        ):
            env.pop(k, None)
    for d in ("data", "cache", "config", "cfg"):
        (OC_HOME / d).mkdir(parents=True, exist_ok=True)
    env.update({
        "MINIMAX_JWT": jwt,
        "OPENCODE_CONFIG": _ensure_oc_config(workspace_access),
        "OPENCODE_CONFIG_DIR": str(OC_HOME / "cfg"),
        "XDG_DATA_HOME": str(OC_HOME / "data"),
        "XDG_CACHE_HOME": str(OC_HOME / "cache"),
        "XDG_CONFIG_HOME": str(OC_HOME / "config"),
        # Keep policy centralized in the generated config: no global/project
        # config pickup, no CLAUDE.md ingestion from the workspace.
        "OPENCODE_DISABLE_GLOBAL_CONFIG": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    })
    return env


def _resolve_mode() -> str:
    """Which transport this turn should use: 'opencode' or 'cloud'."""
    if MINIMAX_BRIDGE_MODE == "cloud":
        return "cloud"
    if MINIMAX_BRIDGE_MODE == "opencode":
        return "opencode"
    return "opencode" if current_opencode_bin() else "cloud"


def _oc_prompt(message: str, ctx: dict, workspace: str, model: str,
               fresh_session: bool, workspace_access: str = "writable") -> str:
    """Prompt for an opencode turn. Memory normally rides the persistent
    opencode session, so we do NOT replay bridge-local history every turn (that
    would double-feed it). The exception is a FRESH session for a context that
    already has history (first opencode turn after cloud mode, or a reset state
    dir): seed it once with the summary + recent context."""
    preamble = (
        "[@minimax operating notes]\n"
        f"You are @minimax in a shared multi-agent Agent Chat room — a {model} "
        "agent on the opencode runtime. Your working directory for this chat is "
        f"{workspace}. "
        + (
            "This turn is enforced read-only: inspect with read/glob/grep and "
            "report findings, but do not edit files or run shell commands. "
            if workspace_access == "read-only" else
            "You have real file read/write, bash, glob/grep, and webfetch tools; "
            "do file and command work there and stay inside it. "
        )
        +
        "Prefer reversible actions; never run destructive commands unless the "
        "room explicitly asks. The room header on each turn tells you the "
        "current roster and context. Deliver substance in the same turn — "
        "never reply with only an announcement of what you are about to do. "
        "Only your text output is posted to the room."
    )
    blocks = [preamble]
    if fresh_session:
        summary = str(ctx.get("summary") or "").strip()
        if summary:
            blocks.append("[Summary of earlier conversation]\n" + summary)
        history_lines = common.format_history_lines(
            ctx.get("messages", [])[-MAX_CONTEXT_MESSAGES:]
        )
        if history_lines:
            blocks.append("[Bridge-local recent context]\n" + "\n\n".join(history_lines))
    blocks.append("[New Agent Chat prompt]\n" + message)
    return "\n\n".join(blocks)


async def _run_opencode(
    prompt: str, model: str, context_id: str, workspace: str,
    oc_session: Optional[str], *, workspace_access: str = "writable",
) -> "tuple[str, Optional[dict], Optional[float], Optional[str]]":
    """One `opencode run` turn. Returns (reply, usage, cost_usd, session_id).
    Raises CloudAuthError when the cloud token is missing/expired/rejected
    (same remedy as cloud mode), OpencodeError for CLI-level failures."""
    binary = current_opencode_bin()
    if not binary:
        raise OpencodeError(
            f"opencode binary not found at {MINIMAX_OPENCODE_BIN} — is the "
            "MiniMax Hub app installed? (Set MINIMAX_OPENCODE_BIN to override.)"
        )
    jwt = _load_jwt()
    if not jwt:
        msg = (
            "No MiniMax cloud token found. Sign in to the MiniMax Code app "
            f"(expected a JWT in {MINIMAX_AUTH_FILE}) or set MINIMAX_API_KEY."
        )
        _record_cloud(False, "auth", msg)
        raise CloudAuthError(msg)
    if _jwt_is_expired(jwt):
        msg = "MiniMax cloud token expired — open the MiniMax Code app to refresh it."
        _record_cloud(False, "auth", msg)
        raise CloudAuthError(msg)

    argv = [binary, "run", "-m", model, "--format", "json"]
    if oc_session:
        argv += ["-s", oc_session]
    # `--` so a prompt that begins with '-' can never parse as a flag.
    argv += ["--", prompt]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,  # an open stdin pipe can make `run` wait on it
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=_oc_env(jwt, workspace_access),
            start_new_session=True,
        )
    except OSError as exc:
        raise OpencodeError(f"failed to spawn opencode: {exc!r}") from exc

    text_parts: dict[str, str] = {}
    part_order: list[str] = []
    session_id: Optional[str] = oc_session or None
    tokens_in = tokens_out = 0
    cache_read = cache_write = 0
    cost_usd = 0.0
    saw_usage = False
    err_detail: Optional[str] = None
    model_calls = 0
    def _joined_text() -> str:
        return "\n\n".join(
            text_parts[p].strip() for p in part_order if text_parts[p].strip()
        ).strip()

    def _handle_line(raw: bytes) -> None:
        nonlocal session_id, tokens_in, tokens_out, cache_read, cache_write
        nonlocal cost_usd, saw_usage, err_detail, model_calls
        raw = raw.strip()
        if not raw:
            return
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(evt, dict):
            return
        part = evt.get("part") if isinstance(evt.get("part"), dict) else {}
        sid = evt.get("sessionID") or part.get("sessionID")
        if sid:
            session_id = str(sid)
        etype = str(evt.get("type") or "")
        if etype == "text" and part.get("type") == "text":
            pid = str(part.get("id") or len(part_order))
            if pid not in text_parts:
                part_order.append(pid)
            text_parts[pid] = str(part.get("text") or "")
            joined = _joined_text()
            if joined:
                common.set_partial_response(PROGRESS, context_id, joined)
                _set_progress(context_id, common.clip(text_parts[pid]), True)
        elif etype == "step_start":
            _set_progress(context_id, "Working…", True)
        elif "tool" in etype:
            tool = str(part.get("tool") or part.get("name") or "a tool")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            title = str(state.get("title") or "").strip()
            _set_progress(
                context_id,
                common.clip(f"Using {tool}: {title}" if title else f"Using {tool}…"),
                True,
            )
        elif etype == "step_finish":
            model_calls += 1
            toks = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            try:
                tokens_in += int(toks.get("input") or 0)
                tokens_out += int(toks.get("output") or 0) + int(toks.get("reasoning") or 0)
                cache = toks.get("cache") if isinstance(toks.get("cache"), dict) else {}
                cache_read += int(cache.get("read") or 0)
                cache_write += int(cache.get("write") or 0)
                saw_usage = True
            except Exception:
                pass
            try:
                cost_usd += float(part.get("cost") or 0.0)
            except Exception:
                pass
        elif etype == "error" or evt.get("error"):
            detail = evt.get("error")
            err_detail = common.clip(
                detail if isinstance(detail, str) else json.dumps(detail), 300
            )

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

    async def _read_events() -> None:
        # Chunked read + manual newline split: a single oversized event line
        # (a big tool result) would overflow readline()'s 64 KiB limit — the
        # "Separator is not found" crash the claude/codex bridges already fixed.
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
                buf = buf[nl + 1:]
        if buf:
            _handle_line(buf)

    stderr_task = asyncio.create_task(_drain_stderr())
    try:
        await asyncio.wait_for(_read_events(), timeout=MINIMAX_TIMEOUT_S)
        rc = await proc.wait()
    except asyncio.TimeoutError:
        await _terminate_process_tree(proc)
        stderr_task.cancel()
        raise OpencodeError(
            f"opencode timed out after {int(MINIMAX_TIMEOUT_S)}s"
        ) from None
    except asyncio.CancelledError:
        await _terminate_process_tree(proc)
        stderr_task.cancel()
        raise
    stderr_data = b""
    try:
        stderr_data = await stderr_task
    except Exception:
        pass
    stderr_text = stderr_data.decode("utf-8", "replace").strip()

    reply = _joined_text()
    if rc != 0 or (not reply):
        blob = " ".join(x for x in (err_detail or "", stderr_text) if x)
        if re.search(r"unauthorized|401|token is required|token expired", blob, re.I):
            msg = (
                f"MiniMax cloud auth failed via opencode: {common.clip(blob, 300)} "
                "The JWT is likely expired — open the MiniMax Code app to refresh it."
            )
            _record_cloud(False, "auth", msg)
            raise CloudAuthError(msg)
        raise OpencodeError(
            f"opencode exited rc={rc} with "
            f"{'no reply text' if not reply else 'an error'}: "
            f"{common.clip(blob, 400) or '(no stderr)'}"
        )
    usage: Optional[dict] = None
    if saw_usage:
        usage = {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
            "model_calls": max(1, model_calls),
        }
        if cache_read:
            usage["cache_read_input_tokens"] = cache_read
        if cache_write:
            usage["cache_creation_input_tokens"] = cache_write
    _record_cloud(True)
    return reply, usage, (cost_usd if cost_usd > 0 else None), session_id


async def _call_cloud(
    client: httpx.AsyncClient,
    model: str,
    thinking: bool,
    system: Optional[str],
    user_content: str,
    *,
    max_tokens: int = MINIMAX_MAX_TOKENS,
    timeout_s: float = MINIMAX_TIMEOUT_S,
    record: bool = True,
) -> "tuple[str, Optional[dict], bool]":
    """One non-streaming call to the MiniMax cloud /messages endpoint. Returns
    (reply_text, usage, had_tool_use). Raises CloudAuthError on 401/403,
    CloudError otherwise.

    `record` drives the /health upstream signal. Only the user-facing turn should
    record: the background summarizer runs AFTER the reply, so a summary hiccup
    must NOT flip @minimax to 'degraded' when the turn itself succeeded."""
    def _rec(ok: bool, kind: Optional[str] = None, error: str = "") -> None:
        if record:
            _record_cloud(ok, kind, error)
    jwt = _load_jwt()
    if not jwt:
        msg = (
            "No MiniMax cloud token found. Sign in to the MiniMax Code app "
            f"(expected a JWT in {MINIMAX_AUTH_FILE}) or set MINIMAX_API_KEY."
        )
        _rec(False, "auth", msg)
        raise CloudAuthError(msg)
    payload: dict = {
        "model": _model_id(model),
        "max_tokens": max_tokens,
        # M3 is switchable; M2.7 forces thinking on and ignores 'disabled'.
        "thinking": {"type": "enabled" if thinking else "disabled"},
        "messages": [{"role": "user", "content": user_content}],
    }
    if system:
        payload["system"] = system
    try:
        r = await client.post(
            f"{MINIMAX_API_BASE}/messages",
            json=payload,
            headers={
                "Authorization": f"Bearer {jwt}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            timeout=httpx.Timeout(timeout_s, connect=MINIMAX_CONNECT_TIMEOUT_S),
        )
    except httpx.HTTPError as exc:
        # Connect/timeout/transport error — the cloud (or the network) is
        # unreachable. Record it so /health can go amber instead of lying green.
        _rec(False, "net", f"MiniMax cloud unreachable: {exc!r}")
        raise CloudError(f"MiniMax cloud unreachable: {exc!r}") from exc
    if r.status_code in (401, 403):
        msg = (
            f"MiniMax cloud auth failed (HTTP {r.status_code}): {r.text.strip()[:300]}. "
            "The JWT is likely expired — open the MiniMax Code app to refresh it."
        )
        _rec(False, "auth", msg)
        raise CloudAuthError(msg)
    if r.status_code >= 400:
        msg = f"MiniMax cloud HTTP {r.status_code}: {r.text.strip()[:500]}"
        _rec(False, "net", msg)
        raise CloudError(msg)
    try:
        data = r.json()
    except Exception as exc:
        msg = f"MiniMax cloud returned non-JSON: {r.text.strip()[:300]}"
        _rec(False, "net", msg)
        raise CloudError(msg) from exc
    text = _extract_text(data.get("content"))
    usage = _map_usage(data.get("usage"))
    _rec(True)
    return text, usage, _content_has_tool_use(data.get("content"))


def _prompt(message: str, ctx: dict) -> str:
    """Assemble the single user prompt: running summary + bridge-local recent
    context + the current room turn. Mirrors the claude/codex bridges so memory
    semantics are identical across the local-history agents."""
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
    """Fold older messages into a compact running summary via the cheap model."""
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
    async with httpx.AsyncClient() as client:
        text, _usage, _tools = await _call_cloud(
            client, SUMMARY_MODEL, False, None, instruction,
            max_tokens=1024, timeout_s=SUMMARY_TIMEOUT_S,
            record=False,  # background compaction must not flip @minimax health
        )
    return text.strip() or old_summary


async def _maybe_summarize(ctx: dict) -> bool:
    """Compact overflow into ctx['summary'] when the context outgrows the window.
    Fail-open (never raises): on any error, hard-cap the message list instead.
    Returns True if this turn actually compacted, for the `session_rotated` badge."""
    before = len(ctx.get("messages") or [])
    await common.maybe_summarize(
        ctx, _summarize,
        trigger=SUMMARY_TRIGGER, keep=SUMMARY_KEEP,
        hard_cap=SUMMARY_HARD_CAP, max_chars=SUMMARY_MAX_CHARS,
    )
    return len(ctx.get("messages") or []) < before


async def _cloud_turn(
    message: str, ctx: dict, model: str, thinking: bool, context_id: str,
) -> "tuple[str, Optional[dict]]":
    """One chat-only turn over the cloud /messages endpoint — the legacy
    (pre-opencode) path, still used as the `auto` fallback and under
    MINIMAX_BRIDGE_MODE=cloud. Includes the no-tools guard: this path has no
    tools, and MiniMax-M3 is tool-trained, so it sometimes hallucinates
    tool-call markup that must never reach the room."""
    async with httpx.AsyncClient() as client:
        reply, usage, had_tool_use = await _call_cloud(
            client, model, thinking, SYSTEM_PROMPT, _prompt(message, ctx)
        )
    reply, leaked = _strip_tool_markup(reply)
    if (had_tool_use or leaked) and len(reply) < 80:
        _set_progress(context_id, "Answering without tools…", True)
        corrective = (
            "\n\n[bridge notice] Your previous attempt emitted a tool call, "
            "but you have NO tools and cannot open links or attachments — it "
            "was discarded. Answer the room turn NOW in plain prose using "
            "only what is already in the conversation. If a link or file "
            "matters and its contents are not quoted in the chat, say so "
            "briefly and answer from what is known."
        )
        try:
            async with httpx.AsyncClient() as client:
                retry_reply, retry_usage, _ = await _call_cloud(
                    client, model, thinking, SYSTEM_PROMPT,
                    _prompt(message, ctx) + corrective,
                )
            retry_reply, _ = _strip_tool_markup(retry_reply)
            if retry_reply:
                reply = retry_reply
                usage = retry_usage or usage
        except Exception as retry_exc:
            print(f"[minimax] no-tools corrective retry failed "
                  f"context_id={context_id}: {retry_exc!r}",
                  file=sys.stderr, flush=True)
        if not reply:
            reply = NO_TOOLS_FALLBACK
    _record_cloud(True)
    return reply, usage


@app.get("/health")
async def health():
    """Cheap readiness probe. The UI's Reconnect button + the orchestrator's
    bridge registry both hit this. We do NOT round-trip the cloud here — that
    would couple our liveness to network latency and burn a token on every poll.
    `jwt_present` reflects whether a cloud token is currently readable."""
    status, reason = _health_status()
    resolved = _resolve_mode()
    data = {
        "status": status,
        "provider_base": MINIMAX_API_BASE,
        "transport": "opencode-cli" if resolved == "opencode" else "cloud-direct",
        "mode": MINIMAX_BRIDGE_MODE,
        "opencode_bin_present": bool(current_opencode_bin()),
        "opencode_bin": current_opencode_bin() or MINIMAX_OPENCODE_BIN,
        "model": MINIMAX_DEFAULT_MODEL,
        "thinking": MINIMAX_DEFAULT_THINKING,
        "timeout_s": MINIMAX_TIMEOUT_S,
        "max_tokens": MINIMAX_MAX_TOKENS,
        "turn_timeout_seconds": MINIMAX_TIMEOUT_S,
        "token_present": bool(BRIDGE_TOKEN),
        "jwt_present": bool(_load_jwt()),
        "model_options": list(MINIMAX_MODEL_OPTIONS),
        "thinking_options": ["off", "on"],
        "summary_trigger": SUMMARY_TRIGGER,
        "summary_hard_cap": SUMMARY_HARD_CAP,
        "summary_model": SUMMARY_MODEL,
        "tracked_contexts": len(_load_contexts()),
    }
    if reason:
        data["reason"] = reason
    if _LAST_CLOUD["ok"] is not None:
        data["last_cloud_ok"] = bool(_LAST_CLOUD["ok"])
        data["last_cloud_kind"] = _LAST_CLOUD["kind"]
        data["last_cloud_ts"] = _LAST_CLOUD["ts"]
    return data


# Self-reported capability card. The orchestrator's concierge reads this (cached)
# to route work to the right agent instead of guessing from a static table —
# keep `best_for`/`strengths` honest as the agent's role evolves.
@app.get("/capabilities")
async def capabilities():
    agentic = _resolve_mode() == "opencode"
    if agentic:
        best_for = (
            "Agentic work via MiniMax-M3 on the local opencode runtime — real "
            "file read/write, shell, and web-fetch tools in the per-chat "
            "workspace, plus fast reasoning, drafting, and long-context digestion."
        )
        strengths = [
            "file and workspace tasks (read/write/edit)",
            "running shell commands",
            "fetching and digesting web pages",
            "step-by-step reasoning",
            "large context window",
        ]
        avoid = (
            "Deep multi-hour repo engineering — @claude / @codex remain the "
            "heavyweight coding agents; @minimax handles general agentic tasks."
        )
        blurb = (
            "MiniMax-M3 agent on the local opencode CLI: real tools + workspace; "
            "direct, quick, candid about uncertainty."
        )
    else:
        best_for = (
            "Quick chat, drafting, and step-by-step reasoning via MiniMax-M3 over "
            "the cloud API — fast turnaround on questions, explanations, and inline code."
        )
        strengths = [
            "fast conversational answers",
            "step-by-step reasoning",
            "drafting and rewriting",
            "inline code snippets",
            "large context window",
        ]
        avoid = (
            "File edits, running commands, or workspace tasks — this agent has no "
            "tools or shell; route those to a coding agent (@claude / @codex)."
        )
        blurb = "MiniMax-M3 cloud assistant; direct, quick, candid about uncertainty. No tools/workspace."
    return {
        "id": "minimax",
        "model": MINIMAX_DEFAULT_MODEL,
        "best_for": best_for,
        "strengths": strengths,
        "avoid": avoid,
        "voice": (
            "Warm, quick, and candid; answers directly and says plainly when it's unsure."
        ),
        "supports": {
            "live_progress": True,
            "cancel": False,
            "tool_transparency": agentic,
            "model_picker": True,
            "thinking_toggle": True,
            "cost_readout": agentic,
            "context_rotation": True,
            "remember_this": False,
            "self_check": False,
            "read_only_workspace": agentic,
        },
        "blurb": blurb,
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
        }
    }


@app.post("/api/api_message")
async def api_message(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Re-read env files so a Secrets-panel grant is visible to this turn
    # without a bridge restart.
    common.load_env_files(_ENV_FILES, overwrite=True)
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    model, thinking = _minimax_options(body)
    workspace_access = str(body.get("workspace_access") or "writable").strip().lower()
    if workspace_access not in {"writable", "read-only"}:
        return JSONResponse(
            {"error": "workspace_access must be writable or read-only"},
            status_code=400,
        )
    context_id, ctx = _context(body.get("context_id"))
    reused = bool(body.get("context_id")) and (ctx.get("messages") or ctx.get("summary"))
    # Mark the turn active immediately so a poll landing before the cloud call
    # returns sees "Working on it…" rather than the previous turn's final line.
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

    transport = _resolve_mode()
    if workspace_access == "read-only" and transport != "opencode":
        return JSONResponse(
            {"error": "MiniMax read-only workspace mode requires opencode"},
            status_code=409,
        )
    usage = None
    cost_usd: Optional[float] = None
    oc_session_new: Optional[str] = None
    try:
        _set_progress(context_id, "Working on it…", True)
        if transport == "opencode":
            workspace = common.resolve_workspace(body, MINIMAX_WORKDIR)
            local_message = _localize_attachments(message)
            oc_session = str(ctx.get("oc_session") or "").strip() or None
            try:
                try:
                    reply, usage, cost_usd, oc_session_new = await _run_opencode(
                        _oc_prompt(local_message, ctx, workspace, model,
                                   fresh_session=not oc_session,
                                   workspace_access=workspace_access),
                        model, context_id, workspace, oc_session,
                        workspace_access=workspace_access,
                    )
                except OpencodeError as first_err:
                    if not oc_session:
                        raise
                    # The stored session id may be stale (opencode state dir
                    # reset/moved). One retry on a fresh session, seeded with
                    # the bridge-local history so memory survives the reset.
                    print(f"[minimax] opencode session retry "
                          f"context_id={context_id}: {first_err!r}",
                          file=sys.stderr, flush=True)
                    _set_progress(context_id, "Restarting session…", True)
                    reply, usage, cost_usd, oc_session_new = await _run_opencode(
                        _oc_prompt(local_message, ctx, workspace, model,
                                   fresh_session=True,
                                   workspace_access=workspace_access),
                        model, context_id, workspace, None,
                        workspace_access=workspace_access,
                    )
                # Hygiene only — tools are real in this mode, but stray
                # tool-markup TEXT should still never reach the room.
                reply, _ = _strip_tool_markup(reply)
            except CloudAuthError:
                raise
            except Exception as oc_exc:
                if MINIMAX_BRIDGE_MODE == "opencode" or workspace_access == "read-only":
                    raise
                # auto mode: honest per-turn fallback to the chat-only cloud
                # path — a Hub update yanking the binary degrades @minimax to
                # its pre-opencode behavior instead of breaking the room.
                print(f"[minimax] opencode failed; falling back to cloud-direct "
                      f"context_id={context_id}: {oc_exc!r}",
                      file=sys.stderr, flush=True)
                transport = "cloud"
        if transport == "cloud":
            reply, usage = await _cloud_turn(message, ctx, model, thinking, context_id)
        _end_progress(context_id)
    except CloudAuthError as e:
        _record_cloud(False, "auth", str(e))
        _end_progress(context_id)
        print(f"[minimax] cloud auth error context_id={context_id}: {e!r}",
              file=sys.stderr, flush=True)
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:
        _end_progress(context_id)
        # The real cause used to vanish (str(e) went out only in the HTTP body,
        # which nothing logs). Log the full traceback to stderr (-> the bridge
        # log) with the context_id so recurring 500s are self-diagnosing.
        print(f"[minimax] api_message 500 context_id={context_id} "
              f"reused={bool(body.get('context_id'))}: {e!r}",
              file=sys.stderr, flush=True)
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

    if not reply:
        reply = "(minimax returned an empty response.)"

    # Store the assistant reply, then compact older turns into a running summary
    # instead of dropping them. Reload the store so a concurrent turn's write
    # isn't clobbered (matches the claude bridge's read-modify-write).
    contexts = _load_contexts()
    ctx = contexts.setdefault(context_id, {"messages": []})
    if oc_session_new:
        # Persist the opencode session so the next turn continues it (-s).
        ctx["oc_session"] = oc_session_new
    ctx.setdefault("messages", []).append({"role": "assistant", "text": reply})
    ctx["updated_ts"] = int(time.time() * 1000)
    rotated = await _maybe_summarize(ctx)
    _save_contexts(contexts)

    turn_count = len([m for m in ctx.get("messages", []) if m.get("role") == "user"])
    resp = {
        "response": reply,
        "context_id": context_id,
        "model": model,
        # Say so when the caller's model pick was coerced, rather than reporting
        # the substitute as if it had been the choice.
        **common.model_fallback_meta(body.get("model"), model),
        "thinking": thinking,
        # `reasoning` mirrors the codex/claude bridges — some downstream tooling
        # keys off it.
        "reasoning": "on" if thinking else "off",
        "session_reused": bool(reused),
        "session_rotated": bool(rotated),
        "turn_count": turn_count,
        # Which path actually answered this turn: the agentic opencode CLI, or
        # the chat-only cloud fallback.
        "transport": "opencode-cli" if transport == "opencode" else "cloud-direct",
    }
    if usage:
        resp["usage"] = usage
    if cost_usd is not None:
        resp["cost_usd"] = round(float(cost_usd), 6)
    return resp
