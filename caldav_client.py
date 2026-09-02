"""CalDAV + CardDAV client — the calendar/contacts belt tool's backend.

Small, dependency-free (stdlib only), the calendar/contacts sibling of
imap_search.py / imap_compose.py. Talks to a standard CalDAV/CardDAV server
(cPanel / Dovecot / SabreDAV — e.g. InMotion `mail.example.com:2080`)
over HTTPS with Basic auth. Provider-neutral: the caller passes the full collection
URL + credentials that the mail host publishes.

Operations, deliberately narrow:
  * list_events(range)      — REPORT calendar-query, read VEVENTs in a window
  * create_event(...)       — PUT a new VEVENT into the calendar collection
  * delete_event(uid|href)  — DELETE one event
  * list_contacts()         — REPORT addressbook-query, read VCARDs

Access is enforced in app.py (per-agent signed token + per-agent vaulted CALDAV_*
credential); this module only takes a URL + credentials and returns structured data.
"""
from __future__ import annotations

import base64
import datetime as dt
import re
import ssl
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from typing import Optional

from remote_host_safety import UnsafeRemoteHost, validate_https_url

CONNECT_TIMEOUT = 20.0
_UTC = dt.timezone.utc

CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
CARDDAV_NS = "urn:ietf:params:xml:ns:carddav"
DAV_NS = "DAV:"

MAX_EVENTS = 200
MAX_CONTACTS = 500


class CalDavError(Exception):
    """Raised for auth failures, bad requests, or connection problems."""


class _SafeDavRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow only same-origin redirects that remain safe HTTPS targets."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            old = validate_https_url(req.full_url, purpose="DAV")
            new = validate_https_url(newurl, purpose="DAV redirect")
        except UnsafeRemoteHost as exc:
            raise CalDavError(str(exc)) from exc
        old_origin = (old.scheme.lower(), old.hostname.lower(), old.port or 443)
        new_origin = (new.scheme.lower(), new.hostname.lower(), new.port or 443)
        if old_origin != new_origin:
            raise CalDavError("DAV redirect changed origin; credentials were not forwarded")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ---------------------------------------------------------------- HTTP

def _dav_request(method: str, url: str, username: str, password: str,
                 body: Optional[str] = None, depth: Optional[str] = None,
                 extra_headers: Optional[dict] = None, timeout: float = CONNECT_TIMEOUT):
    """One DAV request. Returns (status, text, headers). Non-2xx/207 raises."""
    try:
        validate_https_url(url, purpose="DAV")
    except UnsafeRemoteHost as exc:
        raise CalDavError(str(exc)) from exc
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", "Basic " + auth)
    if depth is not None:
        req.add_header("Depth", depth)
    if body is not None and "Content-Type" not in (extra_headers or {}):
        req.add_header("Content-Type", "application/xml; charset=utf-8")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    ctx = ssl.create_default_context()
    try:
        opener = urllib.request.build_opener(
            _SafeDavRedirectHandler(), urllib.request.HTTPSHandler(context=ctx)
        )
        resp = opener.open(req, timeout=timeout)
        text = resp.read().decode("utf-8", "replace")
        return resp.status, text, dict(resp.headers)
    except CalDavError:
        raise
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code == 401:
            raise CalDavError("CalDAV auth failed (check username / password)")
        raise CalDavError(f"CalDAV {method} failed: HTTP {e.code} {detail}".strip())
    except urllib.error.URLError as e:
        raise CalDavError(f"CalDAV connect failed ({url}): {e.reason}")
    except Exception as e:
        raise CalDavError(f"CalDAV {method} error: {e}")


# ---------------------------------------------------------------- iCal / vCard parsing

