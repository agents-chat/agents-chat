"""self_resources — Junior watches his own footprint and brain.

Owner's rule: the monitor is free to run, but it must watch its own resources so it
never becomes the incident. Stdlib only (psutil is not installed, and the same code
runs on the Windows client), so every probe is guarded and degrades to "unknown"
rather than raising:
  * brain      — is the local qwen/Ollama server (Junior's narrator) reachable
  * load       — 1-min load average vs cpu count (POSIX only; skipped on Windows)
  * memory     — this process's peak RSS (resource module; skipped where absent)
  * log_growth — are the foreman/audit logs runaway on disk

Amber, not red: a monitor that's straining is a warning to attend to, not a
customer-facing outage. Keyed on the condition, never the live number.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import Sensor, Signal
from ..http_utils import require_http_url


def _ollama_up(base_url: str, timeout: float) -> tuple[bool, str]:
    import urllib.request
    try:
        url = require_http_url(base_url.rstrip("/") + "/api/tags")
        req = urllib.request.Request(url, method="GET")
        # require_http_url above restricts urllib to its HTTP(S) handlers.
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            return (200 <= r.status < 500), f"http {r.status}"
    except Exception as e:
        return False, str(e)[:80]


def _load_ratio() -> float | None:
    """1-min load average / cpu count, or None where unavailable (Windows)."""
    try:
        one = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        return one / cpus
    except (OSError, AttributeError):
        return None


def _peak_rss_mb() -> float | None:
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS, kilobytes on Linux.
        import sys
        return ru / (1024 * 1024) if sys.platform == "darwin" else ru / 1024
    except Exception:
        return None


def _dir_mb(path: Path) -> float:
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size / (1024 * 1024)
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0.0
    return total / (1024 * 1024)


class SelfResourcesSensor(Sensor):
    id = "self_resources"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        base_url = (sc.get("ollama_url")
                    or (self.cfg.raw.get("junior") or {}).get("base_url")
                    or "http://127.0.0.1:11434")
        probe_timeout = float(sc.get("probe_timeout_sec", 2))
        amber_load = float(sc.get("amber_load_ratio", 4.0))
        red_load = float(sc.get("red_load_ratio", 8.0))
        amber_logs_mb = float(sc.get("amber_logs_mb", 200))

        data_dir = Path(self.cfg.path).parent
        metrics: dict = {}
        problems: list[str] = []
        severity = "green"

        # Brain (Junior's own model server).
        brain_ok, brain_note = _ollama_up(base_url, probe_timeout)
        metrics["brain_ok"] = brain_ok
        metrics["brain_note"] = brain_note
        if not brain_ok:
            problems.append(f"monitor brain (Ollama) unreachable — {brain_note}")
            severity = "amber"

        # Load.
        lr = _load_ratio()
        if lr is not None:
            metrics["load_ratio"] = round(lr, 2)
            if lr >= red_load:
                problems.append(f"host load {lr:.1f}× cores")
                severity = "red"
            elif lr >= amber_load:
                problems.append(f"host load {lr:.1f}× cores")
                if severity != "red":
                    severity = "amber"

        # Own memory (informational; not a threshold — just recorded for audit).
        rss = _peak_rss_mb()
        if rss is not None:
            metrics["peak_rss_mb"] = round(rss, 1)

        # Log growth — the foreman's own files + audit trails.
        logs_mb = round(
            _dir_mb(data_dir / "junior_audit") + _dir_mb(data_dir / "senior_audit")
            + _dir_mb(data_dir / "jarvis_foreman_events.jsonl")
            + _dir_mb(data_dir / "jarvis_foreman_chat_outbox.done.jsonl"),
            1,
        )
        metrics["logs_mb"] = logs_mb
        if logs_mb >= amber_logs_mb:
            problems.append(f"monitor logs at {logs_mb:.0f} MB")
            if severity != "red":
                severity = "amber"

        if severity == "green":
            note = "green"
            if lr is not None:
                note = f"load {lr:.1f}×"
            return [Signal(self.id, "green", f"monitor healthy ({note})",
                           dedupe_key="self:ok", detail={"metrics": metrics})]

        # Condition-keyed: which self-problems are live, not their magnitudes.
        dedupe_key = "self:" + ",".join(sorted(
            c for c, on in (("brain", not brain_ok),
                            ("load", lr is not None and lr >= amber_load),
                            ("logs", logs_mb >= amber_logs_mb)) if on
        ))
        return [Signal(self.id, severity, ("self: " + "; ".join(problems))[:220],
                       dedupe_key=dedupe_key,
                       detail={"metrics": metrics, "offenders": problems})]
