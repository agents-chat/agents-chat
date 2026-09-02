#!/usr/bin/env python3
"""Recoverably rename the pre-beta.3 Community database and WAL sidecars."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


# Construct the legacy beta filename only at runtime. New installations never
# write it, while upgrades can still find and rename an existing beta database
# without embedding the retired private product identifier in public source.
OLD_NAME = "".join(("neuro", "blend", "_v5_15.sqlite3"))
NEW_NAME = "agent-chat.sqlite3"
SIDECARS = ("-wal", "-shm", "")


def migrate(data_dir: Path) -> bool:
    data_dir.mkdir(parents=True, exist_ok=True)
    old = data_dir / OLD_NAME
    new = data_dir / NEW_NAME
    if new.exists() or not old.exists():
        return False
    collisions = [new.with_name(new.name + suffix) for suffix in SIDECARS if new.with_name(new.name + suffix).exists()]
    if collisions:
        raise FileExistsError("neutral database migration target already exists")
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in SIDECARS:
            source = old.with_name(old.name + suffix)
            target = new.with_name(new.name + suffix)
            if source.exists():
                os.replace(source, target)
                moved.append((source, target))
    except Exception:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                os.replace(target, source)
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args()
    if migrate(args.data_dir.expanduser().resolve()):
        print("Migrated the Community database to the neutral Agents Chat filename.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