def _unfold(text: str) -> list[str]:
    """RFC 5545 / 6350 line unfolding: continuation lines start with space/tab."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(s: str) -> str:
    return (s.replace("\\,", ",").replace("\\;", ";")
             .replace("\\n", "\n").replace("\\N", "\n").replace("\\\\", "\\").strip())


def _escape(s: str) -> str:
    return (str(s or "").replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _split_prop(line: str):
    """'DTSTART;TZID=America/Chicago:20260712T140000' -> (KEY, {params}, value)."""
    name, _, value = line.partition(":")
    segs = name.split(";")
    key = segs[0].upper()
    params: dict[str, str] = {}
    for p in segs[1:]:
        if "=" in p:
            pk, pv = p.split("=", 1)
            params[pk.upper()] = pv
    return key, params, value


def _parse_dt(val: str, params: dict) -> Optional[dt.datetime]:
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
    if tzid:
        try:
            from zoneinfo import ZoneInfo
            return d.replace(tzinfo=ZoneInfo(tzid)).astimezone(_UTC)
        except Exception:
            pass
    return d.replace(tzinfo=_UTC)  # naive/floating → assume UTC


def _human(d: Optional[dt.datetime], all_day: bool) -> str:
    if not d:
        return ""
    local = d.astimezone()
    return local.strftime("%b %-d, %Y") if all_day else local.strftime("%b %-d, %Y %-I:%M %p")


def _event_from_ics(ics_text: str) -> Optional[dict]:
    """Parse the FIRST VEVENT out of a calendar-data blob into a JSON-able dict."""
    lines = _unfold(ics_text)
    inside = False
    ev: dict = {}
    for line in lines:
        if line.strip() == "BEGIN:VEVENT":
            inside = True
            ev = {}
            continue
        if line.strip() == "END:VEVENT":
            break
        if not inside or ":" not in line:
            continue
        key, params, val = _split_prop(line)
        if key == "UID":
            ev["uid"] = val.strip()
        elif key == "SUMMARY":
            ev["summary"] = _unescape(val)
        elif key == "LOCATION":
            ev["location"] = _unescape(val)
        elif key == "DESCRIPTION":
            ev["description"] = _unescape(val)
        elif key == "STATUS":
            ev["status"] = val.strip().upper()
        elif key == "DTSTART":
            ev["_start"] = _parse_dt(val, params)
            ev["all_day"] = params.get("VALUE") == "DATE" or (len(val.strip()) == 8 and "T" not in val)
        elif key == "DTEND":
            ev["_end"] = _parse_dt(val, params)
    if not ev.get("uid") and not ev.get("summary"):
        return None
    start, end = ev.pop("_start", None), ev.pop("_end", None)
    all_day = bool(ev.get("all_day"))
    ev["start_epoch"] = int(start.timestamp()) if start else 0
    ev["end_epoch"] = int(end.timestamp()) if end else 0
    ev["start_human"] = _human(start, all_day)
    ev["end_human"] = _human(end, all_day)
    ev.setdefault("summary", "")
    ev.setdefault("uid", "")
    return ev


def _contacts_from_vcard(vcf_text: str) -> list[dict]:
    """Parse VCARD(s) into {name, emails[], phones[], org}."""
    out: list[dict] = []
    cur: Optional[dict] = None
    for line in _unfold(vcf_text):
        s = line.strip()
        if s.upper() == "BEGIN:VCARD":
            cur = {"name": "", "emails": [], "phones": [], "org": ""}
            continue
        if s.upper() == "END:VCARD":
            if cur and (cur["name"] or cur["emails"] or cur["phones"]):
                out.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        key, _params, val = _split_prop(line)
        val = _unescape(val)
        if key == "FN":
            cur["name"] = val
        elif key == "N" and not cur["name"]:
            cur["name"] = " ".join(p for p in val.replace(";", " ").split() if p)
        elif key == "EMAIL":
            if val and val not in cur["emails"]:
                cur["emails"].append(val)
        elif key == "TEL":
            if val and val not in cur["phones"]:
                cur["phones"].append(val)
        elif key == "ORG":
            cur["org"] = val.replace(";", " ").strip()
    return out


# ---------------------------------------------------------------- helpers

def _coerce_dt(value) -> dt.datetime:
    """Accept a datetime, epoch (int/float/numeric str), or ISO-ish string →
    aware UTC datetime. Raises CalDavError on anything unparseable."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_UTC)
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, _UTC)
    s = str(value or "").strip()
    if not s:
        raise CalDavError("missing date/time")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return dt.datetime.fromtimestamp(float(s), _UTC)
    iso = s.replace("Z", "+00:00").replace(" ", "T", 1) if "T" not in s and " " in s else s.replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=_UTC)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=_UTC)
        except ValueError:
            continue
    raise CalDavError(f"unparseable date/time: {value!r}")


def _ical_utc(d: dt.datetime) -> str:
    return d.astimezone(_UTC).strftime("%Y%m%dT%H%M%SZ")


