"""Probe Agent Chat agent bridge endpoints (HTTP reachability)."""

from __future__ import annotations

import json
import os
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .base import Sensor, Signal
from ..http_utils import require_http_url

# Default bridge message URLs used by app roster (best-effort; env can override).
_DEFAULT_BRIDGES: dict[str, str] = {
    "hermes": os.environ.get("HERMES_AGENT_URL", "http://127.0.0.1:55011/api/api_message"),
    "ops-agent": os.environ.get(
        "OPS_AGENT_AGENT_URL", "http://127.0.0.1:55015/api/api_message"
    ),
    "lead-agent": os.environ.get(
        "LEAD_AGENT_AGENT_URL", "http://127.0.0.1:55017/api/api_message"
    ),
    "sales-agent": os.environ.get(
        "SALES_AGENT_AGENT_URL", "http://127.0.0.1:55018/api/api_message"
    ),
    "support-agent": os.environ.get(
        "SUPPORT_AGENT_AGENT_URL", "http://127.0.0.1:55020/api/api_message"
    ),
    # Ports match the roster in app.py / CLAUDE.md. The live app always sets these
    # via env, but the fallbacks must still be correct: run from a plain shell
    # (`python -m jarvis_foreman.cli tick`) they used to point grok at Antigravity's
    # 55014 and claude at MiniMax's 55012, and since _probe accepts any HTTP reply
    # as "up", that reported the wrong process's health under the wrong name.
    "grok": os.environ.get("GROK_AGENT_URL", "http://127.0.0.1:55019/api/api_message"),
    "claude": os.environ.get("CLAUDE_AGENT_URL", "http://127.0.0.1:55010/api/api_message"),
    "perplexity": os.environ.get(
        "PERPLEXITY_AGENT_URL", "http://127.0.0.1:55023/health"
    ),
}

# Health-family statuses app.py treats as degraded. A bridge that is listening but
# cannot serve a turn is NOT healthy, and reporting it as up is how a false-green
# dot survives (the exact bug already fixed once for @minimax).
_DEGRADED_STATUSES = {
    "degraded", "error", "missing-token", "setup-required",
    "not-authenticated", "not-ready",
}


def _probe(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """GET-ish probe: many bridges only POST; we hit host root or accept 405/404 as up."""
    # Derive health-ish URL: strip to scheme://host:port/
    try:
        safe_url = require_http_url(url)
        parsed = urlsplit(safe_url)
        base = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
    except ValueError as exc:
        return False, f"ValueError: {exc}"
    # A /health URL is probed as given rather than stripped to the host root: the
    # point of hitting it is to read the status field, which the root does not carry.
    if parsed.path.rstrip("/").endswith("/health"):
        base = safe_url
    try:
        req = urllib.request.Request(base, method="GET")
        # require_http_url above restricts urllib to its HTTP(S) handlers.
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = resp.read(4096)
            status = ""
            try:
                status = str((json.loads(body.decode("utf-8", "replace")) or {}).get("status") or "")
            except Exception:
                status = ""
            if status in _DEGRADED_STATUSES:
                return False, f"status {status}"
            return True, f"http {resp.status}"
    except urllib.error.HTTPError as e:
        # Any HTTP response means process is listening.
        return True, f"http {e.code}"
    except Exception as e:
        return False, type(e).__name__ + (f": {e}" if str(e) else "")


class BridgeHealthSensor(Sensor):
    id = "bridge_health"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        bridges: dict[str, str] = dict(_DEFAULT_BRIDGES)
        bridges.update(sc.get("urls") or {})
        only = sc.get("agents")
        if only:
            bridges = {k: v for k, v in bridges.items() if k in only}

        down: list[dict[str, Any]] = []
        up: list[str] = []
        for agent_id, url in bridges.items():
            ok, note = _probe(url)
            if ok:
                up.append(agent_id)
            else:
                down.append({"agent": agent_id, "url": url, "error": note})

        if not down:
            return [
                Signal(
                    sensor_id=self.id,
                    severity="green",
                    headline=f"bridges ok ({len(up)})",
                    detail={"up": up},
                )
            ]
        sev = "red" if len(down) >= max(1, len(bridges) // 2) else "amber"
        names = ", ".join(d["agent"] for d in down)
        return [
            Signal(
                sensor_id=self.id,
                severity=sev,
                headline=f"bridge down: {names}",
                dedupe_key="bridge:down:" + ",".join(sorted(d["agent"] for d in down)),
                detail={"down": down, "up": up},
            )
        ]
