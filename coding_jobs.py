"""Coding jobs — verify-gated work on a project workspace.

A job is not a chat message. It has a type, a crew, a workspace or worktree,
a verify command, and a done-state that only a real command can grant.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar, cast


JOB_TYPES: dict[str, dict[str, Any]] = {
    "build": {
        "id": "build",
        "label": "Add a feature",
        "blurb": "Plan, implement, then review.",
        "implementer_pref": ("codex", "claude", "antigravity", "minimax", "grok"),
        "reviewer_pref": ("claude", "codex"),
        "needs_plan": True,
        "needs_review": True,
        "plan_only": False,
        "auto_continue": True,
    },
    "fix": {
        "id": "fix",
        "label": "Fix a bug",
        "blurb": "One change, then a real test command.",
        "implementer_pref": ("codex", "grok", "claude", "minimax", "antigravity"),
        "reviewer_pref": ("claude", "codex"),
        "needs_plan": False,
        "needs_review": True,
        "plan_only": False,
    },
    "docs": {
        "id": "docs",
        "label": "Needs the live web",
        "blurb": "Use a web-connected coder.",
        "implementer_pref": ("grok", "claude", "codex"),
        "reviewer_pref": ("claude",),
        "needs_plan": False,
        "needs_review": False,
        "plan_only": False,
    },
    "review": {
        "id": "review",
        "label": "Review only",
        "blurb": "No code until you approve a plan.",
        "implementer_pref": (),
        "reviewer_pref": ("claude", "codex"),
        "needs_plan": True,
        "needs_review": True,
        "plan_only": True,
    },
    "rebuild": {
        "id": "rebuild",
        "label": "Rebuild this app",
        "blurb": "Copy Agents Chat into a new folder and improve that copy. The live app is left alone.",
        "implementer_pref": ("codex", "claude", "antigravity", "minimax", "grok"),
        "reviewer_pref": ("claude", "codex"),
        "verifier_pref": ("codex", "claude", "grok"),
        "needs_plan": True,
        "needs_review": True,
        "plan_only": False,
        "loop": True,
        "isolate_copy": True,
        "auto_continue": True,
        "never_merge_live": True,
        "premium": True,
    },
}

DEFAULT_REBUILD_GOAL = (
    "Take this isolated copy of Agents Chat and make a version that is at least "
    "50% better than the original: clearer UX for coding work, fewer dead ends, "
    "a job board a stranger can trust, and a product someone can actually run. "
    "Do not touch the live app. The copy is the product."
)
DEFAULT_MAX_ROUNDS = 6
MAX_ROUNDS_CAP = 12

# Confirmation card, not a quiz. GO accepts these unless the user replies first.
REBUILD_ASSUMPTIONS = (
    "Work happens on a COPY in this project. The live app is not edited.",
    "First pass is the whole product — architecture and UX — not a single bugfix.",
    "Done means tests pass and a local start still comes up.",
)

# Every alternative has to name what is being rebuilt. A bare "rebuild",
# "NN% more", or "more awesome" anywhere in a sentence used to match, so
# ordinary messages ("the rebuild failed last night, any idea why?", "we need
# 20% more headroom", "rebuild the CSS") were held as whole-product rebuild
# jobs and never reached an agent at all.
_APP = r"(?:agent.?chat|agents chat|this app|the app)"
_WHOLE = rf"(?:{_APP}|it|this|everything)"
_REBUILD_RE = re.compile(
    r"\b(?:"
    rf"rebuild\s+(?:the\s+)?{_WHOLE}|"
    rf"rewrite\s+(?:the\s+)?(?:app|agent.?chat)|"
    rf"make\s+{_APP}.{{0,80}}(?:better|awesome|amazing|great)|"
    rf"improve\s+{_APP}|"
    rf"copy\s+(?:of\s+|the\s+)?(?:current\s+)?(?:agent.?chat|agents chat)"
    r")\b",
    re.I,
)
_REVIEW_CHAT_RE = re.compile(
    r"\b(review|seo|audit|critique|proofread|look (over|at) this|tweaks?)\b",
    re.I,
)
_DOCS_RE = re.compile(
    r"\b(look up|latest docs|current docs|according to the docs|web.?search)\b",
    re.I,
)
_FIX_RE = re.compile(
    r"\b(fix|bug|crash|broken|failing test|regression|doesn'?t work)\b",
    re.I,
)
_BUILD_RE = re.compile(
    r"\b(add|implement|feature|build|create|ship|scaffold|"
    r"one[-\s]?page|landing\s*page|website|web\s*site|web\s*page)\b",
    re.I,
)
_CAREER_ADVICE_RE = re.compile(
    r"^\s*(?:"
    r"(?:do|would|could)\s+i\s+(?:fit|qualify)\b|"
    r"am\s+i\s+(?:a\s+)?(?:good\s+)?fit\b|"
    r"how\s+well\s+do(?:es)?\s+my\s+(?:resume|r[\u00e9e]sum[\u00e9e]|background|experience)\s+"
    r"(?:fit|match)\b|"
    r"(?:assess|analy[sz]e|compare)\s+my\s+"
    r"(?:resume|r[\u00e9e]sum[\u00e9e]|background|experience)\s+"
    r"(?:against|for|to)\b"
    r")",
    re.I,
)


def infer_job_type(goal: str) -> Optional[str]:
    """Map a plain ask to a managed job, or None to stay in ordinary chat.

    Rebuild/fix/build/docs become orchestrator-run jobs. Review and SEO-style
    asks stay in the same composer — the user already said the words; they
    should not fill out another form.
    """
    text = (goal or "").strip()
    if not text:
        return None
    # Classify the user's leading request, not incidental vocabulary inside a
    # pasted source. Job descriptions commonly contain words such as "website",
    # "build", and "review"; a career-fit question containing that source is
    # still analysis, not an instruction to open a host coding pipeline.
    if _CAREER_ADVICE_RE.search(text):
        return None
    if _REBUILD_RE.search(text):
        return "rebuild"
    review = bool(_REVIEW_CHAT_RE.search(text))
    fixing = bool(_FIX_RE.search(text))
    building = bool(_BUILD_RE.search(text))
    if review and not fixing and not building:
        return None
    if _DOCS_RE.search(text):
        return "docs"
    if fixing:
        return "fix"
    if building:
        return "build"
    return None

STATUSES = (
    "plan",
    "implement",
    "verify",
    "review",
    "done",
    "blocked",
    "cancelled",
)

GRAPH_VERSION = 2
GRAPH_PHASES = ("plan", "implement", "verify", "review", "done")
GRAPH_NODE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "plan", "phase": "plan", "position": 1,
        "label": "Plan mission", "role_key": "reviewer", "depends_on": (),
    },
    {
        "key": "implement", "phase": "implement", "position": 2,
        "label": "Build change", "role_key": "implementer", "depends_on": ("plan",),
    },
    {
        "key": "verify", "phase": "verify", "position": 3,
        "label": "Verify evidence", "role_key": "verifier", "depends_on": ("implement",),
    },
    {
        "key": "review", "phase": "review", "position": 4,
        "label": "Review change", "role_key": "reviewer", "depends_on": ("verify",),
    },
    {
        "key": "done", "phase": "done", "position": 5,
        "label": "Owner integration", "role_key": "owner", "depends_on": ("review",),
    },
)

# From -> allowed next. User-driven: approve, merge, reject, cancel, retry.
TRANSITIONS: dict[str, frozenset[str]] = {
    "plan": frozenset({"implement", "review", "cancelled", "blocked"}),
    "implement": frozenset({"verify", "blocked", "cancelled"}),
    "verify": frozenset({"review", "implement", "done", "blocked", "cancelled"}),
    "review": frozenset({"done", "blocked", "cancelled", "implement"}),
    "done": frozenset(),
    "blocked": frozenset({"implement", "plan", "cancelled"}),
    "cancelled": frozenset(),
}

# A goal-change freeze can pause any job that is still the live work.
PAUSEABLE_STATUSES = ("plan", "implement", "verify", "review", "blocked")

_MAX_GOAL = 4000
_MAX_COMMAND = 500
_BRANCH_RE = re.compile(r"^ac-job-\d+$")


def job_type_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": spec["id"],
            "label": spec["label"],
            "blurb": spec["blurb"],
            "needs_plan": bool(spec["needs_plan"]),
            "needs_review": bool(spec["needs_review"]),
            "plan_only": bool(spec["plan_only"]),
            "loop": bool(spec.get("loop")),
            "premium": bool(spec.get("premium")),
            "isolate_copy": bool(spec.get("isolate_copy")),
        }
        for spec in JOB_TYPES.values()
    ]


def is_rebuild(job_or_type: Any) -> bool:
    if isinstance(job_or_type, dict):
        return str(job_or_type.get("job_type") or "") == "rebuild"
    return str(job_or_type or "") == "rebuild"


def isolate_product_dir(parent: Path, job_id: int) -> Path:
    return Path(parent) / f"rebuild-{int(job_id)}"


def parse_ship_verdict(text: str) -> str:
    """Reviewer must say SHIP or CONTINUE. Default CONTINUE so we keep looping."""
    lines = [line.strip().upper() for line in (text or "").splitlines() if line.strip()]
    first = lines[0] if lines else ""
    # Every agent also receives the shared bottom-line response contract. Accept
    # that presentation prefix without weakening the fail-closed verdict: the
    # first substantive token must still be SHIP (or an existing ready synonym).
    first = re.sub(
        r"^[#>*_\-\s]*(?:BOTTOM\s+LINE\s*:\s*)?",
        "",
        first,
        count=1,
    )
    if re.match(r"^SHIP(?:\b|\s*:)", first):
        return "ship"
    if first.startswith("READY TO SHIP") or first.startswith("ACCEPT THE PRODUCT"):
        return "ship"
    return "continue"


def clamp_max_rounds(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROUNDS
    return max(1, min(value, MAX_ROUNDS_CAP))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS coding_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            project_id INTEGER,
            job_type TEXT NOT NULL,
            goal TEXT NOT NULL,
            verify_command TEXT NOT NULL DEFAULT '',
            cost_limit_usd REAL,
            status TEXT NOT NULL DEFAULT 'plan',
            crew_json TEXT NOT NULL DEFAULT '{}',
            workspace TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '',
            worktree TEXT NOT NULL DEFAULT '',
            last_command TEXT NOT NULL DEFAULT '',
            last_verify_json TEXT NOT NULL DEFAULT '{}',
            report_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            bg_job_id INTEGER,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            finished_ts INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_coding_jobs_owner_updated
            ON coding_jobs(owner_user_id, updated_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_coding_jobs_chat
            ON coding_jobs(chat_id, updated_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_coding_jobs_project
            ON coding_jobs(project_id, updated_ts DESC);
        CREATE TABLE IF NOT EXISTS coding_job_followups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            turn_id TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_ts INTEGER NOT NULL,
            consumed_ts INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_coding_job_followups_fingerprint
            ON coding_job_followups(job_id, fingerprint);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_coding_job_followups_turn
            ON coding_job_followups(job_id, turn_id) WHERE turn_id != '';
        CREATE INDEX IF NOT EXISTS idx_coding_job_followups_pending
            ON coding_job_followups(job_id, status, id);
        CREATE TABLE IF NOT EXISTS coding_job_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            project_id INTEGER,
            workspace TEXT NOT NULL DEFAULT '',
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'implementer',
            result TEXT NOT NULL,
            verify_command TEXT NOT NULL DEFAULT '',
            created_ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_coding_job_outcomes_ws
            ON coding_job_outcomes(workspace, agent_id, created_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_coding_job_outcomes_project
            ON coding_job_outcomes(project_id, agent_id, created_ts DESC);
        CREATE TABLE IF NOT EXISTS coding_job_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            node_key TEXT NOT NULL,
            phase TEXT NOT NULL,
            position INTEGER NOT NULL,
            label TEXT NOT NULL,
            role_key TEXT NOT NULL,
            agent_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            depends_on_json TEXT NOT NULL DEFAULT '[]',
            attempt INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_ts INTEGER,
            finished_ts INTEGER,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            UNIQUE(job_id, node_key)
        );
        CREATE INDEX IF NOT EXISTS idx_coding_job_nodes_job_position
            ON coding_job_nodes(job_id, position);
        CREATE TABLE IF NOT EXISTS coding_job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            node_key TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            from_value TEXT NOT NULL DEFAULT '',
            to_value TEXT NOT NULL DEFAULT '',
            actor_type TEXT NOT NULL DEFAULT 'system',
            actor_id TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_coding_job_events_job_created
            ON coding_job_events(job_id, created_ts, id);
        CREATE TABLE IF NOT EXISTS orchestrator_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'build',
            lesson TEXT NOT NULL,
            facts_json TEXT NOT NULL DEFAULT '{}',
            created_ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orchestrator_lessons_owner
            ON orchestrator_lessons(owner_user_id, created_ts DESC);
        """
    )
    missing = conn.execute(
        "SELECT j.* FROM coding_jobs j "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM coding_job_nodes n WHERE n.job_id=j.id"
        ")"
    ).fetchall()
    for row in missing:
        _ensure_job_graph(conn, serialize_job(row), event_type="graph_backfilled")
    # Graph metadata is an execution contract, not historical prose. Refresh it
    # on boot so jobs created under an older graph version immediately inherit
    # stronger boundaries without losing attempts, timestamps, or event history.
    for row in conn.execute("SELECT * FROM coding_jobs").fetchall():
        _sync_job_graph(
            conn,
            serialize_job(row),
            reason="graph execution contract refreshed",
        )


