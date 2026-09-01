import asyncio
import json
from pathlib import Path
import subprocess
from unittest import mock

import app
import pytest


APP_DIR = Path(__file__).resolve().parents[1]
FRONTEND = (APP_DIR / "static" / "app-runtime.js").read_text(encoding="utf-8")


def test_perplexity_is_a_managed_first_party_agent():
    agent = app.AGENTS_BY_ID["perplexity"]
    assert agent["url"] == "http://127.0.0.1:55023/api/api_message"
    assert agent["token_env"] == "AGENT_TOKEN_PERPLEXITY"
    assert app.MANAGED_BRIDGES["perplexity"]["health_url"].endswith(":55023/health")


def test_perplexity_exposes_desktop_computer_capabilities():
    support = app.AGENT_CAPABILITIES["perplexity"]["supports"]
    for key in (
        "web_search", "citations", "local_computer", "local_files",
        "native_apps", "connectors", "desktop_approvals",
    ):
        assert support[key] is True
    assert app.AGENT_CAPABILITIES["perplexity"]["files"] == "approved-local-folders"
    assert app.AGENT_CAPABILITIES["perplexity"]["exec"] is True
    assert app.AGENT_CAPABILITIES["perplexity"]["ui"]["surface"] == "desktop"


def test_perplexity_desktop_runtime_and_controls_stay_in_sync():
    assert app.PERPLEXITY_MODEL_OPTIONS == ("perplexity-computer",)
    for value in app.PERPLEXITY_MODEL_OPTIONS:
        assert f"'{value}'" in FRONTEND
    assert "Desktop subscription" in FRONTEND
    assert "Computer orchestrator" in FRONTEND
    assert "perplexity: state.perplexity" in FRONTEND


def test_perplexity_settings_accept_old_names_but_store_current_names():
    assert app._normalize_perplexity_settings(
        {"model": "pro-search", "tools": "full"}
    ) == {"model": "perplexity-computer", "tools": "desktop"}


def test_community_perplexity_onboarding_is_one_click_and_least_privilege():
    assert "/api/community/agents/perplexity/setup" in FRONTEND
    assert "Connect Perplexity" in FRONTEND
    assert "Verify connection" in FRONTEND
    assert "Leave python3 off" in FRONTEND
    assert "No Perplexity API key is needed" in FRONTEND


def test_community_release_carries_a_scoped_universal_helper():
    from scripts import perplexity_agent_bridge as bridge

    assert bridge.BUNDLED_DRIVER.is_file()
    assert bridge.BUNDLED_DRIVER.name == "perplexity_desktop_driver.bin"
    info = bridge.BUNDLED_DRIVER.parents[1] / "Info.plist"
    assert info.is_file()
    assert "chat.agents.perplexity-driver" in info.read_text(encoding="utf-8")

    completed = subprocess.run(
        [str(bridge.BUNDLED_DRIVER), "--workspace-folder-capability"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    capability = json.loads(completed.stdout)
    assert capability == {
        "status": "ok",
        "workspace_folder_selection": True,
    }


def test_perplexity_receives_selected_folder_on_initial_and_context_retry(tmp_path):
    payloads = []

    class Response:
        text = ""

        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {"response": "ok"}

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, _url, **kwargs):
            payloads.append(dict(kwargs.get("json") or {}))
            return Response(404 if len(payloads) == 1 else 200)

    agent = dict(app.AGENTS_BY_ID["perplexity"])
    agent.update(token="test-token", token_env="")
    contexts = {"perplexity": "stale-context"}
    app.AGENT_LOCKS.clear()
    app.PROVIDER_LOCKS.clear()
    with mock.patch.object(app.httpx, "AsyncClient", Client), \
            mock.patch.object(app, "_chat_contexts", return_value=contexts), \
            mock.patch.object(app, "_save_contexts"), \
            mock.patch.object(app, "_message_with_full_preamble", return_value="full prompt"), \
            mock.patch.object(app, "_perplexity_settings_for_chat", return_value={}), \
            mock.patch.object(
                app, "_chat_workspace_info",
                return_value={"source": "chat", "path": str(tmp_path)},
            ), \
            mock.patch.object(
                app, "_cap_short_circuit", new=mock.AsyncMock(return_value=False),
            ), \
            mock.patch.object(
                app, "_usage_budget_short_circuit", new=mock.AsyncMock(return_value=False),
            ):
        result = asyncio.run(app.call_agent(agent, "review the folder", 824))

    assert result == ("ok", {})
    assert len(payloads) == 2
    assert payloads[0]["workspace"] == str(tmp_path)
    assert payloads[1]["workspace"] == str(tmp_path)


def test_perplexity_never_receives_an_implicit_scratch_folder(tmp_path):
    with mock.patch.object(
        app, "_chat_workspace_info",
        return_value={"source": "scratch", "path": str(tmp_path)},
    ):
        assert app._agent_request_workspace("perplexity", 825) == ""
        with pytest.raises(RuntimeError, match="explicitly attached"):
            app._agent_request_workspace(
                "perplexity", 825, explicit_workspace=str(tmp_path),
            )

    with mock.patch.object(
        app, "_chat_workspace_info",
        return_value={"source": "project", "path": str(tmp_path)},
    ):
        assert app._agent_request_workspace("perplexity", 825) == str(tmp_path)
        with pytest.raises(RuntimeError, match="does not match"):
            app._agent_request_workspace(
                "perplexity", 825, explicit_workspace=str(tmp_path / "different"),
            )


def test_perplexity_workspace_mismatch_is_refused_before_any_http_call(tmp_path):
    selected = tmp_path / "selected"
    different = tmp_path / "different"
    selected.mkdir()
    different.mkdir()
    agent = dict(app.AGENTS_BY_ID["perplexity"])
    agent.update(token="test-token", token_env="")
    app.AGENT_LOCKS.clear()
    app.PROVIDER_LOCKS.clear()

    with mock.patch.object(
        app, "_chat_workspace_info",
        return_value={"source": "chat", "path": str(selected)},
    ), mock.patch.object(
        app, "_cap_short_circuit", new=mock.AsyncMock(return_value=False),
    ), mock.patch.object(
        app, "_usage_budget_short_circuit", new=mock.AsyncMock(return_value=False),
    ), mock.patch.object(app.httpx, "AsyncClient") as client:
        with pytest.raises(RuntimeError, match="does not match"):
            asyncio.run(
                app.call_agent(
                    agent, "review", 826, workspace=str(different),
                )
            )
    client.assert_not_called()
