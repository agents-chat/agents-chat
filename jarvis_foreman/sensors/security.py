"""security — Junior guards the two invariants that keep this box from leaking.

CLAUDE.md's hard rule is that the app + agent-bridge listeners stay on loopback
(public reach is provided only by the Tailscale HTTPS proxy). This sensor makes
that rule observable, plus watches for an authentication-failure spike:

  * open_listener — a watched port (app 8086, the agent bridges) bound to anything
                    other than loopback (0.0.0.0, *, ::, or a LAN/public IP). A
                    bridge exposed beyond loopback is a real exposure → red.
  * auth_fails    — a burst of unauthorized/forbidden/401/403 lines in a configured
                    auth log within the last hour → amber.

Both probes are guarded and cross-platform: if lsof is absent or we're on the
Windows client, the listener check is *skipped*, not failed; if no auth log is
configured or the file is gone, that check is skipped too. Stdlib only (psutil is
not installed and this also runs on Windows): subprocess/re/pathlib/datetime.

Dedupe discipline (see sensors/base.Signal): the key names the offending CONDITION
SET — which ports are exposed, and whether auth is spiking — never a live count.
Counts churn every poll and would mint a fresh cooldown slot each tick.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import Sensor, Signal

# Bound the auth-log read so a giant log never balloons memory; we only ever care
# about the recent tail (last hour of lines lives here comfortably).
_MAX_SCAN_BYTES = 4_000_000

# Auth-failure line pattern (case-insensitive), matching the CLAUDE.md-style rule.
_AUTH_RE = re.compile(r"unauthorized|forbidden|401|403|auth fail", re.I)

# Leading/embedded timestamp shapes we can place in time. Auth failures show up in
# nginx/apache access + error logs, syslog, and app JSON lines, so cover the common
# formats; a matched line we cannot timestamp is left out of the window (conservative).
_EPOCH_RE = re.compile(r"^(\d{9,11})(?:\.\d+)?\b")
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_SLASH_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_CLF_RE = re.compile(r"\[(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})")
_SYSLOG_RE = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1, localhost (ignoring brackets + IPv6 zone id)."""
    h = host.strip("[]").lower()
    if "%" in h:  # drop IPv6 zone id, e.g. fe80::1%lo0
        h = h.split("%", 1)[0]
    if h in ("localhost", "::1", "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1"):
        return True
    return h.startswith("127.") or h.startswith("::ffff:127.")


def _parse_listen_line(line: str) -> tuple[str, str, int, str] | None:
    """From an lsof -sTCP:LISTEN row, pull (command, host, port, raw_addr) or None.

    Row shape: `python3.1 1234 owner 5u IPv4 0x.. 0t0 TCP 127.0.0.1:8086 (LISTEN)`
    — the NAME (addr:port) sits just before the "(LISTEN)" token.
    """
    if "(LISTEN)" not in line:
        return None
    parts = line.split()
    try:
        addr = parts[parts.index("(LISTEN)") - 1]
    except (ValueError, IndexError):
        return None
    host, sep, port_s = addr.rpartition(":")
    if not sep or not port_s.isdigit():
        return None
    return parts[0], host.strip("[]"), int(port_s), addr


def _scan_listeners(watch_ports: set[int]) -> tuple[list[dict[str, Any]], str]:
    """Non-loopback listeners on watched ports, plus a status note.

    Skips (returns an empty list) rather than crashing when the check cannot run:
    on Windows, without lsof on PATH, or if lsof errors/times out.
    """
    if os.name == "nt":
        return [], "skipped: not POSIX (no lsof)"
    if not shutil.which("lsof"):
        return [], "skipped: lsof missing"
    try:
        proc = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
            check=False,  # lsof exits 1 when nothing matches; that is not an error
        )
    except Exception as e:  # timeout / OS error → skip this check, do not crash
        return [], f"skipped: lsof failed ({type(e).__name__})"

    offenders: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for line in (proc.stdout or "").splitlines()[1:]:  # drop header row
        parsed = _parse_listen_line(line)
        if not parsed:
            continue
        command, host, port, addr = parsed
        if port not in watch_ports or _is_loopback(host):
            continue
        key = (port, addr, command)
        if key in seen:
            continue
        seen.add(key)
        offenders.append({"port": port, "addr": addr, "command": command})
    return offenders, ("exposed" if offenders else "ok")


