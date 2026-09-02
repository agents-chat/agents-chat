import importlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_community_config_is_portable_and_identity_neutral():
    text = (ROOT / "config.community.env").read_text(encoding="utf-8")
    lowered = text.lower()

    assert "agent_chat_edition=community" in lowered
    assert "session_cookie_secure=0" in lowered
    assert "require_auth=1" in lowered
    assert "agent_chat_github_repo_url=https://github.com/agents-chat/agents-chat" in lowered
    assert "/users/" not in lowered
    assert "user_display_name=owner" in lowered
    assert "orchestrator_admin_name=owner" in lowered
    assert "orchestrator_admin_preferred_name=owner" in lowered
    assert "@localhost" in lowered


def test_community_agents_are_generic_and_loopback_only(monkeypatch):
    monkeypatch.setenv("AGENT_CHAT_EDITION", "community")
    module = importlib.import_module("community_config")
    agents = module.community_agents()

    assert module.community_mode() is True
    assert {agent["id"] for agent in agents} == {
        "codex",
        "claude",
        "hermes",
        "minimax",
        "antigravity",
        "grok",
        "openrouter",
    }
    for agent in agents:
        assert agent["url"].startswith("http://127.0.0.1:")
        assert agent["name"]
        assert agent["token_env"].startswith("AGENT_TOKEN_")


def test_custom_env_file_is_loaded_before_repository_env(tmp_path, monkeypatch):
    config = tmp_path / "community.env"
    config.write_text("AGENT_CHAT_EDITION=community\nUSER_DISPLAY_NAME=Community Owner\n")
    monkeypatch.setenv("AGENT_CHAT_ENV_FILE", str(config))

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    custom_position = source.index('Path(_CUSTOM_ENV_FILE).expanduser()')
    repository_position = source.index('BASE_DIR / ".env"')
    assert custom_position < repository_position
    isolated_branch = source[custom_position:repository_position]
    assert "else:" in isolated_branch
    assert "isolation boundary" in source