def _extract_dav_texts(xml_text: str, ns: str, local: str) -> list[tuple[str, str]]:
    """From a multistatus body, return [(href, element-text)] for every {ns}local."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise CalDavError(f"could not parse DAV response: {e}")
    out: list[tuple[str, str]] = []
    for resp in root.iter("{%s}response" % DAV_NS):
        href_el = resp.find("{%s}href" % DAV_NS)
        href = (href_el.text or "").strip() if href_el is not None else ""
        for el in resp.iter("{%s}%s" % (ns, local)):
            if el.text and el.text.strip():
                out.append((href, el.text))
    return out


# ---------------------------------------------------------------- public: calendar

def list_events(calendar_url: str, username: str, password: str,
                start=None, end=None, limit: int = 100) -> dict:
    """REPORT calendar-query over `calendar_url` for VEVENTs in [start, end].
    Defaults to a 30-day window from now. Returns {count, events:[...]} newest-last."""
    calendar_url = (calendar_url or "").strip()
    if not calendar_url:
        raise CalDavError("missing calendar URL")
    now = dt.datetime.now(_UTC)
    d_start = _coerce_dt(start) if start else now
    d_end = _coerce_dt(end) if end else (now + dt.timedelta(days=30))
    limit = max(1, min(int(limit or 100), MAX_EVENTS))

    body = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        '<D:prop><D:getetag/><C:calendar-data/></D:prop>'
        '<C:filter><C:comp-filter name="VCALENDAR">'
        '<C:comp-filter name="VEVENT">'
        f'<C:time-range start="{_ical_utc(d_start)}" end="{_ical_utc(d_end)}"/>'
        '</C:comp-filter></C:comp-filter></C:filter>'
        '</C:calendar-query>'
    )
    status, text, _ = _dav_request("REPORT", calendar_url, username, password,
                                   body=body, depth="1")
    if status not in (200, 207):
        raise CalDavError(f"calendar REPORT returned HTTP {status}")
    events: list[dict] = []
    for href, cal_data in _extract_dav_texts(text, CALDAV_NS, "calendar-data"):
        ev = _event_from_ics(cal_data)
        if ev:
            ev["href"] = href
            events.append(ev)
    events.sort(key=lambda e: e.get("start_epoch") or 0)
    events = events[:limit]
    return {"calendar_url": calendar_url, "count": len(events), "events": events}


def create_event(calendar_url: str, username: str, password: str, *,
                 summary: str, start, end=None, description: str = "",
                 location: str = "", uid: str = "") -> dict:
    """PUT a new VEVENT into the calendar collection. `start`/`end` accept a
    datetime, epoch, or ISO string; a missing `end` defaults to start + 1h.
    Returns {uid, href, summary, start_human}."""
    calendar_url = (calendar_url or "").strip().rstrip("/")
    if not calendar_url:
        raise CalDavError("missing calendar URL")
    if not (summary or "").strip():
        raise CalDavError("event needs a summary")
    d_start = _coerce_dt(start)
    d_end = _coerce_dt(end) if end else (d_start + dt.timedelta(hours=1))
    uid = (uid or "").strip() or f"{uuid.uuid4().hex}@agent-chat"
    now = dt.datetime.now(_UTC)

    ics_lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Agent Chat//CalDAV//EN",
        "CALSCALE:GREGORIAN", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{_ical_utc(now)}",
        f"DTSTART:{_ical_utc(d_start)}", f"DTEND:{_ical_utc(d_end)}",
        f"SUMMARY:{_escape(summary)}",
    ]
    if description:
        ics_lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        ics_lines.append(f"LOCATION:{_escape(location)}")
    ics_lines += ["END:VEVENT", "END:VCALENDAR"]
    ics = "\r\n".join(ics_lines) + "\r\n"

    href = f"{calendar_url}/{uid}.ics"
    status, _text, _hdr = _dav_request(
        "PUT", href, username, password, body=ics,
        extra_headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
    )
    if status not in (200, 201, 204):
        raise CalDavError(f"create event returned HTTP {status}")
    return {"uid": uid, "href": href, "summary": summary,
            "start_human": _human(d_start, False), "start_epoch": int(d_start.timestamp())}


def delete_event(calendar_url: str, username: str, password: str,
                 uid: str = "", href: str = "") -> dict:
    """DELETE one event by full href or by uid (relative to the collection)."""
    calendar_url = (calendar_url or "").strip().rstrip("/")
    target = (href or "").strip()
    if not target:
        uid = (uid or "").strip()
        if not (calendar_url and uid):
            raise CalDavError("delete needs an href or (calendar_url + uid)")
        target = f"{calendar_url}/{uid}.ics"
    status, _text, _hdr = _dav_request("DELETE", target, username, password)
    if status not in (200, 204, 404):
        raise CalDavError(f"delete event returned HTTP {status}")
    return {"deleted": target, "status": status}


# ---------------------------------------------------------------- public: contacts

def list_contacts(addressbook_url: str, username: str, password: str,
                  limit: int = 200) -> dict:
    """REPORT addressbook-query over `addressbook_url`. Returns {count, contacts:[...]}."""
    addressbook_url = (addressbook_url or "").strip()
    if not addressbook_url:
        raise CalDavError("missing address book URL")
    limit = max(1, min(int(limit or 200), MAX_CONTACTS))
    body = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">'
        '<D:prop><D:getetag/><C:address-data/></D:prop>'
        '</C:addressbook-query>'
    )
    status, text, _ = _dav_request("REPORT", addressbook_url, username, password,
                                   body=body, depth="1")
    if status not in (200, 207):
        raise CalDavError(f"contacts REPORT returned HTTP {status}")
    contacts: list[dict] = []
    for _href, card in _extract_dav_texts(text, CARDDAV_NS, "address-data"):
        contacts.extend(_contacts_from_vcard(card))
    contacts.sort(key=lambda c: (c.get("name") or "").lower())
    contacts = contacts[:limit]
    return {"addressbook_url": addressbook_url, "count": len(contacts), "contacts": contacts}
