"""Reddit Data API search — official application-only OAuth2 (client_credentials).

Server-side, no end-user login. Credentials come from the environment
(``REDDIT_CLIENT_ID`` / ``REDDIT_CLIENT_SECRET``); the bearer token is cached
in-process until shortly before it expires. Two async helpers are exposed:

    reddit_search(query, subreddit=..., sort=..., time_filter=..., limit=...)
    subreddit_search(query, limit=...)

This is the STRUCTURED path (exact scores/comment counts). It only works once a
Reddit Data API app is approved and its credentials are configured — Reddit gated
new legacy Data API apps behind a review request. Until then the tool endpoint
(/api/tools/reddit/search) falls back to a reddit.com-scoped web search; see
web_search.reddit_web_search. Callers should gate on ``credentials_configured()``.

Reddit reference: token endpoint ``https://www.reddit.com/api/v1/access_token`` with
HTTP Basic auth (client_id:client_secret) and ``grant_type=client_credentials``;
authenticated calls go to ``https://oauth.reddit.com`` with a descriptive User-Agent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("reddit")

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"

# Reddit asks for a unique, descriptive UA: <platform>:<app id>:<version> (by /u/<user>)
_DEFAULT_UA = "web:agent-chat:1.0 (by /u/go-to-sleep-its3am)"

_VALID_SORTS = {"relevance", "hot", "top", "new", "comments"}
_VALID_TIMES = {"hour", "day", "week", "month", "year", "all"}
# Reddit subreddit names are [A-Za-z0-9_] (max 21). Validating the normalized value
# closes path manipulation ('..', '/', '?', '#') of the authenticated OAuth GET.
_SUBREDDIT_RE = re.compile(r"[A-Za-z0-9_]{1,50}")


class RedditError(RuntimeError):
    """Raised when Reddit authentication or a search request fails."""


class _TokenCache:
    def __init__(self) -> None:
        self.token: Optional[str] = None
        self.expires_at: float = 0.0
        self.lock = asyncio.Lock()


_cache = _TokenCache()


def _user_agent() -> str:
    return (os.environ.get("REDDIT_USER_AGENT") or _DEFAULT_UA).strip()


def _credentials() -> tuple[str, str]:
    client_id = (os.environ.get("REDDIT_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("REDDIT_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise RedditError(
            "Reddit API credentials are not configured. "
            "Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET (Secrets panel)."
        )
    return client_id, client_secret


def credentials_configured() -> bool:
    """True when both Reddit credentials are present in the environment."""
    return bool((os.environ.get("REDDIT_CLIENT_ID") or "").strip()
                and (os.environ.get("REDDIT_CLIENT_SECRET") or "").strip())


async def _get_token(client: httpx.AsyncClient, *, force: bool = False) -> str:
    """Return a cached bearer token, refreshing via client_credentials if needed."""
    async with _cache.lock:
        now = time.time()
        if not force and _cache.token and now < _cache.expires_at - 60:
            return _cache.token
        client_id, client_secret = _credentials()
        try:
            resp = await client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"User-Agent": _user_agent()},
            )
        except httpx.HTTPError as exc:
            raise RedditError(f"Reddit token request failed: {exc}") from exc
        if resp.status_code == 401:
            raise RedditError(
                "Reddit rejected the client credentials (401). "
                "Check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET."
            )
        if resp.status_code != 200:
            raise RedditError(
                f"Reddit token request failed ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RedditError(f"Reddit token response missing access_token: {data}")
        _cache.token = token
        _cache.expires_at = now + int(data.get("expires_in") or 3600)
        return token


def _normalize_post(child: dict[str, Any]) -> dict[str, Any]:
    d = child.get("data", {}) or {}
    permalink = d.get("permalink")
    return {
        "id": d.get("id"),
        "fullname": d.get("name"),
        "title": d.get("title"),
        "subreddit": d.get("subreddit"),
        "author": d.get("author"),
        "score": d.get("score"),
        "upvote_ratio": d.get("upvote_ratio"),
        "num_comments": d.get("num_comments"),
        "created_utc": d.get("created_utc"),
        "permalink": f"https://www.reddit.com{permalink}" if permalink else None,
        "url": d.get("url"),
        "is_self": d.get("is_self"),
        "over_18": d.get("over_18"),
        "selftext": (d.get("selftext") or "")[:500],
    }


def _normalize_subreddit(child: dict[str, Any]) -> dict[str, Any]:
    d = child.get("data", {}) or {}
    return {
        "name": d.get("display_name"),
        "title": d.get("title"),
        "subscribers": d.get("subscribers"),
        "over_18": d.get("over18"),
        "url": f"https://www.reddit.com{d.get('url')}" if d.get("url") else None,
        "description": (d.get("public_description") or "")[:500],
    }


async def _oauth_get(path: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        token = await _get_token(client)
        headers = {"Authorization": f"bearer {token}", "User-Agent": _user_agent()}
        try:
            resp = await client.get(API_BASE + path, params=params, headers=headers)
            if resp.status_code == 401:
                # Cached token may be stale — force one refresh and retry.
                token = await _get_token(client, force=True)
                headers["Authorization"] = f"bearer {token}"
                resp = await client.get(API_BASE + path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise RedditError(f"Reddit search request failed: {exc}") from exc
        if resp.status_code == 429:
            raise RedditError("Reddit rate limit hit (429). Slow down and retry shortly.")
        if resp.status_code != 200:
            raise RedditError(
                f"Reddit search failed ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()


async def reddit_search(
    query: str,
    subreddit: str | None = None,
    sort: str = "relevance",
    time_filter: str = "all",
    limit: int = 25,
    restrict_sr: bool = True,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Search Reddit posts via the official Data API (requires credentials).

    query        - search terms (required)
    subreddit    - restrict to r/<subreddit>; None searches all of Reddit
    sort         - relevance | hot | top | new | comments
    time_filter  - hour | day | week | month | year | all (applies to top/relevance)
    limit        - 1..100 results
    restrict_sr  - when a subreddit is given, keep results inside it
    """
    if not query or not query.strip():
        raise RedditError("query is required")
    sort = sort if sort in _VALID_SORTS else "relevance"
    time_filter = time_filter if time_filter in _VALID_TIMES else "all"
    limit = max(1, min(int(limit or 25), 100))

    params: dict[str, Any] = {
        "q": query.strip(),
        "sort": sort,
        "t": time_filter,
        "limit": limit,
        "type": "link",
        "raw_json": 1,
    }
    if subreddit:
        sr = subreddit.strip().lstrip("/").removeprefix("r/").strip("/")
        if not _SUBREDDIT_RE.fullmatch(sr):
            raise RedditError(f"invalid subreddit name: {subreddit!r}")
        path = f"/r/{sr}/search"
        params["restrict_sr"] = "true" if restrict_sr else "false"
    else:
        sr = None
        path = "/search"

    data = await _oauth_get(path, params, timeout)
    children = (data.get("data", {}) or {}).get("children", []) or []
    posts = [_normalize_post(c) for c in children if c.get("kind") == "t3"]
    return {
        "query": query.strip(),
        "subreddit": sr,
        "sort": sort,
        "time": time_filter,
        "count": len(posts),
        "results": posts,
    }


async def subreddit_search(query: str, limit: int = 10, timeout: float = 20.0) -> dict[str, Any]:
    """Find subreddits matching ``query`` (useful before a scoped search)."""
    if not query or not query.strip():
        raise RedditError("query is required")
    limit = max(1, min(int(limit or 10), 100))
    params = {"q": query.strip(), "limit": limit, "raw_json": 1}
    data = await _oauth_get("/subreddits/search", params, timeout)
    children = (data.get("data", {}) or {}).get("children", []) or []
    subs = [_normalize_subreddit(c) for c in children if c.get("kind") == "t5"]
    return {"query": query.strip(), "count": len(subs), "results": subs}
