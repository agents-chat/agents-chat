"""Outbound channels — chat/alert/log are live; phone channels are Phase D stubs.

The chat channel writes a *briefing*, not a flag dump. It posts as the senior
seat, so it should read like that agent noticed something — with the episode
context (new / still broken / recovered) the bus hands it, so one message can
stand in for the twenty the bus chose not to send.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import ForemanConfig
    from .events import FlagEvent

log = logging.getLogger("jarvis_foreman.channels")

# These JSONL files are append-only and, once the foreman is enabled, grow every
# tick — the event log gets a line per sensor per poll, forever. Nothing consumes
# the deep history (the HUD reads only the newest ~24 lines), so cap each file:
# when it crosses the high-water mark, keep the newest half. The size check is a
# cheap stat() on the common path; a rewrite happens only every few thousand lines.
_LOG_CAP_BYTES = 512 * 1024
_LOG_KEEP_BYTES = 256 * 1024


def append_capped(path: Path, line: str) -> None:
    """Append one line, trimming the file to its newest ~256 KB when it exceeds 512 KB."""
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        if path.stat().st_size <= _LOG_CAP_BYTES:
            return
        with path.open("rb") as f:
            f.seek(-_LOG_KEEP_BYTES, 2)
            tail = f.read()
        nl = tail.find(b"\n")  # drop the partial first line the seek landed inside
        if nl != -1:
            tail = tail[nl + 1:]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(tail)
        tmp.replace(path)
    except OSError:
        log.debug("append_capped trim failed for %s", path, exc_info=True)

# What a sensor watches, in words a human would use.
_SENSOR_LABELS = {
    "automation_pulse": "Automations",
    "docker_containers": "Docker",
    "bridge_health": "Agent bridges",
    "disk": "Disk",
}

_SEV_WORD = {"red": "red", "amber": "amber", "green": "clear"}


def sensor_label(sensor_id: str | None) -> str:
    if not sensor_id:
        return "System"
    return _SENSOR_LABELS.get(sensor_id, sensor_id.replace("_", " ").capitalize())


def _verb(label: str) -> str:
    """"Automations are red", not "Automations is red"."""
    return "are" if label.endswith("s") and not label.endswith("ss") else "is"


def human_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "a moment"
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{round(s / 60)}m"
    if s < 172800:
        return f"{round(s / 3600)}h"
    return f"{round(s / 86400)}d"


def _plain(text: str) -> str:
    """Strip the markdown a TTS voice would otherwise read out loud."""
    text = re.sub(r"[*_`#]+", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def detail_lines(event: "FlagEvent") -> list[str]:
    """Two or three concrete facts under the headline. No counters."""
    d = event.detail or {}
    out: list[str] = []
    if event.sensor_id == "docker_containers" and d.get("missing"):
        out.append("Not running: " + ", ".join(d["missing"]))
    elif event.sensor_id == "bridge_health" and d.get("down"):
        for item in d["down"][:3]:
            out.append(f"{item.get('agent')} — {item.get('error') or 'unreachable'}")
    elif event.sensor_id == "automation_pulse":
        # Offenders only — the counts are already in the headline.
        out.extend((d.get("offenders") or [])[:3])
    elif event.sensor_id == "disk" and d.get("free_gb") is not None:
        out.append(f"{d['free_gb']} GiB free of {d.get('total_gb', '?')} GiB")
    if d.get("error"):
        out.append(str(d["error"])[:160])
    return out[:4]


def brief_text(event: "FlagEvent") -> str:
    """The markdown body. Also the fallback for clients that can't draw a card."""
    label = sensor_label(event.sensor_id)
    state = getattr(event, "state", "new")
    headline = event.headline or ""
    since = getattr(event, "since_ts", None)
    dur = human_duration((time.time() - float(since)) if since else None)

    if state == "recovered":
        was = (event.detail or {}).get("recovered_from") or "degraded"
        head = f"**{label} — recovered.** Back to normal after {dur} {was}."
        return f"{head}\n\n{headline}" if headline else head

    if state == "ongoing":
        repeats = int(getattr(event, "repeats", 0) or 0)
        sev = _SEV_WORD.get(event.severity, event.severity)
        head = f"**{label} — still {sev}.**"
        tail = f"_Unchanged for {dur}"
        if repeats:
            tail += f"; {repeats} checks since I last raised it"
        tail += "._"
        return f"{head}\n\n{headline}\n\n{tail}"

    sev = _SEV_WORD.get(event.severity, event.severity)
    head = f"**{label} — {sev}.**"
    body = headline
    lines = detail_lines(event)
    if lines:
        body += "\n" + "\n".join(f"- {ln}" for ln in lines)
    return f"{head}\n\n{body}"


