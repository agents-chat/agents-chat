import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional
import base64
import mimetypes
import httpx

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import sys as _sys
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
import _bridge_common as common  # shared bridge core (single source of truth)


APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent.parent
STATE_DIR = APP_DIR / "antigravity_bridge"
STATE_DIR.mkdir(exist_ok=True)
CONTEXT_STORE = STATE_DIR / "contexts.json"
AGY_RUN_LOG_DIR = STATE_DIR / "logs"
AGY_RUN_LOG_DIR.mkdir(exist_ok=True)


_ENV_FILES = (
    APP_DIR / ".env",
    REPO_DIR / ".env",
    APP_DIR / "agents" / "shared.env",
    APP_DIR / "agents" / "antigravity.env",
)
common.load_env_files(_ENV_FILES)


# The Antigravity CLI ("agy") — Google's headless coding agent. It ships
# separately from the IDE and installs to ~/.local/bin/agy. Like `claude -p`
# and `codex exec`, `agy -p "<prompt>"` runs a single prompt non-interactively
# and prints the reply. Auth is shared with the signed-in Antigravity desktop
# app via the macOS Keychain — no separate login. Override with ANTIGRAVITY_BIN.
def _resolve_agy_bin(configured: str) -> str:
    if configured and Path(configured).is_file():
        return configured
    for cand in (
        Path.home() / ".local" / "bin" / "agy",
        Path("/opt/homebrew/bin/agy"),
        Path("/usr/local/bin/agy"),
    ):
        if cand.is_file():
            return str(cand)
    found = shutil.which("agy")
    return found or configured


AGY_BIN_CONFIGURED = os.environ.get("ANTIGRAVITY_BIN", str(Path.home() / ".local" / "bin" / "agy"))
_AGY_BIN_CACHE = _resolve_agy_bin(AGY_BIN_CONFIGURED)


def current_agy_bin() -> str:
    global _AGY_BIN_CACHE
    if _AGY_BIN_CACHE and Path(_AGY_BIN_CACHE).is_file():
        return _AGY_BIN_CACHE
    _AGY_BIN_CACHE = _resolve_agy_bin(AGY_BIN_CONFIGURED)
    return _AGY_BIN_CACHE


# Fallback workspace when the orchestrator doesn't pass a per-chat `workspace`
# (a direct/legacy caller). Deliberately NOT the Agent-Chat repo — agents must
# never do real work inside the chat app folder. The orchestrator normally
# sends a per-chat workspace that overrides this.
ANTIGRAVITY_WORKDIR = os.environ.get(
    "ANTIGRAVITY_BRIDGE_WORKDIR", str(Path.home() / "AGENTS" / "workspaces" / "antigravity")
)
ANTIGRAVITY_MODEL = os.environ.get("ANTIGRAVITY_BRIDGE_MODEL", "Gemini 3.1 Pro (High)")
ANTIGRAVITY_TIMEOUT_HARD_CAP_S = float(
    os.environ.get("ANTIGRAVITY_BRIDGE_TIMEOUT_HARD_CAP_S", "600")
)
ANTIGRAVITY_TIMEOUT_S = min(
    float(os.environ.get("ANTIGRAVITY_BRIDGE_TIMEOUT_S", "600")),
    ANTIGRAVITY_TIMEOUT_HARD_CAP_S,
)
MAX_CONTEXT_MESSAGES = int(os.environ.get("ANTIGRAVITY_BRIDGE_MAX_CONTEXT_MESSAGES", "12"))
# Rolling summarization (same policy as the Claude/Codex bridges): once a
# context exceeds SUMMARY_TRIGGER messages, fold all but the last SUMMARY_KEEP
# into a persistent running summary via a cheap model, so long chats keep their
# gist without the prompt growing forever. SUMMARY_HARD_CAP bounds the list if
# summarization is unavailable (fail-open).
SUMMARY_TRIGGER = int(os.environ.get("ANTIGRAVITY_BRIDGE_SUMMARY_TRIGGER", "16"))
SUMMARY_KEEP = int(os.environ.get("ANTIGRAVITY_BRIDGE_SUMMARY_KEEP", "8"))
SUMMARY_HARD_CAP = int(os.environ.get("ANTIGRAVITY_BRIDGE_SUMMARY_HARD_CAP", "24"))
SUMMARY_MODEL = os.environ.get("ANTIGRAVITY_BRIDGE_SUMMARY_MODEL", "Gemini 3.5 Flash (Low)")
SUMMARY_TIMEOUT_S = float(os.environ.get("ANTIGRAVITY_BRIDGE_SUMMARY_TIMEOUT_S", "120"))
SUMMARY_MAX_CHARS = int(os.environ.get("ANTIGRAVITY_BRIDGE_SUMMARY_MAX_CHARS", "2000"))
BRIDGE_TOKEN = os.environ.get("AGENT_TOKEN_ANTIGRAVITY", "").strip()

