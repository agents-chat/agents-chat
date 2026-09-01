import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fastapi.testclient import TestClient

from scripts import openrouter_agent_bridge as bridge


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _AsyncClient:
    response = _Response()
    request = None

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, headers, json):
        type(self).request = {"url": url, "headers": headers, "json": json}
        return type(self).response


class OpenRouterBridgeTests(unittest.TestCase):
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
                {
                    "AGENT_TOKEN_OPENROUTER": "bridge-secret",
                    "OPENROUTER_API_KEY": "upstream-secret-value",
                },
                clear=False,
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge.PROGRESS.clear()
        bridge.CONTEXT_LOCKS.clear()
        bridge.LAST_UPSTREAM.update(status="never-called", reason="", ts=0)
        self.client = TestClient(bridge.app)
        self.headers = {"X-API-KEY": "bridge-secret"}

    def test_health_requires_both_separate_credentials(self):
        health = self.client.get("/health").json()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["token_present"])
        self.assertTrue(health["upstream_key_present"])
        self.assertEqual(health["model"], "openrouter/free")
        self.assertNotIn("upstream-secret-value", str(health))

        with mock.patch.dict(bridge.os.environ, {"OPENROUTER_API_KEY": ""}):
            missing = self.client.get("/health").json()
        self.assertEqual(missing["status"], "missing-token")
        self.assertIn("OpenRouter API key", missing["reason"])

    def test_auth_failure_is_closed_before_any_provider_call(self):
        response = self.client.post("/api/api_message", json={"message": "do not run"})
        self.assertEqual(response.status_code, 401)

    def test_only_free_model_slugs_are_allowed_and_everything_else_falls_back(self):
        model, reasoning, metadata = bridge._options(
            {"model": "z-ai/glm-5.2:free", "reasoning": "high"}
        )
        self.assertEqual((model, reasoning), ("z-ai/glm-5.2:free", "high"))
        self.assertFalse(metadata.get("model_fallback"))

        for rejected in (
            "qwen/qwen3-coder", "z-ai/glm-5.3-flash", "openrouter/auto",
            "~openai/gpt-latest", "bad slug with spaces",
        ):
            with self.subTest(model=rejected):
                model, reasoning, metadata = bridge._options(
                    {"model": rejected, "reasoning": "impossible"}
                )
                self.assertEqual((model, reasoning), ("openrouter/free", "default"))
                self.assertTrue(metadata.get("model_fallback"))

        model, reasoning, metadata = bridge._options({
            "model": "stealth/ox-alpha", "reasoning": "max",
        })
        self.assertEqual((model, reasoning), ("openrouter/free", "max"))
        self.assertEqual(metadata["model_requested"], "stealth/ox-alpha")
        self.assertTrue(metadata["model_fallback"])

    def test_every_advertised_option_is_free_and_dispatchable(self):
        for advertised in bridge.MODEL_OPTIONS:
            with self.subTest(model=advertised):
                model, reasoning, metadata = bridge._options({"model": advertised})
                self.assertEqual(model, advertised)
                self.assertEqual(reasoning, "default")
                self.assertFalse(metadata.get("model_fallback"))
                self.assertTrue(
                    advertised == "openrouter/free" or advertised.endswith(":free")
                )

    def test_paid_glm_flash_is_removed_and_free_router_is_the_fallback(self):
        self.assertNotIn("z-ai/glm-5.3-flash", bridge.MODEL_OPTIONS)
        self.assertNotIn("openrouter/auto", bridge.MODEL_OPTIONS)
        self.assertEqual(len(bridge.MODEL_OPTIONS), 18)
        self.assertEqual(bridge.DEFAULT_MODEL, "openrouter/free")
        self.assertEqual(
            bridge.RETIRED_MODEL_ALIASES["stealth/ox-alpha"],
            "openrouter/free",
        )

    def test_message_uses_safe_free_router_and_never_forwards_the_bridge_token(self):
        _AsyncClient.response = _Response(payload={
            "id": "gen-1",
            "model": "minimax/minimax-m3:free",
            "choices": [{"message": {"role": "assistant", "content": "Done."}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
                "cost": 0,
            },
        })
        _AsyncClient.request = None
        with mock.patch.object(bridge.httpx, "AsyncClient", _AsyncClient):
            response = self.client.post(
                "/api/api_message",
                headers=self.headers,
                json={"message": "Analyze this.", "reasoning": "high"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["response"], "Done.")
        self.assertEqual(body["model"], "minimax/minimax-m3:free")
        self.assertEqual(body["usage"]["total_tokens"], 15)
        self.assertEqual(body["cost_usd"], 0)
        sent = _AsyncClient.request
        self.assertEqual(sent["url"], bridge.OPENROUTER_URL)
        self.assertEqual(sent["headers"]["Authorization"], "Bearer upstream-secret-value")
        self.assertNotIn("bridge-secret", str(sent))
        self.assertEqual(sent["json"]["model"], "openrouter/free")
        self.assertNotIn("session_id", sent["json"])
        self.assertEqual(
            sent["json"]["reasoning"], {"effort": "high", "exclude": True}
        )

    def test_retired_ox_selection_falls_back_visibly_without_paid_routing(self):
        _AsyncClient.response = _Response(payload={
            "model": "minimax/minimax-m3:free",
            "choices": [{"message": {"role": "assistant", "content": "Done."}}],
        })
        _AsyncClient.request = None
        with mock.patch.object(bridge.httpx, "AsyncClient", _AsyncClient):
            response = self.client.post(
                "/api/api_message",
                headers=self.headers,
                json={"message": "Analyze this.", "model": "stealth/ox-alpha"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(_AsyncClient.request["json"]["model"], "openrouter/free")
        self.assertEqual(body["model"], "minimax/minimax-m3:free")
        self.assertEqual(body["model_requested"], "stealth/ox-alpha")
        self.assertTrue(body["model_fallback"])

    def test_provider_credit_and_rate_statuses_are_preserved(self):
        for upstream_status in (402, 429):
            with self.subTest(status=upstream_status):
                _AsyncClient.response = _Response(
                    status_code=upstream_status,
                    payload={"error": {"message": "provider condition"}},
                )
                with mock.patch.object(bridge.httpx, "AsyncClient", _AsyncClient):
                    response = self.client.post(
                        "/api/api_message",
                        headers=self.headers,
                        json={"message": "Try once."},
                    )
                self.assertEqual(response.status_code, upstream_status)

    def test_capabilities_are_api_only(self):
        support = self.client.get("/capabilities").json()["supports"]
        self.assertTrue(support["model_picker"])
        self.assertFalse(support["local_files"])
        self.assertFalse(support["native_apps"])


if __name__ == "__main__":
    unittest.main()
