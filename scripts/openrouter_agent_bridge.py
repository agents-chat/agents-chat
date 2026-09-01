"""OpenRouter API bridge for Agent Chat.

The upstream OpenRouter credential is loaded only by this loopback bridge.  Agent
Chat itself authenticates with a separate random inbound token, so the provider
key never enters browser state, chat prompts, or the agent registry.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import sys as _sys

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
import _bridge_common as common


APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent.parent
STATE_DIR = APP_DIR / "openrouter_bridge"
CONTEXT_STORE = STATE_DIR / "contexts.json"

_ENV_FILES = (
    APP_DIR / ".env",
    REPO_DIR / ".env",
    APP_DIR / "agents" / "shared.env",
    APP_DIR / "agents" / "openrouter.env",
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openrouter/free"
RETIRED_MODEL_ALIASES = {
    # OpenRouter removed Ox Alpha's final endpoint on its announced retirement
    # date (2026-08-26). Keep accepting stale chat/env settings, but resolve
    # them honestly to the provider's maintained router.
    "stealth/ox-alpha": DEFAULT_MODEL,
}
MODEL_OPTIONS = (
    DEFAULT_MODEL,
    "cohere/north-mini-code:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "liquid/lfm-2.5-2.6b:free",
    "minimax/minimax-m2.7:free",
    "minimax/minimax-m3:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3.5-lightning:free",
    "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free",
    "thinkingmachines/inkling:free",
    "thinkingmachines/inkling-small:free",
    "z-ai/glm-5.2:free",
)
REASONING_OPTIONS = ("default", "max", "high", "low")
MODEL_ID_RE = re.compile(
    r"^(?=.{1,160}$)~?[A-Za-z0-9][A-Za-z0-9._~:+/-]*$"
)
MAX_CONTEXT_MESSAGES = max(
    2, min(100, int(os.environ.get("OPENROUTER_BRIDGE_MAX_CONTEXT_MESSAGES", "30")))
)
MAX_CONTEXT_CHARS = max(
    4_000, min(2_000_000, int(os.environ.get("OPENROUTER_BRIDGE_MAX_CONTEXT_CHARS", "120000")))
)
MAX_CONTEXTS = max(
    10, min(1000, int(os.environ.get("OPENROUTER_BRIDGE_MAX_CONTEXTS", "200")))
)
TIMEOUT_S = max(
    10.0, min(900.0, float(os.environ.get("OPENROUTER_BRIDGE_TIMEOUT_S", "300")))
)

SYSTEM_PROMPT = (
    "You are the OpenRouter teammate inside Agent Chat. Follow the current user "
    "and room instructions, answer directly, and state uncertainty honestly. "
    "This API bridge provides conversation only: never claim you used local files, "
    "apps, a browser, or external tools unless their results are present in the prompt."
)

app = FastAPI(title="OpenRouter Agent Chat Bridge")
PROGRESS: dict[str, dict] = {}
LAST_UPSTREAM: dict[str, Any] = {"status": "never-called", "reason": "", "ts": 0}
CONTEXT_LOCKS: dict[str, asyncio.Lock] = {}


def _refresh_env() -> None:
    common.load_env_files(_ENV_FILES, overwrite=True)


def _bridge_token() -> str:
    _refresh_env()
    return os.environ.get("AGENT_TOKEN_OPENROUTER", "").strip()


def _upstream_key() -> str:
    _refresh_env()
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def _authorized(request: Request) -> bool:
    return common.authorized(request, _bridge_token())


def _context(context_id: Optional[str]) -> tuple[str, dict]:
    return common.get_or_create_context(CONTEXT_STORE, context_id)


def _context_lock(context_id: str) -> asyncio.Lock:
    lock = CONTEXT_LOCKS.get(context_id)
    if lock is None:
        lock = CONTEXT_LOCKS[context_id] = asyncio.Lock()
    return lock


def _save_context(context_id: str, ctx: dict) -> None:
    contexts = common.load_contexts(CONTEXT_STORE)
    ctx["updated_ts"] = int(time.time() * 1000)
    contexts[context_id] = ctx
    if len(contexts) > MAX_CONTEXTS:
        oldest = sorted(
            contexts,
            key=lambda key: int((contexts.get(key) or {}).get("updated_ts") or 0),
        )[: len(contexts) - MAX_CONTEXTS]
        for key in oldest:
            contexts.pop(key, None)
            CONTEXT_LOCKS.pop(key, None)
    common.save_contexts(CONTEXT_STORE, contexts)


def _valid_model(value: object) -> str:
    model = str(value or "").strip()
    model = RETIRED_MODEL_ALIASES.get(model, model)
    if not MODEL_ID_RE.fullmatch(model):
        return DEFAULT_MODEL
    if model != DEFAULT_MODEL and not model.endswith(":free"):
        return DEFAULT_MODEL
    return model


def _options(body: dict) -> tuple[str, str, dict]:
    requested = body.get("model")
    model = _valid_model(requested or os.environ.get("OPENROUTER_BRIDGE_MODEL") or DEFAULT_MODEL)
    reasoning = str(
        body.get("reasoning") or os.environ.get("OPENROUTER_BRIDGE_REASONING") or "default"
    ).strip().lower()
    if reasoning not in REASONING_OPTIONS:
        reasoning = "default"
    return model, reasoning, common.model_fallback_meta(requested, model)


def _trim_messages(messages: list[dict]) -> list[dict]:
    kept: list[dict] = []
    chars = 0
    for item in reversed(messages[-MAX_CONTEXT_MESSAGES:]):
        content = str(item.get("content") or "")
        if kept and chars + len(content) > MAX_CONTEXT_CHARS:
            break
        kept.append({"role": str(item.get("role") or "user"), "content": content})
        chars += len(content)
    return list(reversed(kept))


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("text", "output_text"):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _upstream_error(response: httpx.Response) -> str:
    status = int(response.status_code)
    if status == 401:
        return "OpenRouter rejected the API key."
    if status == 402:
        return "OpenRouter reports that this account needs credits."
    if status == 429:
        return "OpenRouter rate-limited this request."
    detail = ""
    try:
        data = response.json()
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            detail = error
    except Exception:
        detail = ""
    detail = " ".join(detail.split())[:300]
    return detail or f"OpenRouter returned HTTP {status}."


@app.get("/health")
async def health():
    inbound = bool(_bridge_token())
    upstream = bool(_upstream_key())
    status = "ok" if inbound and upstream else "missing-token"
    missing = []
    if not inbound:
        missing.append("local bridge token")
    if not upstream:
        missing.append("OpenRouter API key")
    requested_model = str(
        os.environ.get("OPENROUTER_BRIDGE_MODEL") or DEFAULT_MODEL
    ).strip()
    model = _valid_model(requested_model)
    return {
        "status": status,
        "reason": "" if status == "ok" else f"Missing {' and '.join(missing)}.",
        "runtime": "openrouter-api",
        "model": model,
        "model_options": list(MODEL_OPTIONS),
        "reasoning_options": list(REASONING_OPTIONS),
        "token_present": inbound,
        "upstream_key_present": upstream,
        "last_upstream": dict(LAST_UPSTREAM),
        **common.model_fallback_meta(requested_model, model),
    }


@app.get("/capabilities")
async def capabilities():
    return {
        "summary": "OpenRouter model access with the Free Models Router as the safe default.",
        "best_for": "Long-horizon reasoning, coding analysis, drafting, and model comparison.",
        "strengths": ["reasoning", "coding", "long context", "model choice"],
        "model": _valid_model(os.environ.get("OPENROUTER_BRIDGE_MODEL") or DEFAULT_MODEL),
        "runtime": "openrouter-api",
        "supports": {
            "chat": True,
            "sessions": True,
            "model_picker": True,
            "reasoning_control": True,
            "local_files": False,
            "native_apps": False,
        },
    }


@app.post("/api/api_log_get")
async def api_log_get(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "invalid API key"}, status_code=401)
    body = await request.json()
    context_id = str((body or {}).get("context_id") or "").strip()
    if not context_id:
        return JSONResponse({"error": "context_id is required"}, status_code=400)
    row = PROGRESS.get(context_id) or {
        "text": "Waiting for input", "active": False, "ts": int(time.time() * 1000)
    }
    return {
        "context_id": context_id,
        "log": {
            "progress": row.get("text") or "Waiting for input",
            "progress_active": bool(row.get("active")),
            "items": [],
        },
    }


@app.post("/api/api_message")
async def api_message(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "invalid API key"}, status_code=401)
    api_key = _upstream_key()
    if not api_key:
        return JSONResponse({"error": "OpenRouter API key is not configured"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = str((body or {}).get("message") or "").strip()
    if not text:
        return JSONResponse({"error": "message is required"}, status_code=400)
    context_id, ctx = _context((body or {}).get("context_id"))
    model, reasoning, fallback_meta = _options(body or {})
    async with _context_lock(context_id):
        history = _trim_messages(list(ctx.get("messages") or []))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
        messages.append({"role": "user", "content": text})
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if reasoning != "default":
            payload["reasoning"] = {"effort": reasoning, "exclude": True}
        common.set_progress(PROGRESS, context_id, f"OpenRouter is asking {model}…", True)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": "Agent Chat",
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                response = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            LAST_UPSTREAM.update(status="network-error", reason=str(exc)[:200], ts=int(time.time() * 1000))
            common.end_progress(PROGRESS, context_id)
            return JSONResponse({"error": "OpenRouter could not be reached."}, status_code=502)
        if response.status_code >= 400:
            reason = _upstream_error(response)
            LAST_UPSTREAM.update(status=f"http-{response.status_code}", reason=reason, ts=int(time.time() * 1000))
            common.end_progress(PROGRESS, context_id)
            # Preserve the two provider states Agent Chat handles specially:
            # 402 activates its credit guard and 429 its bounded backoff.  Do
            # not relay upstream 401/403, because those status codes mean the
            # *local* bridge token is stale at the Agent Chat boundary.
            outward_status = response.status_code if response.status_code in {
                402, 408, 409, 425, 429, 500, 502, 503, 504,
            } else 502
            return JSONResponse({"error": reason}, status_code=outward_status)
        try:
            data = response.json()
        except ValueError:
            common.end_progress(PROGRESS, context_id)
            return JSONResponse({"error": "OpenRouter returned invalid JSON."}, status_code=502)
        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        answer = _message_text(message)
        if not answer:
            common.end_progress(PROGRESS, context_id)
            return JSONResponse({"error": "OpenRouter returned no assistant message."}, status_code=502)
        stored_user = common.strip_system_header(text) or text
        ctx["messages"] = _trim_messages([
            *history,
            {"role": "user", "content": stored_user},
            {"role": "assistant", "content": answer},
        ])
        _save_context(context_id, ctx)
        common.end_progress(PROGRESS, context_id)
        LAST_UPSTREAM.update(status="ok", reason="", ts=int(time.time() * 1000))
        used_model = str(data.get("model") or model)
        result: dict[str, Any] = {
            "response": answer,
            "context_id": context_id,
            "model": used_model,
            "reasoning": reasoning,
            "runtime": "openrouter-api",
            **fallback_meta,
        }
        usage = data.get("usage")
        if isinstance(usage, dict):
            result["usage"] = usage
            cost = usage.get("cost")
            if isinstance(cost, (int, float)):
                result["cost_usd"] = cost
        return result
