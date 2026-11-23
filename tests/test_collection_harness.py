import copy
import json
from unittest import TestCase, mock

from harness import run_task


class RoutedHarnessTest(TestCase):
    def setUp(self):
        self.task = {"type": "workflow", "scenario": {"prompt": "Suppress the synthetic contact.",
                     "truth": {"secret": "hidden grader control"}, "state": {}, "tools": ["suppress"], "files": {}}}
        self.model = "test/open"

    def reply(self, calls=None, model=None):
        return {"model": model or self.model, "choices": [{"finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": "" if calls else "Suppressed.",
                            "tool_calls": calls or []}}]}

    def test_stateful_tool_then_answer(self):
        task = copy.deepcopy(self.task)
        tool = {"type": "function", "id": "1", "function": {"name": "suppress",
                "arguments": json.dumps({"email": "sue@example.test", "reason": "opt out"})}}
        complete = mock.Mock(side_effect=[self.reply([tool]), self.reply()])
        result = run_task(task, model=self.model, complete=complete)
        self.assertEqual(result["out"]["log"][0]["tool"], "suppress")
        self.assertEqual(result["messages"][-2]["tool_call_id"], "1")
        self.assertEqual(result["finish"], "answered")
        self.assertNotIn("hidden grader control", repr(complete.call_args_list))
        self.assertEqual(task, self.task)

    def test_identity_rejected_before_tool_execution(self):
        with self.assertRaisesRegex(ValueError, "different model"):
            run_task(self.task, model=self.model, complete=lambda _: self.reply(model="wrong/model"))

    def test_research_routes_to_champion_with_assigned_model(self):
        with mock.patch("tyche_arena.host.run", return_value=[]) as research:
            self.assertEqual(run_task({"type": "lead_research", "icp": {"industry": "software"}}, model=self.model), [])
        research.assert_called_once_with({"industry": "software"}, assigned_model=self.model)
