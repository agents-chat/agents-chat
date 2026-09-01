import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import sys as _sys
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
import _bridge_common as common  # shared bridge core (single source of truth)


APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent.parent
STATE_DIR = APP_DIR / "grok_bridge"
STATE_DIR.mkdir(exist_ok=True)
CONTEXT_STORE = STATE_DIR / "contexts.json"


_ENV_FILES = (
    APP_DIR / ".env",
    REPO_DIR / ".env",
    APP_DIR / "agents" / "shared.env",
    APP_DIR / "agents" / "grok.env",
)
common.load_env_files(_ENV_FILES)

# xAI's Grok CLI (`grok`), installed via https://x.ai/cli/install.sh to
# ~/.grok/bin/grok. We drive it headlessly (`grok -p`), the same one-shot,
# reconstruct-memory-each-turn model as the Codex/Claude bridges.
GROK_BIN = os.environ.get("GROK_BIN", str(Path.home() / ".grok" / "bin" / "grok"))
GROK_HOME = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))
GROK_AUTH_FILE = GROK_HOME / "auth.json"
# Fallback workspace when the orchestrator doesn't pass a per-chat `workspace`
# (a direct/legacy caller). Deliberately NOT the Agent-Chat repo — agents must
# never do real work inside the chat app folder. The orchestrator normally
# sends a per-chat workspace that overrides this.
GROK_WORKDIR = os.environ.get(
    "GROK_BRIDGE_WORKDIR", str(Path.home() / "AGENTS" / "workspaces" / "grok")
)
GROK_MODEL = os.environ.get("GROK_BRIDGE_MODEL", "grok-4.6")
GROK_REASONING = os.environ.get("GROK_BRIDGE_REASONING", "low")
# Sandbox profile for the grok process (off | workspace | read-only | strict).
# Default off = unrestricted, so localhost/network tools work (Codex's
# workspace-write seatbelt silently blocked localhost; grok's `off` avoids it).
# Tool approvals are handled by --permission-mode below, not the sandbox.
GROK_SANDBOX = os.environ.get("GROK_BRIDGE_SANDBOX", "").strip()
GROK_PERMISSION_MODE = os.environ.get("GROK_BRIDGE_PERMISSION_MODE", "bypassPermissions")
GROK_TIMEOUT_HARD_CAP_S = float(os.environ.get("GROK_BRIDGE_TIMEOUT_HARD_CAP_S", "600"))
GROK_TIMEOUT_S = min(
    float(os.environ.get("GROK_BRIDGE_TIMEOUT_S", "600")), GROK_TIMEOUT_HARD_CAP_S,
)
MAX_CONTEXT_MESSAGES = int(os.environ.get("GROK_BRIDGE_MAX_CONTEXT_MESSAGES", "12"))
# Hard ceiling on the total prompt character count handed to the Grok CLI (via
# --prompt-file, so there's no stdin chunk limit like Codex has — this just
# bounds token cost). Trim oldest history lines until we're under budget.
MAX_PROMPT_CHARS = int(os.environ.get("GROK_BRIDGE_MAX_PROMPT_CHARS", "48000"))
# Rolling summarization (mirror of the Codex/Claude bridges): once a context
# exceeds SUMMARY_TRIGGER messages, fold all but the last SUMMARY_KEEP into a
# persistent running summary via the fast model, so @grok keeps long-term chat
# memory without an ever-growing prompt. SUMMARY_HARD_CAP bounds it if
# summarization is unavailable (fail-open).
SUMMARY_TRIGGER = int(os.environ.get("GROK_BRIDGE_SUMMARY_TRIGGER", "16"))
SUMMARY_KEEP = int(os.environ.get("GROK_BRIDGE_SUMMARY_KEEP", "8"))
SUMMARY_HARD_CAP = int(os.environ.get("GROK_BRIDGE_SUMMARY_HARD_CAP", "24"))
SUMMARY_MODEL = os.environ.get("GROK_BRIDGE_SUMMARY_MODEL", "grok-composer-2.5-fast")
SUMMARY_TIMEOUT_S = float(os.environ.get("GROK_BRIDGE_SUMMARY_TIMEOUT_S", "120"))
# Hard ceiling on the stored running summary, so even a model that ignores the
# ~200-word instruction can't slowly bloat the prompt over many compactions.
SUMMARY_MAX_CHARS = int(os.environ.get("GROK_BRIDGE_SUMMARY_MAX_CHARS", "2000"))
BRIDGE_TOKEN = os.environ.get("AGENT_TOKEN_GROK", "").strip()
MODEL_OPTIONS = {"grok-4.6", "grok-4.5", "grok-composer-2.5-fast"}
# Installed grok CLI (2026-08) only accepts these three; xhigh is a Codex/Claude
# knob and the CLI rejects it with a hard error before the turn starts.
REASONING_OPTIONS = {"low", "medium", "high"}

