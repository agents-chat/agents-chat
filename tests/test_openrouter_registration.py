import asyncio
import json
import os
from pathlib import Path
from unittest import mock

import app


APP_DIR = Path(__file__).resolve().parents[1]
FRONTEND = (APP_DIR / "static" / "app-runtime.js").read_text(encoding="utf-8")
TEMPLATE = (APP_DIR / "templates" / "index.html").read_text(encoding="utf-8")


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _AsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, *, headers):
        self.calls.append({"url": url, "headers": headers})
        return self.responses.pop(0)


def _catalog_row(
    model_id, *, name=None, prompt="0", completion="0",
    input_modalities=None, output_modalities=None, pricing=None,
):
    return {
        "id": model_id,
        "name": name or model_id,
        "pricing": pricing if pricing is not None else {
            "prompt": prompt, "completion": completion,
        },
        "architecture": {
            "input_modalities": input_modalities or ["text"],
            "output_modalities": output_modalities or ["text"],
        },
    }


def test_openrouter_is_a_managed_first_party_agent():
    agent = app.AGENTS_BY_ID["openrouter"]
    assert agent["url"] == "http://127.0.0.1:55024/api/api_message"
    assert agent["token_env"] == "AGENT_TOKEN_OPENROUTER"
    assert app.MANAGED_BRIDGES["openrouter"]["health_url"].endswith(":55024/health")


def test_default_and_every_packaged_option_are_strictly_free():
    assert app.OPENROUTER_DEFAULT_MODEL == "openrouter/free"
    assert app.OPENROUTER_MODEL_OPTIONS[0] == "openrouter/free"
    assert len(app.OPENROUTER_MODEL_OPTIONS) == 18
    assert all(app._openrouter_model_id_is_free(
        model_id
    ) for model_id in app.OPENROUTER_MODEL_OPTIONS)
    assert "stealth/ox-alpha" not in app.OPENROUTER_MODEL_OPTIONS
    assert "z-ai/glm-5.3-flash" not in app.OPENROUTER_MODEL_OPTIONS
    assert "openrouter/auto" not in app.OPENROUTER_MODEL_OPTIONS
    assert "z-ai/glm-5.2:free" in app.OPENROUTER_MODEL_OPTIONS
    with mock.patch.object(app, "_OPENROUTER_FREE_MODEL_OPTIONS", ()):
        assert app._normalize_openrouter_settings({
            "model": "z-ai/glm-5.2:free", "reasoning": "high",
        }) == {"model": "z-ai/glm-5.2:free", "reasoning": "high"}
        assert app._normalize_openrouter_settings({
            "model": "qwen/qwen3-coder", "reasoning": "high",
        }) == {"model": "openrouter/free", "reasoning": "high"}
        assert app._normalize_openrouter_settings({
            "model": "vendor/made-up:free", "reasoning": "high",
        }) == {"model": "openrouter/free", "reasoning": "high"}
    assert app._normalize_openrouter_settings({
        "model": "not a model slug", "reasoning": "unknown",
    }) == {
        "model": app.OPENROUTER_DEFAULT_MODEL,
        "reasoning": app.OPENROUTER_DEFAULT_REASONING,
    }
    assert app._normalize_openrouter_settings({
        "model": "stealth/ox-alpha", "reasoning": "max",
    }) == {"model": "openrouter/free", "reasoning": "max"}
    assert not any(model.startswith("~") for model in app.OPENROUTER_MODEL_OPTIONS)


def test_live_catalog_keeps_every_free_text_output_and_rejects_nonfree_or_any_cost():
    models, labels = app._openrouter_free_chat_models([
        _catalog_row("openrouter/free", name="OpenRouter: Free Models Router"),
        _catalog_row("vendor/free-text:free", name="Vendor Free Text"),
        _catalog_row("vendor/free-vision:free", input_modalities=["text", "image"]),
        _catalog_row("vendor/zero-price-preview"),
        _catalog_row("vendor/extra-cost:free", pricing={
            "prompt": "0", "completion": "0", "web_search": "0.001",
        }),
        _catalog_row("vendor/paid-chat", prompt="0.000001"),
        _catalog_row("vendor/embedding:free", output_modalities=["embeddings"]),
        _catalog_row("vendor/music:free", output_modalities=["audio"]),
        _catalog_row("vendor/content-safety:free"),
    ])
    assert models[0] == "openrouter/free"
    assert set(models) == {
        "openrouter/free", "vendor/free-text:free", "vendor/free-vision:free",
        "vendor/content-safety:free",
    }
    assert labels["vendor/free-text:free"] == "Vendor Free Text"


