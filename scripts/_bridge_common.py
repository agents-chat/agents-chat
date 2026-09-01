"""Shared core for the Agent Chat bridges (Claude + Codex).

Both bridges are stateless one-shot CLI wrappers (`claude -p`, `codex exec
--ephemeral`) that reconstruct chat memory themselves each turn. That memory
machinery — the room-header stripping, the context store, the rolling-summary
policy, prompt history assembly, auth and progress tracking — used to be
copy-pasted into each bridge and could silently drift. It now lives here, once,
so there is a single source of truth. Only genuinely model-specific bits (how
the CLI is invoked, the system preamble, attachment localization, the HTTP
routes) stay in the individual bridges.

This module is intentionally dependency-light (stdlib only) and side-effect
free, so importing it from a long-lived FastAPI process is cheap and safe.
@hermes does not use this — it keeps no local message history (it delegates to
the Hermes server's conversation state).
"""
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional


def executable_command(executable: str, *args: str) -> list[str]:
    """Return a shell-free argv, wrapping Windows npm batch shims when needed."""
    path = Path(str(executable)).expanduser()
    command = [str(path), *args]
    if os.name == "nt" and path.suffix.lower() in (".cmd", ".bat"):
        # Put the cmd.exe builtin first and the shim path after it.  Passing a
        # quoted batch path directly after `/c` triggers cmd.exe's historical
        # quote-stripping rule, so a per-user path containing a space (any
        # profile name with a space in it) is split at that space.  `call`
        # avoids that special case while subprocess still quotes every argv
        # item normally.
        return ["cmd.exe", "/d", "/s", "/c", "call", str(path), *args]
    return command