# Native Grok runs on the same Mac as Agent Chat, so local files are the most
# reliable way to hand it attachments. Docker/Tailscale URLs can be unreachable
# from CLI file tools, but the bytes are already on this disk.
ATTACHMENTS_DIR = Path(os.environ.get("DATA_DIR", APP_DIR)) / "attachments"
LOCAL_FILES_DIR = STATE_DIR / "files"
LOCAL_FILES_DIR.mkdir(exist_ok=True)
_ATTACHMENT_URL_RE = re.compile(r"https?://[^/\s]+/attachments/(\d+)/([a-f0-9]{32})")

app = FastAPI(title="Grok Agent Chat Bridge")


# Live "current step" text per context_id, surfaced to Agent Chat via
# api_log_get so @grok shows a thinking line while it works — the same
# mechanism the Codex/Claude bridges use. Updated as the streamed
# `grok -p --output-format streaming-json` events arrive in _run_grok.
# In-memory only (single uvicorn worker), bounded below to avoid growth.
PROGRESS: dict[str, dict] = {}


def _set_progress(context_id: str, text: str, active: bool = True, **kwargs) -> None:
    common.set_progress(PROGRESS, context_id, text, active, **kwargs)


def _end_progress(context_id: str) -> None:
    common.end_progress(PROGRESS, context_id)


def _grok_authenticated() -> bool:
    """True iff the Grok CLI has usable credentials, so we never spawn a headless
    turn that would silently block on the interactive device/OAuth login prompt.
    `grok login` writes ~/.grok/auth.json; enterprise/CI paths inject a key or an
    external auth-provider command instead."""
    if os.environ.get("GROK_DEPLOYMENT_KEY", "").strip():
        return True
    if os.environ.get("GROK_AUTH_PROVIDER_COMMAND", "").strip():
        return True
    return GROK_AUTH_FILE.is_file()


# A line the grok process prints (as plain text, not a JSON event) when its
# credentials are missing/expired and it falls back to the interactive login
# flow. If we see this we kill the turn immediately rather than hang for the
# full timeout waiting on a browser confirmation that will never come.
_GROK_LOGIN_PROMPT_RE = re.compile(
    r"open this url|sign in with grok|waiting for authorization|"
    r"to sign in|not authenticated|please (?:log|sign) ?in|"
    r"confirm this code",
    re.IGNORECASE,
)


# The visible width of Agent Chat's live "working…" line: the typing indicator
# is `max-width: 60ch` with a right-truncating `truncate` class, so anything past
# ~60 monospace chars is clipped off the END — i.e. the newest words. We size the
# streamed progress line to fit inside that so the tail we show is the tail the
# user actually sees, keeping the live line pinned to what Grok is writing *now*
# rather than a longer window whose freshest words fall off the right edge.
PROGRESS_LINE_CHARS = int(os.environ.get("GROK_BRIDGE_PROGRESS_LINE_CHARS", "58"))


# A buffer ending in a lowercase letter, digit, or sentence-ending punctuation.
# Deliberately excludes an uppercase last char so a tokenizer splitting an
# acronym ('A' + 'B') isn't mistaken for a block boundary and spaced apart.
_STREAM_SEAM_RE = re.compile(r"[a-z0-9.!?]\Z")
_THOUGHT_SEAM_RE = _STREAM_SEAM_RE  # back-compat alias