def test_dynamic_free_catalog_feeds_orchestrator_and_saved_skill_pins():
    free_models = (
        "openrouter/free", "vendor/new-zero-price-chat:free",
    )
    with mock.patch.object(app, "_OPENROUTER_FREE_MODEL_OPTIONS", free_models), \
         mock.patch.object(app, "_OPENROUTER_FREE_MODEL_LABELS", {}):
        options = app._openrouter_model_options()
        assert options == free_models
        assert "z-ai/glm-5.3-flash" not in options
        assert len(options) == len(set(options))
        assert app._orchestrator_knobs("openrouter")["models"] == list(options)
        pins = app._skill_agent_settings_normalize({
            "openrouter": {"model": "vendor/new-zero-price-chat:free"},
        })
        assert pins == {
            "openrouter": {"model": "vendor/new-zero-price-chat:free"},
        }


def test_public_catalog_refresh_is_credential_free_and_persists_last_good_list(tmp_path):
    client = _AsyncClient([_Response(200, {"data": [
        _catalog_row("openrouter/free"),
        _catalog_row("vendor/new-free-chat:free", name="New Free Chat"),
    ]})])
    cache_file = tmp_path / "openrouter-models.json"
    with mock.patch.object(app.httpx, "AsyncClient", return_value=client), \
         mock.patch.object(app, "OPENROUTER_CATALOG_CACHE_FILE", cache_file), \
         mock.patch.object(app, "_OPENROUTER_FREE_MODEL_OPTIONS", ()), \
         mock.patch.object(app, "_OPENROUTER_FREE_MODEL_LABELS", {}), \
         mock.patch.object(app, "_OPENROUTER_CATALOG_CHECKED_TS", 0.0), \
         mock.patch.object(app, "_OPENROUTER_CATALOG_STATUS", "fallback"), \
         mock.patch.object(app, "_OPENROUTER_CATALOG_DETAIL", ""), \
         mock.patch.object(app, "_OPENROUTER_CATALOG_LOCK", asyncio.Lock()):
        snapshot = asyncio.run(app._refresh_openrouter_free_catalog(force=True))
    assert snapshot["catalog_status"] == "live"
    assert snapshot["free_model_options"] == [
        "openrouter/free", "vendor/new-free-chat:free",
    ]
    assert client.calls == [{"url": app.OPENROUTER_CATALOG_URL, "headers": {}}]
    if os.name != "nt":
        assert cache_file.stat().st_mode & 0o777 == 0o600
    assert "vendor/new-free-chat:free" in cache_file.read_text()


def test_cached_catalog_cannot_reintroduce_a_paid_or_zero_price_preview(tmp_path):
    cache_file = tmp_path / "openrouter-models.json"
    cache_file.write_text(json.dumps({
        "checked_ts": 1,
        "models": [
            {"id": "openrouter/free", "label": "Free Models Router"},
            {"id": "openrouter/auto", "label": "Auto"},
            {"id": "vendor/zero-price-preview", "label": "Preview"},
        ],
    }))
    with mock.patch.object(app, "OPENROUTER_CATALOG_CACHE_FILE", cache_file), \
         mock.patch.object(app, "_OPENROUTER_FREE_MODEL_OPTIONS", ()), \
         mock.patch.object(app, "_OPENROUTER_FREE_MODEL_LABELS", {}), \
         mock.patch.object(app, "_OPENROUTER_CATALOG_CHECKED_TS", 0.0), \
         mock.patch.object(app, "_OPENROUTER_CATALOG_STATUS", "fallback"), \
         mock.patch.object(app, "_OPENROUTER_CATALOG_DETAIL", ""):
        app._load_openrouter_catalog_cache()
        assert app._openrouter_model_options() == ("openrouter/free",)


def test_legacy_ox_skill_pin_stays_free_instead_of_inheriting_room_model():
    pins = app._skill_agent_settings_normalize({
        "openrouter": {"model": "stealth/ox-alpha", "reasoning": "max"},
    })
    assert pins == {
        "openrouter": {"model": "openrouter/free", "reasoning": "max"},
    }


def test_openrouter_is_conversation_only_and_has_model_controls():
    capabilities = app.AGENT_CAPABILITIES["openrouter"]
    assert capabilities["files"] == "none"
    assert capabilities["exec"] is False
    assert capabilities["supports"]["local_files"] is False
    assert capabilities["supports"]["model_picker"] is True
    knobs = app._orchestrator_knobs("openrouter")
    assert knobs["strength_key"] == "reasoning"
    assert "openrouter/free" in knobs["models"]


