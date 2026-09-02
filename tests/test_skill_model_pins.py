"""Per-skill model pins: choose the model/reasoning each agent uses for a skill.

Two invariants carry the whole feature:
  1. UNSET MEANS INHERIT. An absent agent — or an absent knob under a present
     agent — follows the room's pick. It must never resolve to the shipped .env
     default, or adding this feature would re-introduce the silent-downgrade bug
     it exists to prevent, for every skill that pins one agent and not another.
  2. PINS ARE RUN-SCOPED. A skill that runs in a chat you also type in must not
     change what your own later messages run on.
"""
import unittest
from unittest import mock

import app


class NormalizeTests(unittest.TestCase):
    """_skill_agent_settings_normalize DROPS what it can't validate — it must not
    substitute a default, which is what the per-agent normalizers do."""

    def test_valid_pins_survive(self):
        out = app._skill_agent_settings_normalize({
            "codex": {"model": "gpt-5.6-sol", "reasoning": "xhigh"},
        })
        self.assertEqual(out, {"codex": {"model": "gpt-5.6-sol", "reasoning": "xhigh"}})

    def test_partial_pin_stays_partial(self):
        """Pinning reasoning alone must NOT drag a model in with it."""
        out = app._skill_agent_settings_normalize({"grok": {"reasoning": "high"}})
        self.assertEqual(out, {"grok": {"reasoning": "high"}})

    def test_retired_model_is_dropped_not_defaulted(self):
        out = app._skill_agent_settings_normalize({
            "codex": {"model": "gpt-4-retired", "reasoning": "high"},
        })
        self.assertEqual(out, {"codex": {"reasoning": "high"}})
        self.assertNotIn("model", out["codex"])

    def test_agent_with_nothing_valid_disappears_entirely(self):
        out = app._skill_agent_settings_normalize({"codex": {"model": "nope"}})
        self.assertEqual(out, {})

    def test_empty_string_means_inherit(self):
        """The UI's 'Room default' option posts '' — it must not be stored."""
        out = app._skill_agent_settings_normalize({"codex": {"model": "", "reasoning": ""}})
        self.assertEqual(out, {})

    def test_unknown_agents_and_junk_are_ignored(self):
        for raw in ({"hermes": {"model": "x"}}, {"codex": "nope"}, [], None, "x"):
            self.assertEqual(app._skill_agent_settings_normalize(raw), {}, raw)

    def test_minimax_thinking_coerces_to_bool(self):
        self.assertEqual(
            app._skill_agent_settings_normalize({"minimax": {"thinking": "false"}}),
            {"minimax": {"thinking": False}},
        )
        self.assertEqual(
            app._skill_agent_settings_normalize({"minimax": {"thinking": True}}),
            {"minimax": {"thinking": True}},
        )

    def test_antigravity_has_no_reasoning_knob(self):
        """Its effort is baked into the model name, so a reasoning pin is junk."""
        out = app._skill_agent_settings_normalize({
            "antigravity": {"model": "Gemini 3.6 Flash (High)", "reasoning": "high"},
        })
        self.assertEqual(out, {"antigravity": {"model": "Gemini 3.6 Flash (High)"}})

