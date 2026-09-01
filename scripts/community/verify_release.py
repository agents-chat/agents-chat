#!/usr/bin/env python3
"""Verify public source or an immutable Community Edition release artifact.

Source mode scans only version-controlled files and deliberately ignores the
developer virtual environment. Artifact mode additionally enforces the signed
file manifest and rejects unlisted files. Neither mode contains maintainer-
specific names, clients, paths, or replacement rules.

Both modes also apply generic identity heuristics — shapes of personal data such
as absolute home directories, mailboxes, LAN addresses and mDNS hostnames — that
name nobody and are useful to anyone republishing this tree. A maintainer who
keeps an authoritative private denylist outside the release can additionally
pass `--policy <file>`; public users never do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MARKER = ROOT / ".community-release.json"
SELF = Path(__file__).resolve()
IGNORED_PARTS = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".tox", "dist", "build",
}
SECRET_PATTERNS = {
    "openai-shaped-secret": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github-shaped-secret": re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]+\b"),
    "google-shaped-secret": re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    "telegram-shaped-secret": re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "slack-shaped-secret": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+\b"),
    "private-key": re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
}
# --- Generic identity heuristics ---------------------------------------------
# These describe SHAPES of personal data. They contain nobody's name, no company
# and no path from any particular machine, so they ship safely and are worth
# running against any tree. Without them the builder's private policy — which is
# deliberately never published — would be the only gate standing between a
# maintainer's home directory, mailbox or LAN and a public release.
#
# Findings report a category and a filename only, never the matched text.

# The home-directory segment must start alphanumerically and be at least two
# characters long. That keeps elided documentation paths ("/Users/...") and the
# apostrophe-quoting example used in the Windows launcher notes (whose segment
# stops at the apostrophe after a single letter) from firing, while every
# realistic profile name still matches.
USER_HOME_PATTERN = re.compile(
    r"(?:(?<![\w.-])(?:/Users/|/home/)|(?<!\w)[A-Za-z]:\\{1,2}Users\\{1,2})"
    r"([A-Za-z0-9][A-Za-z0-9._-]+)"
)
# The build sanitizer rewrites the maintainer's home directory to a neutral
# placeholder, so a correctly sanitized tree legitimately contains one of these.
# Allowlisting the placeholder *names* keeps the path shape itself covered.
PLACEHOLDER_HOME_NAMES = {
    "all users", "default", "example", "owner", "public", "shared", "someone",
    "user", "username", "youruser", "yourname",
}
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
# Reserved names (RFC 2606, RFC 6761, RFC 6762) can never route to a real
# mailbox, so documentation and fixtures may use them freely.
RESERVED_EMAIL_TLDS = {"example", "invalid", "local", "localhost", "test"}
RESERVED_EMAIL_DOMAINS = {"example.com", "example.net", "example.org"}
# RFC 2142-style role mailboxes address a system, not a person. The domain of a
# role address can still leak an employer, which is what --policy is for.
ROLE_EMAIL_LOCAL_PARTS = {
    "abuse", "admin", "administrator", "contact", "hello", "help", "hostmaster",
    "info", "noc", "no-reply", "noreply", "postmaster", "security", "support",
    "sysadmin", "webmaster",
}
# A one- or two-character local part is a syntax placeholder ("a@b.com",
# "<id@mail.example>"), not somebody's mailbox.
PLACEHOLDER_EMAIL_LOCAL_LENGTH = 2
# RFC 1918 only. Link-local 169.254.0.0/16 is deliberately out of scope because
# 169.254.169.254 is the cloud metadata address every SSRF guard must name.
PRIVATE_IP_PATTERN = re.compile(
    r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])"
)
# An mDNS name is a specific machine on somebody's LAN. Only host positions
# count (scheme-prefixed or port-suffixed), so an address at a ".local" test
# domain or a variable spelled "config.local" is not mistaken for a hostname.
MDNS_HOST_PATTERN = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://[A-Za-z0-9][A-Za-z0-9-]*\.local\b"
    r"|(?<![\w.@-])[A-Za-z0-9][A-Za-z0-9-]*\.local:\d{2,5}\b)"
)
# `launchctl ... gui/<uid>` bakes the numeric account id of whoever wrote the
# command into the docs; the portable spelling is gui/$(id -u).
MACOS_USER_ID_PATTERN = re.compile(r"\bgui/\d+\b")
# Mirrors the builder: legal attribution is intentionally public in the license
# and the owner-approved trademark policy, so a persona-name blocker from a
# --policy file must not fire there. Every other file stays covered.
LEGAL_ATTRIBUTION_FILES = {"LICENSE", "TRADEMARKS.md"}
LEGAL_ATTRIBUTION_CATEGORY = "private-persona"
BINARY_METADATA_MARKERS = (
    b"<x:" + b"xmpmeta",
    b"http://ns.adobe.com/" + b"xap/1.0/",
    b"Canva " + b"(Renderer)",
)
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
# The double-clickable launchers (*.cmd on Windows, *.command on macOS) are
# scanned like any other shipped text file; omitting them left the six most
# user-facing files in the release unswept for secrets.
TEXT_SUFFIXES = {
    "", ".bat", ".cmd", ".command", ".css", ".env", ".html", ".ini", ".js",
    ".json", ".md", ".lock", ".mjs", ".plist", ".ps1", ".py", ".sh", ".svg",
    ".swift", ".toml", ".txt", ".yaml", ".yml",
}


class PolicyError(Exception):
    """An unusable --policy file, reported as a message instead of a traceback."""


def load_policy_patterns(path: Path) -> dict[str, re.Pattern[str]]:
    """Compile the optional maintainer-side denylist.

    This script ships publicly, so it holds no private strings of its own. A
    maintainer keeps the authoritative blocker list in the private tree and
    points at it with --policy to run the same scan the builder runs.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"cannot read policy file {path}: {exc.strerror or exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PolicyError(f"policy file {path} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"policy file {path} must contain a JSON object")
    blockers = data.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        raise PolicyError(f'policy file {path} has no non-empty "blockers" list')
    compiled: dict[str, re.Pattern[str]] = {}
    for index, item in enumerate(blockers):
        if not isinstance(item, dict) or not isinstance(item.get("pattern"), str) or not item["pattern"]:
            raise PolicyError(f'policy file {path}: blocker #{index} needs a non-empty "pattern"')
        category = str(item.get("category") or f"policy-blocker-{index}")
        try:
            compiled[category] = re.compile(
                item["pattern"], re.IGNORECASE if item.get("ignore_case") else 0
            )
        except re.error as exc:
            raise PolicyError(
                f"policy file {path}: blocker {category!r} has an invalid regex: {exc}"
            ) from exc
    return compiled


