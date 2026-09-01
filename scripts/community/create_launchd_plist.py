#!/usr/bin/env python3
"""Write the user-scoped macOS launchd job for Community Edition."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import tempfile
from pathlib import Path


DEFAULT_LABEL = "com.agentchat.community"


def write_plist(
    destination: Path,
    app_root: Path,
    community_home: Path,
    python: Path,
    *,
    label: str = DEFAULT_LABEL,
    port: int = 8086,
) -> None:
    destination = destination.expanduser().resolve()
    logs = community_home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": [str(app_root / "scripts/community/run_macos.sh")],
        "EnvironmentVariables": {
            "AGENT_CHAT_HOME": str(community_home),
            "AGENT_CHAT_PYTHON": str(python),
            "PORT": str(port),
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(logs / "agent-chat.log"),
        "StandardErrorPath": str(logs / "agent-chat.error.log"),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            plistlib.dump(payload, stream, sort_keys=True)
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--community-home", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--port", type=int, default=8086)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{2,127}", args.label):
        parser.error("--label must be a launchd-style identifier")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    write_plist(
        args.output,
        args.app_root.resolve(),
        args.community_home.expanduser().resolve(),
        args.python.expanduser().absolute(),
        label=args.label,
        port=args.port,
    )
    print(f"Created launchd job at {args.output.expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
