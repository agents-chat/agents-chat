"""Small URL validation helpers for stdlib HTTP probes.

``urllib`` supports local-file and custom URL handlers in addition to HTTP.  The
foreman reads probe targets from environment/config files, so validate the scheme
before handing a value to ``urlopen``.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def require_http_url(value: str) -> str:
    """Return *value* when it is an absolute HTTP(S) URL, otherwise raise.

    User-info is intentionally rejected.  Probe credentials do not belong in URLs,
    and rejecting them avoids accidentally disclosing secrets in sensor details.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("probe URL must be a non-empty absolute HTTP(S) URL")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("probe URL contains a control character")

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("probe URL scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("probe URL must not contain user-info")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("probe URL has an invalid port") from exc
    return value