def validate_verify_command(command: str) -> tuple[str, str]:
    cleaned = (command or "").strip()
    if len(cleaned) > _MAX_COMMAND:
        return "", f"command too long (max {_MAX_COMMAND} chars)"
    if any(ch in cleaned for ch in "\r\n\x00"):
        return "", "command may not contain newlines or null bytes"
    return cleaned, ""


def validate_goal(goal: str) -> tuple[str, str]:
    cleaned = (goal or "").strip()
    if not cleaned:
        return "", "goal is required"
    if len(cleaned) > _MAX_GOAL:
        return "", f"goal too long (max {_MAX_GOAL} chars)"
    return cleaned, ""


def _instruction_fingerprint(text: str) -> str:
    """Stable identity for retry/double-click deduplication."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def enqueue_instruction(
    conn: sqlite3.Connection,
    job_id: int,
    instruction: str,
    *,
    turn_id: str,
) -> dict[str, Any]:
    """Queue one distinct instruction only while its coding job is active.

    The conditional INSERT closes the terminal-transition race: if completion
    committed first this returns ``accepted=False`` and the caller starts a new
    job; if this INSERT committed first the runner sees a durable pending row.
    """
    cleaned, error = validate_goal(instruction)
    if error:
        return {"accepted": False, "created": False, "duplicate": False, "error": error}
    job = get_job(conn, int(job_id))
    if not job or job.get("status") not in PAUSEABLE_STATUSES:
        return {"accepted": False, "created": False, "duplicate": False, "error": "job closed"}
    fingerprint = _instruction_fingerprint(cleaned)
    if fingerprint == _instruction_fingerprint(job.get("goal") or ""):
        return {"accepted": True, "created": False, "duplicate": True, "error": ""}
    turn_key = str(turn_id or "").strip()[:96]
    placeholders = ",".join("?" for _ in PAUSEABLE_STATUSES)
    cur = conn.execute(
        "INSERT OR IGNORE INTO coding_job_followups"
        "(job_id,owner_user_id,chat_id,turn_id,text,fingerprint,status,created_ts) "
        "SELECT id,owner_user_id,chat_id,?,?,?,'pending',? FROM coding_jobs "
        f"WHERE id=? AND status IN ({placeholders})",
        (
            turn_key, cleaned, fingerprint, int(time.time() * 1000),
            int(job_id), *PAUSEABLE_STATUSES,
        ),
    )
    if int(cur.rowcount or 0) > 0:
        return {"accepted": True, "created": True, "duplicate": False, "error": ""}
    current = get_job(conn, int(job_id))
    if not current or current.get("status") not in PAUSEABLE_STATUSES:
        return {"accepted": False, "created": False, "duplicate": False, "error": "job closed"}
    duplicate = conn.execute(
        "SELECT 1 FROM coding_job_followups "
        "WHERE job_id=? AND (fingerprint=? OR (turn_id!='' AND turn_id=?)) LIMIT 1",
        (int(job_id), fingerprint, turn_key),
    ).fetchone()
    return {
        "accepted": bool(duplicate),
        "created": False,
        "duplicate": bool(duplicate),
        "error": "" if duplicate else "instruction was not queued",
    }


def pending_instructions(
    conn: sqlite3.Connection, job_id: int, *, limit: int = 20,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, job_id, turn_id, text, created_ts FROM coding_job_followups "
        "WHERE job_id=? AND status='pending' ORDER BY id LIMIT ?",
        (int(job_id), max(1, min(int(limit), 50))),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_instructions_applied(
    conn: sqlite3.Connection, job_id: int, instruction_ids: list[int],
) -> int:
    ids = list(dict.fromkeys(
        int(item) for item in instruction_ids if isinstance(item, (int, str)) and str(item).isdigit()
    ))[:50]
    ids = [item for item in ids if item > 0]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        "UPDATE coding_job_followups SET status='applied', consumed_ts=? "
        f"WHERE job_id=? AND status='pending' AND id IN ({placeholders})",
        (int(time.time() * 1000), int(job_id), *ids),
    )
    return int(cur.rowcount or 0)


def can_transition(current: str, nxt: str) -> bool:
    return nxt in TRANSITIONS.get(current, frozenset())


_JSON_DEFAULT = TypeVar("_JSON_DEFAULT", dict, list)


def _parse_json(raw: Any, default: _JSON_DEFAULT) -> _JSON_DEFAULT:
    if isinstance(raw, type(default)):
        return cast(_JSON_DEFAULT, raw)
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default
    return cast(_JSON_DEFAULT, parsed) if isinstance(parsed, type(default)) else default


def serialize_job(row: sqlite3.Row | dict) -> dict[str, Any]:
    get = row.keys if hasattr(row, "keys") else None
    data = {k: row[k] for k in (get() if get else row.keys())} if not isinstance(row, dict) else dict(row)
    crew = _parse_json(data.get("crew_json"), {})
    verify = _parse_json(data.get("last_verify_json"), {})
    report = _parse_json(data.get("report_json"), {})
    spec = JOB_TYPES.get(str(data.get("job_type") or ""), {})
    return {
        "id": int(data["id"]),
        "owner_user_id": int(data["owner_user_id"]) if data.get("owner_user_id") is not None else None,
        "chat_id": int(data["chat_id"]),
        "project_id": int(data["project_id"]) if data.get("project_id") is not None else None,
        "job_type": data.get("job_type"),
        "type_label": spec.get("label") or data.get("job_type"),
        "goal": data.get("goal") or "",
        "verify_command": data.get("verify_command") or "",
        "cost_limit_usd": data.get("cost_limit_usd"),
        "status": data.get("status") or "",
        "crew": crew,
        "workspace": data.get("workspace") or "",
        "branch": data.get("branch") or "",
        "worktree": data.get("worktree") or "",
        "last_command": data.get("last_command") or "",
        "last_verify": verify,
        "report": report,
        "error": data.get("error") or "",
        "bg_job_id": data.get("bg_job_id"),
        "created_ts": data.get("created_ts"),
        "updated_ts": data.get("updated_ts"),
        "finished_ts": data.get("finished_ts"),
        "needs_plan": bool(spec.get("needs_plan")),
        "needs_review": bool(spec.get("needs_review")),
        "plan_only": bool(spec.get("plan_only")),
        "loop": bool(spec.get("loop") or report.get("loop")),
        "auto_continue": bool(spec.get("auto_continue")),
        "premium": bool(spec.get("premium")),
        "never_merge_live": bool(spec.get("never_merge_live")),
        "product_path": report.get("product_path") or "",
        "round": int(report.get("round") or 1),
        "max_rounds": int(report.get("max_rounds") or DEFAULT_MAX_ROUNDS),
        "git": bool(data.get("branch")),
        "ping_telegram": bool(report.get("ping_telegram")),
        "need_you": bool(report.get("need_you")),
        "need_you_parked": bool(report.get("need_you_parked")),
        "diff_stat": str(report.get("diff_stat") or ""),
        "how_to_run": str(report.get("how_to_run") or ""),
    }


def _graph_active_phase(job: dict[str, Any]) -> str:
    status = str(job.get("status") or "plan")
    if status in GRAPH_PHASES:
        return status
    report = job.get("report") or {}
    if report.get("review"):
        return "review"
    if report.get("verification") or job.get("last_verify"):
        return "verify"
    if report.get("implement") or report.get("task_brief"):
        return "implement"
    return "plan"


def _graph_node_status(
    job: dict[str, Any], phase: str, *, active_phase: str = "",
) -> str:
    current = active_phase if active_phase in GRAPH_PHASES else _graph_active_phase(job)
    current_index = GRAPH_PHASES.index(current)
    node_index = GRAPH_PHASES.index(phase)
    job_status = str(job.get("status") or "plan")
    if job_status == "done":
        return "done"
    if node_index < current_index:
        return "done"
    if node_index > current_index:
        return "queued"
    if job_status == "blocked":
        return "blocked"
    if job_status == "cancelled":
        return "cancelled"
    return "active"


def _graph_node_agent(job: dict[str, Any], role_key: str) -> str:
    if role_key == "owner":
        return ""
    return str((job.get("crew") or {}).get(role_key) or "")


def _graph_node_metadata(
    job: dict[str, Any], role_key: str, phase: str,
) -> dict[str, Any]:
    folder = str(job.get("worktree") or job.get("workspace") or "")
    if role_key == "owner":
        return {
            "access_mode": "owner-gate",
            "enforcement": "owner-action",
            "write_scope": "integration",
        }
    if phase in {"plan", "verify", "review"}:
        return {
            "access_mode": "read-only",
            "enforcement": "bridge-tool-policy",
            "write_scope": "",
        }
    return {
        "access_mode": "writable-workspace",
        "enforcement": "bridge-tool-policy",
        "write_scope": folder,
    }


def _graph_desired_nodes(
    job: dict[str, Any], *, active_phase: str = "",
) -> list[dict[str, Any]]:
    return [
        {
            **definition,
            "agent_id": _graph_node_agent(job, str(definition["role_key"])),
            "status": _graph_node_status(
                job, str(definition["phase"]), active_phase=active_phase,
            ),
            "metadata": _graph_node_metadata(
                job,
                str(definition["role_key"]),
                str(definition["phase"]),
            ),
        }
        for definition in GRAPH_NODE_DEFINITIONS
    ]


def _record_graph_event(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    event_type: str,
    node_key: str = "",
    from_value: str = "",
    to_value: str = "",
    actor_type: str = "system",
    actor_id: str = "",
    detail: Optional[dict[str, Any]] = None,
    created_ts: Optional[int] = None,
) -> None:
    conn.execute(
        "INSERT INTO coding_job_events("
        "job_id,node_key,event_type,from_value,to_value,actor_type,actor_id,"
        "detail_json,created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            int(job_id), str(node_key or "")[:80], str(event_type or "event")[:80],
            str(from_value or "")[:160], str(to_value or "")[:160],
            str(actor_type or "system")[:40], str(actor_id or "")[:100],
            json.dumps(detail or {}),
            int(created_ts or int(time.time() * 1000)),
        ),
    )


def _ensure_job_graph(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    *,
    event_type: str = "graph_created",
) -> bool:
    job_id = int(job["id"])
    exists = conn.execute(
        "SELECT 1 FROM coding_job_nodes WHERE job_id=? LIMIT 1", (job_id,),
    ).fetchone()
    if exists:
        return False
    now = int(time.time() * 1000)
    for node in _graph_desired_nodes(job):
        status = str(node["status"])
        active = status == "active"
        finished = status == "done"
        conn.execute(
            "INSERT INTO coding_job_nodes("
            "job_id,node_key,phase,position,label,role_key,agent_id,status,"
            "depends_on_json,attempt,metadata_json,started_ts,finished_ts,"
            "created_ts,updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, node["key"], node["phase"], int(node["position"]),
                node["label"], node["role_key"], node["agent_id"], status,
                json.dumps(list(node["depends_on"])), 1 if active else 0,
                json.dumps(node["metadata"]), now if active else None,
                now if finished else None, now, now,
            ),
        )
    _record_graph_event(
        conn,
        job_id=job_id,
        event_type=event_type,
        to_value=str(job.get("status") or "plan"),
        detail={"graph_version": GRAPH_VERSION, "node_count": len(GRAPH_NODE_DEFINITIONS)},
        created_ts=now,
    )
    return True


def _sync_job_graph(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    *,
    actor_type: str = "system",
    actor_id: str = "",
    reason: str = "",
) -> None:
    if _ensure_job_graph(conn, job):
        return
    now = int(time.time() * 1000)
    existing = {
        str(row["node_key"]): row
        for row in conn.execute(
            "SELECT * FROM coding_job_nodes WHERE job_id=?", (int(job["id"]),),
        ).fetchall()
    }
    active_phase = ""
    if str(job.get("status") or "") in {"blocked", "cancelled"}:
        current = next((
            row for row in existing.values()
            if str(row["status"] or "") in {"active", "blocked", "cancelled"}
        ), None)
        if current and str(current["phase"] or "") in GRAPH_PHASES:
            # The legacy job status collapses every paused phase into `blocked`
            # or `cancelled`. The durable graph remembers the actual node, even
            # when older report fields from later phases remain populated.
            active_phase = str(current["phase"])
    for desired in _graph_desired_nodes(job, active_phase=active_phase):
        node_key = str(desired["key"])
        row = existing.get(node_key)
        if not row:
            # A partially migrated graph heals one missing canonical node rather
            # than discarding the event history for the nodes already present.
            conn.execute(
                "INSERT INTO coding_job_nodes("
                "job_id,node_key,phase,position,label,role_key,agent_id,status,"
                "depends_on_json,attempt,metadata_json,started_ts,finished_ts,"
                "created_ts,updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    int(job["id"]), node_key, desired["phase"], desired["position"],
                    desired["label"], desired["role_key"], desired["agent_id"],
                    desired["status"], json.dumps(list(desired["depends_on"])),
                    1 if desired["status"] == "active" else 0,
                    json.dumps(desired["metadata"]),
                    now if desired["status"] == "active" else None,
                    now if desired["status"] == "done" else None, now, now,
                ),
            )
            _record_graph_event(
                conn, job_id=int(job["id"]), node_key=node_key,
                event_type="node_repaired", to_value=str(desired["status"]),
                actor_type=actor_type, actor_id=actor_id,
                detail={"reason": reason[:300]} if reason else {}, created_ts=now,
            )
            continue

        prior_status = str(row["status"] or "queued")
        next_status = str(desired["status"])
        prior_agent = str(row["agent_id"] or "")
        next_agent = str(desired["agent_id"] or "")
        attempt = int(row["attempt"] or 0)
        started_ts = row["started_ts"]
        finished_ts = row["finished_ts"]
        if next_status == "active" and prior_status != "active":
            attempt += 1
            started_ts = now
            finished_ts = None
        elif next_status in {"done", "cancelled"} and prior_status != next_status:
            finished_ts = now
        elif next_status not in {"done", "cancelled"} and prior_status != next_status:
            finished_ts = None

        changed = (
            prior_status != next_status
            or prior_agent != next_agent
            or str(row["label"] or "") != str(desired["label"])
            or str(row["role_key"] or "") != str(desired["role_key"])
            or str(row["metadata_json"] or "") != json.dumps(desired["metadata"])
        )
        if changed:
            conn.execute(
                "UPDATE coding_job_nodes SET phase=?,position=?,label=?,role_key=?,"
                "agent_id=?,status=?,depends_on_json=?,attempt=?,metadata_json=?,"
                "started_ts=?,finished_ts=?,updated_ts=? WHERE job_id=? AND node_key=?",
                (
                    desired["phase"], desired["position"], desired["label"],
                    desired["role_key"], next_agent, next_status,
                    json.dumps(list(desired["depends_on"])), attempt,
                    json.dumps(desired["metadata"]), started_ts, finished_ts, now,
                    int(job["id"]), node_key,
                ),
            )
        if prior_status != next_status:
            _record_graph_event(
                conn, job_id=int(job["id"]), node_key=node_key,
                event_type="status_changed", from_value=prior_status,
                to_value=next_status, actor_type=actor_type, actor_id=actor_id,
                detail={"reason": reason[:300]} if reason else {}, created_ts=now,
            )
        if prior_agent != next_agent:
            _record_graph_event(
                conn, job_id=int(job["id"]), node_key=node_key,
                event_type="assignment_changed", from_value=prior_agent,
                to_value=next_agent, actor_type=actor_type, actor_id=actor_id,
                detail={"reason": reason[:300]} if reason else {}, created_ts=now,
            )


def _serialize_graph_node(row: sqlite3.Row | dict) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": int(data["id"]),
        "key": str(data.get("node_key") or ""),
        "phase": str(data.get("phase") or ""),
        "position": int(data.get("position") or 0),
        "label": str(data.get("label") or ""),
        "role_key": str(data.get("role_key") or ""),
        "agent_id": str(data.get("agent_id") or ""),
        "status": str(data.get("status") or "queued"),
        "depends_on": _parse_json(data.get("depends_on_json"), []),
        "attempt": int(data.get("attempt") or 0),
        "metadata": _parse_json(data.get("metadata_json"), {}),
        "started_ts": data.get("started_ts"),
        "finished_ts": data.get("finished_ts"),
        "created_ts": data.get("created_ts"),
        "updated_ts": data.get("updated_ts"),
    }


def _serialize_graph_event(row: sqlite3.Row | dict) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": int(data["id"]),
        "node_key": str(data.get("node_key") or ""),
        "type": str(data.get("event_type") or "event"),
        "from": str(data.get("from_value") or ""),
        "to": str(data.get("to_value") or ""),
        "actor_type": str(data.get("actor_type") or "system"),
        "actor_id": str(data.get("actor_id") or ""),
        "detail": _parse_json(data.get("detail_json"), {}),
        "created_ts": data.get("created_ts"),
    }


def job_graph(
    conn: sqlite3.Connection, job_id: int, *, event_limit: int = 40,
) -> dict[str, Any]:
    nodes = [
        _serialize_graph_node(row)
        for row in conn.execute(
            "SELECT * FROM coding_job_nodes WHERE job_id=? ORDER BY position,id",
            (int(job_id),),
        ).fetchall()
    ]
    status_by_key = {
        str(node.get("key") or ""): str(node.get("status") or "queued")
        for node in nodes
    }
    ready: list[str] = []
    running: list[str] = []
    blocked: dict[str, list[str]] = {}
    for node in nodes:
        blockers = [
            str(dependency)
            for dependency in node.get("depends_on") or []
            if status_by_key.get(str(dependency)) != "done"
        ]
        status = str(node.get("status") or "queued")
        is_ready = status in {"active", "queued"} and not blockers
        if status == "done":
            execution_state = "complete"
        elif status == "cancelled":
            execution_state = "cancelled"
        elif status == "blocked":
            execution_state = "blocked"
        elif status == "active" and not blockers:
            execution_state = "running"
        elif status == "queued" and not blockers:
            execution_state = "ready"
        else:
            execution_state = "waiting"
        node["blocked_by"] = blockers
        node["ready"] = is_ready
        node["execution_state"] = execution_state
        key = str(node.get("key") or "")
        if is_ready:
            ready.append(key)
        if execution_state == "running":
            running.append(key)
        if blockers:
            blocked[key] = blockers

    limit = max(0, min(int(event_limit), 100))
    events: list[dict[str, Any]] = []
    if limit:
        rows = conn.execute(
            "SELECT * FROM coding_job_events WHERE job_id=? "
            "ORDER BY created_ts DESC,id DESC LIMIT ?",
            (int(job_id), limit),
        ).fetchall()
        events = [_serialize_graph_event(row) for row in reversed(rows)]
    return {
        "version": GRAPH_VERSION,
        "nodes": nodes,
        "events": events,
        "scheduler": {
            "mode": "dependency-aware",
            "ready": ready,
            "running": running,
            "blocked": blocked,
        },
    }


def _attach_job_graph(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    *,
    event_limit: int = 40,
) -> dict[str, Any]:
    job["graph"] = job_graph(conn, int(job["id"]), event_limit=event_limit)
    return job


def staff_crew(
    job_type: str,
    available_ids: list[str],
    scores: Optional[dict[str, float]] = None,
    capability_scores: Optional[dict[str, float]] = None,
    read_only_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Pick implementer + reviewer from who is actually available.

    Ranking is deliberately evidence-first and deterministic:

    1. verified outcome score (passes minus failures on this workspace),
    2. learned capability/task-fit score,
    3. the job type's hard-coded preference order.

    The static order therefore remains the exact baseline when no evidence is
    supplied. A learned strength or weakness can break an otherwise equal
    choice, while even very strong learned evidence cannot overrule a different
    verified outcome score. Missing agents are skipped, never invented. When
    ``read_only_ids`` is supplied, protected reviewer/verifier seats are drawn
    only from that explicitly enforceable subset; implementer ranking is unchanged.
    """
    spec = JOB_TYPES.get(job_type) or JOB_TYPES["fix"]
    available = list(dict.fromkeys(
        str(aid).strip() for aid in available_ids if str(aid).strip()
    ))
    available_set = set(available)
    read_only_set = (
        {str(aid).strip() for aid in read_only_ids if str(aid).strip()}
        if read_only_ids is not None else None
    )
    outcome_score_map = scores or {}
    capability_score_map = capability_scores or {}

    def evidence_score(score_map: dict[str, float], aid: str) -> float:
        """Treat malformed/non-finite evidence as neutral, never as a route crash."""
        try:
            value = float(score_map.get(aid, 0.0))
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    def rank(
        prefs: tuple[str, ...],
        exclude: Optional[set[str]] = None,
        *,
        include_unpreferred: bool = True,
        eligible: Optional[set[str]] = None,
    ) -> list[str]:
        blocked = exclude or set()
        baseline = list(prefs) + (available if include_unpreferred else [])
        candidates = list(dict.fromkeys(
            aid for aid in baseline
            if aid not in blocked
            and aid in available_set
            and (eligible is None or aid in eligible)
        ))
        baseline_index = {aid: index for index, aid in enumerate(candidates)}
        return sorted(
            candidates,
            key=lambda aid: (
                -evidence_score(outcome_score_map, aid),
                -evidence_score(capability_score_map, aid),
                baseline_index[aid],
            ),
        )

    def pick(
        prefs: tuple[str, ...],
        exclude: Optional[set[str]] = None,
        *,
        eligible: Optional[set[str]] = None,
    ) -> Optional[str]:
        ranked = rank(prefs, exclude, eligible=eligible)
        return ranked[0] if ranked else None

    implementer = None if spec.get("plan_only") else pick(tuple(spec.get("implementer_pref") or ()))
    reviewer = pick(
        tuple(spec.get("reviewer_pref") or ()),
        exclude={implementer} if implementer else set(),
        eligible=read_only_set,
    )
    taken = {a for a in (implementer, reviewer) if a}
    verifier_pref = tuple(spec.get("verifier_pref") or ())
    verifier = (
        pick(verifier_pref, exclude=taken, eligible=read_only_set)
        if verifier_pref else None
    )
    why = []
    if implementer:
        why.append(f"@{implementer} implements")
    if reviewer:
        why.append(f"@{reviewer} plans and reviews")
    if verifier:
        why.append(f"@{verifier} verifies the build")
    if spec.get("plan_only"):
        why.append("no writer until the plan is approved")
    return {
        "implementer": implementer,
        "reviewer": reviewer,
        "verifier": verifier,
        "alternates": [
            aid for aid in rank(
                tuple(spec.get("implementer_pref") or ()),
                exclude={a for a in (implementer, reviewer, verifier) if a},
                include_unpreferred=False,
            )
        ],
        "why": ", ".join(why) or "no capable agent online",
        "job_type": spec["id"],
    }


