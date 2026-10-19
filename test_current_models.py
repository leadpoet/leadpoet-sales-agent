"""Offline checks for model selection, request compatibility, and spend accounting."""

import importlib
import json
from pathlib import Path
import unittest
from unittest import mock

import harness

llm = harness.llm
governor = harness.governor
scout = harness.scout_module
bonus = harness.bonus
eventgrade = harness.eventgrade
fitproof = importlib.import_module(f"{llm.__package__}.fitproof")
roster = importlib.import_module(f"{llm.__package__}.roster")

GEMINI = "google/gemini-3.5-flash-lite"
SONNET = "anthropic/claude-sonnet-5.5"


class CurrentModelsTest(unittest.TestCase):
    def setUp(self):
        governor.install(None)
        llm.reset()
        harness.diagnostics.reset()
        self.addCleanup(governor.install, None)
        self.addCleanup(llm.reset)
        self.addCleanup(harness.diagnostics.reset)
        scout_state = mock.patch.dict(scout.LAST, clear=True)
        scout_state.start()
        self.addCleanup(scout_state.stop)

    def test_all_active_model_selections_use_current_models(self):
        strategy = json.loads(Path(harness.__file__).with_name("strategy.json").read_text())
        for key in ("fitproof_model", "scout_model"):
            self.assertEqual(strategy[key], GEMINI)
        for key in ("paragraph_model", "roster_model"):
            self.assertEqual(strategy[key], SONNET)
        self.assertEqual([bonus.MODEL, eventgrade.MODEL, fitproof.MODEL, scout.MODEL], [GEMINI] * 4)
        self.assertEqual([harness.PARAGRAPH_MODEL, roster.ROSTER_MODEL], [SONNET] * 2)
        self.assertEqual(harness.reverify_module.MODEL, "perplexity/sonar")

    def test_wire_requests_preserve_messages_limits_and_supported_parameters(self):
        messages = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Evidence."}]
        extra = {"top_p": 0.9, "top_k": 10, "reasoning": {"effort": "low"},
                 "response_format": {"type": "json_object"}, "unexpected": "discard"}
        for model in (GEMINI, SONNET, "google/gemini-2.5-flash", "anthropic/claude-sonnet-4.5"):
            with self.subTest(model=model):
                captured = []
                def transport(body):
                    captured.append(body)
                    return 200, {"choices": [{"message": {"content": '{"ok":true}'}}]}
                with mock.patch.object(llm, "TRANSPORT", transport):
                    self.assertEqual(llm.chat(messages, model=model, max_tokens=9000,
                                              temperature=0.2, extra=extra), '{"ok":true}')
                body, = captured
                self.assertEqual(body["model"], model)
                self.assertEqual(body["messages"], messages)
                self.assertEqual(body["max_tokens"], 4096)
                self.assertEqual(body["reasoning"], extra["reasoning"])
                self.assertEqual(body["response_format"], extra["response_format"])
                self.assertNotIn("unexpected", body)
                self.assertNotIn("top_k", body)  # Not an allowed broker field for any model.
                if model == SONNET:
                    self.assertNotIn("temperature", body)
                    self.assertNotIn("top_p", body)
                else:
                    self.assertEqual(body["temperature"], 0.2)
                    self.assertEqual(body["top_p"], 0.9)

    def test_scout_body_matches_actual_gemini_and_roster_wire_requests(self):
        for model in (GEMINI, SONNET):
            with self.subTest(model=model):
                captured = []
                def transport(body):
                    captured.append(body)
                    return 200, {"choices": [{"message": {"content": '{"companies":[]}'}}]}
                with mock.patch.object(llm, "TRANSPORT", transport):
                    self.assertEqual(scout.llm_json("Evidence.", model=model, max_tokens=7000), {"companies": []})
                self.assertEqual(captured, [scout.chat_body("Evidence.", 7000, model)])
                self.assertNotIn("reasoning", captured[0])  # Preserve the provider default.

    def test_new_prices_reserve_and_settle_governed_calls(self):
        for model, input_rate, output_rate in ((GEMINI, 0.30e-6, 2.50e-6), (SONNET, 2e-6, 10e-6)):
            with self.subTest(model=model):
                gov = governor.Governor(cap_usd=3.94)
                governor.install(gov)
                expected_cmax = round((300 / 3 + 64) * input_rate + 1000 * output_rate, 6)
                expected_cost = round(100 * input_rate + 50 * output_rate, 6)
                def transport(body):
                    self.assertEqual(list(gov.inflight.values()), [expected_cmax])
                    return 200, {"choices": [{"message": {"content": "ok"}}],
                                 "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
                with mock.patch.object(llm, "TRANSPORT", transport), mock.patch.object(governor, "refresh"):
                    self.assertEqual(llm.chat([{"role": "user", "content": "x" * 300}],
                                              model=model, max_tokens=1000), "ok")
                self.assertEqual(gov.inflight, {})
                self.assertAlmostEqual(gov.committed(), expected_cost)
                self.assertEqual(governor.llm_cost(model, {"cost": 0.123}), 0.123)
        self.assertEqual(governor.MODEL_PRICES["google/gemini-2.5-flash"], (0.30e-6, 2.50e-6, 0.0))
        self.assertEqual(governor.MODEL_PRICES["anthropic/claude-sonnet-4.5"], (3e-6, 15e-6, 0.0))

    def test_retry_keeps_compatible_wire_body_and_charges_success_once(self):
        gov = governor.Governor(cap_usd=3.94)
        governor.install(gov)
        success = {"choices": [{"message": {"content": "ok"}}], "usage": {"cost": 0.01}}
        transport = mock.Mock(side_effect=[(503, {}), (200, success)])
        with mock.patch.object(llm, "TRANSPORT", transport), mock.patch.object(llm, "SLEEP") as sleep, \
                mock.patch.object(governor, "refresh"):
            self.assertEqual(llm.chat([{"role": "user", "content": "Evidence."}], model=SONNET), "ok")
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(transport.call_args_list[0], transport.call_args_list[1])
        self.assertNotIn("temperature", transport.call_args.args[0])
        sleep.assert_called_once()
        self.assertAlmostEqual(gov.committed(), 0.01)
        self.assertEqual(gov.inflight, {})


if __name__ == "__main__":
    unittest.main()
