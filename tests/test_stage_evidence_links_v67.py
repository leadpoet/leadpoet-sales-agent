"""v67: stage-evidence links on hosts the judge cannot read are dropped; wording is not filtered."""

from __future__ import annotations

from experiments.harness_bakeoff.adapters import pydantic_ai as adapter


def _run(stage: str, name: str, items: list[dict]) -> list[dict]:
    companies = [{"company_name": name, "company_stage_evidence": items}]
    return adapter._clean_stage_evidence({"company_stage": stage}, companies)[0]["company_stage_evidence"]


def test_unreadable_hosts_are_dropped() -> None:
    kept = _run("Series C+", "LaunchDarkly", [
        {"url": "https://www.cbinsights.com/research/launchdarkly-series-d-funding/", "quote": "Series D."},
        {"url": "https://www.linkedin.com/company/launchdarkly", "quote": "Series D."},
        {"url": "https://launchdarkly.com/blog/announcing-200-million-in-funding/", "quote": "Series D round."},
    ])
    assert [item["url"] for item in kept] == ["https://launchdarkly.com/blog/announcing-200-million-in-funding/"]


def test_first_party_we_quotes_without_the_name_are_kept() -> None:
    # EdgeTier and Payslip qualified on 09-27 with exactly such links.
    items = [{"url": "https://www.edgetier.com/news/series-a",
              "quote": "We are delighted to announce that we have raised 6 million euro in Series A funding."}]
    assert _run("Series A", "EdgeTier", list(items)) == items