def test_custom_env_is_authoritative_and_runtime_data_is_user_scoped(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = tmp_path / "community.env"
    config.write_text(
        "AGENT_CHAT_EDITION=community\n"
        "REQUIRE_AUTH=0\n"
        "DATA_DIR=\n"
        "STORAGE_DB=\n"
        "AGENT_TOKEN_CODEX=\n",
        encoding="utf-8",
    )
    inherited_calendar = tmp_path / "private-calendars.json"
    inherited_calendar.write_text('{"calendars": [{"url": "https://private.invalid"}]}')
    env = os.environ.copy()
    env.update({
        "AGENT_CHAT_ENV_FILE": str(config),
        "AGENT_CHAT_EDITION": "community",
        "DATA_DIR": str(data_dir),
        "STORAGE_DB": str(data_dir / "community.sqlite3"),
        "AGENT_CHAT_DB": str(data_dir / "community.sqlite3"),
        "CUSTOM_AGENTS_FILE": str(data_dir / "custom_agents.json"),
        "AGENT_TOKEN_CODEX": "must-not-survive",
        "CALENDAR_ICS_URL": "https://private.invalid/calendar.ics",
        "CALENDARS_STORE": str(inherited_calendar),
        "AGENTS": '[{"id":"private"}]',
        "AGENTCHAT_TEST": "1",
    })
    code = (
        "import json, os, app, calendar_ics; "
        "print(json.dumps({"
        "'data': str(app.DATA_DIR), "
        "'token': os.environ.get('AGENT_TOKEN_CODEX'), "
        "'legacy_calendar': os.environ.get('CALENDAR_ICS_URL'), "
        "'calendar_store': calendar_ics._STORE_PATH, "
        "'agent_env': str(app._agent_env_file('codex')), "
        "'workspaces': str(app.WORKSPACES_ROOT), "
        "'projects': str(app.PROJECTS_FOLDER_ROOT), "
        "'discover': [str(p) for p in app.WORKSPACE_DISCOVER_ROOTS], "
        "'agents': [a['id'] for a in app.AGENTS], "
        "'hermes_card': app._get_agent_card_override('hermes')"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert payload["data"] == str(data_dir)
    assert payload["token"] == ""
    assert payload["legacy_calendar"] is None
    assert payload["calendar_store"] == str(data_dir / "calendars.json")
    assert payload["agent_env"] == str(data_dir / "agents" / "codex.env")
    assert payload["workspaces"] == str(data_dir / "workspaces")
    assert payload["projects"] == str(data_dir / "projects")
    assert payload["discover"] == [str(data_dir / "projects")]
    assert payload["agents"] == [
        "codex", "claude", "hermes", "minimax", "antigravity", "grok",
        "openrouter",
    ]
    assert "home services" not in json.dumps(payload["hermes_card"])
    assert "contained agent runtime" in payload["hermes_card"]["best_for"]


def test_mit_license_and_public_security_policy_exist():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "MIT License" in license_text
    assert "Copyright (c) 2026" in license_text
    assert "Reporting a vulnerability" in security


def test_true_free_will_is_seeded_and_accepts_connected_agent_names(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = tmp_path / "community.env"
    config.write_text("AGENT_CHAT_EDITION=community\nREQUIRE_AUTH=0\n", encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "AGENT_CHAT_ENV_FILE": str(config),
        "AGENT_CHAT_EDITION": "community",
        "DATA_DIR": str(data_dir),
        "STORAGE_DB": str(data_dir / "community.sqlite3"),
        "AGENT_CHAT_DB": str(data_dir / "community.sqlite3"),
        "AGENTCHAT_TEST": "1",
    })
    code = """
import asyncio
import json
import app

s = app._get_skill('true-free-will-orchestration')
room, unknown = app._freewill_roster_from_inputs(
    {'agent_pool': '@codex, Claude, hermes'}, [a['id'] for a in app.AGENTS]
)

async def no_refresh():
    return None

app._refresh_capabilities = no_refresh
app._allowed_agent_ids_for_chat = lambda _cid: {a['id'] for a in app.AGENTS}
app._spawn_chat_task = lambda _cid, coro: coro.close()
cid = app._create_chat_record('True Free Will test', targets=[a['id'] for a in app.AGENTS])
applied = asyncio.run(app.apply_skill(
    s, cid, inputs={
        'topic': 'When should the orchestrator choose each agent?',
        'agent_pool': '@codex, Claude, hermes',
    }
))
print(json.dumps({
    'status': s['status'],
    'mode': s['mode'],
    'body': json.loads(s['body']),
    'steps': [x['agent_id'] for x in s['steps']],
    'inputs': [x['name'] for x in s['inputs']],
    'room': room,
    'unknown': unknown,
    'pool_is_not_topic': app._freewill_topic_from_inputs({'agent_pool': 'Codex, Claude'}),
    'applied_agents': applied['agents'],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert payload["status"] == "active"
    assert payload["mode"] == "freewill"
    assert payload["body"] == {"variant": "true", "rounds": 2, "questions": 8}
    assert payload["steps"] == [
        "codex", "claude", "hermes", "minimax", "antigravity", "grok",
    ]
    assert payload["inputs"] == ["topic", "agent_pool"]
    assert payload["room"] == ["codex", "claude", "hermes"]
    assert payload["unknown"] == []
    assert payload["pool_is_not_topic"] == ""
    assert payload["applied_agents"] == ["codex", "claude", "hermes"]


def test_whats_new_serves_the_public_history_in_community_mode(monkeypatch):
    from fastapi.testclient import TestClient

    import app as app_module

    client = TestClient(app_module.app)

    monkeypatch.setenv("AGENT_CHAT_EDITION", "community")
    community = client.get("/api/changelog")
    assert community.status_code == 200
    assert community.text.startswith("# Changelog")
    assert "## [0.1.0-beta.3]" in community.text


def test_community_report_problem_is_public_github_and_never_auto_attaches_chat():
    from conftest import read_frontend_source
    html = read_frontend_source()
    section = html[html.index("(function bindSupportTicket(){"):html.index("// ------------------------- Voice Chat")]

    assert "public GitHub issue" in html
    assert "Nothing is submitted until" in html
    assert "Review on GitHub" in section
    assert "window.open(issueUrl" in section
    assert "if (isCommunity)" in section
    community_branch = section[section.index("if (isCommunity)"):section.index("const scopeEl")]
    assert "/api/chats/" not in community_branch
    assert "include_chat" not in community_branch
    assert "support was notified" not in section


def test_fresh_community_usage_does_not_claim_unconfigured_subscription_costs(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "community_mode", lambda: True)
    monkeypatch.setattr(app_module, "_get_setting", lambda *_args, **_kwargs: "")
    subscriptions = app_module._usage_settings()["subscriptions"]

    assert subscriptions
    assert all(item["monthly_usd"] == 0 for item in subscriptions.values())
    assert all(
        item.get("credit_balance", 0) == 0
        for item in subscriptions.values()
        if item.get("kind") == "credits"
    )
