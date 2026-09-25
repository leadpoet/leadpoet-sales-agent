"""Focused contract checks for explicit public-listing stage annotations."""

from __future__ import annotations

import json
import unittest

from experiments.harness_bakeoff.models import CompanyResult


def _company(stage: str) -> dict[str, object]:
    return {
        "company_name": "Example",
        "company_website": "https://example.com",
        "industry": "Financial Services",
        "employee_count": "1,001-5,000",
        "company_stage": stage,
        "country": "Australia",
        "fit_summary": "The company matches the requested ICP.",
        "fit_evidence_urls": ["https://example.com/about"],
        "intent_signals": [
            {
                "matched_icp_signal": 0,
                "description": "The company announced a current business event.",
                "date": "2026-08-20",
                "why_now": "The event gives a timely and relevant contact reason.",
                "url": "https://example.com/news/event",
                "snippet": "The company announced the event.",
            }
        ],
    }


class PublicListingStageNormalizationTests(unittest.TestCase):
    def test_explicit_exchange_and_ticker_annotation_normalizes_and_round_trips(self) -> None:
        result = CompanyResult.model_validate(_company("Public (ASX: MQG)"))
        dumped = result.model_dump(mode="json")

        self.assertEqual(dumped["company_stage"], "Public")
        self.assertEqual(
            CompanyResult.model_validate_json(json.dumps(dumped)).model_dump(mode="json"),
            dumped,
        )

    def test_ambiguous_public_language_and_free_form_stages_are_preserved(self) -> None:
        stages = (
            "public sector",
            "planned IPO",
            "reportedly public",
            "Public (unverified)",
            "Public (planned IPO: 2027)",
            "Hardware scale-up",
        )

        for stage in stages:
            with self.subTest(stage=stage):
                dumped = CompanyResult.model_validate(_company(stage)).model_dump(
                    mode="json"
                )
                self.assertEqual(dumped["company_stage"], stage)
                self.assertEqual(
                    CompanyResult.model_validate_json(json.dumps(dumped)).model_dump(
                        mode="json"
                    ),
                    dumped,
                )


if __name__ == "__main__":
    unittest.main()
