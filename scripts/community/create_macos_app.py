#!/usr/bin/env python3
"""Create a local, user-scoped macOS app launcher for Agents Chat."""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import tempfile
from pathlib import Path


BUNDLE_ID = "chat.agents.community"


def create_app(destination: Path, opener: Path) -> None:
    destination = destination.expanduser().absolute()
    opener = opener.expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        contents = temporary / "Contents"
        executable_root = contents / "MacOS"
        executable_root.mkdir(parents=True)
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleDevelopmentRegion": "en",
                    "CFBundleDisplayName": "Agents Chat",
                    "CFBundleExecutable": "Agents Chat",
                    "CFBundleIdentifier": BUNDLE_ID,
                    "CFBundleInfoDictionaryVersion": "6.0",
                    "CFBundleName": "Agents Chat",
                    "CFBundlePackageType": "APPL",
                    "CFBundleShortVersionString": "0.1.0",
                    "LSMinimumSystemVersion": "12.0",
                },
                stream,
                sort_keys=True,
            )
        executable = executable_root / "Agents Chat"
        executable.write_text(
            "#!/usr/bin/env bash\nexec " + shlex.quote(str(opener)) + "\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--opener", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"launcher already exists: {args.output}")
    if not args.opener.is_file():
        parser.error(f"opener is missing: {args.opener}")
    create_app(args.output, args.opener)
    print(f"Created Agents Chat app at {args.output.expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
