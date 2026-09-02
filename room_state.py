"""Room work state — evidence, Monday notes, one job, and the shelf.

No model calls. Cards and files are assembled from structured fields already
on disk. The user sees one or two plain sentences, then optional details.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


HANDOFF_NAME = "handoff.md"
HANDOFF_CONFLICT_NAME = "agent-chat-handoff.md"
HANDOFF_MARKER = "<!-- Agent Chat room handoff -->"
HANDOFF_LINE_CAP = 40
STORY_CAP = 360
NEXT_CAP = 280
FIELD_CAP = 240
STATUS_DRAFT = "draft"
STATUS_KEPT = "kept"
SHELF_PAUSED = "paused"
SHELF_RESUMED = "resumed"
SHELF_DROPPED = "dropped"
EVIDENCE_KINDS = frozenset({"", "file", "test", "user"})

# User changed the goal mid-flight. Conservative on purpose — "also check X"
# must not freeze a running turn.
_GOAL_CHANGE_RE = re.compile(
    r"\b("
    r"stop(?:\s+that)?|wait|hold on|never ?mind|forget (?:that|it)|"
    r"change of plans|scrap that|instead|actually[,:]|"
    r"not that|cancel (?:that|it|the)|different (?:idea|plan|goal)|"
    r"write the .+ instead|do this instead|new goal"
    r")\b",
    re.I,
)
_EVERYONE_THINK_RE = re.compile(
    r"\b(what does everyone think|all of you|everyone weigh in|"
    r"round[\s-]?robin|go around the (?:room|table))\b",
    re.I,
)


def now_ms() -> int:
    return int(time.time() * 1000)


def _clean(text: Any, cap: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).replace("`", "'").strip()
    if not re.search(r"\w", t):
        return ""
    return t[:cap].strip()


def _json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS room_handoffs (
            chat_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'draft',
            gen INTEGER NOT NULL DEFAULT 1,
            story TEXT NOT NULL DEFAULT '',
            next_line TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            decided TEXT NOT NULL DEFAULT '',
            open_loop TEXT NOT NULL DEFAULT '',
            who_owes TEXT NOT NULL DEFAULT '',
            files_json TEXT NOT NULL DEFAULT '[]',
            carried_forward TEXT NOT NULL DEFAULT '',
            source_chat_id INTEGER,
            updated_ts INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS room_shelves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            owner_user_id INTEGER,
            agent_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            old_goal TEXT NOT NULL DEFAULT '',
            new_goal TEXT NOT NULL DEFAULT '',
            story TEXT NOT NULL DEFAULT '',
            next_line TEXT NOT NULL DEFAULT '',
            file_rel TEXT NOT NULL DEFAULT '',
            last_command TEXT NOT NULL DEFAULT '',
            last_result TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'paused',
            created_ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_room_shelves_chat
            ON room_shelves(chat_id, created_ts DESC);
        CREATE TABLE IF NOT EXISTS room_assignments (
            chat_id INTEGER PRIMARY KEY,
            agent_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            verb TEXT NOT NULL DEFAULT '',
            quiet_json TEXT NOT NULL DEFAULT '[]',
            updated_ts INTEGER NOT NULL
        );
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(team_decisions)").fetchall()}
    if "evidence_kind" not in cols:
        conn.execute(
            "ALTER TABLE team_decisions ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT ''"
        )
    if "evidence_ref" not in cols:
        conn.execute(
            "ALTER TABLE team_decisions ADD COLUMN evidence_ref TEXT NOT NULL DEFAULT ''"
        )
    if "evidence_ok" not in cols:
        conn.execute(
            "ALTER TABLE team_decisions ADD COLUMN evidence_ok INTEGER NOT NULL DEFAULT 0"
        )


# ------------------------- Evidence gate --------------------------------------

def evaluate_evidence(
    *,
    source: str = "agent",
    evidence_kind: str = "",
    evidence_ref: str = "",
    workspace: Optional[str] = None,
    test_passed: Optional[bool] = None,
) -> dict:
    """Decide whether a write may become ground. No model. Bytes or the owner."""
    kind = (evidence_kind or "").strip().lower()
    if kind not in EVIDENCE_KINDS:
        kind = ""
    ref = _clean(evidence_ref, 240)
    if source == "human":
        return {"kind": "user", "ref": ref, "ok": True, "reason": "you said so"}
    if kind == "user":
        return {
            "kind": "user", "ref": ref, "ok": False,
            "reason": "only the owner can confirm this",
        }
    if kind == "file":
        if not ref or not workspace:
            return {"kind": "file", "ref": ref, "ok": False, "reason": "no file to check"}
        path = Path(workspace) / ref
        try:
            resolved = path.resolve()
            root = Path(workspace).resolve()
            if root not in resolved.parents and resolved != root:
                return {"kind": "file", "ref": ref, "ok": False, "reason": "file outside this chat"}
            if resolved.is_file():
                return {"kind": "file", "ref": ref, "ok": True, "reason": "file is there"}
        except OSError:
            pass
        return {"kind": "file", "ref": ref, "ok": False, "reason": "file not found"}
    if kind == "test":
        if test_passed is True:
            return {"kind": "test", "ref": ref, "ok": True, "reason": "check passed"}
        return {"kind": "test", "ref": ref, "ok": False, "reason": "check has not passed"}
    return {"kind": kind, "ref": ref, "ok": False, "reason": "no proof yet"}


def decision_is_settled(row: dict) -> bool:
    if (row.get("source") or "") == "human":
        return True
    return bool(row.get("evidence_ok"))


def decision_story(statement: str, *, decided_by: str = "", scar: str = "") -> str:
    who = _clean(decided_by, 40)
    body = _clean(statement, 220)
    if not body:
        return ""
    lead = f"{who} checked it: " if who and not who.startswith("human") else ""
    text = lead + body
    if scar:
        text = text.rstrip(".") + ". " + _clean(scar, 140)
    return _clean(text, STORY_CAP)


def decision_next_line(settled: bool) -> str:
    if settled:
        return "This is the standing answer unless you change it."
    return "This is still a guess until a file, a check, or you says otherwise."


# ------------------------- One actor ------------------------------------------

def looks_like_everyone_think(text: str) -> bool:
    return bool(_EVERYONE_THINK_RE.search(text or ""))


def looks_like_goal_change(text: str) -> bool:
    return bool(_GOAL_CHANGE_RE.search(text or ""))


def verb_from_text(text: str) -> str:
    raw = _clean(text, 80)
    if not raw:
        return "take this"
    raw = re.sub(r"^@[\w.-]+\s*", "", raw)
    words = raw.split()
    return " ".join(words[:6]).rstrip(".,;:") or "take this"


def pick_assignment(
    text: str,
    roster: list[str],
    *,
    lead_id: Optional[str] = None,
    mentioned: Optional[list[str]] = None,
    names: Optional[dict[str, str]] = None,
) -> Optional[dict]:
    """One next actor with a verb. None if the roster is empty."""
    order = [t for t in (roster or []) if t]
    if not order:
        return None
    named = [t for t in (mentioned or []) if t in order]
    actor = named[0] if named else (lead_id if lead_id in order else order[0])
    quiet = [t for t in order if t != actor]
    label = (names or {}).get(actor) or actor
    return {
        "agent_id": actor,
        "agent_name": label,
        "verb": verb_from_text(text),
        "quiet": quiet,
    }


def collapse_auto_panel(
    route_mode: str,
    target_ids: list[str],
    *,
    user_mode: str,
    mentioned: Optional[list[str]] = None,
    text: str = "",
    names: Optional[dict[str, str]] = None,
) -> tuple[str, list[str], Optional[dict]]:
    """Preserve the planned route; record an assignment only for a singleton."""
    targets = list(target_ids or [])
    mentioned = list(mentioned or [])
    if len(targets) == 1:
        return route_mode, targets, pick_assignment(
            text,
            targets,
            mentioned=mentioned,
            names=names,
        )
    return route_mode, targets, None


def assignment_strip(asg: dict) -> str:
    name = asg.get("agent_name") or asg.get("agent_id") or "Someone"
    verb = asg.get("verb") or "take this"
    n_quiet = len(asg.get("quiet") or [])
    if n_quiet:
        return f"{name} is on this. The others are standing by unless you ask them in."
    return f"{name} is on this: {verb}."


# ------------------------- Handoff note ---------------------------------------

def build_handoff_copy(
    *,
    goal: str = "",
    decided: str = "",
    open_loop: str = "",
    who_owes: str = "",
) -> tuple[str, str]:
    g = _clean(goal, FIELD_CAP)
    d = _clean(decided, FIELD_CAP)
    o = _clean(open_loop, FIELD_CAP)
    w = _clean(who_owes, FIELD_CAP)
    if d and g:
        story = f"We were working on this: {g.rstrip('.')}. What we actually know: {d.rstrip('.')}."
    elif d:
        story = d if d.endswith(".") else d + "."
    elif g:
        story = f"We were working on this: {g.rstrip('.')}."
    else:
        story = "Nothing durable has been written down yet."
    if o:
        next_line = f"Still open: {o.rstrip('?')}?"
    else:
        next_line = "Nothing else is waiting."
    if w:
        next_line += f" {w.rstrip('.')}."
    else:
        next_line += " Nobody picked up leftover work, so it waits here until you say so."
    return _clean(story, STORY_CAP), _clean(next_line, NEXT_CAP)


def render_handoff_markdown(row: dict) -> str:
    status = "Kept for later" if row.get("status") == STATUS_KEPT else "Draft · this chat only"
    lines = [
        HANDOFF_MARKER,
        "# Where we left off",
        status,
        "",
        row.get("story") or "Nothing durable has been written down yet.",
        "",
        row.get("next_line") or "Nothing else is waiting.",
        "",
        "## We know",
        row.get("decided") or "Nothing settled yet.",
        "",
        "## We don't",
        row.get("open_loop") or "Nothing listed.",
        "",
        "## Files",
    ]
    files = row.get("files") or _json_list(row.get("files_json"))
    if files:
        lines.extend(f"- {f}" for f in files[:8])
    else:
        lines.append("- none yet")
    carried = (row.get("carried_forward") or "").strip()
    if carried:
        lines.extend(["", "## Carried forward", carried])
    text = "\n".join(lines).strip() + "\n"
    clipped = "\n".join(text.splitlines()[:HANDOFF_LINE_CAP])
    return clipped + ("\n" if not clipped.endswith("\n") else "")


def write_handoff_file(workspace: str, markdown: str) -> Optional[str]:
    if not workspace:
        return None
    root = Path(workspace)
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / HANDOFF_NAME
        if path.exists():
            try:
                owned = HANDOFF_MARKER in path.read_text(encoding="utf-8")[:512]
            except OSError:
                return None
            if not owned:
                path = root / HANDOFF_CONFLICT_NAME
        if path.exists():
            try:
                if HANDOFF_MARKER not in path.read_text(encoding="utf-8")[:512]:
                    return None
            except OSError:
                return None
        tmp = root / f".{path.name}.tmp"
        tmp.write_text(markdown, encoding="utf-8")
        os.replace(tmp, path)
        return str(path)
    except OSError:
        return None


def save_handoff(conn: sqlite3.Connection, chat_id: int, fields: dict) -> dict:
    cid = int(chat_id)
    existing = conn.execute(
        "SELECT gen, status, carried_forward, source_chat_id FROM room_handoffs WHERE chat_id=?",
        (cid,),
    ).fetchone()
    prev_gen = int(existing["gen"]) if existing else 0
    status = fields.get("status") or (existing["status"] if existing else STATUS_DRAFT)
    if status == STATUS_KEPT and existing and existing["status"] == STATUS_KEPT:
        # Never clobber a kept note with a quieter draft.
        if fields.get("status") != STATUS_KEPT and not fields.get("force"):
            return get_handoff(conn, cid) or {}
    carried = fields.get("carried_forward")
    if carried is None:
        carried = existing["carried_forward"] if existing else ""
    story, next_line = build_handoff_copy(
        goal=fields.get("goal") or "",
        decided=fields.get("decided") or "",
        open_loop=fields.get("open_loop") or "",
        who_owes=fields.get("who_owes") or "",
    )
    if fields.get("story"):
        story = _clean(fields["story"], STORY_CAP)
    if fields.get("next_line"):
        next_line = _clean(fields["next_line"], NEXT_CAP)
    files = fields.get("files") or []
    if not isinstance(files, list):
        files = []
    files = [_clean(f, 80) for f in files if _clean(f, 80)][:8]
    now = now_ms()
    conn.execute(
        """
        INSERT INTO room_handoffs(
            chat_id, status, gen, story, next_line, goal, decided, open_loop,
            who_owes, files_json, carried_forward, source_chat_id, updated_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            status=excluded.status,
            gen=excluded.gen,
            story=excluded.story,
            next_line=excluded.next_line,
            goal=excluded.goal,
            decided=excluded.decided,
            open_loop=excluded.open_loop,
            who_owes=excluded.who_owes,
            files_json=excluded.files_json,
            carried_forward=excluded.carried_forward,
            source_chat_id=excluded.source_chat_id,
            updated_ts=excluded.updated_ts
        """,
        (
            cid, status, prev_gen + 1, story, next_line,
            _clean(fields.get("goal"), FIELD_CAP),
            _clean(fields.get("decided"), FIELD_CAP),
            _clean(fields.get("open_loop"), FIELD_CAP),
            _clean(fields.get("who_owes"), FIELD_CAP),
            json.dumps(files),
            _clean(carried, 600),
            fields.get("source_chat_id") or (existing["source_chat_id"] if existing else None),
            now,
        ),
    )
    row = get_handoff(conn, cid) or {}
    workspace = fields.get("workspace") or ""
    if workspace and fields.get("write_file"):
        path = write_handoff_file(workspace, render_handoff_markdown(row))
        if path:
            row["path"] = path
    return row


def get_handoff(conn: sqlite3.Connection, chat_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM room_handoffs WHERE chat_id=?", (int(chat_id),)
    ).fetchone()
    if not row:
        return None
    return _handoff_public(row)


def handoff_worth_showing(row: Optional[dict]) -> bool:
    """True only when there is a real verdict or a kept note — not last-message residue."""
    if not row:
        return False
    if (row.get("status") or "") == STATUS_KEPT:
        return True
    if (row.get("carried_forward") or "").strip():
        return True
    if (row.get("decided") or "").strip():
        return True
    return False


def _handoff_public(row) -> dict:
    files = _json_list(row["files_json"])
    public = {
        "chat_id": int(row["chat_id"]),
        "status": row["status"],
        "gen": int(row["gen"]),
        "story": row["story"],
        "next_line": row["next_line"],
        "goal": row["goal"],
        "decided": row["decided"],
        "open_loop": row["open_loop"],
        "who_owes": row["who_owes"],
        "files": files,
        "files_json": row["files_json"],
        "carried_forward": row["carried_forward"],
        "source_chat_id": row["source_chat_id"],
        "updated_ts": int(row["updated_ts"] or 0),
        "kind": "room_handoff",
    }
    public["worth_showing"] = handoff_worth_showing(public)
    return public


def keep_handoff(
    conn: sqlite3.Connection, chat_id: int, *, workspace: str = "",
) -> Optional[dict]:
    row = get_handoff(conn, chat_id)
    if not row:
        return None
    conn.execute(
        "UPDATE room_handoffs SET status=?, updated_ts=? WHERE chat_id=?",
        (STATUS_KEPT, now_ms(), int(chat_id)),
    )
    row = get_handoff(conn, chat_id)
    if row and workspace:
        path = write_handoff_file(workspace, render_handoff_markdown(row))
        if path:
            row["path"] = path
    return row


def carry_handoff(
    conn: sqlite3.Connection, from_chat_id: int, to_chat_id: int, *, workspace: str = "",
) -> Optional[dict]:
    src = get_handoff(conn, from_chat_id)
    if not src:
        return None
    carried = src.get("story") or src.get("decided") or "Brought forward from the last chat."
    return save_handoff(conn, to_chat_id, {
        "status": STATUS_KEPT,
        "goal": src.get("goal") or "",
        "decided": src.get("decided") or "",
        "open_loop": src.get("open_loop") or "",
        "who_owes": "",
        "files": src.get("files") or [],
        "carried_forward": carried,
        "source_chat_id": int(from_chat_id),
        "workspace": workspace,
        "write_file": bool(workspace),
        "story": f"Brought forward. {carried}",
        "next_line": "This is the starting note for the new chat. Add to it as you go.",
        "force": True,
    })


# ------------------------- Shelf ----------------------------------------------

def build_shelf_copy(
    *,
    agent_name: str = "",
    old_goal: str = "",
    new_goal: str = "",
    file_rel: str = "",
    last_result: str = "",
) -> tuple[str, str]:
    who = _clean(agent_name, 40) or "Someone"
    old = _clean(old_goal, 120) or "the last job"
    new = _clean(new_goal, 120) or "the new one"
    story = f"{who} was mid-fix on {old} when you switched to {new}."
    if file_rel:
        story += f" The file {file_rel} is unfinished."
    if last_result:
        story += f" Last check: {last_result}."
    next_line = (
        "Leave it here and come back after the new work, or open the file now. "
        "It will not jump back to life on its own."
    )
    return _clean(story, STORY_CAP), _clean(next_line, NEXT_CAP)


def save_shelf(conn: sqlite3.Connection, chat_id: int, fields: dict) -> dict:
    story, next_line = build_shelf_copy(
        agent_name=fields.get("agent_name") or "",
        old_goal=fields.get("old_goal") or "",
        new_goal=fields.get("new_goal") or "",
        file_rel=fields.get("file_rel") or "",
        last_result=fields.get("last_result") or "",
    )
    if fields.get("story"):
        story = _clean(fields["story"], STORY_CAP)
    if fields.get("next_line"):
        next_line = _clean(fields["next_line"], NEXT_CAP)
    cur = conn.execute(
        """
        INSERT INTO room_shelves(
            chat_id, owner_user_id, agent_id, agent_name, old_goal, new_goal,
            story, next_line, file_rel, last_command, last_result, status, created_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(chat_id),
            fields.get("owner_user_id"),
            _clean(fields.get("agent_id"), 40),
            _clean(fields.get("agent_name"), 40),
            _clean(fields.get("old_goal"), FIELD_CAP),
            _clean(fields.get("new_goal"), FIELD_CAP),
            story, next_line,
            _clean(fields.get("file_rel"), 160),
            _clean(fields.get("last_command"), 200),
            _clean(fields.get("last_result"), 80),
            SHELF_PAUSED,
            now_ms(),
        ),
    )
    return get_shelf(conn, int(cur.lastrowid)) or {}


