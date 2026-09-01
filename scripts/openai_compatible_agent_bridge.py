"""Local adapter from Agent Chat's agent contract to an OpenAI-compatible API.

One process is launched per Community connector. Configuration is intentionally
environment-only so upstream credentials stay in the server-side connector store
and never reach the browser.
"""

from __future__ import annotations

import os
import secrets
import time
from collections import OrderedDict

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


INBOUND_TOKEN = os.environ.get("AGENT_ADAPTER_TOKEN", "").strip()
UPSTREAM_URL = os.environ.get("AGENT_ADAPTER_UPSTREAM_URL", "").strip()
UPSTREAM_TOKEN = os.environ.get("AGENT_ADAPTER_UPSTREAM_TOKEN", "").strip()
UPSTREAM_MODEL = os.environ.get("AGENT_ADAPTER_MODEL", "").strip() or "agent"
AGENT_NAME = os.environ.get("AGENT_ADAPTER_NAME", "Connected agent").strip()
MAX_CONTEXTS = 100
MAX_MESSAGES = 40

app = FastAPI(title=f"Agent Chat adapter — {AGENT_NAME}")
_contexts: "OrderedDict[str, list[dict]]" = OrderedDict()
_activity: dict[str, dict] = {}


class AgentMessage(BaseModel):
    message: str
    context_id: str | None = None


class LogRequest(BaseModel):
    context_id: str = ""
    length: int = 1


def _authorize(x_api_key: str | None) -> None:
    if not INBOUND_TOKEN or not x_api_key or not secrets.compare_digest(x_api_key, INBOUND_TOKEN):
        raise HTTPException(status_code=401, detail="invalid API key")


def _context(context_id: str | None) -> tuple[str, list[dict]]:
    cid = (context_id or "").strip() or secrets.token_hex(8)
    if cid not in _contexts:
        _contexts[cid] = []
    _contexts.move_to_end(cid)
    while len(_contexts) > MAX_CONTEXTS:
        old, _ = _contexts.popitem(last=False)
        _activity.pop(old, None)
    return cid, _contexts[cid]


@app.get("/health")
async def health():
    return {
        "status": "ok" if INBOUND_TOKEN and UPSTREAM_URL else "degraded",
        "agent": AGENT_NAME,
        "model": UPSTREAM_MODEL,
    }


@app.get("/capabilities")
async def capabilities():
    return {
        "summary": f"{AGENT_NAME} through an OpenAI-compatible local connection.",
        "best_for": "General-purpose agent work using the connected runtime and its tools.",
        "model": UPSTREAM_MODEL,
        "supports": {"chat": True, "sessions": True},
    }


@app.post("/api/api_log_get")
async def log_get(body: LogRequest, x_api_key: str | None = Header(default=None)):
    _authorize(x_api_key)
    cid = body.context_id.strip()
    if not cid:
        raise HTTPException(status_code=400, detail="context_id is required")
    state = _activity.get(cid) or {"progress": "Waiting for input", "active": False}
    return {
        "context_id": cid,
        "log": {
            "progress": state["progress"],
            "progress_active": bool(state["active"]),
            "items": [],
        },
    }


@app.post("/api/api_message")
async def api_message(body: AgentMessage, x_api_key: str | None = Header(default=None)):
    _authorize(x_api_key)
    if not UPSTREAM_URL:
        raise HTTPException(status_code=503, detail="upstream URL is not configured")
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is required")
    cid, history = _context(body.context_id)
    history.append({"role": "user", "content": text})
    del history[:-MAX_MESSAGES]
    _activity[cid] = {"progress": f"{AGENT_NAME} is working…", "active": True, "ts": time.time()}
    headers = {"Content-Type": "application/json"}
    if UPSTREAM_TOKEN:
        headers["Authorization"] = f"Bearer {UPSTREAM_TOKEN}"
    payload = {"model": UPSTREAM_MODEL, "messages": history, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(UPSTREAM_URL, headers=headers, json=payload)
        if response.status_code >= 400:
            detail = response.text.strip()[:500] or f"upstream HTTP {response.status_code}"
            raise HTTPException(status_code=502, detail=detail)
        data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        answer = message.get("content") if isinstance(message, dict) else None
        if not isinstance(answer, str) or not answer.strip():
            raise HTTPException(status_code=502, detail="upstream returned no assistant message")
        answer = answer.strip()
        history.append({"role": "assistant", "content": answer})
        del history[:-MAX_MESSAGES]
        _activity[cid] = {"progress": "Waiting for input", "active": False, "ts": time.time()}
        result = {"response": answer, "context_id": cid, "model": data.get("model") or UPSTREAM_MODEL}
        if isinstance(data.get("usage"), dict):
            result["usage"] = data["usage"]
        return result
    except HTTPException:
        _activity[cid] = {"progress": "Connection needs attention", "active": False, "ts": time.time()}
        raise
    except (httpx.HTTPError, ValueError) as exc:
        _activity[cid] = {"progress": "Connection needs attention", "active": False, "ts": time.time()}
        raise HTTPException(status_code=502, detail=str(exc)[:500]) from exc
