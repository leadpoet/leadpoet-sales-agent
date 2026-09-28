"""The fit re-check keeps a company whose fetched stage evidence names the required stage."""

from __future__ import annotations

from experiments.harness_bakeoff import reverify

VERDICT = {"observed_country": "United States", "observed_employee_band": "11-50",
           "observed_stage": "Bootstrapped", "same_company": True, "confidence": "high",
           "sources": ["https://example.com/a"]}
ICP = {"company_stage": "Seed", "employee_count": ["11-50"]}


def _company(quote: str) -> dict:
    return {"company_name": "Mesta", "country": "United States",
            "company_stage_evidence": [{"url": "https://www.prnewswire.com/x", "quote": quote}]}


def test_named_round_in_stage_evidence_outranks_a_sonar_stage_guess() -> None:
    decision, reason = reverify.decide(VERDICT, _company(
        "Mesta today announced it has raised a $5.5 million seed round led by Village Global."), ICP)
    assert decision == "unknown" and "stage unconfirmed" in reason


def test_sourced_stage_contradiction_without_evidence_still_drops() -> None:
    assert reverify.decide(VERDICT, {"company_name": "X", "country": "United States"}, ICP)[0] == "drop"
    assert reverify.decide(VERDICT, _company("Mesta launched a customer portal."), ICP)[0] == "drop"


def test_public_ticker_quote_counts_as_public_stage_evidence() -> None:
    verdict = dict(VERDICT, observed_stage="Private Equity")
    company = _company("Marcus & Millichap, Inc. (NYSE: MMI) today reported results.")
    assert reverify.decide(verdict, company, {"company_stage": "Public"})[0] == "unknown"
