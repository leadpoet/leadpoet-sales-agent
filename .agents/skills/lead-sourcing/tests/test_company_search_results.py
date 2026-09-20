"""Observed company search replies retain evidence, pagination and exact bills."""
import copy
import json
import unittest
from unittest.mock import patch

import test_observed_provider_results as observed
import budget_guard as budget
import deepline
import test_research_tools as native
from research_tools import ResearchTools


class CompanySearchResultsTests(unittest.TestCase):
    bill = {'credits_charged': .02, 'cost_usd': .002,
            'pricing_status': 'final', 'settlement_status': 'queued'}
    normalize = observed.ObservedProviderResultsTests.normalize

    def samples(self):
        return {
            'crustdata_v3_company_search': {
                'companies': [{'basic_info': {'name': 'Example', 'primary_domain': 'example.com',
                    'professional_network_url': 'https://www.linkedin.com/company/example',
                    'description': 'Company description', 'all_domains': ['example.com']}}],
                'total_count': 8931, 'next_cursor': 'company-cursor'},
            'fullenrich_company_search': {
                'companies': [{'id': 'company-1', 'name': 'Example', 'domain': 'example.com',
                    'description': 'Company description', 'headcount': 200,
                    'social_profiles': {'professional_network': {
                        'url': 'https://www.linkedin.com/company/example'}}}],
                'metadata': {'total': 1988, 'offset': 0, 'search_after': 'company-cursor'}},
        }

    def test_returned_companies_preserve_identity_raw_fields_and_final_charge(self):
        for tool, raw in self.samples().items():
            with self.subTest(tool=tool):
                result = self.normalize(tool, raw)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(len(result['results']), 1)  # Not total_count/total.
                row = result['results'][0]
                self.assertEqual((row['company'], row['domain'], row['entity_type']),
                                 ('Example', 'example.com', 'company'))
                self.assertEqual(row['company_linkedin_url'], 'https://www.linkedin.com/company/example')
                self.assertEqual(row['content_kind'], 'unverified')
                self.assertIsNone(row['signal'])
                for field in ('contact_name', 'contact_title', 'contact_url', 'contact_email'):
                    self.assertFalse(row.get(field), field)
                for field, value in raw['companies'][0].items():
                    self.assertEqual(row[field], value)
                self.assertEqual(result['pagination']['next_cursor'], 'company-cursor')
                total = 'total_count' if tool.startswith('crustdata') else 'total'
                self.assertEqual(result['pagination'][total], 8931 if total == 'total_count' else 1988)
                self.assertEqual(result['billing'], self.bill)

    def test_empty_search_preserves_pagination_without_inventing_free_billing(self):
        for tool, raw in self.samples().items():
            raw['companies'] = []
            with self.subTest(tool=tool):
                result = self.normalize(tool, raw, bill=False)
                self.assertEqual((result['status'], result['results']), ('no_results', []))
                self.assertIn('pagination', result)
                self.assertNotIn('billing', result)

    def test_missing_or_malformed_companies_remain_errors_with_charge(self):
        for tool, raw in self.samples().items():
            for rows in (None, {}, ['invalid'], [None]):
                with self.subTest(tool=tool, rows=rows):
                    result = self.normalize(tool, dict(raw, companies=rows))
                    self.assertEqual(result['status'], 'schema_error')
                    self.assertEqual(result['results'], [])
                    self.assertEqual(result['billing'], self.bill)

    def test_provider_failures_are_not_hidden_by_company_rows(self):
        for tool, raw in self.samples().items():
            for failure in ({'success': False}, {'ok': False}, {'status': 'rate_limited'}):
                with self.subTest(tool=tool, failure=failure):
                    result = self.normalize(tool, dict(raw, **failure))
                    self.assertIn(result['status'], deepline._FAILURE_STATUSES)
                    self.assertEqual(result['results'], [])
                    self.assertEqual(result['billing'], self.bill)

    def test_company_parser_does_not_apply_to_other_tools_or_pending_jobs(self):
        for tool, raw in self.samples().items():
            self.assertNotEqual(self.normalize('unrelated_search', raw)['status'], 'ok')
            self.assertNotEqual(self.normalize(tool, raw, status='pending')['status'], 'ok')

    def test_missing_primary_domain_is_not_filled_from_unverified_alternative(self):
        raw = self.samples()['crustdata_v3_company_search']
        raw['companies'][0]['basic_info'].update(primary_domain='', website='', all_domains=['alternative.example'])
        row = self.normalize('crustdata_v3_company_search', raw)['results'][0]
        self.assertIsNone(row['domain'])
        self.assertEqual(row['basic_info']['all_domains'], ['alternative.example'])

    def test_saved_schema_errors_recover_in_memory_without_paid_replay(self):
        for tool, raw in self.samples().items():
            saved = {'provider': 'deepline', 'operation': 'execute', 'tool': tool,
                'status': 'schema_error', 'receipt_status': 'complete', 'results': [],
                'attempt': {'request': {'operation': 'execute', 'tool': tool, 'payload': {}, 'limit': 10}},
                'provider_response': {'exit_code': 0, 'body': {'status': 'completed', 'job_id': 'saved-request',
                    'toolResponse': {'rawV2': raw}, 'billing': self.bill.copy()}}}
            before = copy.deepcopy(saved)
            with patch.object(deepline, 'run', side_effect=AssertionError('No provider dispatch')):
                result = ResearchTools._receipt_projection(saved)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(len(result['results']), 1)
            self.assertEqual(budget.settlement_billing(result), self.bill)
            self.assertEqual(saved, before)