def next_implementer(crew: dict, failed_id: str) -> Optional[str]:
    alts = [a for a in (crew.get("alternates") or []) if a and a != failed_id]
    return alts[0] if alts else None


CREW_SEATS = ("implementer", "reviewer", "verifier")


def _crew_why(crew: dict) -> str:
    parts = []
    if crew.get("implementer"):
        parts.append(f"@{crew['implementer']} implements")
    if crew.get("reviewer"):
        parts.append(f"@{crew['reviewer']} plans and reviews")
    if crew.get("verifier"):
        parts.append(f"@{crew['verifier']} verifies the build")
    return ", ".join(parts) or "no capable agent online"


def assign_crew_seat(crew: dict, role: str, agent_id: str) -> tuple[Optional[dict], str]:
    """Put agent_id on role. Vacated seats refill from alternates."""
    seat = str(role or "").strip()
    aid = str(agent_id or "").strip()
    if seat not in CREW_SEATS:
        return None, "unknown seat"
    if not aid:
        return None, "agent required"
    next_crew = dict(crew or {})
    vacated = None
    for key in CREW_SEATS:
        if next_crew.get(key) == aid and key != seat:
            next_crew[key] = None
            vacated = key
    previous = str(next_crew.get(seat) or "")
    next_crew[seat] = aid
    alts = [a for a in (next_crew.get("alternates") or []) if a and a != aid]
    if previous and previous != aid and previous not in alts:
        alts.insert(0, previous)
    if vacated and not next_crew.get(vacated):
        fill = next((a for a in alts if a != aid), None)
        if fill:
            next_crew[vacated] = fill
            alts = [a for a in alts if a != fill]
    next_crew["alternates"] = alts
    next_crew["why"] = _crew_why(next_crew)
    return next_crew, ""


