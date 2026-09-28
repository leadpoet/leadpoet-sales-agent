"""v6 output carries company_stage_evidence through every pass to the Arena."""

from __future__ import annotations

import pytest

from experiments.harness_bakeoff import evidence as _evidence
from experiments.harness_bakeoff.models import validate_companies
from experiments.harness_bakeoff.prompt import build_prompt

STAGE = [{"url": "https://www.nasdaq.com/market-activity/stocks/acme",
          "quote": "Acme Inc. (NASDAQ: ACME) common stock trades on the Nasdaq Global Select Market."}]


def _company(**overrides):
    company = {
        "company_name": "Acme", "company_website": "https://acme.example/", "industry": "Software",
        "employee_count": "51-200", "company_stage": "Public", "country": "United States",
        "intent_details": ("Acme launched its workflow platform on August 20, 2026, which could create "
                           "implementation work as customers adopt it, connecting Acme to this ICP."),
        "intent_signals": [{"matched_icp_signal": 0, "description": "Acme launched its workflow platform.",
                            "url": "https://acme.example/news/launch", "date": "2026-08-20"}],
        "company_stage_evidence": STAGE,
    }
    company.update(overrides)
    return company


ICP = {"prompt": "workflow software", "industry": "Software", "country": "United States",
       "company_stage": "Public", "employee_count": ["51-200"], "max_companies": 5,
       "intent_signal": "launched a product", "intent_category": "PRODUCT_LAUNCH",
       "intent_max_age_days": 365, "intent_details_policy": "intent_details_v1"}


def test_stage_evidence_survives_evidence_pass_and_validation() -> None:
    page = ("Acme launched its workflow platform on August 20, 2026. " * 20) + "Acme Inc is a software company."
    verified = _evidence.verify_companies(ICP, [_company()], lambda url: page,
                                          seconds_left=lambda: 200.0, min_seconds=5.0, report=[])
    final = validate_companies(verified, 5, intent_details_policy="intent_details_v1")
    assert final[0]["company_stage_evidence"] == STAGE


def test_stage_evidence_is_bounded_like_the_arena_schema() -> None:
    assert validate_companies([_company(company_stage_evidence=[])], 5,
                              intent_details_policy="intent_details_v1")[0]["company_stage_evidence"] == []
    with pytest.raises(ValueError):
        validate_companies([_company(company_stage_evidence=STAGE * 4)], 5, intent_details_policy="intent_details_v1")
    with pytest.raises(ValueError):
        validate_companies([_company(company_stage_evidence=[{"url": "ftp://x", "quote": "q"}])], 5,
                           intent_details_policy="intent_details_v1")


def test_v6_prompt_asks_for_stage_evidence_not_fit_urls() -> None:
    prompt = build_prompt(ICP, max_companies=5)
    assert "company_stage_evidence" in prompt
    assert "- fit_evidence_urls, in this order" not in prompt