def speech_text(event: "FlagEvent") -> str:
    """One sentence, spoken. Never read the headline verbatim — it carries schedule
    ids, clipped error text and, in one real case, a customer's phone number."""
    label = sensor_label(event.sensor_id)
    v = _verb(label)
    state = getattr(event, "state", "new")
    sev = _SEV_WORD.get(event.severity, event.severity)
    if state == "recovered":
        return _plain(f"{label} {v} back to normal.")
    if state == "ongoing":
        return _plain(f"{label} {v} still {sev}.")
    lead = "Heads up." if event.severity == "amber" else "You should look at this."
    return _plain(f"{lead} {label} {v} {sev}.")


def card_metadata(event: "FlagEvent", senior: str) -> dict[str, Any]:
    """Structured payload so the chat renderer can draw a card, not a wall of text."""
    return {
        "kind": "foreman",
        "source": "jarvis_foreman",
        "foreman": True,
        "event_id": event.id,
        "severity": event.severity,
        "state": getattr(event, "state", "new"),
        "sensor_id": event.sensor_id,
        "sensor_label": sensor_label(event.sensor_id),
        "headline": event.headline,
        "detail_lines": detail_lines(event),
        "repeats": int(getattr(event, "repeats", 0) or 0),
        "since_ts": getattr(event, "since_ts", None),
        "recovered_from": (event.detail or {}).get("recovered_from"),
        "speech": speech_text(event),
        "senior": senior,
    }


class Channel(ABC):
    name: str = "base"

    def __init__(self, cfg: "ForemanConfig"):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.channel_enabled(self.name)

    @abstractmethod
    def emit(self, event: "FlagEvent") -> dict[str, Any]:
        ...


class LogChannel(Channel):
    """Always available: append JSON lines under data/jarvis_foreman_events.jsonl."""

    name = "log"

    def emit(self, event: "FlagEvent") -> dict[str, Any]:
        path = self.cfg.path.parent / "jarvis_foreman_events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        append_capped(path, json.dumps(event.to_dict(), ensure_ascii=False))
        return {"ok": True, "channel": self.name, "path": str(path)}


class ChatChannel(Channel):
    """Queue a briefing for the control room chat; app.py drains the outbox."""

    name = "chat"

    def emit(self, event: "FlagEvent") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "disabled"}
        outbox = self.cfg.path.parent / "jarvis_foreman_chat_outbox.jsonl"
        senior = self.cfg.senior_id()
        payload = {
            "ts": time.time(),
            "control_chat_id": self.cfg.raw.get("control_chat_id"),
            "as_agent": senior,
            "text": brief_text(event),
            "card": card_metadata(event, senior),
            "event_id": event.id,
            "event": event.to_dict(),
        }
        with outbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return {"ok": True, "channel": self.name, "outbox": str(outbox)}


class AlertBellChannel(Channel):
    name = "alert_bell"

    def emit(self, event: "FlagEvent") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "disabled"}
        path = self.cfg.path.parent / "jarvis_foreman_alerts.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "severity": event.severity,
                        "state": getattr(event, "state", "new"),
                        "sensor_id": event.sensor_id,
                        "headline": event.headline,
                        "event_id": event.id,
                    }
                )
                + "\n"
            )
        return {"ok": True, "channel": self.name}


class TelegramChannel(Channel):
    name = "telegram"

    def emit(self, event: "FlagEvent") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "disabled"}
        # Phase B: call existing outbox sweeper / notify path
        log.info("telegram stub: %s %s", event.severity, event.headline)
        return {"ok": True, "channel": self.name, "stub": True}


class VoiceAgenticChannel(Channel):
    """Leave a spoken line for the Control Room HUD to pick up and say."""

    name = "voice_agentic"

    def emit(self, event: "FlagEvent") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "disabled"}
        path = self.cfg.path.parent / "jarvis_foreman_voice_hints.jsonl"
        append_capped(
            path,
            json.dumps(
                {
                    "ts": time.time(),
                    "action": "open_voice_chat",
                    "roster_agent_id": self.cfg.senior_id(),
                    "event_id": event.id,
                    "severity": event.severity,
                    "headline": event.headline,
                    "speech": speech_text(event),
                }
            ),
        )
        return {"ok": True, "channel": self.name}


class TwilioChannel(Channel):
    name = "twilio"

    def emit(self, event: "FlagEvent") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "disabled"}
        return {"ok": True, "channel": self.name, "stub": True, "note": "Phase D"}


class RetellChannel(Channel):
    name = "retell"

    def emit(self, event: "FlagEvent") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "disabled"}
        return {
            "ok": True,
            "channel": self.name,
            "stub": True,
            "note": "Phase D — pipe only, brain stays in Agent Chat",
        }


def build_channels(cfg: "ForemanConfig") -> dict[str, Channel]:
    chans: list[Channel] = [
        LogChannel(cfg),
        ChatChannel(cfg),
        AlertBellChannel(cfg),
        TelegramChannel(cfg),
        VoiceAgenticChannel(cfg),
        TwilioChannel(cfg),
        RetellChannel(cfg),
    ]
    return {c.name: c for c in chans}
