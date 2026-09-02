"""Model picks must survive a restart and reach turns the browser didn't start.

The settings cards are a global per-agent preference in the browser, replayed on
every /api/send and cached per chat IN MEMORY. Every other path — automations,
scheduled skills, free-will, Telegram, and anything at all after an app restart
— reads that cache, so an empty cache used to mean the shipped .env default.
That is how a room set to gpt-5.6 answered on gpt-5.5 four minutes after a
restart, with a chip that reported the substitute as if it were the pick.
"""
import unittest
from unittest import mock

import app


class RememberedSettingsTests(unittest.TestCase):
    def setUp(self):
        # Each test owns the remembered store; never touch the real one.
        self.owner = 7
        self._store = mock.patch.object(
            app, "_REMEMBERED_AGENT_SETTINGS", {self.owner: {}},
        )
        self._loaded = mock.patch.object(
            app, "_REMEMBERED_AGENT_SETTINGS_LOADED", {self.owner},
        )
        self._set = mock.patch.object(app, "_set_setting")
        self._owner = mock.patch.object(app, "_chat_owner_id", return_value=self.owner)
        self._store.start(); self._loaded.start(); self.set_setting = self._set.start()
        self._owner.start()
        self.addCleanup(self._store.stop)
        self.addCleanup(self._loaded.stop)
        self.addCleanup(self._set.stop)
        self.addCleanup(self._owner.stop)
        app.CODEX_SETTINGS_BY_CHAT.pop(4242, None)
        app.GROK_SETTINGS_BY_CHAT.pop(4242, None)
        app.OPENROUTER_SETTINGS_BY_CHAT.pop(4242, None)
        self.addCleanup(app.CODEX_SETTINGS_BY_CHAT.pop, 4242, None)
        self.addCleanup(app.GROK_SETTINGS_BY_CHAT.pop, 4242, None)
        self.addCleanup(app.OPENROUTER_SETTINGS_BY_CHAT.pop, 4242, None)

    def test_no_pick_yet_falls_back_to_shipped_default(self):
        s = app._codex_settings_for_chat(4242)
        self.assertEqual(s["model"], app.CODEX_DEFAULT_MODEL)
        self.assertEqual(s["reasoning"], app.CODEX_DEFAULT_REASONING)

    def test_remembered_pick_survives_an_empty_per_chat_cache(self):
        """The restart case: cache gone, pick honoured anyway."""
        pick = {"model": "gpt-5.6-sol", "reasoning": "high"}
        app._remember_agent_settings(self.owner, app.CODEX_AGENT_ID, pick)
        app.CODEX_SETTINGS_BY_CHAT.clear()  # what a restart leaves behind
        s = app._codex_settings_for_chat(4242)
        self.assertEqual(s["model"], "gpt-5.6-sol")
        self.assertEqual(s["reasoning"], "high")

    def test_per_chat_cache_still_wins_over_the_remembered_pick(self):
        app._remember_agent_settings(
            self.owner, app.CODEX_AGENT_ID,
            {"model": "gpt-5.6-sol", "reasoning": "high"},
        )
        app.CODEX_SETTINGS_BY_CHAT[4242] = {"model": "gpt-5.4", "reasoning": "low"}
        self.assertEqual(app._codex_settings_for_chat(4242)["model"], "gpt-5.4")

    def test_retired_model_in_the_store_degrades_to_the_default(self):
        """An upgrade can drop a model someone picked months ago."""
        app._REMEMBERED_AGENT_SETTINGS[self.owner][app.CODEX_AGENT_ID] = {
            "model": "gpt-4-retired", "reasoning": "nonsense",
        }
        s = app._codex_settings_for_chat(4242)
        self.assertEqual(s["model"], app.CODEX_DEFAULT_MODEL)
        self.assertEqual(s["reasoning"], app.CODEX_DEFAULT_REASONING)

    def test_grok_xhigh_clamps_to_high_not_the_default(self):
        """The grok CLI rejects xhigh; a persisted Codex-style pick must not 500."""
        s = app._normalize_grok_settings({"model": "grok-4.6", "reasoning": "xhigh"})
        self.assertEqual(s, {"model": "grok-4.6", "reasoning": "high"})

    def test_repeat_pick_does_not_rewrite_the_row(self):
        pick = {"model": "grok-4.5", "reasoning": "high"}
        app._remember_agent_settings(self.owner, app.GROK_AGENT_ID, pick)
        app._remember_agent_settings(self.owner, app.GROK_AGENT_ID, dict(pick))
        self.assertEqual(self.set_setting.call_count, 1)

    def test_a_failed_write_never_breaks_the_send(self):
        self.set_setting.side_effect = RuntimeError("disk full")
        app._remember_agent_settings(
            self.owner, app.GROK_AGENT_ID,
            {"model": "grok-4.5", "reasoning": "high"},
        )
        # In-memory value still updated, so this process keeps honouring the pick
        self.assertEqual(app._grok_settings_for_chat(4242)["reasoning"], "high")

    def test_missing_send_key_preserves_remembered_and_cached_choice(self):
        app._REMEMBERED_AGENT_SETTINGS[self.owner][app.CODEX_AGENT_ID] = {
            "model": "gpt-5.6-sol", "reasoning": "high",
        }
        app._capture_send_agent_settings({}, 4242, self.owner)
        self.assertNotIn(4242, app.CODEX_SETTINGS_BY_CHAT)
        self.assertEqual(app._codex_settings_for_chat(4242)["model"], "gpt-5.6-sol")

        app.CODEX_SETTINGS_BY_CHAT[4242] = {"model": "gpt-5.4", "reasoning": "low"}
        app._capture_send_agent_settings({}, 4242, self.owner)
        self.assertEqual(app.CODEX_SETTINGS_BY_CHAT[4242]["model"], "gpt-5.4")

    def test_every_settings_agent_has_a_remembered_fallback(self):
        """Each per-chat cache must resolve without its chat present."""
        for fn in (
            app._codex_settings_for_chat, app._claude_settings_for_chat,
            app._minimax_settings_for_chat, app._antigravity_settings_for_chat,
            app._grok_settings_for_chat,
        ):
            s = fn(4242)
            self.assertIn("model", s, fn.__name__)
            self.assertIn("workspace", s, fn.__name__)
        openrouter = app._openrouter_settings_for_chat(4242)
        self.assertIn("model", openrouter)
        self.assertNotIn("workspace", openrouter)