def _git(cwd: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)[:400]
    out = (proc.stdout or proc.stderr or "").strip()
    return int(proc.returncode), out[:800]


def is_git_repo(folder: Path) -> bool:
    code, _ = _git(folder, "rev-parse", "--is-inside-work-tree")
    return code == 0


def current_branch(folder: Path) -> str:
    code, out = _git(folder, "rev-parse", "--abbrev-ref", "HEAD")
    return out if code == 0 else ""


def create_worktree(workspace: Path, job_id: int) -> dict[str, Any]:
    """Detach the job onto its own branch + worktree. No git → work in place."""
    workspace = Path(workspace)
    if not is_git_repo(workspace):
        return {
            "ok": True,
            "git": False,
            "branch": "",
            "worktree": str(workspace),
            "base_branch": "",
            "error": "",
        }
    base = current_branch(workspace) or "HEAD"
    branch = f"ac-job-{int(job_id)}"
    dest = workspace.resolve().parent / ".agentchat-worktrees" / f"job-{int(job_id)}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    code, out = _git(workspace, "worktree", "add", "-b", branch, str(dest), "HEAD", timeout=60)
    if code != 0:
        return {
            "ok": False,
            "git": True,
            "branch": "",
            "worktree": str(workspace),
            "base_branch": base,
            "error": out or "git worktree add failed",
        }
    return {
        "ok": True,
        "git": True,
        "branch": branch,
        "worktree": str(dest),
        "base_branch": base,
        "error": "",
    }


