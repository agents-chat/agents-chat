"""General web search for Agent Chat agents, via the Tavily Search API.

One shared, deterministic web-search capability every agent (and the orchestrator)
can call — rather than depending on whichever agent happens to have its own
browser. Includes a Reddit-scoped convenience (``include_domains=['reddit.com']``)
so the "search Reddit" ask works today even though Reddit's own Data API is gated
behind an approval request. When official Reddit OAuth credentials exist, the
/api/tools/reddit endpoint prefers the structured Reddit API instead (see
reddit_search.py); this module is the general-purpose and fallback path.

Key: ``TAVILY_API_KEY`` (Secrets vault -> agents/shared.env, loaded into the app).
Free tier: 1,000 searches/month, no credit card. https://app.tavily.com
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger("web_search")

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"

_VALID_DEPTH = {"basic", "advanced"}
_VALID_TOPIC = {"general", "news"}
_VALID_TIME = {"day", "week", "month", "year"}


class WebSearchError(RuntimeError):
    """Raised when web search is misconfigured or the provider request fails."""


def provider_configured() -> bool:
    """True when a Tavily API key is present in the environment."""
    return bool((os.environ.get("TAVILY_API_KEY") or "").strip())


def _api_key() -> str:
    key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if not key:
        raise WebSearchError(
            "Web search is not configured. Add a TAVILY_API_KEY "
            "(free at https://app.tavily.com) via the Secrets panel."
        )
    return key


def _domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        return urlparse(url).netloc or None
    except Exception:
        return None


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "snippet": (item.get("content") or "")[:800],
        "score": item.get("score"),
        "published_date": item.get("published_date"),
        "source": _domain(item.get("url")),
    }


async def web_search(
    query: str,
    *,
    count: int = 10,
    site: str | None = None,
    sites: list[str] | None = None,
    search_depth: str = "basic",
    topic: str = "general",
    time_range: str | None = None,
    include_answer: bool = False,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Search the web via Tavily.

    query         - search terms (required)
    count         - 1..20 results
    site / sites  - restrict to one or more domains (e.g. "reddit.com")
    search_depth  - "basic" (1 credit) or "advanced" (2 credits)
    topic         - "general" or "news"
    time_range    - optional recency filter: day | week | month | year
    include_answer- ask Tavily for a synthesized answer alongside results
    """
    if not query or not query.strip():
        raise WebSearchError("query is required")
    count = max(1, min(int(count or 10), 20))
    search_depth = search_depth if search_depth in _VALID_DEPTH else "basic"
    topic = topic if topic in _VALID_TOPIC else "general"

    include_domains: list[str] = []
    if site:
        include_domains.append(str(site).strip())
    if sites:
        include_domains.extend(str(s).strip() for s in sites if str(s).strip())

    payload: dict[str, Any] = {
        "query": query.strip(),
        "search_depth": search_depth,
        "topic": topic,
        "max_results": count,
        "include_answer": bool(include_answer),
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if time_range in _VALID_TIME:
        payload["time_range"] = time_range

    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(TAVILY_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise WebSearchError(f"Web search request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise WebSearchError(
                f"Tavily rejected the API key ({resp.status_code}). Check TAVILY_API_KEY."
            )
        if resp.status_code == 429:
            raise WebSearchError(
                "Web search rate/credit limit hit (429). Free tier is 1,000 searches/month."
            )
        if resp.status_code != 200:
            raise WebSearchError(
                f"Web search failed ({resp.status_code}): {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise WebSearchError(f"Web search response was not JSON: {exc}") from exc

    results = [_normalize(r) for r in (data.get("results") or [])]
    out: dict[str, Any] = {
        "query": query.strip(),
        "provider": "tavily",
        "count": len(results),
        "results": results,
    }
    if include_domains:
        out["include_domains"] = include_domains
    if data.get("answer"):
        out["answer"] = data["answer"]
    return out


async def reddit_web_search(query: str, *, count: int = 10, **kw: Any) -> dict[str, Any]:
    """Reddit-scoped web search (include_domains=['reddit.com'])."""
    result = await web_search(query, count=count, site="reddit.com", **kw)
    result["scope"] = "reddit"
    return result


async def fetch_url(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Read a web page's clean text via Tavily Extract (same API key). Tavily does
    the fetch, so this is SSRF-safe (the app never fetches an agent-supplied URL
    itself) — no need to block internal/loopback addresses here."""
    if not url or not url.strip():
        raise WebSearchError("url is required")
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {"urls": [u], "extract_depth": "basic"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(TAVILY_EXTRACT_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise WebSearchError(f"Page fetch failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise WebSearchError(
                f"Tavily rejected the API key ({resp.status_code}). Check TAVILY_API_KEY."
            )
        if resp.status_code == 429:
            raise WebSearchError(
                "Fetch rate/credit limit hit (429). Free tier is 1,000 requests/month."
            )
        if resp.status_code != 200:
            raise WebSearchError(f"Page fetch failed ({resp.status_code}): {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise WebSearchError(f"Fetch response was not JSON: {exc}") from exc

    results = data.get("results") or []
    if not results:
        failed = data.get("failed_results") or []
        why = ""
        if failed and isinstance(failed[0], dict):
            why = f": {failed[0].get('error') or 'blocked or empty'}"
        raise WebSearchError(f"Could not read {u}{why}")
    top = results[0]
    content = top.get("raw_content") or ""
    return {
        "url": top.get("url") or u,
        "length": len(content),
        "content": content[:20000],
        "truncated": len(content) > 20000,
    }
