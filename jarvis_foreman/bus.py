"""Foreman bus: poll sensors → policy → channels. No UI coupling.

Noise model — the bus thinks in *episodes*, not ticks.

An episode is one continuous problem, identified by a sensor's `dedupe_key`
(see `sensors/base.Signal`). While an episode is open the bus reports it once,
then stays quiet until the cooldown lapses, then says "still broken, N checks
later" — one message, not N. When the condition clears it says so once and
forgets the episode.

The rule that matters: an episode's identity must not contain anything that
changes on its own. Keying on the headline (which carries live counts) mints a
fresh cooldown slot every poll, so nothing is ever suppressed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any

from .channels import build_channels
from .config import ForemanConfig, load_config
from .events import EpisodeState, EventKind, FlagEvent
from .sensors import build_default_registry

log = logging.getLogger("jarvis_foreman.bus")

_SEV_RANK = {"green": 0, "amber": 1, "red": 2}
# Channels a human actually hears. The log channel is never suppressed: the
# audit trail records every tick even when nobody is told.
_NOISY = ("chat", "alert_bell", "telegram", "voice_agentic")
_STATE_V = 2
# Retired operator sensors are scrubbed silently on the next route. Do not emit a
# recovery: these episodes were product misclassification, not recovered incidents.
_RETIRED_SENSOR_IDS = frozenset({"memory_gap"})

# The episode file is read-modify-written by two threads of the same process: the
# background poll loop (`_jarvis_foreman_loop_tick` → asyncio.to_thread) and the
# ack endpoint (`/api/control-room/ack` → asyncio.to_thread). Without this, an ack
# that lands while a tick holds a stale snapshot is silently overwritten — the
# operator presses "Ack 4h", sees the toast, and gets paged anyway. Module-level so
# it is shared across ForemanBus instances, which the two paths each construct.
_STATE_LOCK = threading.RLock()


def _rank(severity: str) -> int:
    return _SEV_RANK.get(severity, 0)


def _parse_hhmm(raw: Any, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(raw).split(":", 1)
        return int(hh) % 24, int(mm) % 60
    except Exception:
        return fallback


class ForemanBus:
    def __init__(self, cfg: ForemanConfig | None = None):
        self.cfg = cfg or load_config()
        self.sensors = build_default_registry(self.cfg)
        self.channels = build_channels(self.cfg)

    def reload(self) -> None:
        self.cfg = load_config(self.cfg.path)
        self.sensors = build_default_registry(self.cfg)
        self.channels = build_channels(self.cfg)

    # ---------------------------------------------------------------- state

    def _state_path(self):
        return self.cfg.path.parent / "jarvis_foreman_dedupe.json"

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return {"v": _STATE_V, "sensors": {}, "resolved": {}}
        try:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {"v": _STATE_V, "sensors": {}, "resolved": {}}
        # v1 was a flat {"chat:sensor|sev|headline": ts} map keyed on volatile
        # headlines. Those keys can never match again; drop them rather than
        # carry a map that grows a new entry on every poll.
        if not isinstance(raw, dict) or raw.get("v") != _STATE_V:
            return {"v": _STATE_V, "sensors": {}, "resolved": {}}
        sensors = raw.get("sensors")
        resolved = raw.get("resolved")
        return {
            "v": _STATE_V,
            "sensors": sensors if isinstance(sensors, dict) else {},
            # Operator-resolved conditions: {sensor_id: {key, ts}}. Independent of
            # the per-episode `sensors` map so it survives episode churn.
            "resolved": resolved if isinstance(resolved, dict) else {},
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        tmp.replace(path)

    # --------------------------------------------------------------- policy

    def _cooldown(self, channel: str) -> float:
        dd = (self.cfg.raw.get("dedupe") or {}) if self.cfg.raw else {}
        if channel == "alert_bell":
            return float(dd.get("alert_cooldown_sec") or 600)
        return float(dd.get("chat_cooldown_sec") or 1800)

    def _dedupe_on(self) -> bool:
        dd = (self.cfg.raw.get("dedupe") or {}) if self.cfg.raw else {}
        return dd.get("enabled") is not False

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        q = (self.cfg.raw.get("quiet_hours") or {}) if self.cfg.raw else {}
        if not q.get("enabled"):
            return False
        now = now or datetime.now()
        sh, sm = _parse_hhmm(q.get("start"), (22, 0))
        eh, em = _parse_hhmm(q.get("end"), (7, 0))
        cur, start, end = now.hour * 60 + now.minute, sh * 60 + sm, eh * 60 + em
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end  # window wraps past midnight

    def _quiet_blocks(self, severity: str, quiet: bool) -> bool:
        """Quiet hours mute everything except red, unless red is muted too."""
        if not quiet:
            return False
        q = (self.cfg.raw.get("quiet_hours") or {}) if self.cfg.raw else {}
        if severity == "red" and q.get("allow_red", True):
            return False
        return True

    @staticmethod
    def _acked(episode: dict[str, Any], now: float) -> bool:
        """An acknowledged episode stays silent until the ack lapses. Only silence —
        the sensor keeps polling and the HUD keeps showing it red. A *changed*
        condition or an escalation opens a new episode, which drops the ack: you
        cannot mute tomorrow's problem by acknowledging today's."""
        try:
            return float(episode.get("ack_until") or 0) > now
        except (TypeError, ValueError):
            return False

    def _recovery_channels(self) -> list[str]:
        rec = (self.cfg.raw.get("recovery") or {}) if self.cfg.raw else {}
        if rec.get("enabled") is False:
            return []
        chans = rec.get("channels")
        return list(chans) if isinstance(chans, list) else ["chat"]

    # ------------------------------------------------------------------ ack

    def acknowledge(self, sensor_id: str, hours: float) -> dict[str, Any]:
        """Silence an OPEN episode for `hours`. Returns the resulting ack window.

        There is nothing to acknowledge on a healthy sensor — an ack is scoped to the
        episode that is currently open, so it evaporates the moment that episode does.
        """
        with _STATE_LOCK:
            state = self._load_state()
            episode = state["sensors"].get(sensor_id)
            if not isinstance(episode, dict):
                return {"ok": False, "error": "no open episode for that sensor"}
            if hours <= 0:
                episode.pop("ack_until", None)
                until = None
            else:
                until = time.time() + hours * 3600
                episode["ack_until"] = until
            self._save_state(state)
        return {"ok": True, "sensor_id": sensor_id, "ack_until": until}

    def ack_windows(self) -> dict[str, float]:
        """{sensor_id: ack_until} for episodes still inside their ack window."""
        now = time.time()
        out = {}
        for sid, ep in (self._load_state().get("sensors") or {}).items():
            if sid in _RETIRED_SENSOR_IDS:
                continue
            if isinstance(ep, dict) and self._acked(ep, now):
                out[sid] = float(ep["ack_until"])
        return out

    # -------------------------------------------------------------- resolve

    def resolve(self, sensor_id: str, key: str) -> dict[str, Any]:
        """Mark a sensor's CURRENT condition (identified by its dedupe `key`) as
        resolved by the operator. Stronger than ack: resolve *clears* the flag from
        the pulse (not just mutes it), for a fix the sensor can't see or a false
        positive. It holds only while the condition is unchanged — a new/changed
        condition mints a different key, so tomorrow's problem is never pre-resolved.
        """
        key = str(key or "").strip()
        if not key:
            return {"ok": False, "error": "no condition key to resolve"}
        with _STATE_LOCK:
            state = self._load_state()
            state.setdefault("resolved", {})[sensor_id] = {"key": key, "ts": time.time()}
            self._save_state(state)
        return {"ok": True, "sensor_id": sensor_id, "key": key}

    def unresolve(self, sensor_id: str) -> dict[str, Any]:
        """Undo a resolve — the flag returns to whatever the sensor currently reads."""
        with _STATE_LOCK:
            state = self._load_state()
            state.setdefault("resolved", {}).pop(sensor_id, None)
            self._save_state(state)
        return {"ok": True, "sensor_id": sensor_id}

    def resolved_map(self) -> dict[str, str]:
        """{sensor_id: resolved_key} — the exact condition the operator cleared."""
        out: dict[str, str] = {}
        for sid, r in (self._load_state().get("resolved") or {}).items():
            if sid in _RETIRED_SENSOR_IDS:
                continue
            if isinstance(r, dict) and r.get("key"):
                out[sid] = str(r["key"])
        return out

    # --------------------------------------------------------------- events

    def _event(
        self,
        sig,
        *,
        kind: str,
        state: str,
        hints: list[str],
        repeats: int = 0,
        since_ts: float | None = None,
        detail_extra: dict[str, Any] | None = None,
    ) -> FlagEvent:
        detail = dict(getattr(sig, "detail", {}) or {})
        if detail_extra:
            detail.update(detail_extra)
        return FlagEvent(
            source=f"sensor:{sig.sensor_id}",
            kind=kind,
            severity=sig.severity,
            headline=sig.headline,
            detail=detail,
            sensor_id=sig.sensor_id,
            target_seat="senior_pm",
            channel_hints=list(hints),
            state=state,
            repeats=repeats,
            since_ts=since_ts,
            dedupe_key=sig.key() if hasattr(sig, "key") else "",
        )

    def _poll_all(self) -> list:
        signals = []
        for s in self.sensors:
            try:
                signals.extend(s.poll())
            except Exception as e:
                log.exception("sensor %s failed", getattr(s, "id", s))
                sid = getattr(s, "id", "unknown")
                signals.append(
                    type(
                        "Sig",
                        (),
                        {
                            "sensor_id": sid,
                            "severity": "amber",
                            "headline": f"sensor error: {e}",
                            "detail": {"error": str(e)},
                            "dedupe_key": f"{sid}:sensor_error",
                            "key": lambda self: self.dedupe_key,
                        },
                    )()
                )
        return signals

    # ----------------------------------------------------------------- tick

    def tick(self) -> dict[str, Any]:
        """One poll cycle. Safe to call from a background task or CLI.

        Sensor polling (docker exec, HTTP probes, sqlite) happens outside the state
        lock; only the read-modify-write of the episode file is serialized, so a
        concurrent ack never waits on a slow container check.
        """
        return self.tick_with_signals()[0]

    def tick_with_signals(self) -> tuple[dict[str, Any], list]:
        """Like tick(), but also returns the raw Signals polled this cycle. Lets the
        caller build a snapshot (for narration / Senior) off one poll instead of
        re-polling all sensors — every sensor's stable Signal.key() rides along."""
        if not self.cfg.enabled:
            return {"ok": True, "skipped": True, "reason": "foreman disabled"}, []

        signals = self._poll_all()
        with _STATE_LOCK:
            return self._route(signals), signals

    def _route(self, signals: list) -> dict[str, Any]:
        """Episode bookkeeping + channel fan-out. Caller holds `_STATE_LOCK`."""
        state = self._load_state()
        sensors_state: dict[str, Any] = state["sensors"]
        resolved = state.get("resolved") or {}
        now = time.time()
        quiet = self.in_quiet_hours()
        dedupe_on = self._dedupe_on()
        dirty = False

        for sid in _RETIRED_SENSOR_IDS:
            if sensors_state.pop(sid, None) is not None:
                dirty = True
            if resolved.pop(sid, None) is not None:
                dirty = True

        worst = None
        for sig in signals:
            if worst is None or _rank(sig.severity) > _rank(worst.severity):
                worst = sig

        results: list[dict[str, Any]] = []

        for sig in signals:
            sid = sig.sensor_id
            prev = sensors_state.get(sid) if isinstance(sensors_state.get(sid), dict) else None

            # ---- recovered / still fine ---------------------------------
            if sig.severity == "green":
                if prev and _rank(prev.get("severity", "green")) > 0:
                    since = prev.get("since")
                    evt = self._event(
                        sig,
                        kind=EventKind.FLAG.value,
                        state=EpisodeState.RECOVERED.value,
                        hints=self._recovery_channels(),
                        repeats=int(prev.get("total_repeats") or 0),
                        since_ts=since,
                        detail_extra={
                            "recovered_from": prev.get("severity"),
                            "duration_sec": (now - float(since)) if since else None,
                        },
                    )
                    results.append(self.channels["log"].emit(evt))
                    # A 3am "it's fine now" is not worth waking anyone for.
                    if not quiet:
                        for name in evt.channel_hints:
                            ch = self.channels.get(name)
                            if ch is not None:
                                results.append(ch.emit(evt))
                    sensors_state.pop(sid, None)
                    dirty = True
                else:
                    results.append(
                        self.channels["log"].emit(
                            self._event(
                                sig,
                                kind=EventKind.SIGNAL.value,
                                state=EpisodeState.NEW.value,
                                hints=["log"],
                            )
                        )
                    )
                    if prev:
                        sensors_state.pop(sid, None)
                        dirty = True
                continue

            # ---- open or continue an episode ----------------------------
            key = sig.key()
            # A resolved condition (operator marked it handled) stays silent until the
            # condition changes — same discipline as ack, keyed on the dedupe key.
            is_resolved = (resolved.get(sid) or {}).get("key") == key
            escalated = prev is not None and _rank(sig.severity) > _rank(prev.get("severity", "green"))
            new_episode = prev is None or prev.get("key") != key or escalated

            if new_episode:
                episode = {
                    "key": key,
                    "severity": sig.severity,
                    "since": float(prev["since"]) if (prev and escalated and prev.get("since")) else now,
                    "last_emit": {},
                    "suppressed": {},
                    "total_repeats": 0,
                }
                sensors_state[sid] = episode
            else:
                episode = prev
                # De-escalation (red→amber) on the same condition keeps the episode
                # open: it's the same problem improving, not a new one.
                episode["severity"] = sig.severity
                episode["total_repeats"] = int(episode.get("total_repeats") or 0) + 1
            dirty = True

            pol = self.cfg.escalate_for(sig.severity)
            hints = list(pol.get("channels") or [])
            if pol.get("open_voice"):
                hints.append("voice_agentic")

            # Audit trail: every tick, every severity, always.
            results.append(
                self.channels["log"].emit(
                    self._event(
                        sig,
                        kind=EventKind.FLAG.value,
                        state=EpisodeState.NEW.value if new_episode else EpisodeState.ONGOING.value,
                        hints=hints,
                        repeats=int(episode.get("total_repeats") or 0),
                        since_ts=episode.get("since"),
                    )
                )
            )

            last_emit = episode.setdefault("last_emit", {})
            suppressed = episode.setdefault("suppressed", {})

            for name in hints:
                ch = self.channels.get(name)
                if ch is None:
                    continue
                if name not in _NOISY:
                    results.append(ch.emit(self._event(
                        sig, kind=EventKind.FLAG.value,
                        state=EpisodeState.NEW.value if new_episode else EpisodeState.ONGOING.value,
                        hints=hints, since_ts=episode.get("since"),
                    )))
                    continue

                # A resolved condition is silent on every human channel until it
                # changes; the log channel above still records the tick.
                if is_resolved:
                    suppressed[name] = int(suppressed.get(name) or 0) + 1
                    results.append({
                        "ok": True, "channel": name, "skipped": True,
                        "reason": "resolved", "headline": sig.headline,
                    })
                    continue

                # Both gates leave last_emit stale on purpose: when the ack or the quiet
                # window lapses, the cooldown check below trips immediately and reports
                # what was held back, instead of swallowing it silently.
                if self._acked(episode, now):
                    suppressed[name] = int(suppressed.get(name) or 0) + 1
                    results.append({
                        "ok": True, "channel": name, "skipped": True,
                        "reason": "acknowledged", "headline": sig.headline,
                        "ack_until": episode.get("ack_until"),
                    })
                    continue

                if self._quiet_blocks(sig.severity, quiet):
                    suppressed[name] = int(suppressed.get(name) or 0) + 1
                    results.append({
                        "ok": True, "channel": name, "skipped": True,
                        "reason": "quiet_hours", "headline": sig.headline,
                    })
                    continue

                last = last_emit.get(name)
                due = (
                    not dedupe_on
                    or new_episode
                    or last is None
                    or (now - float(last)) >= self._cooldown(name)
                )
                if not due:
                    suppressed[name] = int(suppressed.get(name) or 0) + 1
                    results.append({
                        "ok": True, "channel": name, "skipped": True,
                        "reason": "deduped", "headline": sig.headline,
                        "suppressed": suppressed[name],
                    })
                    continue

                evt = self._event(
                    sig,
                    kind=EventKind.FLAG.value,
                    state=EpisodeState.NEW.value if new_episode else EpisodeState.ONGOING.value,
                    hints=hints,
                    # On a re-surface, say how many checks went by while we stayed quiet.
                    repeats=0 if new_episode else int(suppressed.get(name) or 0),
                    since_ts=episode.get("since"),
                )
                results.append(ch.emit(evt))
                last_emit[name] = now
                suppressed[name] = 0

        # GC episodes for sensors no longer in the registry (disabled or removed from
        # config). Their state — including any ack_until — would otherwise persist in
        # the dedupe file forever, since the recovery path that pops an episode only
        # runs for sensors that still poll. Keyed on the registry, not this tick's
        # signals, so a sensor that merely returned nothing this cycle is not evicted.
        known = {getattr(s, "id", None) for s in self.sensors}
        for sid in [k for k in sensors_state if k not in known]:
            sensors_state.pop(sid, None)
            dirty = True

        if dirty:
            try:
                self._save_state(state)
            except Exception:
                log.exception("foreman dedupe save failed")

        return {
            "ok": True,
            "signal_count": len(signals),
            "quiet_hours": quiet,
            "worst": None
            if worst is None
            else {"severity": worst.severity, "headline": worst.headline},
            "senior": self.cfg.senior_id(),
            "channel_results": results,
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bus = ForemanBus()
    # Allow one-shot even if disabled (ops / dry-run)
    import sys

    force = "--force" in sys.argv
    if force:
        bus.cfg.raw["enabled"] = True
    print(bus.tick())


if __name__ == "__main__":
    main()