# Exact model strings as reported by `agy models` (an unknown value silently
# falls back to agy's default, so we keep these verbatim). The UI offers the
# same list with friendly labels.
MODEL_OPTIONS = {
    "Gemini 3.7 Flash (High)",
    "Gemini 3.7 Flash (Medium)",
    "Gemini 3.7 Flash (Low)",
    "Gemini 3.6 Flash (High)",
    "Gemini 3.6 Flash (Medium)",
    "Gemini 3.6 Flash (Low)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
}

# Auto-approve tool permission requests so the headless agent never blocks
# waiting for an approval it can't get (it would just time out). This gives agy
# real file/command access in its workspace — parity with the Claude/Codex
# bridges. Set ANTIGRAVITY_BRIDGE_SKIP_PERMISSIONS=0 to require approval (and
# accept that agentic tool turns may hang).
SKIP_PERMISSIONS = os.environ.get("ANTIGRAVITY_BRIDGE_SKIP_PERMISSIONS", "1").lower() in (
    "1", "true", "yes", "on",
)
SANDBOX = os.environ.get("ANTIGRAVITY_BRIDGE_SANDBOX", "0").lower() in ("1", "true", "yes", "on")
# Extra dirs granted on top of the per-chat workspace (which is added by
# _agy_cmd at call time). Empty by default — the workspace itself is no longer
# baked in here.
ADD_DIRS = [
    d.strip()
    for d in re.split(r"[,\n]", os.environ.get("ANTIGRAVITY_BRIDGE_ADD_DIRS", ""))
    if d.strip()
]

# Attachments shared in chat are served to Dockerized agents at a host URL this
# native bridge can't reach. The files live on this same disk, so we mirror them
# (with their real filename/extension) and point agy at the local copy so it can
# Read them — including images/screenshots.
ATTACHMENTS_DIR = Path(os.environ.get("DATA_DIR", APP_DIR)) / "attachments"
LOCAL_FILES_DIR = STATE_DIR / "files"
LOCAL_FILES_DIR.mkdir(exist_ok=True)
_ATTACHMENT_URL_RE = re.compile(r"https?://[^/\s]+/attachments/(\d+)/([a-f0-9]{32})")

mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/markdown", ".markdown")


