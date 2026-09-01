"""Email draft-writer / sender — the write-side sibling of imap_search.py.

Small, dependency-free (stdlib only). Uses the same credential as the read-only
search tool (agents/<id>.env → EMAIL_ACCOUNT + EMAIL_APP_PASSWORD), and the same
host settings (EMAIL_IMAP_HOST / EMAIL_SMTP_HOST / *_PORT, default Gmail). Works
against Gmail / Workspace or a cPanel / Dovecot mailbox (InMotion, etc.).

Two operations:
  * DRAFT: compose a new message or a threaded reply and APPEND it to the Drafts
    folder with the \\Draft flag. Nothing is transmitted.
  * SEND: transmit immediately over SMTP. For non-Gmail hosts the sent message is
    also APPENDed to the Sent folder (\\Seen) so it shows up in the user's mail
    client — Gmail does that server-side, generic SMTP does not.

There is intentionally NO delete / move / label / mark-read code in this module —
its only mailbox mutations are appending a draft or a sent copy.

CLI:
  python3 imap_compose.py draft --to a@b.com --subject "Hi" --body "..."
  python3 imap_compose.py reply --msgid "<id@mail.gmail.com>" --body "..."
  python3 imap_compose.py reply --query "from:jane subject:invoice" --body-file /tmp/r.txt
  python3 imap_compose.py reply --msgid "<id>" --body "..." --send    # actually sends
  python3 imap_compose.py list-drafts
  python3 imap_compose.py draft --env agents/codex.env --to ...        # a different mailbox
"""
from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from email.header import decode_header
from email.utils import (formataddr, formatdate, getaddresses, make_msgid,
                         parseaddr, parsedate_to_datetime)

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465  # SSL
CONNECT_TIMEOUT = 20.0
DEFAULT_FROM_NAME = "Agents Chat User"
DEFAULT_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents", "claude.env")


class EmailComposeError(Exception):
    """Raised for auth failures, missing originals, or connection problems."""


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


def _env_config(env_path: str = DEFAULT_ENV) -> dict:
    """Read account/password/host settings from an agents/<id>.env file."""
    vals: dict[str, str] = {}
    wanted = {
        "EMAIL_ACCOUNT", "EMAIL_APP_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_IMAP_PORT",
        "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT", "EMAIL_FROM_NAME",
    }
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k in wanted:
                    vals[k] = v.strip().strip('"').strip("'")
    except OSError as e:
        raise EmailComposeError(f"cannot read credentials file {env_path}: {e}")
    return make_config(
        account=vals.get("EMAIL_ACCOUNT", ""),
        password=vals.get("EMAIL_APP_PASSWORD", ""),
        imap_host=vals.get("EMAIL_IMAP_HOST", ""),
        imap_port=vals.get("EMAIL_IMAP_PORT", ""),
        smtp_host=vals.get("EMAIL_SMTP_HOST", ""),
        smtp_port=vals.get("EMAIL_SMTP_PORT", ""),
        from_name=vals.get("EMAIL_FROM_NAME", ""),
        _require=True,
        _src=env_path,
    )


def make_config(*, account: str, password: str, imap_host: str = "", imap_port="",
                smtp_host: str = "", smtp_port="", from_name: str = "",
                _require: bool = True, _src: str = "") -> dict:
    """Normalize a mailbox config. Blank host/port fall back to Gmail defaults.
    app.py builds this straight from an agent's vault; the CLI builds it from a file."""
    account = (account or "").strip()
    host = (imap_host or "").strip() or DEFAULT_IMAP_HOST
    # See imap_search: strip-all only for Gmail App Passwords; elsewhere trim edges.
    password = re.sub(r"\s", "", password or "") if _is_gmail(host) else (password or "").strip()

    def _port(v, default):
        try:
            return int(str(v).strip()) if str(v).strip() else default
        except (TypeError, ValueError):
            return default

    if _require and (not account or not password):
        where = f" in {_src}" if _src else ""
        raise EmailComposeError(f"EMAIL_ACCOUNT / EMAIL_APP_PASSWORD missing{where}")
    return {
        "account": account,
        "password": password,
        "imap_host": host,
        "imap_port": _port(imap_port, DEFAULT_IMAP_PORT),
        "smtp_host": (smtp_host or "").strip() or DEFAULT_SMTP_HOST,
        "smtp_port": _port(smtp_port, DEFAULT_SMTP_PORT),
        "from_name": (from_name or "").strip() or DEFAULT_FROM_NAME,
    }


