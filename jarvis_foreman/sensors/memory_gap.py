"""memory_gap — internal learn-on-miss telemetry.

The reverse of end-of-chat capture: a MISS teaches the system. Every empty lookup
against shared memory — the owner searched the decisions ledger, or an agent reached
for context mid-task and got nothing — is recorded in the `memory_gaps` table by
app.py. This sensor measures repeated misses for diagnostics, but a failed literal
lookup is not an owner decision or an incident. It therefore remains green even when
rows accumulate; explicit reconciliation in app.py is responsible for resolving them.

Read-only, stdlib + sqlite3 only (same shape as backlog/cost_guard). Dedupe
discipline (see sensors/base.Signal): key on the SET of repeating gap ids, never the
count — the same open holes are one episode, a new hole is genuinely new news.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .base import Sensor, Signal

# Reuse the automation sensor's DB resolver so every sensor reads the same live store.
try:
    from .automation_pulse import _default_db  # type: ignore
except Exception:  # pragma: no cover - defensive
    def _default_db() -> Path:
        return Path(__file__).resolve().parent.parent.parent / "data" / "neuroblend_v5_15.sqlite3"


class MemoryGapSensor(Sensor):
    id = "memory_gap"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        min_ask = int(sc.get("min_ask", 2))   # silent until a gap is asked at least twice
        max_show = int(sc.get("max_show", 8))
        db = Path(sc.get("db_path") or _default_db())

        if not db.is_file():
            return [Signal(self.id, "green", "memory telemetry: store not ready",
                           dedupe_key="memory_gap:no_db", detail={"db": str(db)})]

        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, query_text, ask_count, asked_by FROM memory_gaps "
                    "WHERE status='open' AND ask_count>=? "
                    "ORDER BY ask_count DESC, last_ts DESC LIMIT ?",
                    (min_ask, max_show),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:  # locked / no table yet → internal/green, never alert
            return [Signal(self.id, "green", "memory-gap telemetry unavailable",
                           dedupe_key="memory_gap:check_failed", detail={"error": str(e)})]

        if not rows:
            return [Signal(self.id, "green", "memory telemetry: no repeated misses",
                           dedupe_key="memory_gap:clean", detail={})]

        offenders = [
            f"[{(r['query_text'] or '').strip()[:80]}] x{r['ask_count']}"
            for r in rows
        ]
        n = len(rows)
        headline = f"memory telemetry: {n} repeated lookup miss{'es' if n != 1 else ''}"
        # Episode identity = the SET of open repeating gap ids (not the count, which
        # churns as ask_count ticks up each time the same hole is hit again).
        dedupe_key = "memory_gap:" + ",".join(str(r["id"]) for r in sorted(rows, key=lambda x: x["id"]))
        return [Signal(
            self.id, "green", headline[:220], dedupe_key=dedupe_key,
            detail={"offenders": offenders, "count": n, "internal": True},
        )]
