"""Offline rendering controls; evidence and the strict output validator stay intact."""
import copy
import unittest
from unittest import mock

import harness

idm = harness.details_module


class FallbackProseFormatTest(unittest.TestCase):
    def signal(self, description, **overrides):
        return {"index": 0, "description": description, "date": "2026-10-05",
                "date_visible": False, "url": "https://example.com/news/launch",
                "quote": "# The original evidence quote stays verbatim.", **overrides}

    def render(self, signal):
        return idm.fallback_paragraph(company_name="ExampleCo", icp={}, signals=[signal])

    def test_heading_without_visible_date_is_rendered_as_prose(self):
        signal = self.signal("# ExampleCo launched a new analytics service for customers")
        before = copy.deepcopy(signal)
        text = self.render(signal)
        self.assertIn("ExampleCo launched a new analytics service for customers", text)
        self.assertNotIn("#", text)
        self.assertEqual(harness.sm.validate_intent_details_text(text), text)
        self.assertEqual(signal, before)

    def test_existing_date_and_plain_prose_are_unchanged(self):
        for visible in (False, True):
            with self.subTest(visible=visible):
                signal = self.signal("ExampleCo launched a new analytics service for customers", date_visible=visible)
                with mock.patch.object(idm, "strip_markdown_dup", side_effect=lambda text: text):
                    previous = self.render(signal)
                self.assertEqual(self.render(signal), previous)
                if visible:
                    self.assertIn(idm.human_date(signal["date"]), previous)

    def test_date_and_year_are_not_list_markers(self):
        for value in ("2026-10-05 product launch", "October 5, 2026 product launch", "2026. Product launch details"):
            with self.subTest(value=value):
                self.assertEqual(idm.strip_markdown_dup(value), value)

    def test_existing_heading_cleanup_works_for_all_supported_heading_levels(self):
        for count in range(1, 7):
            with self.subTest(count=count):
                text = self.render(self.signal("#" * count + " ExampleCo launched a new analytics service"))
                self.assertNotIn("#", text)

    def test_list_and_code_block_input_still_fail_strict_validation(self):
        for prefix in ("* ", "\u2022 ", "1. ", "1) ", "> ", "```\n"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ValueError):
                    self.render(self.signal(prefix + "ExampleCo launched a new analytics service"))

    def test_existing_helper_removes_a_leading_hyphen_without_changing_evidence(self):
        signal = self.signal("- ExampleCo launched a new analytics service")
        before = copy.deepcopy(signal)
        self.assertNotIn("- ", self.render(signal))
        self.assertEqual(signal, before)
        with self.assertRaises(ValueError):
            harness.sm.validate_intent_details_text("- ExampleCo launched a new analytics service")

    def test_chrome_disabled_preserves_existing_strict_rejection(self):
        with mock.patch.dict(idm._os.environ, {"ARENA_STRIP_CHROME": "0"}):
            with self.assertRaisesRegex(ValueError, "prose"):
                self.render(self.signal("# ExampleCo launched a new analytics service"))

    def test_final_validator_errors_are_not_swallowed(self):
        with mock.patch.object(harness.sm, "validate_intent_details_text", side_effect=ValueError("strict sentinel")) as validate:
            with self.assertRaisesRegex(ValueError, "strict sentinel"):
                self.render(self.signal("# ExampleCo launched a new analytics service"))
            validate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