def test_frontend_has_write_only_setup_and_free_only_model_picker():
    assert "/api/community/agents/openrouter/setup" in FRONTEND
    assert "data-openrouter-api-key" in FRONTEND
    assert 'type="password"' in FRONTEND
    assert 'data-openrouter-setting="model"' in FRONTEND
    assert '<select data-openrouter-setting="model"' in FRONTEND
    assert 'data-openrouter-custom-model' not in FRONTEND
    assert 'data-openrouter-apply-custom' not in FRONTEND
    assert 'datalist id="openrouter-model-options"' not in FRONTEND
    assert "openRouterModelCatalog" in FRONTEND
    assert "Every model in this dropdown is free" in FRONTEND
    assert "PAID / VARIABLE" not in FRONTEND
    assert "custom routes are blocked" in FRONTEND
    assert "OPENROUTER_RETIRED_MODEL_ALIASES" in FRONTEND
    assert "(?=.{1,160}$)~?[A-Za-z0-9]" in FRONTEND
    assert "openrouter: state.openrouter" in FRONTEND


def test_private_settings_has_first_class_openrouter_setup():
    assert 'id="settingsOpenRouter"' in TEMPLATE
    assert "/api/settings/openrouter" in FRONTEND
    assert "renderOpenRouterProvider" in FRONTEND
    assert 'id="openrouterProviderKey" type="password"' in FRONTEND
    assert "Every selectable model is free" in FRONTEND


def test_settings_status_never_returns_the_openrouter_key():
    with mock.patch.dict(app.os.environ, {
        "OPENROUTER_API_KEY": "provider-credential-placeholder-private",
    }):
        with mock.patch.object(
            app, "_probe_agent_capability",
            new=mock.AsyncMock(return_value={"online": True, "degraded_reason": ""}),
        ), mock.patch.object(
            app, "_refresh_openrouter_free_catalog",
            new=mock.AsyncMock(return_value=app._openrouter_catalog_snapshot()),
        ):
            payload = asyncio.run(app._openrouter_settings_payload())
    assert payload["configured"] is True
    assert payload["ready"] is True
    assert payload["default_model"] == "openrouter/free"
    assert "openrouter/free" in payload["free_model_options"]
    assert payload["model_options"] == payload["free_model_options"]
    assert "api_key" not in payload
    assert "provider-credential-placeholder-private" not in json.dumps(payload)


def test_provider_detection_requires_only_the_private_upstream_key():
    with mock.patch.dict(app.os.environ, {"OPENROUTER_API_KEY": ""}):
        missing = app._community_provider_detection("openrouter")
    assert missing["found"] is True
    assert missing["connectable"] is False
    assert missing["setup_kind"] == "openrouter_api_key"

    with mock.patch.dict(app.os.environ, {"OPENROUTER_API_KEY": "key-present"}):
        ready = app._community_provider_detection("openrouter")
    assert ready["connectable"] is True
    assert ready["setup_kind"] == "openrouter_ready"


def test_setup_validates_the_key_before_reading_the_public_catalog():
    client = _AsyncClient([_Response(401)])
    with mock.patch.object(app.httpx, "AsyncClient", return_value=client):
        valid, detail = asyncio.run(app._community_validate_openrouter_key(
            "provider-credential-placeholder-rejected"
        ))
    assert valid is False
    assert "rejected" in detail
    assert [call["url"] for call in client.calls] == [
        "https://openrouter.ai/api/v1/key"
    ]


def test_setup_requires_the_safe_default_after_the_key_is_authenticated():
    client = _AsyncClient([
        _Response(200, {"data": {"label": "Agent Chat"}}),
        _Response(200, {"data": [{"id": "openrouter/free"}]}),
    ])
    with mock.patch.object(app.httpx, "AsyncClient", return_value=client):
        valid, detail = asyncio.run(app._community_validate_openrouter_key(
            "provider-credential-placeholder-accepted"
        ))
    assert valid is True
    assert detail == "OpenRouter key verified."
    assert [call["url"] for call in client.calls] == [
        "https://openrouter.ai/api/v1/key",
        "https://openrouter.ai/api/v1/models",
    ]


def test_release_manifest_carries_the_bridge_ui_icon_and_contract_tests():
    includes = set(json.loads(
        (APP_DIR / "community" / "release_manifest.json").read_text(encoding="utf-8")
    )["include"])
    assert {
        "scripts/openrouter_agent_bridge.py",
        "scripts/run_openrouter_bridge.sh",
        "static/openrouter-icon.svg",
        "tests/test_openrouter_agent_bridge.py",
        "tests/test_openrouter_registration.py",
    } <= includes