def _has_user_home_path(text: str) -> bool:
    for match in USER_HOME_PATTERN.finditer(text):
        if match.group(1).lower() not in PLACEHOLDER_HOME_NAMES:
            return True
    return False


def _has_personal_email(text: str) -> bool:
    for match in EMAIL_PATTERN.finditer(text):
        local, _, domain = match.group(0).partition("@")
        domain = domain.lower()
        if domain.rsplit(".", 1)[-1] in RESERVED_EMAIL_TLDS:
            continue
        if any(domain == d or domain.endswith("." + d) for d in RESERVED_EMAIL_DOMAINS):
            continue
        if local.lower() in ROLE_EMAIL_LOCAL_PARTS:
            continue
        if len(local) <= PLACEHOLDER_EMAIL_LOCAL_LENGTH:
            continue
        return True
    return False


IDENTITY_CHECKS = {
    "user-home-path": _has_user_home_path,
    "personal-email-address": _has_personal_email,
    "private-network-address": lambda text: PRIVATE_IP_PATTERN.search(text) is not None,
    "mdns-hostname": lambda text: MDNS_HOST_PATTERN.search(text) is not None,
    "macos-user-id": lambda text: MACOS_USER_ID_PATTERN.search(text) is not None,
}


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _ignored(path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in path.relative_to(ROOT).parts)


def _read_marker() -> tuple[dict, list[dict[str, str]]]:
    if not MARKER.is_file():
        return {}, [{"category": "missing-release-marker", "path": MARKER.name}]
    try:
        marker = json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, [{"category": "invalid-release-marker", "path": MARKER.name}]
    findings = []
    if marker.get("edition") != "community":
        findings.append({"category": "wrong-edition", "path": MARKER.name})
    return marker, findings


