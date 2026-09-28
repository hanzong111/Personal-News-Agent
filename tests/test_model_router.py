"""Tests for the Hermes model-router plugin (hermes/plugins/model-router), using a fake gateway."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parent.parent / "hermes" / "plugins" / "model-router"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(PLUGIN.exists(), "model-router plugin not installed")
class RouterLogicTest(unittest.TestCase):
    def setUp(self):
        self.r = _load("router_under_test", PLUGIN / "router.py")

    def test_commands_and_small_talk_never_call_the_judge(self):
        judge = lambda p: self.fail("judge called")
        self.assertEqual((None, "skip"), self.r.decide("/new", None, judge))
        self.assertEqual((None, "skip"), self.r.decide("   ", None, judge))
        self.assertEqual(("easy", "rule"), self.r.decide("thanks!", "hard", judge))

    def test_judge_label_is_used_and_follow_ups_get_the_hint(self):
        seen = []
        judge = lambda p: seen.append(p) or "Hard."
        self.assertEqual(("hard", "llm"), self.r.decide("and KPJ?", "hard", judge))
        self.assertIn("previous message was hard", seen[0])
        self.assertEqual(("easy", "llm"), self.r.decide("latest news on gamuda", None, lambda p: "easy"))

    def test_judge_failure_falls_back_to_keywords(self):
        def boom(p):
            raise TimeoutError
        self.assertEqual(("hard", "fallback"), self.r.decide("analyse TENAGA fundamentals", None, boom))
        self.assertEqual(("easy", "fallback"), self.r.decide("what time is the digest", None, boom))
        self.assertEqual(("easy", "fallback"), self.r.decide("what's in the canvas", None, lambda p: "banana"))


class _Conv(SimpleNamespace):
    pass


class FakeGateway:
    def __init__(self):
        self.conv = _Conv(model_override=None, reasoning_override=None)
        self.evicted = 0

    def _session_key_for_source(self, source):
        return "agent:main:feishu:dm:x"

    def _session_state(self, key):
        return SimpleNamespace(conversation=self.conv)

    def _set_session_reasoning_override(self, key, value):
        self.conv.reasoning_override = None if value is None else dict(value)

    def _evict_cached_agent(self, key):
        self.evicted += 1


@unittest.skipUnless(PLUGIN.exists(), "model-router plugin not installed")
class DispatchHookTest(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("hermes_constants", types.SimpleNamespace(
            parse_reasoning_effort=lambda v: {"enabled": True, "effort": v}))
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = _load("model_router_plugin_under_test", PLUGIN / "__init__.py")
        self.plugin.LOG = Path(self.tmp.name) / "router.jsonl"
        self.gw = FakeGateway()

    def tearDown(self):
        self.tmp.cleanup()

    def _send(self, text, reply):
        with patch.object(self.plugin, "_judge", side_effect=lambda p: reply):
            return self.plugin._on_dispatch(event=SimpleNamespace(text=text, source=object()), gateway=self.gw)

    def test_hard_sets_opus_then_easy_clears_it(self):
        self.assertIsNone(self._send("analyse gamuda, should I add?", "hard"))
        self.assertEqual("claude-opus-5", self.gw.conv.model_override["model"])
        self.assertEqual("medium", self.gw.conv.reasoning_override["effort"])
        self._send("what did the 18:30 digest say", "easy")
        self.assertIsNone(self.gw.conv.model_override)
        self.assertIsNone(self.gw.conv.reasoning_override)
        self.assertEqual(2, self.gw.evicted)
        self._send("and the price of KPJ?", "easy")
        self.assertEqual(2, self.gw.evicted)                  # unchanged route -> cached agent kept
        self.assertEqual(3, len(self.plugin.LOG.read_text().splitlines()))

    def test_manual_model_choice_is_left_alone(self):
        self.gw.conv.model_override = {"model": "claude-haiku-4-5", "provider": "anthropic", "api_key": None}
        self._send("analyse gamuda deeply", "hard")
        self.assertEqual("claude-haiku-4-5", self.gw.conv.model_override["model"])
        self.assertIn("manual-override-kept", self.plugin.LOG.read_text())

    def test_persisted_manual_override_is_respected_after_restart(self):
        store = SimpleNamespace(get_model_override=lambda key: {"model": "claude-haiku-4-5"})
        with patch.object(self.plugin, "_judge", side_effect=lambda p: "hard"):
            self.plugin._on_dispatch(event=SimpleNamespace(text="analyse gamuda", source=object()),
                                     gateway=self.gw, session_store=store)
        self.assertIsNone(self.gw.conv.model_override)        # untouched; Hermes rehydrates the user's choice


if __name__ == "__main__":
    unittest.main()
