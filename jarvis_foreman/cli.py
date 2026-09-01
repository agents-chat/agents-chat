#!/usr/bin/env python3
"""CLI: python -m jarvis_foreman.cli [status|tick|enable|disable|set-senior ID|set-control-chat ID]"""

from __future__ import annotations

import json
import sys

from .bus import ForemanBus
from .config import load_config, save_config


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    cfg = load_config()

    if cmd == "status":
        print(
            json.dumps(
                {
                    "enabled": cfg.enabled,
                    "senior": cfg.senior_id(),
                    "junior": cfg.seat_roster_id("junior_pm"),
                    "path": str(cfg.path),
                    "sensors": {
                        k: v.get("enabled")
                        for k, v in (cfg.raw.get("sensors") or {}).items()
                    },
                    "channels": {
                        k: v.get("enabled")
                        for k, v in (cfg.raw.get("channels") or {}).items()
                    },
                },
                indent=2,
            )
        )
        return 0

    if cmd == "enable":
        cfg.raw["enabled"] = True
        save_config(cfg)
        print("foreman enabled")
        return 0

    if cmd == "disable":
        cfg.raw["enabled"] = False
        save_config(cfg)
        print("foreman disabled")
        return 0

    if cmd == "set-senior":
        if len(argv) < 2:
            print("usage: set-senior <roster_agent_id>", file=sys.stderr)
            return 2
        cfg.raw.setdefault("seats", {}).setdefault("senior_pm", {})[
            "roster_agent_id"
        ] = argv[1]
        save_config(cfg)
        print(f"senior_pm → {argv[1]}")
        return 0

    if cmd == "set-control-chat":
        if len(argv) < 2:
            print("usage: set-control-chat <chat_id|null>", file=sys.stderr)
            return 2
        raw = argv[1].strip().lower()
        if raw in ("null", "none", "clear", "-"):
            cfg.raw["control_chat_id"] = None
        else:
            cfg.raw["control_chat_id"] = int(raw)
        save_config(cfg)
        print(f"control_chat_id → {cfg.raw.get('control_chat_id')}")
        return 0

    if cmd == "tick":
        bus = ForemanBus(cfg)
        force = "--force" in argv
        if force:
            bus.cfg.raw["enabled"] = True
        print(json.dumps(bus.tick(), indent=2, default=str))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
