"""Observed BuiltWith/Instagram replies retain results and exact final prices."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import budget_guard as budget
import deepline
from research_tools import ResearchTools


class ObservedProviderResultsTests(unittest.TestCase):
    bill = {'credits_charged': .14, 'cost_usd': .014,
            'pricing_status': 'final', 'settlement_status': 'queued'}

    def builtwith(self):
        return {'data': {'Results': [{'Lookup': 'example.com', 'Meta': {'CompanyName': 'Example'},
                'Result': {'Paths': [{'Technologies': [{'Name': 'Example CDN', 'LastDetected': 1789776000000}]}]}}],
                'Errors': []}, 'meta': {'status': 200}}

    def instagram(self):
        return {'success': True, 'data': {'user': {'id': '123', 'username': 'example_org',
                'full_name': 'Example Organization', 'biography': 'Public profile description',
                'edge_followed_by': {'count': 10}, 'external_url': 'https://example.com'}}}

    def normalize(self, tool, raw, *, bill=True, status='completed'):
        envelope = {'status': status, 'job_id': 'fixture-request',
                    'toolResponse': {'rawV2': raw, 'view': 'data'}}
        if bill:
            envelope['billing'] = self.bill.copy()
        response = {'exit_code': 0, 'body': envelope, 'headers': {'x-request-id': 'fixture-header'}}
        original = copy.deepcopy(response)
        request = {'operation': 'execute', 'tool': tool, 'payload': {}, 'limit': 10}
        result, _ = deepline.normalize_response(request, response)
        self.assertEqual(response, original)
        self.assertEqual(result['job_id'], 'fixture-request')
        self.assertEqual(result['request_id'], 'fixture-header')
        if bill:
            self.assertEqual(budget.settlement_billing(result), self.bill)
        else:
            self.assertNotIn('billing', result)
        return result

    def test_builtwith_retains_domain_technology_details_and_one_final_bill(self):
        raw = self.builtwith()
        # The live reply contains unnamed technology entries. Keep their raw
        # metadata without inventing names or discarding the company profile.
        raw['data']['Results'][0]['Result']['Paths'][0]['Technologies'].append({'Name': '', 'LastDetected': 1789776000000})
        raw['data']['Results'].append(dict(raw['data']['Results'][0], Lookup='https://www.Example.org./path'))
        boundary_domain = '.'.join(['a' * 63] * 3 + ['a' * 61])
        raw['data']['Results'].append(dict(raw['data']['Results'][0], Lookup=boundary_domain))
        result = self.normalize('builtwith_domain_lookup', raw)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([row['domain'] for row in result['results']], ['example.com', 'example.org', boundary_domain])
        self.assertEqual(result['results'][0]['company'], 'Example')
        self.assertEqual(result['results'][0]['Result'], raw['data']['Results'][0]['Result'])
        self.assertEqual(result['billing']['credits_charged'], .14)

    def test_builtwith_empty_results_do_not_invent_a_free_bill(self):
        raw = self.builtwith()
        raw['data']['Results'] = []
        result = self.normalize('builtwith_domain_lookup', raw, bill=False)
        self.assertEqual((result['status'], result['results']), ('no_results', []))

    def test_builtwith_errors_and_malformed_results_keep_charges_without_success(self):
        cases = []
        for change in ({'Errors': [{'Code': -1, 'Message': 'Failed'}]}, {'Errors': None},
                       {'Results': [None]}, {'Results': [{'Lookup': 'example.com', 'Result': {}}]}):
            raw = self.builtwith()
            raw['data'].update(change)
            cases.append(raw)
        for lookup in ('', ' ', 'https://[invalid', 'https://', 'javascript:foo', 'not a domain',
                       'https://user@example.com', 'example.com:bad', '-foo.com', 'foo-.com',
                       'foo.-bar.com', 'a' * 64 + '.com', '.'.join(['a' * 63] * 4), None):
            raw = self.builtwith()
            raw['data']['Results'][0]['Lookup'] = lookup
            cases.append(raw)
        for technologies in (None, ['bad']):
            raw = self.builtwith()
            raw['data']['Results'][0]['Result']['Paths'][0]['Technologies'] = technologies
            cases.append(raw)
        raw = self.builtwith()
        raw['meta']['status'] = 500
        cases.append(raw)
        for raw in cases:
            with self.subTest(raw=raw):
                result = self.normalize('builtwith_domain_lookup', raw)
                self.assertNotEqual(result['status'], 'ok')
                self.assertEqual(result['results'], [])

    def test_instagram_retains_profile_without_inventing_a_business_contact(self):
        raw = self.instagram()
        result = self.normalize('scrapecreators_instagram_profile', raw)
        self.assertEqual(result['status'], 'ok')
        row = result['results'][0]
        self.assertEqual(row['instagram_profile'], raw['data']['user'])
        self.assertEqual(row['evidence_url'], 'https://www.instagram.com/example_org/')
        self.assertEqual(row['evidence_text'], 'Public profile description')
        self.assertFalse(row.get('contact_name'))
        self.assertNotEqual(row.get('entity_type'), 'contact')

    def test_instagram_missing_identity_or_failed_response_is_not_a_free_miss(self):
        cases = [{'success': False, 'data': self.instagram()['data']},
                 {'success': True, 'data': {'user': None}}, {'success': True, 'data': {}}]
        for change in ({'username': ''}, {'username': '../someone'}, {'username': 'x/y'},
                       {'id': ''}, {'id': None}, {'biography': {'text': 'unexpected'}}):
            raw = self.instagram()
            raw['data']['user'].update(change)
            cases.append(raw)
        for raw in cases:
            with self.subTest(raw=raw):
                result = self.normalize('scrapecreators_instagram_profile', raw)
                self.assertNotEqual(result['status'], 'ok')
                self.assertEqual(result['results'], [])

    def test_observed_shapes_are_scoped_to_the_exact_tool_and_completed_wrapper(self):
        for tool, raw in [('builtwith_domain_lookup', self.builtwith()),
                          ('scrapecreators_instagram_profile', self.instagram())]:
            self.assertNotEqual(self.normalize('unrelated_tool', raw)['status'], 'ok')
            self.assertNotEqual(self.normalize(tool, raw, status='pending')['status'], 'ok')

    def test_body_request_ids_survive_both_completed_envelopes_without_headers(self):
        for tool, raw in [('builtwith_domain_lookup', self.builtwith()),
                          ('scrapecreators_instagram_profile', self.instagram())]:
            for id_key in ('request_id', 'requestId'):
                for wrapper in ({'toolResponse': {'rawV2': raw, 'view': 'data'}}, {'result': {'data': raw}}):
                    with self.subTest(tool=tool, id_key=id_key, wrapper=list(wrapper)):
                        response = {'exit_code': 0, 'body': dict(wrapper, status='completed',
                            job_id='fixture-job', billing=self.bill.copy(), **{id_key: 'body-request'})}
                        before = copy.deepcopy(response)
                        result, _ = deepline.normalize_response(
                            {'operation': 'execute', 'tool': tool, 'payload': {}, 'limit': 10}, response)
                        self.assertEqual(result['status'], 'ok')
                        self.assertEqual(result['request_id'], 'body-request')
                        self.assertEqual(result['job_id'], 'fixture-job')
                        self.assertEqual(budget.settlement_billing(result), self.bill)
                        self.assertEqual(response, before)

    def test_pdl_company_tagline_and_ownership_are_not_a_contact_or_buying_signal(self):
        company = {'name': 'Example', 'website': 'example.com', 'type': 'private',
                   'headline': 'Tools for growing teams', 'linkedin_url': 'linkedin.com/company/example',
                   'employee_count': 250, 'location': {'country': 'united states'}}
        raw = {'data': {'status': 200, 'data': [company], 'total': 1}, 'meta': {'status': 200}}
        result = self.normalize('peopledatalabs_company_search', raw)
        self.assertEqual(result['status'], 'ok')
        row = result['results'][0]
        self.assertEqual((row['entity_type'], row['company'], row['domain']), ('company', 'Example', 'example.com'))
        self.assertIsNone(row['signal'])
        for field in ('contact_name', 'contact_title', 'contact_url', 'contact_email'):
            self.assertFalse(row.get(field), field)
        for field, value in company.items():
            self.assertEqual(row[field], value)
        self.assertEqual(row['content_kind'], 'unverified')

    def test_pdl_company_mapping_does_not_change_person_results(self):
        person = {'full_name': 'Ada Example', 'headline': 'Engineering Director',
                  'linkedin_url': 'https://linkedin.com/in/ada-example'}
        row = deepline.normalize_evidence(person, tool='peopledatalabs_person_search')
        self.assertEqual((row['entity_type'], row['contact_name'], row['contact_title']),
                         ('contact', 'Ada Example', 'Engineering Director'))

    def test_saved_error_reprojects_without_dispatch_mutation_or_new_billing(self):
        for tool, raw in [('builtwith_domain_lookup', self.builtwith()),
                          ('scrapecreators_instagram_profile', self.instagram())]:
            saved = {'provider': 'deepline', 'operation': 'execute', 'tool': tool,
                'status': 'schema_error', 'receipt_status': 'complete', 'results': [],
                'attempt': {'request': {'operation': 'execute', 'tool': tool, 'payload': {}, 'limit': 10}},
                'provider_response': {'exit_code': 0, 'body': {'status': 'completed', 'job_id': 'saved-request',
                    'toolResponse': {'rawV2': raw, 'view': 'data'}, 'billing': self.bill.copy()}}}
            before = copy.deepcopy(saved)
            with patch.object(deepline, 'run', side_effect=AssertionError('No provider dispatch')):
                result = ResearchTools._receipt_projection(saved)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(budget.settlement_billing(result), self.bill)
            self.assertEqual(saved, before)


if __name__ == '__main__':
    unittest.main()
