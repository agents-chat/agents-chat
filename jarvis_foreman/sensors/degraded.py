"""degraded — "up but slow": latency creep on the agent-bridge /health endpoints.

Down/up is bridge_health's job. This sensor catches the *earlier* signal: a bridge
that still answers but has gotten slow, before it tips over into an outright failure.
We time a short GET to each bridge's `<base>/health` and grade the response time.

Only bridges that actually respond are timed. A bridge that is DOWN — connection
refused, or so wedged the probe times out — is skipped here on purpose: that is
bridge_health's incident to raise, not a latency reading we can trust.

Cross-platform: stdlib urllib only (psutil is not installed and this also runs on the
Windows client). Nothing POSIX-only is touched, and poll() never raises.

Dedupe discipline (see sensors/base.Signal): key on the SET of slow agents, never on
the milliseconds — latency churns every tick and would mint a fresh cooldown slot each
poll. Green uses the fixed key "degraded:ok".
"""

from __future__ import annotations

import os
import time
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .base import Sensor, Signal
from ..http_utils import require_http_url

# Same bridge map idea as bridge_health.py: {agent: base_url}, env-overridable, with
# localhost fallbacks copied from bridge_health (ports match the roster in app.py /
# CLAUDE.md). The fallbacks carry the POST message path; _health_url() strips that back
# to scheme://host:port before appending /health, so either form works here.
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
    "grok": os.environ.get("GROK_AGENT_URL", "http://127.0.0.1:55019/api/api_message"),
    "claude": os.environ.get("CLAUDE_AGENT_URL", "http://127.0.0.1:55010/api/api_message"),
    "perplexity": os.environ.get(
        "PERPLEXITY_AGENT_URL", "http://127.0.0.1:55023/api/api_message"
    ),
}


def _health_url(base: str) -> str:
    """Normalize any bridge URL to its /health endpoint.

    The map's fallbacks (and the live app's env) point at POST message paths
    (…/api/api_message); we only want scheme://host:port then /health, so strip the
    path first. Invalid or non-HTTP(S) configuration is rejected.
    """
    safe_base = require_http_url(base)
    parsed = urlsplit(safe_base)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _time_health(url: str, timeout: float) -> tuple[bool, float]:
    """Time a short GET to `url`.

    Returns (responded, elapsed_sec). responded is False when the bridge is
    unreachable — connection refused or the probe timed out — which means it's down,
    not slow, and the caller skips it (bridge_health owns down/up). An HTTP error
    status (404/405/5xx) still counts as responded: the process answered, so its
    latency is a valid sample.
    """
    start = time.perf_counter()
    try:
        url = require_http_url(url)
        req = urllib.request.Request(url, method="GET")
        # require_http_url above restricts urllib to its HTTP(S) handlers.
        with urllib.request.urlopen(req, timeout=timeout):  # nosec B310
            return True, time.perf_counter() - start
    except urllib.error.HTTPError:
        # Server answered with a non-2xx status — it's up, just not OK. Timing stands.
        return True, time.perf_counter() - start
    except Exception:
        # URLError (refused/DNS), socket timeout, etc. → down/unreachable; not ours.
        return False, 0.0


class DegradedSensor(Sensor):
    id = "degraded"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        bridges: dict[str, str] = dict(_DEFAULT_BRIDGES)
        bridges.update(sc.get("urls") or {})
        only = sc.get("agents")
        if only:
            bridges = {k: v for k, v in bridges.items() if k in only}

        timeout = float(sc.get("probe_timeout_sec", 4))
        amber_ms = float(sc.get("amber_ms", 1500))
        red_ms = float(sc.get("red_ms", 5000))

        try:
            measured: list[dict[str, Any]] = []  # responded → carries latency
            skipped: list[str] = []              # down/unreachable → not our concern
            for agent_id, base in bridges.items():
                responded, elapsed = _time_health(_health_url(base), timeout)
                if not responded:
                    skipped.append(agent_id)
                    continue
                measured.append({"agent": agent_id, "sec": round(elapsed, 3),
                                 "ms": round(elapsed * 1000, 1)})
        except Exception as e:  # config drift / anything unexpected → amber, never crash
            return [Signal(self.id, "amber", "degraded check failed",
                           dedupe_key="degraded:check_failed", detail={"error": str(e)})]

        red = [m for m in measured if m["ms"] >= red_ms]
        amber = [m for m in measured if red_ms > m["ms"] >= amber_ms]
        slow = red + amber
        if not slow:
            return [Signal(
                self.id, "green",
                f"bridge latency ok ({len(measured)} responding)",
                dedupe_key="degraded:ok",
                detail={"measured": measured, "skipped": skipped},
            )]

        severity = "red" if red else "amber"
        worst_first = sorted(slow, key=lambda m: m["ms"], reverse=True)
        # Condition-keyed: which agents are slow, never how slow (ms churns each poll).
        slow_agents = sorted(m["agent"] for m in slow)
        offenders = [f"{m['agent']} {m['sec']:.1f}s" for m in worst_first]
        return [Signal(
            self.id, severity,
            ("bridge slow: " + ", ".join(m["agent"] for m in worst_first))[:220],
            dedupe_key="degraded:slow:" + ",".join(slow_agents),
            detail={"measured": measured, "skipped": skipped, "offenders": offenders},
        )]