def _tracked_source_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        relative_paths = [p for p in result.stdout.decode().split("\0") if p]
        return [ROOT / p for p in relative_paths if (ROOT / p).is_file()]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        marker, _ = _read_marker()
        marked = marker.get("files") or {}
        if marked:
            return [ROOT / p for p in sorted(marked) if (ROOT / p).is_file()]
        return [
            path for path in sorted(ROOT.rglob("*"))
            if path.is_file() and not _ignored(path)
        ]


def _scan_files(
    paths: list[Path], policy: dict[str, re.Pattern[str]] | None = None
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in sorted(paths):
        if not path.is_file() or path.resolve() == SELF or _ignored(path):
            continue
        relative = _relative(path)
        if relative in seen:
            continue
        seen.add(relative)
        if path.suffix.lower() in SENSITIVE_SUFFIXES:
            findings.append({"category": "sensitive-file-extension", "path": relative})
        try:
            raw = path.read_bytes()
        except OSError:
            findings.append({"category": "unreadable-file", "path": relative})
            continue
        if any(marker in raw for marker in BINARY_METADATA_MARKERS):
            findings.append({"category": "embedded-author-metadata", "path": relative})
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = raw.decode("utf-8", errors="ignore")
        for category, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append({"category": category, "path": relative})
        for category, check in IDENTITY_CHECKS.items():
            if check(text):
                findings.append({"category": category, "path": relative})
        for category, pattern in (policy or {}).items():
            if relative in LEGAL_ATTRIBUTION_FILES and category == LEGAL_ATTRIBUTION_CATEGORY:
                continue
            if pattern.search(text):
                findings.append({"category": category, "path": relative})
    return findings


def verify_source(policy: dict[str, re.Pattern[str]] | None = None) -> list[dict[str, str]]:
    return _scan_files(_tracked_source_files(), policy)


def verify_artifact(policy: dict[str, re.Pattern[str]] | None = None) -> list[dict[str, str]]:
    marker, findings = _read_marker()
    expected_files = marker.get("files") or {}
    for relative, expected in sorted(expected_files.items()):
        path = ROOT / relative
        if not path.is_file():
            findings.append({"category": "missing-marked-file", "path": relative})
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            findings.append({"category": "hash-mismatch", "path": relative})
    actual_files: dict[str, Path] = {}
    unexpected_ignored_roots: set[str] = set()
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        parts = path.relative_to(ROOT).parts
        if ".git" in parts:
            continue
        ignored_part = next((part for part in parts if part in IGNORED_PARTS), None)
        if ignored_part:
            unexpected_ignored_roots.add(ignored_part + "/")
            continue
        actual_files[_relative(path)] = path
    allowed = set(expected_files) | {MARKER.name, _relative(SELF)}
    for relative in sorted(set(actual_files) - allowed):
        findings.append({"category": "unexpected-artifact-file", "path": relative})
    for relative in sorted(unexpected_ignored_roots):
        findings.append({"category": "unexpected-artifact-path", "path": relative})
    findings.extend(_scan_files(list(actual_files.values()), policy))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source", action="store_true", help="scan public source without enforcing artifact hashes")
    mode.add_argument("--artifact", action="store_true", help="verify immutable release hashes and scan every artifact file")
    parser.add_argument(
        "--policy", type=Path,
        help="optional maintainer-side denylist (JSON with a \"blockers\" list) applied on top of the built-in checks",
    )
    args = parser.parse_args()
    mode_name = "artifact" if args.artifact else "source"
    policy = None
    if args.policy is not None:
        try:
            policy = load_policy_patterns(args.policy)
        except PolicyError as exc:
            print(json.dumps({"ok": False, "mode": mode_name, "error": str(exc), "findings": []}, indent=2))
            return 2
    findings = verify_artifact(policy) if args.artifact else verify_source(policy)
    findings = sorted(findings, key=lambda item: (item["category"], item["path"]))
    print(json.dumps({"ok": not findings, "mode": mode_name, "findings": findings}, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