class LoadRememberedTests(unittest.TestCase):
    def setUp(self):
        self._store = mock.patch.object(app, "_REMEMBERED_AGENT_SETTINGS", {})
        self._loaded = mock.patch.object(app, "_REMEMBERED_AGENT_SETTINGS_LOADED", set())
        self._store.start(); self._loaded.start()
        self.addCleanup(self._store.stop)
        self.addCleanup(self._loaded.stop)

    def test_unparseable_store_is_ignored_not_fatal(self):
        with mock.patch.object(app, "_get_setting", return_value="{not json"):
            app._load_remembered_agent_settings(7)
        self.assertEqual(app._REMEMBERED_AGENT_SETTINGS, {7: {}})

    def test_non_dict_entries_are_dropped(self):
        with mock.patch.object(app, "_get_setting", return_value='{"codex": "nope", "grok": {"model": "grok-4.5"}}'):
            app._load_remembered_agent_settings(7)
        self.assertNotIn("codex", app._REMEMBERED_AGENT_SETTINGS[7])
        self.assertIn("grok", app._REMEMBERED_AGENT_SETTINGS[7])

    def test_owners_load_isolated_rows(self):
        values = {
            app._agent_settings_key(7): (
                '{"codex":{"model":"gpt-5.6-sol","reasoning":"high"}}'
            ),
            app._agent_settings_key(8): (
                '{"codex":{"model":"gpt-5.4","reasoning":"low"}}'
            ),
        }
        with mock.patch.object(
            app, "_get_setting", side_effect=lambda key, default="": values.get(key, default),
        ):
            app._load_remembered_agent_settings(7)
            app._load_remembered_agent_settings(8)
        self.assertEqual(
            app._REMEMBERED_AGENT_SETTINGS[7]["codex"]["model"], "gpt-5.6-sol",
        )
        self.assertEqual(
            app._REMEMBERED_AGENT_SETTINGS[8]["codex"]["model"], "gpt-5.4",
        )

    def test_legacy_row_is_only_migrated_for_bootstrap_admin(self):
        legacy = '{"codex":{"model":"gpt-5.6-sol","reasoning":"high"}}'

        def get_setting(key, default=""):
            return legacy if key == app._AGENT_SETTINGS_KEY else default

        with mock.patch.object(app, "_default_admin_id", return_value=7), \
             mock.patch.object(app, "_get_setting", side_effect=get_setting), \
             mock.patch.object(app, "_set_setting") as set_setting:
            app._load_remembered_agent_settings(7)
            app._load_remembered_agent_settings(8)
        self.assertIn("codex", app._REMEMBERED_AGENT_SETTINGS[7])
        self.assertEqual(app._REMEMBERED_AGENT_SETTINGS[8], {})
        set_setting.assert_called_once_with(
            app._agent_settings_key(7),
            '{"codex": {"model": "gpt-5.6-sol", "reasoning": "high"}}',
        )


if __name__ == "__main__":
    unittest.main()
