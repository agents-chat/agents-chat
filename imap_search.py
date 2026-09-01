"""Read-only IMAP mailbox search — the email belt tool's backend.

Small, dependency-free (stdlib only), mirroring web_search.py / reddit_search.py so
`app.py` can expose it at GET /api/tools/email/search. Works against ANY IMAP host
with a plain password / App Password (no OAuth): Gmail / Google Workspace over
`imap.gmail.com`, or a cPanel / Dovecot mailbox (e.g. InMotion
`secure318.inmotionhosting.com:993`). The mailbox is ALWAYS selected read-only, so a
search never marks anything read and can never send.

When the host is Gmail we use Gmail's `X-GM-RAW` extension so agents can pass real
Gmail search syntax (`from:`, `newer_than:`, `has:attachment`, …). For every other
host we translate a small, common query grammar (`from:`, `to:`, `subject:`,
`since:`, `newer_than:Nd`, `is:unread`, free text) into standard RFC 3501 SEARCH
keys, so the same tool works everywhere.

Access is enforced in app.py (per-agent signed token + per-agent vaulted credential);
this module only takes an account + password (+ host) and returns structured results.
"""
from __future__ import annotations

import email
import html
import imaplib
import re
import time
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
DEFAULT_SINCE_DAYS = 14
CONNECT_TIMEOUT = 20.0


class EmailSearchError(Exception):
    """Raised for auth failures, bad queries, or connection problems."""


def _is_gmail(host: str) -> bool:
    h = (host or "").lower()
    return "gmail.com" in h or "googlemail.com" in h


def _decode(s: str) -> str:
    if not s:
        return ""
    out = []
    for part, enc in decode_header(s):
        out.append(part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part)
    return "".join(out).strip()


