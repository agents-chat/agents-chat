"""chat_activity — Junior watches all agent chats and flags anything off.

Deterministic detections over the live SQLite `messages` table (read-only), the
cheap wide net:
  * unanswered   — a chat whose newest message is from the user, idle past an SLA
  * stalled      — newest message is an open hold/question/input_request, unresolved
  * agent_error  — system messages carrying an error / stopped / limit / HTTP 5xx

These become flags that the bus escalates and Senior analyzes. The fuzzy "anything
feels off" net is a separate qwen judgment pass in the loop (jarvis_foreman is kept
free of LLM calls); this sensor is the reliable, always-true backbone.

Dedupe discipline (see sensors/base.Signal): key on the SET of offending chat ids,
never on counts or the headline — the headline's numbers churn every tick.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .base import Sensor, Signal

# Reuse the automation sensor's DB resolver so both read the same live store.
try:
    from .automation_pulse import _default_db  # type: ignore
except Exception:  # pragma: no cover - defensive
    def _default_db() -> str:
        env = os.environ.get("AGENT_CHAT_DB")
        if env:
            return env
        here = Path(__file__).resolve().parent.parent.parent
        for name in ("data/neuroblend_v5_15.sqlite3", "data/neuroblend_v5_13.sqlite3"):
            p = here / name
            if p.is_file() and p.stat().st_size > 0:
                return str(p)
        return str(here / "data" / "neuroblend_v5_15.sqlite3")


# System-message text that means an agent hit trouble. Matched case-insensitively
# via LIKE; kept conservative so ordinary prose ("there was an error in judgment")
# is unlikely — these are the app's own generated status lines.
#
# NB: "<Agent> was stopped." is deliberately NOT here. That line is emitted on a
# *graceful* stop — a user hitting stop, or concurrent sessions cycling a seat —
# not a failed turn (known-benign item B). Counting it as an agent_error forced
# the whole sensor red every time a seat was cycled. Real failures still surface
# via the %error% / %limit% / %HTTP 5% / %unavailable% / %failed to% patterns.
_DEFAULT_ERROR_LIKES = (
    "%error%",
    "%hit your%limit%",
    "%spend limit%",
    "%usage limit%",
    "%session limit%",
    "%HTTP 5%",
    "%unavailable%",
    "%failed to%",
)

# Transient self-healing breadcrumbs that must NOT count as failures. A bridge
# that hits a blip logs "<Agent> hit a temporary error — retrying in 2s (1/2): …"
# before it either recovers or emits the terminal "<Agent> error: …" line. Those
# retry breadcrumbs match %error% too, so a single incident that auto-retried
# twice was logged as three "agent errors" and forced the whole board red (known-
# benign family: transient, self-healed events counted as terminal failures). We
# keep the terminal error — a genuine failed turn still surfaces once — and drop
# only the in-flight retry breadcrumbs.
_TRANSIENT_EXCLUDE_LIKES = (
    "%retrying in%",
    # Failover breadcrumb: "↪️ <Agent> was unavailable (…)" is logged when the
    # orchestrator *successfully* rerouted the turn to a backup model. The turn
    # did not fail; a separate terminal "<Agent> error:" line already carries the
    # real failure. Counting the failover note too double-counts one incident.
    "%↪️%",
    # Job-completion summaries that merely *describe* errors ("… all 14 errors are
    # the same thing …") are reports, not failures. They match %error%/%limit% but
    # are the healthy output of a finished background job.
    "%Background job%done%",
    # Informational status line: "⚠️ … usage limit reached — resets around …" is a
    # heads-up with a reset time, emitted alongside the terminal error line for the
    # same incident. Keep the terminal line, drop the duplicate advisory.
    "%limit reached%resets%",
)

# Provider account-quota phrases. A turn blocked purely by an external plan limit
# (ChatGPT/Codex, Claude Max) is a self-resolving capacity condition that failover
# already handles — it is worth an AMBER heads-up but should not force the whole
# board RED the way a genuine hard failure (HTTP 5xx, "failed to …") does. Matched
# case-insensitively against the error line text.
_SOFT_LIMIT_MARKERS = (
    "usage limit",
    "session limit",
    "spend limit",
    "hit your",
)

# metadata.kind values that mean "waiting on a human / stuck mid-turn".
_HOLD_KINDS = ("hold", "infra_hold", "question", "input_request")


def _is_benign_hold(kind: str, skill_id, submitted, transient) -> bool:
    """True when a hold-kind last message is a normal lifecycle state, not a stall.

    Two families, both false positives that were forcing chat-activity red:
      * skill-launcher forms — an ``input_request`` carrying a ``skill_id`` with
        ``submitted`` still false is a parameter form the user simply hasn't run
        yet (an opt-in skill they chose not to launch), not a stuck agent turn.
      * transient infra holds — ``infra_hold`` with ``transient`` true is a run
        interrupted by a server restart / dropped connection (known-benign B);
        nothing failed, it's just resumable and was never resumed.
    Genuine mid-conversation questions/holds (no skill_id, non-transient) still
    count as stalled.
    """
    if kind == "input_request" and skill_id not in (None, "", 0) and not submitted:
        return True
    if kind == "infra_hold" and transient:
        return True
    return False


class ChatActivitySensor(Sensor):
    id = "chat_activity"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        db = Path(sc.get("db_path") or _default_db())
        if not db.is_file():
            return [Signal(self.id, "green", "no chat store yet", dedupe_key="chat:ok")]

        unans_amber = float(sc.get("unanswered_amber_min", 30)) * 60
        unans_red = float(sc.get("unanswered_red_min", 120)) * 60
        stalled_amber = float(sc.get("stalled_amber_min", 30)) * 60
        stalled_red = float(sc.get("stalled_red_min", 180)) * 60
        error_hours = float(sc.get("error_lookback_hours", 6))
        include_archived = bool(sc.get("include_archived", False))
        likes = list(sc.get("error_likes") or _DEFAULT_ERROR_LIKES)
        excludes = list(sc.get("error_exclude_likes") or _TRANSIENT_EXCLUDE_LIKES)

        now_ms = int(time.time() * 1000)
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                return self._detect(
                    conn, now_ms, unans_amber, unans_red, stalled_amber, stalled_red,
                    error_hours, include_archived, likes, excludes,
                )
            finally:
                conn.close()
        except Exception as e:  # DB locked / schema drift → amber, never crash the bus
            return [Signal(self.id, "amber", f"chat scan failed: {e}",
                           dedupe_key="chat:scan_failed", detail={"error": str(e)})]

    def _detect(self, conn, now_ms, unans_amber, unans_red, stalled_amber,
                stalled_red, error_hours, include_archived, likes,
                excludes=()) -> list[Signal]:
        # Titles for offender labels. Trashed chats are never actionable: their
        # unresolved hold/input cards remain in history for Restore, but Foreman
        # must not keep paging on work the user deliberately removed.
        title_by_id: dict[int, str] = {}
        arch: set[int] = set()
        for r in conn.execute(
            "SELECT id, title, COALESCE(archived,0) a FROM chats "
            "WHERE deleted_ts IS NULL"
        ):
            title_by_id[int(r["id"])] = (r["title"] or f"#{r['id']}")
            if r["a"]:
                arch.add(int(r["id"]))

        def visible(cid: int) -> bool:
            return cid in title_by_id and (include_archived or cid not in arch)

        # Last message per chat via the covering index (chat_id, id) — no table scan.
        last = conn.execute(
            "SELECT m.chat_id cid, m.role role, m.ts ts, "
            "       COALESCE(json_extract(m.metadata_json,'$.kind'),'') kind, "
            "       COALESCE(json_extract(m.metadata_json,'$.resolved'),0) resolved, "
            "       json_extract(m.metadata_json,'$.skill_id') skill_id, "
            "       COALESCE(json_extract(m.metadata_json,'$.submitted'),0) submitted, "
            "       COALESCE(json_extract(m.metadata_json,'$.transient'),0) transient, "
            "       c.targets_json AS targets "
            "FROM messages m JOIN (SELECT chat_id, MAX(id) mid FROM messages GROUP BY chat_id) x "
            "ON m.id = x.mid JOIN chats c ON m.chat_id = c.id"
        ).fetchall()

        unanswered: list[dict] = []   # user waiting
        stalled: list[dict] = []      # open hold/question
        for r in last:
            cid = int(r["cid"])
            if not visible(cid):
                continue
            age = max(0.0, (now_ms - int(r["ts"] or 0)) / 1000.0)

            # If targets_json is "[]" or empty, no agents are pulled into the chat, so it shouldn't trigger an unanswered flag.
            has_targets = r["targets"] not in (None, "", "[]")

            if r["role"] == "user" and age >= unans_amber and has_targets:
                unanswered.append({"chat_id": cid, "title": title_by_id.get(cid, f"#{cid}"), "age_sec": age})
            elif (r["kind"] in _HOLD_KINDS and not r["resolved"] and age >= stalled_amber
                  and not _is_benign_hold(r["kind"], r["skill_id"], r["submitted"], r["transient"])):
                stalled.append({"chat_id": cid, "title": title_by_id.get(cid, f"#{cid}"),
                                "age_sec": age, "kind": r["kind"]})

        # Recent agent/system error lines.
        cutoff = now_ms - int(error_hours * 3600 * 1000)
        errs = conn.execute(
            "SELECT m.chat_id cid, m.agent_id, m.text, m.ts FROM messages AS m "
            "WHERE m.role='system' AND m.ts > ? "
            "AND EXISTS (SELECT 1 FROM json_each(?) AS inc WHERE m.text LIKE inc.value) "
            "AND NOT EXISTS (SELECT 1 FROM json_each(?) AS exc WHERE m.text LIKE exc.value) "
            "ORDER BY m.ts DESC LIMIT 40",
            (cutoff, json.dumps(list(likes)), json.dumps(list(excludes))),
        ).fetchall()
        errors = []
        for e in errs:
            cid = int(e["cid"])
            if not visible(cid):
                continue
            text = (e["text"] or "")
            low = text.lower()
            soft = any(m in low for m in _SOFT_LIMIT_MARKERS)
            errors.append({
                "chat_id": cid, "agent": e["agent_id"] or "system",
                "text": text[:160], "title": title_by_id.get(cid, f"#{cid}"),
                "soft": soft,
            })

        return self._build_signals(
            unanswered, stalled, errors, unans_red, stalled_red,
        )

    def _build_signals(self, unanswered, stalled, errors, unans_red, stalled_red) -> list[Signal]:
        if not unanswered and not stalled and not errors:
            return [Signal(self.id, "green", "all chats moving; nothing waiting",
                           dedupe_key="chat:ok")]

        # Severity: red if any waiter is past the red SLA or there is a HARD error
        # line; amber otherwise. A hard error (HTTP 5xx, "failed to …", a stop) means
        # an agent genuinely failed a turn. Soft provider-quota lines (external plan
        # limits that failover already reroutes and that reset on their own) are worth
        # an amber heads-up but must not pin the board red for hours.
        hard_errors = [e for e in errors if not e.get("soft")]
        red = bool(hard_errors) \
            or any(u["age_sec"] >= unans_red for u in unanswered) \
            or any(s["age_sec"] >= stalled_red for s in stalled)
        severity = "red" if red else "amber"

        bits = []
        if unanswered:
            bits.append(f"{len(unanswered)} unanswered")
        if stalled:
            bits.append(f"{len(stalled)} stalled")
        if hard_errors:
            n = len(hard_errors)
            bits.append(f"{n} agent error{'s' if n != 1 else ''}")
        soft_errors = [e for e in errors if e.get("soft")]
        if soft_errors:
            bits.append(f"{len(soft_errors)} provider-quota hold{'s' if len(soft_errors) != 1 else ''}")
        headline = "chat: " + ", ".join(bits)

        # Offender chat ids drive the episode identity — same troubled set = same
        # episode even as ages/counts drift. Errors keyed by chat too.
        offend_ids = sorted({u["chat_id"] for u in unanswered}
                            | {s["chat_id"] for s in stalled}
                            | {e["chat_id"] for e in errors})
        dedupe_key = "chat:" + ",".join(str(i) for i in offend_ids)

        # Human-readable offender bullets for the card / Senior context.
        offenders: list[str] = []
        for u in unanswered[:4]:
            offenders.append(f"Unanswered {int(u['age_sec']//60)}m — {u['title']}")
        for s in stalled[:4]:
            offenders.append(f"Stalled ({s['kind']}) {int(s['age_sec']//60)}m — {s['title']}")
        for e in errors[:4]:
            offenders.append(f"{e['agent']}: {e['text']}")

        return [Signal(
            self.id, severity, headline[:220], dedupe_key=dedupe_key,
            detail={
                "unanswered": unanswered,
                "stalled": stalled,
                "errors": errors,
                "offenders": offenders[:8],
                "offender_chat_ids": offend_ids,
            },
        )]