class ResolutionTests(unittest.TestCase):
    CID = 5150

    def setUp(self):
        for d in (app.CODEX_SETTINGS_BY_CHAT, app.GROK_SETTINGS_BY_CHAT):
            d.pop(self.CID, None)
        self.addCleanup(app.CODEX_SETTINGS_BY_CHAT.pop, self.CID, None)
        self.addCleanup(app.GROK_SETTINGS_BY_CHAT.pop, self.CID, None)
        ws = mock.patch.object(app, "_chat_workspace", return_value="/tmp/ws")
        ws.start(); self.addCleanup(ws.stop)

    def _with_pins(self, pins):
        tok = app._RUN_MODEL_PINS.set(pins)
        self.addCleanup(app._RUN_MODEL_PINS.reset, tok)

    def test_pin_overrides_the_rooms_model(self):
        app.CODEX_SETTINGS_BY_CHAT[self.CID] = {"model": "gpt-5.5", "reasoning": "low"}
        self._with_pins({"codex": {"model": "gpt-5.6-sol", "reasoning": "xhigh"}})
        s = app._codex_settings_for_chat(self.CID)
        self.assertEqual(s["model"], "gpt-5.6-sol")
        self.assertEqual(s["reasoning"], "xhigh")

    def test_unpinned_knob_keeps_the_rooms_value(self):
        """The heart of invariant 1, at knob granularity."""
        app.CODEX_SETTINGS_BY_CHAT[self.CID] = {"model": "gpt-5.4", "reasoning": "high"}
        self._with_pins({"codex": {"reasoning": "low"}})
        s = app._codex_settings_for_chat(self.CID)
        self.assertEqual(s["model"], "gpt-5.4")   # room's, not the .env default
        self.assertEqual(s["reasoning"], "low")   # pinned

    def test_unpinned_agent_is_untouched(self):
        app.GROK_SETTINGS_BY_CHAT[self.CID] = {"model": "grok-4.5", "reasoning": "high"}
        self._with_pins({"codex": {"model": "gpt-5.6-sol"}})
        self.assertEqual(app._grok_settings_for_chat(self.CID)["reasoning"], "high")

    def test_no_pins_at_all_is_todays_behaviour(self):
        app.CODEX_SETTINGS_BY_CHAT[self.CID] = {"model": "gpt-5.4", "reasoning": "high"}
        s = app._codex_settings_for_chat(self.CID)
        self.assertEqual((s["model"], s["reasoning"]), ("gpt-5.4", "high"))

    def test_pins_do_not_mutate_the_per_chat_cache(self):
        """Invariant 2: the chat's own settings survive the run untouched."""
        app.CODEX_SETTINGS_BY_CHAT[self.CID] = {"model": "gpt-5.5", "reasoning": "low"}
        self._with_pins({"codex": {"model": "gpt-5.6-sol", "reasoning": "xhigh"}})
        app._codex_settings_for_chat(self.CID)
        self.assertEqual(
            app.CODEX_SETTINGS_BY_CHAT[self.CID],
            {"model": "gpt-5.5", "reasoning": "low"},
        )

    def test_pins_are_invisible_outside_the_run(self):
        app.CODEX_SETTINGS_BY_CHAT[self.CID] = {"model": "gpt-5.5", "reasoning": "low"}
        tok = app._RUN_MODEL_PINS.set({"codex": {"model": "gpt-5.6-sol"}})
        app._RUN_MODEL_PINS.reset(tok)   # what apply_skill's finally does
        self.assertEqual(app._codex_settings_for_chat(self.CID)["model"], "gpt-5.5")

    def test_pin_survives_an_empty_chat_cache(self):
        """A scheduled run in a brand-new chat still gets the skill's models."""
        self._with_pins({"codex": {"model": "gpt-5.6-sol"}})
        self.assertEqual(app._codex_settings_for_chat(self.CID)["model"], "gpt-5.6-sol")

    def test_run_memo_covers_dispatch_outside_the_task_tree(self):
        app.ACTIVE_TURN_RUN[self.CID] = 987654
        app._RUN_PINS_BY_RUN[987654] = {"codex": {"model": "gpt-5.6-sol"}}
        self.addCleanup(app.ACTIVE_TURN_RUN.pop, self.CID, None)
        self.addCleanup(app._RUN_PINS_BY_RUN.pop, 987654, None)
        self.assertEqual(app._codex_settings_for_chat(self.CID)["model"], "gpt-5.6-sol")

    def test_finishing_a_run_drops_its_memo(self):
        app._RUN_PINS_BY_RUN[987655] = {"codex": {"model": "gpt-5.6-sol"}}
        with mock.patch.object(app, "_db", side_effect=RuntimeError("no db")):
            app._turn_run_finish(987655, "done")
        self.assertNotIn(987655, app._RUN_PINS_BY_RUN)


class SummaryTests(unittest.TestCase):
    def test_summary_names_each_pinned_agent(self):
        line = app._skill_pin_summary({"agent_settings": {
            "codex": {"model": "gpt-5.6-sol", "reasoning": "xhigh"},
        }})
        self.assertIn("gpt-5.6-sol", line)
        self.assertIn("xhigh", line)

    def test_no_pins_no_line(self):
        self.assertEqual(app._skill_pin_summary({"agent_settings": {}}), "")
        self.assertEqual(app._skill_pin_summary({}), "")


class UnattendedAutoTests(unittest.TestCase):
    def setUp(self):
        self.skill = {
            "name": "Repository scan",
            "description": "Inspect the project and summarize findings",
            "body": "",
            "steps": [],
            "agent_settings": {},
            "owner_user_id": 7,
        }

    def test_auto_preference_routes_skill_and_ignores_legacy_explicit_pin(self):
        self.skill["agent_settings"] = {
            "codex": {"model": "gpt-5.5", "reasoning": "xhigh"},
        }
        with mock.patch.object(app, "_auto_reasoning_preference", return_value=True), \
             mock.patch.object(app, "_chat_owner_id", return_value=7):
            pins, tier, enabled = app._skill_effective_model_pins(
                self.skill, 99, ["codex", "claude"], "pipeline", {},
            )
        self.assertTrue(enabled)
        self.assertEqual(tier, "high")
        self.assertNotEqual(pins["codex"], {"model": "gpt-5.5", "reasoning": "xhigh"})
        self.assertEqual(pins["codex"], {"model": "gpt-5.6-sol", "reasoning": "high"})
        self.assertIn("claude", pins)

    def test_auto_off_preserves_explicit_skill_pin(self):
        self.skill["agent_settings"] = {
            "codex": {"model": "gpt-5.5", "reasoning": "xhigh"},
        }
        with mock.patch.object(app, "_auto_reasoning_preference", return_value=False), \
             mock.patch.object(app, "_chat_owner_id", return_value=7):
            pins, tier, enabled = app._skill_effective_model_pins(
                self.skill, 99, ["codex"], "pipeline", {},
            )
        self.assertFalse(enabled)
        self.assertIsNone(tier)
        self.assertEqual(pins["codex"], {"model": "gpt-5.5", "reasoning": "xhigh"})

    def test_auto_off_leaves_unpinned_agents_unmodified(self):
        with mock.patch.object(app, "_auto_reasoning_preference", return_value=False), \
             mock.patch.object(app, "_chat_owner_id", return_value=7):
            pins, tier, enabled = app._skill_effective_model_pins(
                self.skill, 99, ["codex"], "pipeline", {},
            )
        self.assertFalse(enabled)
        self.assertIsNone(tier)
        self.assertEqual(pins, {})


if __name__ == "__main__":
    unittest.main()