def _extract_chat_id(message: str, workspace: str) -> Optional[int]:
    # 1. Look for Include "chat_id": 1201 or similar instructions
    m = re.search(r'chat_id"?\s*:\s*"?(\d{1,12})', message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # 2. Look for chat-<number> in workspace path
    m = re.search(r'chat-(\d+)', workspace)
    if m:
        return int(m.group(1))
    return None


async def _auto_upload_artifacts(reply: str, chat_id: int, workspace: str) -> None:
    fence_re = re.compile(r"```artifact[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
    matches = fence_re.findall(reply)
    if not matches:
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        for block in matches:
            meta = {}
            first, _, rest = block.partition("\n")
            first_stripped = first.strip()
            if first_stripped.startswith("{"):
                try:
                    meta = json.loads(first_stripped)
                except json.JSONDecodeError:
                    try:
                        meta = json.loads(block.strip())
                    except json.JSONDecodeError:
                        pass

            path_str = str(meta.get("path") or "").strip()
            if not path_str:
                continue

            path = Path(path_str)
            if not path.is_absolute():
                path = Path(workspace) / path

            try:
                path = path.resolve()
                if not path.is_file():
                    continue
            except Exception:
                continue

            try:
                data = path.read_bytes()
                if not data:
                    continue

                if len(data) > 200 * 1024 * 1024:
                    continue

                name = path.name
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                # attachment_save rejects active browser content twice over
                # (mime check + byte sniff, its stored-XSS guard) — no rename
                # or declared MIME gets HTML past it. Skip such files here so
                # e.g. the hourly Email Radar report.html stops bouncing 415s.
                head = data[:4096].lstrip().lower()
                if head.startswith(b"\xef\xbb\xbf"):
                    head = head[3:].lstrip()
                if (mime in ("text/html", "application/xhtml+xml", "image/svg+xml")
                        or mime.endswith(("/xml", "+xml"))
                        or head.startswith((b"<!doctype html", b"<html", b"<script", b"<?xml",
                                            b"<svg", b"<iframe", b"<object", b"<embed"))
                        or any(n in head for n in (b"<script", b"javascript:", b"<svg", b"<iframe",
                                                   b"<object", b"<embed", b"srcdoc=", b"onload=",
                                                   b"onerror=", b"data:text/html"))):
                    print(f"Skipped auto-upload of {name} (active browser content; server would 415)", flush=True)
                    continue

                payload = {
                    "chat_id": chat_id,
                    "name": name,
                    "mime": mime,
                    "content_b64": base64.b64encode(data).decode("ascii"),
                    "text": f"Antigravity delivered **{name}** to the Files tab.",
                }

                orchestrator_url = (
                    os.environ.get("ORCHESTRATOR_API_BASE")
                    or os.environ.get("ORCHESTRATOR_INTERNAL_URL")
                    or "http://127.0.0.1:8086"
                ).rstrip("/")

                resp = await client.post(
                    f"{orchestrator_url}/api/chat_bridge/attachment_save",
                    json=payload,
                    headers={"X-API-KEY": BRIDGE_TOKEN},
                )
                if resp.status_code == 200:
                    print(f"Auto-uploaded {name} successfully to chat {chat_id}")
                else:
                    print(f"Failed to auto-upload {name}: {resp.status_code} - {resp.text}")
            except Exception as e:
                print(f"Error processing auto-upload for {path}: {e}")


app = FastAPI(title="Antigravity Agent Chat Bridge")


# Live "current step" text per context_id, surfaced to Agent Chat via
# api_log_get so @antigravity shows a thinking line while it works — the same
# mechanism the other agents use. agy's -p mode prints only the final reply (no
# structured event stream), so the line is coarse: "Thinking…" for the duration
# of the turn rather than per-tool narration. In-memory only (single worker).
PROGRESS: dict[str, dict] = {}


def _set_progress(context_id: str, text: str, active: bool = True, **kwargs) -> None:
    common.set_progress(PROGRESS, context_id, text, active, **kwargs)


def _end_progress(context_id: str) -> None:
    common.end_progress(PROGRESS, context_id)


def _child_env() -> dict:
    """Environment for the agy subprocess. Ensure ~/.local/bin is on PATH (agy
    is installed there) and that the Keychain-backed auth the IDE established is
    reachable. We leave the inherited env otherwise intact."""
    # Re-read env files so a Secrets-panel grant reaches the next spawn
    # without a bridge restart.
    common.load_env_files(_ENV_FILES, overwrite=True)
    env = dict(os.environ)
    local_bin = str(Path.home() / ".local" / "bin")
    path = env.get("PATH", "")
    if local_bin not in path.split(os.pathsep):
        env["PATH"] = local_bin + os.pathsep + path if path else local_bin
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
    "You are Antigravity inside the user's local Agent Chat roster as @antigravity, "
    "running through Google's Antigravity CLI. Be warm, precise, "
    "and honest about what you can verify. "
    "You have real tool access in your per-chat workspace (its path is stated below): "
    "you can "
    "read, edit, and create files and run commands. Use them when they help answer — "
    "read files before describing them, and make the edits the user asks for. Prefer "
    "reversible changes, stay inside the assigned workspace, and ask before destructive "
    "or system-wide actions. "
    "Attachments the user shares in chat are mirrored to local disk; each is listed with "
    "a 'local file:' path in the message — read that path directly (it works for "
    "images/screenshots too) instead of trying to fetch the http URL. "
    "The Agent Chat orchestrator has already included the current room state and "
    "collaboration instructions below."
)


def _localize_attachments(message: str) -> str:
    """Mirror any chat attachment referenced by URL to a locally readable copy
    (with its real filename) and annotate the URL with that local path, so the
    native bridge agent can read it without fetching the unreachable host URL."""
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
# with the Codex/Claude bridges). Alias keeps existing call sites unchanged.
_strip_system_header = common.strip_system_header


def _prompt(
    message: str,
    ctx: dict,
    model: str,
    workspace: str,
    *,
    workspace_access: str = "writable",
) -> str:
    history = ctx.get("messages", [])[-MAX_CONTEXT_MESSAGES:]
    history_lines = common.format_history_lines(history)
    history_block = "\n\n".join(history_lines) if history_lines else "No prior bridge-local context."
    blocks = [
        f"{SYSTEM_PROMPT} Your working directory for this chat is {workspace}; "
        + (
            "this turn is enforced read-only. Inspect files and report findings; "
            "do not edit, create, delete, rename, or execute project files. "
            if workspace_access == "read-only" else
            "read, edit, and create files there. "
        )
        +
        f"Agent Chat is running you with model '{model}', set on your "
        "card in the Agent Chat UI. If asked which model you're using, state this "
        "value directly rather than guessing. Your memory of this chat is managed by "
        "the bridge: each turn you receive a running summary of older turns plus the "
        "most recent messages verbatim."
    ]
    summary = str(ctx.get("summary") or "").strip()
    if summary:
        blocks.append("[Summary of earlier conversation]\n" + summary)
    blocks.append("[Bridge-local recent context]\n" + history_block)
    blocks.append("[New Agent Chat prompt]\n" + message)
    return "\n\n".join(blocks)


def _agy_log_path() -> Path:
    path = AGY_RUN_LOG_DIR / f"agy-{int(time.time() * 1000)}-{os.getpid()}.log"
    try:
        logs = sorted(AGY_RUN_LOG_DIR.glob("agy-*.log"), key=lambda p: p.stat().st_mtime)
        for old in logs[:-40]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
    return path


def _read_agy_log(log_path: Optional[Path]) -> str:
    if not log_path or not log_path.is_file():
        return ""
    try:
        return log_path.read_text(errors="replace")
    except Exception:
        return ""


def _agy_failure_detail(stderr_text: str, log_text: str) -> str:
    text = "\n".join(t for t in (stderr_text, log_text) if t).strip()
    if not text:
        return ""

    quota = re.search(
        r"RESOURCE_EXHAUSTED \(code 429\):\s*(.+?)(?:\n|$)",
        text,
    )
    if quota:
        detail = " ".join(quota.group(1).split())
        detail = re.split(r":\s*RESOURCE_EXHAUSTED \(code 429\):", detail, maxsplit=1)[0].strip()
        return f"Antigravity quota is exhausted: {detail}"

    if "You are not logged into Antigravity" in text and not re.search(
        r"(OAuth: authenticated successfully|Print mode: silent auth succeeded)",
        text,
    ):
        return "Antigravity is not logged in. Open the Antigravity app on this Mac and sign in, then reconnect @antigravity."

    agent_error = re.search(r"agent executor error:\s*(.+?)(?:\n|$)", text)
    if agent_error:
        return "Antigravity backend error: " + " ".join(agent_error.group(1).split())

    return ""


def _agy_cmd(
    prompt: str,
    model: str,
    workspace: str,
    log_path: Optional[Path] = None,
    *,
    workspace_access: str = "writable",
) -> list:
    cmd = common.executable_command(
        current_agy_bin(), "-p", prompt, "--model", model
    )
    if workspace_access == "read-only":
        cmd += ["--mode", "plan"]
    elif SKIP_PERMISSIONS:
        cmd.append("--dangerously-skip-permissions")
    if SANDBOX:
        cmd.append("--sandbox")
    cmd += ["--add-dir", workspace]
    for d in ADD_DIRS if workspace_access != "read-only" else []:
        cmd += ["--add-dir", d]
    # Give agy's own print-mode wait a little more headroom than our outer
    # asyncio timeout so we surface a clean "timed out" rather than agy's.
    cmd += ["--print-timeout", f"{int(ANTIGRAVITY_TIMEOUT_S) + 60}s"]
    if log_path:
        cmd += ["--log-file", str(log_path)]
    return cmd


async def _run_agy_raw(
    prompt: str,
    model: str,
    timeout_s: float,
    workspace: str = ANTIGRAVITY_WORKDIR,
    *,
    workspace_access: str = "writable",
) -> str:
    """Run a single agy -p prompt and return its stdout text (raises on error)."""
    log_path = _agy_log_path()
    proc = await asyncio.create_subprocess_exec(
        *_agy_cmd(
            prompt, model, workspace, log_path,
            workspace_access=workspace_access,
        ),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
        env=_child_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("Antigravity timed out while answering.")
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    log_text = _read_agy_log(log_path)
    diagnostic = _agy_failure_detail(stderr_text, log_text)
    if proc.returncode not in (0, None):
        detail = diagnostic or stderr_text or stdout_text
        raise RuntimeError(detail[-1200:] or f"Antigravity (agy) exited with {proc.returncode}.")
    if not stdout_text and diagnostic:
        raise RuntimeError(diagnostic[-1200:])
    return stdout_text


async def _run_antigravity(
    prompt: str,
    model: str,
    context_id: str,
    workspace: str,
    *,
    workspace_access: str = "writable",
) -> str:
    _set_progress(context_id, "Thinking…", True)
    try:
        reply = await _run_agy_raw(
            prompt,
            model,
            ANTIGRAVITY_TIMEOUT_S,
            workspace,
            workspace_access=workspace_access,
        )
    finally:
        _end_progress(context_id)
    return reply or "(Antigravity returned an empty response.)"


async def _summarize(old_summary: str, messages: list) -> str:
    """Fold older messages into a compact running summary via a cheap agy model."""
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
    model = SUMMARY_MODEL if SUMMARY_MODEL in MODEL_OPTIONS else ANTIGRAVITY_MODEL
    return (await _run_agy_raw(instruction, model, SUMMARY_TIMEOUT_S)).strip() or old_summary


async def _maybe_summarize(ctx: dict) -> None:
    await common.maybe_summarize(
        ctx, _summarize,
        trigger=SUMMARY_TRIGGER, keep=SUMMARY_KEEP,
        hard_cap=SUMMARY_HARD_CAP, max_chars=SUMMARY_MAX_CHARS,
    )


def _antigravity_options(body: dict) -> str:
    model = str(body.get("model") or ANTIGRAVITY_MODEL).strip()
    if model not in MODEL_OPTIONS:
        model = ANTIGRAVITY_MODEL if ANTIGRAVITY_MODEL in MODEL_OPTIONS else "Gemini 3.1 Pro (High)"
    return model


@app.get("/health")
async def health():
    bin_path = current_agy_bin()
    return {
        "status": "ok" if BRIDGE_TOKEN else "missing-token",
        "agy_bin": bin_path,
        "agy_bin_present": bool(bin_path) and Path(bin_path).is_file(),
        "workdir": ANTIGRAVITY_WORKDIR,
        "model": ANTIGRAVITY_MODEL,
        "token_present": bool(BRIDGE_TOKEN),
        "model_options": sorted(MODEL_OPTIONS),
        "skip_permissions": SKIP_PERMISSIONS,
        "sandbox": SANDBOX,
        "add_dirs": ADD_DIRS,
        "turn_timeout_seconds": ANTIGRAVITY_TIMEOUT_S,
    }


# Self-reported capability card. The orchestrator's concierge reads this (cached)
# to route work to the right agent instead of guessing from a static table —
# keep `best_for`/`strengths` honest and current as this agent's role evolves.
@app.get("/capabilities")
async def capabilities():
    return {
        "id": "antigravity",
        "model": ANTIGRAVITY_MODEL,
        "best_for": (
            "Tool-using coding in the shared workspace via Google's Antigravity "
            "(agy) — implementation, edits, and running commands across Gemini / "
            "Claude / GPT-OSS models."
        ),
        "strengths": [
            "implementation",
            "tool use",
            "in-place workspace edits",
            "running and checking commands",
            "multi-model (Gemini/Claude/GPT-OSS)",
        ],
        "avoid": "Long-form prose or open-ended strategy — prefer a writer/strategist.",
        "voice": (
            "Capable and plainspoken; uses tools to implement and run things "
            "directly, then reports what it actually did."
        ),
        "blurb": "Google Antigravity (agy) coding agent; capable, tool-using, plainspoken.",
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
    model = _antigravity_options(body)
    # Attached to every reply, so a model pick we could not honour shows up on
    # the turn that dropped it instead of passing as the room's choice.
    model_meta = common.model_fallback_meta(body.get("model"), model)
    workspace = common.resolve_workspace(body, ANTIGRAVITY_WORKDIR)
    workspace_access = str(body.get("workspace_access") or "writable").strip().lower()
    if workspace_access not in {"writable", "read-only"}:
        return JSONResponse(
            {"error": "workspace_access must be writable or read-only"},
            status_code=400,
        )

    context_id, ctx = _context(body.get("context_id"))
    # Mark the new turn active immediately so a poll landing before agy spawns
    # sees "Thinking…" rather than the previous turn's final line.
    _set_progress(context_id, "Thinking…", True)
    contexts = _load_contexts()
    ctx = contexts.setdefault(context_id, ctx)
    ctx.setdefault("messages", []).append({"role": "user", "text": _strip_system_header(message)})
    ctx["updated_ts"] = int(time.time() * 1000)
    _save_contexts(contexts)

    try:
        reply = await _run_antigravity(
            _prompt(
                message, ctx, model, workspace,
                workspace_access=workspace_access,
            ),
            model,
            context_id,
            workspace,
            workspace_access=workspace_access,
        )
        # Auto-upload files referenced in ```artifact blocks to the Files tab
        try:
            chat_id = _extract_chat_id(message, workspace)
            if chat_id and workspace_access != "read-only":
                await _auto_upload_artifacts(reply, chat_id, workspace)
        except Exception as ae:
            print(f"Artifact auto-upload failed: {ae}")
    except Exception as e:
        _end_progress(context_id)
        detail = str(e)
        if detail.startswith(("Antigravity quota is exhausted:", "Antigravity is not logged in.")):
            reply = detail
            contexts = _load_contexts()
            ctx = contexts.setdefault(context_id, {"messages": []})
            ctx.setdefault("messages", []).append({"role": "assistant", "text": reply})
            ctx["updated_ts"] = int(time.time() * 1000)
            _save_contexts(contexts)
            return {
                "response": reply,
                "context_id": context_id,
                "model": model,
                **model_meta,
                "ok": False,
            }
        return JSONResponse({"error": str(e)}, status_code=500)

    contexts = _load_contexts()
    ctx = contexts.setdefault(context_id, {"messages": []})
    ctx.setdefault("messages", []).append({"role": "assistant", "text": reply})
    ctx["updated_ts"] = int(time.time() * 1000)
    await _maybe_summarize(ctx)
    _save_contexts(contexts)
    return {
        "response": reply,
        "context_id": context_id,
        "model": model,
        **model_meta,
    }
