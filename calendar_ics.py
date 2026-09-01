"""Read a Google Calendar (or any) secret ICS feed and return upcoming events.

Stdlib only — no icalendar/dateutil available. A small RFC 5545 parser plus a
BOUNDED RRULE expander covering the common recurring cases (DAILY/WEEKLY/MONTHLY/
YEARLY with INTERVAL/COUNT/UNTIL, and weekly BYDAY). Exotic rules (BYMONTHDAY,
BYSETPOS, …) fall back to their base occurrence — good enough for "what's coming
up," and honest about the limit. Read-only. The feed URL is a secret treated
like an API key. Normal setup is orchestrator-owned inside chat; Settings remains
an advanced manual fallback.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import logging
import os
import re
import secrets as _secrets
import socket
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger("calendar")

try:  # 3.9+ stdlib; needs system tzdata (present on macOS)
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_UTC = dt.timezone.utc
_MAX_ICS_BYTES = 8 * 1024 * 1024  # ample for a personal ICS; caps a huge/slow feed


class CalendarError(RuntimeError):
    """Raised when the calendar feed is misconfigured or unreadable."""


# --- Multi-calendar store ---------------------------------------------------
# The owner can connect several ICS feeds (personal, work, family, …). Each is a
# named, colored, individually-toggleable calendar. They live in a small JSON
# file next to the app (gitignored, chmod 600) — NOT in shared.env, whose value
# grammar can't hold names/colors. The URLs are secrets: only masked hints ever
# leave the server. The normal setup path is conversational: app.py intercepts a
# private iCal address before it reaches chat storage or an agent, tests it, and
# saves it here. The Settings card remains an advanced/manual fallback. A legacy
# single-feed CALENDAR_ICS_URL is still honoured — merged in as the "primary"
# calendar — and folded into the store once, at boot, by
# migrate_primary_into_store().

_STORE_PATH = (os.environ.get("CALENDARS_STORE") or "").strip() or str(
    Path(__file__).parent / "calendars.json")
_DEFAULT_COLORS = ["#e0b64c", "#6ea8fe", "#7ee787", "#f78fb3",
                   "#c792ea", "#f0883e", "#4cc9c0", "#ff6b6b"]
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SCHEMES = ("http://", "https://", "webcal://")


def ics_url() -> str:
    """The legacy single-feed URL (CALENDAR_ICS_URL). Kept for back-compat."""
    return (os.environ.get("CALENDAR_ICS_URL") or "").strip()


def _gen_id() -> str:
    return "cal_" + _secrets.token_hex(4)


def _norm_color(c: Any) -> str:
    c = str(c or "").strip()
    return c if _HEX_RE.match(c) else _DEFAULT_COLORS[0]


def _next_color(existing: list[dict]) -> str:
    used = {c.get("color") for c in existing}
    for col in _DEFAULT_COLORS:
        if col not in used:
            return col
    return _DEFAULT_COLORS[len(existing) % len(_DEFAULT_COLORS)]


def _validate_url(u: str) -> str:
    """Return a cleaned URL or raise CalendarError. (Scheme only — the feed is
    only truly validated when fetched, by upcoming()/test.)"""
    u = (u or "").strip()
    if not u.lower().startswith(_SCHEMES):
        raise CalendarError("must be an http(s) or webcal iCal URL")
    return u


def _read_doc() -> dict:
    p = Path(_STORE_PATH)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text() or "{}")
    except Exception:
        log.warning("calendars store unreadable (%s); treating as empty", p)
        return {}
    return d if isinstance(d, dict) else {}


def _write_doc(doc: dict) -> None:
    p = Path(_STORE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _load_store() -> list[dict]:
    """Stored extra calendars (each has a real url)."""
    raw = _read_doc().get("calendars")
    out: list[dict] = []
    if isinstance(raw, list):
        for c in raw:
            if not isinstance(c, dict):
                continue
            url = str(c.get("url") or "").strip()
            if not url:
                continue
            cid = str(c.get("id") or "").strip()
            out.append({
                "id": cid if _ID_RE.match(cid) else _gen_id(),
                "name": str(c.get("name") or "").strip() or "Calendar",
                "url": url,
                "color": _norm_color(c.get("color")),
                "enabled": bool(c.get("enabled", True)),
            })
    return out


def _save_store(cals: list[dict]) -> None:
    doc = _read_doc()
    doc["calendars"] = [{
        "id": c["id"], "name": c["name"], "url": c["url"],
        "color": c["color"], "enabled": bool(c["enabled"]),
    } for c in cals]
    _write_doc(doc)


def all_calendars() -> list[dict]:
    """Every connected feed: the legacy env "primary" (if set) followed by the
    stored calendars, deduped by URL. Each entry carries a real url + a source
    tag. Internal use — see public_calendars() for the masked view."""
    out: list[dict] = []
    seen: set[str] = set()
    env = ics_url()
    if env and env.lower().startswith(_SCHEMES):
        out.append({"id": "primary", "name": "My calendar", "url": env,
                    "color": _DEFAULT_COLORS[0], "enabled": True, "source": "env"})
        seen.add(env)
    for c in _load_store():
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        out.append({**c, "source": "store"})
    return out


def enabled_feeds() -> list[dict]:
    return [c for c in all_calendars() if c.get("enabled") and c.get("url")]


def configured() -> bool:
    """True when there is at least one enabled feed to read."""
    return bool(enabled_feeds())


def _hint_for(u: str) -> str:
    if not u:
        return ""
    try:
        host = u.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        host = ""
    return f"{host}/…{u[-6:]}" if len(u) >= 6 else "set"


def url_hint() -> str:
    """A masked hint for the primary/legacy feed — never the full secret URL."""
    return _hint_for(ics_url())


def public_calendars() -> list[dict]:
    """Masked list for the Settings UI — url replaced by a hint, never the secret."""
    return [{
        "id": c["id"], "name": c["name"], "color": c["color"],
        "enabled": bool(c["enabled"]), "url_hint": _hint_for(c["url"]),
        "source": c["source"],
    } for c in all_calendars()]


def add_calendar(name: str, url: str, color: Optional[str] = None) -> dict:
    """Add a stored calendar. Raises CalendarError on a bad or duplicate URL."""
    url = _validate_url(url)
    for existing in all_calendars():
        if existing["url"] == url:
            raise CalendarError("That calendar is already connected.")
    cals = _load_store()
    entry = {
        "id": _gen_id(),
        "name": (name or "").strip() or "Calendar",
        "url": url,
        "color": _norm_color(color) if color else _next_color(cals),
        "enabled": True,
    }
    cals.append(entry)
    _save_store(cals)
    return entry


def update_calendar(cid: str, *, name: Optional[str] = None,
                    color: Optional[str] = None, enabled: Optional[bool] = None,
                    url: Optional[str] = None) -> Optional[dict]:
    """Patch a stored calendar's name/color/enabled/url. Returns the updated
    entry, or None if no stored calendar has that id (e.g. the env primary)."""
    cals = _load_store()
    for c in cals:
        if c["id"] == cid:
            if name is not None:
                c["name"] = str(name).strip() or c["name"]
            if color is not None:
                c["color"] = _norm_color(color)
            if enabled is not None:
                c["enabled"] = bool(enabled)
            if url is not None:
                c["url"] = _validate_url(url)
            _save_store(cals)
            return c
    return None


def remove_calendar(cid: str) -> bool:
    """Delete a stored calendar by id. Returns False if it wasn't in the store
    (the env primary is removed by clearing CALENDAR_ICS_URL, not here)."""
    cals = _load_store()
    kept = [c for c in cals if c["id"] != cid]
    if len(kept) == len(cals):
        return False
    _save_store(kept)
    return True


def migrate_primary_into_store(delete_env=None) -> bool:
    """One-time: fold a legacy CALENDAR_ICS_URL into the store as a normal,
    fully-editable calendar named "My calendar", then clear the env secret so the
    store is the single source of truth. Idempotent — a no-op once the URL is in
    the store or the env var is unset. Skipped under pytest so tests keep driving
    the env path directly. Returns True if a migration happened."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    env = ics_url()
    if not (env and env.lower().startswith(_SCHEMES)):
        return False
    cals = _load_store()
    already = any(c["url"] == env for c in cals)
    if not already:
        cals.insert(0, {"id": _gen_id(), "name": "My calendar", "url": env,
                        "color": _DEFAULT_COLORS[0], "enabled": True})
        _save_store(cals)
    if delete_env:
        try:
            delete_env("CALENDAR_ICS_URL")
        except Exception:
            log.exception("could not clear CALENDAR_ICS_URL after calendar migration")
    os.environ.pop("CALENDAR_ICS_URL", None)
    return not already