def _line_epoch(line: str, now: float) -> float | None:
    """Best-effort: epoch seconds of the line's leading/embedded timestamp, or None."""
    m = _EPOCH_RE.match(line)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = _ISO_RE.search(line) or _SLASH_RE.search(line)
    if m:
        try:
            y, mo, d, h, mi, s = (int(x) for x in m.groups())
            return datetime(y, mo, d, h, mi, s).timestamp()
        except (ValueError, OverflowError):
            pass
    m = _CLF_RE.search(line)
    if m:
        mo = _MONTHS.get(m.group(2).title())
        if mo:
            try:
                return datetime(int(m.group(3)), mo, int(m.group(1)),
                                int(m.group(4)), int(m.group(5)),
                                int(m.group(6))).timestamp()
            except (ValueError, OverflowError):
                pass
    m = _SYSLOG_RE.match(line)
    if m:
        mo = _MONTHS.get(m.group(1).title())
        if mo:
            try:
                yr = datetime.fromtimestamp(now).year
                args = (mo, int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)))
                ts = datetime(yr, *args).timestamp()
                # syslog carries no year; a ts well in the future belongs to last year
                if ts - now > 86400:
                    ts = datetime(yr - 1, *args).timestamp()
                return ts
            except (ValueError, OverflowError):
                pass
    return None


def _tail_text(path: Path, max_bytes: int) -> str:
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    return data.decode("utf-8", "replace")


def _scan_auth(path: Path, window_sec: float, now: float,
               max_bytes: int) -> tuple[int, int, bool]:
    """(recent_matches, total_matches, timed).

    `recent` counts auth-failure lines whose timestamp is within the window;
    `timed` is whether we could timestamp any matched line at all. When no matched
    line carries a parseable timestamp, the caller falls back to `total`.
    """
    text = _tail_text(path, max_bytes)
    cutoff = now - window_sec
    recent = total = 0
    timed = False
    for line in text.splitlines():
        if not _AUTH_RE.search(line):
            continue
        total += 1
        ts = _line_epoch(line, now)
        if ts is not None:
            timed = True
            if ts >= cutoff:
                recent += 1
    return recent, total, timed


class SecuritySensor(Sensor):
    id = "security"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        watch_ports = {
            int(p) for p in sc.get(
                "watch_ports",
                [8086, 55009, 55010, 55011, 55012, 55014, 55015, 55017, 55018, 55019, 55020, 55023],
            )
        }
        amber_auth_fails = int(sc.get("amber_auth_fails", 20))
        auth_log = sc.get("auth_log")
        window_sec = float(sc.get("auth_window_min", 60)) * 60

        open_listeners: list[dict[str, Any]] = []
        auth_spike = False
        offenders: list[str] = []
        detail: dict[str, Any] = {"checks": {}}

        try:
            # (1) Listeners beyond loopback — the exposure that matters most.
            open_listeners, lstatus = _scan_listeners(watch_ports)
            detail["checks"]["listeners"] = lstatus
            detail["open_listeners"] = open_listeners
            for o in open_listeners:
                offenders.append(
                    f"{o['command']} listening on {o['addr']} "
                    f"(watched port {o['port']}) — beyond loopback"
                )

            # (2) Auth-failure spike — only when a log path is configured + present.
            if auth_log:
                p = Path(auth_log)
                if p.is_file():
                    recent, total, timed = _scan_auth(
                        p, window_sec, time.time(), _MAX_SCAN_BYTES)
                    count = recent if timed else total
                    detail["auth_fails"] = {
                        "path": str(p), "count": count, "total_matched": total,
                        "timestamped": timed, "threshold": amber_auth_fails,
                        "window_min": int(window_sec // 60),
                    }
                    if count > amber_auth_fails:
                        auth_spike = True
                        detail["checks"]["auth"] = "spike"
                        offenders.append(
                            f"{count} auth failures in last {int(window_sec // 60)}m "
                            f"in {p.name} (> {amber_auth_fails})"
                        )
                    else:
                        detail["checks"]["auth"] = "ok"
                else:
                    detail["checks"]["auth"] = "skipped: log missing"
            else:
                detail["checks"]["auth"] = "skipped: not configured"
        except Exception as e:  # never let the monitor become the incident
            return [Signal(self.id, "amber", "security check failed",
                           dedupe_key="security:check_failed",
                           detail={"error": str(e)})]

        if not open_listeners and not auth_spike:
            return [Signal(self.id, "green", "security: listeners loopback-only",
                           dedupe_key="security:ok", detail=detail)]

        # A non-loopback listener on a watched port is a real exposure (red); an
        # auth-fail spike on its own is a warning (amber).
        severity = "red" if open_listeners else "amber"

        # Key on the offending CONDITION SET: which ports are exposed + whether auth
        # is spiking. Same exposed set = same episode even as counts drift.
        key_bits: list[str] = []
        if open_listeners:
            ports = sorted({o["port"] for o in open_listeners})
            key_bits.append("open_listener:" + ",".join(str(p) for p in ports))
        if auth_spike:
            key_bits.append("auth_fails")
        dedupe_key = "security:" + ";".join(key_bits)

        bits: list[str] = []
        if open_listeners:
            n = len(open_listeners)
            bits.append(f"{n} listener{'s' if n != 1 else ''} beyond loopback")
        if auth_spike:
            bits.append("auth-fail spike")
        headline = "security: " + ", ".join(bits)

        detail["offenders"] = offenders[:8]
        return [Signal(self.id, severity, headline[:220],
                       dedupe_key=dedupe_key, detail=detail)]