def merge_worktree(workspace: Path, branch: str, worktree: str) -> dict[str, Any]:
    """Capture the reviewed worktree, merge its branch, then clean it up.

    Coding agents normally leave a reviewed diff in the worktree rather than
    creating a commit.  Merging the branch pointer in that state would report
    success while the diff remained only in the worktree, and the force-cleanup
    below would then discard it.  The owner's Merge action is the commit
    boundary: stage and commit the isolated job diff first, and preserve the
    worktree on any failure or residual dirty state.
    """
    workspace = Path(workspace)
    if not branch or not _BRANCH_RE.match(branch):
        return {"ok": False, "error": "refusing to merge an unexpected branch name"}
    if not is_git_repo(workspace):
        return {"ok": True, "merged": False, "error": ""}
    committed = False
    if worktree:
        wt = Path(worktree)
        if wt.exists():
            if not wt.is_dir() or not is_git_repo(wt):
                return {
                    "ok": False, "merged": False,
                    "error": "coding worktree is not a valid git checkout; it was preserved",
                }
            actual_branch = current_branch(wt)
            if actual_branch != branch:
                return {
                    "ok": False, "merged": False,
                    "error": "coding worktree is on an unexpected branch; it was preserved",
                }
            code, dirty = _git(wt, "status", "--porcelain=v1", "--untracked-files=all")
            if code != 0:
                return {
                    "ok": False, "merged": False,
                    "error": dirty or "could not inspect coding worktree; it was preserved",
                }
            if dirty:
                code, out = _git(wt, "add", "--all")
                if code != 0:
                    return {
                        "ok": False, "merged": False,
                        "error": out or "could not stage coding changes; worktree was preserved",
                    }
                code, out = _git(
                    wt,
                    "-c", "user.name=Agent Chat",
                    "-c", "user.email=agent-chat@localhost",
                    "commit", "-m", f"Agent Chat coding job {branch}",
                    timeout=60,
                )
                if code != 0:
                    return {
                        "ok": False, "merged": False,
                        "error": out or "could not commit coding changes; worktree was preserved",
                    }
                committed = True
                # A nested repository/submodule can remain dirty even after the
                # outer commit. Never force-remove a worktree while Git still
                # reports bytes that were not captured by the branch.
                code, remaining = _git(
                    wt, "status", "--porcelain=v1", "--untracked-files=all",
                )
                if code != 0 or remaining:
                    return {
                        "ok": False, "merged": False,
                        "error": (
                            remaining
                            or "could not confirm a clean coding worktree; it was preserved"
                        ),
                    }
    code, out = _git(workspace, "merge", "--no-ff", "--no-edit", branch, timeout=60)
    if code != 0:
        return {"ok": False, "merged": False, "error": out or "merge failed"}
    cleanup_worktree(workspace, branch, worktree)
    return {"ok": True, "merged": True, "committed": committed, "error": ""}


