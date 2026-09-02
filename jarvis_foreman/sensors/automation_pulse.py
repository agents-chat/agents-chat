"""Read recent automation_runs health from Agent Chat SQLite if present."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .base import Sensor, Signal


def _clip(text: str, limit: int) -> str:
    """Trim on a word boundary. `err[:48]` cut mid-word ("...and this sweep is iden").

    Also flattens whitespace and collapses `---` runs. This text is an agent-authored
    error string — it can carry whatever an agent read out of an email or a web page —
    and it ends up inside a fenced block in a headless agent's instructions. Newlines
    and dash runs are the two ways a payload could try to break out of that fence.
    """
    text = " ".join((text or "").split())
    text = re.sub(r"-{2,}", "-", text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(",;:.—-") + "…"


def _default_db() -> Path:
    env = os.environ.get("AGENT_CHAT_DB")
    if env:
        return Path(env)
    # Common locations under the app tree
    root = Path(__file__).resolve().parent.parent.parent
    for cand in (
        root / "data" / "agent-chat.sqlite3",
        root / "data" / "app.sqlite",
    ):
        if cand.exists():
            return cand
    return root / "data" / "agent-chat.sqlite3"


class AutomationPulseSensor(Sensor):
    id = "automation_pulse"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        db = Path(sc.get("db_path") or _default_db())
        hours = float(sc.get("lookback_hours", 24))
        if not db.exists():
            return [
                Signal(
                    sensor_id=self.id,
                    severity="green",
                    headline="automation_pulse: no db yet",
                    detail={"db": str(db)},
                )
            ]
        since = time.time() - hours * 3600
        # The app writes started_ts/finished_ts in MILLISECONDS (int(time.time()*1000)),
        # but `since` is in seconds. Comparing the two directly makes every historical
        # run pass the "last N hours" window (a ms value is ~1000x a seconds cutoff), so
        # the sensor counted the whole table — 451 lifetime runs, 14 long-closed failures
        # from a June safety-audit sweep — as if they were current, firing a false RED.
        # Normalize any ms-scale timestamp back to seconds at query time. 1e11 is the
        # boundary: seconds-now ~1.78e9 stays as-is, ms-now ~1.78e12 gets divided.
        _ts_secs = (
            "(CASE WHEN COALESCE(finished_ts, started_ts, 0) > 100000000000 "
            "THEN COALESCE(finished_ts, started_ts, 0) / 1000 "
            "ELSE COALESCE(finished_ts, started_ts, 0) END)"
        )
        # Business findings often land as health=red + status=ok (agent said "this is hot").
        # Those are not infrastructure crashes. Default: treat finding-red as amber; hard
        # failures (status failed / failed_hard / error) stay red.
        findings_as_amber = sc.get("findings_as_amber", True)
        # A health=amber + status=ok run is the automation *working* and flagging a
        # routine business finding — e.g. the 3x/day marketing-mailbox check reporting
        # "no urgent items, but new unread mail arrived". That is not a degraded
        # schedule, and because such findings are almost always present it would pin the
        # Automations board amber forever (the "never returns to green" anti-pattern this
        # sensor already guards against for stale failures). Those findings reach Owner
        # through the automation's own notify path; the foreman board is for infra
        # health. Default: treat a clean-status amber finding as informational (ok).
        # Hard failures (status failed/error, health failed_hard/error) are unaffected.
        amber_findings_as_ok = sc.get("amber_findings_as_ok", True)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='automation_runs'"
            )
            if not cur.fetchone():
                con.close()
                return [
                    Signal(
                        sensor_id=self.id,
                        severity="green",
                        headline="automation_runs table absent",
                        detail={},
                    )
                ]
            cur.execute("PRAGMA table_info(automation_runs)")
            cols = {r[1] for r in cur.fetchall()}

            if "health" in cols:
                grouped_sql = "".join((
                    "SELECT health, status, COUNT(*) AS n FROM automation_runs WHERE ",
                    _ts_secs,
                    " >= ? GROUP BY health, status",
                ))
                cur.execute(
                    grouped_sql,
                    (since,),
                )
            else:
                grouped_sql = "".join((
                    "SELECT NULL AS health, status, COUNT(*) AS n FROM automation_runs WHERE ",
                    _ts_secs,
                    " >= ? GROUP BY status",
                ))
                cur.execute(
                    grouped_sql,
                    (since,),
                )
            rows_grouped = [dict(r) for r in cur.fetchall()]

            # Severity must describe the box NOW, not the last 24 hours. Grouping the
            # whole window (above) answers "did anything go wrong today", which can
            # never return to green once something has: a 09:00 failure pins the board
            # amber until it ages out, so fixing the automation changes nothing and the
            # recovery notice never fires. Current state = the MOST RECENT run of each
            # schedule. Fix it, let it run once, and the flag clears itself.
            _sid = "schedule_id" if "schedule_id" in cols else "NULL"
            _health = "health" if "health" in cols else "NULL"
            # Each inserted expression is selected from the fixed literals above;
            # no config or database value becomes SQL text.
            latest_sql = "".join((
                "SELECT COALESCE(", _sid, ", -1) AS schedule_id, COALESCE(",
                _health, ", '') AS health, COALESCE(status, '') AS status, ",
                _ts_secs, " AS ts FROM automation_runs WHERE ", _ts_secs,
                " >= ? ORDER BY ts ASC",
            ))
            cur.execute(
                latest_sql,
                (since,),
            )
            latest_by_sched: dict[int, dict] = {}
            for r in cur.fetchall():
                # ORDER BY ts ASC, so the last write for a schedule wins.
                latest_by_sched[int(r["schedule_id"])] = dict(r)
            rows = rows_grouped

            # Top bad schedules for a useful headline (not just "451 runs").
            # Every column here must be guarded against `cols`. `health`, `error` and
            # `headline` used to be interpolated unconditionally: on a table without
            # them sqlite raises "no such column", the whole poll lands in the except
            # below, and the sensor reports amber "automation_pulse read failed" — a
            # phantom flag on a perfectly healthy box, which no fix could ever clear.
            _tag = "COALESCE(NULLIF(health, ''), status)" if "health" in cols else "status"
            _err = "MAX(COALESCE(error, ''))" if "error" in cols else "''"
            _headline = "MAX(COALESCE(headline, ''))" if "headline" in cols else "''"
            _bad_health = (
                " OR lower(COALESCE(health, '')) IN "
                "('red', 'amber', 'failed', 'failed_hard', 'held', 'error')"
                if "health" in cols else ""
            )
            top_bad: list[dict] = []
            if "schedule_id" in cols:
                top_bad_sql = "".join((
                    "SELECT schedule_id, ", _tag, " AS tag, status, COUNT(*) AS n, ",
                    _err, " AS sample_error, ", _headline,
                    " AS sample_headline FROM automation_runs WHERE ", _ts_secs,
                    " >= ? AND (lower(COALESCE(status, '')) IN "
                    "('failed', 'error', 'failed_hard') ", _bad_health,
                    ") GROUP BY schedule_id, tag, status ORDER BY n DESC LIMIT 12",
                ))
                cur.execute(
                    top_bad_sql,
                    (since,),
                )
                top_bad = [dict(r) for r in cur.fetchall()]

            # Optional names from skill_schedules
            name_by_id: dict[int, str] = {}
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='skill_schedules'"
            )
            if cur.fetchone() and top_bad:
                ids = sorted(
                    {
                        int(r["schedule_id"])
                        for r in top_bad
                        if r.get("schedule_id") is not None
                    }
                )
                if ids:
                    cur.execute(
                        "SELECT id, name, series_chat_id FROM skill_schedules "
                        "WHERE id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))",
                        (json.dumps(ids),),
                    )
                    for r in cur.fetchall():
                        label = (r["name"] or "").strip()
                        if not label and r["series_chat_id"] is not None:
                            label = f"chat#{r['series_chat_id']}"
                        name_by_id[int(r["id"])] = label or f"sched#{r['id']}"

            con.close()
        except Exception as e:
            return [
                Signal(
                    sensor_id=self.id,
                    severity="amber",
                    headline="automation_pulse read failed",
                    detail={"error": str(e)},
                )
            ]

        red_on = set(sc.get("red_on") or ["red", "failed_hard"])
        amber_on = set(sc.get("amber_on") or ["amber", "failed", "held"])

        def _classify(row: dict) -> str:
            """One run -> 'fail' | 'finding' | 'amber' | 'ok'."""
            health = (row.get("health") or "").lower().strip()
            status = (row.get("status") or "").lower().strip()
            tag = health or status
            if status in ("failed", "error", "failed_hard") or health in ("failed_hard", "error"):
                return "fail"
            if health in red_on or health == "red":
                return "finding"
            # Clean-status business finding (the schedule ran fine and just flagged a
            # routine amber). Demote to ok so it doesn't hold the infra board open.
            if (
                amber_findings_as_ok
                and health == "amber"
                and status in ("", "ok", "done", "success")
            ):
                return "ok"
            if (
                health in amber_on
                or status in amber_on
                or status == "stopped"
                or tag in amber_on
            ):
                return "amber"
            return "ok"

        # Window counts stay as CONTEXT for the headline ("across N runs / 24h") …
        total = sum(int(r.get("n") or 0) for r in rows)
        bad = [r for r in rows if _classify(r) != "ok"]

        # … but severity comes from current state: how each schedule stands right now.
        # A schedule that failed this morning and has since run clean is not a problem.
        n_failed = n_finding_red = n_amber = 0
        unhealthy: set[int] = set()
        for sid, run in latest_by_sched.items():
            kind = _classify(run)
            if kind == "ok":
                continue
            unhealthy.add(sid)
            if kind == "fail":
                n_failed += 1
            elif kind == "finding":
                n_finding_red += 1
            else:
                n_amber += 1

        if n_failed > 0:
            worst = "red"
        elif n_finding_red > 0 and not findings_as_amber:
            worst = "red"
        elif n_finding_red > 0 or n_amber > 0:
            worst = "amber"
        else:
            worst = "green"

        # Build short offender list: "sched#16×20 (stale email…)". Only schedules that
        # are unhealthy *right now* get named — a schedule that misbehaved earlier and
        # has since run clean is history, not an offender, and must not hold the flag
        # open or keep re-notifying through the dedupe key.
        offenders: list[str] = []
        by_sched: dict[int, int] = {}
        err_by_sched: dict[int, str] = {}
        for r in top_bad:
            sid = r.get("schedule_id")
            if sid is None or int(sid) not in unhealthy:
                continue
            sid = int(sid)
            by_sched[sid] = by_sched.get(sid, 0) + int(r.get("n") or 0)
            err = (r.get("sample_error") or r.get("sample_headline") or "").strip()
            if err and sid not in err_by_sched:
                err_by_sched[sid] = _clip(err, 48)

        for sid, n in sorted(by_sched.items(), key=lambda kv: -kv[1])[:3]:
            label = name_by_id.get(sid) or f"sched#{sid}"
            bit = f"{label}×{n}"
            if err_by_sched.get(sid):
                bit += f" ({err_by_sched[sid]})"
            offenders.append(bit)

        if worst == "green":
            return [
                Signal(
                    sensor_id=self.id,
                    severity="green",
                    headline=(
                        f"automations ok ({total} run{'' if total == 1 else 's'} "
                        f"/ {hours:.0f}h)"
                    ),
                    dedupe_key="automations:ok",
                    detail={"rows": rows},
                )
            ]

        # The headline is a summary, nothing else. Offenders live in `detail` and are
        # rendered as bullets by the chat card — jamming them in here produced a
        # 220-char string that got clipped mid-word and read aloud by the TTS voice,
        # phone numbers and all.
        # These counts are SCHEDULES standing unhealthy right now, not runs. Say so —
        # "2 finding-red across 8 runs" invited reading them as run tallies.
        parts = []
        if n_failed:
            parts.append(f"{n_failed} failed")
        if n_finding_red:
            parts.append(f"{n_finding_red} finding-red")
        if n_amber:
            parts.append(f"{n_amber} amber")
        counts = ", ".join(parts) if parts else "degraded"
        n_sched = len(latest_by_sched)
        head = (
            f"automations {worst} — {counts} of {n_sched} "
            f"schedule{'' if n_sched == 1 else 's'} "
            f"({total} run{'' if total == 1 else 's'} / {hours:.0f}h)"
        )

        # The condition is *which schedules are unhealthy*, not how many times they
        # have run. `total` climbs with every unrelated automation in the 24h window,
        # so keying on the headline re-notifies forever. Keying on the offender set
        # means: tell me when a new schedule breaks, not when the counter moves.
        # Severity is excluded on purpose — amber↔red on the same offenders is one
        # episode escalating, which the bus tracks separately.
        dedupe_key = "automations:" + ",".join(str(sid) for sid in sorted(unhealthy))

        return [
            Signal(
                sensor_id=self.id,
                severity=worst,
                headline=head[:220],
                dedupe_key=dedupe_key,
                detail={
                    "bad": bad,
                    "rows": rows,
                    "top_bad": top_bad,
                    "n_failed": n_failed,
                    "n_finding_red": n_finding_red,
                    "n_amber": n_amber,
                    "findings_as_amber": findings_as_amber,
                    "offenders": offenders,
                },
            )
        ]
