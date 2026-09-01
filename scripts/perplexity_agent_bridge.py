"""Subscription-backed Perplexity desktop bridge for Agent Chat.

Perplexity's signed daemon rejects third-party XPC clients, and the desktop app
does not publish an inbound CLI. This bridge therefore uses the app's visible
macOS Accessibility contract: it opens or resumes a task, submits the request
through the normal Computer composer, and reads the rendered response. It never
copies Perplexity cookies, bearer tokens, Keychain items, or private cache data.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import sys as _sys

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)
import _bridge_common as common


APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent.parent
STATE_DIR = APP_DIR / "perplexity_bridge"
CONTEXT_STORE = STATE_DIR / "contexts.json"
RUNTIME_DRIVER = (
    STATE_DIR
    / "Agent Chat Perplexity Driver.app"
    / "Contents"
    / "MacOS"
    / "perplexity_desktop_driver"
)
BUNDLED_DRIVER = (
    APP_DIR
    / "assets"
    / "macos"
    / "Agent Chat Perplexity Driver.app"
    / "Contents"
    / "MacOS"
    / "perplexity_desktop_driver.bin"
)

_ENV_FILES = (
    APP_DIR / ".env",
    REPO_DIR / ".env",
    APP_DIR / "agents" / "shared.env",
    APP_DIR / "agents" / "perplexity.env",
)

TIMEOUT_S = float(os.environ.get("PERPLEXITY_BRIDGE_TIMEOUT_S", "900"))
MAX_CONTEXT_MESSAGES = int(
    os.environ.get("PERPLEXITY_BRIDGE_MAX_CONTEXT_MESSAGES", "12")
)
MAX_TRANSCRIPT_CHARS = int(
    os.environ.get("PERPLEXITY_BRIDGE_MAX_TRANSCRIPT_CHARS", "24000")
)

app = FastAPI(title="Perplexity Desktop Agent Chat Bridge")
PROGRESS: dict[str, dict] = {}
LAST_UPSTREAM: dict[str, Any] = {
    "status": "never-called",
    "reason": "",
    "ts": 0,
}
DESKTOP_LOCK = asyncio.Lock()


class PerplexityDesktopError(RuntimeError):
    def __init__(self, detail: str, *, http_status: int, retryable: bool) -> None:
        super().__init__(detail)
        self.http_status = int(http_status)
        self.retryable = bool(retryable)


def _refresh_env() -> None:
    common.load_env_files(_ENV_FILES, overwrite=True)


def _bridge_token() -> str:
    _refresh_env()
    return os.environ.get("AGENT_TOKEN_PERPLEXITY", "").strip()


def _driver_path() -> Path:
    _refresh_env()
    configured = os.environ.get("PERPLEXITY_DESKTOP_DRIVER", "").strip()
    if configured:
        return Path(configured).expanduser()
    # Developer installs keep the locally built helper in the ignored runtime
    # directory. Community releases carry a prebuilt universal helper so the
    # owner never needs Xcode or Terminal just to connect Perplexity.
    return RUNTIME_DRIVER if RUNTIME_DRIVER.is_file() else BUNDLED_DRIVER


def _authorized(request: Request) -> bool:
    return common.authorized(request, _bridge_token())


def _load_contexts() -> dict:
    return common.load_contexts(CONTEXT_STORE)


def _save_contexts(contexts: dict) -> None:
    common.save_contexts(CONTEXT_STORE, contexts)


def _context(context_id: Optional[str]) -> tuple[str, dict]:
    return common.get_or_create_context(CONTEXT_STORE, context_id)


def _set_progress(context_id: str, text: str, active: bool = True) -> None:
    common.set_progress(PROGRESS, context_id, text, active)


def _end_progress(context_id: str) -> None:
    common.end_progress(PROGRESS, context_id)


_GENERATED_SUFFIX_RE = re.compile(
    r"(?m)^\s*(?:"
    r"--- recent attachments still available in this chat ---|"
    r"--- how to read attachments ---|"
    r"--- possibly relevant earlier messages \(retrieved from history\) ---|"
    r"--- tools \(pre-authorized:|"
    r"--- credentials & escape hatches ---|"
    r"--- APIs selected for this skill ---|"
    r"\[FINAL RESPONSE FORMAT\b"
    r")",
    re.IGNORECASE,
)


def _clean_message(text: str) -> str:
    """Keep the real room transcript, not Agent Chat-only capability scaffolding.

    The desktop Computer task has its own tools and approval model.  Sending it
    generated curl endpoints, credential inventories, or response-format
    contracts makes those app-internal notes look like fabricated user content.
    Inline attachment lines stay inside ``--- new messages`` and therefore remain
    available; only the first known generated suffix and everything after it is
    removed.
    """
    cleaned = common.strip_system_header(str(text or "")).strip()
    match = _GENERATED_SUFFIX_RE.search(cleaned)
    return cleaned[: match.start()].rstrip() if match else cleaned


def _workspace_from_body(body: dict) -> str:
    """Return one existing absolute workspace folder, or ``""`` when omitted.

    Agent Chat validates its chat binding before it reaches this bridge, but the
    loopback endpoint is also a boundary of its own.  Never create a caller-
    supplied directory here (``resolve_workspace`` intentionally does that for
    CLI bridges), and never let a relative/control-character path reach the
    desktop chooser or the model prompt.
    """
    if "workspace" not in body or body.get("workspace") in (None, ""):
        return ""
    raw = body.get("workspace")
    if not isinstance(raw, str):
        raise PerplexityDesktopError(
            "workspace must be an existing absolute folder",
            http_status=400,
            retryable=False,
        )
    raw = raw.strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise PerplexityDesktopError(
            "workspace must be an existing absolute folder",
            http_status=400,
            retryable=False,
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise PerplexityDesktopError(
            "workspace must be an existing absolute folder",
            http_status=400,
            retryable=False,
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PerplexityDesktopError(
            "workspace folder does not exist",
            http_status=400,
            retryable=False,
        ) from exc
    if not resolved.is_dir():
        raise PerplexityDesktopError(
            "workspace must be an existing absolute folder",
            http_status=400,
            retryable=False,
        )
    return str(resolved)


def _workspace_prompt(message: str, workspace: str) -> str:
    cleaned = _clean_message(message)
    if not workspace:
        return cleaned
    # JSON quoting keeps unusual-but-valid path characters on one unambiguous
    # line.  The driver confirms the same raw path in Perplexity's folder picker
    # before this text is ever submitted.
    shown = json.dumps(workspace, ensure_ascii=False)
    return (
        "[Agent Chat workspace folder]\n"
        f"The exact folder selected for this request is {shown}. "
        "Treat its local files as read/review-only unless the user's current "
        "request explicitly asks you to create, change, move, or delete them. "
        "Work only within that selected folder when local files are needed. Do "
        "not claim you read a file unless it was actually available through the "
        "selected folder.\n\n"
        + cleaned
    )


def _fresh_task_prompt(
    context_id: str,
    history: list[dict],
    current: str,
    workspace: str = "",
) -> str:
    transcript: list[str] = []
    for item in history[-MAX_CONTEXT_MESSAGES:]:
        role = str(item.get("role") or "").strip().lower()
        text = _clean_message(item.get("text") or "")
        if role in ("user", "assistant") and text:
            transcript.append(f"{role.upper()}: {text}")
    transcript.append(f"USER: {_workspace_prompt(current, workspace)}")
    joined = "\n\n".join(transcript)
    if len(joined) > MAX_TRANSCRIPT_CHARS:
        joined = joined[-MAX_TRANSCRIPT_CHARS:]
    marker = context_id.replace("-", "")[:10]
    return (
        f"Agent Chat task AC-{marker}.\n\n"
        "You are Perplexity Computer working as an agent inside Agent Chat. "
        "Use your normal signed-in desktop capabilities, including web research, "
        "connected services, approved local folders, and supported Mac apps when "
        "the request calls for them. Respect every on-device approval gate. Never "
        "claim an action succeeded unless it visibly did; if approval or user input "
        "is needed, ask clearly and stop at that boundary. Return the useful result "
        "in full, preserving source links when available. Start the final response "
        "with `BOTTOM LINE:` followed by one concise outcome sentence.\n\n"
        "Conversation transcript:\n\n"
        + joined
    )


def _driver_json(command: list[str], *, timeout: float) -> dict[str, Any]:
    driver = _driver_path()
    if not driver.is_file():
        raise PerplexityDesktopError(
            "The Agent Chat Perplexity desktop driver has not been built.",
            http_status=503,
            retryable=False,
        )
    app_bundle = driver.parents[2]
    if app_bundle.suffix != ".app" or not app_bundle.is_dir():
        raise PerplexityDesktopError(
            "The Perplexity desktop driver must be installed as a macOS app bundle.",
            http_status=503,
            retryable=False,
        )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    result_fd, result_name = tempfile.mkstemp(
        prefix="driver-result-", suffix=".json", dir=STATE_DIR
    )
    os.close(result_fd)
    result_path = Path(result_name)
    result_path.unlink(missing_ok=True)
    try:
        try:
            completed = subprocess.run(
                [
                    "/usr/bin/open",
                    "-n",
                    "-W",
                    str(app_bundle),
                    "--args",
                    *command,
                    "--result-file",
                    str(result_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
        except subprocess.TimeoutExpired as exc:
            raise PerplexityDesktopError(
                "Perplexity desktop timed out before returning a result.",
                http_status=504,
                retryable=True,
            ) from exc
        if not result_path.is_file():
            detail = completed.stderr.strip()[:500] or "The desktop driver returned no status."
            raise PerplexityDesktopError(detail, http_status=502, retryable=True)
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PerplexityDesktopError(
                "The desktop driver returned an unreadable status.",
                http_status=502,
                retryable=True,
            ) from exc
        return payload if isinstance(payload, dict) else {}
    finally:
        result_path.unlink(missing_ok=True)


def _driver_health() -> dict[str, Any]:
    try:
        return _driver_json(["--health"], timeout=10)
    except PerplexityDesktopError as exc:
        return {"status": "driver-missing", "error": str(exc)}


def _write_private_temp(text: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="request-",
        suffix=".txt",
        dir=STATE_DIR,
        delete=False,
    )
    try:
        os.chmod(handle.name, 0o600)
        handle.write(text)
        return Path(handle.name)
    finally:
        handle.close()


def _run_desktop_sync(
    message: str,
    history: list[dict],
    context_id: str,
    thread_title: str,
    workspace: str = "",
) -> tuple[str, dict[str, Any]]:
    followup_path = _write_private_temp(_workspace_prompt(message, workspace))
    fresh_path = _write_private_temp(
        _fresh_task_prompt(context_id, history, message, workspace)
    )
    try:
        if workspace:
            capability = _driver_json(
                ["--workspace-folder-capability"], timeout=10
            )
            if (
                capability.get("status") != "ok"
                or capability.get("workspace_folder_selection") is not True
            ):
                raise PerplexityDesktopError(
                    "The installed Perplexity desktop driver cannot safely select "
                    "Agent Chat workspace folders yet.",
                    http_status=503,
                    retryable=False,
                )
        command = [
            "--prompt-file", str(followup_path),
            "--fresh-prompt-file", str(fresh_path),
            "--timeout", str(TIMEOUT_S),
        ]
        if thread_title:
            command.extend(["--thread-title", thread_title])
        if workspace:
            command.extend(["--workspace-folder", workspace])
        try:
            result = _driver_json(command, timeout=TIMEOUT_S + 30)
        except PerplexityDesktopError as exc:
            # Once the desktop helper has been launched, a timeout or missing
            # result is outcome-unknown: the prompt may already have run. Never
            # invite Agent Chat's automatic HTTP retry to submit it again.
            raise PerplexityDesktopError(
                str(exc), http_status=exc.http_status, retryable=False
            ) from exc
    finally:
        followup_path.unlink(missing_ok=True)
        fresh_path.unlink(missing_ok=True)

    status = str(result.get("status") or "error")
    answer = str(result.get("response") or "").strip()
    if status not in ("ok", "timeout-partial") or not answer:
        detail = str(result.get("error") or "Perplexity desktop returned no answer.")
        permission_error = "accessibility permission" in detail.casefold()
        raise PerplexityDesktopError(
            detail,
            http_status=503 if permission_error else 502,
            retryable=False,
        )
    workspace_selected = result.get("workspace_selected") is True
    if workspace and not workspace_selected:
        raise PerplexityDesktopError(
            "Perplexity did not confirm the Agent Chat workspace folder; no "
            "folder-backed result was accepted.",
            http_status=502,
            retryable=False,
        )
    return answer, {
        "model": "perplexity-computer",
        "runtime": "desktop-subscription",
        "thread_title": str(result.get("thread_title") or "").strip(),
        "continued": bool(result.get("continued", False)),
        "workspace_selected": workspace_selected,
        "partial": status == "timeout-partial",
    }


async def _run_perplexity(
    message: str,
    history: list[dict],
    context_id: str,
    thread_title: str = "",
    workspace: str = "",
) -> tuple[str, dict[str, Any]]:
    _set_progress(context_id, "Perplexity Computer is working in the desktop app…")
    async with DESKTOP_LOCK:
        return await asyncio.to_thread(
            _run_desktop_sync, message, history, context_id, thread_title, workspace
        )


@app.get("/health")
async def health():
    token_present = bool(_bridge_token())
    driver = await asyncio.to_thread(_driver_health)
    driver_status = str(driver.get("status") or "driver-missing")
    if not token_present:
        status = "missing-token"
        reason = "AGENT_TOKEN_PERPLEXITY is not configured."
    elif driver_status == "accessibility-required":
        status = "setup-required"
        reason = "Grant Accessibility access to Agent Chat Perplexity Driver."
    elif driver_status != "ok":
        status = "degraded"
        reason = str(driver.get("error") or driver_status)
    else:
        status = "ok"
        reason = ""
    return {
        "status": status,
        "reason": reason,
        "token_present": token_present,
        "runtime": "desktop-subscription",
        "model": "perplexity-computer",
        "desktop": driver,
        "turn_timeout_seconds": TIMEOUT_S,
        "last_upstream": dict(LAST_UPSTREAM),
    }


@app.get("/capabilities")
async def capabilities():
    return {
        "id": "perplexity",
        "model": "perplexity-computer",
        "best_for": (
            "Subscription-backed Perplexity Computer tasks: current research, "
            "connected services, approved local folders, and supported Mac apps."
        ),
        "strengths": [
            "Perplexity Computer orchestration",
            "live web research and source links",
            "approved local files and folders",
            "supported native Mac apps",
            "installed Perplexity connectors",
            "visible on-device approval gates",
        ],
        "avoid": (
            "The bridge cannot bypass Perplexity or macOS approval gates, and "
            "desktop tasks are serialized because they share one visible app."
        ),
        "voice": "Source-first desktop operator; explicit about approvals and observed results.",
        "blurb": "Perplexity Computer — your signed-in desktop subscription in Agent Chat.",
        "supports": {
            "web_search": True,
            "citations": True,
            "local_computer": True,
            "local_files": True,
            "native_apps": True,
            "connectors": True,
            "desktop_approvals": True,
        },
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
        "context_id": context_id,
        "log": {
            "progress": entry.get("text", ""),
            "progress_active": bool(entry.get("active", False)),
            "partial_response": entry.get("partial_response", ""),
        },
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
    try:
        workspace = _workspace_from_body(body)
    except PerplexityDesktopError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "retryable": exc.retryable,
                "error_provider": "perplexity",
                "model": "perplexity-computer",
            },
            status_code=exc.http_status,
        )

    context_id, ctx = _context(body.get("context_id"))
    previous = list(ctx.get("messages") or [])
    prior_title = str(ctx.get("thread_title") or "")
    prior_workspace = str(ctx.get("workspace") or "")
    workspace_changed = workspace != prior_workspace
    # Perplexity's folder selection belongs to one request and its existing-task
    # composer does not expose the Folders control.  Every workspace-bearing turn
    # therefore starts a fresh task and replays bounded history.  The persisted
    # marker also makes the first turn after detachment fresh once, preventing a
    # folder-bearing task from being resumed; after success the marker is cleared
    # and later no-folder turns may continue normally.
    force_fresh = bool(workspace) or workspace_changed
    dispatch_title = "" if force_fresh else prior_title

    try:
        reply, metadata = await _run_perplexity(
            message, previous, context_id, dispatch_title, workspace
        )
        LAST_UPSTREAM.update(status="ok", reason="", ts=int(time.time() * 1000))
    except Exception as exc:
        _end_progress(context_id)
        status = int(getattr(exc, "http_status", 500))
        # Unknown desktop failures are not safe to replay: the request may have
        # been submitted even when response extraction or context handling failed.
        retryable = bool(getattr(exc, "retryable", False))
        LAST_UPSTREAM.update(
            status="error", reason=str(exc)[:500], ts=int(time.time() * 1000)
        )
        return JSONResponse(
            {
                "error": str(exc),
                "retryable": retryable,
                "error_provider": "perplexity",
                "model": "perplexity-computer",
            },
            status_code=status,
        )

    _end_progress(context_id)
    contexts = _load_contexts()
    stored = contexts.setdefault(context_id, {"messages": []})
    stored.setdefault("messages", []).extend([
        {"role": "user", "text": _clean_message(message)},
        {"role": "assistant", "text": reply},
    ])
    stored["messages"] = stored["messages"][-MAX_CONTEXT_MESSAGES:]
    if metadata.get("thread_title"):
        stored["thread_title"] = metadata["thread_title"]
    if workspace:
        stored["workspace"] = workspace
    else:
        stored.pop("workspace", None)
    stored["updated_ts"] = int(time.time() * 1000)
    context_persisted = True
    try:
        _save_contexts(contexts)
    except Exception as exc:
        # The desktop result is already real and may include external actions.
        # Deliver it instead of returning a retryable 500 that could run the turn
        # twice. The next request can safely start from the last durable context.
        context_persisted = False
        LAST_UPSTREAM.update(
            status="degraded",
            reason=f"Perplexity replied, but its Agent Chat context was not saved: {exc}"[:500],
            ts=int(time.time() * 1000),
        )

    return {
        "response": reply,
        "context_id": context_id,
        "context_persisted": context_persisted,
        **metadata,
        **common.model_fallback_meta(body.get("model"), "perplexity-computer"),
    }
