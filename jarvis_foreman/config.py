"""Foreman config — seats, sensors, escalate policy. File-backed for Phase A."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

def _default_path() -> Path:
    """Resolve `JARVIS_FOREMAN_CONFIG` on every call, never once at import.

    Freezing this at import time made test isolation depend on module import
    order: whichever test module imported this one first decided the path for
    the whole session. When a test module lost that race, `load_config()` fell
    back to the live `data/jarvis_foreman.json` and the control-room POST tests
    happily wrote their fixture chat id (1001) over the operator's real pin.
    """
    return Path(
        os.environ.get(
            "JARVIS_FOREMAN_CONFIG",
            str(Path(__file__).resolve().parent.parent / "data" / "jarvis_foreman.json"),
        )
    )


# Back-compat for callers that read the module attribute directly. Prefer
# `_default_path()`; this snapshot is only correct if the env never changes.
_DEFAULT_PATH = _default_path()


def default_config() -> dict[str, Any]:
    return {
        "v": 1,
        "enabled": False,  # off until Owner flips it
        "poll_interval_sec": 60,
        "control_chat_id": None,  # optional room for flag posts
        # Junior's brain: a cheap local model narrates each tick for the HUD live
        # panel + durable audit. Off-path from severity — if it fails, the sensors
        # (and every flag/channel) behave exactly as before; only the narration line
        # falls back to a deterministic template. See jarvis_foreman/narrator.py.
        "junior": {
            "enabled": True,
            "model": "qwen3:4b-instruct",
            "base_url": "http://127.0.0.1:11434",
            "timeout_sec": 8,
            "max_sentences": 2,
        },
        "seats": {
            "senior_pm": {
                "roster_agent_id": "grok",
                "capabilities": ["chat", "voice", "tools_full"],
            },
            "junior_pm": {
                "roster_agent_id": None,  # None = in-process only
                "capabilities": ["monitor", "flag", "read_sensors"],
                "model_hint": "cheap",
            },
        },
        "sensors": {
            "bridge_health": {"enabled": True},
            "docker_containers": {
                "enabled": True,
                # Real compose names on this box (not bare "hermes")
                "containers": [
                    "Ops Agent",
                    "Hermes",
                    "Support Agent",
                    "Lead Agent",
                    "Sales Agent",
                ],
            },
            "automation_pulse": {
                "enabled": True,
                "lookback_hours": 24,
                "amber_on": ["amber", "failed", "held"],
                "red_on": ["red", "failed_hard"],
            },
            "disk": {
                "enabled": True,
                "path": str(Path(__file__).resolve().parent.parent / "data"),
                "amber_free_gb": 10,
                "red_free_gb": 2,
            },
            # Junior watches every agent chat: a user waiting past an SLA, a
            # conversation stalled on an open hold/question, or an agent that errored
            # or was stopped. The wide net; Senior decides what's real.
            "chat_activity": {
                "enabled": True,
                "unanswered_amber_min": 30,
                "unanswered_red_min": 120,
                "stalled_amber_min": 30,
                "stalled_red_min": 180,
                "error_lookback_hours": 6,
                "include_archived": False,
            },
            # Junior watches his own footprint: brain (Ollama) reachable, host load,
            # runaway logs. The monitor must never become the incident.
            "self_resources": {
                "enabled": True,
                "amber_load_ratio": 4.0,
                "red_load_ratio": 8.0,
                "amber_logs_mb": 200,
            },
            # OAuth tokens / TLS certs / API keys to watch for expiry. Operator fills
            # `items` (e.g. the weekly Brokerage OAuth re-login):
            #   {"name": "Brokerage OAuth", "expires_at": "2026-07-17T09:00:00Z", "kind": "oauth"}
            "credentials": {
                "enabled": True,
                "red_days": 2,
                "amber_days": 7,
                "items": [],
            },
            # "Up but slow" — bridge /health latency creep (down is bridge_health's
            # job). probe_timeout>red_ms so a slow-but-alive bridge is timed as red,
            # not dropped as down.
            "degraded": {
                "enabled": True,
                "probe_timeout_sec": 6,
                "amber_ms": 1500,
                "red_ms": 5000,
            },
            # Queue depth: foreman chat outbox + pending automation items.
            "backlog": {
                "enabled": True,
                "amber_outbox": 20,
                "red_outbox": 100,
                "amber_items": 50,
                "red_items": 200,
            },
            # Token/cost budget guard. Dollar budget wins when set; else meter tokens.
            # NOTE: daily_token_budget is a placeholder with headroom over current
            # usage (~5M/day) — set it to your real budget, or set daily_budget_usd.
            "cost_guard": {
                "enabled": True,
                "daily_budget_usd": None,
                "daily_token_budget": 20_000_000,
                "amber_pct": 80,
                "prices_usd_per_mtok": {},
                "default_price_usd_per_mtok": 1.0,
                "top_n": 5,
            },
            # Uncommitted / unexpected changes to the live build (catches a stray edit
            # to a bridge script). data/ is gitignored so runtime state never flaps it.
            "drift": {
                "enabled": True,
                "red_files": 25,
                "include_untracked": False,
                "timeout_sec": 8,
            },
            # CLAUDE.md rule: app + bridge listeners must stay on loopback. Flag any
            # watched port bound beyond 127.0.0.1/::1. auth_log optional (None = skip).
            "security": {
                "enabled": True,
                "watch_ports": [8086, 55009, 55010, 55011, 55012, 55014, 55015, 55017, 55018, 55019, 55020, 55023],
                "auth_log": None,
                "amber_auth_fails": 20,
                "auth_window_min": 60,
            },
            # Backup freshness — newest backup older than the thresholds → amber/red.
            "backups": {
                "enabled": True,
                "amber_hours": 48,
                "red_hours": 168,
            },
            # Learn-on-miss is internal retrieval telemetry. The diagnostic sensor is
            # retained for explicit polling only and is not in the default registry.
            "memory_gap": {
                "enabled": False,
                "min_ask": 2,
                "max_show": 8,
            },
        },
        # Senior — the reasoning brain. When Junior raises a NEW flag, the seated
        # senior_pm agent is invoked to analyze it (real vs noise, root cause,
        # recommended fix). Real ones post a full briefing into the control chat and
        # push a notification. Analysis runs only on new flags, so cost is bounded.
        "senior": {
            "enabled": True,
            "min_confidence": 0.5,   # below this, Senior's "real" verdict is logged but not pushed
            "timeout_sec": 40,
            "context_messages": 12,  # recent messages per offending chat to hand Senior
            "notify": True,          # push a notification for confirmed real flags
            # If the seated provider is capped/degraded, keep the Control Room
            # useful without spending repeated doomed calls. Ordered and bounded.
            "fallback_agent_ids": ["grok", "minimax"],
        },
        # One message per episode, then silence until the cooldown lapses.
        # Episodes are identified by Signal.dedupe_key, never by the headline.
        "dedupe": {
            "enabled": True,
            "chat_cooldown_sec": 1800,
            "alert_cooldown_sec": 600,
        },
        # Say so once when a problem clears — the news Owner actually wants.
        "recovery": {"enabled": True, "channels": ["chat"]},
        # The Control Room speaks with its own voice, not the senior agent's. bm_fable
        # is the youngest-sounding of the British male Kokoro voices; pitch is not the
        # cue (bm_george sits ~20Hz higher and still reads as older). Any id not in the
        # kokoro server's whitelist is silently swapped for af_heart — a FEMALE voice —
        # so only ever put a whitelisted id here.
        "voice": {"id": "bm_fable", "speed": 1.06},
        "escalate": {
            "green": {"channels": [], "open_voice": False},
            "amber": {"channels": ["chat", "alert_bell"], "open_voice": False},
            "red": {
                "channels": ["chat", "alert_bell", "telegram"],
                # Leaves a spoken line for the Control Room to announce. Nothing plays
                # unless the panel is open and "Announce" is on, so this cannot ambush
                # anyone — and the foreman itself stays off until it's switched on.
                "open_voice": True,
            },
        },
        "channels": {
            "chat": {"enabled": True},
            "alert_bell": {"enabled": True},
            "telegram": {"enabled": False},
            "voice_agentic": {"enabled": True},
            "twilio": {"enabled": False},
            "retell": {"enabled": False},
        },
        # Overnight: hold amber, still page for red. Anything held is reported on
        # the first tick after the window closes, not dropped.
        "quiet_hours": {
            "enabled": True,
            "start": "22:00",
            "end": "07:00",
            "allow_red": True,
        },
    }


@dataclass
class ForemanConfig:
    raw: dict[str, Any] = field(default_factory=default_config)
    path: Path = field(default_factory=_default_path)

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled"))

    def seat_roster_id(self, seat: str) -> str | None:
        seats = self.raw.get("seats") or {}
        s = seats.get(seat) or {}
        rid = s.get("roster_agent_id")
        return str(rid) if rid else None

    def senior_id(self) -> str:
        return self.seat_roster_id("senior_pm") or "grok"

    def sensor_cfg(self, sensor_id: str) -> dict[str, Any]:
        return dict((self.raw.get("sensors") or {}).get(sensor_id) or {})

    def escalate_for(self, severity: str) -> dict[str, Any]:
        return dict((self.raw.get("escalate") or {}).get(severity) or {})

    def channel_enabled(self, name: str) -> bool:
        ch = (self.raw.get("channels") or {}).get(name) or {}
        return bool(ch.get("enabled"))


def load_config(path: Path | None = None) -> ForemanConfig:
    p = path or _default_path()
    if not p.exists():
        cfg = ForemanConfig(raw=default_config(), path=p)
        save_config(cfg)
        return cfg
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    # Merge missing keys from defaults (forward-compatible upgrades)
    base = default_config()
    _deep_merge_missing(base, raw)
    _merge_required_runtime_targets(base)
    return ForemanConfig(raw=base, path=p)


def save_config(cfg: ForemanConfig) -> None:
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg.raw, f, indent=2, sort_keys=False)
        f.write("\n")
    tmp.replace(cfg.path)


def _deep_merge_missing(base: dict, overlay: dict) -> None:
    """Write overlay into base; for dict values, fill missing keys only from base then overlay wins."""
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge_missing(base[k], v)
        else:
            base[k] = deepcopy(v)


def _merge_required_runtime_targets(config: dict) -> None:
    """Keep older saved configs aware of newly shipped local services.

    Saved list values intentionally override defaults, so ordinary deep-merge
    cannot add a new bridge/container after an upgrade. Preserve every custom
    entry and append only the canonical services/ports the live app now owns.
    """
    sensors = config.setdefault("sensors", {})
    docker_cfg = sensors.setdefault("docker_containers", {})
    containers = list(docker_cfg.get("containers") or [])
    for name in ("Hermes", "Support Agent"):
        if name not in containers:
            containers.append(name)
    docker_cfg["containers"] = containers

    security_cfg = sensors.setdefault("security", {})
    ports = [int(port) for port in (security_cfg.get("watch_ports") or [])]
    for port in (55020, 55023):
        if port not in ports:
            ports.append(port)
    security_cfg["watch_ports"] = ports