def _append_delta(parts: list, delta: str) -> None:
    """Append one thought/text delta, restoring the seam between stream blocks.

    Grok runs consecutive blocks together in one delta stream with no boundary
    event: '…calculate 17*23 carefully' is followed straight away by a delta
    'The list_dir returned…', giving 'carefullyThe'. The same drop happens on
    answer tokens ('live.' + 'I will…' → 'live.I'). Continuation tokens inside
    a word arrive lowercase, so a delta that opens with a capital and no
    leading space marks a new block — restore the space the stream dropped."""
    if not delta:
        return
    if parts and delta[:1].isupper() and _STREAM_SEAM_RE.search(parts[-1]):
        parts.append(" ")
    parts.append(delta)


def _append_thought(parts: list, delta: str) -> None:
    _append_delta(parts, delta)


def _append_answer(parts: list, delta: str) -> None:
    _append_delta(parts, delta)


def _stream_tail(buf: str, limit: int = PROGRESS_LINE_CHARS) -> str:
    """The newest words of a token-delta stream, cut on a word boundary.

    Grok streams `thought` and `text` one token at a time — {"type":"thought",
    "data":"The"}, {"type":"thought","data":" user"} — whereas the Codex/Claude
    bridges are handed a whole reasoning block per event and clip it from the
    head. Head-clipping a token stream would pin the progress line to the first
    few words for the entire turn, so we show the tail and let it advance as Grok
    writes. We size it to ``PROGRESS_LINE_CHARS`` (the width the UI actually
    shows) so the freshest words are the visible ones, not clipped off the right.
    """
    text = " ".join(str(buf or "").split())
    if len(text) <= limit:
        return text
    tail = text[-(limit - 1):]  # leave room for the leading ellipsis
    cut = tail.find(" ")
    if 0 <= cut <= 16:  # drop a leading word fragment, but never most of the line
        tail = tail[cut + 1:]
    return "…" + tail


# NOTE: Grok's headless `streaming-json` output emits only {type: thought|text|
# end} — tool calls (shell, file read/write/edit, web search) produce NO events
# of their own; they surface purely as narration inside the `thought` stream,
# which the accumulated live line already shows. Verified 2026-07-10 by running a
# five-tool task (create/read/shell/edit/web-search): the tools ran, the file was
# written, and the only event types on the wire were thought/text/end. So there is
# deliberately no per-tool "Running: …" / "Searching the web" mapping here — it
# would be dead code guessing at a schema the CLI never produces. If a future CLI
# version starts emitting structured tool events, add the mapping then, against
# the real shape.


def _load_contexts() -> dict:
    return common.load_contexts(CONTEXT_STORE)


def _save_contexts(contexts: dict) -> None:
    common.save_contexts(CONTEXT_STORE, contexts)


def _context(context_id: Optional[str]) -> tuple[str, dict]:
    return common.get_or_create_context(CONTEXT_STORE, context_id)


def _authorized(request: Request) -> bool:
    return common.authorized(request, BRIDGE_TOKEN)


# Room-header stripping lives in _bridge_common (single source of truth shared
# with the Codex/Claude bridges). Alias keeps existing call sites unchanged.
_strip_system_header = common.strip_system_header


