"""Outbound-host validation for credentialed Community integrations."""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import ParseResult, urlparse


PRIVATE_HOSTS_ENV = "AGENT_CHAT_ALLOW_PRIVATE_INTEGRATION_HOSTS"


class UnsafeRemoteHost(ValueError):
    """Raised when credentials would be sent to an unsafe remote target."""


def private_integration_hosts_allowed() -> bool:
    return os.environ.get(PRIVATE_HOSTS_ENV, "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def validate_remote_host(host: str, port: int, *, purpose: str) -> str:
    """Resolve *host* and require every address to be globally routable."""
    value = str(host or "").strip().strip("[]").rstrip(".")
    if not value:
        raise UnsafeRemoteHost(f"{purpose} host is missing")
    if private_integration_hosts_allowed():
        return value
    lowered = value.lower()
    if lowered == "localhost" or lowered.endswith(".localhost") or lowered.endswith(".local"):
        raise UnsafeRemoteHost(
            f"{purpose} host is private; set {PRIVATE_HOSTS_ENV}=1 only for a server you trust"
        )
    try:
        rows = socket.getaddrinfo(value, int(port), type=socket.SOCK_STREAM)
    except (OSError, TypeError, ValueError) as exc:
        raise UnsafeRemoteHost(f"could not resolve {purpose} host") from exc
    addresses = {row[4][0].split("%", 1)[0] for row in rows if row[4]}
    if not addresses:
        raise UnsafeRemoteHost(f"could not resolve {purpose} host")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeRemoteHost(f"{purpose} host resolved unexpectedly") from exc
        if not parsed.is_global:
            raise UnsafeRemoteHost(
                f"{purpose} host resolves to a private or reserved address; "
                f"set {PRIVATE_HOSTS_ENV}=1 only for a server you trust"
            )
    return value


def validate_https_url(url: str, *, purpose: str) -> ParseResult:
    """Require a credential-safe HTTPS URL whose host resolves publicly."""
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port or 443
    except ValueError as exc:
        raise UnsafeRemoteHost(f"{purpose} URL is invalid") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise UnsafeRemoteHost(f"{purpose} URL must use https://")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeRemoteHost(f"{purpose} URL must not contain credentials")
    validate_remote_host(parsed.hostname, port, purpose=purpose)
    return parsed
