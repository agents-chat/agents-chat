#!/usr/bin/env python3
"""Create a private, user-scoped Community Edition configuration."""

from __future__ import annotations

import argparse
import os
import secrets
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "config.community.env"


def _replace_line(text: str, key: str, value: str) -> str:
    prefix = f"{key}="
    lines = text.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{prefix}{value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{prefix}{value}")
    return "\n".join(lines) + "\n"


def build_config(
    display_name: str,
    admin_email: str,
    port: int = 8086,
    admin_password: str | None = None,
) -> str:
    admin_password = admin_password or secrets.token_urlsafe(18)
    text = TEMPLATE.read_text(encoding="utf-8")
    text = _replace_line(text, "ORCHESTRATOR_ACCESS_TOKEN", secrets.token_urlsafe(48))
    text = _replace_line(text, "COMMUNITY_FIRST_RUN_SETUP", "1")
    text = _replace_line(text, "USER_DISPLAY_NAME", display_name)
    text = _replace_line(text, "ORCHESTRATOR_ADMIN_NAME", display_name)
    text = _replace_line(text, "ORCHESTRATOR_ADMIN_PREFERRED_NAME", display_name)
    text = _replace_line(text, "ORCHESTRATOR_ADMIN_EMAIL", admin_email)
    text = _replace_line(text, "ORCHESTRATOR_ADMIN_PASSWORD", admin_password)
    text = _replace_line(text, "ORCHESTRATOR_PUBLIC_URL", f"http://127.0.0.1:{port}")
    text = _replace_line(
        text,
        "ALLOWED_ORIGINS",
        f"http://127.0.0.1:{port},http://localhost:{port}",
    )
    return text


def write_private_config(destination: Path, text: str, *, force: bool = False) -> None:
    destination = destination.expanduser().resolve()
    if destination.exists() and not force:
        raise FileExistsError(f"configuration already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--display-name", default="Owner")
    parser.add_argument("--admin-email", default="admin@localhost")
    parser.add_argument("--port", type=int, default=8086)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    try:
        admin_password = secrets.token_urlsafe(18)
        write_private_config(
            args.output,
            build_config(
                args.display_name.strip() or "Owner",
                args.admin_email.strip(),
                args.port,
                admin_password,
            ),
            force=args.force,
        )
    except (FileExistsError, OSError) as exc:
        parser.error(str(exc))
    print(f"Created private configuration at {args.output.expanduser()}")
    print("\nAgents Chat is ready for you")
    print("  Your browser will ask for your name, a username or email, and password.")
    print("  There is no temporary password to copy or save.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
