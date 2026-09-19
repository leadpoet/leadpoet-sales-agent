"""An empty provider failure is not a successful miss or a free-call proof."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import budget_guard as budget
import deepline


class EmptyResultFailureTests(unittest.TestCase):
    bill = {'credits_charged': .5, 'cost_usd': .05,
            'pricing_status': 'final', 'settlement_status': 'queued'}

    def normalize(self, body):
        before = copy.deepcopy(body)
        result, _ = deepline.normalize_response(
            {'operation': 'execute', 'tool': 'contextdev_post_web_search', 'payload': {}, 'limit': 10},
            {'exit_code': 0, 'body': body})
        self.assertEqual(body, before)
        self.assertEqual(result['job_id'], 'empty-request')
        self.assertEqual(budget.settlement_billing(result), self.bill)
        return result

    def test_nested_empty_failures_preserve_the_error_and_exact_reported_bill(self):
        for failure in ({'error': 'provider unavailable'}, {'errors': ['provider unavailable']},
                        {'success': False, 'message': 'provider unavailable'},
                        {'status': 429, 'error': 'rate limit reached'}):
            raw = dict(failure, results=[])
            for wrapper in ({'toolResponse': {'rawV2': raw}}, {'tool_response': {'raw': raw}},
                            {'result': {'data': raw}}, {'data': raw}):
                for status in ('completed', 'no_results'):
                    with self.subTest(failure=failure, wrapper=list(wrapper), status=status):
                        result = self.normalize(dict(wrapper, status=status, job_id='empty-request', billing=self.bill))
                        self.assertIn(result['status'], {'provider_error', 'rate_limited'})
                        self.assertTrue(result.get('error'))
                        self.assertEqual(result['results'], [])

    def test_empty_success_and_result_row_errors_keep_their_existing_meaning(self):
        cases = [({'results': [], 'error': None}, 'completed', 'no_results'),
                 ({'results': [], 'errors': []}, 'completed', 'no_results'),
                 ({'results': [{'name': 'Example', 'error': 'A field in a returned row', 'status': 429}]},
                  'completed', 'ok'),
                 ({'results': [{'name': 'Example', 'error': 'A field in a returned row'}],
                   'errors': ['Another row failed']}, 'partial', 'partial')]
        for raw, outer, expected in cases:
            with self.subTest(raw=raw):
                result = self.normalize({'status': outer, 'job_id': 'empty-request', 'billing': self.bill,
                                         'toolResponse': {'rawV2': raw}})
                self.assertEqual(result['status'], expected)
                self.assertEqual(len(result['results']), len(raw['results']))

    def test_no_match_marker_does_not_hide_conflicting_failures(self):
        marker = {'error': True, 'error_code': 'NO_MATCH', 'status': 'no_result'}
        for extra in ({'error': 'provider unavailable'}, {'errors': ['provider unavailable']},
                      {'status': 'auth_failed'}, {'status': []}, {'error_code': 'TIMEOUT'}, {'success': False}):
            with self.subTest(extra=extra):
                result = self.normalize({'status': 'no_result', 'job_id': 'empty-request',
                    'billing': self.bill, 'toolResponse': {'rawV2': dict(marker, **extra)}})
                self.assertIn(result['status'], deepline._FAILURE_STATUSES)
                self.assertEqual(result['results'], [])


if __name__ == '__main__':
    unittest.main()