def _connect(cfg: dict) -> imaplib.IMAP4_SSL:
    try:
        M = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=CONNECT_TIMEOUT)
    except Exception as e:
        raise EmailComposeError(f"IMAP connect failed ({cfg['imap_host']}:{cfg['imap_port']}): {e}")
    try:
        M.login(cfg["account"], cfg["password"])
    except imaplib.IMAP4.error as e:
        raise EmailComposeError(f"IMAP login failed (check the password): {e}")
    return M


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')


def _quote_mailbox(name: str) -> str:
    return name if name.startswith('"') else f'"{name}"'


def _folder_exists(M: imaplib.IMAP4_SSL, name: str) -> bool:
    try:
        typ, _ = M.select(name, readonly=True)
        return typ == "OK"
    except Exception:
        return False


def _find_folder(M: imaplib.IMAP4_SSL, special_use: bytes, fallbacks: list[str]) -> str:
    """Locate a folder by RFC 6154 special-use attribute (\\Drafts, \\Sent, \\All …),
    which Gmail and modern Dovecot advertise in LIST. Falls back to trying common
    names (Gmail's [Gmail]/… and Dovecot's INBOX.… layouts). Returns a quoted name."""
    typ, data = M.list()
    if typ == "OK":
        for raw in data or []:
            if not isinstance(raw, bytes):
                continue
            m = _LIST_RE.match(raw)
            if not m:
                continue
            if special_use.lower() in m.group("flags").lower():
                name = m.group("name").strip().decode("utf-8", "replace")
                return _quote_mailbox(name)
    for fb in fallbacks:
        if _folder_exists(M, _quote_mailbox(fb)):
            return _quote_mailbox(fb)
    return _quote_mailbox(fallbacks[0]) if fallbacks else '"Drafts"'


def _drafts_folder(M: imaplib.IMAP4_SSL) -> str:
    return _find_folder(M, rb"\\Drafts", ["[Gmail]/Drafts", "Drafts", "INBOX.Drafts"])


def _sent_folder(M: imaplib.IMAP4_SSL) -> str:
    return _find_folder(M, rb"\\Sent", ["[Gmail]/Sent Mail", "Sent", "INBOX.Sent", "Sent Items"])


def _plain_body(msg) -> str:
    """Full text/plain body (or stripped HTML) of a message, for quoting."""
    plain = htmlb = ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if "attachment" in str(part.get("Content-Disposition")):
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
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
    text = plain
    if not text and htmlb:
        text = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "", htmlb, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _imap_quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch_original(M: imaplib.IMAP4_SSL, gmail: bool, msgid: str = "", query: str = ""):
    """Find the message being replied to. Gmail searches All Mail with X-GM-RAW;
    other hosts search INBOX / Sent / Archive by Message-ID (or a text query).
    Newest match wins."""
    msgid = (msgid or "").strip().strip("<>")
    query = (query or "").strip()
    if not msgid and not query:
        raise EmailComposeError("reply needs --msgid or --query")

    if gmail:
        folder = _find_folder(M, rb"\\All", ["[Gmail]/All Mail"])
        typ, _ = M.select(folder, readonly=True)
        if typ != "OK":
            raise EmailComposeError(f"cannot open {folder}")
        raw = f"rfc822msgid:{msgid}" if msgid else query
        typ, data = M.search(None, "X-GM-RAW", _imap_quote(raw))
        if typ != "OK":
            raise EmailComposeError("IMAP search rejected the query")
        ids = data[0].split() if data and data[0] else []
        if not ids:
            raise EmailComposeError(f"no message found for: {raw}")
        return _fetch_msg(M, ids[-1])

    # Non-Gmail: probe a few folders for the original.
    all_folder = _find_folder(M, rb"\\All", [])
    folders = ["INBOX", _sent_folder(M)]
    if all_folder and all_folder not in folders:
        folders.append(all_folder)
    for arch in ("Archive", "INBOX.Archive"):
        q = _quote_mailbox(arch)
        if q not in folders:
            folders.append(q)
    if msgid:
        crit = ["HEADER", "Message-ID", _imap_quote("<" + msgid + ">")]
    else:
        crit = ["TEXT", _imap_quote(query)]
    for folder in folders:
        try:
            typ, _ = M.select(folder, readonly=True)
        except Exception:
            continue
        if typ != "OK":
            continue
        typ, data = M.search(None, *crit)
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        if ids:
            return _fetch_msg(M, ids[-1])
    raise EmailComposeError(f"no message found for: {msgid or query}")


