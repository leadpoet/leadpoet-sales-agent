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
from linkedin_receipts import email_identity_fields, linkedin_receipt_errors
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
            self.assertIn("201-500", cells)

    def test_forged_values_fail_review_strict_validation_and_workbook_export(self):
        node = os.environ.get("TYCHE_WORKSPACE_NODE") or shutil.which("node")
        for owner, field, value in (("company", "employee_range", "11-50"),):
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
        path = self.receipt_path(company=True)
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




    def save_profile(self, contact, positions, experience=()):
        """Give a fixture profile the name and explicit roles HarvestAPI returns."""
        path = self.path.parent / "receipts" / (contact["location_evidence"]["source"]["route_id"] + ".json")
        receipt = json.loads(path.read_text())
        first, _, last = contact["full_name"].partition(" ")
        receipt["provider_response"]["body"]["element"].update(
            linkedinUrl=contact["linkedin_url"], firstName=first, lastName=last,
            currentPositions=list(positions), experience=list(experience))
        path.write_text(json.dumps(receipt))






if __name__ == "__main__":
    unittest.main()
