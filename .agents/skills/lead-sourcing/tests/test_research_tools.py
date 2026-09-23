"""Native tool journeys use fixture provider responses, never paid services."""
import copy
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import budget_guard as budget
import deepline
import email_receipts
import research_input
import research_tools
from research_tools import ResearchTools
import run_attempt as runner
from test_research_interface import setup_request
from tyche_tools import SandboxedTools, serve


class FixtureProvider:
    def __init__(self):
        self.requests = []
        self.raw = {"status": "ok", "element": {"name": "ExamplePay", "website": "https://example.test",
            "linkedinUrl": "https://www.linkedin.com/company/examplepay/",
            "employeeCountRange": {"start": 51, "end": 200}}}
        self.rate = .2
        self.billed_rate = None
        self.delay = 0
        self.dispatch_barrier = None
        self.peak = self.active = 0
        self.lock = threading.Lock()

    def __call__(self, request, capture):
        self.requests.append(copy.deepcopy(request))
        tool = request.get("tool", "fixture-search")
        if request["operation"] != "execute":
            key = "email" if tool in {"zerobounce_validate", "bounceban_verify_single"} else "url" if (tool.startswith("harvestapi") or tool in {"firecrawl_scrape", "contextdev_post_web_crawl", "discolike_extract", "generic_http_request"}) else "website" if tool == "aviato_get_company_funding_rounds" else "query"
            fields = ["first_name", "last_name", "domain"] if tool in {"fixture_email_finder", "hunter_email_finder"} else [key]
            if tool in {"hunter_domain_search", "findymail_find_from_domain", "search_contact"}:
                fields = ["domain"]
            if tool == "bounceban_get_single_status":
                fields = ["id"]
            if tool in {"fixture_person_lookup", "hunter_people_find"}:
                fields = ["email"]  # A lookup keyed by an address the caller already holds.
            properties = {field: {"type": "string"} for field in fields}
            if tool == "search_contact":
                properties["contact_linkedin"] = {"type": "string"}
            if tool == "harvestapi_get_profile":
                properties["findEmail"] = {"type": "string", "enum": ["true", "false"]}
            return {"provider": "deepline", "operation": request["operation"], "status": "ok", "results": [{
                "toolId": tool, "callable": True, "connected": True, "billingSource": "managed_by_deepline",
                "inputSchema": {"fields": [{"name": field, "required": True, "type": "string"} for field in fields],
                    "jsonSchema": {"properties": properties, "additionalProperties": False}},
                "pricing": {"creditsPerUnit": self.rate, "unit": "call"}}]}, 0
        def dispatch():
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                if self.dispatch_barrier:
                    self.dispatch_barrier.wait(timeout=30)
                time.sleep(self.delay)
                raw = {"exit_code": 0, "body": copy.deepcopy(self.raw), "stderr": ""}
                rate = self.rate if self.billed_rate is None else self.billed_rate
                raw["body"]["billing"] = {"credits_charged": rate, "cost_usd": round(rate * .1, 8),
                    "pricing_status": "pending" if raw["body"].get("status") in {"partial", "verifying", "pending", "processing", "queued"} else "final",
                    "settlement_status": "queued"}
                capture(raw)
                return deepline.normalize_response(request, raw)
            finally:
                with self.lock:
                    self.active -= 1
        return budget.guarded_call(request, "deepline", dispatch)


def check(target="example.test", **options):
    return {"target": target, "phase": "account_verification", "purpose": "Check company fit",
            "tool": "harvestapi_get_company", "inputs": {"url": "https://www.linkedin.com/company/" + target}, **options}


def captured_page(tools, provider, *, target="example.test", url="https://example.test/news",
                  text="Example announced a planned partnership.", date="2026-01-01"):
    metadata = {"sourceURL": url, "statusCode": 200}
    if date:
        metadata["article:published_time"] = date
    provider.raw = {"status": "completed", "toolResponse": {"rawV2": {"data": {
        "metadata": metadata, "markdown": text}}}}
    return tools.lookup([check(target, purpose="Read captured page " + url, tool="firecrawl_scrape", inputs={"url": url})])["lookups"][0]["results"][0]["ref"]


def review_findings(packet):
    # Fixture judgments exercise persistence/gates, not model factual accuracy.
    return [{"target": company["company"]["domain"], "source_refs": list(company["sources"]),
             "finding": "The captured manufacturing and completed integration passages support the fixture fit and factual prose; potential coordination benefits remain qualified analysis."}
            for company in packet["companies"]]