def _fetch_msg(M: imaplib.IMAP4_SSL, num):
    typ, d = M.fetch(num, "(RFC822)")
    if typ != "OK" or not d or not d[0]:
        raise EmailComposeError("could not fetch the original message")
    return email.message_from_bytes(d[0][1])


# No line-folding: Python's default folder RFC2047-mangles long In-Reply-To /
# References values (single tokens can't fold at whitespace), which breaks thread
# association. Servers accept long header lines.
_NOFOLD = email.policy.SMTP.clone(max_line_length=None)


def build_message(cfg: dict, to: str, subject: str, body: str,
                  cc: str = "", bcc: str = "") -> EmailMessage:
    account = cfg["account"]
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name") or DEFAULT_FROM_NAME, account))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=account.split("@", 1)[1])
    msg.set_content(body)  # body must be encoded under the default policy (3.9 quirk)
    msg.policy = _NOFOLD  # …but serialize headers unfolded
    return msg


def build_reply(cfg: dict, orig, body: str, reply_all: bool = False) -> EmailMessage:
    """Reply threaded into the original conversation: correct To / Re: subject /
    In-Reply-To / References, with the original quoted below the new text."""
    account = cfg["account"]
    name, addr = parseaddr(orig.get("Reply-To") or orig.get("From", ""))
    to = formataddr((_decode(name), addr)) if addr else ""
    if not to:
        raise EmailComposeError("original has no usable From/Reply-To address")
    cc = ""
    if reply_all:
        others = [formataddr((_decode(n), a))
                  for n, a in getaddresses([orig.get("To", ""), orig.get("Cc", "")])
                  if a and a.lower() not in (account.lower(), addr.lower())]
        cc = ", ".join(dict.fromkeys(others))

    subj = _decode(orig.get("Subject", ""))
    subject = subj if re.match(r"(?i)^\s*re\s*:", subj) else f"Re: {subj}"

    try:
        dt = parsedate_to_datetime(orig.get("Date"))
        when = dt.astimezone().strftime("%a, %b %-d, %Y at %-I:%M %p")
    except Exception:
        when = orig.get("Date", "")
    quoted = _plain_body(orig)
    quoted = "\n".join("> " + ln for ln in quoted.splitlines()) if quoted else ""
    attribution = f"On {when}, {_decode(name) or addr} <{addr}> wrote:"
    full = body.rstrip() + ("\n\n" + attribution + "\n" + quoted if quoted else "")

    msg = build_message(cfg, to, subject, full, cc=cc)
    omid = (orig.get("Message-ID") or "").strip()
    if omid:
        msg["In-Reply-To"] = omid
        refs = (orig.get("References", "") + " " + omid).split()
        if len(refs) > 10:  # keep first + last 9 so the unfolded line stays < 998 bytes
            refs = refs[:1] + refs[-9:]
        msg["References"] = " ".join(refs)
    return msg


def append_to(M: imaplib.IMAP4_SSL, folder: str, msg: EmailMessage, flags: str) -> str:
    typ, data = M.append(folder, flags, imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    if typ != "OK":
        raise EmailComposeError(f"APPEND to {folder} failed: {data}")
    return folder


def append_draft(M: imaplib.IMAP4_SSL, msg: EmailMessage) -> str:
    return append_to(M, _drafts_folder(M), msg, r"(\Draft)")


def smtp_send(cfg: dict, msg: EmailMessage) -> None:
    try:
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=CONNECT_TIMEOUT) as s:
            s.login(cfg["account"], cfg["password"])
            s.send_message(msg)
    except Exception as e:
        raise EmailComposeError(f"SMTP send failed ({cfg['smtp_host']}:{cfg['smtp_port']}): {e}")


def list_drafts(M: imaplib.IMAP4_SSL, limit: int = 10) -> list[dict]:
    folder = _drafts_folder(M)
    typ, _ = M.select(folder, readonly=True)
    if typ != "OK":
        raise EmailComposeError(f"cannot open {folder}")
    typ, data = M.search(None, "ALL")
    ids = data[0].split() if typ == "OK" and data and data[0] else []
    out = []
    for num in reversed(ids[-limit:]):
        typ, d = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (TO SUBJECT DATE MESSAGE-ID)])")
        if typ != "OK" or not d or not d[0]:
            continue
        hdr = email.message_from_bytes(d[0][1])
        out.append({"to": _decode(hdr.get("To", "")), "subject": _decode(hdr.get("Subject", "")),
                    "date": hdr.get("Date", ""), "message_id": (hdr.get("Message-ID") or "").strip()})
    return out