def _extract_text(msg):
    """Return (plain, html) best-effort decoded bodies for a message (either may be
    ""). Shared by the snippet (triage preview) and the full-body reader so both see
    the same content — the first text/plain and first text/html parts, skipping
    attachments."""
    plain = htmlb = ""
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition")):
                continue
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not htmlb:
                htmlb = text
    else:
        try:
            text = (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            htmlb = text
        else:
            plain = text
    return plain, htmlb


def _strip_html(text: str) -> str:
    """Drop comments/style/script and tags, turning block ends into newlines so
    forwarded / nested content keeps its line structure."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|blockquote)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def _snippet(msg, limit: int = 220) -> str:
    """Best-effort readable snippet: prefer text/plain, fall back to stripped HTML.
    Strips URLs and collapses whitespace — a compact one-line preview for triage."""
    plain, htmlb = _extract_text(msg)
    text = plain or htmlb
    if not text:
        return ""
    if not plain:  # strip HTML
        text = _strip_html(text)
    text = html.unescape(text)
    text = re.sub(r"[​-‍⁠﻿͏\xad]", "", text).replace("\xa0", " ")
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _body(msg, limit: int = 20000) -> str:
    """Full decoded, readable body — what an agent needs to actually READ a message
    rather than triage it. Prefers text/plain, falls back to stripped HTML. Unlike the
    snippet it KEEPS URLs (opt-out / action links matter when reading) and PRESERVES
    line breaks, so a forwarded email's nested original stays legible below the cover
    note. Capped at `limit` chars as a safety bound against huge messages."""
    plain, htmlb = _extract_text(msg)
    if plain:
        text = plain
    elif htmlb:
        text = _strip_html(htmlb)
    else:
        return ""
    text = html.unescape(text)
    text = re.sub(r"[​-‍⁠﻿͏\xad]", "", text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def _imap_quote(v: str) -> str:
    """RFC 3501 quoted-string for a single SEARCH value (may contain spaces)."""
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _since_str(days: int) -> str:
    return time.strftime("%d-%b-%Y", time.localtime(time.time() - max(1, days) * 86400))


_KV_RE = re.compile(r'(?P<key>\w+):(?P<val>"[^"]*"|\S+)')


def _standard_search_args(query: str, since_days: int):
    """Translate our small query grammar into RFC 3501 SEARCH tokens for non-Gmail
    hosts. Returns (charset, [args]). Multiple criteria are ANDed by the server.
    Recognized: from:, to:, subject:, since:YYYY-MM-DD, newer_than:Nd, is:unread /
    is:read (also `unseen`/`seen`); everything left over becomes a TEXT match. When
    no date is given we bound the window to `since_days` so we never dump a mailbox."""
    q = (query or "").strip()
    if not q:
        return (None, ["(SINCE " + _imap_quote(_since_str(since_days)) + ")"])

    args: list[str] = []
    has_date = False
    remainder = q
    for m in list(_KV_RE.finditer(q)):
        key = m.group("key").lower()
        val = m.group("val")
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        consumed = True
        if key == "from":
            args += ["FROM", _imap_quote(val)]
        elif key == "to":
            args += ["TO", _imap_quote(val)]
        elif key in ("subject", "subj"):
            args += ["SUBJECT", _imap_quote(val)]
        elif key == "since":
            # accept YYYY-MM-DD or DD-Mon-YYYY; fall back silently on garbage
            args += ["SINCE", _imap_quote(_normalize_date(val))]
            has_date = True
        elif key in ("newer_than", "newer"):
            m2 = re.match(r"(\d+)\s*d", val, re.I)
            if m2:
                args += ["SINCE", _imap_quote(_since_str(int(m2.group(1))))]
                has_date = True
            else:
                consumed = False
        elif key == "is":
            if val.lower() in ("unread", "unseen"):
                args.append("UNSEEN")
            elif val.lower() in ("read", "seen"):
                args.append("SEEN")
            else:
                consumed = False
        else:
            consumed = False
        if consumed:
            remainder = remainder.replace(m.group(0), " ", 1)

    for bare in ("unseen", "unread"):
        if re.search(rf"(?<!\w){bare}(?!\w)", remainder, re.I) and "UNSEEN" not in args:
            args.append("UNSEEN")
            remainder = re.sub(rf"(?<!\w){bare}(?!\w)", " ", remainder, flags=re.I)

    free = re.sub(r"\s+", " ", remainder).strip().strip('"')
    if free:
        args += ["TEXT", _imap_quote(free)]
    if not has_date:
        args += ["SINCE", _imap_quote(_since_str(since_days))]
    if not args:  # pathological: only produced date above, but keep a safety net
        args = ["(SINCE " + _imap_quote(_since_str(since_days)) + ")"]

    charset = None
    if any(ord(ch) > 127 for ch in q):
        charset = "UTF-8"
    return (charset, args)


def _normalize_date(v: str) -> str:
    v = v.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        try:
            return time.strftime("%d-%b-%Y", time.strptime(v[:10], "%Y-%m-%d"))
        except Exception:
            pass
    return v  # assume already DD-Mon-YYYY


def _gmail_search_args(query: str, since_days: int):
    """Gmail: hand the raw query to X-GM-RAW (one quoted argument), or a date window."""
    q = (query or "").strip()
    if q:
        return (None, ["X-GM-RAW", _imap_quote(q)])
    return (None, ["(SINCE " + _imap_quote(_since_str(since_days)) + ")"])


def email_search(account: str, app_password: str, query: str = "",
                 limit: int = DEFAULT_LIMIT, since_days: int = DEFAULT_SINCE_DAYS,
                 authuser: str = "0", imap_host: str = DEFAULT_IMAP_HOST,
                 imap_port: int = DEFAULT_IMAP_PORT, full: bool = False) -> dict:
    """Read-only search of `account`'s inbox on `imap_host`. Returns a JSON-able dict:
      {account, host, query, count, messages: [{id, from, from_email, subject,
       date_epoch, date_human, snippet, gmail_link}]}  (newest first). For non-Gmail
    hosts `gmail_link` is empty; the message `id` (RFC822 Message-ID) still identifies
    each message for a reply.

    When `full` is true, each message also carries a `body` field: the complete
    decoded, readable message text (URLs and line breaks preserved), not just the
    ~220-char `snippet`. This is what lets an agent actually READ a message — e.g. a
    forwarded email whose original notice is nested below the sender's cover note.
    Because full bodies are large, pair `full` with a narrow query / small limit."""
    account = (account or "").strip()
    imap_host = (imap_host or DEFAULT_IMAP_HOST).strip() or DEFAULT_IMAP_HOST
    try:
        imap_port = int(imap_port or DEFAULT_IMAP_PORT)
    except (TypeError, ValueError):
        imap_port = DEFAULT_IMAP_PORT
    gmail = _is_gmail(imap_host)
    # Gmail shows App Passwords as space-separated groups whose real value has none,
    # so strip ALL whitespace there. For other hosts the password is user-chosen and
    # may legitimately contain spaces — only trim paste artifacts (incl. \xa0).
    app_password = re.sub(r"\s", "", app_password or "") if gmail else (app_password or "").strip()
    if not account or not app_password:
        raise EmailSearchError("missing account or password")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    try:
        M = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=CONNECT_TIMEOUT)
    except Exception as e:
        raise EmailSearchError(f"IMAP connect failed ({imap_host}:{imap_port}): {e}")
    try:
        try:
            M.login(account, app_password)
        except imaplib.IMAP4.error as e:
            raise EmailSearchError(f"IMAP login failed (check the password / IMAP enabled): {e}")
        M.select("INBOX", readonly=True)  # readonly => never marks mail read
        charset, args = (_gmail_search_args if gmail else _standard_search_args)(query, since_days)
        typ, data = M.search(charset, *args)
        if typ != "OK":
            raise EmailSearchError("IMAP search rejected the query")
        ids = data[0].split() if data and data[0] else []
        ids = ids[-limit:]  # newest matches

        out = []
        for num in reversed(ids):
            typ, d = M.fetch(num, "(RFC822)")
            if typ != "OK" or not d or not d[0]:
                continue
            msg = email.message_from_bytes(d[0][1])
            try:
                dt = parsedate_to_datetime(msg.get("Date"))
            except Exception:
                dt = None
            name, addr = parseaddr(msg.get("From", ""))
            msgid = (msg.get("Message-ID") or f"noid-{num.decode()}").strip().strip("<>")
            item = {
                "id": msgid,
                "from": _decode(name) or addr,
                "from_email": addr.lower(),
                "subject": _decode(msg.get("Subject")),
                "date_epoch": int(dt.timestamp()) if dt else 0,
                "date_human": dt.astimezone().strftime("%b %-d, %Y %-I:%M %p") if dt else "",
                "snippet": _snippet(msg),
                "gmail_link": (f"https://mail.google.com/mail/u/{authuser}/#search/rfc822msgid:{msgid}"
                               if gmail else ""),
            }
            if full:
                item["body"] = _body(msg)
            out.append(item)
        return {"account": account, "host": imap_host, "query": query,
                "count": len(out), "messages": out}
    finally:
        try:
            M.logout()
        except Exception:
            pass