def _parse_env_value(v: str) -> str:
    """Decode a KEY=value value that may be single- or double-quoted, matching how
    bash `source` treats these same files. Bare values are returned unchanged
    (back-compat). A single-quoted value is literal except for the shell escape
    '\\'' -> ' ; a double-quoted value just loses its wrapper. Keep in lockstep
    with app.py's _parse_env_value / _env_quote."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        inner = v[1:-1]
        if v[0] == "'":
            return inner.replace("'\\''", "'")
        return inner.replace('\\"', '"').replace('\\\\', '\\')
    return v


# Keys this process itself set from env FILES (never launchd/inherited env).
# Lets a per-turn re-load UNSET a key whose line has been deleted from every
# file — i.e. a Secrets-panel revoke takes effect on the next agent run instead
# of lingering in os.environ until the bridge restarts. Only keys we wrote are
# ever popped, so environment supplied by launchd/the shell always survives.
_FILE_LOADED_KEYS: set = set()


def load_env_files(paths, *, overwrite: bool = False) -> None:
    """Load simple KEY=VALUE files without adding a dotenv dependency.

    `agents/shared.env` is the common credential layer for current and future
    host-side bridges. Per-agent files should be listed after it so callers
    using `overwrite=True` can deliberately override shared configuration.

    Single-/double-quoted values are unwrapped (see _parse_env_value) so a secret
    with spaces or symbols — written single-quoted by the app's Secrets panel —
    loads as its real value rather than with literal quotes.

    Re-invoking with the same paths refreshes os.environ: new keys appear
    (grants), and keys this loader previously set that have vanished from every
    file are removed (revokes). Bridges call this per turn/spawn for exactly
    that reason.
    """
    seen: dict = {}
    for env_file in paths:
        env_file = Path(env_file)
        if not env_file.is_file():
            continue
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key = key.strip()
            # Preserve the original per-line precedence: first file wins for
            # setdefault loads, last file wins for overwrite loads.
            if overwrite or key not in seen:
                seen[key] = _parse_env_value(value)
    for key, value in seen.items():
        if overwrite:
            os.environ[key] = value
            _FILE_LOADED_KEYS.add(key)
        elif key not in os.environ:
            os.environ[key] = value
            _FILE_LOADED_KEYS.add(key)
    for key in _FILE_LOADED_KEYS - seen.keys():
        os.environ.pop(key, None)
        _FILE_LOADED_KEYS.discard(key)


# --- Per-chat workspace resolution ------------------------------------------
# Phase 1 of un-sandboxing the agents: the orchestrator passes a `workspace`
# (absolute path) per chat so the agent does its real file/command work in that
# project dir instead of inside the Agent-Chat app folder. The path is created
# if missing. Falls back to `fallback` when no usable workspace is supplied —
# a legacy/direct caller (no `workspace` field) keeps working, just in the
# bridge's own scratch dir rather than the chat repo. Only absolute paths are
# honored; a relative or unsafe value is ignored in favor of `fallback`.
def resolve_workspace(body, fallback: str) -> str:
    raw = ""
    if isinstance(body, dict):
        raw = str(body.get("workspace") or "").strip()
    for candidate in (raw, fallback):
        if not candidate:
            continue
        try:
            path = Path(candidate).expanduser()
            if path.is_absolute():
                path.mkdir(parents=True, exist_ok=True)
                return str(path)
        except OSError:
            continue
    return fallback


# --- Agent Chat room-header stripping --------------------------------------
# The orchestrator (app.py) injects a fresh room-state header — roster, company
# facts, mode block, behavior rules — into EVERY turn. Storing historical copies
# in a bridge's local contexts.json is pure waste: they get replayed into every
# future prompt. We store only the real message content; the live header always
# arrives fresh with the current turn. The patterns key off app.py's prompt
# format (the `[<MODE> …]` header and the `--- new messages in the room ---`
# delimiter) — if that format changes, update it here (one place).
AGENT_CHAT_HEADER_RE = re.compile(
    r"^\[(?:1:1 CHAT MODE|GROUP CHAT MODE|COLLABORATION MODE|New Agent Chat prompt"
    r"|Same (?:group chat|private 1:1 chat) session)"
    r"[^\]]*\].*?(?=\n--- new messages|\Z)",
    re.DOTALL,
)
NEW_MESSAGES_RE = re.compile(r"^--- new messages in the room ---\s*", re.MULTILINE)


def strip_system_header(message: str) -> str:
    """Drop the repetitive Agent Chat room-state header from a stored message,
    keeping only the real conversational content. The current turn still
    receives the full live header — only the stored-for-replay copy is trimmed.
    """
    # Preferred: keep only what follows the "--- new messages ---" delimiter,
    # i.e. the actual room messages for this turn.
    parts = NEW_MESSAGES_RE.split(message, maxsplit=1)
    if len(parts) == 2:
        return parts[1].strip()
    # No delimiter (e.g. a context-only ping): drop a leading bracketed header
    # block if present. If that consumes the whole message it was pure header,
    # so "" is returned and nothing meaningful is stored.
    return AGENT_CHAT_HEADER_RE.sub("", message).strip()


# --- Context store I/O (atomic JSON file per bridge) ------------------------
def load_contexts(store: Path) -> dict:
    try:
        if store.is_file():
            data = json.loads(store.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_contexts(store: Path, contexts: dict) -> None:
    # Community uninstall/reinstall preserves the owner's configuration and
    # chats but intentionally removes the installed app tree. Bridge context
    # folders live under that tree, so the first real turn after reinstall must
    # be able to recreate its private state directory from scratch.
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_suffix(".tmp")
    tmp.write_text(json.dumps(contexts, indent=2))
    tmp.replace(store)


def get_or_create_context(store: Path, context_id: Optional[str]) -> tuple[str, dict]:
    contexts = load_contexts(store)
    if context_id and context_id in contexts:
        return context_id, contexts[context_id]
    # Honor a caller-supplied id (the orchestrator pre-seeds one so the live
    # thinking line works from the very first turn) when it's a safe slug;
    # otherwise mint our own.
    if context_id and re.fullmatch(r"[A-Za-z0-9]{6,64}", context_id):
        new_id = context_id
    else:
        new_id = uuid.uuid4().hex[:12]
    ctx = {"messages": [], "created_ts": int(time.time() * 1000)}
    contexts[new_id] = ctx
    save_contexts(store, contexts)
    return new_id, ctx


# --- Auth -------------------------------------------------------------------
def authorized(request, token: str) -> bool:
    """True iff the request carries the bridge's x-api-key. No token => closed."""
    if not token:
        return False
    return request.headers.get("x-api-key", "") == token