class CompanySearchBillingJourneyTests(unittest.TestCase):
    setUp = native.ResearchToolTests.setUp
    unbilled_provider = native.ResearchToolTests.unbilled_provider

    def test_native_finish_settles_zero_usage_then_allows_next_paid_lookup(self):
        tool = 'fullenrich_company_search'
        self.provider.raw = {'status': 'completed', 'job_id': 'usage-company-request', 'toolResponse': {
            'rawV2': CompanySearchResultsTests().samples()[tool]}}
        def execute(request, capture):
            if request['operation'] != 'execute' and request.get('tool') == tool:
                result, code = self.provider(request, capture)
                result['results'][0].update(provider='fullenrich',
                    pricing={'unit': 'usage', 'creditsPerUnit': None, 'usdPerUnit': None})
                return result, code
            return self.unbilled_provider(request, capture)
        self.tools.execute = execute
        self.tools.start(self.request, max_usd=.75)
        outcome = self.tools.lookup([native.check(tool=tool, inputs={'query': 'Example'})])['lookups'][0]
        self.assertEqual(outcome['status'], 'ok')
        rid = outcome['route']
        receipt_path = self.path.parent / 'receipts' / (rid + '.json')
        before, calls = receipt_path.read_bytes(), len(self.provider.requests)
        initial = budget.load_ledger(self.path)
        started = json.loads(self.path.read_text())['stop_check']['started_at']
        self.assertEqual(budget.spending_stop(initial), 'billing_pending')
        self.assertTrue(initial['calls'][rid]['catalog_sha256'])
        row = {'id': 'usage-bill', 'request_id': 'usage-company-request', 'provider': 'fullenrich',
            'operation': tool, 'status': 'completed', 'charge_state': 'free', 'credits': 0, 'delta': 0,
            'outcome': 'miss', 'provider_units': 0, 'pricing_basis': 'usage', 'pricing_model': 'provider_usage'}
        self.tools.execute = None  # Resume through the native billing recovery path.
        with patch.object(deepline, '_invoke', return_value=(0, json.dumps({'org_id': 'fixture-org',
                'recent': {'entries': [row], 'has_more': False}}), '')), patch('deepline_http.api_key', return_value=None):
            finished = self.tools.finish()
        self.assertNotEqual(finished.get('reason'), 'billing_pending')
        self.assertFalse(finished['delivery_allowed'])
        state = budget.load_ledger(self.path)
        self.assertEqual((state['calls'][rid]['state'], state['calls'][rid]['actual_credits']), ('settled', '0'))
        self.assertEqual((receipt_path.read_bytes(), len(self.provider.requests)), (before, calls))
        self.assertEqual(state['usd_limit'], initial['usd_limit'])
        self.assertEqual(json.loads(self.path.read_text())['stop_check']['started_at'], started)
        self.provider.raw = {'status': 'ok', 'results': [{'company': 'Next', 'domain': 'next.test'}]}
        self.tools.execute = self.provider
        self.tools.lookup([native.check('next.test')])
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))['provider_usd'], .02)
        self.assertEqual(receipt_path.read_bytes(), before)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])


if __name__ == '__main__':
    unittest.main()
