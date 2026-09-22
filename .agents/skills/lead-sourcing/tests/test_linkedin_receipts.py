import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_client_output import client_document, VALIDATOR
from test_export_xlsx import EXPORTER_PATH, read_first_sheet_rows
from linkedin_fixtures import write_linkedin_receipts
from linkedin_receipts import linkedin_receipt_errors
import run_attempt


class LinkedInReceiptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "results.json"
        self.doc = client_document()
        self.doc["stop_check"] = {"started_at": "2026-09-01T12:34:56Z", "next_actions": []}
        self.doc["stop_audit"] = {"frontier_complete": True, "route_frontier": [
            {"route_id": r["route_id"], "state": "exhausted", "reason": "Evidence reviewed.",
             "exhaustion_basis": "no_new_unique_candidates"} for r in self.doc["routes"]]}
        self.doc["budget"] = {"limits": {"deepline_credits": 5, "scrapingdog_credits": 0}}
        run_attempt.refresh(self.doc)
        self.path.write_text(json.dumps(self.doc))
        write_linkedin_receipts(self.path, self.doc)
        self.path.write_text(json.dumps(self.doc))

    def receipt_path(self, company=False):
        entity = self.doc["accepted"][0]["company" if company else "primary_contact"]
        evidence = entity["employee_range_evidence" if company else "location_evidence"]
        return self.path.parent / "receipts" / (evidence["source"]["route_id"] + ".json")

    def strict_check(self):
        result = subprocess.run([sys.executable, VALIDATOR.__file__, str(self.path)],
                                capture_output=True, text=True, timeout=10)
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_review_fills_missing_values_and_strict_delivery_reads_saved_result(self):
        row = copy.deepcopy(self.doc["accepted"][0])
        row["company"].pop("employee_range")
        for field in ("country", "state", "city"):
            row["primary_contact"].pop(field)
        receipts_before = {p: p.read_bytes() for p in self.path.parent.glob("receipts/*.json")}
        with patch("deepline.run", side_effect=AssertionError("no provider call allowed")):
            run_attempt.save_review(self.path, {"companies": [{"state": "accepted", "row": row}]})
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["accepted"], self.doc["accepted"])
        self.assertEqual({p: p.read_bytes() for p in receipts_before}, receipts_before)
        before = self.path.read_bytes()
        code, result = self.strict_check()
        self.assertEqual(code, 0, result)
        self.assertTrue(result["delivery_allowed"])
        self.assertEqual(self.path.read_bytes(), before)
        node = os.environ.get("TYCHE_WORKSPACE_NODE") or shutil.which("node")
        node_modules = os.environ.get("TYCHE_WORKSPACE_NODE_MODULES")
        if node and node_modules:
            destination = self.path.parent / "leads.xlsx"
            exported = subprocess.run([node, str(EXPORTER_PATH), str(self.path), str(destination),
                "--node-modules", node_modules], capture_output=True, text=True, timeout=90)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            cells = read_first_sheet_rows(destination)[1]
            self.assertEqual(cells[9:12], ["Columbus", "Ohio", "United States"])
            self.assertEqual(cells[14], "201-500")

    def test_forged_values_fail_review_strict_validation_and_workbook_export(self):
        node = os.environ.get("TYCHE_WORKSPACE_NODE") or shutil.which("node")
        for owner, field, value in (("company", "employee_range", "11-50"),
                                    ("primary_contact", "country", "Canada"),
                                    ("primary_contact", "city", "Toronto"),
                                    ("primary_contact", "state", "Ontario")):
            with self.subTest(field=field):
                document = copy.deepcopy(self.doc)
                row = document["accepted"][0]
                row[owner][field] = value
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, field + " must match"):
                    run_attempt.save_review(self.path, {"companies": [{"state": "accepted", "row": row}]})
                self.assertEqual(self.path.read_bytes(), before)
                self.path.write_text(json.dumps(document))
                code, output = self.strict_check()
                self.assertEqual(code, 2)
                self.assertFalse(output["delivery_allowed"])
                self.assertIn(field + " must match", " ".join(output["errors"]))
                if node:
                    destination = self.path.parent / "leads.xlsx"
                    destination.write_bytes(b"existing workbook")
                    result = subprocess.run([node, str(EXPORTER_PATH), str(self.path), str(destination),
                        "--node-modules", "unused-for-rejected-input"], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(field + " must match", result.stderr)
                    self.assertEqual(destination.read_bytes(), b"existing workbook")
                self.path.write_text(json.dumps(self.doc))

    def test_missing_foreign_failed_or_wrong_entity_receipts_cannot_supply_values(self):
        path = self.receipt_path()
        original = path.read_bytes()
        for mutation in ("missing", "foreign", "request", "pending", "failed", "raw_failure", "wrong_entity", "no_body"):
            with self.subTest(mutation=mutation):
                receipt = json.loads(original)
                if mutation == "missing": path.unlink()
                else:
                    if mutation == "foreign": receipt["run_fingerprint"] = "another-run"
                    elif mutation == "request": receipt["request_fingerprint"] = "another-request"
                    elif mutation == "pending": receipt["receipt_status"] = "pending"
                    elif mutation == "failed": receipt["status"] = "rate_limited"
                    elif mutation == "raw_failure": receipt["provider_response"]["body"]["status"] = "provider_error"
                    elif mutation == "wrong_entity": receipt["provider_response"]["body"]["element"]["linkedinUrl"] += "-other"
                    else: receipt.pop("provider_response")
                    path.write_text(json.dumps(receipt))
                self.assertTrue(linkedin_receipt_errors(self.doc, self.path), mutation)
                path.write_bytes(original)

    def test_raw_response_is_authoritative_not_normalized_results_or_member_count(self):
        path = self.receipt_path(company=True)
        receipt = json.loads(path.read_text())
        profile = receipt["provider_response"]["body"]["element"]
        profile["employeeCount"] = 12
        receipt["results"] = [{"employee_range": "11-50", "linkedinUrl": profile["linkedinUrl"]}]
        path.write_text(json.dumps(receipt))
        self.assertEqual(linkedin_receipt_errors(self.doc, self.path), [])
        profile.pop("employeeCountRange")
        profile["employee_range"] = "201-500"
        path.write_text(json.dumps(receipt))
        self.assertTrue(linkedin_receipt_errors(self.doc, self.path))

    def test_profile_city_conflict_uses_saved_linkedin_text_without_new_lookup(self):
        path = self.receipt_path()
        receipt = json.loads(path.read_text())
        receipt["provider_response"]["body"]["element"]["location"] = {
            "linkedinText": "Litchfield Park, Arizona, United States",
            "parsed": {"countryFull": "United States", "state": "Arizona", "city": "Yuma"}}
        path.write_text(json.dumps(receipt))
        before = path.read_bytes()
        contact = self.doc["accepted"][0]["primary_contact"]
        contact.update(state="Arizona", city="Yuma")
        with patch("deepline.run", side_effect=AssertionError("no provider call allowed")):
            self.assertIn("city must match", " ".join(linkedin_receipt_errors(self.doc, self.path)))
            contact.pop("city")
            self.assertEqual(linkedin_receipt_errors(self.doc, self.path, fill_missing=True), [])
        self.assertEqual(contact["city"], "Litchfield Park")
        self.assertEqual(path.read_bytes(), before)

    def test_supported_aliases_formatting_and_optional_location(self):
        path = self.receipt_path()
        receipt = json.loads(path.read_text())
        location = receipt["provider_response"]["body"]["element"]["location"]
        location["parsed"]["country"] = "US"
        location["countryCode"] = "US"
        location["parsed"].pop("city")
        location["parsed"].pop("state")
        path.write_text(json.dumps(receipt))
        row = self.doc["accepted"][0]
        row["company"]["employee_range"] = "201–500"
        row["primary_contact"].update(country="US")
        row["primary_contact"].pop("city")
        row["primary_contact"].pop("state")
        before = copy.deepcopy(self.doc)
        self.assertEqual(linkedin_receipt_errors(self.doc, self.path, fill_missing=True), [])
        self.assertEqual(self.doc, before)
        row["primary_contact"]["city"] = "Columbus"
        self.assertTrue(linkedin_receipt_errors(self.doc, self.path))

    def test_backups_must_also_match_their_saved_profile(self):
        backup = copy.deepcopy(self.doc["accepted"][0]["primary_contact"])
        backup["country"] = "Canada"
        self.doc["accepted"][0]["backup_contacts"] = [backup]
        errors = linkedin_receipt_errors(self.doc, self.path)
        self.assertIn("backup_contacts[0].country", " ".join(errors))

    def save_profile(self, contact, positions, experience=()):
        """Give a fixture profile the name and explicit roles HarvestAPI returns."""
        path = self.path.parent / "receipts" / (contact["location_evidence"]["source"]["route_id"] + ".json")
        receipt = json.loads(path.read_text())
        first, _, last = contact["full_name"].partition(" ")
        receipt["provider_response"]["body"]["element"].update(
            linkedinUrl=contact["linkedin_url"], firstName=first, lastName=last,
            currentPositions=list(positions), experience=list(experience))
        path.write_text(json.dumps(receipt))

    def test_each_buyer_needs_their_own_current_role_at_the_selected_company(self):
        from linkedin_receipts import contact_verification_errors
        row = self.doc["accepted"][0]
        company, first = row["company"], row["primary_contact"]
        # A second buyer with an independent profile receipt, as two-contact requests save them.
        second = copy.deepcopy(first)
        second.update(full_name="Ben Example", current_title="Decoration Buyer", role_match="approved_family",
                      linkedin_url="https://www.linkedin.com/in/ben-example", email="ben@example.com")
        source = dict(first["location_evidence"]["source"], route_id="harvest-fields-second-buyer")
        second["location_evidence"] = dict(first["location_evidence"], evidence_url=second["linkedin_url"], source=source)
        route = next(r for r in self.doc["routes"] if r["route_id"] == first["location_evidence"]["source"]["route_id"])
        self.doc["routes"].append(dict(route, route_id=source["route_id"], request_fingerprint="second-buyer"))
        receipt = json.loads(self.receipt_path().read_text())
        (self.path.parent / "receipts" / (source["route_id"] + ".json")).write_text(
            json.dumps(dict(receipt, route_id=source["route_id"], request_fingerprint="second-buyer")))
        row["backup_contacts"] = [second]

        def role(person, **changes):
            return {"companyName": company["canonical_name"], "companyLinkedinUrl": company["linkedin_url"],
                    "position": person["current_title"], "current": True, **changes}
        def check(person):
            return " ".join(contact_verification_errors(self.doc, self.path, company, person))
        for person in (first, second):
            self.save_profile(person, [role(person)])
            self.assertEqual(check(person), "")
        ended = {"endDate": {"text": "Dec 2023"}, "current": False}
        elsewhere = "https://www.linkedin.com/company/another-retailer"
        cases = (
            # The LinkedIn entity identifies the employer; a parent's display name on the same page still matches.
            ("parent name, same company page", [role(second, companyName="Example Group Purchasing")], (), ""),
            ("another employer", [role(second, companyName="Another Retailer", companyLinkedinUrl=elsewhere)], (), "current company"),
            ("network parent page", [role(second, companyLinkedinUrl="https://www.linkedin.com/company/example-group")], (), "current company"),
            ("left the company", [role(second, companyName="Another Retailer", companyLinkedinUrl=elsewhere)],
             [role(second, **ended)], "current company"),
            ("stale title", [role(second, position="Garden Tools Buyer")], [role(second, **ended)], "current_title must match"),
        )
        for name, positions, experience, expected in cases:
            with self.subTest(name):
                self.save_profile(second, positions, experience)
                self.assertIn(expected, check(second)) if expected else self.assertEqual(check(second), "")
                # The first buyer's own receipt is untouched by the second buyer's profile.
                self.assertEqual(check(first), "")
                # Saved output carries the same proof: delivery validation rechecks every contact with an email.
                delivery = " ".join(e for e in linkedin_receipt_errors(self.doc, self.path) if "backup_contacts[0]" in e)
                self.assertIn(expected, delivery) if expected else self.assertEqual(delivery, "")
        # A pending profile without an email is not saved contact output and stays outside this check.
        pending = {k: v for k, v in second.items() if k not in {"email", "email_validation", "email_source"}}
        row["backup_contacts"] = [pending]
        self.assertFalse([e for e in linkedin_receipt_errors(self.doc, self.path) if "current" in e])

    def test_strict_delivery_rejects_a_saved_contact_that_no_longer_matches_its_profile(self):
        row = self.doc["accepted"][0]
        company, contact = row["company"], row["primary_contact"]
        self.save_profile(contact, [{"companyName": company["canonical_name"], "companyLinkedinUrl": company["linkedin_url"],
                                     "position": contact["current_title"], "current": True}])
        self.path.write_text(json.dumps(self.doc))
        code, result = self.strict_check()
        self.assertEqual((code, result["errors"]), (0, []))
        for field, value, expected in (("full_name", "Someone Else", "full_name must match the saved current LinkedIn profile"),
                                       ("current_title", "Previous Role", "current_title must match the saved current LinkedIn profile")):
            with self.subTest(field=field):
                saved = copy.deepcopy(self.doc)
                saved["accepted"][0]["primary_contact"][field] = value
                self.path.write_text(json.dumps(saved))
                code, result = self.strict_check()
                self.assertEqual(code, 2)
                self.assertFalse(result["delivery_allowed"])
                self.assertIn(expected, " ".join(result["errors"]))
        # Older saved contacts carry their profile URL only in their evidence; they still resolve at delivery.
        legacy = copy.deepcopy(self.doc)
        legacy["accepted"][0]["primary_contact"].pop("linkedin_url")
        self.assertFalse([e for e in linkedin_receipt_errors(legacy, self.path) if "profile" in e.casefold()])
        legacy["accepted"][0]["primary_contact"]["full_name"] = "Someone Else"
        self.assertIn("full_name must match", " ".join(linkedin_receipt_errors(legacy, self.path)))
        # The fallback never masks a saved LinkedIn URL that points at someone else's profile.
        other = copy.deepcopy(self.doc)
        other["accepted"][0]["primary_contact"]["linkedin_url"] = "https://www.linkedin.com/in/someone-else"
        self.assertIn("Verify the selected person's LinkedIn profile", " ".join(linkedin_receipt_errors(other, self.path)))
        # Malformed evidence is an unverified profile, never a crash.
        broken = copy.deepcopy(self.doc)
        broken["accepted"][0]["primary_contact"]["location_evidence"] = "not an object"
        self.assertIn("Verify the selected person's LinkedIn profile", " ".join(linkedin_receipt_errors(broken, self.path)))

    def test_exported_link_must_be_the_saved_profile_url_not_another_page_or_an_opaque_id(self):
        """The saved link is what the workbook shows. Saved outputs once carried a member id or a website page there."""
        row = self.doc["accepted"][0]
        company, contact = row["company"], row["primary_contact"]
        self.save_profile(contact, [{"companyName": company["canonical_name"], "companyLinkedinUrl": company["linkedin_url"],
                                     "position": contact["current_title"], "current": True}])
        self.path.write_text(json.dumps(self.doc))
        self.assertEqual(self.strict_check()[1]["errors"], [])
        for label, link, message in (("website page", "https://example.test/team/ada-example", "the saved LinkedIn link must be the same LinkedIn /in/ URL"),
                                     ("opaque member id", "https://www.linkedin.com/in/ACoAAAExampleOpaqueMemberIdxxxxxxxx", "member id is lookup input, not a profile link"),
                                     ("company page", company["linkedin_url"], "the saved LinkedIn link must be the same LinkedIn /in/ URL")):
            with self.subTest(link=label):
                saved = copy.deepcopy(self.doc)
                saved["accepted"][0]["primary_contact"]["linkedin_url"] = link
                saved["accepted"][0]["primary_contact"]["contact_url"] = link
                self.path.write_text(json.dumps(saved))
                code, result = self.strict_check()
                self.assertEqual((code, result["delivery_allowed"]), (2, False))
                self.assertIn(message, " ".join(result["errors"]))
                self.assertIn("exactly one matching LinkedIn entity", " ".join(linkedin_receipt_errors(saved, self.path)))
        # A member id is lookup input, never output: refused in the saved link or in the evidence itself.
        member = "https://www.linkedin.com/in/ACwAAB3g9uUBp7dK2C9mqJ4w7mN1q3xEyLXqZbo"
        for label, apply in (("member id as saved link", lambda pc: pc.update(linkedin_url=member, contact_url=member)),
                             ("member id as link and evidence", lambda pc: (pc.update(linkedin_url=member, contact_url=member), pc["location_evidence"].update(evidence_url=member)))):
            with self.subTest(link=label):
                saved = copy.deepcopy(self.doc)
                apply(saved["accepted"][0]["primary_contact"])
                self.path.write_text(json.dumps(saved))
                code, result = self.strict_check()
                self.assertEqual((code, result["delivery_allowed"]), (2, False))
                self.assertIn("member id is lookup input, not a profile link", " ".join(result["errors"]))
        # With no saved link, a contact_url on LinkedIn that is not this person's profile is refused too.
        saved = copy.deepcopy(self.doc)
        saved["accepted"][0]["primary_contact"].pop("linkedin_url")
        saved["accepted"][0]["primary_contact"]["contact_url"] = company["linkedin_url"]
        self.path.write_text(json.dumps(saved))
        code, result = self.strict_check()
        self.assertEqual((code, result["delivery_allowed"]), (2, False))
        self.assertIn("the saved LinkedIn link must be the same LinkedIn /in/ URL", " ".join(result["errors"]))
        # A public slug that merely contains digits or capitals is not a member id.
        for slug in ("michael-merino-4b68047", "ACME-Buyer-2024", "emmanuel-bertail-734135146",
                     "ACME-Corporation-Procurement-Director-2024", "ACcounting-and-Finance-Leader-Boston-MA"):
            saved = copy.deepcopy(self.doc)
            link = "https://www.linkedin.com/in/" + slug
            saved["accepted"][0]["primary_contact"].update(linkedin_url=link, contact_url=link)
            saved["accepted"][0]["primary_contact"]["location_evidence"]["evidence_url"] = link
            self.path.write_text(json.dumps(saved))
            self.assertNotIn("member id", " ".join(self.strict_check()[1]["errors"]), slug)
        # The same rule now covers the company's saved link, which the workbook also shows verbatim.
        saved = copy.deepcopy(self.doc)
        saved["accepted"][0]["company"]["linkedin_url"] = "https://example.test/about"
        self.path.write_text(json.dumps(saved))
        code, result = self.strict_check()
        self.assertEqual((code, result["delivery_allowed"]), (2, False))
        self.assertIn("the saved LinkedIn link must be the same LinkedIn /company/ URL", " ".join(result["errors"]))

    def test_exported_contact_without_requested_email_still_needs_a_current_role_at_the_company(self):
        # The request opts out of contact data, so no email gate ever ran for this exported contact.
        self.doc["request"]["contact_fields"] = []
        contact = self.doc["accepted"][0]["primary_contact"]
        for key in ("email", "email_validation", "email_source"):
            contact.pop(key, None)
        self.doc["routes"] = [r for r in self.doc["routes"] if r.get("phase") != "email_validation"]
        saved = {r["route_id"] for r in self.doc["routes"]}
        frontier = self.doc["stop_audit"]["route_frontier"]
        self.doc["stop_audit"]["route_frontier"] = [f for f in frontier if f["route_id"] in saved]
        run_attempt.refresh(self.doc)
        genuine = self.receipt_path().read_bytes()
        node, modules = os.environ.get("TYCHE_WORKSPACE_NODE"), os.environ.get("TYCHE_WORKSPACE_NODE_MODULES")

        def deliver(document, receipt=genuine):
            self.receipt_path().write_bytes(receipt)
            self.path.write_text(json.dumps(document))
            code, result = self.strict_check()
            rows = None
            if node and modules:
                workbook = self.path.parent / "leads.xlsx"
                workbook.unlink(missing_ok=True)
                exported = subprocess.run([node, str(EXPORTER_PATH), str(self.path), str(workbook),
                    "--node-modules", modules], capture_output=True, text=True, timeout=90)
                rows = len(read_first_sheet_rows(workbook)) - 1 if exported.returncode == 0 else 0
            return code, " ".join(result["errors"]), rows

        self.assertIn(deliver(self.doc), ((0, "", 1), (0, "", None)))  # A valid no-email contact stays deliverable.
        elsewhere = json.loads(genuine)
        elsewhere["provider_response"]["body"]["element"]["currentPositions"] = [{
            "companyName": "Another Retailer", "companyLinkedinUrl": "https://www.linkedin.com/company/another-retailer",
            "position": contact["current_title"], "current": True}]
        for name, field, value, receipt, expected in (
                ("name", "full_name", "Someone Else", genuine, "full_name must match"),
                ("title", "current_title", "Previous Role", genuine, "current_title must match"),
                ("employer", None, None, json.dumps(elsewhere).encode(), "current company")):
            with self.subTest(name):
                document = copy.deepcopy(self.doc)
                if field:
                    document["accepted"][0]["primary_contact"][field] = value
                code, errors, rows = deliver(document, receipt)
                self.assertEqual(code, 2)
                self.assertIn(expected, errors)
                self.assertIn(rows, (0, None))  # The saved workbook is never written for it.
        # An extra profile that is not exported stays outside the check and does not disturb delivery.
        pending = copy.deepcopy(self.doc)
        extra = {k: v for k, v in contact.items() if k != "current_title"}
        pending["accepted"][0]["backup_contacts"] = [dict(extra, full_name="Pending Person")]
        self.assertIn(deliver(pending), ((0, "", 1), (0, "", None)))
        # An exported backup without an email is checked like the primary.
        backup = copy.deepcopy(self.doc)
        backup["accepted"][0]["backup_contacts"] = [dict(contact, current_title="Previous Role")]
        errors = " ".join(linkedin_receipt_errors(backup, self.path))
        self.assertIn("backup_contacts[0]: current_title must match", errors)
        # Malformed contact fields are the request contract's to report; this check must not raise.
        malformed = copy.deepcopy(self.doc)
        malformed["request"]["contact_fields"] = [["email"]]
        self.assertEqual(linkedin_receipt_errors(malformed, self.path), [])


if __name__ == "__main__":
    unittest.main()
