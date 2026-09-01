import json
import os
import time
import uuid
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
# This is a Docker-only sidecar reached by sibling containers over the private
# Compose network (the compose files use ``expose``, not a published host port).
HOST = os.environ.get("OLLAMA_THINKLESS_HOST", "0.0.0.0")  # nosec B104
PORT = int(os.environ.get("OLLAMA_THINKLESS_PORT", "11435"))
REQUEST_TIMEOUT_S = float(os.environ.get("OLLAMA_THINKLESS_TIMEOUT_S", "900"))
# Context window the upstream model is loaded with. Agent prompts (system + tools +
# history) are large; if they fill the window there is no room left to generate, so
# the model emits a 1-2 token fragment then stops (done_reason=length) -- which looks
# like gibberish ("II", "PleaseII"). Keep this comfortably above the largest prompt
# Hermes sends. gemma4 supports up to 131072.
NUM_CTX = int(os.environ.get("OLLAMA_PROXY_NUM_CTX", "32768"))
# Keep the model resident between turns so each message doesn't reload GBs from disk.
KEEP_ALIVE = os.environ.get("OLLAMA_PROXY_KEEP_ALIVE", "30m")

app = FastAPI(title="Agent Chat Ollama Thinkless Proxy")


def _now() -> int:
    return int(time.time())


def _flatten_content(content: Any) -> str:
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
    return "" if content is None else str(content)


def _messages_for_ollama(messages: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return out
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        converted: dict[str, Any] = {
            "role": role,
            "content": _flatten_content(message.get("content")),
        }
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            converted["tool_calls"] = tool_calls
        if role == "tool" and message.get("tool_call_id"):
            converted["tool_call_id"] = message.get("tool_call_id")
        out.append(converted)
    return out


def _options_from_openai(body: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    token_cap = body.get("max_tokens") or body.get("max_completion_tokens")
    if isinstance(token_cap, int) and token_cap > 0:
        options["num_predict"] = token_cap
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("seed", "seed"),
        ("frequency_penalty", "frequency_penalty"),
        ("presence_penalty", "presence_penalty"),
    ):
        value = body.get(source)
        if isinstance(value, (int, float)):
            options[target] = value
    stop = body.get("stop")
    if isinstance(stop, str):
        options["stop"] = [stop]
    elif isinstance(stop, list):
        options["stop"] = [str(item) for item in stop]
    return options


def _ollama_payload(body: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        # This service is a provider adapter: callers send their provider-facing
        # label (for example MiniMax-M3), but Ollama needs the configured local
        # model name. Never let that external label override the fallback model.
        "model": str(os.environ.get("OLLAMA_PROXY_MODEL") or body.get("model") or "gemma4:12b"),
        "messages": _messages_for_ollama(body.get("messages")),
        "stream": stream,
        "think": False,
        "keep_alive": KEEP_ALIVE,
    }
    options = _options_from_openai(body)
    options.setdefault("num_ctx", NUM_CTX)
    payload["options"] = options
    for key in ("tools", "tool_choice", "format"):
        value = body.get(key)
        if value is not None:
            payload[key] = value
    return payload


def _usage(data: dict[str, Any]) -> dict[str, int]:
    prompt = int(data.get("prompt_eval_count") or 0)
    completion = int(data.get("eval_count") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
    return {
        "status": "ok" if resp.status_code == 200 else "error",
        "ollama_base_url": OLLAMA_BASE_URL,
        "ollama_status": resp.status_code,
        "think": False,
    }


@app.get("/v1/models")
async def models() -> JSONResponse:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
    if resp.status_code >= 400:
        return JSONResponse({"error": resp.text}, status_code=resp.status_code)
    tags = resp.json().get("models", [])
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": item.get("name"),
                    "object": "model",
                    "created": _now(),
                    "owned_by": "ollama",
                }
                for item in tags
                if isinstance(item, dict) and item.get("name")
            ],
        }
    )


@app.post("/api/show")
async def api_show(request: Request) -> JSONResponse:
    body = await request.json()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{OLLAMA_BASE_URL}/api/show", json=body)
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.get("/api/tags")
async def api_tags() -> JSONResponse:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    payload = _ollama_payload(body, stream=stream)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = _now()

    if not stream:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            resp = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        if resp.status_code >= 400:
            return JSONResponse({"error": resp.text}, status_code=resp.status_code)
        data = resp.json()
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        response_message: dict[str, Any] = {"role": "assistant", "content": content}
        if isinstance(message.get("tool_calls"), list):
            response_message["tool_calls"] = message["tool_calls"]
        finish_reason = "stop" if data.get("done_reason") in (None, "stop") else data.get("done_reason")
        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": response_message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": _usage(data),
            }
        )

    async def event_stream():
        first = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(first)}\n\n"
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            async with client.stream("POST", f"{OLLAMA_BASE_URL}/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    text = await resp.aread()
                    err = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": payload["model"],
                        "choices": [{"index": 0, "delta": {"content": text.decode(errors="replace")}, "finish_reason": "error"}],
                    }
                    yield f"data: {json.dumps(err)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    message = data.get("message") if isinstance(data.get("message"), dict) else {}
                    content = message.get("content") if isinstance(message.get("content"), str) else ""
                    if content:
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": payload["model"],
                            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    if data.get("done"):
                        finish_reason = "stop" if data.get("done_reason") in (None, "stop") else data.get("done_reason")
                        done_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": payload["model"],
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                        }
                        yield f"data: {json.dumps(done_chunk)}\n\n"
                        break
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level=os.environ.get("UVICORN_LOG_LEVEL", "info"))
