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

    def test_provider_failure_leaves_complete_replay_evidence(self):
        events = []
        one = {"id": "once", "type": "function", "function": {
            "name": "suppress", "arguments": '{"email":"test@example.test"}'}}
        response = self.reply([one])
        response["choices"][0]["message"]["reasoning"] = "Inspect the consent flag."
        with self.assertRaises(TimeoutError):
            run_task(self.task, model=self.model,
                     complete=mock.Mock(side_effect=[response, TimeoutError()]), record=events.append)
        self.assertEqual([e["kind"] for e in events], ["start", "before", "reply", "tool", "before", "interrupted"])
        self.assertEqual(events[-2]["state"]["suppression"], ["test@example.test"])
        self.assertEqual(events[2]["message"]["reasoning"], "Inspect the consent flag.")
        self.assertEqual(events[3]["output"], '{"ok": true}')
        self.assertEqual(events[-1]["error_type"], "TimeoutError")
        self.assertEqual(events[-2]["turn"], 1)

    def test_resume_does_not_repeat_a_write_or_expose_the_checkpoint_to_the_model(self):
        one = {"id":"once", "type":"function", "function":{
            "name":"suppress","arguments":'{"email":"test@example.test"}'}}
        captured = []
        complete = mock.Mock(side_effect=[self.reply([one]), self.reply()])
        result = run_task(self.task, model=self.model, complete=complete, record=captured.append)
        before, = [e for e in captured if e["kind"] == "before" and e["turn"] == 1]
        source = {k: before[k] for k in ("state", "files", "log")}
        prefix = result["messages"][:-1]
        source.update(messages=prefix, tools=result["tools"])
        expert = mock.Mock(return_value=self.reply(model="other/open"))
        out = run_task(self.task, model="other/open", complete=expert, resume=source, max_tokens=127)
        self.assertEqual(out["out"]["log"], result["out"]["log"])
        self.assertEqual(expert.call_args.args[0]["messages"], complete.call_args.args[0]["messages"])
        self.assertEqual(expert.call_args.args[0]["max_tokens"], 127)
        self.assertNotIn("hidden grader control", repr(expert.call_args_list))
        self.assertEqual(out["messages"][:len(prefix)], prefix)
        source["tools"] = []
        with self.assertRaisesRegex(ValueError, "tool schemas"):
            run_task(self.task, model="other/open", complete=expert, resume=source)
        self.assertEqual(expert.call_count, 1)