class ResearchToolTests(unittest.TestCase):
    def setUp(self):
        clock = patch("provider_pricing.datetime")
        clock.start().now.return_value = datetime(2026, 9, 17, tzinfo=timezone.utc)
        self.addCleanup(clock.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "run/results.json"
        self.provider = FixtureProvider()
        self.tools = ResearchTools(self.path, execute=self.provider)
        self.request = setup_request()["request"]

    def start(self, **options):
        return self.tools.call("tyche_start", {"request": self.request, **options})

    def test_disabling_scrapingdog_keeps_default_deepline_budget_on_start_and_resume(self):
        self.start(max_usd=2.5, provider_credit_limits={"scrapingdog": 0})
        state = budget.load_ledger(self.path)
        self.assertEqual(state["credit_limits"], {"deepline": "25.0", "scrapingdog": "0"})
        self.assertEqual(state["usd_limit"], "2.5")
        self.lookup()
        state = budget.load_ledger(self.path)
        self.assertEqual(budget.actual_cost_summary(state)["provider_usd"], .02)
        with self.assertRaisesRegex(budget.BudgetError, "disabled"):
            budget.check_allowance(state, "scrapingdog", None, 0)
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        self.start(max_usd=2.5, provider_credit_limits={"scrapingdog": 0})
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)

    def test_empty_exclusions_start_and_bound_resume_preserve_criteria(self):
        self.request["icp"]["exclusions"] = []
        self.start(max_usd=1.2)
        self.assertEqual(json.loads(self.path.read_text())["request"]["icp"]["exclusions"], [])
        self.assertEqual(budget.load_ledger(self.path)["calls"], {})
        source = self.path.parent / "request-exclusions.json"
        source.write_text("[]")
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        self.request["icp"].pop("exclusions")
        self.start(max_usd=1.2)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)

    def test_bound_exclusions_preserve_long_list_and_resume_without_retyping(self):
        self.path.parent.mkdir()
        exclusions = [f"Excluded Company {i}" for i in range(450)] + ["Ä Exact Name & Co", "pure reinsurers"]
        source = self.path.parent / "request-exclusions.json"
        source.write_text(json.dumps(exclusions, ensure_ascii=False), encoding="utf-8")
        self.request["icp"]["exclusions"] = ["Excluded Company 0", "Unrequested extra exclusion"]
        original = copy.deepcopy(self.request)
        self.start(max_usd=1.2)
        document = json.loads(self.path.read_text())
        self.assertEqual(document["request"]["icp"]["exclusions"], exclusions)
        self.assertEqual(self.request, original)
        self.assertEqual(budget.load_ledger(self.path)["calls"], {})
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), source.read_bytes()
        self.request["icp"].pop("exclusions")
        self.start(max_usd=1.2)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), source.read_bytes()), before)
        calls = len(self.provider.requests)
        source.write_text(json.dumps(exclusions[:-1]))
        with self.assertRaisesRegex(ValueError, "differs from the saved exclusions"):
            self.start(max_usd=1.2)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before[:2])
        self.assertEqual(len(self.provider.requests), calls)

    def test_invalid_bound_exclusions_fail_before_catalog_or_ledger_creation(self):
        self.path.parent.mkdir()
        source = self.path.parent / "request-exclusions.json"
        for contents in ('not JSON', '{}', '[null]', '[""]', '[42]'):
            with self.subTest(contents=contents):
                source.write_text(contents)
                with self.assertRaises(ValueError):
                    self.start()
                self.assertEqual(self.provider.requests, [])
                self.assertFalse(self.path.exists())
                self.assertFalse(self.path.with_name(self.path.name + ".budget.json").exists())
        source.unlink()
        outside = self.path.parent.parent / "outside.json"
        outside.write_text('["Outside"]')
        source.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "inside this run directory"):
            self.start()
        self.assertEqual(self.provider.requests, [])
        outside.unlink()
        with self.assertRaisesRegex(ValueError, "inside this run directory"):
            self.start()
        self.assertEqual(self.provider.requests, [])

    def test_dollar_budget_cannot_silently_become_a_credit_override(self):
        # Regression: the $2.50 cybersecurity run received a 2.5-credit cap.
        self.request["budget"] = {"deepline_credits": 2.5, "scrapingdog_credits": 0, "hard_stop": True}
        before = copy.deepcopy(self.request)
        with self.assertRaisesRegex(ValueError, "set max_usd for dollars"):
            self.start(max_usd=2.5)
        self.assertEqual(self.request, before)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".budget.json").exists())
        self.assertEqual(self.provider.requests, [])
        self.request.pop("budget")
        self.start(max_usd=2.5)
        state = budget.load_ledger(self.path)
        self.assertEqual(state["usd_limit"], "2.5")
        self.assertEqual(state["credit_limits"]["deepline"], "25.0")

    def test_explicit_credit_limit_remains_binding_and_resume_preserves_spend(self):
        self.start(max_usd=2.5, provider_credit_limits={"deepline": .2})
        self.lookup()
        state = budget.load_ledger(self.path)
        self.assertEqual(state["credit_limits"]["deepline"], "0.2")
        self.assertEqual(budget.spending_stop(state), "budget_exhausted")
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.start()  # The caller need not repeat the saved credit limit.
        self.start(provider_credit_limits={"deepline": .2})
        with self.assertRaises(ValueError):
            self.start(provider_credit_limits={"deepline": 25})
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_saved_legacy_credit_budget_can_resume_without_rewriting_it(self):
        import research_input
        request = copy.deepcopy(self.request)
        request["budget"] = {"deepline_credits": 2.5, "scrapingdog_credits": 0, "hard_stop": True}
        document, options = research_input.start_document(self.path, {"request": request, "max_usd": 2.5})
        document["budget"]["policy"] = "reserved"
        self.path.parent.mkdir(parents=True)
        budget.create_run(self.path, document, **options)
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        self.tools.start(document["request"])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)
        self.assertEqual(self.provider.requests, [])

    def test_invalid_explicit_credit_limits_fail_before_catalog_calls(self):
        for value in ({"deepline": -1}, {"deepline": True}, {"deepline": "2.5"}, {"typo": 2.5}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.start(provider_credit_limits=value)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".budget.json").exists())
        self.assertEqual(self.provider.requests, [])

    def lookup(self, *checks):
        return self.tools.call("tyche_lookup", {"checks": list(checks or [check()])})

    def test_paid_post_rows_remain_selectable_beyond_preview_and_in_old_receipts(self):
        self.start()
        for count in (40, 50):
            with self.subTest(count=count):
                self.provider.raw = {"toolResponse": {"rawV2": {"elements": [
                    {"id": str(i), "content": f"Appointment announcement {i}",
                     "linkedinUrl": f"https://www.linkedin.com/posts/example-{i}",
                     "postedAt": {"date": "2026-08-03"}} for i in range(count)]}}}
                view = self.lookup(check(f"posts-{count}.test", tool="harvestapi_company_posts"))["lookups"][0]
                self.assertEqual((len(view["results"]), view["result_count"], view["next_offset"]), (10, count, 10))
                rid = view["route"]
                receipt_path = self.path.parent / "receipts" / (rid + ".json")
                saved = json.loads(receipt_path.read_text())
                self.assertEqual(len(saved["results"]), count)
                # Simulate the historical preview-only normalization, retaining
                # its immutable full response and original requested limit.
                saved["results"] = saved["results"][:10]
                receipt_path.write_text(json.dumps(saved))
                before = receipt_path.read_bytes()
                calls = len(self.provider.requests)
                page = self.tools.inspect(ref=rid, offset=10)
                self.assertEqual(page["result_count"], count)
                self.assertEqual(page["results"][3]["ref"], rid + ":13")
                row, _, _ = self.tools._resolve(rid + ":13")
                self.assertEqual(row["evidence_text"], "Appointment announcement 13")
                self.assertEqual(self.tools.inspect(ref=rid, offset=count - 1)["next_offset"], None)
                self.assertEqual((receipt_path.read_bytes(), len(self.provider.requests)), (before, calls))

    def test_legacy_unknown_variable_prices_reject_overrides_before_dispatch(self):
        document, options = research_input.start_document(self.path, {
            "request": self.request, "max_usd": 1, "verification_reserve_credits": 0})
        document["budget"]["policy"] = "reserved"
        self.path.parent.mkdir(parents=True)
        budget.create_run(self.path, document, **options)
        # Existing version 1 calls keep their reservation contract. New native
        # requests use actual costs and no longer expose reservation overrides.
        for tool, override in (("firecrawl_scrape", .02), ("firecrawl_search", .03)):
            with self.subTest(tool=tool):
                self.provider.rate = None
                inputs = {"url": "https://example.test/report.pdf"} if tool.endswith("scrape") else {"query": "insurer results"}
                with self.assertRaisesRegex(ValueError, "No whole-call price"):
                    self.tools.lookup([check(tool=tool, inputs=inputs, max_cost_credits=override)])
        self.assertTrue(all(r["operation"] == "describe" for r in self.provider.requests))
        self.assertEqual(budget.load_ledger(self.path)["calls"], {})
        # A larger reservation remains valid for a supported price.
        self.provider.rate = .2
        self.tools.lookup([check(max_cost_credits=.3)])
        charge = next(iter(budget.load_ledger(self.path)["calls"].values()))
        self.assertEqual(float(charge["maximum_credits"]), .3)
        self.assertEqual(float(charge["actual_credits"]), .2)

    def qualifying_signal(self, ref):
        return [{"criterion": "partnership", "signal": "PARTNERSHIP", "status": "pass",
                 "claim": "Fixture partnership reviewed", "evidence": [{"ref": ref,
                     "text": "Fixture company announced the requested partnership.", "event_date": "2026-09-01"}]}] + [
            {"requirement_ref": r["ref"], "claim": "Fixture company matches this filter", "status": "pass",
             "evidence": [{"ref": ref, "text": "Fixture company profile"}]}
            for r in research_tools.request_requirements(self.request) if r["ref"].startswith("icp:")]

    def saved_funding(self, target="example.test"):
        rows = [{"id": "round-c", "name": "Series C - ExamplePay", "stage": "Series C",
                 "announcedOn": "2024-11-28T00:00:00.000Z", "moneyRaised": 45000000}]
        self.provider.raw = {"toolResponse": {"rawV2": {"fundingRounds": rows}},
                             "output_preview": {"kind": "list", "rowCount": len(rows), "preview": rows}}
        return self.lookup(check(target, tool="aviato_get_company_funding_rounds",
            inputs={"website": "https://" + target}))["lookups"][0]["results"][0]["ref"]

    def selected_contact(self, target="example.test", first="Ada", last="Example", position=None, email="ada@example.test", profile_fields=None):
        """A reviewed account and current person, all from local provider fixtures."""
        ref = self.lookup(check(target))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": target, "decision": "qualify_account", "reason": "Verified fit",
            "company": {"ref": ref}, "account_fit": {"ref": ref, "text": "Provides payments infrastructure"},
            "qualification_checks": self.qualifying_signal(ref)}])
        self.provider.raw = {"status": "ok", "element": {"linkedinUrl": "https://www.linkedin.com/in/ada-example/",
            "firstName": first, "lastName": last, "email": email, "currentPosition": [{"companyName": "ExamplePay",
                "companyLinkedinUrl": "https://www.linkedin.com/company/examplepay/", "title": "Head of Payments", **(position or {})}],
            "location": {"parsed": {"countryFull": "Singapore"}}, **(profile_fields or {})}}
        profile = self.lookup(check(target, phase="contact_verification", tool="harvestapi_get_profile",
            inputs={"url": "https://www.linkedin.com/in/ada-example/"}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": target, "decision": "hold_contact", "reason": "Selected current buyer",
            "primary_contact": {"ref": profile, "requested_role": "Head of Payments", "role_match": "exact"}}])
        return profile

    def test_new_custom_criteria_require_explicit_evidence_fields_before_any_call(self):
        self.request["icp"]["custom_criteria"] = ["Current Series A"]
        with self.assertRaisesRegex(ValueError, "required_attributes.*buying_signals"):
            self.start()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".budget.json").exists())
        self.assertEqual(self.provider.requests, [])
        self.request["icp"]["required_attributes"] = self.request["icp"].pop("custom_criteria")
        self.start()
        self.assertIn({"ref": "attribute:0", "label": "Current Series A", "importance": "required"},
                      self.tools.inspect(field="requirements")["requirements"])

    def test_reject_reuses_saved_outside_size_without_extra_judgment_or_spend(self):
        self.request["icp"]["company_size"] = {"min_employees": 201, "max_employees": 10000}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Reviewing fit",
            "company": {"ref": ref, "description": "Existing business description."}}])
        held = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(held["qualification_checks"], [])  # Code does not choose rejection.
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Too small"}])
        row = json.loads(self.path.read_text())["rejected"][0]
        self.assertEqual(row["candidate"], held["candidate"])
        check = row["qualification_checks"][0]
        self.assertEqual((check["criterion"], check["status"], check["importance"]), ("company_size", "fail", "required"))
        self.assertIn("51-200", check["claim"])
        self.assertEqual(check["evidence"][0]["source"]["route_id"], ref.split(":")[0])
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_reject_can_select_new_receipt_above_size_limit(self):
        self.request["icp"]["company_size"] = {"min_employees": 1, "max_employees": 50}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Too large",
            "company": {"ref": ref}}])
        self.assertEqual(len(json.loads(self.path.read_text())["rejected"]), 1)

    def test_matching_overlapping_and_missing_ranges_do_not_prove_rejection(self):
        for label, band, size in [
            ("matching", {"min_employees": 51, "max_employees": 200}, {"start": 51, "end": 200}),
            ("overlapping", {"min_employees": 100, "max_employees": 500}, {"start": 51, "end": 200}),
            ("missing", {"min_employees": 201}, None),
        ]:
            with self.subTest(label=label):
                path = self.path.parent / label / "results.json"
                provider = FixtureProvider()
                provider.raw["element"]["employeeCountRange"] = size
                tools = ResearchTools(path, execute=provider)
                request = copy.deepcopy(self.request)
                request["icp"]["company_size"] = band
                tools.start(request)
                ref = tools.lookup([check()])["lookups"][0]["results"][0]["ref"]
                before = path.read_bytes(), budget.ledger_path(path).read_bytes(), len(provider.requests)
                with self.assertRaisesRegex(ValueError, "requires an evidenced required failure"):
                    tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Size uncertain",
                        "company": {"ref": ref}}])
                self.assertEqual((path.read_bytes(), budget.ledger_path(path).read_bytes(), len(provider.requests)), before)

    def test_size_rejection_requires_matching_saved_receipt(self):
        self.request["icp"]["company_size"] = {"min_employees": 201}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Size review",
            "company": {"ref": ref}}])
        original = json.loads(self.path.read_text())
        for key, value, error in [("employee_range", "1-10", "conflicts with its Harvest receipt"),
                                  ("linkedin_url", "https://www.linkedin.com/company/other/", "conflicts with its Harvest receipt"),
                                  ("employee_range_evidence", {"source": {"route_id": "another-run"}}, "Unknown saved result reference")]:
            with self.subTest(key=key, value=value):
                document = copy.deepcopy(original)
                document["unresolved"][0]["candidate"][key] = value
                self.path.write_text(json.dumps(document))
                before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
                with self.assertRaisesRegex(ValueError, error):
                    self.tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Too small"}])
                self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_size_rejection_keeps_explicit_check_consistency_validation(self):
        self.request["icp"]["company_size"] = {"min_employees": 201}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        with self.assertRaisesRegex(ValueError, "company_size decision contradicts"):
            self.tools.review(companies=[{"target": "example.test", "decision": "reject", "reason": "Review size",
                "company": {"ref": ref}, "qualification_checks": [{"criterion": "company_size", "importance": "required",
                    "status": "pass", "claim": "An explicit contradictory judgment", "evidence": [{"ref": ref}]}]}])

    def test_company_review_reuses_unique_domain_matched_getter_and_closes_it(self):
        self.request["icp"]["company_size"] = {"min_employees": 51, "max_employees": 200}
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Verified fit",
            "company": {"description": "Reviewed description."}, "qualification_checks": self.qualifying_signal(ref)}])
        document = json.loads(self.path.read_text())
        company = document["unresolved"][0]["candidate"]
        self.assertEqual(company["employee_range"], "51-200")
        self.assertEqual(company["description"], "Reviewed description.")
        self.assertEqual(company["employee_range_evidence"]["source"]["route_id"], ref.split(":")[0])
        frontier = next(x for x in document["stop_audit"]["route_frontier"] if x["route_id"] == ref.split(":")[0])
        self.assertEqual(frontier["state"], "exhausted")
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_company_reuse_skips_wrong_domain_and_coalesces_identical_getters(self):
        self.start()
        self.provider.raw["element"]["website"] = "https://wrong.test"
        wrong = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.provider.raw["element"]["website"] = "https://example.test"
        for suffix in ("correct", "duplicate"):
            correct = self.lookup(check(approach="receipt-fixture-" + suffix, inputs={"url": "https://www.linkedin.com/company/" + suffix}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Research remaining facts"}])
        company = json.loads(self.path.read_text())["unresolved"][0]["candidate"]
        self.assertEqual(company["employee_range_evidence"]["source"]["route_id"], correct.split(":")[0])
        self.assertNotEqual(company["employee_range_evidence"]["source"]["route_id"], wrong.split(":")[0])

    def test_company_review_rejects_wrong_domain_getter_evidence_for_every_decision(self):
        self.start()
        self.provider.raw["element"]["website"] = "https://wrong.test"
        wrong = self.lookup()["lookups"][0]["results"][0]["ref"]
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for decision, status in (("qualify_account", "pass"), ("reject", "fail")):
            with self.subTest(decision=decision), self.assertRaisesRegex(ValueError, "differs from saved ref"):
                self.tools.review(companies=[{"target": "example.test", "decision": decision,
                    "reason": "Review selected getter evidence", "qualification_checks": [{
                        "requirement_ref": "icp:industries", "status": status,
                        "claim": "The selected company record supports this decision.",
                        "evidence": [{"ref": wrong}]}]}])
            self.assertEqual(
                (self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)),
                before,
            )

    def test_company_review_rejects_unbound_domainless_getter_evidence_for_every_decision(self):
        self.start()
        self.provider.raw["element"].update(
            name="ExamplePay", website=None, linkedinUrl="https://www.linkedin.com/company/example/")
        unrelated = self.lookup()["lookups"][0]["results"][0]["ref"]
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "has no company domain"):
            self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
                "reason": "Select ambiguous same-name facts", "company": {"ref": unrelated}}])
        self.assertEqual(
            (self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)),
            before,
        )
        for decision, status in (("qualify_account", "pass"), ("reject", "fail")):
            with self.subTest(decision=decision), self.assertRaisesRegex(ValueError, "has no company domain"):
                self.tools.review(companies=[{"target": "example.test", "decision": decision,
                    "reason": "Review ambiguous same-name evidence", "qualification_checks": [{
                        "requirement_ref": "icp:industries", "status": status,
                        "claim": "The domainless company record supports this decision.",
                        "evidence": [{"ref": unrelated}]}]}])
            self.assertEqual(
                (self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)),
                before,
            )

    def test_company_review_keeps_domainless_getter_after_verified_linkedin_binding(self):
        self.start()
        verified = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Save domain-matched identity", "company": {"ref": verified}}])
        self.provider.raw["element"]["website"] = None
        bound = self.lookup(check(approach="refresh-bound-company",
            inputs={"url": "https://www.linkedin.com/company/examplepay/?refresh=1"}))["lookups"][0]["results"][0]["ref"]
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Review the already bound company", "company": {"ref": bound}}])
        company = json.loads(self.path.read_text())["unresolved"][0]["candidate"]
        self.assertEqual(company["linkedin_url"], "https://www.linkedin.com/company/examplepay/")
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        second = self.lookup(check(approach="second-refresh-bound-company",
            inputs={"url": "https://www.linkedin.com/company/examplepay/?refresh=2"}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Reuse the original verified binding", "company": {"ref": second}}])

        self.provider.raw["element"]["linkedinUrl"] = "https://www.linkedin.com/company/unrelated/"
        unrelated = self.lookup(check(approach="unrelated-domainless-refresh",
            inputs={"url": "https://www.linkedin.com/company/unrelated/"}))["lookups"][0]["results"][0]["ref"]
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "is not bound"):
            self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
                "reason": "Reject unrelated identity", "company": {"ref": unrelated}}])
        self.assertEqual(
            (self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_company_review_keeps_matching_getter_and_press_negative_evidence(self):
        for source_kind in ("getter", "press"):
            with self.subTest(source_kind=source_kind):
                directory = tempfile.TemporaryDirectory()
                self.addCleanup(directory.cleanup)
                path = Path(directory.name) / "run/results.json"
                provider = FixtureProvider()
                tools = ResearchTools(path, execute=provider)
                tools.call("tyche_start", {"request": copy.deepcopy(self.request)})
                if source_kind == "getter":
                    reference = tools.call("tyche_lookup", {"checks": [check()]})["lookups"][0]["results"][0]["ref"]
                else:
                    reference = captured_page(tools, provider, text="Example does not provide the requested product.")
                calls = len(provider.requests)
                tools.review(companies=[{"target": "example.test", "decision": "reject",
                    "reason": "Saved negative evidence establishes the mismatch.", "qualification_checks": [{
                        "requirement_ref": "icp:industries", "status": "fail",
                        "claim": "The saved source shows a different business category.",
                        "evidence": [{"ref": reference}]}]}])
                saved = json.loads(path.read_text())
                self.assertEqual(saved["rejected"][0]["candidate"]["domain"], "example.test")
                self.assertEqual(saved["rejected"][0]["qualification_checks"][0]["status"], "fail")
                self.assertEqual(len(provider.requests), calls)

    def test_conflicting_company_getters_require_selection_and_preserve_it(self):
        self.start()
        first = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.provider.raw["element"]["linkedinUrl"] = "https://www.linkedin.com/company/another-company/"
        second = self.lookup(check(inputs={"url": "https://www.linkedin.com/company/another-company/"}))["lookups"][0]["results"][0]["ref"]
        review = {"target": "example.test", "decision": "accept", "reason": "Selected company",
                  "qualification_checks": self.qualifying_signal(first)}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "saved company getters disagree") as error:
            self.tools.review(companies=[review])
        self.assertIn(first, str(error.exception))
        self.assertIn(second, str(error.exception))
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.tools.review(companies=[{**review, "decision": "hold_account", "company": {"ref": first}}])
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Continue company review",
                                     "company": {"description": "Updated prose."}}])
        company = json.loads(self.path.read_text())["unresolved"][0]["candidate"]
        self.assertEqual(company["linkedin_url"], "https://www.linkedin.com/company/examplepay/")


    def test_company_selection_without_size_is_not_replaced_by_later_getter(self):
        self.start()
        self.provider.raw["element"]["employeeCountRange"] = None
        first = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Selected identity",
            "company": {"ref": first}}])
        self.provider.raw["element"]["employeeCountRange"] = {"start": 51, "end": 200}
        self.lookup(check(inputs={"url": "https://www.linkedin.com/company/later-observation/"}))
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Review other facts",
            "company": {"description": "Preserved identity."}}])
        company = json.loads(self.path.read_text())["unresolved"][0]["candidate"]
        self.assertEqual(company["employee_range_evidence"]["source"]["route_id"], first.split(":")[0])
        self.assertIsNone(company["employee_range"])

    def test_legacy_custom_criteria_resume_without_rewriting_request_or_budget(self):
        document, options = research_tools.research_input.start_document(self.path, {"request": self.request})
        document["request"]["icp"]["custom_criteria"] = ["Original legacy condition"]
        research_tools.runner.refresh(document)
        self.path.parent.mkdir(parents=True)
        budget.create_run(self.path, document, **options)
        self.lookup()  # A saved paid receipt must also survive the legacy resume.
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        self.tools.start(document["request"])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)
        self.assertEqual(len(self.provider.requests), calls)

    def test_original_request_is_bound_once_and_available_on_resume(self):
        source = self.path.parent.parent / "original.txt"
        source.write_text("Find multi-site businesses; hiring is preferred.")
        self.tools.environment["TYCHE_REQUEST_FILE"] = str(source)
        self.assertEqual(self.start()["request"]["original_text"], source.read_text())
        before = self.path.read_bytes()
        source.write_text("Changed outside the saved run")
        self.start()
        self.assertEqual(before, self.path.read_bytes())
        self.assertIn("multi-site", self.tools.inspect()["request"]["original_text"])

    def test_target_offering_context_is_consistent_at_start_company_and_final_review(self):
        self.request['product_service'] = {'description': 'A platform that governs business data.', 'perspective': 'target'}
        started = self.start()
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        company = self.tools.inspect(target='example.test', field='evidence_review')
        final = self.tools.review_delivery(json.loads(self.path.read_text()))
        for packet in (started, company, final):
            guidance = packet['writing_requirements']
            self.assertEqual(guidance['product_service'], self.request['product_service'])
            self.assertIn("target company's own offering", guidance['offering_context'])
            self.assertIn('Do not invent an external seller', guidance['offering_context'])
            self.assertEqual(guidance['intent_details'], research_tools.WRITING_REQUIREMENTS['intent_details'])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_seller_and_missing_offering_context_do_not_inherit_target_interpretation(self):
        offering = {'description': 'Specialized staffing services.', 'perspective': 'seller'}
        guidance = research_tools.writing_requirements({'product_service': offering})
        self.assertIn("user's offering", guidance['offering_context'])
        self.assertIn('do not assert confirmed demand', guidance['offering_context'])
        guidance['product_service']['perspective'] = 'target'
        self.assertEqual(offering['perspective'], 'seller')
        for request in ({}, {'product_service': None}):
            unknown = research_tools.writing_requirements(request)
            self.assertEqual(unknown['product_service'], {})
            self.assertIn('do not invent an offering', unknown['offering_context'])

    def test_research_review_handoff_preserves_state_and_cannot_approve(self):
        self.start()
        document = json.loads(self.path.read_text())
        ref = runner.review_fingerprint(document)
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.environment['TYCHE_FINALIZATION_ONLY'] = '0'
        for supplied in (None, ref):
            result = self.tools.review_delivery(document, supplied)
            self.assertEqual(result['status'], 'review_handoff')
            self.assertFalse(result['delivery_allowed'])
            self.assertNotIn('review_ref', result)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        final = ResearchTools(self.path, execute=self.provider, environment={'TYCHE_FINALIZATION_ONLY': '1'})
        packet = final.review_delivery(document)
        self.assertEqual(packet['status'], 'review_required')
        self.assertEqual(packet['review_ref'], ref)
        self.assertIn('reopen the exact saved source URL once', packet['instructions'])
        self.assertIn('preserve the captured qualification ref', packet['instructions'])
        self.assertIn('No new searches, new source URLs or provider lookups', packet['instructions'])
        self.assertIsNone(final.review_delivery(document, ref, []))
        self.assertEqual(json.loads(self.path.read_text())['final_review']['review_ref'], ref)
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before[1:])


    def test_review_shows_saved_source_not_only_the_rewritten_claim(self):
        self.start()
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Review source strength",
            "account_fit": {"ref": "web:0:0"},
            "qualification_checks": [{"criterion": "hiring", "importance": "preferred", "status": "unknown",
                "claim": "Persistence is unverified", "evidence": [{"ref": "web:0:0", "text": "Unproven persistence claim"}]}]}],
            web=[{"target": "example.test", "purpose": "Inspect hiring evidence", "query": "careers",
                "operation": "open", "response": {"status": "ok", "results": [
                    {"url": "https://example.test/jobs", "text": "One current vacancy. No posting date is shown."}]}}])
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        proof = next(iter(packet["sources"].values()))
        self.assertEqual(proof["text"], "One current vacancy. No posting date is shown.")
        self.assertEqual(proof["capture_method"], "agent_recorded_web")
        self.assertIsNone(proof["date"])
        self.assertNotIn("text", packet["company"]["account_fit"])
        self.assertEqual(len(packet["company"]["account_fit"]["source_refs"]), 1)
        self.assertEqual(packet["company"]["qualification_checks"][0]["evidence"][0]["text"], "Unproven persistence claim")
        self.assertEqual(json.loads(self.path.read_text())["unresolved"][0]["qualification_checks"][0]["status"], "unknown")

    def test_review_identifies_adapter_saved_provider_text_without_dispatch(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Review source",
            "account_fit": {"ref": ref, "text": "Research interpretation"}}])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        proof = next(iter(packet["sources"].values()))
        self.assertEqual(proof["capture_method"], "provider_response")
        self.assertNotEqual(proof["text"], "Research interpretation")
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_selected_point_lookups_close_without_closing_searches(self):
        self.start()
        company = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Selected company point lookup", "company": {"ref": company}}])
        search = self.lookup(check(tool="fixture_search", inputs={"query": "more companies"}))["lookups"][0]
        frontier = {r["route_id"]: r for r in json.loads(self.path.read_text())["stop_audit"]["route_frontier"]}
        self.assertEqual(frontier[company.split(":")[0]]["state"], "exhausted")
        self.assertEqual(frontier[search["route"]]["state"], "continuable")

    def test_review_labels_search_snippets_and_reuses_identical_excerpts(self):
        self.start()
        snippet = "ExamplePay announced a planned capacity expansion."
        self.provider.raw = {"status": "ok", "results": [{"url": "https://example.test/announcement", "snippet": snippet}]}
        ref = self.lookup(check(tool="fixture_search", inputs={"query": "expansion"}))["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Read full source next",
            "account_fit": {"ref": ref}}])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        proof = packet["sources"][ref]
        self.assertEqual(proof["capture_method"], "provider_response")
        self.assertEqual(proof["content_kind"], "search_excerpt")
        self.assertEqual(proof["text"], snippet)
        self.assertEqual(packet["company"]["account_fit"]["source_refs"], [ref])
        self.assertNotIn("text", packet["company"]["account_fit"])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_review_keeps_distinct_evidence_even_when_compact_prefixes_match(self):
        self.start()
        prefix = "Current source context. " * 100
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Resolve source conflict",
            "account_fit": {"ref": "web:0:0", "text": prefix + "The expansion is completed."}}],
            web=[{"target": "example.test", "purpose": "Read source", "query": "https://example.test/announcement",
                "operation": "open", "response": {"status": "ok", "results": [
                    {"url": "https://example.test/announcement", "text": prefix + "The expansion is planned."}]}}])
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        self.assertIn("text", packet["company"]["account_fit"])
        self.assertEqual(next(iter(packet["sources"].values()))["content_kind"], "unverified")




    def test_review_preserves_independent_legacy_signal_beside_other_checks(self):
        row = {"signal_evidence": {"signal": "Expansion", "evidence_text": "Opened a new location"},
               "qualification_checks": [{"signal": "Hiring", "status": "unknown", "evidence": []}]}
        view = self.tools._company_review(row, {})
        self.assertEqual(view["signal_evidence"]["signal"], "Expansion")
        self.assertEqual(view["signal_evidence"]["text"], "Opened a new location")




    def profile_email_result(self, fields, **response_fields):
        self.start()
        ref = self.selected_contact(email=None)
        self.provider.raw["element"].update(fields)
        self.provider.rate = .14
        def execute(request, capture):
            body, code = self.provider(request, capture)
            if request.get("operation") == "execute":
                body.update(response_fields)
            return body, code
        self.tools.execute = execute
        result = self.lookup(check(tool="harvestapi_get_profile", contact_ref=ref,
                                   inputs={"findEmail": "true"}))
        rid = result["lookups"][0]["route"]
        doc = json.loads(self.path.read_text())
        frontier = next(r for r in doc["stop_audit"]["route_frontier"] if r["route_id"] == rid)
        return rid, doc, frontier






    def test_malformed_saved_requirements_return_an_actionable_error(self):
        self.start()
        document = json.loads(self.path.read_text())
        for fields in ({"icp": None}, {"buying_signals": [None]}, {"buying_signals": [{}]},
                       {"icp": {"required_attributes": "not a list"}}):
            damaged = copy.deepcopy(document)
            damaged["request"].update(fields)
            with self.subTest(fields=fields), patch.object(self.tools, "_document", return_value=damaged):
                with self.assertRaisesRegex(ValueError, "Restore the original saved request"):
                    self.tools.call("tyche_inspect", {"field": "requirements"})

    def test_unknown_reference_and_cost_inspection_are_actionable(self):
        self.start()
        current = self.lookup()["lookups"][0]["route"]
        with self.assertRaisesRegex(ValueError, current):
            self.tools.inspect(ref="mistyped-reference:0")
        self.assertIn("costs", self.tools.inspect(field="costs"))

    def test_start_without_age_window_preserves_unspecified_policy_on_resume(self):
        self.request.pop("time_window")
        for signal in self.request["buying_signals"]:
            signal.pop("max_age_days", None)
        self.start()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["request"]["time_window"], {})
        self.lookup()
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.start()
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        changed = copy.deepcopy(self.request)
        changed["time_window"] = {"max_age_days": 30}
        with self.assertRaises(ValueError):
            self.tools.call("tyche_start", {"request": changed})
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)







    def test_discovery_attribution_preserves_selected_index_and_report_link(self):
        self.start()
        company = copy.deepcopy(self.provider.raw['element'])
        self.provider.raw = {'results': [{'name': 'Unrelated', 'website': 'https://unrelated.test'}, company]}
        discovery = self.lookup(check('discovery', phase='account_discovery', tool='fixture_company_search',
            inputs={'query': 'payments companies'}))['lookups'][0]['results'][1]['ref']
        self.provider.raw = {'status': 'ok', 'element': company}
        verified = self.lookup(check())['lookups'][0]['results'][0]['ref']
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_account', 'reason': 'Selected account',
            'company': {'ref': verified, 'discovery_source': {'ref': discovery}}}])
        row = json.loads(self.path.read_text())['unresolved'][0]
        saved = row['candidate']['discovery_source']
        self.assertEqual(saved['source']['tool'], 'fixture_company_search')
        self.assertEqual(saved['source']['result_index'], 1)
        self.assertEqual(saved['source']['route_id'], discovery.split(':')[0])
        self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_account', 'reason': 'Keep researching',
                                    'company': {'description': 'Selected business.'}}])
        self.assertEqual(json.loads(self.path.read_text())['unresolved'][0]['candidate']['discovery_source'], saved)
        with self.assertRaisesRegex(ValueError, 'account-discovery'):
            self.tools.review(companies=[{'target': 'example.test', 'decision': 'hold_account', 'reason': 'Wrong stage',
                'company': {'discovery_source': {'ref': verified}}}])
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'scripts'))
        from run_costs import write_research_report, report as cost_report
        report = {'request': {'target_count': 1}, 'accepted': [{'company': row['candidate']}]}
        write_research_report(self.path.parent, report, cost_report(report, []), 'Fixture report')
        text = (self.path.parent / 'report.md').read_text()
        self.assertIn('deepline/fixture_company_search', text)
        self.assertIn('receipts/' + discovery.split(':')[0] + '.json', text)










    def test_source_group_saves_explicit_decisions_without_closing_unreviewed_work(self):
        self.start()
        results = [self.lookup(check(tool="fixture-search", inputs={"query": str(i)}, approach=f"independent source {i}"))["lookups"][0] for i in range(3)]
        refs = [r["route"] for r in results[:2]]
        self.tools.call("tyche_review", {"sources": [{"refs": refs, "state": "exhausted", "reason": "Read both; no further page"}]})
        pending = self.tools.inspect(field="pending_sources")["items"]
        self.assertEqual([r["ref"] for r in pending], [results[2]["route"]])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.tools.review(sources=[{"ref": refs[0], "refs": refs, "state": "exhausted", "reason": "Invalid selection"}])
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, r"input.sources\[0\].refs\[1\].*Saved choices"):
            self.tools.call("tyche_review", {"sources": [{"refs": [results[2]["route"], "missing-lookup"],
                "state": "exhausted", "reason": "Unknown receipt must not partially close the group"}]})
        self.assertEqual(before, self.path.read_bytes())

    def test_selected_open_page_closes_once_but_search_results_stay_open(self):
        self.start()
        for operation in ("open", "search_query"):
            result = self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Reviewed fit only",
                "account_fit": {"ref": "web:0:0"}}], web=[{"target": "example.test", "purpose": "Read source",
                "query": operation, "operation": operation, "response": {"status": "ok", "results": [
                    {"url": "https://example.test/about", "text": "Provides payments infrastructure"}]}}])
            rid = result["web_references"]["web:0"]
            pending = self.tools.inspect(field="pending_sources")["items"]
            self.assertEqual(rid in [r["ref"] for r in pending], operation == "search_query")



    def test_source_review_changes_invalidate_approval_but_cost_updates_do_not(self):
        doc = {"request": {}, "accepted": [], "unresolved": [], "rejected": [],
               "routes": [{"cost_credits": None}], "stop_audit": {"route_frontier": [
                   {"route_id": "source-1", "state": "exhausted", "reason": "Reviewed"}]}}
        before = runner.review_fingerprint(doc)
        doc["routes"][0]["cost_credits"] = 1
        self.assertEqual(runner.review_fingerprint(doc), before)
        doc["stop_audit"]["route_frontier"][0]["reason"] = "Reopened after a contradictory source"
        self.assertNotEqual(runner.review_fingerprint(doc), before)

    def test_missing_source_excerpt_has_a_specific_diagnostic(self):
        from validate_run import source_evidence_error
        evidence = {"url": "https://example.test/news", "date": "2026-06-01", "date_basis": "published",
                    "source": {"provider": "public_web", "operation": "open", "route_id": "source-1"}}
        self.assertEqual(source_evidence_error(evidence, "qualification"),
                         "qualification requires dated source evidence; missing or invalid: text (supporting source excerpt)")



    def test_completion_advice_exposes_missing_fit_source_before_acceptance(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        reviewed = {"target": "example.test", "decision": "qualify_account", "reason": "Fit checks reviewed",
                    "company": {"ref": ref}, "qualification_checks": self.qualifying_signal(ref)}
        result = self.tools.review(companies=[reviewed])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        selected = self.tools.call("tyche_inspect", {"field": "completion_candidates", "limit": 1})
        self.assertEqual(selected["total"], 1)
        self.assertIsNone(selected["next_offset"])
        self.assertEqual(selected["value"], self.tools.inspect()["completion_candidates"])
        for packet in (result["progress"], self.tools.inspect(), {"completion_candidates": selected["value"]}):
            due = packet["completion_candidates"][0]
            self.assertTrue(any("account_fit" in message and "source evidence" in message for message in due["missing"]))
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        repaired = self.tools.review(companies=[{"target": "example.test", "decision": "hold_contact",
            "reason": "Selected the existing fit source", "account_fit": {"ref": ref, "text": "Provides payments infrastructure"}}])
        self.assertFalse(any("account_fit" in message for message in repaired["progress"]["completion_candidates"][0]["missing"]))
        self.assertEqual(len(self.provider.requests), before[2])
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before[1])


    def test_banner_single_vacancy_review_keeps_rapid_hiring_unresolved(self):
        self.request["buying_signals"] = [{"kind": "RAPID_HIRING", "query": "Rapid clinical hiring"}]
        self.start()
        finding = {"target": "banner.example", "decision": "hold_account", "reason": "A single vacancy does not establish rapid hiring",
            "qualification_checks": [{"criterion": "rapid hiring", "importance": "required", "status": "unknown",
                "claim": "One current nursing vacancy is insufficient to infer rapid hiring", "signal": "RAPID_HIRING",
                "evidence": [{"ref": "web:0:0"}]}]}
        result = self.tools.review(companies=[finding], web=[{"target": "banner.example", "purpose": "Review hiring strength",
            "query": "https://banner.example/careers", "operation": "open", "response": {"status": "ok", "results": [
                {"url": "https://banner.example/careers", "text": "Banner Health lists one current nursing vacancy."}]}}])
        self.assertEqual(result["progress"]["summary"]["accepted_companies"], 0)
        with self.assertRaisesRegex(ValueError, "required signal coverage"):
            self.tools.review(companies=[{"target": "banner.example", "decision": "accept", "reason": "Try to accept with the unresolved required claim"}])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["unresolved"][0]["qualification_checks"][0]["status"], "unknown")
        self.assertFalse(saved["rejected"])






    def test_finish_without_completion_returns_actionable_work_without_export(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Company review remains incomplete", "company": {"ref": ref}}])
        before = budget.ledger_path(self.path).read_bytes()
        with patch("research_tools.subprocess.run") as exported:
            result = self.tools.finish()
        self.assertEqual(result["status"], "needs_research")
        self.assertEqual(result["progress"]["completion_candidates"][0]["target"], "example.test")
        self.assertFalse(result["delivery_allowed"])
        exported.assert_not_called()
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())

    def test_incomplete_finish_exposes_saved_discovery_reviews_without_more_spending(self):
        self.start()
        lookup = self.lookup(check(target="discovery", phase="account_discovery",
            tool="fixture-search", inputs={"query": "companies"}))["lookups"][0]
        before = budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        with patch("research_tools.subprocess.run") as exported:
            result = self.tools.finish()
        self.assertEqual(result["status"], "needs_research")
        self.assertFalse(result["delivery_allowed"])
        self.assertEqual(result["progress"]["review_due"]["count"], 1)
        self.assertEqual(result["progress"]["review_due"]["sources"], result["pending_sources"])
        self.assertEqual(self.tools.inspect(field="pending_sources")["items"], result["pending_sources"])
        self.assertEqual([s["ref"] for s in result["pending_sources"]], [lookup["route"]])
        self.assertEqual(result["pending_sources"][0]["target"], "discovery")
        self.tools.review(sources=[{"ref": lookup["route"], "state": "exhausted",
            "reason": "Fixture results reviewed; no further page or qualifying evidence."}])
        self.assertNotIn(lookup["route"], [s["ref"] for s in self.tools.finish().get("pending_sources", [])])
        self.assertEqual(self.tools.inspect()["review_due"]["count"], 0)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(before, budget.ledger_path(self.path).read_bytes())
        exported.assert_not_called()

    def test_incomplete_finish_without_candidates_preserves_continue_and_accounting(self):
        self.start()
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        calls = list(self.provider.requests)
        with patch("research_tools.subprocess.run") as exported:
            for _ in range(2):
                result = self.tools.finish()
                self.assertEqual(result["status"], "needs_research")
                self.assertEqual(result["progress"]["stop"], "continue")
                self.assertFalse(result["progress"]["completion_candidates"])
                self.assertFalse(result["pending_sources"])
                self.assertFalse(result["delivery_allowed"])
        exported.assert_not_called()
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)
        self.assertEqual(self.provider.requests, calls)

    def test_source_reminders_ignore_closed_aliases_and_page_all_open_sources(self):
        self.start()
        first = self.lookup(check(target="old-alias"))["lookups"][0]["route"]
        self.tools.review(sources=[{"ref": first, "state": "exhausted", "reason": "Wrong identity; source reviewed"}])
        for i in range(4):
            self.lookup(check(target="discovery", phase="account_discovery", approach=f"source family {i}",
                              tool="fixture-search", inputs={"query": str(i)}))
        progress = self.tools.inspect()["review_due"]
        self.assertEqual((progress["count"], progress["scopes"]), (4, ["discovery"]))
        self.assertEqual(len(progress["sources"]), 3)
        a = self.tools.inspect(field="pending_sources", limit=2)
        b = self.tools.inspect(field="pending_sources", offset=a["next_offset"], limit=2)
        self.assertIsNone(b["next_offset"])
        self.assertEqual(a["items"] + b["items"], self.tools.finish()["pending_sources"])

    def test_strategy_feedback_requires_review_and_allows_another_method(self):
        self.start()
        lookups = [self.lookup(check(tool="fixture-search", inputs={"query": f"variant {i}"},
            approach=f"search wording {i}"))["lookups"][0] for i in range(2)]
        self.assertEqual(self.tools.inspect()["strategy_review"]["count"], 0)
        result = self.tools.review(sources=[{"ref": r["route"], "state": "exhausted",
            "reason": "Read the results; they do not resolve the requested fact."} for r in lookups])
        feedback = result["progress"]["strategy_review"]
        self.assertEqual(feedback["count"], 1)
        self.assertEqual(feedback["items"][0]["sources"], [r["route"] for r in lookups])
        self.assertEqual(feedback["items"][0]["tools"], ["fixture-search"])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        self.assertEqual(self.tools.inspect(field="strategy_review")["value"], feedback)
        self.assertEqual(before, (self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)))
        # Advisory feedback does not add a dispatch veto or change the ledger.
        self.lookup(check(tool="other-search", inputs={"query": "different evidence source"}, approach="different source"))

    def test_strategy_feedback_clears_when_review_adds_evidence(self):
        self.start()
        lookups = [self.lookup(check(inputs={"url": f"https://example.test/source-{i}"},
            approach=f"company lookup {i}"))["lookups"][0] for i in range(2)]
        self.tools.review(sources=[{"ref": r["route"], "state": "exhausted", "reason": "Reviewed source"}
                                   for r in lookups])
        self.assertEqual(self.tools.inspect()["strategy_review"]["count"], 1)
        ref = lookups[0]["results"][0]["ref"]
        self.tools.review(companies=[{"target": "example.test", "decision": "qualify_account", "reason": "Fit verified",
            "company": {"ref": ref}, "account_fit": {"ref": ref, "text": "Provides payments infrastructure"},
            "qualification_checks": self.qualifying_signal(ref)}])
        self.assertEqual(self.tools.inspect()["strategy_review"]["count"], 0)

    def test_strategy_feedback_stays_scoped_and_ignores_pending_or_failed_work(self):
        document = {"request": {"target_count": 5}, "accepted": [], "unresolved": [], "rejected": [],
                    "routes": [], "stop_audit": {"route_frontier": []}}
        def attempt(scope, phase, *, status="no_results", reviewed=True, catalog=False):
            rid = str(len(document["routes"]))
            document["routes"].append(dict(route_id=rid, scope=scope, phase=phase, tool="fixture-search",
                entity_type="tool_catalog" if catalog else "research", provider_status=status,
                approach="query " + rid, progress_before=[]))
            document["stop_audit"]["route_frontier"].append(dict(route_id=rid,
                state="exhausted" if reviewed else "continuable", reason="Reviewed response"))
        attempt("one.test", "account_verification")
        attempt("two.test", "account_verification")
        attempt("one.test", "account_discovery")
        attempt("one.test", "account_verification", catalog=True)
        attempt("one.test", "account_verification", status="timeout")
        self.assertEqual(runner.strategy_reminder(document)["count"], 0)
        attempt("one.test", "account_verification")
        self.assertEqual(runner.strategy_reminder(document)["count"], 1)
        attempt("one.test", "account_verification", reviewed=False)
        self.assertEqual(runner.strategy_reminder(document)["count"], 0)
        for phase in ("contact_verification", "email_validation"):
            attempt("two.test", phase)
            attempt("two.test", phase)
        self.assertEqual(runner.strategy_reminder(document)["count"], 0)







    def test_start_resume_and_cached_describe_need_no_manual_bookkeeping(self):
        self.start()
        result = self.lookup()
        ref = result["lookups"][0]["results"][0]["ref"]
        self.assertEqual(self.tools.inspect(ref=ref)["facts"]["employee_range"], "51-200")
        self.lookup(check("another.test"))
        self.assertEqual(len([r for r in self.provider.requests if r["operation"] == "describe"]), 1)
        ledger = budget.ledger_path(self.path).read_bytes()
        self.start()
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.lookup()
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])


    def test_free_startup_prerequisites_overlap_and_expose_reusable_descriptions(self):
        result = self.start()
        expected = ["harvestapi_get_company"]
        self.assertEqual(result["cached_descriptions"], expected)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        self.assertEqual(float(budget.load_ledger(self.path)["verification_reserve_credits"]), 0)
        self.tools.execute = lambda *args: self.fail("Saved contracts must not be requested again")
        for tool in expected:
            self.tools.inspect(tool=tool)
        self.assertEqual(self.start()["cached_descriptions"], expected)
        self.assertFalse(budget.load_ledger(self.path)["calls"])

    def test_native_start_rejects_removed_reservation_parameter(self):
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.start(verification_reserve_credits=.4)
        self.assertFalse(self.provider.requests)
        self.assertFalse(self.path.exists())

    def test_transient_startup_timeouts_recover_once_without_agent_or_paid_work(self):
        attempts = {}
        def catalog(request, capture):
            self.assertEqual(request["operation"], "describe")
            tool = request["tool"]
            attempts[tool] = attempts.get(tool, 0) + 1
            if attempts[tool] == 1:
                capture({"timed_out": True, "body": "", "stderr": ""})
                return {"provider": "deepline", "operation": "describe", "tool": tool,
                        "status": "timeout", "results": []}, 2
            return self.provider(request, capture)
        self.tools.execute = catalog
        result = self.start()
        self.assertEqual(sorted(attempts), result["cached_descriptions"])
        self.assertEqual(list(attempts.values()), [2])
        started = json.loads(self.path.read_text())["stop_check"]["started_at"]
        for prefix in ("company",):
            archived = list(self.path.parent.glob(prefix + "-tool-*.json"))
            self.assertEqual(len(archived), 1)
            failed = json.loads(archived[0].read_text())
            self.assertEqual(failed["status"], "timeout")
            self.assertTrue(failed["provider_response"]["timed_out"])
            self.assertEqual(failed["started_at"], started)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        self.tools.execute = lambda *args: self.fail("Successful prerequisites should be reused")
        self.start()

    def test_startup_retry_is_bounded_and_does_not_retry_access_or_schema_failures(self):
        for status, count in (("timeout", 2), ("provider_error", 2), ("auth_failed", 1),
                              ("quota_exceeded", 1), ("rate_limited", 1), ("schema_error", 1)):
            with self.subTest(status=status):
                calls = []
                def catalog(request, capture):
                    calls.append(request)
                    return {"provider": "deepline", "operation": "describe", "tool": request["tool"],
                            "status": status, "results": []}, 2
                self.tools.execute = catalog
                with self.assertRaises(research_tools.OperationalBlock) as error:
                    self.tools._startup_contract("zerobounce_validate", status + ".json", "2026-09-01T00:00:00+00:00")
                self.assertIn(status, str(error.exception))
                self.assertEqual(len(calls), count)
                self.assertTrue(all(c["operation"] == "describe" for c in calls))
                self.assertFalse(self.path.exists())
                self.assertFalse(self.path.with_name(self.path.name + ".budget.json").exists())

    def test_unpriced_required_tool_is_available_without_a_price_forecast(self):
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request.get("tool") == "harvestapi_get_company" and request["operation"] == "describe":
                body["results"][0]["pricing"] = {"unit": "usage", "creditsPerUnit": None}
            return body, code
        self.tools.execute = catalog
        with patch("provider_pricing.profile_price", side_effect=AssertionError("No price forecast")):
            result = self.start()
            self.assertIn("harvestapi_get_company", result["cached_descriptions"])
            self.assertFalse(budget.load_ledger(self.path)["calls"])
            before = len(self.provider.requests)
            self.tools.inspect(tool="harvestapi_get_company")
            self.start()
            self.assertEqual(len(self.provider.requests), before)
            self.lookup()
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_unavailable_required_company_tool_stops_before_research(self):
        def unavailable(request, capture):
            body, code = self.provider(request, capture)
            body["results"][0]["connected"] = False
            return body, code
        self.tools.execute = unavailable
        result = self.start()
        self.assertEqual(result["status"], "operationally_blocked")
        self.assertIn("required tool is unavailable", result["reason"])
        self.assertFalse(self.path.exists())
        self.assertTrue(all(r["operation"] == "describe" for r in self.provider.requests))





    def test_required_auth_failure_blocks_discovery_without_rejecting_companies(self):
        self.start()
        self.provider.raw = {"error": "unauthorized API key"}
        result = self.lookup()
        self.assertEqual(result["status"], "operationally_blocked")
        self.assertIn("auth_failed", result["reason"])
        self.assertEqual(json.loads(self.path.read_text())["rejected"], [])
        calls = len(self.provider.requests)
        self.assertEqual(self.lookup(check("second.test"))["status"], "operationally_blocked")
        self.assertEqual(len(self.provider.requests), calls)
        ledger = budget.ledger_path(self.path).read_bytes()
        self.tools.inspect(tool="harvestapi_get_company", refresh=True)
        self.assertIsNone(self.tools.inspect()["operational_block"])
        self.assertEqual(json.loads((self.path.parent / "operational-status.json").read_text())["status"], "ready")
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.lookup()

    def test_no_result_is_a_research_gap_not_an_operational_block(self):
        self.start()
        self.provider.raw = {"status": "completed", "toolResponse": {
            "rawV2": {"error": None, "status": 200, "element": None}}}
        result = self.lookup()
        self.assertNotIn("status", result)
        self.assertIsNone(result["progress"]["operational_block"])
        route = result["lookups"][0]["route"]
        saved = self.tools._receipt(route)["result"]
        self.assertEqual(saved["status"], "no_results")
        self.assertEqual(float(budget.load_ledger(self.path)["calls"][route]["actual_credits"]), self.provider.rate)

    def test_complete_failed_response_does_not_request_receipt_recovery(self):
        self.start()
        self.provider.raw = {"unrecognized_payload": True}
        route = self.lookup()["lookups"][0]["route"]
        before = budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "complete.*schema_error") as error:
            self.tools.inspect(ref=route + ":0")
        self.assertIn(route, str(error.exception))
        self.assertNotIn("recover its receipt", str(error.exception))
        self.assertEqual((budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_launcher_clock_includes_setup_and_is_preserved_on_resume(self):
        from datetime import datetime, timedelta, timezone
        started = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        with patch.dict(os.environ, {"TYCHE_RUN_STARTED_AT": started}):
            self.start()
        self.assertEqual(json.loads(self.path.read_text())["stop_check"]["started_at"], started)
        self.assertGreaterEqual(self.tools.inspect()["elapsed_seconds"], 90)
        with patch.dict(os.environ, {"TYCHE_RUN_STARTED_AT": datetime.now(timezone.utc).isoformat()}):
            self.start()
        self.assertEqual(json.loads(self.path.read_text())["stop_check"]["started_at"], started)

    def test_observed_cap_blocks_next_dispatch_without_protected_reserve(self):
        self.start(max_usd=.01)
        self.lookup()
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))["total_usd"], .02)
        before = len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            self.lookup(check("second.test"))
        self.assertEqual(len(self.provider.requests), before)
        self.assertEqual(len(budget.load_ledger(self.path)["calls"]), 1)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])




    def test_saved_schema_error_can_be_reprojected_without_dispatch_or_rewriting(self):
        self.start()
        self.provider.raw = {"status": "completed", "toolResponse": {"rawV2": {"search_results": [{
            "role_title": "Head of Claims", "is_current": False, "end_date": "2025-01-01",
            "person": {"full_name": "Ada Example", "linkedin_info": {
                "public_profile_url": "https://www.linkedin.com/in/ada-example"}}}]}}}
        found = self.lookup(check(tool="forager_person_role_search", inputs={"query": "claims"}))["lookups"][0]
        path = self.path.parent / "receipts" / (found["route"] + ".json")
        saved = json.loads(path.read_text())
        saved.update(status="schema_error", results=[], error="Unknown response shape")
        path.write_text(json.dumps(saved))
        before = path.read_bytes(), budget.ledger_path(self.path).read_bytes(), self.path.read_bytes()
        calls = len(self.provider.requests)
        page = self.tools.inspect(ref=found["route"])
        self.assertEqual((page["status"], page["saved_status"], page["result_count"]), ("ok", "schema_error", 1))
        displayed = page["results"][0]["facts"]
        self.assertEqual((displayed["role_title"], displayed["is_current"], displayed["end_date"]),
                         ("Head of Claims", False, "2025-01-01"))
        self.assertFalse(displayed.get("contact_title"))
        self.assertIsNone(page["error"])
        row = self.tools.inspect(ref=page["results"][0]["ref"])["facts"]
        self.assertEqual(row["contact_name"], "Ada Example")
        self.assertEqual(row["content_kind"], "unverified")
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual((path.read_bytes(), budget.ledger_path(self.path).read_bytes(), self.path.read_bytes()), before)
        saved["provider_response"]["body"]["toolResponse"]["rawV2"]["error"] = "Provider rejected request"
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, "no evidence can be selected"):
            self.tools.inspect(ref=page["results"][0]["ref"])


    def test_spending_pause_preserves_review_without_clearing_ledger_or_allowing_calls(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        reason = "provider billed above its reserved bound; reconcile pricing before further paid calls"
        with budget.transaction(budget.ledger_path(self.path)) as ledger:
            ledger["blocked"] = reason
        before = budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        result = self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Funding remains unverified", "company": {"ref": ref}}])
        self.assertEqual(result["progress"]["budget"]["blocked"], reason)
        self.assertEqual(json.loads(self.path.read_text())["unresolved"][0]["reason_text"], "Funding remains unverified")
        blocked = self.lookup(check("another.test"))
        self.assertEqual(blocked["status"], "operationally_blocked")
        with patch.object(research_tools.subprocess, "run") as export:
            self.assertEqual(self.tools.finish()["status"], "operationally_blocked")
            export.assert_not_called()
        self.assertFalse(blocked["delivery_allowed"])
        self.assertEqual(json.loads(Path(blocked["status_file"]).read_text())["reason"], reason)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)

    def test_all_selected_field_conflicts_are_reported_before_web_is_saved(self):
        self.start()
        self.provider.raw["element"]["locations"] = [{"headquarter": True, "country": "Canada"}]
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        before = self.path.read_bytes()
        receipts = sorted((self.path.parent / "receipts").glob("*.json"))
        web = [{"target": "example.test", "purpose": "Review the announcement", "query": "example.test news",
                "response": {"status": "ok", "results": [{"url": "https://example.test/news", "text": "An observed company announcement"}]}}]
        company = {"target": "example.test", "decision": "hold_account", "reason": "Funding still needs research",
                   "company": {"ref": ref, "website": "https://www.example.test/", "hq_country": "United States",
                               "employee_range_evidence": {}}}
        with self.assertRaises(ValueError) as error:
            self.tools.review(companies=[company], web=web)
        for field in ("website", "hq_country", "employee_range_evidence"):
            self.assertIn(field, str(error.exception))
        self.assertIn("Keep the selected ref", str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted((self.path.parent / "receipts").glob("*.json")), receipts)
        company["company"] = {"ref": ref}
        self.tools.review(companies=[company], web=web)
        self.assertEqual(len(json.loads(self.path.read_text())["unresolved"]), 1)

    def test_missing_company_hq_does_not_discard_reviewed_fields(self):
        self.start()
        self.provider.raw["element"]["locations"] = [{"headquarter": False, "country": "Canada"}]
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        self.assertNotIn("hq_country", self.tools._harvest({"ref": ref}, "example.test"))
        facts = self.tools._harvest({"ref": ref, "hq_country": "United States", "hq_state": "Minnesota"}, "example.test")
        self.assertEqual(facts["hq_country"], "United States")
        self.assertEqual(facts["hq_state"], "Minnesota")
        self.assertEqual(facts["employee_range"], "51-200")
        source = captured_page(self.tools, self.provider, url="https://example.test/about",
                               text="Our headquarters is in Minnesota, United States.")
        calls = len(self.provider.requests)
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Headquarters verified; remaining fit still needs research",
            "company": {"ref": ref, "hq_country": "United States", "hq_state": "Minnesota"},
            "qualification_checks": [{"criterion": "headquarters", "importance": "preferred", "status": "pass",
                "claim": "Company headquarters is in Minnesota, United States",
                "evidence": [{"ref": source}]}]}])
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Other fit remains unverified", "company": {"description": "Company description."}}])
        saved = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(saved["candidate"]["hq_country"], "United States")
        self.assertEqual(saved["candidate"]["hq_state"], "Minnesota")
        self.assertIn("headquarters is in Minnesota", saved["qualification_checks"][0]["evidence"][0]["text"])
        self.assertEqual(len(self.provider.requests), calls)

    def test_publication_date_alias_is_retained_and_missing_date_is_not_invented(self):
        self.start()
        result = self.tools.review(web=[{"target": "example.test", "purpose": "Review publication", "query": "example.test news",
            "response": {"status": "ok", "results": [
                {"url": "https://example.test/news", "text": "A dated announcement", "published_date": "2026-02-09"},
                {"url": "https://example.test/about", "text": "Current company information"}]}}])
        route = result["web_references"]["web:0"]
        evidence = self.tools._evidence({"ref": route + ":0", "date_basis": "published"})
        self.assertEqual(evidence["date"], "2026-02-09")
        with self.assertRaisesRegex(ValueError, "cannot replace captured metadata"):
            self.tools._evidence({"ref": route + ":1", "date_basis": "published"})
        current = self.tools._evidence({"ref": route + ":1"})
        self.assertEqual(current["date_basis"], "observed_current")

    def test_undated_capture_accepts_only_its_effective_observation_date(self):
        self.start()
        ref = captured_page(self.tools, self.provider, date=None,
                            text="The annual results report group operating profit for 2025.")
        receipt = self.path.parent / "receipts" / (ref.split(":")[0] + ".json")
        original = receipt.read_bytes()
        implicit = self.tools._evidence({"ref": ref})
        explicit = self.tools._evidence({"ref": ref, "date": implicit["date"],
                                         "date_basis": "observed_current"})
        self.assertEqual(explicit, implicit)
        self.assertEqual(explicit["date"], self.tools._document()["request"]["as_of_date"])
        for override in ({"date": "2000-01-01"}, {"date_basis": "published"}):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, "cannot replace captured metadata"):
                self.tools._evidence({"ref": ref, **override})
        self.assertEqual(receipt.read_bytes(), original)

    def test_funding_reference_supplies_saved_date_and_text_without_manual_copy(self):
        self.start()
        rows = [
            {"name": "Series B - ExamplePay", "announcedOn": "2024-11-28T00:00:00.000Z"},
            {"name": "Undated round - ExamplePay"}]
        self.provider.raw = {"toolResponse": {"rawV2": {"fundingRounds": rows}},
                             "output_preview": {"kind": "list", "rowCount": len(rows), "preview": rows}}
        results = self.lookup(check(tool="aviato_get_company_funding_rounds", inputs={"website": "https://example.test"}))["lookups"][0]["results"]
        evidence = self.tools._evidence({"ref": results[0]["ref"]})
        self.assertEqual((evidence["date"], evidence["date_basis"], evidence["text"]),
                         ("2024-11-28", "published", "Series B - ExamplePay"))
        with self.assertRaisesRegex(ValueError, "no publication/event date"):
            self.tools._evidence({"ref": results[1]["ref"]})

    def test_url_free_funding_reuses_raw_receipt_for_company_review(self):
        self.request["icp"]["required_attributes"] = ["Funding stage is Series C or later"]
        self.start()
        company_ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        funding_ref = self.saved_funding()
        before = budget.ledger_path(self.path).read_bytes()
        calls = len(self.provider.requests)
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Reviewed stage and fit",
            "company": {"ref": company_ref}, "account_fit": {"ref": company_ref},
            "qualification_checks": self.qualifying_signal(company_ref) + [{"requirement_ref": "attribute:0", "status": "pass",
                "claim": "Funding history records Series C; this is a stage check, not a recent event", "evidence": [{"ref": funding_ref}]}]}])
        saved = json.loads(self.path.read_text())
        evidence = saved["unresolved"][0]["qualification_checks"][-1]["evidence"][0]
        self.assertIsNone(evidence["url"])
        self.assertEqual(evidence["source"]["result_index"], 0)
        self.assertFalse(runner.qualification_errors(saved, run_file=self.path))
        from validate_run import qualification_evidence_error
        self.assertIsNotNone(qualification_evidence_error(evidence, "funding", saved,
            {"domain": "example.test"}, saved["unresolved"][0]["qualification_checks"][-1], None))
        packet = self.tools.inspect(target="example.test", field="evidence_review")
        self.assertEqual(packet["sources"][funding_ref]["record"]["id"], "round-c")
        self.assertEqual(packet["sources"][funding_ref]["date"], "2024-11-28")
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)

    def test_url_free_funding_rejects_wrong_identity_tampering_and_signal_use(self):
        from source_receipts import funding_record
        from validate_run import qualification_evidence_error, source_evidence_error
        self.request["icp"]["required_attributes"] = ["Funding stage is Series C or later"]
        self.start()
        ref = self.saved_funding()
        evidence = self.tools._evidence({"ref": ref})
        document = json.loads(self.path.read_text())
        company = {"domain": "example.test"}
        check_value = {"criterion": self.request["icp"]["required_attributes"][0]}
        receipt_path = self.path.parent / "receipts" / (ref.split(":")[0] + ".json")
        receipt_bytes = receipt_path.read_bytes()
        original = json.loads(receipt_bytes)
        for label, replacement in (("foreign run", {"run_fingerprint": "other"}),
                ("wrong request", {"request_fingerprint": "other"}),
                ("incomplete", {"receipt_status": "pending"}),
                ("unknown outcome", {"status": "error"}),
                ("pending", {"pending_verification": {"id": "pending"}}),
                ("missing raw", {"provider_response": None})):
            with self.subTest(label=label):
                receipt_path.write_text(json.dumps({**original, **replacement}))
                with self.assertRaises(ValueError):
                    funding_record(self.path, document, company, evidence)
        receipt_path.write_bytes(receipt_bytes)
        for field, value in (("date", "2026-01-01"), ("text", "Different claim"), ("date_basis", "observed_current")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                funding_record(self.path, document, company, {**evidence, field: value})
        for field, value in (("result_index", 3), ("result_index", True), ("tool", "web_search")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                funding_record(self.path, document, company, {**evidence, "source": {**evidence["source"], field: value}})
        with self.assertRaisesRegex(ValueError, "identify this company"):
            funding_record(self.path, document, {"domain": "other.test"}, evidence)
        altered = copy.deepcopy(original)
        altered["attempt"]["request"]["payload"]["website"] = "https://other.test"
        receipt_path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            funding_record(self.path, document, {"domain": "other.test"}, evidence)
        altered = copy.deepcopy(original)
        altered["results"][0]["stage"] = "Invented Series D"
        receipt_path.write_text(json.dumps(altered))
        self.assertEqual(funding_record(self.path, document, company, evidence)["stage"], "Series C")
        altered["provider_response"]["body"]["toolResponse"]["rawV2"]["fundingRounds"][0]["website"] = "https://other.test"
        altered["provider_response"]["body"]["output_preview"]["preview"][0]["website"] = "https://other.test"
        receipt_path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(ValueError, "different company"):
            funding_record(self.path, document, company, evidence)
        receipt_path.write_bytes(receipt_bytes)
        self.assertIsNone(qualification_evidence_error(evidence, "stage", document, company, check_value, self.path))
        for disallowed in ({**check_value, "signal": "FUNDING"}, {"criterion": "unrequested stage"}):
            self.assertIn("signals require a source URL", qualification_evidence_error(evidence, "signal", document, company, disallowed, self.path))
        self.assertIn("HTTP/HTTPS", source_evidence_error(evidence, "account_fit"))
        receipt_path.unlink()
        self.assertIn("No such file", qualification_evidence_error(evidence, "stage", document, company, check_value, self.path))

    def test_compatible_result_reviews_merge_into_one_saved_route_decision(self):
        self.start()
        observed = self.tools.review(web=[{"target": "discovery", "purpose": "Read funding sources", "query": "funding sources",
            "response": {"status": "ok", "results": [{"text": "First source"}, {"text": "Second source"}]}}])
        rid = observed["web_references"]["web:0"]
        self.tools.review(sources=[{"ref": rid + ":0", "state": "exhausted", "reason": "First source reviewed"},
                                   {"ref": rid + ":1", "state": "exhausted", "reason": "Second source reviewed"}])
        saved = json.loads(self.path.read_text())
        route = next(r for r in saved["stop_audit"]["route_frontier"] if r["route_id"] == rid)
        self.assertEqual(route["state"], "exhausted")
        self.assertEqual(route["reason"], "First source reviewed\nSecond source reviewed")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "conflicting source decisions"):
            self.tools.review(sources=[{"ref": rid + ":0", "state": "exhausted", "reason": "Done"},
                                       {"ref": rid + ":1", "state": "blocked", "reason": "Not reviewed"}])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_attached_web_date_fails_before_saving_and_corrected_retry_works(self):
        self.start()
        web = [{"target": "example.test", "purpose": "Review event", "query": "example event",
                "response": {"status": "ok", "results": [{"url": "https://example.test/news", "text": "Opened a plant"}]}}]
        companies = [{"target": "example.test", "decision": "hold_account", "reason": "More evidence needed",
                      "signal_evidence": {"ref": "web:0:0", "signal": "FACILITY_OPENING", "date_basis": "published"}}]
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "cannot replace captured metadata"):
            self.tools.review(companies=companies, web=web)
        self.assertEqual(self.path.read_bytes(), before)
        web[0]["response"]["results"][0]["published_date"] = json.loads(self.path.read_text())["request"]["as_of_date"]
        result = self.tools.review(companies=companies, web=web)
        self.assertIn("web:0", result["web_references"])

    def test_crossed_company_web_indexes_fail_before_saving_and_can_be_corrected(self):
        self.start()
        web = [{"target": target, "purpose": "Review company news", "query": target,
                "response": {"status": "ok", "results": [{"url": f"https://{target}/news",
                    "text": f"Announcement about {target}"}]}}
               for target in ("first.test", "second.test")]
        company = {"target": "second.test", "decision": "hold_account", "reason": "Review fit",
                   "account_fit": {"ref": "web:0:0"}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, "web:0:0.*first.test.*second.test.*web:1:0"):
            self.tools.review(companies=[company], web=web)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        company["account_fit"]["ref"] = "web:1:0"
        saved = self.tools.review(companies=[company], web=web)
        row = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(row["account_fit"]["evidence_url"], "https://second.test/news")
        # A deliberately shared saved source remains available; scope is not a semantic gate.
        company["target"] = "first.test"
        company["account_fit"]["ref"] = saved["web_references"]["web:1"] + ":0"
        self.tools.review(companies=[company])
        self.assertEqual(len(json.loads(self.path.read_text())["unresolved"]), 2)

    def test_shared_discovery_observation_remains_usable_with_company_observations(self):
        self.start()
        web = [{"target": target, "purpose": "Review source", "query": target,
                "response": {"status": "ok", "results": [{"url": "https://news.test/shared",
                    "text": "A shared announcement."}]}}
               for target in ("discovery", "example.test")]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Shared source reviewed", "account_fit": {"ref": "web:0:0"}}], web=web)
        row = json.loads(self.path.read_text())["unresolved"][0]
        self.assertEqual(row["account_fit"]["evidence_url"], "https://news.test/shared")

    def test_review_exposes_judgment_under_review_without_state_changes(self):
        self.request["icp"]["required_attributes"] = ["Operates multiple sites"]
        self.request["buying_signals"] = [{"kind": "EXPANSION", "importance": "preferred",
            "query": "Completed expansion into a new market", "max_age_days": 365}]
        self.start()
        row = {"qualification_checks": [
            {"criterion": "operates multiple sites", "importance": "required", "status": "pass", "evidence": []},
            {"criterion": "expansion", "signal": "EXPANSION", "importance": "preferred", "status": "pass",
             "claim": "An expansion was proposed", "evidence": []},
            {"criterion": "hiring", "signal": "HIRING", "importance": "preferred", "status": "unknown",
             "claim": "Hiring has not been established", "evidence": []}]}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        original = copy.deepcopy(row)
        view = self.tools._company_review(row, {})
        self.assertEqual(view["qualification_checks"][0]["requirement"]["label"], "Operates multiple sites")
        signal = view["signal_checks"][0]
        self.assertEqual(signal["requirement"]["query"], "Completed expansion into a new market")
        self.assertEqual(signal["requirement"]["ref"], "signal:0")
        self.assertEqual(signal["draft_claim"], "An expansion was proposed")
        self.assertEqual(len(view["qualification_checks"]), 1)
        self.assertEqual([c["recorded_status"] for c in view["signal_checks"]], ["pass", "unknown"])
        for check in view["signal_checks"] + view["qualification_checks"]:
            self.assertNotIn("status", check)
            self.assertNotIn("claim", check)
        self.assertEqual(row, original)
        self.assertNotIn("verified_signals", view)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_failed_judgment_returns_reusable_saved_web_reference(self):
        self.start()
        web = [{"target": "example.test", "purpose": "Read observed page", "query": "observed page",
                "response": {"status": "ok", "results": [{"url": "https://example.test", "text": "Verified page text"}]}}]
        with self.assertRaisesRegex(ValueError, "Web observations were saved as") as error:
            self.tools.review(web=web, companies=[{"target": "example.test", "decision": "accept", "reason": "Missing contact evidence"}])
        rid = json.loads(self.path.read_text())["routes"][-1]["route_id"]
        self.assertIn(rid, str(error.exception))
        self.assertEqual(self.tools.inspect(ref=rid + ":0")["facts"]["text"], "Verified page text")

    def test_native_review_contract_names_judgment_fields_before_saving_observations(self):
        self.start()
        payload = {"companies": [{"target": "example.test", "decision": "hold_account", "reason": "Review fit",
            "qualification_checks": [{"criterion": "product", "status": "unknown", "evidence": []}]}],
            "web": [{"target": "example.test", "purpose": "Review source", "query": "company information",
                "response": {"status": "ok", "results": [{"url": "https://example.test", "text": "Observed facts"}]}}]}
        before = self.path.read_bytes()
        receipt_count = len(list((self.path.parent / "receipts").glob("*.json"))) if (self.path.parent / "receipts").exists() else 0
        with self.assertRaisesRegex(ValueError, "missing fields: claim"):
            self.tools.call("tyche_review", payload)
        check = payload["companies"][0]["qualification_checks"][0]
        check.update(importance="required", claim="Product fit still needs review",
            evidence=[{"ref": "web:0:0", "date_basis": "2026-09-14"}])
        with self.assertRaisesRegex(ValueError, "date_basis must be one of"):
            self.tools.call("tyche_review", payload)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(list((self.path.parent / "receipts").glob("*.json"))), receipt_count)
        check["evidence"][0]["date_basis"] = "observed_current"
        result = self.tools.call("tyche_review", payload)
        self.assertEqual(result["saved_companies"], ["example.test"])

    def test_signal_age_is_checked_at_account_gate_and_delivery(self):
        request = {"as_of_date": "2026-09-14", "time_window": {"max_age_days": 365},
                   "buying_signals": [{"kind": "FACILITY_OPENING", "max_age_days": 365}, {"kind": "HIRING", "max_age_days": 90}]}
        row = {"stage": "contact", "candidate": {"domain": "example.test"},
               "signal_evidence": {"signal": "FACILITY_OPENING", "evidence_date": "2025-04-01"}}
        document = {"request": request, "unresolved": [row]}
        self.assertIn("event_date is required", ";".join(runner.qualification_errors(document)))
        row["stage"] = "account"
        self.assertEqual(runner.qualification_errors(document), [])
        row["stage"] = "contact"
        for date, valid in [("2025-09-14", True), ("2025-09-13", False), ("2026-09-15", False)]:
            row["signal_evidence"]["evidence_date"] = date
            row["signal_evidence"]["event_date"] = date
            self.assertEqual(not runner.qualification_errors(document), valid)
        row["signal_evidence"] = {"signal": "HIRING", "evidence_date": "2026-06-15", "event_date": "2026-06-15"}
        self.assertIn("2026-06-16 through 2026-09-14", ";".join(runner.qualification_errors(document)))
        document["accepted"], document["unresolved"] = [row], []
        self.assertIn("2026-06-16 through 2026-09-14", ";".join(runner.qualification_errors(document)))

    def test_signal_date_preflight_leaves_rejected_web_batch_unsaved(self):
        self.start()
        date = json.loads(self.path.read_text())["request"]["as_of_date"]
        web = [{"target": target, "purpose": "Review source", "query": target, "operation": "open",
                "response": {"status": "ok", "results": [{"url": "https://" + target,
                    "date": date, "text": "Observed company announcement."}]}}
               for target in ("one.test", "two.test")]
        companies = [{"target": w["target"], "decision": "accept", "reason": "Reviewed company",
            "qualification_checks": [{"requirement_ref": "signal:0", "status": "pass", "claim": "Partnership confirmed",
                "evidence": [{"ref": f"web:{i}:0", "event_date": date}]}]}
            for i, w in enumerate(web)]
        hiring = {"requirement_ref": "signal:1", "status": "pass", "claim": "Hiring observed",
                  "evidence": [{"ref": "web:1:0", "event_date": "2020-01-01"}]}
        companies[1]["qualification_checks"].append(hiring)
        def snapshot():
            return (self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(),
                    {p.name: p.read_bytes() for p in (self.path.parent / "receipts").glob("*.json")},
                    len(self.provider.requests))
        before = snapshot()
        for invalid, message in [("2020-01-01", "not wholly within the requested window"), (None, "event_date is required")]:
            if invalid:
                hiring["evidence"][0]["event_date"] = invalid
            else:
                hiring["evidence"][0].pop("event_date")
            with self.assertRaisesRegex(ValueError, message) as error:
                self.tools.review(companies=companies, web=web)
            self.assertIn("input.companies[1] (two.test)", str(error.exception))
            self.assertIn("No attached web observations", str(error.exception))
            self.assertEqual(snapshot(), before)
        hiring["status"] = "unknown"
        # Date corrections alone cannot make model-transcribed text qualify.
        with self.assertRaisesRegex(ValueError, "tool-captured"):
            self.tools.review(companies=companies, web=web)
        for item in companies:
            item["decision"] = "hold_account"
        result = self.tools.review(companies=companies, web=web)
        self.assertEqual(result["saved_companies"], ["one.test", "two.test"])
        self.assertEqual(len(result["web_references"]), 2)
        receipts = snapshot()[2]
        self.tools.review(companies=companies, web=web)
        self.assertEqual(snapshot()[2], receipts)

    def test_schema_blocks_bad_inputs_but_missing_quote_allows_actual_billing(self):
        self.start()
        with self.assertRaises(ValueError):
            self.lookup(check(inputs={"wrong": "field"}))
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        self.provider.rate, self.provider.billed_rate = None, .2
        result = self.lookup(check(tool="unknown-priced-tool", inputs={"query": "fit"}))
        charge = budget.load_ledger(self.path)["calls"][result["lookups"][0]["route"]]
        self.assertEqual(float(charge["actual_credits"]), .2)
        self.assertNotIn("maximum_credits", charge)
        for bad in ({"checks": []}, {"checks": [check()] * 4}, {"checks": [dict(check(), command="echo")]}, {"checks": [check(max_cost_credits=float("nan"))]}):
            with self.assertRaises(ValueError):
                self.tools.call("tyche_lookup", bad)

    def test_failed_free_pricing_read_can_resume_without_resetting_clock(self):
        self.tools.execute = lambda request, capture: ({"provider":"deepline", "operation":"describe", "status":"provider_error", "results":[]}, 2)
        blocked = self.start()
        self.assertEqual(blocked["status"], "operationally_blocked")
        self.assertIn("tool unavailable", blocked["reason"])
        original = json.loads((self.path.parent / "company-tool.json").read_text())
        self.tools.execute = self.provider
        self.start()
        self.assertEqual(json.loads(self.path.read_text())["stop_check"]["started_at"], original["started_at"])
        self.assertEqual(len(list(self.path.parent.glob("company-tool-*.json"))), 2)
        self.assertFalse(budget.load_ledger(self.path)["calls"])

    def test_multiple_batches_share_three_dispatch_slots_and_lose_no_writes(self):
        self.start()
        self.provider.delay = .1
        batches = [[check(f"company-{i}-{j}.test") for j in range(3)] for i in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda batch: self.lookup(*batch), batches))
        self.assertEqual(sum(len(r["lookups"]) for r in results), 9)
        self.assertEqual(self.provider.peak, 3)
        self.assertEqual(len(budget.load_ledger(self.path)["calls"]), 9)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_receipt_recovery_uses_saved_response_without_redispatch(self):
        self.start()
        self.tools.inspect(tool="harvestapi_get_company")
        with patch.object(runner, "finish_attempt", side_effect=OSError("interrupted after capture")), self.assertRaises(OSError):
            self.lookup()
        rid = next(iter(budget.load_ledger(self.path)["calls"]))
        self.assertEqual(self.tools.inspect()["pending"][0]["ref"], rid)
        calls = len(self.provider.requests)
        self.tools.inspect(recover=rid)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.lookup()

    def test_cap_and_uncertain_charges_survive_native_retry(self):
        self.start(max_usd=.025)
        result = self.lookup(*[check(f"company-{i}.test") for i in range(3)])
        self.assertEqual(len(result["lookups"]), 3)
        self.assertGreaterEqual(len(budget.load_ledger(self.path)["calls"]), 2)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
        before = len(self.provider.requests)
        try:
            blocked = self.lookup(check("later.test"))
        except ValueError as exc:
            self.assertIn("budget_exhausted", str(exc))
        else:
            # A racing batch member can already have recorded quota_exceeded,
            # so the native tool returns its operational block instead.
            self.assertEqual(blocked["status"], "operationally_blocked")
        self.assertEqual(budget.spending_stop(budget.load_ledger(self.path)), "budget_exhausted")
        self.assertEqual(len(self.provider.requests), before)

        other = ResearchTools(self.path.parent.parent / "uncertain/results.json", execute=lambda request, capture:
            self.provider(request, capture) if request["operation"] != "execute" else budget.guarded_call(request, "deepline", lambda: (
                {"provider": "deepline", "operation": "execute", "tool": request["tool"], "status": "timeout", "results": []}, 2)))
        other.start(self.request)
        outcome = other.lookup([check()])["lookups"][0]
        ledger = budget.load_ledger(other.path)
        self.assertIsNone(ledger["calls"][outcome["route"]]["actual_credits"])
        self.assertEqual(ledger["calls"][outcome["route"]]["state"], "pending_billing")
        self.assertNotIn("maximum_credits", ledger["calls"][outcome["route"]])
        with self.assertRaisesRegex(ValueError, "already attempted"):
            other.lookup([check()])

    def unbilled_provider(self, request, capture):
        if request["operation"] != "execute":
            return self.provider(request, capture)
        self.provider.requests.append(copy.deepcopy(request))
        def dispatch():
            raw = {"exit_code": 0, "body": copy.deepcopy(self.provider.raw), "stderr": ""}
            capture(raw)
            return deepline.normalize_response(request, raw)
        return budget.guarded_call(request, "deepline", dispatch)

    def test_native_pending_bill_resumes_from_exact_charge_without_replay_or_reset(self):
        import billing_reconciliation
        self.start(max_usd=.05)
        self.provider.raw['request_id'] = 'pending-native-request'
        self.tools.execute = self.unbilled_provider
        first = self.tools.call('tyche_lookup', {'checks': [check()]})['lookups'][0]
        rid = first['route']
        original = budget.load_ledger(self.path)
        started = json.loads(self.path.read_text())['stop_check']['started_at']
        receipt = self.path.parent / 'receipts' / (rid + '.json')
        saved = receipt.read_bytes()
        calls = len(self.provider.requests)
        view = self.tools.call('tyche_inspect', {'field': 'costs'})
        costs = view['costs']
        self.assertEqual(costs['pending_provider_calls'], 1)
        self.assertIn('tyche_finish', view.get('next', ''))
        self.assertIn('Do not poll', view['next'])
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.load_ledger(self.path), original)
        self.assertIsNone(original['calls'][rid]['actual_credits'])
        stopped = self.tools.call('tyche_finish', {})
        self.assertFalse(stopped['delivery_allowed'])
        self.assertEqual(stopped['status'], 'needs_research')
        self.assertEqual(stopped['progress']['stop'], 'continue')
        self.assertIsNone(stopped['progress']['stop_reason'])
        self.assertEqual(len(self.provider.requests), calls)
        # Unknown billing is retained but does not block a distinct route.
        self.assertIsNone(budget.admission_stop(budget.load_ledger(self.path)))
        self.assertEqual(len(self.provider.requests), calls)
        settled = billing_reconciliation.reconcile(self.path, fetch=lambda: {'recent': {'entries': [{
            'id': 'pending-native-debit', 'request_id': 'pending-native-request',
            'operation': 'harvestapi_get_company', 'provider': 'harvestapi',
            'status': 'completed', 'charge_state': 'posted', 'credits': .2, 'delta': -.2}]}})
        self.assertEqual(settled['matched'], [rid])
        self.assertEqual(receipt.read_bytes(), saved)
        self.assertNotIn('next', self.tools.call('tyche_inspect', {'field': 'costs'}))
        self.tools.execute = self.provider
        self.start()
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            self.tools.call('tyche_lookup', {'checks': [check()]})
        self.assertEqual(len(self.provider.requests), calls)
        self.tools.call('tyche_lookup', {'checks': [check('later.test')]})
        final = budget.load_ledger(self.path)
        self.assertEqual(final['usd_limit'], original['usd_limit'])
        self.assertEqual(json.loads(self.path.read_text())['stop_check']['started_at'], started)
        self.assertEqual(len(final['calls']), 2)
        self.assertEqual(budget.actual_cost_summary(final)['provider_usd'], .04)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_native_finish_keeps_research_open_but_final_delivery_requires_model_usage(self):
        import run_attempt
        self.start()
        sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'scripts'))
        from run_costs import UsageReceipt
        request = self.path.parent / 'request.txt'
        request.write_text('Synthetic interrupted model usage; no external services.')
        receipt = UsageReceipt(request, 'gpt-5.6-luna', 'high', 'fast')
        receipt.finish(130)
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        progress = self.tools.call('tyche_inspect', {})
        self.assertEqual(progress['stop'], 'continue')
        self.assertIsNone(progress['stop_reason'])
        stopped = self.tools.call('tyche_finish', {})
        self.assertFalse(stopped['delivery_allowed'])
        self.assertEqual(stopped['status'], 'needs_research')
        _, preflight = run_attempt.delivery_preflight(
            self.path, json.loads(self.path.read_text()), check_review=False)
        self.assertIn(
            'final delivery requires complete cost accounting: model_usage_pending',
            preflight['errors'])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_successful_free_contract_settles_without_billing_and_allows_next_paid_call(self):
        self.provider.rate = 0
        self.tools.execute = self.unbilled_provider
        self.start()
        outcome = self.lookup()["lookups"][0]
        rid = outcome["route"]
        state = budget.load_ledger(self.path)
        call = state["calls"][rid]
        self.assertEqual((call["actual_credits"], call["state"]), ("0", "settled"))
        self.assertIn("free_evidence", call)
        self.assertNotIn("billing_evidence", call)
        receipt_path = self.path.parent / "receipts" / (rid + ".json")
        before = receipt_path.read_bytes()
        self.assertNotIn("billing", json.loads(before))
        self.assertEqual(budget.actual_cost_summary(state)["providers"]["deepline"]["catalog_free_calls"], [rid])
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
        self.provider.billed_rate = 1  # A reported charge takes precedence even if the quote was free.
        self.tools.execute = self.provider
        self.lookup(check("next.test"))
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))["provider_usd"], .1)
        self.assertEqual(receipt_path.read_bytes(), before)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
        changed = json.loads(before)
        changed["results"][0]["description"] = "Changed after settlement"
        receipt_path.write_text(json.dumps(changed))
        self.assertTrue(any("free-call contract" in e for e in budget.audit_ledger(self.path, json.loads(self.path.read_text()))))

    def test_empty_free_search_resumes_from_bound_contract_without_replay_or_reset(self):
        import billing_reconciliation as billing
        self.provider.rate = 0
        self.provider.raw = {"status": "completed", "job_id": "empty-free-request",
            "toolResponse": {"rawV2": {"results": [], "query": "No matching company"}}}
        self.tools.execute = self.unbilled_provider
        self.start(max_usd=.75)
        with patch.object(billing, "free_call_evidence", return_value=None):
            outcome = self.lookup(check(tool="contextdev_post_web_search", inputs={"query": "No matching company"}))["lookups"][0]
        self.assertEqual(outcome["status"], "no_results")
        rid = outcome["route"]
        path = self.path.parent / "receipts" / (rid + ".json")
        before, calls = path.read_bytes(), len(self.provider.requests)
        initial = budget.load_ledger(self.path)
        self.assertEqual(initial["calls"][rid]["state"], "pending_billing")
        started = json.loads(self.path.read_text())["stop_check"]["started_at"]
        billing.reconcile(self.path, fetch=lambda: self.fail("Bound free contract needs no billing request"))
        state = budget.load_ledger(self.path)
        self.assertEqual((state["calls"][rid]["state"], state["calls"][rid]["actual_credits"]), ("settled", "0"))
        self.assertIn("free_evidence", state["calls"][rid])
        self.assertNotIn("billing_evidence", state["calls"][rid])
        self.assertEqual(state["usd_limit"], initial["usd_limit"])
        self.assertEqual(json.loads(self.path.read_text())["stop_check"]["started_at"], started)
        self.assertEqual((path.read_bytes(), len(self.provider.requests)), (before, calls))
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
        self.provider.raw = {"status": "ok", "results": [{"url": "https://example.com", "text": "A later result"}]}
        self.provider.billed_rate = .1
        self.tools.execute = self.provider
        self.lookup(check("next.test", tool="contextdev_post_web_search", inputs={"query": "Next company"}))
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))["provider_usd"], .01)

    def test_empty_free_label_cannot_hide_failed_transport_or_missing_capture(self):
        import billing_reconciliation as billing
        self.provider.rate = 0
        self.provider.raw = {"status": "completed", "job_id": "empty-free-request", "results": []}
        self.tools.execute = self.unbilled_provider
        self.start()
        with patch.object(billing, "free_call_evidence", return_value=None):
            rid = self.lookup(check(tool="contextdev_post_web_search", inputs={"query": "No matching company"}))["lookups"][0]["route"]
        path = self.path.parent / "receipts" / (rid + ".json")
        original = json.loads(path.read_text())
        call = budget.load_ledger(self.path)["calls"][rid]
        for change in ("timeout", "error", "missing", "partial", "pending"):
            with self.subTest(change=change):
                receipt = copy.deepcopy(original)
                if change == "timeout":
                    receipt["provider_response"]["timed_out"] = True
                elif change == "error":
                    receipt["provider_response"]["body"] = {"status": "failed", "error": "Unavailable"}
                elif change == "missing":
                    receipt.pop("provider_response")
                elif change == "partial":
                    receipt["receipt_status"] = "incomplete"
                else:
                    receipt["pending_verification"] = {"id": "unfinished"}
                path.write_text(json.dumps(receipt))
                self.assertIsNone(billing.free_call_evidence(self.path, rid, call))
        self.assertIsNone(budget.load_ledger(self.path)["calls"][rid]["actual_credits"])

    def test_nested_empty_failure_cannot_settle_from_a_free_catalog(self):
        import billing_reconciliation as billing
        self.provider.rate = 0
        self.provider.raw = {"status": "completed", "job_id": "nested-error",
            "toolResponse": {"rawV2": {"results": [], "error": "provider unavailable"}}}
        self.tools.execute = self.unbilled_provider
        self.start(max_usd=.75)
        outcome = self.lookup(check(tool="contextdev_post_web_search", inputs={"query": "Fixture"}))["lookups"][0]
        self.assertEqual(outcome["status"], "provider_error")
        rid = outcome["route"]
        receipt = self.path.parent / "receipts" / (rid + ".json")
        before, calls = receipt.read_bytes(), len(self.provider.requests)
        initial = budget.load_ledger(self.path)
        call = initial["calls"][rid]
        self.assertEqual(call["state"], "pending_billing")
        self.assertIsNone(call["actual_credits"])
        self.assertNotIn("free_evidence", call)
        self.assertIsNone(billing.free_call_evidence(self.path, rid, call))
        billing.reconcile(self.path, fetch=lambda: {"recent": {"entries": []}})
        final = budget.load_ledger(self.path)
        self.assertIsNone(final["calls"][rid]["actual_credits"])
        self.assertEqual(final["usd_limit"], initial["usd_limit"])
        self.assertEqual((receipt.read_bytes(), len(self.provider.requests)), (before, calls))
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_paid_variable_conditional_or_failed_calls_cannot_use_free_contract_settlement(self):
        for label, pricing, success in [
            ("paid", {"unit": "call", "creditsPerUnit": .1}, True),
            ("variable", {"unit": "usage", "creditsPerUnit": None}, True),
            ("conditional", {"unit": "call", "creditsPerUnit": 0, "details": ["Only first request is free"]}, True),
            ("usd_charge", {"unit": "call", "creditsPerUnit": 0, "usdPerUnit": .1}, True),
            ("failed", {"unit": "call", "creditsPerUnit": 0}, False),
        ]:
            with self.subTest(label=label):
                tools = ResearchTools(self.path.parent.parent / label / "results.json", execute=self.unbilled_provider)
                tools.start(self.request)
                tools.inspect(tool="harvestapi_get_company")
                doc = json.loads(tools.path.read_text())
                catalog = next(r for r in doc["routes"] if r.get("tool") == "harvestapi_get_company")
                path = tools.path.parent / "receipts" / (catalog["route_id"] + ".json")
                descriptor = json.loads(path.read_text())
                descriptor["results"][0]["pricing"] = pricing
                path.write_text(json.dumps(descriptor))
                if not success:
                    self.provider.raw = {"error": "Service unavailable"}
                outcome = tools.lookup([check()])["lookups"][0]
                call = budget.load_ledger(tools.path)["calls"][outcome["route"]]
                self.assertIsNone(call["actual_credits"])
                self.assertNotIn("free_evidence", call)

    def test_parser_repair_settles_saved_free_success_without_replay_or_receipt_rewrite(self):
        import billing_reconciliation
        self.provider.rate = 0
        self.tools.execute = self.unbilled_provider
        self.start()
        with patch.object(deepline, "_execute_output", return_value={"provider": "deepline", "operation": "execute",
                "tool": "harvestapi_get_company", "status": "schema_error", "results": []}):
            rid = self.lookup()["lookups"][0]["route"]
        path = self.path.parent / "receipts" / (rid + ".json")
        before = path.read_bytes()
        self.assertEqual(budget.load_ledger(self.path)["calls"][rid]["state"], "pending_billing")
        calls = len(self.provider.requests)
        billing_reconciliation.reconcile(self.path, fetch=lambda: self.fail("No billing read is needed for this free contract"))
        self.assertEqual(budget.load_ledger(self.path)["calls"][rid]["state"], "settled")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_catalog_changes_after_dispatch_cannot_clear_unknown_charge(self):
        self.start()
        self.tools.inspect(tool="harvestapi_get_company")
        def execute(request, capture):
            if request["operation"] != "execute":
                return self.provider(request, capture)
            def dispatch():
                state = budget.load_ledger(self.path)
                call = state["calls"][request["spend"]["route_id"]]
                path = self.path.parent / "receipts" / (call["catalog_route_id"] + ".json")
                descriptor = json.loads(path.read_text())
                descriptor["results"][0]["pricing"] = {"unit": "call", "creditsPerUnit": 0}
                path.write_text(json.dumps(descriptor))
                raw = {"exit_code": 0, "body": copy.deepcopy(self.provider.raw), "stderr": ""}
                capture(raw)
                return deepline.normalize_response(request, raw)
            return budget.guarded_call(request, "deepline", dispatch)
        self.tools.execute = execute
        rid = self.lookup()["lookups"][0]["route"]
        call = budget.load_ledger(self.path)["calls"][rid]
        self.assertEqual(call["state"], "pending_billing")
        self.assertIsNone(call["actual_credits"])

    def test_combined_report_and_restart_preserve_the_actual_cutoff(self):
        self.start(max_usd=.03)
        request = self.path.parent / "request.txt"
        request.write_text("Synthetic accounting journey; no provider services.")
        sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
        from run_costs import UsageReceipt, save_report
        receipt = UsageReceipt(request, "gpt-5.6-luna", "xhigh", "fast")
        receipt.observe({"type": "thread.started", "thread_id": "fixture-thread"})
        usage = dict(input_tokens=0, cached_input_tokens=0, cache_write_input_tokens=0,
            output_tokens=10000, reasoning_output_tokens=0, total_tokens=10000)
        receipt.observe_response({"thread_id": "fixture-thread", "turn_id": "fixture-turn",
            "response_id": "fixture-response", "usage": usage}, "2026-09-18", "gpt-5.6-luna")
        receipt.observe({"type": "turn.completed", "usage": usage})
        receipt.finish(0)
        self.lookup()
        (self.path.parent / "research-commentary.md").write_text("Synthetic accounting journey.")
        costs = json.loads(save_report(self.path.parent).read_text())
        self.assertEqual(costs["status"], "calculated")
        self.assertEqual(costs["provider_usd"], .02)
        self.assertEqual(costs["estimated_llm_usd"], .012)
        self.assertEqual(costs["total_usd"], .032)
        self.assertEqual(costs["pending_provider_calls"], 0)
        self.assertIn("Known total: $0.0320", (self.path.parent / "report.md").read_text())
        before = budget.ledger_path(self.path).read_bytes()
        self.start()
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            self.lookup(check("next.test"))
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_usd_only_provider_receipt_stops_and_remains_auditable(self):
        self.start(max_usd=.01)
        def execute(request, capture):
            def dispatch():
                raw = {"exit_code": 0, "body": copy.deepcopy(self.provider.raw), "stderr": ""}
                raw["body"]["billing"] = {"cost_usd": .02, "pricing_status": "final", "settlement_status": "queued"}
                capture(raw)
                return deepline.normalize_response(request, raw)
            return budget.guarded_call(request, "deepline", dispatch)
        self.tools.execute = execute
        rid = self.lookup()["lookups"][0]["route"]
        state = budget.load_ledger(self.path)
        self.assertEqual(state["calls"][rid]["state"], "settled")
        self.assertEqual(budget.spending_stop(state), "budget_exhausted")
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])
        with budget.transaction(self.path) as document:
            next(r for r in document["routes"] if r["route_id"] == rid)["cost_usd"] = 0
        self.assertTrue(any("USD cost" in e for e in budget.audit_ledger(self.path, json.loads(self.path.read_text()))))


    def test_inspection_pages_long_sources_without_losing_saved_text(self):
        self.start()
        text = "verified text " * 2400
        observed = self.tools.review(web=[{"target": "discovery", "purpose": "Read long page", "query": "long source",
            "response": {"status": "ok", "results": [{"url": "https://example.test", "text": text}]}}])
        ref = observed["web_references"]["web:0"] + ":0"
        found, offset = "", 0
        while offset is not None:
            page = self.tools.call("tyche_inspect", {"ref": ref, "field": "text", "offset": offset, "limit": 200})
            self.assertEqual(page["text"], text[offset:offset + 12000])
            found += page["text"]
            offset = page["next_offset"]
        self.assertEqual(found, text)

    def test_saved_result_pages_keep_absolute_references_without_new_calls(self):
        self.start()
        rows = [{"url": f"https://example.test/{i}", "text": f"Observed result {i}"} for i in range(13)]
        observed = self.tools.review(web=[{"target": "discovery", "purpose": "Read result list", "query": "company signals",
            "response": {"status": "ok", "results": rows}}])
        rid = observed["web_references"]["web:0"]
        calls = len(self.provider.requests)
        page = self.tools.inspect(ref=rid)
        self.assertEqual(page["next_offset"], 10)
        page = self.tools.inspect(ref=rid, offset=page["next_offset"])
        self.assertEqual([r["ref"] for r in page["results"]], [f"{rid}:{i}" for i in range(10, 13)])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(self.tools.inspect(ref=page["results"][2]["ref"])["facts"], rows[12])
        selected = self.tools.inspect(ref=rid, field="results", offset=10)
        self.assertEqual(selected, {"value": rows[10:], "total": 13, "next_offset": None})
        self.assertEqual(len(self.provider.requests), calls)

    def test_oversized_inspection_limits_keep_pages_bounded_and_read_only(self):
        self.start()
        rows = [{"url": f"https://example.test/{i}", "tags": list(range(23))} for i in range(23)]
        observed = self.tools.review(web=[{"target": "discovery", "purpose": "Saved results", "query": "signals",
            "response": {"status": "ok", "results": rows}}])
        rid = observed["web_references"]["web:0"]
        before = {p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()}
        calls = len(self.provider.requests)
        for requested in (20, 100, 200, 1000000):
            with self.subTest(limit=requested):
                for options, key in (({"ref": rid}, "results"),
                                     ({"ref": rid, "field": "results"}, "value"),
                                     ({"ref": rid + ":0", "field": "tags"}, "items")):
                    page = self.tools.call("tyche_inspect", {**options, "limit": requested})
                    self.assertEqual(len(page[key]), 10)
                    self.assertEqual(page["next_offset"], 10)
                    tail = self.tools.call("tyche_inspect", {**options, "limit": requested, "offset": 20})
                    self.assertEqual(len(tail[key]), 3)
                    self.assertIsNone(tail["next_offset"])
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual({p: p.read_bytes() for p in self.path.parent.rglob("*") if p.is_file()}, before)

    def test_inspect_own_tool_reads_authoritative_schema_without_provider_or_state_changes(self):
        before = self.tools.call("tyche_inspect", {"tool": "tyche_review"})
        self.assertEqual(before["tool"]["toolId"], "tyche_review")
        self.assertEqual(before["tool"]["inputSchema"], research_tools.contract_view(research_tools.TOOLS["tyche_review"][1], "inputSchema"))
        self.assertFalse(self.path.exists())
        self.start()
        saved = self.path.read_bytes()
        calls = len(self.provider.requests)
        block = self.path.parent / "operational-status.json"
        block.write_text('{"status":"blocked"}')
        result = self.tools.call("tyche_inspect", {"tool": "tyche_review", "refresh": True,
            "field": "inputSchema.properties.companies.items.properties.qualification_checks.items.properties.status"})
        self.assertEqual(result["tool"]["enum"], ["pass", "fail", "unknown"])
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertTrue(block.exists())

    def test_tool_view_omits_sdk_help_but_preserves_cached_native_contract(self):
        self.start()
        def annotated(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                body["results"][0].update(usageGuidance={"sdk_help": "irrelevant SDK help " * 1000},
                    outputSchema={"fields": [{"name": "element", "type": "object"}],
                                  "jsonSchema": {"properties": {"element": {"type": "object"}}}})
            return body, code
        self.tools.execute = annotated
        view = self.tools.inspect(tool="harvestapi_get_company", refresh=True)["tool"]
        self.assertNotIn("usageGuidance", view)
        self.assertEqual(view["inputSchema"]["fields"][0]["name"], "url")
        self.assertEqual(view["pricing"]["creditsPerUnit"], .2)
        self.assertEqual(view["output_fields"][0]["name"], "element")
        self.assertEqual(self.tools._description_view({"outputSchema": None})["output_fields"], [])
        self.assertNotIn("reservation_preview", self.tools._description_view({"pricing": None}))
        calls = len(self.provider.requests)
        schema = self.tools.inspect(tool="harvestapi_get_company", field="outputSchema.jsonSchema")["tool"]
        self.assertEqual(schema["properties"]["element"]["type"], "object")
        self.lookup()
        self.assertEqual(len([r for r in self.provider.requests if r["operation"] == "describe"]), 2)
        self.assertEqual(len(self.provider.requests), calls + 1)

    def test_tool_description_exposes_unbounded_result_pricing_before_dispatch(self):
        self.start()
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                body["results"][0]["pricing"] = {"creditsPerUnit": .26, "unit": "result"}
            return body, code
        self.tools.execute = catalog
        ledger = budget.ledger_path(self.path).read_bytes()
        view = self.tools.inspect(tool="fixture_lookup")["tool"]
        self.assertEqual(view["pricing"]["creditsPerUnit"], .26)
        self.assertNotIn("reservation_preview", view)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), ledger)
        calls = len(self.provider.requests)
        self.assertEqual(self.tools.inspect(tool="fixture_lookup")["tool"], view)
        self.lookup(check(tool="fixture_lookup", inputs={"query": "company"}))
        self.assertEqual(len(self.provider.requests), calls + 1)
        self.assertEqual(budget.actual_cost_summary(budget.load_ledger(self.path))["provider_usd"], .02)

    def test_result_limits_do_not_create_monetary_reservations(self):
        self.start()
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                contract = body["results"][0]
                contract["pricing"] = {"creditsPerUnit": .2, "unit": "result"}
                contract["inputSchema"]["fields"].append({"name": "limit", "type": "integer"})
                contract["inputSchema"]["jsonSchema"]["properties"]["limit"] = {"type": "integer", "minimum": 1}
            return body, code
        self.tools.execute = catalog
        view = self.tools.inspect(tool="fixture_search")["tool"]
        self.assertNotIn("reservation_preview", view)
        result = self.lookup(check(tool="fixture_search", inputs={"query": "company", "limit": 2}))
        rid = result["lookups"][0]["route"]
        self.assertNotIn("maximum_credits", budget.load_ledger(self.path)["calls"][rid])
        self.assertNotIn("reservation_preview", self.tools.inspect(tool="harvestapi_get_company")["tool"])
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_result_billing_uses_reported_charge_without_agent_arithmetic(self):
        self.start()
        def catalog(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                contract = body["results"][0]
                contract["pricing"] = {"creditsPerUnit": .56, "unit": "result"}
                contract["inputSchema"]["fields"].append({"name": "page_size", "type": "integer"})
                contract["inputSchema"]["jsonSchema"]["properties"]["page_size"] = {"type": "integer", "minimum": 1}
            return body, code
        self.tools.execute = catalog
        inputs = {"query": "company", "page_size": 3}
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.lookup(check(tool="fixture_search", inputs=inputs, max_cost_credits=1))
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        result = self.lookup(check(tool="fixture_search", inputs=inputs))
        rid = result["lookups"][0]["route"]
        call = budget.load_ledger(self.path)["calls"][rid]
        self.assertNotIn("maximum_credits", call)
        self.assertEqual(float(call["actual_credits"]), self.provider.rate)
        self.assertEqual(budget.audit_ledger(self.path, json.loads(self.path.read_text())), [])

    def test_ordinary_input_guidance_is_complete_and_general_help_remains_compact(self):
        self.start()
        description = "Provider input context. " * 35 + "SQL must include LIMIT <= 100000."
        general_help = "General tool background. " * 1200
        enum = [f"category-{i}" for i in range(60)]
        full = {}
        def described(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                contract = body["results"][0]
                contract["description"] = general_help
                contract["inputSchema"]["description"] = description
                contract["inputSchema"]["fields"][0]["description"] = description
                contract["inputSchema"]["jsonSchema"].update(required=["url"], properties={
                    "url": {"type": "string", "description": description},
                    "category": {"type": "string", "enum": enum},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1}})
                full.update(copy.deepcopy(contract))
            return body, code
        self.tools.execute = described
        view = self.tools.inspect(tool="harvestapi_get_company", refresh=True)["tool"]
        self.assertLess(len(view["description"]), len(general_help) // 10)
        self.assertEqual(view["inputSchema"]["description"], description)
        self.assertEqual(view["inputSchema"]["fields"][0]["description"], description)
        self.assertEqual(view["inputSchema"]["jsonSchema"]["properties"]["url"]["description"], description)
        schema = view["inputSchema"]["jsonSchema"]
        self.assertEqual(schema["required"], ["url"])
        self.assertEqual(schema["properties"]["limit"], full["inputSchema"]["jsonSchema"]["properties"]["limit"])
        field = schema["properties"]["category"]["enum"]["detail_field"]
        self.assertEqual(self.tools.inspect(tool="harvestapi_get_company", field=field, offset=20)["tool"], enum[20:30])
        calls = len(self.provider.requests)
        # An explicit subtree request must expose all its constraints in one
        # response instead of forcing another inspection for every child field.
        whole = self.tools.inspect(tool="harvestapi_get_company", field="inputSchema")["tool"]
        self.assertEqual(whole, full["inputSchema"])
        fields = self.tools.inspect(tool="harvestapi_get_company", field="inputSchema.fields")["tool"]
        self.assertEqual(fields[0]["description"], description)
        whole["jsonSchema"]["required"].append("not_a_real_input")
        self.assertEqual(self.tools._description("harvestapi_get_company"), full)
        restored, offset = "", 0
        while offset is not None:
            page = self.tools.inspect(tool="harvestapi_get_company", field="inputSchema.fields.0.description", offset=offset)
            restored += page["tool"]
            offset = page["next_offset"]
        self.assertEqual(restored, description)
        self.assertEqual(self.tools._description("harvestapi_get_company"), full)
        with self.assertRaisesRegex(ValueError, "input.checks\\[0\\].inputs.*missing required fields: url"):
            self.lookup(check(inputs={"category": "category-50"}))
        self.assertEqual(len(self.provider.requests), calls)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        self.lookup()
        self.assertEqual(len(self.provider.requests), calls + 1)

    def test_large_input_descriptions_are_bounded_without_changing_saved_contract(self):
        self.start()
        description = "Category taxonomy. " * 1500 + "Use only the provider's documented categories."
        full = {}
        def described(request, capture):
            body, code = self.provider(request, capture)
            if request["operation"] == "describe":
                contract = body["results"][0]
                contract["inputSchema"]["fields"][0]["description"] = description
                contract["inputSchema"]["jsonSchema"].update(required=["url"], properties={
                    "url": {"type": "string", "description": description},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1}})
                full.update(copy.deepcopy(contract))
            return body, code
        self.tools.execute = described
        view = self.tools.inspect(tool="harvestapi_get_company", refresh=True)["tool"]
        schema = view["inputSchema"]["jsonSchema"]
        self.assertLess(len(json.dumps(view)), len(json.dumps(full)) / 3)
        self.assertIn("abridged guidance", schema["properties"]["url"]["description"])
        self.assertIn("inputSchema.jsonSchema.properties.url.description", schema["properties"]["url"]["description"])
        self.assertEqual(schema["required"], ["url"])
        self.assertEqual(schema["properties"]["limit"], full["inputSchema"]["jsonSchema"]["properties"]["limit"])
        calls = len(self.provider.requests)
        detail = self.tools.inspect(tool="harvestapi_get_company", field="inputSchema.jsonSchema.properties.url")["tool"]
        self.assertEqual(detail["description"], description)
        self.assertEqual(self.tools._description("harvestapi_get_company"), full)
        self.assertEqual(len(self.provider.requests), calls)
        with self.assertRaisesRegex(ValueError, "missing required fields: url"):
            self.lookup(check(inputs={"limit": 1}))
        self.assertEqual(len(self.provider.requests), calls)
        self.assertFalse(budget.load_ledger(self.path)["calls"])

    def test_reference_corrections_include_exact_input_path_and_saved_choices(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        row = {"target": "example.test", "decision": "hold_account", "reason": "Review fit",
               "account_fit": {"ref": "mistyped:0"}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for bad in ("mistyped:0", ref.rsplit(":", 1)[0] + ":999"):
            row["account_fit"]["ref"] = bad
            with self.assertRaises(ValueError) as error:
                self.tools.call("tyche_review", {"companies": [row]})
            self.assertIn("input.companies[0].account_fit.ref", str(error.exception))
            self.assertIn(ref, str(error.exception))
            self.assertIn("example.test", str(error.exception))
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        row["account_fit"]["ref"] = ref
        self.tools.call("tyche_review", {"companies": [row]})
        self.assertEqual(len(self.provider.requests), before[2])

    def test_reference_typo_suggests_older_receipt_across_scope_alias_without_resolving(self):
        self.start()
        ref = self.lookup(check("ExamplePay"))["lookups"][0]["results"][0]["ref"]
        for index in range(4):
            self.lookup(check(f"other-{index}.test"))
        rid = ref.split(":")[0]
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        for bad in (rid[:-1] + ":0", rid[:-2] + "xx:0"):
            with self.subTest(reference=bad), self.assertRaises(ValueError) as error:
                self.tools.call("tyche_review", {"companies": [{"target": "example.test",
                    "decision": "hold_account", "reason": "Check saved evidence", "account_fit": {"ref": bad}}]})
            self.assertIn(ref, str(error.exception))
            self.assertIn('"target": "examplepay"', str(error.exception))
            self.assertIn("no replacement was selected", str(error.exception))
            with self.assertRaises(ValueError) as inspection:
                self.tools.call("tyche_inspect", {"ref": bad})
            self.assertIn(ref, str(inspection.exception))
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.assertEqual(self.tools._reference_choices("unrelated-run:0", "missing.test"), [])

    def test_stale_web_alias_returns_saved_choices_without_resaving_or_spending(self):
        self.start()
        web = {"target": "example.test", "purpose": "Read company source", "query": "company source",
               "operation": "find", "response": {"status": "ok", "results": [
                   {"url": "https://example.test/about", "text": "The company provides verified business data services."}]}}
        saved = self.tools.review(web=[web])["web_references"]["web:0"] + ":0"
        # Later provider calls must not bury the source behind irrelevant profiles.
        for index in range(4):
            self.lookup(check(purpose=f"Inspect company observation {index}",
                              inputs={"url": f"https://www.linkedin.com/company/observation-{index}/"}))
        row = {"target": "example.test", "decision": "hold_account", "reason": "Review saved company source",
               "account_fit": {"ref": "web:0:0"}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        receipts = sorted(self.path.parent.joinpath("receipts").iterdir())
        for bad, attached in (("web:0:0", []), ("web:0:99", [web])):
            row["account_fit"]["ref"] = bad
            with self.assertRaises(ValueError) as error:
                self.tools.call("tyche_review", {"companies": [row], "web": attached})
            self.assertIn("input.companies[0].account_fit.ref", str(error.exception))
            self.assertIn(saved, str(error.exception))
            self.assertIn("https://example.test/about", str(error.exception))
            self.assertIn("no replacement was selected", str(error.exception))
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
            self.assertEqual(sorted(self.path.parent.joinpath("receipts").iterdir()), receipts)
        row["account_fit"]["ref"] = saved
        result = self.tools.call("tyche_review", {"companies": [row]})
        self.assertEqual(result["saved_companies"], ["example.test"])
        self.assertEqual(len(self.provider.requests), before[2])

    def test_input_corrections_show_valid_fields_and_leave_paid_work_untouched(self):
        self.start()
        before = self.path.read_bytes(), len(self.provider.requests)
        with self.assertRaisesRegex(ValueError, r"input.companies\[0\].*input.sources"):
            self.tools.call("tyche_review", {"companies": [{"target": "example.test", "decision": "hold_account",
                "reason": "Needs evidence", "sources": []}]})
        for invalid_limit in (0, -1, 1.5, True):
            with self.subTest(limit=invalid_limit), self.assertRaises(ValueError):
                self.tools.call("tyche_inspect", {"limit": invalid_limit})
        with self.assertRaisesRegex(ValueError, r"input.checks has 4 items; allowed count: 1–3"):
            self.lookup(*[check() for _ in range(4)])
        with self.assertRaisesRegex(ValueError, r"input.checks\[0\].inputs.*wrong.*allowed fields: \['url'\]"):
            self.lookup(check(inputs={"url": "https://example.test", "wrong": "value"}))
        with self.assertRaisesRegex(ValueError, r"input.field.*stop_audit.route_frontier"):
            self.tools.call("tyche_inspect", {"field": "route_frontier"})
        self.assertEqual((self.path.read_bytes(), len(self.provider.requests)), before)
        self.assertFalse(budget.load_ledger(self.path)["calls"])

    def test_web_observation_shape_errors_are_precise_and_do_not_save_partial_work(self):
        self.start()
        before = self.path.read_bytes()
        receipts = sorted(self.path.parent.joinpath("receipts").iterdir())
        observation = {"target": "example.test", "purpose": "Review source", "query": "source query",
                       "status": "ok", "results": [{"url": "https://example.test/news", "text": "Observed source"}]}
        with self.assertRaisesRegex(ValueError, r"input.web\[0\].response.results"):
            self.tools.call("tyche_review", {"web": [observation]})
        observation["response"] = {"status": observation.pop("status"), "results": observation.pop("results")}
        for bad_results, expected in (("Pasted tool transcript", r"input.web\[0\].response.results requires array"),
                                      (["Pasted source"], r"input.web\[0\].response.results\[0\] requires object")):
            observation["response"]["results"] = bad_results
            with self.assertRaisesRegex(ValueError, expected):
                self.tools.review(web=[observation])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(self.path.parent.joinpath("receipts").iterdir()), receipts)
        self.assertFalse(budget.load_ledger(self.path)["calls"])
        observation["response"]["results"] = [{"url": "https://example.test/news", "text": "Observed source"}]
        result = self.tools.review(web=[observation])
        self.assertIn("web:0", result["web_references"])
        self.assertEqual(self.tools.inspect(ref=result["web_references"]["web:0"] + ":0")["facts"]["text"], "Observed source")

    def test_invalid_web_status_reports_allowed_values_before_saving_any_observation(self):
        self.start()
        web = {"target": "example.test", "purpose": "Review source", "query": "https://example.test/news",
               "operation": "open", "response": {"status": "ok", "results": [
                   {"url": "https://example.test/news", "text": "Observed source"}]}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()
        receipts = sorted(self.path.parent.joinpath("receipts").iterdir())
        for invalid in ("success", "pending", "unknown"):
            other = copy.deepcopy(web)
            other["response"]["status"] = invalid
            with self.subTest(status=invalid), self.assertRaisesRegex(ValueError, r"input.web\[1\].response.status.*ok"):
                self.tools.call("tyche_review", {"web": [web, other]})
            self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes()), before)
            self.assertEqual(sorted(self.path.parent.joinpath("receipts").iterdir()), receipts)
        self.assertEqual(len(self.tools.review(web=[web])["web_references"]), 1)

    def test_catalog_pages_show_usable_tools_and_keep_original_evidence_indices(self):
        self.start()
        rows = [{"toolId": "monitor", "callable": False, "deployCommand": "monitor setup"}] * 10
        rows += [{"toolId": f"research_{i}", "callable": True, "description": f"Research capability {i}",
                  "usageGuidance": "SDK material"} for i in range(12)]
        def search(request, capture):
            body, code = self.provider(request, capture)
            body["results"] = copy.deepcopy(rows)
            return body, code
        self.tools.execute = search
        view = self.tools.inspect(query="research")
        rid = view["route"]
        self.assertEqual(view["result_count"], 12)
        self.assertEqual(view["non_callable_count"], 10)
        self.assertEqual(view["results"][0]["ref"], f"{rid}:10")
        self.assertNotIn("usageGuidance", view["results"][0]["facts"])
        calls = len(self.provider.requests)
        page = self.tools.inspect(ref=rid, offset=view["next_offset"])
        self.assertEqual([r["ref"] for r in page["results"]], [f"{rid}:20", f"{rid}:21"])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(self.tools.inspect(ref=f"{rid}:21")["facts"], rows[21])
        self.assertEqual(runner.read_receipt(self.path, rid)["result"]["results"], rows)
        self.assertEqual(len(self.provider.requests), calls)
        rows[:] = rows[:10]
        view = self.tools.inspect(query="no match")
        self.assertEqual(view["results"], [])
        self.assertIn("No callable tools matched", view["catalog_note"])

    def test_inspection_selects_company_and_run_fields_instead_of_ignoring_them(self):
        self.start()
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Needs funding evidence",
                                     "company": {"canonical_name": "ExamplePay"}, "intent_details": "Saved research paragraph."}])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        selected = self.tools.inspect(target="example.test", field="company.candidate.canonical_name")
        self.assertEqual(selected, {"value": "ExamplePay"})
        self.assertEqual(self.tools.inspect(target="example.test", field="candidate.canonical_name"), selected)
        self.assertEqual(self.tools.call("tyche_inspect", {"target": "example.test", "field": "intent_details"}),
                         {"value": "Saved research paragraph."})
        self.assertEqual(self.tools.inspect(target="example.test", field="route_count"), {"value": 0})
        with self.assertRaisesRegex(ValueError, "Unknown company field.*saved fields"):
            self.tools.inspect(target="example.test", field="missing")
        with self.assertRaisesRegex(ValueError, "Unknown company field"):
            self.tools.inspect(target="missing.test", field="intent_details")
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        self.assertEqual(self.tools.inspect(field="stop_check.started_at")["value"],
                         json.loads(self.path.read_text())["stop_check"]["started_at"])
        with self.assertRaisesRegex(ValueError, "available fields"):
            self.tools.inspect(field="nonexistent")

    def test_progress_reports_required_gaps_without_promoting_optional_hiring(self):
        self.start()
        checks = [{"criterion": name, "importance": importance, "status": "unknown", "claim": "Needs research", "evidence": []}
                  for name, importance in [("current funding stage", "required"), ("hiring bonus", "preferred")]]
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account", "reason": "Funding is unresolved",
                                     "company": {"canonical_name": "ExamplePay"}, "qualification_checks": checks}])
        missing = self.tools.inspect()["companies"][0]["missing"]
        self.assertIn("current funding stage", missing)
        self.assertFalse(any("HIRING" in item for item in missing))
        self.assertTrue(any("geographies" in item for item in missing))
        self.assertEqual(self.tools.inspect(target="example.test", field="qualification_checks")["value"], checks)

    def test_selected_run_fields_page_without_repeating_or_changing_state(self):
        self.start()
        document = json.loads(self.path.read_text())
        roles = [f"Requested role {i}" for i in range(22)]
        document["request"]["requested_roles"] = roles
        self.path.write_text(json.dumps(document))
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        found, offset = [], 0
        while offset is not None:
            page = self.tools.call("tyche_inspect", {"field": "request.requested_roles", "offset": offset})
            self.assertEqual(page["total"], len(roles))
            found.extend(page["value"])
            offset = page["next_offset"]
        self.assertEqual(found, roles)
        self.assertEqual(self.tools.inspect(field="request.requested_roles", offset=99),
                         {"value": [], "total": 22, "next_offset": None})
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_company_fields_page_full_text_and_source_history(self):
        self.start()
        narrative = "Supported company context. " * 160
        self.tools.review(companies=[{"target": "example.test", "decision": "hold_account",
            "reason": "Research pending", "intent_details": narrative}])
        self.tools.review(web=[{"target": "example.test", "purpose": f"Review company fact {i}", "query": str(i),
            "response": {"status": "ok", "results": [{"url": f"https://example.test/{i}", "text": f"Source {i}"}]}}
            for i in range(12)])
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        found, offset = "", 0
        while offset is not None:
            page = self.tools.inspect(target="example.test", field="intent_details", offset=offset)
            found += page["value"]
            offset = page["next_offset"]
        self.assertEqual(found, narrative)
        first = self.tools.inspect(target="example.test", field="recent_sources")
        last = self.tools.inspect(target="example.test", field="recent_sources", offset=first["next_offset"])
        self.assertEqual(first["total"], 12)
        self.assertEqual(len(last["value"]), 2)
        self.assertIsNone(last["next_offset"])
        self.assertFalse({r["ref"] for r in first["value"]} & {r["ref"] for r in last["value"]})
        self.assertEqual(self.tools.inspect(target="example.test")["recent_sources"],
                         (first["value"] + last["value"])[-10:])
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)

    def test_recorded_provider_error_explains_recovery_without_releasing_unknown_cost(self):
        self.start()
        calls = []
        def failing(request, capture):
            if request["operation"] != "execute":
                return self.provider(request, capture)
            calls.append(request)
            def dispatch():
                raw = {"exit_code": 1, "body": {"error": "Bad request"}, "stderr": ""}
                capture(raw)
                return deepline.normalize_response(request, raw)
            return budget.guarded_call(request, "deepline", dispatch)
        self.tools.execute = failing
        found = self.lookup()["lookups"][0]
        self.assertTrue(found["recorded"])
        self.assertEqual(runner.read_receipt(self.path, found["route"])["result"]["receipt_status"], "complete")
        self.assertIn("cannot resolve unknown billing", found["recovery_note"])
        before = budget.ledger_path(self.path).read_bytes()
        self.tools.inspect(recover=found["route"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.ledger_path(self.path).read_bytes(), before)
        self.assertIsNone(budget.load_ledger(self.path)["calls"][found["route"]]["actual_credits"])

    def test_sandbox_relay_requires_host_metadata_and_refuses_policy_drift(self):
        relay = SandboxedTools(self.path)
        with patch("tyche_tools.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "sandbox metadata"):
                relay.call("tyche_inspect", {}, {})
            state = {"permissionProfile": {"mode": "read-only"}, "sandboxCwd": self.path.parent.as_uri()}
            with self.assertRaisesRegex(ValueError, "differs"):
                relay.call("tyche_inspect", {}, {"codex/sandbox-state-meta": state})
            state["sandboxCwd"] = Path(__file__).resolve().parents[4].as_uri()
            relay.state = {**state, "permissionProfile": {"mode": "restricted"}}
            with self.assertRaisesRegex(ValueError, "Sandbox changed"):
                relay.call("tyche_inspect", {}, {"codex/sandbox-state-meta": state})
            launch.assert_not_called()

    def test_web_review_is_one_handoff_and_retry_preserves_receipt(self):
        self.start()
        web = {"target": "example.test", "purpose": "Read product", "query": "https://example.test/product",
            "operation": "open", "response": {"status": "ok", "results": [{"url": "https://example.test/product",
                "text": "ExamplePay provides merchant payment processing."}]}}
        company = {"target": "example.test", "decision": "hold_account", "reason": "Funding still needs review",
                   "company": {"canonical_name": "ExamplePay"},
                   "account_fit": {"ref": "web:0:0", "date_basis": "observed_current"}}
        result = self.tools.review(companies=[company], web=[web])
        rid = result["web_references"]["web:0"]
        before = (self.path.parent / "receipts" / (rid + ".json")).read_bytes()
        self.tools.review(companies=[company], web=[web], sources=[{"ref": "web:0", "state": "exhausted", "reason": "Page reviewed"}])
        self.assertEqual((self.path.parent / "receipts" / (rid + ".json")).read_bytes(), before)
        self.assertEqual(len([r for r in json.loads(self.path.read_text())["routes"] if r["provider"] == "public_web"]), 1)
        web["response"]["results"][0]["text"] += " Its customers include retailers."
        later = self.tools.review(web=[web])["web_references"]["web:0"]
        self.assertNotEqual(later, rid)
        self.assertEqual((self.path.parent / "receipts" / (rid + ".json")).read_bytes(), before)
        self.assertEqual(self.tools.review(web=[web])["web_references"]["web:0"], later)
        self.assertEqual(len([r for r in json.loads(self.path.read_text())["routes"] if r["provider"] == "public_web"]), 2)

    def test_raw_receipt_authority_and_foreign_reference_rejection(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        receipt = self.path.parent / "receipts" / (ref.split(":")[0] + ".json")
        saved = json.loads(receipt.read_text())
        saved["results"][0]["employee_range"] = "10,001+"
        receipt.write_text(json.dumps(saved))
        self.assertEqual(self.tools.inspect(ref=ref)["facts"]["employee_range"], "51-200")
        saved["run_fingerprint"] = "foreign-run"
        receipt.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, "another run"):
            self.tools.inspect(ref=ref)

    def test_conflicting_evidence_source_returns_the_selected_receipt_and_url(self):
        self.start()
        ref = self.lookup()["lookups"][0]["results"][0]["ref"]
        row, source, _ = self.tools._resolve(ref)
        company = {"target": "example.test", "decision": "hold_account", "reason": "Funding needs research",
                   "company": {"canonical_name": "ExamplePay"},
                   "account_fit": {"ref": ref, "source": {"provider": "public_web", "route_id": "wrong"},
                                   "evidence_url": "https://example.test/wrong-page"}}
        before = self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)
        with self.assertRaises(ValueError) as error:
            self.tools.review(companies=[company])
        message = str(error.exception)
        for expected in (ref, ".source", source["tool"], source["route_id"], row["company_linkedin_url"], "Omit source"):
            self.assertIn(expected, message)
        self.assertEqual((self.path.read_bytes(), budget.ledger_path(self.path).read_bytes(), len(self.provider.requests)), before)
        company["account_fit"] = {"ref": ref}
        self.tools.review(companies=[company])
        self.assertEqual(len(self.provider.requests), before[2])

    def test_readonly_transport_lists_tools_and_refuses_mutation(self):
        stream = io.StringIO('\n'.join(json.dumps(m) for m in [
            {"id": 1, "method": "initialize"}, {"id": 2, "method": "tools/list"}]) + '\n')
        output = io.StringIO()
        serve(ResearchTools(self.path, readonly=True), stream, output)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(messages[1]["result"]["tools"]), 6)
        session = ResearchTools(self.path, readonly=True)
        self.assertEqual(session.call("tyche_inspect", {})["status"], "not_started")
        with self.assertRaisesRegex(ValueError, "read-only"):
            session.call("tyche_start", {"request": self.request})




if __name__ == "__main__":
    unittest.main()