def _localize_attachments(message: str) -> str:
    """Annotate chat attachment URLs with local paths Grok can read directly."""
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
    sandbox: str = GROK_SANDBOX,
) -> str:
    history = ctx.get("messages", [])[-MAX_CONTEXT_MESSAGES:]
    history_lines = common.format_history_lines(history)
    summary = str(ctx.get("summary") or "").strip()
    summary_block = (
        f"[Summary of earlier conversation]\n{summary}\n\n" if summary else ""
    )
    # Trim history lines from the oldest end until the assembled prompt will
    # fit within MAX_PROMPT_CHARS (bounds token cost on long chats).
    preamble_len = 2600  # rough char count of the static system preamble below
    summary_len = len(summary_block)
    budget = MAX_PROMPT_CHARS - preamble_len - summary_len - len(message) - 200
    if budget > 0:
        while history_lines and sum(len(l) for l in history_lines) > budget:
            history_lines.pop(0)
    history_block = "\n\n".join(history_lines) if history_lines else "No prior bridge-local context."

    return (
        "You are Grok, xAI's model, inside the user's local Agent Chat roster as "
        "@grok. Answer as Grok running through the Grok CLI on this computer: "
        "direct, sharp, a little irreverent, and honest about what you can "
        "verify. "
        f"You have real workspace access at {workspace}. "
        + (
            "This turn is enforced read-only: inspect files and report findings, "
            "but do not edit, create, delete, rename, or execute project files, "
            "and do not use browser or MCP action tools. "
            if sandbox == "read-only" else
            "You can read, edit, and create files there, and should use those "
            "tools when the user asks for file or app work. Prefer reversible "
            "changes, stay inside the assigned workspace, and ask before "
            "destructive or system-wide actions. "
        )
        + f"The Grok subprocess runs with permission-mode={GROK_PERMISSION_MODE!r}"
        + (f" and sandbox={sandbox!r}" if sandbox else " and no OS sandbox")
        + ". Prefer local file paths over remote fetches. "
        "If chrome-devtools MCP tools are present they attach to the user's "
        "already-open Chrome through the local loopback permission server "
        "(http://127.0.0.1:55022/mcp). Use them only for the asked task. Do "
        "not open, click, screenshot, or switch to unrelated tabs (mail, "
        "banking, cPanel, insurance). "
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


def _base_argv(
    model: str,
    reasoning: str,
    workspace: str,
    output_format: str,
    *,
    sandbox: Optional[str] = None,
) -> list:
    """Common grok headless invocation. --prompt-file is added by the caller so a
    huge assembled prompt never hits an argv length limit."""
    argv = common.executable_command(
        GROK_BIN,
        "-m", model,
        "--reasoning-effort", reasoning,
        "--cwd", workspace,
        "--permission-mode", GROK_PERMISSION_MODE,
        "--no-memory",  # bridge manages chat memory itself; don't mix in grok's
        "--output-format", output_format,
    )
    resolved_sandbox = GROK_SANDBOX if sandbox is None else sandbox
    if resolved_sandbox:
        argv += ["--sandbox", resolved_sandbox]
    return argv


async def _summarize(old_summary: str, messages: list) -> str:
    """Fold older messages into a compact running summary via the fast model."""
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
    with tempfile.NamedTemporaryFile(
        prefix="grok-summary-", suffix=".txt", delete=False, mode="w", encoding="utf-8"
    ) as f:
        f.write(instruction)
        prompt_path = Path(f.name)
    try:
        cmd = common.executable_command(
            GROK_BIN, "--prompt-file", str(prompt_path), "-m", SUMMARY_MODEL,
            "--cwd", GROK_WORKDIR, "--sandbox", "read-only",
            "--no-memory", "--output-format", "json",
        )
        common.load_env_files(_ENV_FILES, overwrite=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SUMMARY_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError("summary timed out")
        if proc.returncode not in (0, None):
            raise RuntimeError((stderr or stdout).decode("utf-8", errors="replace")[-400:])
        reply = ""
        try:
            reply = str(json.loads(stdout.decode("utf-8", errors="replace")).get("text") or "").strip()
        except Exception:
            reply = stdout.decode("utf-8", errors="replace").strip()
        return reply or old_summary
    finally:
        try:
            prompt_path.unlink()
        except FileNotFoundError:
            pass


async def _maybe_summarize(ctx: dict) -> None:
    """When a context outgrows the window, compact the overflow into ctx['summary'].
    Fail-open: on any error, hard-cap the message list instead (never raises).
    Policy lives in _bridge_common; _summarize is the Grok-specific model call."""
    await common.maybe_summarize(
        ctx, _summarize,
        trigger=SUMMARY_TRIGGER, keep=SUMMARY_KEEP,
        hard_cap=SUMMARY_HARD_CAP, max_chars=SUMMARY_MAX_CHARS,
    )


def _grok_options(body: dict) -> tuple[str, str]:
    model = str(body.get("model") or GROK_MODEL).strip()
    reasoning = str(body.get("reasoning") or GROK_REASONING).strip()
    if reasoning in ("xhigh", "max"):
        reasoning = "high"
    if model not in MODEL_OPTIONS:
        model = GROK_MODEL if GROK_MODEL in MODEL_OPTIONS else "grok-4.6"
    if reasoning not in REASONING_OPTIONS:
        reasoning = GROK_REASONING if GROK_REASONING in REASONING_OPTIONS else "low"
    return model, reasoning


# Markers in a Grok failure that mean "this won't clear on a quick auto-retry"
# (auth expiry, usage/quota caps, billing). Surfaced as non-retryable so the app
# raises a clear hold immediately instead of burning its short retry budget.
_GROK_NON_RETRYABLE_RE = re.compile(
    r"not authenticated|please (?:log|sign) ?in|unauthorized|invalid.*token|"
    r"token expired|reauth|usage limit|hit your (?:usage|rate) limit|"
    r"purchase more credits|upgrade to|\bquota\b|out of credits|"
    r"insufficient.*credit|payment required|\bbilling\b",
    re.IGNORECASE,
)


class GrokRunError(RuntimeError):
    """A Grok turn failed with a real reason (from the JSON stream or stderr).
    `retryable` is False for auth/usage caps that a quick retry can't fix; the app
    honors it (see _agent_http_error) to skip pointless retries. `http_status` is
    what the bridge returns so the app classifies it the same way (429 = limit)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.retryable = not bool(_GROK_NON_RETRYABLE_RE.search(message or ""))
        self.http_status = 500 if self.retryable else 429


async def _run_grok(
    prompt: str,
    model: str,
    reasoning: str,
    context_id: str,
    workspace: str,
    *,
    sandbox: str = GROK_SANDBOX,
) -> "tuple[str, Optional[dict]]":
    # Guard BEFORE spawning: an unauthenticated `grok -p` drops into the
    # interactive device/OAuth login flow and would block for the whole timeout.
    if not _grok_authenticated():
        raise GrokRunError(
            "Grok is not authenticated. Run `grok login` on the Mac (writes "
            "~/.grok/auth.json), then reconnect @grok."
        )
    with tempfile.NamedTemporaryFile(
        prefix="grok-agent-", suffix=".txt", delete=False, mode="w", encoding="utf-8"
    ) as f:
        f.write(prompt)
        prompt_path = Path(f.name)
    try:
        cmd = _base_argv(
            model, reasoning, workspace, "streaming-json", sandbox=sandbox,
        ) + [
            "--prompt-file", str(prompt_path),
        ]
        # Re-read env files so a Secrets-panel grant reaches this spawn (the
        # grok child inherits os.environ) without a bridge restart.
        common.load_env_files(_ENV_FILES, overwrite=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,  # never let it block on tty input
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _set_progress(context_id, "Starting up…", True)

        answer_parts: list[str] = []
        thought_parts: list[str] = []
        session_id = ""
        stop_reason = ""
        last_error = ""
        auth_prompt_seen = False

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
            nonlocal session_id, stop_reason, last_error, auth_prompt_seen
            line = raw.strip()
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            try:
                evt = json.loads(text)
            except json.JSONDecodeError:
                # Non-JSON line: the only thing we care about is grok dropping
                # into the interactive login flow (credentials missing/expired).
                if _GROK_LOGIN_PROMPT_RE.search(text):
                    auth_prompt_seen = True
                return
            if not isinstance(evt, dict):
                return
            etype = str(evt.get("type") or "").lower()
            pline: Optional[str] = None
            if etype == "text":
                _append_answer(answer_parts, str(evt.get("data") or ""))
                answer = "".join(answer_parts)
                common.set_partial_response(PROGRESS, context_id, answer)
                pline = _stream_tail(answer)
            elif etype == "thought":
                _append_thought(thought_parts, str(evt.get("data") or ""))
                pline = _stream_tail("".join(thought_parts)) or "Thinking…"
            elif etype == "error":
                last_error = (
                    str(evt.get("message") or evt.get("data") or "").strip() or last_error
                )
            elif etype == "end":
                stop_reason = str(evt.get("stopReason") or "").strip()
                session_id = str(evt.get("sessionId") or "").strip() or session_id
            # Any other event type is ignored: the CLI only emits thought/text/
            # end (+ error), and the live line is already primed with "Starting
            # up…" before the first event arrives (see below).
            if pline:
                _set_progress(context_id, pline, True)

        async def _read_events() -> None:
            # Read raw chunks and split on newlines ourselves rather than using
            # readline(), whose 64 KiB per-line limit raises on a big JSON event.
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
                if auth_prompt_seen:
                    break
            if buf:
                _handle_line(buf)

        stderr_task = asyncio.create_task(_drain_stderr())
        try:
            await asyncio.wait_for(_read_events(), timeout=GROK_TIMEOUT_S)
            if auth_prompt_seen:
                proc.kill()
                await proc.wait()
                _end_progress(context_id)
                raise GrokRunError(
                    "Grok tried to open an interactive login (credentials missing "
                    "or expired). Run `grok login` on the Mac, then reconnect @grok."
                )
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _end_progress(context_id)
            raise RuntimeError("Grok timed out while answering.")
        finally:
            pass

        stderr_data = b""
        try:
            stderr_data = await stderr_task
        except Exception:
            pass
        _end_progress(context_id)

        reply = "".join(answer_parts).strip()
        if proc.returncode not in (0, None):
            stderr_detail = stderr_data.decode("utf-8", errors="replace").strip()
            # Prefer a real failure reason (JSON error event, then stderr); fall
            # back to any partial reply, then the bare exit code.
            detail = (
                last_error
                or stderr_detail
                or reply
                or f"Grok exited with {proc.returncode}."
            )
            raise GrokRunError(detail[-1200:])
        if not reply and last_error:
            raise GrokRunError(last_error[-1200:])
        return reply or "(Grok returned an empty response.)", None
    finally:
        try:
            prompt_path.unlink()
        except FileNotFoundError:
            pass


@app.get("/health")
async def health():
    return {
        "status": "ok" if (BRIDGE_TOKEN and _grok_authenticated()) else (
            "missing-token" if not BRIDGE_TOKEN else "not-authenticated"
        ),
        "grok_bin": GROK_BIN,
        "grok_bin_present": Path(GROK_BIN).is_file(),
        "authenticated": _grok_authenticated(),
        "workdir": GROK_WORKDIR,
        "workdir_present": Path(GROK_WORKDIR).is_dir(),
        "model": GROK_MODEL,
        "reasoning": GROK_REASONING,
        "permission_mode": GROK_PERMISSION_MODE,
        "sandbox": GROK_SANDBOX or "off",
        "token_present": bool(BRIDGE_TOKEN),
        "model_options": sorted(MODEL_OPTIONS),
        "reasoning_options": sorted(REASONING_OPTIONS),
        "attachment_local_files_dir": str(LOCAL_FILES_DIR),
        "turn_timeout_seconds": GROK_TIMEOUT_S,
    }


# Self-reported capability card. The orchestrator's concierge reads this (cached)
# to route work to the right agent instead of guessing from a static table —
# keep `best_for`/`strengths` honest and current as this agent's role evolves.
@app.get("/capabilities")
async def capabilities():
    return {
        "id": "grok",
        "model": GROK_MODEL,
        "best_for": (
            "Fast, current-events-aware coding and reasoning with live web "
            "search — quick implementation, debugging, and candid technical "
            "takes, with an eye on what's happening right now."
        ),
        "strengths": [
            "up-to-the-minute web search & current information",
            "fast implementation & concrete diffs",
            "debugging & root-cause analysis",
            "reading and navigating a codebase",
            "running and verifying commands locally",
            "blunt, opinionated technical judgment",
        ],
        "avoid": "Nothing hard-blocked; for the very longest-horizon planning a dedicated strategist may still edge it out.",
        "voice": (
            "Direct, sharp, and a little irreverent; shows checked diffs and "
            "commands instead of describing them, and is honest about what it "
            "has and hasn't verified."
        ),
        "blurb": "xAI's Grok CLI — fast, web-connected coder with a blunt streak.",
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
    model, reasoning = _grok_options(body)
    workspace = common.resolve_workspace(body, GROK_WORKDIR)
    workspace_access = str(body.get("workspace_access") or "writable").strip().lower()
    if workspace_access not in {"writable", "read-only"}:
        return JSONResponse(
            {"error": "workspace_access must be writable or read-only"},
            status_code=400,
        )
    sandbox = "read-only" if workspace_access == "read-only" else GROK_SANDBOX

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
        reply, usage = await _run_grok(
            _prompt(message, ctx, model, reasoning, workspace, sandbox),
            model, reasoning, context_id, workspace, sandbox=sandbox,
        )
    except Exception as e:
        _end_progress(context_id)
        # Surface the real reason + whether it's worth a quick retry. GrokRunError
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
    # Grok's streaming-json output carries no token counts, so the app meters
    # this turn by estimation (its default path when no usage is returned).
    if isinstance(usage, dict):
        out["usage"] = usage
    return out
