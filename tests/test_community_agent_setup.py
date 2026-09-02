import asyncio
import inspect
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import app as app_module


class CommunityAgentSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app_module._invalidate_community_discovery()
        self.addCleanup(app_module._invalidate_community_discovery)

    def test_connector_accepts_only_local_contract_urls(self):
        self.assertEqual(
            app_module._community_local_url(
                "http://127.0.0.1:50001", protocol="agent_chat"
            ),
            "http://127.0.0.1:50001/api/api_message",
        )
        self.assertEqual(
            app_module._community_local_url(
                "http://localhost:8642", protocol="openai_chat"
            ),
            "http://localhost:8642/v1/chat/completions",
        )
        with self.assertRaisesRegex(ValueError, "localhost"):
            app_module._community_local_url(
                "https://example.com/v1/chat/completions", protocol="openai_chat"
            )

    def test_connector_secrets_are_never_public(self):
        public = app_module._public_agent({
            "id": "hermes-local", "name": "Hermes", "url": "http://127.0.0.1:55200/api/api_message",
            "token": "inbound-secret", "managed_adapter": "openai_chat",
            "upstream_url": "http://127.0.0.1:8642/v1/chat/completions",
            "upstream_token": "upstream-secret", "upstream_model": "hermes-agent",
            "owner_user_id": 1, "source": "community_connector",
        })
        self.assertEqual(public["id"], "hermes-local")
        for key in ("token", "managed_adapter", "upstream_url", "upstream_token",
                    "upstream_model", "owner_user_id", "source"):
            self.assertNotIn(key, public)

    def test_legacy_codex_desktop_placeholder_is_preserved_but_not_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "custom_agents.json"
            store.write_text(
                '{"agents":[{"id":"codex-desktop","name":"Codex",'
                '"url":"http://127.0.0.1:52569/api/api_message",'
                '"source":"community_connector"}]}',
                encoding="utf-8",
            )
            with patch.object(app_module, "CUSTOM_AGENTS_FILE", store), \
                 patch.object(app_module, "LEGACY_CUSTOM_AGENTS_FILE", Path(td) / "missing.json"), \
                 patch.object(app_module, "community_mode", return_value=True):
                loaded = app_module._load_custom_agents()
            stored = store.read_text(encoding="utf-8")
        self.assertEqual(loaded, [])
        self.assertIn("codex-desktop", stored)

    def test_connector_must_pass_readiness_before_it_is_saved(self):
        agent = {
            "id": "codex", "name": "Codex",
            "url": "http://127.0.0.1:55009/api/api_message", "token": "secret",
        }
        blocked = MagicMock(status_code=503, text="setup required")
        blocked.json.return_value = {"error": "Windows blocked the Codex command from starting."}
        with patch.object(app_module.httpx, "post", return_value=blocked):
            with self.assertRaisesRegex(ValueError, "Windows blocked"):
                app_module._community_verify_agent_contract(agent)

    def test_connector_accepts_the_required_empty_context_probe(self):
        agent = {
            "id": "local", "name": "Local",
            "url": "http://127.0.0.1:55111/api/api_message", "token": "secret",
        }
        ready = MagicMock(status_code=400, text="context_id is required")
        with patch.object(app_module.httpx, "post", return_value=ready):
            app_module._community_verify_agent_contract(agent)

    def test_pairing_prompt_is_one_time_registration_not_file_editing(self):
        prompt = app_module._community_pairing_prompt(
            "http://127.0.0.1:8096", "one-time-code", "Hermes Agent"
        )
        self.assertIn("/api/community/agents/self-connect", prompt)
        self.assertIn("one-time-code", prompt)
        self.assertIn("no app-file editing and no restart", prompt)
        self.assertNotIn("custom_agents.json", prompt)

        self.assertIn("must call Hermes Agent's real model, CLI, or runtime", prompt)
        self.assertIn("Do not return a canned", prompt)
        self.assertIn("progress_stage", prompt)
        self.assertIn("ask the user whether", " ".join(prompt.split()))
        self.assertIn("do not restart Agents Chat", prompt)
        self.assertIn("You are the setup assistant", prompt)
        self.assertIn("Codex or Claude Code", prompt)
        self.assertIn("Do not give these instructions to Hermes Agent", prompt)
        self.assertIn("Target runtime to configure and connect: Hermes Agent", prompt)
        self.assertIn("Destination: the user's local Agents Chat", prompt)
        self.assertIn("do not register", prompt.lower())

    def test_manual_connector_requires_a_real_answer_before_registration(self):
        source = inspect.getsource(app_module.api_community_connect_agent)
        self.assertIn("verify_turn=True", source)

    def test_codex_setup_prompt_installs_real_cli_not_placeholder_bridge(self):
        prompt = app_module._community_codex_cli_setup_prompt(
            "http://127.0.0.1:8096", "codex-pairing-code"
        )
        self.assertIn("npm install --global @openai/codex", prompt)
        self.assertIn("codex login status", prompt)
        self.assertIn("NOT", prompt)
        self.assertIn("WindowsApps", prompt)
        self.assertIn("Do not edit Agents Chat", prompt)
        self.assertIn("do not", prompt.lower())
        self.assertIn("localhost bridge", prompt)
        self.assertIn("progress_stage", prompt)
        self.assertIn("codex-pairing-code", prompt)
        self.assertIn("watching", prompt)

    def test_desktop_agent_can_report_truthful_pairing_progress(self):
        code = "progress-test-code"
        app_module._COMMUNITY_PAIRINGS[code] = {
            "owner_user_id": 1,
            "expires": app_module.time.monotonic() + 60,
            "status_id": "progress-status",
            "kind": "self_connect",
            "stage": "prompt_ready",
            "detail": "Prompt ready",
            "used": False,
        }
        try:
            from fastapi.testclient import TestClient
            with patch.object(app_module, "community_mode", return_value=True):
                response = TestClient(app_module.app, base_url="http://127.0.0.1:8086").post(
                    "/api/community/agents/self-connect",
                    json={
                        "pairing_code": code,
                        "progress_stage": "configuring",
                        "detail": "Creating the local adapter.",
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["stage"], "configuring")
            self.assertEqual(app_module._COMMUNITY_PAIRINGS[code]["stage"], "configuring")
            self.assertEqual(
                app_module._COMMUNITY_PAIRINGS[code]["detail"],
                "Creating the local adapter.",
            )
        finally:
            app_module._COMMUNITY_PAIRINGS.pop(code, None)

    async def test_browser_can_watch_only_its_own_pairing_status(self):
        from starlette.requests import Request

        code = "watch-test-code"
        app_module._COMMUNITY_PAIRINGS[code] = {
            "owner_user_id": 7,
            "expires": app_module.time.monotonic() + 60,
            "status_id": "watch-status",
            "kind": "self_connect",
            "stage": "starting",
            "detail": "Starting the local runtime.",
            "used": False,
        }
        request = Request({
            "type": "http", "method": "GET", "path": "/api/community/agents/pairing-status",
            "query_string": b"status_id=watch-status", "headers": [],
            "scheme": "http", "server": ("127.0.0.1", 8086),
            "client": ("127.0.0.1", 50000),
        })
        try:
            with patch.object(app_module, "_current_user", return_value={"id": 7}):
                result = await app_module.api_community_pairing_status(request)
            self.assertEqual(result["stage"], "starting")
            self.assertEqual(result["detail"], "Starting the local runtime.")
            self.assertFalse(result["connected"])
        finally:
            app_module._COMMUNITY_PAIRINGS.pop(code, None)

    def test_live_turn_verification_rejects_placeholder_reply(self):
        agent = {
            "id": "local", "name": "Local",
            "url": "http://127.0.0.1:55111/api/api_message", "token": "secret",
        }
        placeholder = MagicMock(status_code=200)
        placeholder.json.return_value = {"response": "Connected and ready."}
        with patch.object(app_module.secrets, "token_hex", return_value="abc123"), \
             patch.object(app_module.httpx, "post", return_value=placeholder):
            with self.assertRaisesRegex(ValueError, "placeholder response"):
                app_module._community_verify_live_turn(agent)

    def test_live_turn_verification_accepts_dynamic_marker(self):
        agent = {
            "id": "local", "name": "Local",
            "url": "http://127.0.0.1:55111/api/api_message", "token": "secret",
        }
        ready = MagicMock(status_code=200)
        ready.json.return_value = {"response": "AGENTS-CHAT-ABC123"}
        with patch.object(app_module.secrets, "token_hex", return_value="abc123"), \
             patch.object(app_module.httpx, "post", return_value=ready):
            app_module._community_verify_live_turn(agent)

    def test_pairing_callback_uses_code_without_browser_session_or_origin(self):
        from fastapi.testclient import TestClient
        with patch.object(app_module, "community_mode", return_value=True):
            response = TestClient(app_module.app, base_url="http://127.0.0.1:8086").post(
                "/api/community/agents/self-connect",
                json={"pairing_code": "not-a-real-code"},
            )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertIn("Pairing code is invalid or expired", response.text)

    def test_pairing_callback_rejects_nonlocal_host_even_from_loopback_peer(self):
        # Behind Tailscale Funnel every remote request arrives from a loopback
        # socket peer, so locality must pin on the requested Host hostname
        # (mirrors test_community_first_run_rejects_nonlocal_claim).
        from fastapi.testclient import TestClient

        code = "remote-host-test-code"
        app_module._COMMUNITY_PAIRINGS[code] = {
            "owner_user_id": 1,
            "expires": app_module.time.monotonic() + 60,
            "status_id": "remote-host-status",
            "kind": "self_connect",
            "stage": "prompt_ready",
            "detail": "Prompt ready",
            "used": False,
        }
        try:
            with patch.object(app_module, "community_mode", return_value=True):
                remote = TestClient(app_module.app, base_url="https://example.test")
                response = remote.post(
                    "/api/community/agents/self-connect",
                    json={"pairing_code": code, "progress_stage": "configuring"},
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertIn("only allowed from this computer", response.text)
                self.assertEqual(
                    app_module._COMMUNITY_PAIRINGS[code]["stage"], "prompt_ready"
                )

                local = TestClient(app_module.app, base_url="http://127.0.0.1:8086")
                foreign_origin = local.post(
                    "/api/community/agents/self-connect",
                    json={"pairing_code": code, "progress_stage": "configuring"},
                    headers={"Origin": "https://example.test"},
                )
                self.assertEqual(foreign_origin.status_code, 403, foreign_origin.text)
                self.assertEqual(
                    app_module._COMMUNITY_PAIRINGS[code]["stage"], "prompt_ready"
                )
        finally:
            app_module._COMMUNITY_PAIRINGS.pop(code, None)

    def test_codex_detection_prefers_configured_binary(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / ("codex.exe" if os.name == "nt" else "codex")
            binary.touch()
            completed = MagicMock(returncode=0, stdout="ready\n")
            with patch.dict(os.environ, {"CODEX_BIN": str(binary)}), \
                 patch.object(app_module.subprocess, "run", return_value=completed) as run:
                detected = app_module._community_provider_detection("codex")
        self.assertTrue(detected["found"])
        self.assertTrue(detected["connectable"])
        self.assertEqual(detected["path_env"], "CODEX_BIN")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["login", "status"])

    def test_codex_cli_installed_but_signed_out_needs_login(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / ("codex.exe" if os.name == "nt" else "codex")
            binary.touch()
            with patch.dict(os.environ, {"CODEX_BIN": str(binary)}), \
                 patch.object(app_module, "_first_existing_path", return_value=binary), \
                 patch.object(app_module, "_first_usable_cli_path", return_value=None), \
                 patch.object(app_module, "_windows_store_app_install_location", return_value=None):
                detected = app_module._community_provider_detection("codex")
        self.assertTrue(detected["found"])
        self.assertFalse(detected["connectable"])
        self.assertEqual(Path(detected["path"]), binary)
        self.assertIn("sign-in", detected["next"])

    def test_windows_store_codex_is_found_without_claiming_cli_is_connectable(self):
        store_path = Path(r"C:\Program Files\WindowsApps\OpenAI.Codex_test")
        with patch.object(app_module, "_first_existing_path", return_value=None), \
             patch.object(app_module, "_first_usable_cli_path", return_value=None), \
             patch.object(app_module, "_windows_store_app_install_location", return_value=store_path):
            detected = app_module._community_provider_detection("codex")
        self.assertTrue(detected["found"])
        self.assertFalse(detected["connectable"])
        self.assertEqual(detected["setup_kind"], "codex_cli")
        self.assertIn("official command-line runtime", detected["next"])
        self.assertIsNone(detected["path"])

    def test_windows_cli_probe_rejects_protected_app_execution_alias(self):
        with tempfile.TemporaryDirectory() as td:
            alias = Path(td) / "codex.exe"
            alias.touch()
            with patch.object(app_module.sys, "platform", "win32"), \
                 patch.object(app_module.subprocess, "run", side_effect=PermissionError("Access is denied")):
                found = app_module._first_usable_cli_path([alias])
        self.assertIsNone(found)

    def test_windows_cli_probe_accepts_command_that_answers_version(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "codex.exe"
            binary.touch()
            completed = MagicMock(returncode=0, stdout="codex-cli 1.2.3\n")
            with patch.object(app_module.sys, "platform", "win32"), \
                 patch.object(app_module.subprocess, "run", return_value=completed) as run:
                found = app_module._first_usable_cli_path([binary])
        self.assertEqual(found, binary.resolve())
        self.assertEqual(run.call_args.args[0], [str(binary), "--version"])

    def test_windows_store_lookup_uses_appx_without_a_shell(self):
        completed = MagicMock(
            returncode=0,
            stdout="C:\\Program Files\\WindowsApps\\OpenAI.Codex_test\r\n",
        )
        with patch.object(app_module.sys, "platform", "win32"), \
             patch.object(app_module.subprocess, "run", return_value=completed) as run:
            location = app_module._windows_store_app_install_location("OpenAI.Codex")
        self.assertEqual(str(location), r"C:\Program Files\WindowsApps\OpenAI.Codex_test")
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "powershell.exe")
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["timeout"], 4)

    async def test_windows_store_codex_discovery_reports_setup_needed_not_missing(self):
        public = [{"id": "codex", "name": "Codex"}]
        with patch.object(app_module, "_community_discovery_agents_for_user", return_value=public), \
             patch.object(app_module, "_agent_health_checks", AsyncMock(return_value=[])), \
             patch.object(app_module, "_community_provider_detection", return_value={
                 "found": True, "connectable": False, "path": None,
                 "path_env": "CODEX_BIN", "setup_kind": "codex_cli",
                 "next": "Codex desktop is installed. Install its normal CLI.",
             }):
            result = await app_module._community_agent_discovery({"id": 1})
        self.assertEqual(result["agents"][0]["state"], "setup_needed")
        self.assertEqual(result["agents"][0]["setup_kind"], "codex_cli")
        self.assertEqual(result["found_count"], 1)
        self.assertEqual(result["connectable_count"], 0)

    def test_perplexity_setup_starts_bridge_then_requests_scoped_accessibility(self):
        agent = {
            "id": "perplexity", "name": "Perplexity",
            "url": "http://127.0.0.1:55023/api/api_message",
        }
        with patch.object(app_module.sys, "platform", "darwin"), \
             patch.object(app_module, "_community_perplexity_driver_path", return_value=Path("/helper")), \
             patch.object(app_module, "_community_launch_bridge", return_value=(True, "Starting")) as launch, \
             patch.object(app_module, "_community_request_perplexity_accessibility", return_value=(True, "Open Accessibility")) as permission:
            ok, detail = app_module._community_start_perplexity(
                agent, request_accessibility=True
            )
        self.assertTrue(ok)
        self.assertEqual(detail, "Open Accessibility")
        launch.assert_called_once_with(agent)
        permission.assert_called_once_with()

    async def test_perplexity_accessibility_error_becomes_guided_setup_state(self):
        public = [{
            "id": "perplexity", "name": "Perplexity",
            "token_env": "AGENT_TOKEN_PERPLEXITY",
        }]
        with patch.dict(os.environ, {"AGENT_TOKEN_PERPLEXITY": "private-token"}), \
             patch.object(app_module, "_community_discovery_agents_for_user", return_value=public), \
             patch.object(app_module, "_agent_health_checks", AsyncMock(return_value=[{
                 "id": "perplexity", "ok": False,
                 "error": "Grant Accessibility access to Agent Chat Perplexity Driver.",
             }])), \
             patch.object(app_module, "_community_provider_detection", return_value={
                 "found": True, "connectable": True, "path": Path("/Applications/Perplexity.app"),
                 "path_env": None, "setup_kind": "perplexity_desktop",
                 "next": "Ready",
             }), \
             patch.object(app_module, "_community_docker_discovery_rows", return_value=[]), \
             patch.object(app_module, "_community_sync_connected_grants"):
            result = await app_module._community_agent_discovery_uncached({"id": 1})
        row = result["agents"][0]
        self.assertEqual(row["state"], "setup_needed")
        self.assertEqual(row["setup_kind"], "perplexity_accessibility")
        self.assertIn("Leave python3 off", row["detail"])

    async def test_discovery_reuses_cache_and_shares_an_inflight_scan(self):
        calls = 0

        async def scan(_user):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"agents": [{"id": "codex"}], "scan": calls}

        with patch.object(
            app_module, "_community_agent_discovery_uncached", side_effect=scan
        ):
            first, second = await asyncio.gather(
                app_module._community_agent_discovery({"id": 17}),
                app_module._community_agent_discovery({"id": 17}),
            )
            cached = await app_module._community_agent_discovery({"id": 17})

        self.assertEqual(calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(second, cached)
        self.assertIsNot(first, second)

    async def test_discovery_retries_after_failure_and_after_invalidation(self):
        calls = 0

        async def flaky(_user):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary scan failure")
            return {"agents": [], "scan": calls}

        with patch.object(
            app_module, "_community_agent_discovery_uncached", side_effect=flaky
        ):
            with self.assertRaisesRegex(RuntimeError, "temporary"):
                await app_module._community_agent_discovery({"id": 23})
            recovered = await app_module._community_agent_discovery({"id": 23})
            cached = await app_module._community_agent_discovery({"id": 23})
            app_module._invalidate_community_discovery(23)
            refreshed = await app_module._community_agent_discovery({"id": 23})

        self.assertEqual(recovered["scan"], 2)
        self.assertEqual(cached["scan"], 2)
        self.assertEqual(refreshed["scan"], 3)
        self.assertEqual(calls, 3)

    async def test_connect_detected_serializes_passes_and_reports_action(self):
        user = {"id": 17, "role": "admin"}
        request = MagicMock()
        request.json = AsyncMock(return_value={"agents": ["codex"]})
        runtime = {"connected": False, "active": 0, "max_active": 0, "launches": 0}
        counter_lock = threading.Lock()

        async def discovery(_user, *, force=False):
            state = "connected" if runtime["connected"] else "found"
            return {
                "community": True,
                "agents": [{"id": "codex", "name": "Codex", "state": state}],
                "ready_count": int(runtime["connected"]),
            }

        def launch(_agent):
            with counter_lock:
                runtime["active"] += 1
                runtime["max_active"] = max(
                    runtime["max_active"], runtime["active"],
                )
                runtime["launches"] += 1
            time.sleep(0.05)
            runtime["connected"] = True
            with counter_lock:
                runtime["active"] -= 1
            return True, "Starting on this computer"

        token_env = str(app_module.AGENTS_BY_ID["codex"].get("token_env") or "")
        with patch.object(app_module, "_COMMUNITY_CONNECT_DETECTED_LOCK", asyncio.Lock()), \
             patch.object(app_module, "_require_origin", return_value=None), \
             patch.object(app_module, "_require_admin", return_value=None), \
             patch.object(app_module, "connector_setup_enabled", return_value=True), \
             patch.object(app_module, "_current_user", return_value=user), \
             patch.object(
                 app_module,
                 "_community_discovery_agents_for_user",
                 return_value=[{"id": "codex", "name": "Codex"}],
             ), \
             patch.object(
                 app_module,
                 "_community_provider_detection",
                 return_value={"found": True, "connectable": True},
             ), \
             patch.object(app_module, "_community_launch_bridge", side_effect=launch), \
             patch.object(app_module, "_community_agent_discovery", side_effect=discovery), \
             patch.object(app_module.asyncio, "sleep", AsyncMock()), \
             patch.dict(os.environ, {token_env: "test-token"} if token_env else {}):
            first, second = await asyncio.gather(
                app_module.api_community_connect_detected(request),
                app_module.api_community_connect_detected(request),
            )

        self.assertEqual(runtime["max_active"], 1)
        self.assertEqual(runtime["launches"], 1)
        self.assertEqual(first["results"][0]["pre_state"], "found")
        self.assertEqual(first["results"][0]["action"], "newly_started")
        self.assertEqual(second["results"][0]["pre_state"], "connected")
        self.assertEqual(second["results"][0]["action"], "already_connected")

    def test_connect_and_reauth_paths_invalidate_discovery_cache(self):
        mutating_paths = (
            app_module._community_register_agent,
            app_module.api_community_connect_detected,
            app_module.api_agent_reauth_start,
            app_module.api_agent_reauth_status,
            app_module.api_agent_reauth_secret,
            app_module.api_agent_reauth_confirm,
        )
        for path in mutating_paths:
            with self.subTest(path=path.__name__):
                self.assertIn(
                    "_invalidate_community_discovery(", inspect.getsource(path)
                )

    async def test_discovery_community_flag_follows_edition_not_connect_setup(self):
        public = [{"id": "codex", "name": "Codex"}]
        health = AsyncMock(return_value=[])
        detected = {
            "found": True, "connectable": True, "path": None,
            "path_env": "CODEX_BIN", "next": "Codex CLI is ready to connect.",
        }
        with patch.object(app_module, "_community_discovery_agents_for_user", return_value=public), \
             patch.object(app_module, "_agent_health_checks", health), \
             patch.object(app_module, "_community_provider_detection", return_value=detected), \
             patch.object(app_module, "_community_docker_discovery_rows", return_value=[]), \
             patch.object(app_module, "_community_sync_connected_grants"), \
             patch.object(app_module, "community_mode", return_value=False):
            private = await app_module._community_agent_discovery({"id": 1})
        with patch.object(app_module, "_community_discovery_agents_for_user", return_value=public), \
             patch.object(app_module, "_agent_health_checks", health), \
             patch.object(app_module, "_community_provider_detection", return_value=detected), \
             patch.object(app_module, "_community_docker_discovery_rows", return_value=[]), \
             patch.object(app_module, "_community_sync_connected_grants"), \
             patch.object(app_module, "community_mode", return_value=True):
            community = await app_module._community_agent_discovery({"id": 1}, force=True)
        self.assertFalse(private["community"])
        self.assertTrue(community["community"])

    async def test_discovery_never_exposes_detected_paths(self):
        public = [{"id": "codex", "name": "Codex"}]
        with patch.object(app_module, "_community_discovery_agents_for_user", return_value=public), \
             patch.object(app_module, "_agent_health_checks", AsyncMock(return_value=[])), \
             patch.object(app_module, "_community_provider_detection", return_value={
                 "found": True, "connectable": True, "path": Path("/private/tool"),
                 "path_env": "CODEX_BIN", "next": "Install Codex",
             }):
            result = await app_module._community_agent_discovery({"id": 1})
        self.assertEqual(result["agents"][0]["state"], "found")
        self.assertNotIn("path", result["agents"][0])
        self.assertNotIn("/private/tool", repr(result))

    async def test_stale_codex_bridge_is_not_connected_when_cli_is_blocked(self):
        public = [{"id": "codex", "name": "Codex"}]
        health = [{"id": "codex", "ok": True}]
        with patch.object(app_module, "_community_discovery_agents_for_user", return_value=public), \
             patch.object(app_module, "_agent_health_checks", AsyncMock(return_value=health)), \
             patch.object(app_module, "_community_provider_detection", return_value={
                 "found": True, "connectable": False, "path": None,
                 "path_env": "CODEX_BIN", "setup_kind": "codex_cli",
                 "next": "Install the normal CLI.",
             }):
            result = await app_module._community_agent_discovery({"id": 1})
        self.assertEqual(result["agents"][0]["state"], "setup_needed")
        self.assertEqual(result["ready_count"], 0)

    def test_oauth_failure_is_not_treated_as_transient(self):
        class _Resp:
            status_code = 500
            text = '{"error":"Failed to authenticate: OAuth session expired and could not be refreshed"}'
            def json(self):
                return {"error": "Failed to authenticate: OAuth session expired and could not be refreshed"}
        err = app_module._agent_http_error(_Resp())
        self.assertFalse(err.transient)
        self.assertTrue(err.auth_expired)
        self.assertTrue(app_module._is_auth_failure_text(str(err)))

    def test_reauth_recipe_covers_built_ins_and_user_added_agents(self):
        claude = app_module._agent_reauth_recipe({"id": "claude", "name": "Claude"})
        self.assertEqual(claude["kind"], "cli_oauth")
        self.assertTrue(claude["code_needed"])
        minimax = app_module._agent_reauth_recipe({"id": "minimax", "name": "MiniMax"})
        self.assertEqual(minimax["kind"], "desktop_app")
        grok = app_module._agent_reauth_recipe({"id": "grok", "name": "Grok"})
        self.assertEqual(grok["kind"], "cli_login")
        custom = app_module._agent_reauth_recipe({
            "id": "my-zero", "name": "Shop Agent",
            "source": "community_connector", "token_env": "AGENT_TOKEN_MY_ZERO",
        })
        self.assertEqual(custom["kind"], "api_key")
        self.assertTrue(custom["secret"])
        public = app_module._reauth_public({
            **claude, "id": "sess", "stage": "waiting_code",
            "authorize_url": "https://claude.com/oauth", "tmpdir": "/tmp/secret",
            "code_file": "/tmp/secret/code.txt", "pid": 9,
        })
        self.assertNotIn("tmpdir", public)
        self.assertNotIn("code_file", public)
        self.assertNotIn("pid", public)
        self.assertEqual(public["authorize_url"], "https://claude.com/oauth")

    def test_codex_reauth_keeps_cli_path_when_signed_out(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / ("codex.exe" if os.name == "nt" else "codex")
            binary.touch()
            with patch.object(app_module, "_community_provider_detection", return_value={
                "found": True, "connectable": False, "path": binary,
                "path_env": "CODEX_BIN",
            }):
                argv = app_module._reauth_cli_argv(
                    {"id": "codex", "name": "Codex"},
                    {"cli_mode": "login"},
                )
        self.assertIsNotNone(argv)
        self.assertIn("login", argv)
        self.assertTrue(any(str(binary) in str(part) for part in argv))

    def test_windows_ps1_is_not_treated_as_a_cli(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "codex.ps1"
            script.write_text("write-host hi", encoding="utf-8")
            with patch.object(app_module.sys, "platform", "win32"):
                found = app_module._first_existing_path([script])
        self.assertIsNone(found)

    async def test_codex_reauth_probe_restarts_the_bridge(self):
        agent = {"id": "codex", "name": "Codex", "url": "http://127.0.0.1:55009/api/api_message"}
        with patch.object(app_module, "_cli_login_status_ok", return_value=True), \
             patch.object(app_module, "_community_restart_bridge", return_value=(True, "Starting")) as restart, \
             patch.object(app_module, "_wait_local_bridge", return_value=True), \
             patch.object(app_module, "_agent_health_checks", AsyncMock(return_value=[
                 {"id": "codex", "ok": True}
             ])):
            ok, detail = await app_module._reauth_probe_ready(agent)
        self.assertTrue(ok)
        restart.assert_called_once()
        self.assertIn("back", detail.lower())

    def test_onboarding_orchestrator_prefers_found_claude_over_found_codex(self):
        pick = app_module._pick_onboarding_orchestrator([
            {"id": "codex", "name": "Codex", "state": "found", "connectable": True},
            {"id": "claude", "name": "Claude", "state": "found", "connectable": True},
        ])
        self.assertEqual(pick["id"], "claude")
        self.assertIn("conductor", pick["why"].lower())

    def test_onboarding_orchestrator_prefers_found_codex_over_found_minimax(self):
        pick = app_module._pick_onboarding_orchestrator([
            {"id": "minimax", "name": "MiniMax", "state": "found", "connectable": True},
            {"id": "codex", "name": "Codex", "state": "found", "connectable": True},
        ])
        self.assertEqual(pick["id"], "codex")

    def test_onboarding_orchestrator_prefers_connected_codex_over_missing_claude(self):
        pick = app_module._pick_onboarding_orchestrator([
            {"id": "claude", "name": "Claude", "state": "not_found", "connectable": False},
            {"id": "codex", "name": "Codex", "state": "connected", "connectable": False},
        ])
        self.assertEqual(pick["id"], "codex")

    def test_onboarding_orchestrator_prefers_connected_codex_over_unconfigured_openrouter(self):
        pick = app_module._pick_onboarding_orchestrator([
            {
                "id": "openrouter", "name": "OpenRouter", "state": "setup_needed",
                "connectable": False, "setup_kind": "openrouter_api_key",
            },
            {"id": "codex", "name": "Codex", "state": "connected", "connectable": False},
        ])
        self.assertEqual(pick["id"], "codex")

    def test_onboarding_orchestrator_does_not_recommend_empty_connector(self):
        pick = app_module._pick_onboarding_orchestrator([{
            "id": "openrouter", "name": "OpenRouter", "state": "setup_needed",
            "connectable": False, "setup_kind": "openrouter_api_key",
        }])
        self.assertIsNone(pick)

    def test_onboarding_orchestrator_keeps_configured_openrouter_as_fallback(self):
        pick = app_module._pick_onboarding_orchestrator([{
            "id": "openrouter", "name": "OpenRouter", "state": "found",
            "connectable": True, "setup_kind": "openrouter_ready",
        }])
        self.assertEqual(pick["id"], "openrouter")

    def test_connected_openrouter_does_not_outrank_found_desktop_codex(self):
        pick = app_module._pick_onboarding_orchestrator([
            {
                "id": "openrouter", "name": "OpenRouter", "state": "connected",
                "connectable": False, "setup_kind": "openrouter_ready",
            },
            {"id": "codex", "name": "Codex", "state": "found", "connectable": True},
        ])
        self.assertEqual(pick["id"], "codex")

    def test_config_writer_replaces_blank_value_and_keeps_token_private(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config.env"
            config.write_text("AGENT_TOKEN_CODEX=\nKEEP_ME=yes\n", encoding="utf-8")
            old_path = app_module._CUSTOM_ENV_FILE
            old_value = os.environ.get("AGENT_TOKEN_CODEX")
            try:
                app_module._CUSTOM_ENV_FILE = str(config)
                app_module._community_write_env({"AGENT_TOKEN_CODEX": "private-test-token"})
                text = config.read_text(encoding="utf-8")
                self.assertIn("AGENT_TOKEN_CODEX=private-test-token", text)
                self.assertIn("KEEP_ME=yes", text)
                self.assertEqual(os.environ["AGENT_TOKEN_CODEX"], "private-test-token")
            finally:
                app_module._CUSTOM_ENV_FILE = old_path
                if old_value is None:
                    os.environ.pop("AGENT_TOKEN_CODEX", None)
                else:
                    os.environ["AGENT_TOKEN_CODEX"] = old_value

    def test_builtin_bridge_moves_when_another_install_owns_default_port(self):
        agent = {
            "id": "codex", "name": "Codex",
            "url": "http://127.0.0.1:55009/api/api_message",
            "token_env": "AGENT_TOKEN_CODEX",
        }
        process = MagicMock()
        process.poll.return_value = None
        response = MagicMock(status_code=401, text="unauthorized")
        public = {"id": "codex", "url": agent["url"]}
        # COMMUNITY_BRIDGE_STATE_FILE is resolved from DATA_DIR at IMPORT time,
        # so patching DATA_DIR alone does not redirect it: _save_community_bridge_state
        # wrote this fake adapter (pid 1 — launchd) straight into the live
        # data/community_bridges.json, which the app reads at boot to decide what
        # to reap. Patch the resolved path too.
        with tempfile.TemporaryDirectory() as td, \
             patch.object(app_module, "DATA_DIR", Path(td)), \
             patch.object(app_module, "COMMUNITY_BRIDGE_STATE_FILE",
                          Path(td) / "community_bridges.json"), \
             patch.object(app_module, "PUBLIC_AGENTS", [public]), \
             patch.object(app_module, "_community_free_adapter_port", return_value=55240), \
             patch.object(app_module, "_community_write_env") as write_env, \
             patch.object(app_module.socket, "create_connection"), \
             patch.object(app_module.httpx, "post", return_value=response), \
             patch.object(app_module.subprocess, "Popen", return_value=process):
            app_module._COMMUNITY_BRIDGE_PROCESSES.pop("codex", None)
            with patch.dict(os.environ, {"AGENT_TOKEN_CODEX": "community-token"}):
                ok, detail = app_module._community_launch_bridge(agent)
            app_module._COMMUNITY_BRIDGE_PROCESSES.pop("codex", None)

        self.assertTrue(ok)
        self.assertIn("Starting", detail)
        self.assertEqual(agent["url"], "http://127.0.0.1:55240/api/api_message")
        self.assertEqual(public["url"], agent["url"])
        write_env.assert_called_once_with({"CODEX_AGENT_URL": agent["url"]})


if __name__ == "__main__":
    unittest.main()
