"""backups — Junior watches that *something* is backing the app up, recently.

A freshness watcher, not an integrity checker: for each configured backup
location it finds the newest file mtime (walking into dated sub-folders like
`cutover_backups/<stamp>/…`), then reports on the single freshest backup found
anywhere. If even the most-recent backup is stale, that is the news.

Stdlib only (psutil is not installed and the same code runs on the Windows
client); every filesystem probe is guarded and degrades to a single amber rather
than raising. Keyed on the condition ("stale"/"none"), never on the live age —
the number churns every poll and would defeat dedupe (see sensors/base.Signal).
"""

from __future__ import annotations

import time
from pathlib import Path

from .base import Sensor, Signal


def _humanize_age(seconds: float) -> str:
    """Compact human age for a card bullet: "42m ago" / "6.5h ago" / "3.1d ago"."""
    s = max(0.0, seconds)
    if s < 90 * 60:
        return f"{int(s // 60)}m ago"
    if s < 48 * 3600:
        return f"{s / 3600:.1f}h ago"
    return f"{s / 86400:.1f}d ago"


def _newest_mtime(loc: Path) -> float | None:
    """Most-recent file mtime anywhere under `loc` (recursive).

    Falls back to the directory's own mtime when it holds no readable files (a
    freshly-made but empty backup dir still has *a* timestamp). Per-entry errors
    are swallowed so one unreadable file never sinks the whole poll. Returns None
    only if even stat-ing the location fails.
    """
    newest: float | None = None
    try:
        if loc.is_file():
            return loc.stat().st_mtime
        for p in loc.rglob("*"):
            try:
                if p.is_file():
                    m = p.stat().st_mtime
                    if newest is None or m > newest:
                        newest = m
            except OSError:
                continue
        if newest is None:
            # No files under it — use the folder's own mtime as a weak floor.
            newest = loc.stat().st_mtime
    except OSError:
        return None
    return newest


class BackupsSensor(Sensor):
    id = "backups"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        amber_sec = float(sc.get("amber_hours", 48)) * 3600
        red_sec = float(sc.get("red_hours", 168)) * 3600  # 7d

        # Repo root: the config lives at <root>/data/jarvis_foreman.json, so its
        # grandparent is the app tree that holds the backup folders.
        root = Path(self.cfg.path).resolve().parent.parent

        try:
            configured = sc.get("paths")
            if configured:
                # Configured strings: absolute as-is, relative resolved under root.
                candidates = [
                    (Path(p) if Path(p).is_absolute() else root / p) for p in configured
                ]
            else:
                candidates = [
                    root / "backup",
                    root / "cutover_backups",
                    root / "storage_backups",
                ]

            existing = [c for c in candidates if c.exists()]
            if not existing:
                # Absence of any backup location is itself worth a flag.
                missing = [self._label(c, root) for c in candidates]
                return [Signal(
                    self.id, "amber", "no backups found",
                    dedupe_key="backups:none",
                    detail={
                        "offenders": [f"missing: {m}" for m in missing],
                        "checked": missing,
                    },
                )]

            now = time.time()
            locations: list[dict] = []
            for loc in existing:
                m = _newest_mtime(loc)
                if m is None:
                    continue
                locations.append({
                    "path": self._label(loc, root),
                    "newest_epoch": m,
                    "age_sec": max(0.0, now - m),
                })

            if not locations:
                # Locations exist but nothing readable inside any of them.
                missing = [self._label(c, root) for c in existing]
                return [Signal(
                    self.id, "amber", "no readable backups found",
                    dedupe_key="backups:none",
                    detail={
                        "offenders": [f"unreadable: {m}" for m in missing],
                        "checked": missing,
                    },
                )]

            # Freshness is decided by the single freshest backup anywhere: if even
            # the newest one is stale, everything is stale.
            freshest = min(locations, key=lambda d: d["age_sec"])
            best_age = freshest["age_sec"]
            if best_age > red_sec:
                severity = "red"
            elif best_age > amber_sec:
                severity = "amber"
            else:
                severity = "green"

            # Show every location's newest age, freshest first.
            ordered = sorted(locations, key=lambda d: d["age_sec"])
            offenders = [f"{d['path']}: newest {_humanize_age(d['age_sec'])}" for d in ordered]

            if severity == "green":
                return [Signal(
                    self.id, "green",
                    f"backups fresh (newest {_humanize_age(best_age)})",
                    dedupe_key="backups:ok",
                    detail={"locations": ordered},
                )]

            return [Signal(
                self.id, severity,
                f"backups stale (newest {_humanize_age(best_age)})",
                # Condition, not the age: a stale-backup episode stays one episode
                # as the age creeps upward each poll.
                dedupe_key="backups:stale",
                detail={"locations": ordered, "offenders": offenders},
            )]
        except Exception as e:  # never raise out of a poll
            return [Signal(
                self.id, "amber", "backups check failed",
                dedupe_key="backups:check_failed",
                detail={"error": str(e)},
            )]

    @staticmethod
    def _label(p: Path, root: Path) -> str:
        """Repo-relative label when possible, else the absolute path."""
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)