# ---- High-level operations (used by app.py's belt tool and the CLI) ----

def compose(cfg: dict, *, to: str = "", subject: str = "", body: str = "",
            cc: str = "", bcc: str = "", reply_to_msgid: str = "", reply_query: str = "",
            reply_all: bool = False, send: bool = False) -> dict:
    """Build a new message or a threaded reply, then either SEND it (SMTP, plus a
    Sent-folder copy on non-Gmail hosts) or save it as a DRAFT. Returns a JSON-able
    result dict. Opens exactly one IMAP connection and reuses it."""
    gmail = _is_gmail(cfg["imap_host"])
    is_reply = bool(reply_to_msgid or reply_query)
    M = _connect(cfg)
    try:
        if is_reply:
            orig = fetch_original(M, gmail, msgid=reply_to_msgid, query=reply_query)
            msg = build_reply(cfg, orig, body, reply_all=reply_all)
        else:
            if not to:
                raise EmailComposeError("a new message needs a 'to' recipient")
            msg = build_message(cfg, to, subject, body, cc=cc, bcc=bcc)

        result = {
            "to": msg["To"], "subject": msg["Subject"], "message_id": msg["Message-ID"],
            "from": msg["From"], "host": cfg["imap_host"],
        }
        if send:
            smtp_send(cfg, msg)
            result["action"] = "sent"
            if not gmail:  # Gmail auto-files SMTP sends; other hosts need an explicit copy
                try:
                    result["sent_folder"] = append_to(M, _sent_folder(M), msg, r"(\Seen)")
                except EmailComposeError:
                    result["sent_folder"] = None  # sent fine; just couldn't file a copy
        else:
            result["action"] = "draft"
            result["draft_folder"] = append_draft(M, msg)
        return result
    finally:
        try:
            M.logout()
        except Exception:
            pass


def send_email(cfg: dict, **kw) -> dict:
    kw["send"] = True
    return compose(cfg, **kw)


def save_draft(cfg: dict, **kw) -> dict:
    kw["send"] = False
    return compose(cfg, **kw)


def _read_body(args) -> str:
    if args.body is not None:
        return args.body
    if args.body_file:
        with open(args.body_file) as f:
            return f.read()
    return sys.stdin.read()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Save email drafts (default) or send with --send.")
    p.add_argument("--env", default=DEFAULT_ENV, help="env file holding EMAIL_ACCOUNT / EMAIL_APP_PASSWORD (+ host)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("draft", help="compose a new message → Drafts (or --send)")
    d.add_argument("--to", required=True)
    d.add_argument("--cc", default="")
    d.add_argument("--bcc", default="")
    d.add_argument("--subject", required=True)

    r = sub.add_parser("reply", help="compose a threaded reply → Drafts (or --send)")
    r.add_argument("--msgid", default="", help="Message-ID of the mail being answered")
    r.add_argument("--query", default="", help="search for the original (Gmail syntax on Gmail, text elsewhere)")
    r.add_argument("--reply-all", action="store_true")

    for sp in (d, r):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--body", default=None)
        g.add_argument("--body-file", default=None)
        sp.add_argument("--send", action="store_true",
                        help="TRANSMIT via SMTP instead of saving a draft (explicit opt-in)")

    l = sub.add_parser("list-drafts", help="newest drafts (read-only)")
    l.add_argument("--limit", type=int, default=10)

    args = p.parse_args(argv)
    cfg = _env_config(args.env)

    try:
        if args.cmd == "list-drafts":
            M = _connect(cfg)
            try:
                for dr in list_drafts(M, args.limit):
                    print(f"- {dr['date']} | to: {dr['to']} | {dr['subject']} | {dr['message_id']}")
            finally:
                try:
                    M.logout()
                except Exception:
                    pass
            return 0

        body = _read_body(args)
        if args.cmd == "draft":
            res = compose(cfg, to=args.to, subject=args.subject, body=body,
                          cc=args.cc, bcc=args.bcc, send=args.send)
        else:
            res = compose(cfg, body=body, reply_to_msgid=args.msgid, reply_query=args.query,
                          reply_all=args.reply_all, send=args.send)

        if res["action"] == "sent":
            print(f"SENT to {res['to']} — subject: {res['subject']}")
        else:
            print(f"DRAFT saved to {res.get('draft_folder')} — to: {res['to']} — "
                  f"subject: {res['subject']} — {res['message_id']}")
        return 0
    except EmailComposeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
