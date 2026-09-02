"""drift — Junior watches the LIVE build for unexpected edits.

The app is served straight out of this git working tree (see CLAUDE.md), so an
uncommitted change to a bridge script *is* a change to production. This is the
sensor that would have caught an unexplained edit to `scripts/*_agent_bridge.py`:
it reports the tree as drifted the moment a tracked file differs from HEAD, and
again when commits sit unpushed (the change exists only on this box).

Deterministic, read-only, stdlib + git CLI only (psutil is not installed and the
same code runs on the Windows client). Two cheap subprocess probes, each guarded:
  * `git status --porcelain`      — tracked files that differ from HEAD
  * `git rev-list --count @{u}..` — commits ahead of upstream (unpushed)

Dedupe discipline (see sensors/base.Signal): key on the SET of drifted paths, not
on how many there are — the same dirty set is one episode even as it flaps, a new
set is genuinely new news. Counts live in the headline, never the key.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import Sensor, Signal


def _parse_porcelain(out: str, include_untracked: bool) -> list[str]:
    """Changed paths from `git status --porcelain` (v1).

    Each line is `XY <path>`; untracked is `?? <path>`; renames are
    `R  <old> -> <new>` (we report the new path). Untracked is skipped unless
    explicitly opted in — `data/` is gitignored so it never appears here anyway.
    """
    paths: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status, path = line[:2], line[3:]
        if status == "??" and not include_untracked:
            continue
        if " -> " in path:  # rename/copy: keep the destination path
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            paths.append(path)
    return paths


class DriftSensor(Sensor):
    id = "drift"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        repo = Path(sc.get("repo") or Path(__file__).resolve().parents[2])
        timeout = float(sc.get("timeout_sec", 8))
        red_files = int(sc.get("red_files", 25))
        include_untracked = bool(sc.get("include_untracked", False))

        # git missing → nothing we can measure, but that's not an incident.
        if not shutil.which("git"):
            return [Signal(self.id, "green", "code: git not on PATH",
                           dedupe_key="drift:no_git",
                           detail={"note": "git CLI not available", "repo": str(repo)})]

        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=timeout,
            )
        except Exception as e:  # timeout / spawn failure → amber, never crash the bus
            return [Signal(self.id, "amber", "code-changes check failed",
                           dedupe_key="drift:check_failed",
                           detail={"error": str(e), "repo": str(repo)})]

        if proc.returncode != 0:
            # Non-zero almost always means "not a git repo" — don't alarm on it.
            return [Signal(self.id, "green", "code: not a git repo",
                           dedupe_key="drift:no_repo",
                           detail={"note": "git status returned non-zero", "repo": str(repo)})]

        changed = _parse_porcelain(proc.stdout or "", include_untracked)

        # Unpushed commits: only meaningful when an upstream is configured. No
        # upstream → rev-list exits non-zero; treat any failure as "0 ahead".
        ahead = 0
        try:
            p2 = subprocess.run(
                ["git", "-C", str(repo), "rev-list", "--count", "@{u}..HEAD"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=timeout,
            )
            if p2.returncode == 0:
                ahead = int((p2.stdout or "0").strip() or 0)
        except Exception:
            ahead = 0

        if not changed and ahead == 0:
            return [Signal(self.id, "green", "clean — matches HEAD",
                           dedupe_key="drift:clean", detail={"repo": str(repo)})]

        # Severity: red only when the drift is broad (many files). A handful of
        # uncommitted files, or unpushed-but-clean, is amber.
        severity = "red" if len(changed) >= red_files else "amber"

        bits = []
        if changed:
            bits.append(f"{len(changed)} uncommitted")
        if ahead:
            bits.append(f"{ahead} unpushed")
        headline = ", ".join(bits)

        offenders = list(changed[:10])
        if len(changed) > 10:
            offenders.append(f"...+{len(changed) - 10} more")
        if ahead:
            offenders.append(f"{ahead} unpushed commit{'s' if ahead != 1 else ''}")

        # Episode identity = the SET of drifted paths (not the count). A `<unpushed>`
        # token distinguishes "dirty + unpushed" from "dirty + pushed" without letting
        # the churning ahead-count into the key.
        key_parts = sorted(changed)
        if ahead:
            key_parts.append("<unpushed>")
        dedupe_key = "drift:" + ",".join(key_parts)

        return [Signal(
            self.id, severity, headline[:220], dedupe_key=dedupe_key,
            detail={
                "repo": str(repo),
                "changed": changed,
                "changed_count": len(changed),
                "unpushed_commits": ahead,
                "include_untracked": include_untracked,
                "offenders": offenders,
            },
        )]
