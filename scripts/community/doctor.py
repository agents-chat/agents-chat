#!/usr/bin/env python3
"""Value-safe preflight checks for Agents Chat Community Edition."""

from __future__ import annotations

import argparse
import json
import os
import platform
import stat
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def parse_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def check(config: Path, data_dir: Path, expected_platform: str = "auto") -> list[dict[str, str]]:
    results: list[dict[str, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append({"check": name, "status": "pass" if ok else "fail", "detail": detail})

    detected = "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else sys.platform
    platform_ok = expected_platform == "any" or expected_platform == "auto" or detected == expected_platform
    record("platform", platform_ok, f"detected {detected} on {platform.machine() or 'unknown architecture'}")
    required = ("app.py", "community_config.py", "requirements.lock", "templates/index.html")
    missing = [item for item in required if not (ROOT / item).is_file()]
    record("application-files", not missing, "complete" if not missing else f"missing {', '.join(missing)}")

    if not config.is_file():
        record("configuration", False, f"missing {config}")
        return results

    values = parse_config(config)
    record("community-edition", values.get("AGENT_CHAT_EDITION") == "community", "community profile selected")
    record("authentication", values.get("REQUIRE_AUTH") == "1", "authentication enabled")
    token = values.get("ORCHESTRATOR_ACCESS_TOKEN", "")
    record(
        "generated-token",
        len(token) >= 40 and token != "GENERATED_BY_INSTALLER",
        "present and non-placeholder" if len(token) >= 40 else "missing, short, or placeholder",
    )
    if sys.platform == "win32" or expected_platform == "windows":
        record("config-protection", True, "stored in the current Windows user profile")
    else:
        mode = stat.S_IMODE(config.stat().st_mode)
        record("config-protection", mode & 0o077 == 0, f"mode {mode:04o}")
    record(
        "loopback-cookie",
        values.get("SESSION_COOKIE_SECURE") == "0",
        "compatible with local HTTP",
    )

    try:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = data_dir / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("data-directory", True, f"writable {data_dir}")
    except OSError as exc:
        record("data-directory", False, f"not writable: {exc.__class__.__name__}")

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:
        record("python-runtime", False, f"missing module {exc.name}")
    else:
        record("python-runtime", True, f"Python {sys.version_info.major}.{sys.version_info.minor}")
    return results


def main() -> int:
    if sys.platform == "win32":
        default_home = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Agent Chat"
    else:
        default_home = Path.home() / "Library" / "Application Support" / "Agent Chat"
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=default_home / "config.env")
    parser.add_argument("--data-dir", type=Path, default=default_home / "data")
    parser.add_argument("--platform", choices=("auto", "macos", "windows", "any"), default="auto")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = check(args.config.expanduser(), args.data_dir.expanduser(), args.platform)
    if args.json:
        print(json.dumps({"checks": results}, indent=2))
    else:
        for result in results:
            marker = "PASS" if result["status"] == "pass" else "FAIL"
            print(f"[{marker}] {result['check']}: {result['detail']}")
    return 0 if all(item["status"] == "pass" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