def get_shelf(conn: sqlite3.Connection, shelf_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM room_shelves WHERE id=?", (int(shelf_id),)
    ).fetchone()
    return _shelf_public(row) if row else None


def list_shelves(conn: sqlite3.Connection, chat_id: int, *, open_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM room_shelves WHERE chat_id=?"
    args: list[Any] = [int(chat_id)]
    if open_only:
        sql += " AND status=?"
        args.append(SHELF_PAUSED)
    sql += " ORDER BY created_ts DESC LIMIT 20"
    return [_shelf_public(r) for r in conn.execute(sql, args).fetchall()]


def set_shelf_status(conn: sqlite3.Connection, shelf_id: int, status: str) -> Optional[dict]:
    if status not in {SHELF_PAUSED, SHELF_RESUMED, SHELF_DROPPED}:
        return get_shelf(conn, shelf_id)
    conn.execute(
        "UPDATE room_shelves SET status=? WHERE id=?",
        (status, int(shelf_id)),
    )
    return get_shelf(conn, shelf_id)


def _shelf_public(row) -> dict:
    return {
        "id": int(row["id"]),
        "chat_id": int(row["chat_id"]),
        "agent_id": row["agent_id"],
        "agent_name": row["agent_name"],
        "old_goal": row["old_goal"],
        "new_goal": row["new_goal"],
        "story": row["story"],
        "next_line": row["next_line"],
        "file_rel": row["file_rel"],
        "last_command": row["last_command"],
        "last_result": row["last_result"],
        "status": row["status"],
        "created_ts": int(row["created_ts"] or 0),
        "kind": "room_shelf",
    }


# ------------------------- Assignment persistence -----------------------------

def save_assignment(conn: sqlite3.Connection, chat_id: int, asg: dict) -> dict:
    quiet = asg.get("quiet") or []
    conn.execute(
        """
        INSERT INTO room_assignments(chat_id, agent_id, agent_name, verb, quiet_json, updated_ts)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            agent_id=excluded.agent_id,
            agent_name=excluded.agent_name,
            verb=excluded.verb,
            quiet_json=excluded.quiet_json,
            updated_ts=excluded.updated_ts
        """,
        (
            int(chat_id),
            _clean(asg.get("agent_id"), 40),
            _clean(asg.get("agent_name"), 40),
            _clean(asg.get("verb"), 80),
            json.dumps(list(quiet)),
            now_ms(),
        ),
    )
    return get_assignment(conn, chat_id) or {}


def get_assignment(conn: sqlite3.Connection, chat_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM room_assignments WHERE chat_id=?", (int(chat_id),)
    ).fetchone()
    if not row:
        return None
    return {
        "chat_id": int(row["chat_id"]),
        "agent_id": row["agent_id"],
        "agent_name": row["agent_name"],
        "verb": row["verb"],
        "quiet": _json_list(row["quiet_json"]),
        "updated_ts": int(row["updated_ts"] or 0),
        "strip": assignment_strip({
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "verb": row["verb"],
            "quiet": _json_list(row["quiet_json"]),
        }),
    }


def clear_assignment(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM room_assignments WHERE chat_id=?", (int(chat_id),))


def chat_badges(conn: sqlite3.Connection, chat_id: int) -> list[str]:
    """Quiet marks for the chat list. Only real notes, or paused work."""
    out = []
    hand = get_handoff(conn, chat_id)
    if hand and handoff_worth_showing(hand):
        out.append("Kept note" if hand.get("status") == STATUS_KEPT else "Note")
    shelves = list_shelves(conn, chat_id, open_only=True)
    if shelves:
        out.append("Paused")
    return out