# --- Live "current step" progress tracking ----------------------------------
# Each bridge owns its own PROGRESS dict (in-memory, single uvicorn worker) and
# passes it in; the bookkeeping is identical, so it lives here.
def set_progress(progress: dict, context_id: str, text: str, active: bool = True,
                 *, keep_partial: Optional[bool] = None) -> None:
    if not context_id:
        return
    text = " ".join(str(text).split())
    if not text:
        return
    prior = progress.get(context_id) or {}
    # Mid-turn tool/step updates must keep the growing answer. A new turn
    # (previous one already inactive, or an explicit keep_partial=False) must
    # not republish last turn's text as if this turn had already started writing.
    if keep_partial is None:
        keep_partial = bool(prior.get("active"))
    progress[context_id] = {
        "text": text,
        "active": active,
        "ts": int(time.time() * 1000),
    }
    if keep_partial and prior.get("partial_response"):
        progress[context_id]["partial_response"] = prior["partial_response"]
    if len(progress) > 512:
        for key, _ in sorted(progress.items(), key=lambda kv: kv[1].get("ts", 0))[:256]:
            progress.pop(key, None)


def end_progress(progress: dict, context_id: str) -> None:
    if context_id and context_id in progress:
        progress[context_id]["active"] = False
        progress[context_id]["partial_response"] = ""


def set_partial_response(progress: dict, context_id: str, text: str) -> None:
    """Expose the safely accumulated answer text alongside a normal progress line.

    The orchestrator polls this same in-process status endpoint while the original
    /api/api_message request remains pending. Keeping this optional and separate
    from ``text`` preserves existing progress UI/narration behavior, while a voice
    client can begin synthesizing only completed sentences before the final reply
    is durable.
    """
    if not context_id:
        return
    text = str(text or "").strip()
    if not text:
        return
    entry = progress.setdefault(context_id, {
        "text": "Working…",
        "active": True,
        "ts": int(time.time() * 1000),
    })
    entry["partial_response"] = text[:12000]
    entry["ts"] = int(time.time() * 1000)


def clip(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --- Prompt history assembly ------------------------------------------------
def format_history_lines(messages: list) -> list:
    """Render stored turns as ['role: text', …], skipping empties. Both bridges
    use this for the `[Bridge-local recent context]` block; how each frames the
    surrounding prompt (preamble, char budget) stays bridge-specific."""
    lines = []
    for item in messages:
        role = item.get("role", "message")
        text = str(item.get("text", "")).strip()
        if text:
            lines.append(f"{role}: {text}")
    return lines


# --- Honest model resolution -------------------------------------------------
def model_fallback_meta(requested: object, resolved: str) -> dict:
    """Reply keys that admit the caller's model pick was NOT honoured; {} when it
    was (or when the caller didn't name one).

    Every bridge coerces an unknown model to its own default rather than 500ing,
    which is right — a stale UI must not kill the room — but the coercion used to
    be completely silent. A bridge process older than the app's model list then
    ran every turn on its default while the settings card, the reply chip, and
    the agent's own self-description all reported something else. The app copies
    these keys into the turn metadata, so a dropped pick is visible on the turn
    that dropped it."""
    asked = str(requested or "").strip()
    if not asked or asked == resolved:
        return {}
    return {"model_requested": asked, "model_fallback": True}


# --- Rolling summarization policy -------------------------------------------
async def maybe_summarize(ctx: dict, summarize_fn, *, trigger: int, keep: int,
                          hard_cap: int, max_chars: int) -> None:
    """When a context outgrows the window, compact the overflow into
    ctx['summary']. Fail-open: on any error, hard-cap the message list instead
    (never raises). `summarize_fn(old_summary, messages) -> str` is the
    model-specific call (Claude vs Codex CLI)."""
    msgs = ctx.get("messages") or []
    if len(msgs) <= trigger:
        return
    overflow = msgs[:-keep]
    recent = msgs[-keep:]
    try:
        new_summary = await summarize_fn(str(ctx.get("summary") or ""), overflow)
        if new_summary.strip():
            ctx["summary"] = new_summary.strip()[:max_chars]
            ctx["messages"] = recent
            return
    except Exception:
        pass
    if len(msgs) > hard_cap:
        ctx["messages"] = msgs[-hard_cap:]
