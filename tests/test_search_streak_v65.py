"""v65: a fourth consecutive search is refused until a page or profile is read."""

from __future__ import annotations

import asyncio

from experiments.harness_bakeoff import reverify
from experiments.harness_bakeoff.adapters import pydantic_ai as adapter


class _Client:
    deepline_calls = 0

    def call(self, name, arguments):
        return {"ok": True, "tool": name, "text": "page" if name == "fetch_page" else None}


def test_fourth_search_in_a_row_is_refused_for_free_and_a_fetch_resets_it(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "_sourcing_spend_usd", lambda: 0.0)
    budget = adapter._ToolBudget(_Client(), 30)
    for _ in range(3):
        assert budget.call("search_web", {"query": "q"})["ok"] is True
    refused = budget.call("search_web", {"query": "q"})
    assert refused["ok"] is False and refused["error"].startswith("search refused")
    assert budget.calls == 3  # the refusal is not charged
    assert budget.call("fetch_page", {"url": "https://a.example/x"})["ok"] is True
    assert budget.call("search_web", {"query": "q"})["ok"] is True


def test_stage_or_country_conflicts_rank_last_but_identity_mismatch_drops() -> None:
    icp = {"company_stage": "Series B", "country": "United States", "employee_count": ["51-200"]}
    conflict = {"company_name": "A", "company_website": "https://a.example/", "country": "United States"}
    other = {"company_name": "B", "company_website": "https://b.example/", "country": "United States"}

    async def post_json(body):
        import json
        prompt = body["messages"][1]["content"]
        same = "Company name: B" not in prompt
        payload = {"observed_country": "Israel", "observed_employee_band": "51-200", "observed_stage": "Series B",
                   "same_company": same, "confidence": "high", "sources": ["https://s.example/"]}
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    report: list[str] = []
    kept = asyncio.run(reverify.rerank([conflict, other], icp, post_json=post_json, report=report))
    assert [c["company_name"] for c in kept] == ["A"]
    assert any(line.startswith("A: unknown (flagged, kept") for line in report)
    assert any(line.startswith("B: drop") for line in report)
