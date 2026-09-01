import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi.testclient import TestClient

from scripts import perplexity_agent_bridge as bridge


class PerplexityBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(bridge, "STATE_DIR", state),
            mock.patch.object(bridge, "CONTEXT_STORE", state / "contexts.json"),
            mock.patch.object(bridge, "_refresh_env"),
            mock.patch.dict(
                bridge.os.environ,
                {"AGENT_TOKEN_PERPLEXITY": "bridge-secret"},
                clear=False,
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge.PROGRESS.clear()
        bridge.LAST_UPSTREAM.update(status="never-called", reason="", ts=0)
        self.client = TestClient(bridge.app)
        self.headers = {"X-API-KEY": "bridge-secret"}

    def test_health_reports_accessibility_setup_without_claiming_green(self):
        with mock.patch.object(
            bridge,
            "_driver_health",
            return_value={
                "status": "accessibility-required",
                "installed": True,
                "accessibility_trusted": False,
            },
        ):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "setup-required")
        self.assertEqual(response.json()["runtime"], "desktop-subscription")

    def test_health_is_ok_only_when_driver_and_token_are_ready(self):
        with mock.patch.object(
            bridge, "_driver_health", return_value={"status": "ok"}
        ):
            response = self.client.get("/health")
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["token_present"])

    def test_fresh_prompt_replays_context_and_preserves_approval_boundaries(self):
        prompt = bridge._fresh_task_prompt(
            "ctx-1234567890",
            [
                {"role": "user", "text": "Find the source."},
                {"role": "assistant", "text": "Which date range?"},
            ],
            "This month.",
        )
        self.assertIn("AC-ctx1234567", prompt)
        self.assertIn("USER: Find the source.", prompt)
        self.assertIn("ASSISTANT: Which date range?", prompt)
        self.assertIn("USER: This month.", prompt)
        self.assertIn("Respect every on-device approval gate", prompt)
        self.assertIn("Start the final response with `BOTTOM LINE:`", prompt)

    def test_clean_message_keeps_real_ask_and_inline_attachment_not_app_scaffolding(self):
        raw = """[1:1 CHAT MODE]
Generated room preamble.
--- new messages in the room ---
Owner: Review the attached numbers.
  [attachment: totals.csv (text/csv, 12 bytes) http://local/file?k=signed]
  --- extracted text from totals.csv ---
  month,total
  July,42
  --- end extracted text ---

--- tools (pre-authorized: plain HTTP GET, no auth headers — curl or any fetch) ---
Search web: https://generated-tool.example.invalid/query

--- credentials & escape hatches ---
Credentials you hold: EXAMPLE

[FINAL RESPONSE FORMAT — mandatory on this turn]
Generated format block.
"""
        cleaned = bridge._clean_message(raw)
        self.assertIn("Owner: Review the attached numbers.", cleaned)
        self.assertIn("[attachment: totals.csv", cleaned)
        self.assertIn("July,42", cleaned)
        self.assertNotIn("tools (pre-authorized", cleaned)
        self.assertNotIn("credentials & escape hatches", cleaned)
        self.assertNotIn("FINAL RESPONSE FORMAT", cleaned)
        self.assertNotIn("generated-tool.example.invalid", cleaned)

    def test_fresh_prompt_names_the_exact_selected_workspace(self):
        workspace = Path(self.tmp.name) / "Client Files"
        workspace.mkdir()
        prompt = bridge._fresh_task_prompt(
            "ctx-workspace",
            [{"role": "assistant", "text": "I am ready."}],
            "Review the folder.",
            str(workspace),
        )
        self.assertIn("[Agent Chat workspace folder]", prompt)
        self.assertIn(f'"{workspace}"', prompt)
        self.assertIn("Do not claim you read a file", prompt)

    def test_desktop_run_uses_private_prompt_files_and_returns_thread_metadata(self):
        observed = {}

        def driver_json(command, *, timeout):
            if command == ["--workspace-folder-capability"]:
                return {
                    "status": "ok",
                    "workspace_folder_selection": True,
                }
            observed["command"] = list(command)
            observed["followup"] = Path(command[1]).read_text(encoding="utf-8")
            observed["fresh"] = Path(command[3]).read_text(encoding="utf-8")
            return {
                "status": "ok",
                "response": "PPLX-BRIDGE-PROBE-824",
                "thread_title": "PPLX-BRIDGE-PROBE-824",
                "continued": False,
            }

        with mock.patch.object(bridge, "_driver_json", side_effect=driver_json):
            answer, metadata = bridge._run_desktop_sync(
                "Reply exactly.", [], "ctx-1", ""
            )
        self.assertEqual(answer, "PPLX-BRIDGE-PROBE-824")
        self.assertEqual(observed["followup"], "Reply exactly.")
        self.assertIn("Conversation transcript", observed["fresh"])
        self.assertEqual(metadata["runtime"], "desktop-subscription")
        self.assertEqual(metadata["thread_title"], "PPLX-BRIDGE-PROBE-824")
        self.assertFalse(any(Path(p).exists() for p in observed["command"] if p.endswith(".txt")))

    def test_desktop_run_passes_workspace_folder_to_the_driver(self):
        observed = {}
        workspace = Path(self.tmp.name) / "approved"
        workspace.mkdir()

        def driver_json(command, *, timeout):
            if command == ["--workspace-folder-capability"]:
                return {
                    "status": "ok",
                    "workspace_folder_selection": True,
                }
            observed["command"] = list(command)
            observed["followup"] = Path(command[1]).read_text(encoding="utf-8")
            observed["fresh"] = Path(command[3]).read_text(encoding="utf-8")
            return {
                "status": "ok",
                "response": "Folder reviewed.",
                "thread_title": "Folder review",
                "continued": False,
                "workspace_selected": True,
            }

        with mock.patch.object(bridge, "_driver_json", side_effect=driver_json):
            bridge._run_desktop_sync(
                "Review it.", [], "ctx-folder", "", str(workspace)
            )
        index = observed["command"].index("--workspace-folder")
        self.assertEqual(observed["command"][index + 1], str(workspace))
        self.assertIn(str(workspace), observed["followup"])
        self.assertIn(str(workspace), observed["fresh"])

    def test_desktop_error_is_preserved_before_workspace_confirmation_check(self):
        workspace = Path(self.tmp.name) / "approved-error"
        workspace.mkdir()

        def driver_json(command, *, timeout):
            if command == ["--workspace-folder-capability"]:
                return {"status": "ok", "workspace_folder_selection": True}
            return {
                "status": "error",
                "error": "Perplexity's native folder chooser did not open; no prompt was sent.",
            }

        with mock.patch.object(bridge, "_driver_json", side_effect=driver_json):
            with self.assertRaisesRegex(
                bridge.PerplexityDesktopError, "native folder chooser did not open"
            ) as caught:
                bridge._run_desktop_sync(
                    "Review it.", [], "ctx-folder-error", "", str(workspace)
                )
        self.assertFalse(caught.exception.retryable)

    def test_desktop_transport_failure_after_launch_is_not_retryable(self):
        def driver_json(_command, *, timeout):
            raise bridge.PerplexityDesktopError(
                "desktop result was lost", http_status=504, retryable=True
            )

        with mock.patch.object(bridge, "_driver_json", side_effect=driver_json):
            with self.assertRaisesRegex(
                bridge.PerplexityDesktopError, "desktop result was lost"
            ) as caught:
                bridge._run_desktop_sync("Review it.", [], "ctx-lost", "")
        self.assertFalse(caught.exception.retryable)

    def test_workspace_validation_requires_an_existing_absolute_directory(self):
        file_path = Path(self.tmp.name) / "not-a-folder.txt"
        file_path.write_text("x", encoding="utf-8")
        missing = Path(self.tmp.name) / "missing"
        run = mock.AsyncMock()
        with mock.patch.object(bridge, "_run_perplexity", new=run):
            for raw in ("relative/folder", str(missing), str(file_path), 123):
                with self.subTest(workspace=raw):
                    response = self.client.post(
                        "/api/api_message",
                        headers=self.headers,
                        json={"message": "review", "workspace": raw},
                    )
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertFalse(response.json()["retryable"])
        run.assert_not_awaited()

    def test_workspace_attachment_forces_fresh_task_and_replays_history(self):
        context_id = "ctx-folder-attach"
        workspace = Path(self.tmp.name) / "project"
        workspace.mkdir()
        bridge._save_contexts({
            context_id: {
                "messages": [
                    {"role": "user", "text": "Earlier question"},
                    {"role": "assistant", "text": "Earlier answer"},
                ],
                "thread_title": "Old Perplexity task",
            }
        })
        run = mock.AsyncMock(return_value=(
            "Reviewed.",
            {
                "model": "perplexity-computer",
                "runtime": "desktop-subscription",
                "thread_title": "New folder task",
                "continued": False,
                "partial": False,
            },
        ))
        with mock.patch.object(bridge, "_run_perplexity", new=run):
            response = self.client.post(
                "/api/api_message",
                headers=self.headers,
                json={
                    "message": "Review the attached folder.",
                    "context_id": context_id,
                    "workspace": str(workspace),
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        args = run.await_args.args
        self.assertEqual(args[1][0]["text"], "Earlier question")
        self.assertEqual(args[3], "")
        self.assertEqual(args[4], str(workspace.resolve()))
        stored = bridge._load_contexts()[context_id]
        self.assertEqual(stored["workspace"], str(workspace.resolve()))
        self.assertEqual(stored["thread_title"], "New folder task")

    def test_every_workspace_request_and_first_detach_start_fresh(self):
        workspace = Path(self.tmp.name) / "same"
        other = Path(self.tmp.name) / "other"
        workspace.mkdir()
        other.mkdir()

        def exercise(incoming):
            context_id = "ctx-folder-change"
            bridge._save_contexts({
                context_id: {
                    "messages": [{"role": "assistant", "text": "Prior"}],
                    "thread_title": "Existing task",
                    "workspace": str(workspace.resolve()),
                }
            })
            run = mock.AsyncMock(return_value=(
                "Done.",
                {
                    "model": "perplexity-computer",
                    "runtime": "desktop-subscription",
                    "thread_title": "Result task",
                    "continued": False,
                    "partial": False,
                },
            ))
            body = {"message": "Continue.", "context_id": context_id}
            if incoming is not None:
                body["workspace"] = incoming
            with mock.patch.object(bridge, "_run_perplexity", new=run):
                response = self.client.post(
                    "/api/api_message", headers=self.headers, json=body
                )
            self.assertEqual(response.status_code, 200, response.text)
            return run.await_args.args

        same_args = exercise(str(workspace))
        self.assertEqual(same_args[3], "")
        changed_args = exercise(str(other))
        self.assertEqual(changed_args[3], "")
        detached_args = exercise(None)
        self.assertEqual(detached_args[3], "")
        self.assertEqual(detached_args[4], "")

    def test_no_workspace_resumes_after_detach_marker_was_cleared(self):
        context_id = "ctx-after-detach"
        bridge._save_contexts({
            context_id: {
                "messages": [{"role": "assistant", "text": "Prior"}],
                "thread_title": "No-folder task",
            }
        })
        run = mock.AsyncMock(return_value=(
            "Continued.",
            {
                "model": "perplexity-computer",
                "runtime": "desktop-subscription",
                "thread_title": "No-folder task",
                "continued": True,
                "partial": False,
            },
        ))
        with mock.patch.object(bridge, "_run_perplexity", new=run):
            response = self.client.post(
                "/api/api_message",
                headers=self.headers,
                json={"message": "Continue.", "context_id": context_id},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(run.await_args.args[3], "No-folder task")
        self.assertEqual(run.await_args.args[4], "")

    def test_failed_desktop_turn_is_not_persisted_or_automatically_retryable(self):
        context_id = "ctx-outcome-unknown"
        seed = {
            context_id: {
                "messages": [
                    {"role": "user", "text": "Prior question"},
                    {"role": "assistant", "text": "Prior answer"},
                ],
                "thread_title": "Existing task",
                "updated_ts": 123,
            }
        }
        bridge._save_contexts(seed)
        failed = mock.AsyncMock(
            side_effect=RuntimeError("driver result lost after send")
        )
        with mock.patch.object(bridge, "_run_perplexity", new=failed):
            response = self.client.post(
                "/api/api_message",
                headers=self.headers,
                json={"message": "Review once.", "context_id": context_id},
            )
        self.assertEqual(response.status_code, 500, response.text)
        self.assertFalse(response.json()["retryable"])
        self.assertEqual(bridge._load_contexts(), seed)

        succeeded = mock.AsyncMock(return_value=(
            "Reviewed once.",
            {
                "model": "perplexity-computer",
                "runtime": "desktop-subscription",
                "thread_title": "Existing task",
                "continued": True,
                "partial": False,
            },
        ))
        with mock.patch.object(bridge, "_run_perplexity", new=succeeded):
            response = self.client.post(
                "/api/api_message",
                headers=self.headers,
                json={"message": "Review once.", "context_id": context_id},
            )
        self.assertEqual(response.status_code, 200, response.text)
        stored = bridge._load_contexts()[context_id]["messages"]
        self.assertEqual(
            stored,
            seed[context_id]["messages"] + [
                {"role": "user", "text": "Review once."},
                {"role": "assistant", "text": "Reviewed once."},
            ],
        )

    def test_context_save_failure_delivers_the_completed_desktop_answer(self):
        run = mock.AsyncMock(return_value=(
            "Completed answer.",
            {
                "model": "perplexity-computer",
                "runtime": "desktop-subscription",
                "thread_title": "Completed task",
                "continued": False,
                "partial": False,
            },
        ))
        with mock.patch.object(bridge, "_run_perplexity", new=run), \
                mock.patch.object(
                    bridge, "_save_contexts", side_effect=OSError("disk full")
                ):
            response = self.client.post(
                "/api/api_message",
                headers=self.headers,
                json={"message": "Run once."},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["response"], "Completed answer.")
        self.assertFalse(response.json()["context_persisted"])

    def test_message_returns_context_and_desktop_metadata(self):
        run = mock.AsyncMock(
            return_value=(
                "BOTTOM LINE: researched answer",
                {
                    "model": "perplexity-computer",
                    "runtime": "desktop-subscription",
                    "thread_title": "Researched answer",
                    "continued": False,
                    "partial": False,
                },
            )
        )
        with mock.patch.object(bridge, "_run_perplexity", new=run):
            response = self.client.post(
                "/api/api_message",
                headers=self.headers,
                json={"message": "latest news"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["response"], "BOTTOM LINE: researched answer")
        self.assertEqual(body["model"], "perplexity-computer")
        self.assertEqual(body["runtime"], "desktop-subscription")
        self.assertTrue(body["context_id"])
        self.assertEqual(run.await_args.args[4], "")

    def test_driver_source_selects_folder_before_sending(self):
        source = (
            Path(bridge.__file__).with_name("perplexity_desktop_driver.swift")
            .read_text(encoding="utf-8")
        )
        self.assertIn('"--workspace-folder"', source)
        self.assertIn('"More Options"', source)
        self.assertIn('"Folders"', source)
        self.assertIn('"Add another folder"', source)
        self.assertIn('identifier == "PathTextField"', source)
        self.assertIn('identifier == "open-panel"', source)
        self.assertIn('["Open", "Choose", "Select"]', source)
        self.assertIn("openAndSavePanelService", source)
        self.assertIn("ViewBridgeMostRecentlyProxiedKeyboardEventsForUltimateHostApp", source)
        self.assertIn("workspaceFolderChipCount", source)
        self.assertIn("CFEqual", source)
        self.assertIn("node.selected", source)
        self.assertIn("kAXURLAttribute", source)
        run_start = source.index("private func runPrompt")
        run_end = source.index("private func optionValue", run_start)
        run_prompt = source[run_start:run_end]
        self.assertLess(
            run_prompt.index("selectWorkspaceFolder"),
            run_prompt.index("sendPrompt"),
        )

    def test_auth_failure_is_closed(self):
        response = self.client.post(
            "/api/api_message", json={"message": "do not run"}
        )
        self.assertEqual(response.status_code, 401)

    def test_bridge_does_not_extract_private_perplexity_credentials(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PERPLEXITY_API_KEY", source)
        self.assertNotIn("current_user__data", source)
        self.assertNotIn("SecItemCopyMatching", source)
        self.assertNotIn("defaults read ai.perplexity", source)
        self.assertIn("Accessibility", source)


if __name__ == "__main__":
    unittest.main()
