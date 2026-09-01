"""credentials — config-driven expiry radar for OAuth tokens, certs, API keys.

No network, no filesystem: the operator lists the things that expire (the weekly
Brokerage OAuth re-login, a TLS cert, a rotating API key) with a due date, and Junior
counts down. Each item is:

    {"name": str,
     "expires_at": ISO-8601 date/datetime string OR epoch seconds,
     "kind": optional str}

Severity bands (thresholds are config, never hardcoded):
  * red   — any item already expired or due within `red_days`   (default 2)
  * amber — any item due within `amber_days`                    (default 7)
  * green — everything further out (or nothing tracked)

Bad/missing dates are skipped (recorded in detail.skipped), never fatal.

Dedupe discipline (see sensors/base.Signal): key on the SET of expiring item
names, never on the day counts — those tick down every poll and would mint a fresh
cooldown slot each time, defeating suppression. Green path uses a fixed key.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .base import Sensor, Signal


def _parse_expiry(value: object) -> datetime | None:
    """Coerce an ISO-8601 string or epoch-seconds number to an aware datetime.

    Returns None for anything unparseable so the caller can skip that item rather
    than crash the poll. Naive ISO timestamps are assumed UTC for a deterministic,
    platform-independent countdown.
    """
    # bool is an int subclass; a boolean expiry is nonsense.
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # ISO-8601 date or datetime (fromisoformat handles "Z" on 3.11+, but we
        # normalize it anyway to be safe and to accept a trailing "Z" explicitly).
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(iso)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        # Numeric string → epoch seconds (dashes in ISO dates make float() fail,
        # so this only catches genuine epoch strings like "1784000000").
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _days_label(days: float) -> str:
    """Compact countdown for headlines: '3d', '0d' (today), or 'expired'."""
    if days < 0:
        return "expired"
    return f"{int(math.ceil(days))}d"


class CredentialsSensor(Sensor):
    id = "credentials"

    def poll(self) -> list[Signal]:
        sc = self.cfg.sensor_cfg(self.id)
        items = sc.get("items") or []
        red_days = float(sc.get("red_days", 2))
        amber_days = float(sc.get("amber_days", 7))

        try:
            now = datetime.now(timezone.utc)
            expiring: list[dict] = []   # items due within amber_days
            skipped: list[str] = []     # unparseable / malformed, recorded not fatal
            for raw in items:
                if not isinstance(raw, dict):
                    skipped.append(str(raw)[:60])
                    continue
                name = str(raw.get("name") or "").strip()
                dt = _parse_expiry(raw.get("expires_at"))
                if not name or dt is None:
                    skipped.append(name or "<unnamed>")
                    continue
                days = (dt - now).total_seconds() / 86400.0
                if days <= amber_days:
                    expiring.append({
                        "name": name,
                        "kind": raw.get("kind"),
                        "days": days,
                        "band": "red" if days <= red_days else "amber",
                        "expires_at": dt.isoformat(),
                    })
        except Exception as e:  # defensive backstop — the poll must never raise
            return [Signal(self.id, "amber", "credentials check failed",
                           dedupe_key="cred:check_failed", detail={"error": str(e)})]

        if not expiring:
            note = f"tracking {len(items)}" if items else "none tracked"
            return [Signal(self.id, "green", f"credentials current ({note})",
                           dedupe_key="cred:ok",
                           detail={"tracked": len(items), "skipped": skipped})]

        # Soonest-first so headline and offenders lead with the most urgent.
        expiring.sort(key=lambda it: it["days"])
        severity = "red" if any(it["band"] == "red" for it in expiring) else "amber"

        preview = ", ".join(
            f"{it['name']} {_days_label(it['days'])}" for it in expiring[:3]
        )
        if len(expiring) > 3:
            preview += ", …"
        n = len(expiring)
        headline = f"{n} credential{'s' if n != 1 else ''} expiring ({preview})"

        offenders: list[str] = []
        for it in expiring:
            if it["days"] < 0:
                offenders.append(f"{it['name']} expired")
            else:
                offenders.append(f"{it['name']} expires in {int(math.ceil(it['days']))}d")

        # Which credentials are expiring is the condition; the day counts churn and
        # stay out of the key. Same set = same episode even as the numbers count down.
        dedupe_key = "cred:" + ",".join(sorted(it["name"] for it in expiring))

        return [Signal(
            self.id, severity, headline[:220], dedupe_key=dedupe_key,
            detail={
                "offenders": offenders[:12],
                "expiring": expiring,
                "skipped": skipped,
                "tracked": len(items),
            },
        )]
