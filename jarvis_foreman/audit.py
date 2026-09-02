"""Durable audit trails — one JSONL file per day under data/<stream>_audit/.

Two streams: "junior" (what the monitor saw + said, every tick) and "senior"
(each escalation Senior analyzed: verdict, root cause, recommended fix). Distinct
on purpose from data/jarvis_foreman_events.jsonl, which is the *hot* feed the HUD
tails and which append_capped() trims to its newest ~256 KB. These are the
permanent record: uncapped, kept, so you can go back and audit. Daily rotation
keeps any single file small; retention (if ever needed) prunes whole day-files,
never trims lines.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis_foreman.audit")

_STREAMS = ("junior", "senior")


def _audit_dir(cfg, stream: str = "junior") -> Path:
    stream = stream if stream in _STREAMS else "junior"
    return Path(cfg.path).parent / f"{stream}_audit"


def _day_file(cfg, day: str | None = None, stream: str = "junior") -> Path:
    day = day or datetime.now().strftime("%Y-%m-%d")
    return _audit_dir(cfg, stream) / f"{day}.jsonl"


def append_audit(cfg, entry: dict[str, Any], stream: str = "junior") -> None:
    """Append one record to today's file for `stream`. Best-effort; never raises."""
    try:
        path = _day_file(cfg, stream=stream)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.time(), **entry}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        log.debug("%s audit append failed", stream, exc_info=True)


def read_audit(cfg, day: str | None = None, limit: int = 200, stream: str = "junior") -> list[dict[str, Any]]:
    """Newest-last entries from one day-file (defaults to today), capped to `limit`."""
    path = _day_file(cfg, day, stream=stream)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def list_days(cfg, limit: int = 30, stream: str = "junior") -> list[str]:
    """Available audit days for `stream`, newest first (YYYY-MM-DD stems)."""
    d = _audit_dir(cfg, stream)
    if not d.is_dir():
        return []
    days = sorted((p.stem for p in d.glob("*.jsonl")), reverse=True)
    return days[: max(1, limit)]
