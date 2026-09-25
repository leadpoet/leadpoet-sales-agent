"""Checks for the pre-submission fit re-check."""

from __future__ import annotations

import asyncio

from experiments.harness_bakeoff import reverify

ICP = {"company_stage": "Series B", "country": "United States", "employee_count": ["51-200", "201-500"]}
COMPANY = {"company_name": "Stability AI", "company_website": "https://stability.ai/", "country": "United States",
           "company_stage": "Series B", "employee_count": "51-200"}


def test_normalizers() -> None:
    assert reverify.normalize_country("U.S.") == "united states"
    assert reverify.normalize_country("UK") == "united kingdom"
    assert reverify.normalize_stage("Series D round") == "series c+"
    assert reverify.normalize_stage("raised a seed round") == "seed"
    assert reverify.normalize_band(180) == "51-200"
    assert reverify.normalize_band("1,001-5,000") == "1,001-5,000"


def test_sourced_country_contradiction_drops() -> None:
    verdict = {"observed_country": "United Kingdom", "observed_employee_band": "201-500", "observed_stage": "Series C+",
               "same_company": True, "confidence": "high", "sources": ["https://example.org/stability"]}
    decision, reason = reverify.decide(verdict, COMPANY, ICP)
    assert decision == "drop" and "country" in reason


def test_unsourced_disagreement_is_unknown_not_drop() -> None:
    verdict = {"observed_country": "United Kingdom", "observed_employee_band": None, "observed_stage": None,
               "same_company": None, "confidence": "low", "sources": []}
    decision, _ = reverify.decide(verdict, COMPANY, ICP)
    assert decision == "unknown"


def test_full_confirmation_keeps() -> None:
    verdict = {"observed_country": "USA", "observed_employee_band": "51-200", "observed_stage": "Series B",
               "same_company": True, "confidence": "high", "sources": ["https://example.org/x"]}
    assert reverify.decide(verdict, COMPANY, ICP)[0] == "keep"


def test_rerank_orders_confirmed_first_and_survives_failures() -> None:
    good = dict(COMPANY, company_name="Good Co", company_website="https://good.example/")
    bad = dict(COMPANY, company_name="Bad Co", company_website="https://bad.example/")
    unknown = dict(COMPANY, company_name="Unknown Co", company_website="https://unknown.example/")

    async def post_json(body):
        prompt = body["messages"][1]["content"]
        if "Bad Co" in prompt:
            payload = {"observed_country": "Germany", "observed_employee_band": "51-200", "observed_stage": "Series B",
                       "same_company": True, "confidence": "high", "sources": ["https://s.example/bad"]}
        elif "Good Co" in prompt:
            payload = {"observed_country": "United States", "observed_employee_band": "51-200", "observed_stage": "Series B",
                       "same_company": True, "confidence": "high", "sources": ["https://s.example/good"]}
        else:
            raise RuntimeError("transport down")
        import json
        return {"choices": [{"message": {"content": "Result:\n" + json.dumps(payload)}}]}

    report: list[str] = []
    result = asyncio.run(reverify.rerank([unknown, bad, good], ICP, post_json=post_json, report=report))
    assert [c["company_name"] for c in result] == ["Good Co", "Unknown Co"]
    assert any("Bad Co: drop" in line for line in report)
    assert any("Unknown Co: verifier unavailable" in line for line in report)
