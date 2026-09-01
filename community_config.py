"""Portable defaults for the public Agent Chat Community Edition.

This module must stay free of personal names, companies, credentials, private
hosts, and machine-specific paths. Private deployments can keep their own agent
registry outside the public export and provide it with the ``AGENTS`` setting.
"""

from __future__ import annotations

import os


COMMUNITY_VERSION = "0.1.0-beta.1"


def community_agents() -> list[dict]:
    """Return the generic, local-only starter agent registry."""

    return [
        {
            "id": "codex",
            "name": "Codex",
            "url": os.environ.get(
                "CODEX_AGENT_URL", "http://127.0.0.1:55009/api/api_message"
            ),
            "color": "#778bfe",
            "emoji": "C",
            "icon_url": "/static/codex-icon.svg",
            "persona_url": "/static/agent-personas/codex.png?v=2",
            "token_env": "AGENT_TOKEN_CODEX",
            "voice": "Hands-on software engineer; prefers verified results.",
        },
        {
            "id": "claude",
            "name": "Claude",
            "url": os.environ.get(
                "CLAUDE_AGENT_URL", "http://127.0.0.1:55010/api/api_message"
            ),
            "color": "#cc785c",
            "emoji": "C",
            "persona_url": "/static/agent-personas/claude.png?v=3",
            "token_env": "AGENT_TOKEN_CLAUDE",
            "voice": "Reasoning partner and writer; flags uncertainty honestly.",
        },
        {
            "id": "hermes",
            "name": "Hermes",
            "url": os.environ.get(
                "HERMES_AGENT_URL", "http://127.0.0.1:55011/api/api_message"
            ),
            "color": "#4f8cff",
            "emoji": "H",
            "persona_url": "/static/agent-personas/hermes.png?v=2",
            "token_env": "AGENT_TOKEN_HERMES",
            "tagline": "Self-hosted Hermes agent",
            "voice": "General-purpose local agent; practical and candid.",
        },
        {
            "id": "minimax",
            "name": "MiniMax",
            "url": os.environ.get(
                "MINIMAX_AGENT_URL", "http://127.0.0.1:55012/api/api_message"
            ),
            "color": "#d95161",
            "emoji": "M",
            "persona_url": "/static/agent-personas/minimax.png?v=2",
            "token_env": "AGENT_TOKEN_MINIMAX",
            "tagline": "MiniMax models through a local bridge",
            "voice": "Quick, warm, and candid about uncertainty.",
        },
        {
            "id": "antigravity",
            "name": "Antigravity",
            "url": os.environ.get(
                "ANTIGRAVITY_AGENT_URL", "http://127.0.0.1:55014/api/api_message"
            ),
            "color": "#4285f4",
            "emoji": "A",
            "persona_url": "/static/agent-personas/antigravity.png?v=2",
            "token_env": "AGENT_TOKEN_ANTIGRAVITY",
            "tagline": "Google Antigravity through a local bridge",
            "voice": "Tool-using coding agent; capable and plainspoken.",
        },
        {
            "id": "grok",
            "name": "Grok",
            "url": os.environ.get(
                "GROK_AGENT_URL", "http://127.0.0.1:55019/api/api_message"
            ),
            "color": "#8b5cf6",
            "emoji": "G",
            "icon_url": "/static/grok-icon.svg",
            "persona_url": "/static/agent-personas/grok.png?v=2",
            "token_env": "AGENT_TOKEN_GROK",
            "tagline": "Grok CLI through a local bridge",
            "voice": "Direct, web-aware, and honest about uncertainty.",
        },
        {
            "id": "perplexity",
            "name": "Perplexity",
            "url": os.environ.get(
                "PERPLEXITY_AGENT_URL", "http://127.0.0.1:55023/api/api_message"
            ),
            "color": "#1f9d8a",
            "emoji": "P",
            "icon_url": "/static/perplexity-icon.svg",
            "token_env": "AGENT_TOKEN_PERPLEXITY",
            "tagline": "Signed-in Perplexity Computer through a local Mac bridge",
            "voice": "Source-first desktop operator; explicit about approvals and observed results.",
        },
        {
            "id": "openrouter",
            "name": "OpenRouter",
            "url": os.environ.get(
                "OPENROUTER_AGENT_URL", "http://127.0.0.1:55024/api/api_message"
            ),
            "color": "#7c5cff",
            "emoji": "O",
            "icon_url": "/static/openrouter-icon.svg",
            "token_env": "AGENT_TOKEN_OPENROUTER",
            "tagline": "Complete OpenRouter free-model catalog; paid routes blocked",
            "voice": "Free-model reasoning and analysis; explicit about API-only limits.",
        },
    ]


def community_mode() -> bool:
    return os.environ.get("AGENT_CHAT_EDITION", "").strip().lower() == "community"


def connector_setup_opt_in() -> bool:
    """True when a non-Community build has explicitly opted into the connector.

    Kept separate from community_mode() so callers can compose the two, and so a
    test that patches community_mode still controls the gate.
    """
    return os.environ.get("AGENT_CHAT_CONNECT_SETUP", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def connector_setup_enabled() -> bool:
    """True when the in-app 'connect an agent' flow should be served.

    The pairing/self-connect connector is edition-agnostic machinery that was
    only ever reachable in Community Edition, which left every other install
    with the manual fallback: hand-edit custom_agents.json and restart. Any
    build can now opt in with AGENT_CHAT_CONNECT_SETUP=1 and get the same
    verified, no-restart flow.

    This deliberately does NOT gate Community-specific *side effects* — notably
    the roster-authoritative grant sync and the startup bridge auto-launch,
    which stay behind community_mode() because they would fight launchd on a
    supervised install.
    """
    return community_mode() or connector_setup_opt_in()
