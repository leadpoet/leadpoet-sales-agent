#!/usr/bin/env python3
"""Research closes before the hard deadline; the deadline never moves.

Default one worker, real actual_cost ledger, real supervisor, real watchdog and a
real child process standing in for Codex. Fixture provider only; nothing is sent.

Every timed case starts its run with a full hour left and sets the clock only
after startup has finished, so a slow startup cannot consume the test's window.
"""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import codex_tyche
from parallel_sourcing import run_research
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.agents/skills/lead-sourcing/tests'))
from test_research_tools import FixtureProvider, check
from test_research_interface import setup_request
from research_tools import ResearchTools
from validate_run import evaluate_stop, research_closes, run_deadline
import budget_guard
import run_coordination as coordination

# Stands in for `codex exec --json`: one journaled response, then either a normal
# end of turn at FAKE_END_AT or an open turn that never ends.
FAKE_CODEX = r'''
import json, os, sys, time, uuid
from pathlib import Path
phase = 'finalization' if os.environ.get('TYCHE_FINALIZATION_ONLY') == '1' else 'research'
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(phase + '\n')
thread = str(uuid.uuid4())
usage = dict(input_tokens=30000, cached_input_tokens=0, cache_write_input_tokens=0,
             output_tokens=200, reasoning_output_tokens=0, total_tokens=30200)
journal = Path(os.environ['CODEX_HOME']) / 'sessions/2026/09/20' / ('rollout-' + thread + '.jsonl')
journal.parent.mkdir(parents=True, exist_ok=True)
print(json.dumps({'type': 'thread.started', 'thread_id': thread}), flush=True)
journal.write_text(json.dumps({'type': 'token_usage_record', 'timestamp': '2026-09-20T00:00:00Z',
    'payload': {'thread_id': thread, 'turn_id': 'turn', 'response_id': 'response-' + thread, 'usage': usage}}) + '\n')
end_at = float(os.environ['FAKE_END_AT']) if phase == 'research' else time.time()
while time.time() < end_at:
    time.sleep(.05)
print(json.dumps({'type': 'turn.completed', 'usage': usage}), flush=True)
'''
DURATION = 3600


class DeadlineWindDownTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.request = self.root / 'request.txt'
        self.request.write_text('Fixture ICP with an explicit time limit.')
        self.run = self.root / 'results.json'
        self.profile = self.root / 'profile'
        self.log = self.root / 'sessions.log'
        self.provider = FixtureProvider()

    def start(self, *, remaining, closing):
        """Start a one-hour run, then set its clock so `remaining` seconds are left."""
        body = setup_request()
        body['request']['max_duration_seconds'] = DURATION
        environment = {'TYCHE_WIND_DOWN_SECONDS': str(closing)} if closing else {}
        ResearchTools(self.run, execute=self.provider, environment=environment).start(body['request'])
        self.assertEqual(budget_guard.load_ledger(self.run)['version'], 2)  # Default actual_cost.
        return self.rewind(remaining)

    def rewind(self, remaining):
        began = (datetime.now(timezone.utc) - timedelta(seconds=DURATION - remaining)).isoformat()
        with budget_guard.transaction(self.run) as document:
            document['stop_check']['started_at'] = began
        with budget_guard.transaction(budget_guard.ledger_path(self.run)) as ledger:
            ledger['initial_started_at'] = began
        document = json.loads(self.run.read_text())
        self.started = began
        self.limit = run_deadline(document).timestamp()
        self.closes = research_closes(document).timestamp()
        return document

    def supervise(self, *, turn_ends_after_closing):
        env = dict(os.environ, TYCHE_RUN_STARTED_AT=self.started, CODEX_HOME=str(self.profile),
                   FAKE_LOG=str(self.log), FAKE_END_AT=str(self.closes + turn_ends_after_closing))
        def close(request_file, receipt, environment=None):
            # The confirmed-cost gate permits review after an incomplete model turn.
            # Strict delivery still requires that turn's final usage receipt.
            pending = budget_guard.actual_cost_summary(budget_guard.load_ledger(self.run))['missing_model_usage']
            delivered = 'finalization' in self.log.read_text().split() and not pending
            return codex_tyche.write_worker_status(request_file, {
                'status': 'delivered' if delivered else 'incomplete', 'delivery_allowed': delivered})
        with patch('codex_tyche.close_worker', side_effect=close), \
                patch('codex_tyche.save_report', return_value=self.root / 'run-costs.json'), \
                patch.object(codex_tyche.LocalHost, 'export_partial', staticmethod(lambda *a: {'exported': False})), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = codex_tyche.supervise_worker([sys.executable, '-c', FAKE_CODEX, 'prompt'],
                                                self.request, env, self.profile)
        return code, json.loads((self.root / 'worker-status.json').read_text())

    def receipts(self):
        return [json.loads(p.read_text()) for p in sorted((self.root / 'model-usage').glob('*.json'))]

    def sent(self):
        return sum(r['operation'] == 'execute' for r in self.provider.requests)

    def test_inspect_finish_and_provider_admission_agree_while_research_is_closing(self):
        self.start(remaining=20, closing=10)
        tools = ResearchTools(self.run, execute=self.provider)
        self.assertEqual(tools._overview()['stop'], 'continue')
        tools.call('tyche_lookup', {'checks': [check()]})
        self.assertEqual(self.sent(), 1)  # Before closing, research is dispatched as before.
        self.rewind(5)  # Now inside the ten-second closing window, five seconds before the deadline.
        inspect = tools.call('tyche_inspect', {})
        finish = tools.call('tyche_finish', {})
        with self.assertRaisesRegex(ValueError, 'time_limit_reached'):
            tools.call('tyche_lookup', {'checks': [check('late.test')]})
        self.assertLess(time.time(), self.limit)  # All of this happened before the hard deadline.
        self.assertEqual(inspect['stop'], 'time_limit_reached')
        self.assertNotEqual(finish.get('status'), 'needs_research')
        self.assertNotIn('Execute the next useful research action', json.dumps(finish))
        self.assertEqual(self.sent(), 1)  # No provider work opened inside the closing window.

    def test_pool_worker_admission_uses_the_same_closing_time(self):
        self.start(remaining=60, closing=120)  # Inside the window, a minute before the deadline.
        coordination.configure(self.run, 2)
        coordination.register(self.run, 'worker-1', 'worker-1')
        worker = ResearchTools(self.run, execute=self.provider, environment={
            'TYCHE_WORKER_ID': 'worker-1', 'TYCHE_WORKER_GENERATION': 'worker-1'})
        with self.assertRaises(ValueError):
            worker.call('tyche_lookup', {'checks': [check('pool.test')]})
        self.assertLess(time.time(), self.limit)
        self.assertEqual(self.sent(), 0)

    def test_open_turn_that_ends_before_the_deadline_hands_over_to_the_existing_review(self):
        self.start(remaining=16, closing=8)
        code, status = self.supervise(turn_ends_after_closing=2)
        # One research session only: a session ending while research is closed is not relaunched.
        self.assertEqual(self.log.read_text().split(), ['research', 'finalization'])
        self.assertEqual((code, status['delivery_allowed']), (0, True))
        self.assertTrue(all(r['status'] == 'complete' and r['usage_reconciled'] for r in self.receipts()))
        research = min(self.receipts(), key=lambda r: r['started_at'])
        self.assertLess(datetime.fromisoformat(research['finished_at']).timestamp(), self.limit)
        ledger = budget_guard.load_ledger(self.run)
        self.assertIsNone(budget_guard.spending_stop(ledger))
        self.assertEqual(budget_guard.actual_cost_summary(ledger)['missing_model_usage'], [])

    def test_turn_still_open_at_the_deadline_is_stopped_there_and_its_unknown_charge_stays_visible(self):
        self.start(remaining=12, closing=6)
        code, status = self.supervise(turn_ends_after_closing=120)
        stopped_after_limit = time.time() - self.limit
        phases = self.log.read_text().split()
        self.assertEqual(phases.count('research'), 1)
        self.assertIn('finalization', phases)  # Review is allowed, but delivery cannot pass.
        self.assertEqual(code, 1)
        self.assertFalse(status['delivery_allowed'])
        # The hard deadline is unchanged: stopped at the limit, with no extra wait.
        self.assertGreaterEqual(stopped_after_limit, 0)
        self.assertLess(stopped_after_limit, 15)
        killed = min(self.receipts(), key=lambda receipt: receipt['started_at'])
        self.assertEqual((killed['status'], killed['failure_kind'], killed['usage'], killed['usage_reconciled']),
                         ('incomplete', 'deadline_reached', None, False))
        summary = budget_guard.actual_cost_summary(budget_guard.load_ledger(self.run))
        self.assertEqual(len(summary['missing_model_usage']), 1)
        self.assertEqual(summary['status'], 'incomplete')
        self.assertGreater(summary['estimated_llm_usd'], 0)  # Known responses stay counted; nothing is invented.

    def test_without_a_closing_window_the_same_turn_is_stopped_at_the_limit_as_today(self):
        self.start(remaining=8, closing=None)
        self.assertEqual(self.closes, self.limit)
        code, status = self.supervise(turn_ends_after_closing=2)
        self.assertEqual(self.log.read_text().split().count('research'), 1)
        self.assertEqual(code, 1)
        self.assertFalse(status['delivery_allowed'])

    def test_pool_keeps_the_rest_of_the_window_then_stops_at_the_hard_deadline(self):
        self.start(remaining=100, closing=120)  # Research has closed; the deadline is 100 seconds away.
        waiting, stops = [], []
        offset = [0]
        real_time = time.time
        def worker(command, cwd, worker_env, receipt, **options):
            def watch(seconds, into):
                until = time.monotonic() + seconds
                while time.monotonic() < until:
                    into.append(options['cost_stop']())
                    time.sleep(.05)
            watch(1.5, waiting)
            offset[0] = 60  # Past the pool's ordinary 45-second wait, still 40 seconds before the deadline.
            watch(1.5, waiting)
            offset[0] = 120  # Past the hard deadline.
            until = time.monotonic() + 10
            while not options['cost_stop']() and time.monotonic() < until:
                time.sleep(.05)
            stops.append(options['cost_stop']())
            receipt.finish(1)
            return 1
        with patch('run_costs.execute_with_usage', side_effect=worker), \
                patch('parallel_sourcing.time.time', side_effect=lambda: real_time() + offset[0]), \
                contextlib.redirect_stdout(io.StringIO()):
            # The pool drains at the hard deadline. Unknown usage remains for strict delivery.
            run_research(['codex', 'exec', 'Fixture ICP'], self.request,
                         {'TYCHE_RUN_STARTED_AT': self.started}, self.root)
        self.assertEqual(set(waiting), {None})
        self.assertEqual(stops, ['pool_stopped'])
        self.assertEqual(coordination.snapshot(self.run)['phase'], 'finalization')

    def test_operator_extension_must_leave_research_time(self):
        self.start(remaining=-5, closing=120)  # The deadline has passed.
        before = self.run.read_bytes()
        soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        with self.assertRaisesRegex(ValueError, 'must leave research time'):
            codex_tyche.authorize_resume(self.request, soon, 'User asked for one more minute')
        self.assertEqual(self.run.read_bytes(), before)  # Nothing is recorded as granted.
        later = datetime.now(timezone.utc) + timedelta(seconds=600)
        codex_tyche.authorize_resume(self.request, later.isoformat(), 'User asked for ten more minutes')
        document = json.loads(self.run.read_text())
        self.assertEqual(run_deadline(document), later)
        self.assertEqual(research_closes(document), later - timedelta(seconds=120))
        self.assertEqual(ResearchTools(self.run, execute=self.provider)._overview()['stop'], 'continue')

    def test_operator_extension_without_a_window_works_as_before(self):
        self.start(remaining=-5, closing=None)
        soon = datetime.now(timezone.utc) + timedelta(seconds=60)
        codex_tyche.authorize_resume(self.request, soon.isoformat(), 'User asked for one more minute')
        self.assertEqual(run_deadline(json.loads(self.run.read_text())), soon)
        self.assertEqual(ResearchTools(self.run, execute=self.provider)._overview()['stop'], 'continue')

    def test_startup_that_would_finish_after_research_closes_creates_no_run(self):
        body = setup_request()
        body['request']['max_duration_seconds'] = DURATION
        began = (datetime.now(timezone.utc) - timedelta(seconds=DURATION - 60)).isoformat()  # Inside the window.
        with patch.dict(os.environ, {'TYCHE_RUN_STARTED_AT': began}):
            tools = ResearchTools(self.run, execute=self.provider, environment={'TYCHE_WIND_DOWN_SECONDS': '120'})
            result = tools.call('tyche_start', body)
        self.assertEqual(result['status'], 'operationally_blocked')
        self.assertFalse(self.run.exists())
        self.assertFalse(self.run.with_name('results.json.budget.json').exists())
        self.assertEqual(self.sent(), 0)

    def test_retry_with_saved_catalog_descriptions_after_research_closes_creates_no_run(self):
        # An earlier attempt saved its free catalog descriptions 59 minutes ago, then stopped.
        began = (datetime.now(timezone.utc) - timedelta(seconds=DURATION - 60)).isoformat()
        first = ResearchTools(self.run, execute=self.provider)
        for tool, filename in (('harvestapi_get_company', 'company-tool.json'), ('harvestapi_get_profile', 'profile-tool.json')):
            first._startup_contract(tool, filename, began)
        fetched = len(self.provider.requests)
        body = setup_request()
        body['request']['max_duration_seconds'] = DURATION
        retry = ResearchTools(self.run, execute=self.provider, environment={'TYCHE_WIND_DOWN_SECONDS': '120'})
        result = retry.call('tyche_start', body)
        self.assertEqual(len(self.provider.requests), fetched)  # The saved descriptions were reused.
        self.assertEqual(result['status'], 'operationally_blocked')
        self.assertIn('already closed', result['reason'])
        self.assertFalse(self.run.exists())
        self.assertFalse(self.run.with_name('results.json.budget.json').exists())

    def test_malformed_window_setting_is_a_clear_startup_error(self):
        tools = ResearchTools(self.run, execute=self.provider, environment={'TYCHE_WIND_DOWN_SECONDS': 'soon'})
        with self.assertRaisesRegex(ValueError, 'whole number of seconds'):
            tools.start(setup_request()['request'])
        self.assertFalse(self.run.exists())

    def test_short_runs_ordinary_runs_saved_runs_and_resumes_keep_their_behavior(self):
        def saved(duration, closing, folder):
            run = self.root / folder / 'results.json'
            body = setup_request()
            body['request']['max_duration_seconds'] = duration
            environment = {'TYCHE_WIND_DOWN_SECONDS': str(closing)} if closing else {}
            ResearchTools(run, execute=self.provider, environment=environment).start(body['request'])
            return run, json.loads(run.read_text())
        # An ordinary run has no time limit, so nothing closes and nothing is saved.
        run, ordinary = saved(None, 120, 'ordinary')
        self.assertNotIn('closing_seconds', ordinary['stop_check'])
        self.assertIsNone(research_closes(ordinary))
        self.assertEqual(ResearchTools(run, execute=self.provider)._overview()['stop'], 'continue')
        # Two hours gives up 120 seconds; ten minutes keeps nine tenths of its window.
        self.assertEqual([saved(d, 120, f'run-{d}')[1]['stop_check']['closing_seconds'] for d in (7200, 600)],
                         [120, 60])
        # Hosts that do not ask, such as Arena, and every run saved before this change close at the deadline.
        run, unasked = saved(7200, None, 'unasked')
        self.assertNotIn('closing_seconds', unasked['stop_check'])
        self.assertEqual(research_closes(unasked), run_deadline(unasked))
        inside = run_deadline(unasked) - timedelta(seconds=60)
        ledger = budget_guard.load_ledger(run)
        self.assertEqual(evaluate_stop(unasked, now=inside, execution_budget=ledger)['decision'], 'continue')
        # The decision depends only on the saved run, so every process agrees.
        asked = copy.deepcopy(unasked)
        asked['stop_check']['closing_seconds'] = 120
        self.assertEqual(evaluate_stop(asked, now=inside, execution_budget=ledger)['decision'], 'time_limit_reached')
        # A resume cannot change the saved window.
        run, first = saved(7200, 120, 'resumed')
        body = setup_request()
        body['request']['max_duration_seconds'] = 7200
        ResearchTools(run, execute=self.provider, environment={'TYCHE_WIND_DOWN_SECONDS': '900'}).start(body['request'])
        self.assertEqual(json.loads(run.read_text())['stop_check'], first['stop_check'])
        # The local launcher asks for the window and hands it to its tool process.
        self.assertIn('TYCHE_WIND_DOWN_SECONDS', codex_tyche.tool_configuration(run))


if __name__ == '__main__':
    unittest.main()