def cleanup_worktree(workspace: Path, branch: str, worktree: str) -> None:
    if worktree:
        wt = Path(worktree)
        _git(workspace, "worktree", "remove", "--force", str(wt), timeout=30)
        if wt.exists() and ".agentchat-worktrees" in wt.parts:
            shutil.rmtree(wt, ignore_errors=True)
    if branch and _BRANCH_RE.match(branch):
        _git(workspace, "branch", "-D", branch)


def outcome_scores(
    conn: sqlite3.Connection,
    *,
    owner_user_id: int,
    workspace: str = "",
    project_id: Optional[int] = None,
) -> dict[str, float]:
    """Owner-scoped passed-minus-failed signal from the last 30 days."""
    cutoff = int(time.time() * 1000) - 30 * 86400 * 1000
    if project_id:
        rows = conn.execute(
            "SELECT agent_id, result FROM coding_job_outcomes "
            "WHERE owner_user_id=? AND project_id=? AND created_ts>=? "
            "AND verify_command NOT LIKE 'no verify command%'",
            (int(owner_user_id), int(project_id), cutoff),
        ).fetchall()
    elif workspace:
        rows = conn.execute(
            "SELECT agent_id, result FROM coding_job_outcomes "
            "WHERE owner_user_id=? AND workspace=? AND created_ts>=? "
            "AND verify_command NOT LIKE 'no verify command%'",
            (int(owner_user_id), workspace, cutoff),
        ).fetchall()
    else:
        return {}
    scores: dict[str, float] = {}
    for row in rows:
        aid = str(row["agent_id"] or "")
        if not aid:
            continue
        if row["result"] == "passed":
            scores[aid] = scores.get(aid, 0.0) + 1.0
        elif row["result"] == "failed":
            scores[aid] = scores.get(aid, 0.0) - 1.5
    return scores


def outcome_learning_line(conn: sqlite3.Connection, owner_user_id: int) -> str:
    cutoff = int(time.time() * 1000) - 30 * 86400 * 1000
    rows = conn.execute(
        "SELECT agent_id, result, COUNT(*) AS n FROM coding_job_outcomes "
        "WHERE owner_user_id=? AND created_ts>=? "
        "AND verify_command NOT LIKE 'no verify command%' "
        "GROUP BY agent_id, result",
        (int(owner_user_id), cutoff),
    ).fetchall()
    if not rows:
        return ""
    by_agent: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_agent.setdefault(str(row["agent_id"]), {"passed": 0, "failed": 0})
        result = str(row["result"] or "")
        if result in bucket:
            bucket[result] += int(row["n"])
    parts = []
    for aid, counts in sorted(by_agent.items(), key=lambda kv: kv[1]["failed"] + kv[1]["passed"], reverse=True)[:5]:
        parts.append(f"@{aid} {counts['passed']} verifies passed / {counts['failed']} failed")
    return "Coding-job outcomes: " + "; ".join(parts) + "."


