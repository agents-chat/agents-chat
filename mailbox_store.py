"""Per-agent mailbox store — lets ONE agent hold SEVERAL mailboxes, each with its
own authority level.

Storage is a JSON file per agent (agents/<id>.mailboxes.json, chmod 600, gitignored
like the rest of agents/). This module is pure CRUD + a provisioning CLI; the
semantic layer (building IMAP/SMTP configs, applying authority defaults, resolving
which box a tool call targets) lives in app.py so this file stays dependency-free.

Each mailbox entry (all fields optional except account):
  {
    "id": "work",                       # short handle the agent uses to pick this box
    "label": "Work mailbox",
    "account": "agent@example.com",
    "password": "...",
    "imap_host": "mail.example.com",    # blank => Gmail default
    "imap_port": 993, "smtp_host": "...", "smtp_port": 465,
    "from_name": "Agent Chat",
    "authority": "send",                # read | draft | send   (email power)
    "caldav_url": "https://dav.example.com/calendars/<email>/calendar",
    "carddav_url": "https://dav.example.com/addressbooks/<email>/addressbook",
    "caldav_username": "", "caldav_password": "",   # blank => reuse account/password
    "dav_authority": "write"            # read | write          (calendar power)
  }

Email authority ladder: read ⊂ draft ⊂ send (each includes the ones before).
DAV authority: read (view events/contacts) ⊂ write (also create/delete events).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

EMAIL_AUTHORITIES = ("read", "draft", "send")
DAV_AUTHORITIES = ("read", "write")

# Fields we persist. Anything else on an incoming dict is dropped, so a stray
# secret or typo can't ride along into the store.
_FIELDS = (
    "id", "label", "account", "password", "imap_host", "imap_port",
    "smtp_host", "smtp_port", "from_name", "authority",
    "caldav_url", "carddav_url", "caldav_username", "caldav_password", "dav_authority",
)


def default_path(agents_dir, agent_id: str) -> Path:
    return Path(agents_dir) / f"{agent_id}.mailboxes.json"


def load(path) -> dict:
    """Return {"mailboxes": [...]}. Missing/invalid file => empty (never raises)."""
    p = Path(path)
    if not p.is_file():
        return {"mailboxes": []}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {"mailboxes": []}
    boxes = data.get("mailboxes") if isinstance(data, dict) else None
    if not isinstance(boxes, list):
        return {"mailboxes": []}
    return {"mailboxes": [b for b in boxes if isinstance(b, dict) and b.get("account")]}


def _clean(entry: dict) -> dict:
    out = {k: entry[k] for k in _FIELDS if k in entry and entry[k] not in (None, "")}
    if not out.get("id"):
        acct = str(out.get("account", "")).strip()
        out["id"] = (acct.split("@", 1)[0] or "mailbox").strip()
    out["id"] = str(out["id"]).strip()
    return out


def save(path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    boxes = [_clean(b) for b in data.get("mailboxes", []) if isinstance(b, dict) and b.get("account")]
    p.write_text(json.dumps({"mailboxes": boxes}, indent=2))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def get(path, mailbox_id: str) -> Optional[dict]:
    mid = (mailbox_id or "").strip().lower()
    for b in load(path)["mailboxes"]:
        if str(b.get("id", "")).strip().lower() == mid:
            return b
    return None


def add(path, mailbox: dict) -> dict:
    """Upsert a mailbox. Matches an existing entry by id (case-insensitive), else by
    account. Returns the stored (cleaned) entry."""
    entry = _clean(dict(mailbox))
    if not entry.get("account"):
        raise ValueError("mailbox needs an account")
    data = load(path)
    boxes = data["mailboxes"]
    mid = entry["id"].lower()
    acct = entry["account"].lower()
    for i, b in enumerate(boxes):
        if str(b.get("id", "")).lower() == mid or str(b.get("account", "")).lower() == acct:
            boxes[i] = entry
            break
    else:
        boxes.append(entry)
    save(path, {"mailboxes": boxes})
    return entry


def remove(path, mailbox_id: str) -> bool:
    mid = (mailbox_id or "").strip().lower()
    data = load(path)
    kept = [b for b in data["mailboxes"] if str(b.get("id", "")).strip().lower() != mid]
    if len(kept) == len(data["mailboxes"]):
        return False
    save(path, {"mailboxes": kept})
    return True


def masked(path) -> list:
    """Store contents with secrets hidden — safe to show in a UI/CLI."""
    out = []
    for b in load(path)["mailboxes"]:
        m = dict(b)
        for secret in ("password", "caldav_password"):
            if m.get(secret):
                m[secret] = "••••"
        out.append(m)
    return out


# ------------------------------------------------------------------ CLI

def _agents_dir_default() -> Path:
    return Path(os.path.dirname(os.path.abspath(__file__))) / "agents"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Manage an agent's mailboxes (agents/<id>.mailboxes.json).")
    p.add_argument("--agents-dir", default=str(_agents_dir_default()))
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="list an agent's mailboxes (secrets masked)")
    lp.add_argument("--agent", required=True)

    ap = sub.add_parser("add", help="add or update a mailbox")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--id", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--account", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--host", default="", help="sets both imap+smtp host (blank => Gmail)")
    ap.add_argument("--imap-host", default="")
    ap.add_argument("--smtp-host", default="")
    ap.add_argument("--imap-port", default="")
    ap.add_argument("--smtp-port", default="")
    ap.add_argument("--from-name", default="")
    ap.add_argument("--authority", default="send", choices=EMAIL_AUTHORITIES)
    ap.add_argument("--caldav-url", default="")
    ap.add_argument("--carddav-url", default="")
    ap.add_argument("--caldav-username", default="")
    ap.add_argument("--caldav-password", default="")
    ap.add_argument("--dav-authority", default="read", choices=DAV_AUTHORITIES)

    rp = sub.add_parser("remove", help="remove a mailbox by id")
    rp.add_argument("--agent", required=True)
    rp.add_argument("--id", required=True)

    args = p.parse_args(argv)
    path = default_path(args.agents_dir, args.agent)

    if args.cmd == "list":
        boxes = masked(path)
        if not boxes:
            print(f"(no mailboxes for {args.agent})")
        for b in boxes:
            print(f"- {b.get('id')}: {b.get('account')}  [email:{b.get('authority','?')} "
                  f"dav:{b.get('dav_authority','-') if (b.get('caldav_url') or b.get('carddav_url')) else '-'}]")
        return 0

    if args.cmd == "add":
        entry = {
            "id": args.id, "label": args.label, "account": args.account, "password": args.password,
            "imap_host": args.imap_host or args.host, "smtp_host": args.smtp_host or args.host,
            "imap_port": args.imap_port, "smtp_port": args.smtp_port, "from_name": args.from_name,
            "authority": args.authority, "caldav_url": args.caldav_url, "carddav_url": args.carddav_url,
            "caldav_username": args.caldav_username, "caldav_password": args.caldav_password,
            "dav_authority": args.dav_authority,
        }
        stored = add(path, entry)
        print(f"saved mailbox '{stored['id']}' ({stored['account']}) for {args.agent} → {path}")
        return 0

    if args.cmd == "remove":
        ok = remove(path, args.id)
        print(f"removed '{args.id}'" if ok else f"no mailbox '{args.id}' for {args.agent}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
