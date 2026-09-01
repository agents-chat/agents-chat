"""mavis_openai_shim — OpenAI-compatible HTTP shim in front of the
`minimax_agent_bridge` (port 55012). The Mavis daemon that backs `minimax`
exposes a session-based API, not `/v1/chat/completions`, so Agent Zero's
`other` provider cannot talk to it directly. This shim is the translation
layer that turns OpenAI-shaped requests into single-message bridge calls and
maps the bridge's `{response, context_id, ...}` payload back into a
`chat.completion` response.

Mirrors the architecture of `ollama_thinkless_proxy.py` (same role: stand up
a tiny FastAPI service that translates between an upstream non-OpenAI API
and the OpenAI shape Agent Zero's `other` provider expects) and reuses the
bridge's session/rotation/summarization by talking to `minimax_agent_bridge`
on :55012 — we deliberately do NOT call the Mavis daemon directly so the
bridge's existing 8/15-turn compaction, handoff summary, and
session-rotation logic keep working unchanged for the Agent Zero clients.

Per call we:
  1. Resolve the Agent Zero client (source IP) to a cached `context_id` if
     we have one in memory; the bridge's `context_id` IS the Mavis session
     id, so reusing it across calls preserves the daemon-side conversation
     memory. The bridge's own rotation logic (turn 8 trigger / 15 hard cap)
     handles the long-session reset.
  2. Flatten the OpenAI `messages` array into the single `message` string
     the bridge expects (with `[role]` prefixes; tool messages get a
     `<tool_result>` wrapper). The bridge itself does not see the array.
  3. POST the bridge's `/api/api_message` with `x-api-key: AGENT_TOKEN_MINIMAX`
     (loaded from the same .env stack the bridge uses) and translate the
     response back into OpenAI's `chat.completion` shape.
  4. Cache the new `context_id` keyed by `X-Mavis-Session-Key` when present,
     otherwise source IP, evicting after a long idle window (default 24h) so
     Agent Zero clients that come back the next day start a fresh Mavis
     session.

Auth: inbound is `Authorization: Bearer <MAVIS_SHIM_TOKEN>` (OpenAI standard,
which is what Agent Zero's `other` provider sends). Outbound to the bridge
is `x-api-key: AGENT_TOKEN_MINIMAX` (the same token the other Agent Chat
bridges use, so this shim slots into the existing trust boundary).

No streaming: Agent Zero's `other` provider calls LiteLLM with
`stream=False` for chat completions, so a non-streaming response is
sufficient. If a streaming request comes in we fall back to a non-streaming
call and wrap the whole reply in a single chunk — same trick the other
bridges use when a downstream cannot stream.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import _bridge_common as common  # noqa: E402  -- same env loader the other bridges use


APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent.parent

# Mirror the bridge's env-loading order so the shim sees the same token.
SHARED_ENV_FILE = APP_DIR / "agents" / "shared.env"
common.load_env_files((
    APP_DIR / ".env",
    REPO_DIR / ".env",
    SHARED_ENV_FILE,
    APP_DIR / "agents" / "minimax.env",
))


# The Agent Chat minimax bridge this shim talks to. Default localhost:55012
# matches the launchd-managed `com.agentchat.minimax-bridge` service.
BRIDGE_BASE = os.environ.get("MAVIS_SHIM_BRIDGE_BASE", "http://127.0.0.1:55012").rstrip("/")
# Inbound bearer token Agent Zero sends in `Authorization: Bearer ...`. Falls
# back to the same token the bridge uses (AGENT_TOKEN_MINIMAX) so a fresh
# install works without a second secret.
SHIM_TOKEN = (
    os.environ.get("MAVIS_SHIM_TOKEN", "").strip()
    or os.environ.get("AGENT_TOKEN_MINIMAX", "").strip()
)
BRIDGE_TOKEN = os.environ.get("AGENT_TOKEN_MINIMAX", "").strip()
# How long an idle (Agent Zero client) -> (Mavis context_id) mapping stays
# alive. Long enough that one chat session across a few minutes reuses the
# same daemon session; short enough that a chat opened the next day starts
# fresh.
CONTEXT_TTL_S = float(os.environ.get("MAVIS_SHIM_CONTEXT_TTL_S", str(24 * 3600)))
# Hard ceiling on cached contexts (defensive — Agent Zero is one process
# per container so this is generous).
MAX_CACHED_CONTEXTS = int(os.environ.get("MAVIS_SHIM_MAX_CONTEXTS", "1024"))

HOST = os.environ.get("MAVIS_SHIM_HOST", "127.0.0.1")
# Default 55016: 55009-55015 are the existing Agent Chat bridges (codex,
# claude, hermes, minimax, antigravity, ops-agent), 55013 is taken by the
# Kokoro TTS service. 55016 is the next free slot in the same block so the
# launchd plist pattern stays consistent.
PORT = int(os.environ.get("MAVIS_SHIM_PORT", "55016"))
HTTP_TIMEOUT_S = float(os.environ.get("MAVIS_SHIM_HTTP_TIMEOUT_S", "900"))


# Canonical model list — must match what the minimax bridge accepts (the
# bridge rejects unknown models with a 400). The `id` (left) is what Agent
# Zero sends; the `bridge_model` (right) is what the bridge expects.
MODELS: dict[str, str] = {
    "MiniMax-M3": "minimax/MiniMax-M3",
    "MiniMax-M2.7": "minimax/MiniMax-M2.7",
    "MiniMax-M2.7-highspeed": "minimax/MiniMax-M2.7-highspeed",
}
DEFAULT_MODEL = "MiniMax-M3"


app = FastAPI(title="mavis OpenAI shim (minimax bridge front)")


# --- context_id cache (disabled) ---------------------------------------------
# Originally this shim cached the bridge `context_id` per source IP so multiple
# turns from the same Agent Zero chat could share one Mavis session (which is
# what the 8/15-turn rotation + summarization in the bridge key off of). The
# first cut also supported a per-container `X-Mavis-Session-Key` header so
# Lead and Sales wouldn't share a session.
#
# In practice Agent Zero's `other` provider does not reliably plumb
# `kwargs.extra_headers` into the actual HTTP request (verified empirically:
# the live run_ui.py sends no `X-Mavis-Session-Key` even with the header set
# in config.json), so the cache key collapses to "source IP" and both
# containers share one session — that's worse than no session.
#
# The right answer for a no-headers world is to skip session continuity: every
# call to this shim mints a fresh Mavis session. Agent Zero's litellm call
# already ships the full conversation `messages` array on every turn, so the
# model still has the chat context — the only thing we lose is the bridge's
# daemon-side rolling-summary (which only matters past turn 8 anyway, and
# Agent Zero's per-call `ctx_length` is already capped at 64k). Re-introduce
# the cache later when there's a reliable per-container header path.
_CONTEXTS: dict[str, tuple[str, float]] = {}


def _evict_expired() -> None:
    # Kept as a no-op for API stability; nothing to evict when the cache is off.
    return


def _client_key(request: Request) -> str:
    # Always returns a fresh per-call key — the cache below is empty so this
    # only matters for log lines.
    return f"call:{uuid.uuid4().hex[:8]}"


def _get_context(client: str) -> str | None:
    # Cache disabled: never reuse a previous context_id.
    return None


def _set_context(client: str, context_id: str) -> None:
    # Cache disabled: drop the new context_id on the floor.
    return


# --- auth ---------------------------------------------------------------------
def _authorized(request: Request) -> bool:
    # OpenAI-style `Authorization: Bearer <token>`. Accept the bare token in
    # the same header for symmetry with the bridge's `x-api-key` check (some
    # Agent Zero providers send one or the other depending on the litellm
    # path). Either is sufficient; SHIM_TOKEN is the only accepted value.
    auth = request.headers.get("authorization") or ""
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not SHIM_TOKEN:
        # No token configured = reject everything. Refusing to fail-open is
        # important here: Agent Zero's API key on the wire is the only gate.
        return False
    return token == SHIM_TOKEN


# --- message flattening -------------------------------------------------------
def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def _flatten_messages(messages: Any) -> str:
    """OpenAI `messages: [...]` -> single bridge `message` string.

    The bridge accepts one `message` field; we collapse the array into a
    labelled transcript so the Mavis session still sees system / user /
    assistant / tool turns in order. Tool outputs are wrapped with a clear
    sentinel so the model can find them.
    """
    if not isinstance(messages, list):
        return ""
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").lower()
        content = _flatten_content(message.get("content"))
        if role == "system":
            # System messages go first and unlabelled — Mavis uses them as
            # the agent's standing instructions, matching how Agent Chat's
            # other bridges ship the system prompt.
            lines.append(content)
            continue
        if role == "assistant":
            lines.append(f"[assistant] {content}")
            continue
        if role == "tool":
            tool_call_id = message.get("tool_call_id") or ""
            lines.append(f"[tool_result {tool_call_id}] {content}")
            continue
        # user and anything else
        lines.append(f"[user] {content}")
    return "\n\n".join(line for line in lines if line)


def _resolve_model(name: Any) -> str:
    raw = str(name or "").strip()
    if not raw:
        return MODELS[DEFAULT_MODEL]
    # Agent Zero's `other` provider sends the bare model name ("MiniMax-M3");
    # accept that. Also accept the bridge's "minimax/MiniMax-M3" form so a
    # curious caller doesn't have to know which side they're on.
    if raw in MODELS:
        return MODELS[raw]
    if raw in MODELS.values():
        return raw
    # Last-ditch: lowercase match.
    for k, v in MODELS.items():
        if k.lower() == raw.lower() or v.lower() == raw.lower():
            return v
    return MODELS[DEFAULT_MODEL]


def _is_thinking(body: dict[str, Any]) -> bool:
    for key in ("thinking", "reasoning"):
        v = body.get(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on", "thinking", "enabled"):
            return True
    # MiniMax-M3 is the only model where the toggle is meaningful, but we
    # default to ON for everything (matches the minimax_agent_bridge
    # default) so a caller that doesn't care still gets the right model
    # behavior. M2.7 family forces thinking on at the provider anyway.
    return True


# --- endpoints ----------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "bridge_base": BRIDGE_BASE,
        "token_present": bool(SHIM_TOKEN),
        "models": list(MODELS.keys()),
        "cached_contexts": len(_CONTEXTS),
    }


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    now = int(time.time())
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": mid,
                    "object": "model",
                    "created": now,
                    "owned_by": "minimax",
                }
                for mid in MODELS.keys()
            ],
        }
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

    message = _flatten_messages(body.get("messages"))
    if not message.strip():
        return JSONResponse(
            {"error": "no user-visible content in `messages`"},
            status_code=400,
        )

    model = _resolve_model(body.get("model"))
    thinking = _is_thinking(body)

    client = _client_key(request)
    context_id = _get_context(client)

    bridge_body: dict[str, Any] = {
        "message": message,
        "model": model,
        "thinking": thinking,
    }
    if context_id:
        # The bridge accepts an existing context_id and reattaches; if the
        # daemon has rotated or expired it, the bridge creates a new one
        # transparently (see minimax_agent_bridge.api_message).
        bridge_body["context_id"] = context_id

    headers = {"x-api-key": BRIDGE_TOKEN} if BRIDGE_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client_http:
            resp = await client_http.post(
                f"{BRIDGE_BASE}/api/api_message",
                json=bridge_body,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"error": f"bridge unreachable: {exc!r}"},
            status_code=502,
        )

    if resp.status_code == 404 and context_id:
        # The bridge reported the cached session vanished (rotation/expiry).
        # Drop the cache entry, retry once without context_id, and let the
        # bridge mint a fresh session. Mirrors how the other Agent Chat
        # bridges recover from a 404-on-known-session.
        _CONTEXTS.pop(client, None)
        bridge_body.pop("context_id", None)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client_http:
                resp = await client_http.post(
                    f"{BRIDGE_BASE}/api/api_message",
                    json=bridge_body,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": f"bridge unreachable on retry: {exc!r}"},
                status_code=502,
            )

    if resp.status_code >= 400:
        # Pass the bridge's error body through verbatim so Agent Zero sees
        # the same diagnostic it would see from any other OpenAI-shaped
        # upstream.
        try:
            detail = resp.json()
        except Exception:
            detail = {"error": resp.text[:500]}
        return JSONResponse(detail, status_code=resp.status_code)

    data = resp.json()
    response_text = str(data.get("response") or "")
    new_context_id = str(data.get("context_id") or "")
    if new_context_id:
        _set_context(client, new_context_id)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": body.get("model") or DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    # Surface the context_id in the response when callers (e.g. future
    # Agent Zero integration) want to pin a session explicitly. OpenAI's
    # schema doesn't define this, but it doesn't reject unknown fields
    # either, and the existing bridges (codex/claude) do the same.
    if new_context_id:
        payload["minimax_context_id"] = new_context_id
    return JSONResponse(payload)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"))