def create_job(conn: sqlite3.Connection, **fields) -> int:
    now = int(time.time() * 1000)
    cur = conn.execute(
        """
        INSERT INTO coding_jobs (
            owner_user_id, chat_id, project_id, job_type, goal, verify_command,
            cost_limit_usd, status, crew_json, workspace, branch, worktree,
            last_command, last_verify_json, report_json, error, bg_job_id,
            created_ts, updated_ts, finished_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(fields["owner_user_id"]),
            int(fields["chat_id"]),
            fields.get("project_id"),
            fields["job_type"],
            fields["goal"],
            fields.get("verify_command") or "",
            fields.get("cost_limit_usd"),
            fields.get("status") or "plan",
            json.dumps(fields.get("crew") or {}),
            fields.get("workspace") or "",
            fields.get("branch") or "",
            fields.get("worktree") or "",
            "",
            "{}",
            "{}",
            "",
            fields.get("bg_job_id"),
            now,
            now,
            None,
        ),
    )
    job_id = cur.lastrowid
    if job_id is None:
        raise sqlite3.DatabaseError("coding job insert did not return an id")
    row = conn.execute("SELECT * FROM coding_jobs WHERE id=?", (int(job_id),)).fetchone()
    if row:
        _ensure_job_graph(conn, serialize_job(row))
    return int(job_id)


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM coding_jobs WHERE id=?", (int(job_id),)).fetchone()
    return _attach_job_graph(conn, serialize_job(row)) if row else None


def list_jobs(
    conn: sqlite3.Connection,
    owner_user_id: int,
    *,
    chat_id: Optional[int] = None,
    project_id: Optional[int] = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    sql = (
        "SELECT j.* FROM coding_jobs j JOIN chats c ON c.id=j.chat_id "
        "WHERE j.owner_user_id=? AND c.deleted_ts IS NULL"
    )
    args: list[Any] = [int(owner_user_id)]
    if chat_id is not None:
        sql += " AND j.chat_id=?"
        args.append(int(chat_id))
    if project_id is not None:
        sql += " AND j.project_id=?"
        args.append(int(project_id))
    sql += " ORDER BY j.updated_ts DESC LIMIT ?"
    args.append(limit)
    return [
        _attach_job_graph(conn, serialize_job(row), event_limit=12)
        for row in conn.execute(sql, args).fetchall()
    ]


def pause_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error: str = "paused — you changed the goal",
) -> Optional[dict[str, Any]]:
    """Mark a live job cancelled without deleting its files. The shelf keeps them."""
    job = get_job(conn, job_id)
    if not job:
        return None
    if job["status"] in {"done", "cancelled"}:
        return job
    if job["status"] not in PAUSEABLE_STATUSES:
        return None
    if not can_transition(job["status"], "cancelled"):
        return None
    return update_job(
        conn, job_id,
        status="cancelled",
        error=error[:400],
        finished_ts=int(time.time() * 1000),
        _graph_reason=error[:300],
    )


def latest_job_for_chat(
    conn: sqlite3.Connection, chat_id: int, *, statuses: Optional[tuple[str, ...]] = None,
) -> Optional[dict[str, Any]]:
    sql = "SELECT * FROM coding_jobs WHERE chat_id=? "
    args: list[Any] = [int(chat_id)]
    if statuses:
        sql += "AND status IN (" + ",".join("?" * len(statuses)) + ") "
        args.extend(statuses)
    sql += "ORDER BY updated_ts DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    return _attach_job_graph(conn, serialize_job(row), event_limit=12) if row else None


def update_job(conn: sqlite3.Connection, job_id: int, **fields) -> Optional[dict[str, Any]]:
    graph_actor_type = str(fields.pop("_graph_actor_type", "system") or "system")
    graph_actor_id = str(fields.pop("_graph_actor_id", "") or "")
    graph_reason = str(fields.pop("_graph_reason", "") or "")
    allowed = {
        "status", "crew_json", "workspace", "branch", "worktree", "last_command",
        "last_verify_json", "report_json", "error", "bg_job_id", "finished_ts",
        "verify_command", "cost_limit_usd",
    }
    sets = ["updated_ts=?"]
    args: list[Any] = [int(time.time() * 1000)]
    for key, value in fields.items():
        if key == "crew":
            sets.append("crew_json=?")
            args.append(json.dumps(value or {}))
            continue
        if key == "last_verify":
            sets.append("last_verify_json=?")
            args.append(json.dumps(value or {}))
            continue
        if key == "report":
            sets.append("report_json=?")
            args.append(json.dumps(value or {}))
            continue
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        args.append(value)
    args.append(int(job_id))
    conn.execute(f"UPDATE coding_jobs SET {', '.join(sets)} WHERE id=?", args)
    row = conn.execute("SELECT * FROM coding_jobs WHERE id=?", (int(job_id),)).fetchone()
    if not row:
        return None
    job = serialize_job(row)
    _sync_job_graph(
        conn,
        job,
        actor_type=graph_actor_type,
        actor_id=graph_actor_id,
        reason=graph_reason,
    )
    return _attach_job_graph(conn, job)


def record_outcome(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    owner_user_id: int,
    project_id: Optional[int],
    workspace: str,
    agent_id: str,
    role: str,
    result: str,
    verify_command: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO coding_job_outcomes (
            job_id, owner_user_id, project_id, workspace, agent_id, role,
            result, verify_command, created_ts
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            int(job_id),
            int(owner_user_id),
            project_id,
            workspace or "",
            agent_id,
            role,
            result,
            verify_command or "",
            int(time.time() * 1000),
        ),
    )


def build_task_brief(
    job: dict,
    *,
    lessons: Optional[list[str]] = None,
    instructions: Optional[list[dict]] = None,
) -> str:
    """One tight task card so the implementer is not guessing."""
    report = job.get("report") or {}
    plan = str(report.get("plan") or "").strip()
    last_verify = job.get("last_verify") or {}
    verifier_note = str(report.get("verification") or "").strip()
    reviewer_note = str(report.get("review") or "").strip()
    folder = job.get("worktree") or job.get("workspace") or ""
    command = (job.get("verify_command") or "").strip()
    rnd = int(report.get("round") or job.get("round") or 1)
    lines = [
        f"JOB #{job.get('id')} — {job.get('type_label') or job.get('job_type')}",
        f"GOAL: {(job.get('goal') or '').strip()}",
        f"FOLDER: {folder}",
        f"ROUND: {rnd}",
    ]
    if command:
        lines.append(f"DONE WHEN: `{command}` exits 0")
    if is_rebuild(job):
        lines.append("CONSTRAINT: isolated copy only — never edit the live app")
    if plan:
        lines.append("APPROVED PLAN (obey this slice, do not freelance):")
        lines.append(plan[:2500])
    if last_verify and not last_verify.get("passed"):
        verify_cwd = str(last_verify.get("working_dir") or "").strip()
        lines.append(
            "LAST VERIFY RESULT: "
            + str(last_verify.get("summary") or "failed")[:600]
            + (f" (working directory: {verify_cwd})" if verify_cwd else "")
        )
    if verifier_note:
        lines.append("VERIFIER NOTES FROM THE PRIOR ROUND:")
        lines.append(verifier_note[:1800])
    if reviewer_note:
        lines.append("REVIEWER REQUIRED CHANGES FROM THE PRIOR ROUND:")
        lines.append(reviewer_note[:2400])
    if lessons:
        lines.append("LESSONS FROM RECENT AUDITS:")
        lines.extend(f"- {item}" for item in lessons[:4])
    if instructions:
        lines.append("NEW USER INSTRUCTIONS (apply all before verification):")
        for item in instructions[:8]:
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            if text:
                lines.append(f"- {text[:1200]}")
    lines.append("Do only this task. Smallest change. Report what you actually ran.")
    return "\n".join(lines)


def nightly_facts(
    conn: sqlite3.Connection,
    owner_user_id: int,
    *,
    since_ts: int,
) -> dict:
    """Deterministic last-day picture for the orchestrator's night audit."""
    jobs = conn.execute(
        "SELECT id, job_type, status, crew_json, error, updated_ts, finished_ts "
        "FROM coding_jobs WHERE owner_user_id=? AND updated_ts>=? "
        "ORDER BY updated_ts DESC LIMIT 40",
        (int(owner_user_id), int(since_ts)),
    ).fetchall()
    outcomes = conn.execute(
        "SELECT agent_id, role, result FROM coding_job_outcomes "
        "WHERE owner_user_id=? AND created_ts>=? "
        "AND verify_command NOT LIKE 'no verify command%'",
        (int(owner_user_id), int(since_ts)),
    ).fetchall()
    by_agent: dict[str, dict[str, int]] = {}
    for row in outcomes:
        bucket = by_agent.setdefault(str(row["agent_id"]), {"passed": 0, "failed": 0})
        if str(row["result"]) in bucket:
            bucket[str(row["result"])] += 1
    open_jobs = []
    for row in jobs:
        item = {
            "id": int(row["id"]),
            "job_type": row["job_type"],
            "status": row["status"],
            "error": (row["error"] or "")[:160],
        }
        if row["status"] not in ("done", "cancelled"):
            open_jobs.append(item)
    return {
        "jobs_touched": len(jobs),
        "open_jobs": open_jobs,
        "verify_by_agent": by_agent,
        "has_findings": bool(open_jobs or by_agent),
    }


def save_lesson(
    conn: sqlite3.Connection,
    owner_user_id: int,
    lesson: str,
    *,
    kind: str = "build",
    facts: Optional[dict] = None,
) -> None:
    text = (lesson or "").strip()
    if not text:
        return
    conn.execute(
        "INSERT INTO orchestrator_lessons(owner_user_id, kind, lesson, facts_json, created_ts) "
        "VALUES (?,?,?,?,?)",
        (int(owner_user_id), kind, text[:800], json.dumps(facts or {}), int(time.time() * 1000)),
    )


def recent_lessons(
    conn: sqlite3.Connection,
    owner_user_id: int,
    *,
    limit: int = 5,
) -> list[str]:
    limit = max(1, min(int(limit), 12))
    rows = conn.execute(
        "SELECT lesson FROM orchestrator_lessons WHERE owner_user_id=? "
        "ORDER BY id DESC LIMIT ?",
        (int(owner_user_id), limit),
    ).fetchall()
    return [str(r["lesson"]) for r in rows if r["lesson"]]


def lesson_learning_line(conn: sqlite3.Connection, owner_user_id: int) -> str:
    items = recent_lessons(conn, owner_user_id, limit=3)
    if not items:
        return ""
    return "Nightly crew lessons: " + " | ".join(items)


def implementer_prompt(job: dict) -> str:
    report = job.get("report") or {}
    brief = str(report.get("task_brief") or "").strip() or build_task_brief(job)
    handoff = str(report.get("handoff") or "").strip()
    extra = ""
    if handoff:
        extra = (
            "\nHANDOFF — the previous implementer hit a token wall mid-build. "
            "Continue from the files that already exist. Do not restart. "
            f"Last note:\n{handoff[:3000]}\n"
        )
    return (
        "You are the IMPLEMENTER. The orchestrator tasked this exactly. "
        "Do not enlarge the scope.\n\n"
        f"{brief}\n"
        + extra
        + "\nReport the files you changed and the commands you actually ran."
    )


def verifier_prompt(job: dict, result: dict) -> str:
    """Read-only agent pass layered on top of the deterministic command gate."""
    folder = job.get("worktree") or job.get("workspace") or ""
    working_dir = str(result.get("working_dir") or folder)
    command = str(result.get("command") or job.get("verify_command") or "").strip()
    summary = str(result.get("summary") or "No result")[:600]
    output = str(result.get("output_tail") or "").strip()[-3000:]
    gate = "PASSED" if result.get("passed") else "FAILED"
    missing = bool(result.get("configuration_error"))
    return (
        f"You are the VERIFIER on coding job #{job['id']}.\n"
        f"Goal:\n{job.get('goal')}\n\n"
        f"Workspace root: {folder}\n"
        f"Gate working directory: {working_dir}\n"
        "This is a read-only verification turn: inspect the current files and diff, "
        "but do not edit anything. You may run read-only checks. The host result "
        "below is a preliminary gate; the orchestrator will run it again after "
        "your turn so only the final command result can advance the job.\n\n"
        f"PRELIMINARY HOST GATE: {gate}\n"
        f"COMMAND: {command or 'not configured'}\n"
        f"SUMMARY: {summary}\n"
        + (f"OUTPUT TAIL:\n{output}\n" if output else "")
        + (
            "No command was configured or detectable. Name the exact safest command "
            "the owner should use; do not claim the build passed.\n"
            if missing else ""
        )
        + "Start with VERIFIER PASS or VERIFIER FAIL, then report evidence and the next action."
    )


def reviewer_prompt(job: dict) -> str:
    folder = job.get("worktree") or job.get("workspace") or ""
    report = job.get("report") or {}
    verify = job.get("last_verify") or {}
    verifier_note = str(report.get("verification") or "").strip()
    verification_context = (
        "\nAUTHORITATIVE HOST GATE: "
        + ("PASSED" if verify.get("passed") else "FAILED")
        + f" — {str(verify.get('summary') or 'no summary')[:600]}\n"
    )
    if verifier_note:
        verification_context += (
            f"VERIFIER @{report.get('verification_agent') or 'unknown'}:\n"
            f"{verifier_note[:2500]}\n"
        )
    if is_rebuild(job):
        return (
            f"You are the REVIEWER on rebuild job #{job['id']}.\n"
            f"Goal:\n{job.get('goal')}\n\n"
            f"The isolated product is in: {folder}\n"
            "Do not write new features. Judge whether this copy is ready to hand "
            "to the user as a finished product that is materially better than the original.\n"
            + verification_context
            + "First line of your reply MUST be exactly one of:\n"
            "SHIP — the product is ready.\n"
            "CONTINUE — another implementer round is required, and say the next slice.\n"
        )
    auto_managed = bool((job.get("report") or {}).get("auto_managed"))
    verdict = (
        "\nFirst line of your reply MUST be exactly one of:\n"
        "SHIP — the verified change is ready for the owner to merge.\n"
        "CONTINUE — another implementer round is required, and say the next slice.\n"
        if auto_managed else ""
    )
    return (
        f"You are the REVIEWER on coding job #{job['id']}.\n"
        f"Goal:\n{job.get('goal')}\n\n"
        f"The implementer worked in: {folder}\n"
        "Do not write new features. Read the diff, flag risks, and say whether "
        "the verify command's pass is enough to merge. One sharp question only "
        "if you are blocked."
        + verification_context
        + verdict
    )


def planner_prompt(job: dict) -> str:
    extra = ""
    if is_rebuild(job):
        extra = (
            "This is a rebuild: the implementer will work an isolated copy for "
            "several rounds. Sequence the work so each round leaves a runnable product.\n"
        )
    return (
        f"You are planning coding job #{job['id']} "
        f"({job.get('type_label') or job.get('job_type')}).\n"
        f"Goal:\n{job.get('goal')}\n\n"
        + extra
        + "Do NOT write or edit code yet. Restate the goal, list the steps each "
        "with a verifiable done-when, and name the one risk that could waste "
        "the implementer's turn. Keep it short."
    )
