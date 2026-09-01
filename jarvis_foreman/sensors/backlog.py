"""backlog — queue-depth watcher: are work items piling up faster than they drain?

Two independent queues, either of which going deep is worth a flag:
  * outbox   — pending lines in <data_dir>/jarvis_foreman_chat_outbox.jsonl, the
               foreman's own chat send buffer (non-blank JSONL lines = unsent msgs)
  * items    — automation_items rows still in state='pending' in the live SQLite DB

Severity comes from thresholds only, never from the raw depth churning each tick.

Dedupe discipline (see sensors/base.Signal): key on the SET of queues that are
over the line — "backlog:outbox,items" — never on the counts. Depths drift by a
few every poll; keying on them would mint a fresh cooldown slot each tick and
defeat suppression. Same deep queues = same episode. Green uses a fixed key.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .base import Sensor, Signal

# Reuse the automation sensor's DB resolver so both read the same live store.
try:
    from .automation_pulse import _default_db  # type: ignore
except Exception:  # pragma: no cover - defensive
    def _default_db() -> Path:
        return Path(__file__).resolve().parent.parent.parent / "data" / "neuroblend_v5_15.sqlite3"


class BacklogSensor(Sensor):
    id = "backlog"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        amber_outbox = int(sc.get("amber_outbox", 20))
        red_outbox = int(sc.get("red_outbox", 100))
        amber_items = int(sc.get("amber_items", 50))
        red_items = int(sc.get("red_items", 200))

        try:
            outbox_pending = self._count_outbox()
            pending_items = self._count_pending_items(sc)
        except Exception as e:  # DB locked / IO error → amber, never crash the bus
            return [
                Signal(
                    sensor_id=self.id,
                    severity="amber",
                    headline="backlog check failed",
                    dedupe_key="backlog:check_failed",
                    detail={"error": str(e)},
                )
            ]

        # A queue counts as "over" once it hits amber; red implies over too (guards a
        # misconfig where red < amber). Severity is red if either queue crossed red.
        outbox_red = outbox_pending >= red_outbox
        items_red = pending_items >= red_items
        outbox_over = outbox_red or outbox_pending >= amber_outbox
        items_over = items_red or pending_items >= amber_items

        if not outbox_over and not items_over:
            return [
                Signal(
                    sensor_id=self.id,
                    severity="green",
                    headline=f"backlog clear (outbox {outbox_pending}, items {pending_items})",
                    dedupe_key="backlog:ok",
                    detail={"outbox_pending": outbox_pending, "pending_items": pending_items},
                )
            ]

        severity = "red" if (outbox_red or items_red) else "amber"

        # The condition is *which* queues are deep, not how deep. The offending set
        # drives the episode identity; depths live in detail/offenders only.
        over = []
        if outbox_over:
            over.append("outbox")
        if items_over:
            over.append("items")
        dedupe_key = "backlog:" + ",".join(sorted(over))

        bits: list[str] = []
        offenders: list[str] = []
        if outbox_over:
            bits.append(f"outbox {outbox_pending}")
            offenders.append(
                f"chat outbox: {outbox_pending} pending (amber≥{amber_outbox}, red≥{red_outbox})"
            )
        if items_over:
            bits.append(f"items {pending_items}")
            offenders.append(
                f"automation items: {pending_items} pending (amber≥{amber_items}, red≥{red_items})"
            )
        headline = f"backlog {severity}: " + ", ".join(bits)

        return [
            Signal(
                sensor_id=self.id,
                severity=severity,
                headline=headline[:220],
                dedupe_key=dedupe_key,
                detail={
                    "outbox_pending": outbox_pending,
                    "pending_items": pending_items,
                    "offenders": offenders,
                },
            )
        ]

    def _count_outbox(self) -> int:
        """Non-blank lines in the chat outbox JSONL. Absent file → 0 (nothing queued)."""
        data_dir = Path(self.cfg.path).parent
        p = data_dir / "jarvis_foreman_chat_outbox.jsonl"
        if not p.is_file():
            return 0
        n = 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n

    def _count_pending_items(self, sc) -> int:
        """COUNT(*) of automation_items still pending. Absent DB or table → 0."""
        db = Path(sc.get("db_path") or _default_db())
        if not db.is_file():
            return 0
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='automation_items'"
            )
            if not cur.fetchone():
                return 0
            row = conn.execute(
                "SELECT COUNT(*) FROM automation_items WHERE state=?", ("pending",)
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