def _normalize_url(u: str) -> str:
    if u.lower().startswith("webcal://"):
        return "https://" + u[len("webcal://"):]
    return u


def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: continuation lines start with space/tab."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(s: str) -> str:
    return (s.replace("\\,", ",").replace("\\;", ";")
             .replace("\\n", " ").replace("\\N", " ").replace("\\\\", "\\").strip())


def _parse_dt(val: str, params: dict[str, str]) -> Optional[dt.datetime]:
    v = (val or "").strip()
    if not v:
        return None
    if params.get("VALUE") == "DATE" or (len(v) == 8 and "T" not in v):
        try:
            return dt.datetime.strptime(v, "%Y%m%d").replace(tzinfo=_UTC)
        except ValueError:
            return None
    is_utc = v.endswith("Z")
    core = v[:-1] if is_utc else v
    try:
        d = dt.datetime.strptime(core, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if is_utc:
        return d.replace(tzinfo=_UTC)
    tzid = params.get("TZID")
    if tzid and ZoneInfo is not None:
        try:
            return d.replace(tzinfo=ZoneInfo(tzid)).astimezone(_UTC)
        except Exception:
            pass
    return d.replace(tzinfo=_UTC)  # naive/floating → assume UTC


def _parse_events(lines: list[str]) -> list[dict]:
    events: list[dict] = []
    cur: Optional[dict] = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            name, _, val = line.partition(":")
            segs = name.split(";")
            key = segs[0].upper()
            params: dict[str, str] = {}
            for p in segs[1:]:
                if "=" in p:
                    pk, pv = p.split("=", 1)
                    params[pk.upper()] = pv
            if key == "DTSTART":
                cur["start"] = _parse_dt(val, params)
                cur["all_day"] = params.get("VALUE") == "DATE" or (
                    len(val.strip()) == 8 and "T" not in val)
            elif key == "DTEND":
                cur["end"] = _parse_dt(val, params)
            elif key == "SUMMARY":
                cur["summary"] = _unescape(val)
            elif key == "LOCATION":
                cur["location"] = _unescape(val)
            elif key == "RRULE":
                cur["rrule"] = val.strip()
            elif key == "STATUS":
                cur["status"] = val.strip().upper()
    return events


def _add_months(d: dt.datetime, months: int) -> dt.datetime:
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    # clamp day (e.g. Jan 31 + 1mo → Feb 28)
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return d.replace(year=year, month=month, day=day)


def _expand(ev: dict, win_start: dt.datetime, win_end: dt.datetime, cap: int = 60) -> list[tuple]:
    start = ev.get("start")
    if not isinstance(start, dt.datetime):
        return []
    end = ev.get("end") if isinstance(ev.get("end"), dt.datetime) else start
    dur = end - start
    rr = ev.get("rrule")
    if not rr:
        return [(start, start + dur)] if win_start <= start <= win_end else []

    parts: dict[str, str] = {}
    for kv in rr.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.upper()] = v
    freq = parts.get("FREQ", "").upper()
    interval = max(1, int(parts["INTERVAL"]) if parts.get("INTERVAL", "").isdigit() else 1)
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until = _parse_dt(parts["UNTIL"], {}) if parts.get("UNTIL") else None
    byday = [_WEEKDAYS[d[-2:]] for d in parts.get("BYDAY", "").split(",") if d[-2:] in _WEEKDAYS]

    occ: list[tuple] = []
    emitted = 0
    guard = 0

    if freq == "WEEKLY" and byday:
        tod = dt.timedelta(hours=start.hour, minutes=start.minute,
                           seconds=start.second, microseconds=start.microsecond)
        week0 = (start - dt.timedelta(days=start.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        step = 0
        # Fast-forward an unbounded series to the window so an old DTSTART doesn't
        # exhaust the guard before reaching [win_start, win_end] (start a week early
        # so no boundary occurrence is missed). COUNT-bounded series iterate from the
        # start so COUNT stays exact.
        if count is None and week0 < win_start:
            step = max(0, ((win_start - week0).days // 7) // interval - 1)
        while guard < 600:
            guard += 1
            wk = week0 + dt.timedelta(weeks=step * interval)
            if wk > win_end:
                break
            for wd in sorted(byday):
                o = wk + dt.timedelta(days=wd) + tod
                if o < start:
                    continue
                if until and o > until:
                    continue
                if count is not None and emitted >= count:
                    break
                emitted += 1
                if win_start <= o <= win_end:
                    occ.append((o, o + dur))
            if count is not None and emitted >= count:
                break
            step += 1
        return sorted(occ)[:cap]

    step_delta = {"DAILY": dt.timedelta(days=interval),
                  "WEEKLY": dt.timedelta(weeks=interval)}.get(freq)
    cur = start
    # Fast-forward an unbounded DAILY/WEEKLY series to the window (an old DTSTART
    # would otherwise exhaust the guard first). MONTHLY/YEARLY reach ~125yr+ within
    # the guard, so they need no jump. COUNT-bounded series iterate from the start
    # so COUNT semantics stay exact.
    if count is None and step_delta is not None and cur < win_start:
        skip = (win_start - cur) // step_delta
        if skip > 0:
            cur = cur + skip * step_delta
    while guard < 1500:
        guard += 1
        if count is not None and emitted >= count:
            break
        if until and cur > until:
            break
        if cur > win_end:
            break
        if cur >= win_start:
            occ.append((cur, cur + dur))
        emitted += 1
        if freq == "MONTHLY":
            cur = _add_months(cur, interval)
        elif freq == "YEARLY":
            try:
                cur = cur.replace(year=cur.year + interval)
            except ValueError:  # Feb 29
                cur = cur.replace(month=2, day=28, year=cur.year + interval)
        elif step_delta is not None:
            cur = cur + step_delta
        else:
            break  # unknown FREQ → base occurrence only (already handled if in-window)
    return sorted(occ)[:cap]


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_loopback or addr.is_private or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def _assert_public_host(url: str) -> None:
    """SSRF guard: resolve the URL's host and refuse if it maps to a loopback,
    private, link-local, or otherwise-internal address. Blocks pointing a calendar
    feed at internal bridges (127.0.0.1:55009-55015) or cloud metadata
    (169.254.169.254). Raises CalendarError on any non-public/unresolvable host."""
    host = (urlparse(url).hostname or "").strip()
    if not host:
        raise CalendarError("Calendar feed URL is missing a host.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # macOS's resolver intermittently fails under concurrent bursts (several
        # dashboard tabs refreshing at once) while isolated lookups succeed —
        # one short retry clears virtually all of these.
        time.sleep(0.2)
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise CalendarError("Could not resolve the calendar feed host.") from exc
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            raise CalendarError("Calendar feed host is not allowed.")


# Feed cache: calendars change rarely, but dashboard ledger refreshes can call
# upcoming() in tight bursts (several tabs + SSE hints at once). Serving a
# minutes-old copy avoids hammering Google — and avoids the concurrent-resolve
# storms that intermittently gaierror on macOS (observed 2026-08-11: 247×502 vs
# 58×200 in one morning). On fetch failure a stale copy (up to a day old) is
# served instead of erroring the "Coming up" lane empty.
_FEED_CACHE: dict[str, tuple[float, str]] = {}
_FEED_TTL_S = 300.0
_FEED_STALE_MAX_S = 86_400.0


async def _fetch_ics(url: str, timeout: float = 20.0) -> str:
    """Cached wrapper around _fetch_ics_live (TTL 5 min, stale-on-error 24 h)."""
    now = time.time()
    hit = _FEED_CACHE.get(url)
    if hit and now - hit[0] < _FEED_TTL_S:
        return hit[1]
    try:
        body = await _fetch_ics_live(url, timeout)
    except CalendarError:
        if hit and now - hit[0] < _FEED_STALE_MAX_S:
            log.warning("calendar feed fetch failed; serving stale copy (%.0fs old)",
                        now - hit[0])
            return hit[1]
        raise
    _FEED_CACHE[url] = (now, body)
    return body


async def _fetch_ics_live(url: str, timeout: float = 20.0) -> str:
    """Fetch an ICS feed as text, streaming with a hard byte cap so a huge/slow
    feed can't OOM or pin the shared single-process app."""
    target = _normalize_url(url)
    # Validate the host resolves to a public address, and do NOT follow redirects
    # (a 30x could otherwise bounce to an internal address after the check).
    _assert_public_host(target)
    cfg = httpx.Timeout(timeout, connect=10.0)
    async with httpx.AsyncClient(timeout=cfg, follow_redirects=False) as client:
        try:
            async with client.stream(
                "GET", target, headers={"User-Agent": "agent-chat/1.0"}
            ) as resp:
                if 300 <= resp.status_code < 400:
                    raise CalendarError(
                        "Calendar feed redirected — use the direct 'Secret address in iCal "
                        "format' URL (it should return the feed without redirecting)."
                    )
                if resp.status_code == 404:
                    raise CalendarError(
                        "Google returned 404 — that isn't a live iCal feed. In Google Calendar "
                        "→ Settings → your calendar → 'Integrate calendar', copy the full 'Secret "
                        "address in iCal format' (it ends in a long /private-…/basic.ics)."
                    )
                if resp.status_code != 200:
                    raise CalendarError(f"Calendar feed error (HTTP {resp.status_code}).")
                clen = resp.headers.get("content-length", "")
                if clen.isdigit() and int(clen) > _MAX_ICS_BYTES:
                    raise CalendarError("Calendar feed is too large (over 8 MB).")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_ICS_BYTES:
                        raise CalendarError("Calendar feed is too large (over 8 MB).")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            # httpx exception strings can include the complete request URL. For
            # private iCal feeds the path itself is the credential, so never put
            # the underlying exception in a user-visible error or log message.
            raise CalendarError("Could not reach the calendar feed.") from exc
    body = b"".join(chunks).decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in body:
        raise CalendarError(
            "That URL returned a web page, not an iCal feed. Use the 'Secret address in "
            "iCal format' from Google Calendar settings — not the shareable/HTML link."
        )
    return body


_busy_cache: dict[str, Any] = {"ts": 0.0, "url": "", "events": None}


async def busy_now(ttl: float = 300.0) -> tuple[bool, str]:
    """Is the owner in a TIMED meeting right now, on ANY connected calendar?
    Returns (busy, title). Cached ~ttl (keyed on the set of enabled feeds) so it's
    cheap to call from the notification hot path. All-day events don't count as
    'in a meeting'. Fails safe to (False, '') if the feeds are unreachable."""
    feeds = enabled_feeds()
    if not feeds:
        return (False, "")
    import time as _time
    now_mono = _time.monotonic()
    sig = "\n".join(sorted(f["url"] for f in feeds))
    events = _busy_cache["events"]
    if not (_busy_cache["url"] == sig and events is not None
            and now_mono - _busy_cache["ts"] < ttl):
        now = dt.datetime.now(_UTC)
        lo, hi = now - dt.timedelta(days=1), now + dt.timedelta(days=1)
        fresh: list[tuple] = []
        for f in feeds:
            try:
                body = await _fetch_ics(f["url"], timeout=5.0)  # short — hot path
            except CalendarError:
                continue  # one bad feed shouldn't blind the others
            for ev in _parse_events(_unfold(body)):
                if ev.get("status") == "CANCELLED" or ev.get("all_day"):
                    continue
                for (s, e) in _expand(ev, lo, hi):
                    if e > s:  # skip zero-length markers
                        fresh.append((s, e, ev.get("summary") or "meeting"))
        # Negative-cache even a total failure (stamp ts) so a broken/slow feed
        # doesn't re-fetch — and re-block — on every push during an outage.
        events = fresh
        _busy_cache.update(ts=now_mono, url=sig, events=events)
    now = dt.datetime.now(_UTC)
    for (s, e, title) in (events or []):
        if s <= now < e:
            return (True, title)
    return (False, "")


async def _upcoming_one(feed: dict, now: dt.datetime, win_end: dt.datetime,
                        timeout: float) -> list[dict]:
    body = await _fetch_ics(feed["url"], timeout)
    rows: list[dict] = []
    for ev in _parse_events(_unfold(body)):
        if ev.get("status") == "CANCELLED":
            continue
        for (s, e) in _expand(ev, now, win_end):
            rows.append({
                "title": ev.get("summary") or "(no title)",
                "start": s.isoformat(),
                "end": e.isoformat(),
                "location": ev.get("location") or None,
                "all_day": bool(ev.get("all_day")),
                "calendar": feed["name"],
                "calendar_id": feed["id"],
                "color": feed["color"],
            })
    return rows


async def upcoming(days: int = 14, limit: int = 25, timeout: float = 20.0) -> dict[str, Any]:
    """Merge every enabled ICS feed and return events in the next ``days``, sorted
    by start. Each event is tagged with its source ``calendar`` name + ``color``.
    Feeds are fetched concurrently and fail independently — a broken calendar is
    reported in ``errors`` rather than sinking the others. If EVERY feed fails,
    the first error is raised (preserving the single-calendar contract)."""
    feeds = enabled_feeds()
    if not feeds:
        raise CalendarError(
            "No calendar configured. Ask your orchestrator to connect a read-only "
            "calendar, then paste the private iCal address into that chat when asked."
        )
    days = max(1, min(int(days or 14), 60))
    limit = max(1, min(int(limit or 25), 50))
    now = dt.datetime.now(_UTC)
    win_end = now + dt.timedelta(days=days)

    results = await asyncio.gather(
        *[_upcoming_one(f, now, win_end, timeout) for f in feeds],
        return_exceptions=True,
    )
    out: list[dict] = []
    errors: list[dict] = []
    for feed, res in zip(feeds, results):
        if isinstance(res, Exception):
            msg = str(res) if isinstance(res, CalendarError) else "could not be read"
            errors.append({"calendar": feed["name"], "error": msg})
        else:
            out.extend(res)
    if not out and errors and len(errors) == len(feeds):
        raise CalendarError(errors[0]["error"])
    out.sort(key=lambda x: x["start"])
    payload: dict[str, Any] = {
        "window_days": days,
        "count": min(len(out), limit),
        "events": out[:limit],
        "calendars": [{"id": f["id"], "name": f["name"], "color": f["color"]}
                      for f in feeds],
    }
    if errors:
        payload["errors"] = errors
    return payload
