"""Focused checks for the code-enforced output rules."""

from __future__ import annotations

import copy

import pytest

from experiments.harness_bakeoff.adapters import pydantic_ai
from experiments.harness_bakeoff.models import validate_companies


ICP = {
    "icp_id": "icp_test_001",
    "employee_count": ["11-50", "51-200"],
    "intent_max_age_days": 365,
    "excluded_companies": ["banned.example"],
    "verified_example_company": "Example Co",
    "bonus_intents": [],
}


def _signal(index: int, day: str, url: str = "") -> dict[str, object]:
    return {
        "matched_icp_signal": index,
        "description": "The company announced a dated business event.",
        "date": day,
        "why_now": "The event gives a timely reason to reach out.",
        "url": url or f"https://news.example/{day}",
        "snippet": "The company announced the event.",
    }


def _company(name: str, *signals: dict[str, object], band: str = "51-200") -> dict[str, object]:
    return {
        "company_name": name,
        "company_website": f"https://www.{name.lower()}.example/",
        "industry": "Software",
        "employee_count": band,
        "company_stage": "Series A",
        "country": "United States",
        "fit_summary": "Matches the requested ICP.",
        "fit_evidence_urls": [f"https://{name.lower()}.example/about"],
        "intent_signals": list(signals),
    }


@pytest.fixture(autouse=True)
def _fixed_evaluation_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAKEOFF_EVALUATION_DATE", "2026-09-12")


def test_keeps_one_primary_signal_per_judge_domain_newest_first() -> None:
    company = _company(
        "Alpha",
        _signal(0, "2026-03-01", "https://news.example/old"),
        _signal(0, "2026-08-30", "https://news.example/new"),
        _signal(0, "2026-06-15", "https://www.alpha.example/blog/announce"),
        _signal(0, "2026-07-01", "https://blog.alpha.example/again"),
        _signal(0, "2026-05-01", "https://other.press/story"),
        _signal(0, "2026-04-01", "https://fourth.site/story"),
    )

    result = pydantic_ai._harden_output(ICP, [company])

    dates = [signal["date"] for signal in result[0]["intent_signals"]]
    # newest per domain, at most three domains, same host family collapsed
    assert dates == ["2026-08-30", "2026-07-01", "2026-05-01"]
    assert result[0]["company_linkedin"] == ""


def test_drops_bonus_signals_when_icp_lists_none() -> None:
    company = _company("Alpha", _signal(0, "2026-08-30"), _signal(1, "2026-09-01"))

    result = pydantic_ai._harden_output(ICP, [company])

    assert [signal["matched_icp_signal"] for signal in result[0]["intent_signals"]] == [0]


def test_keeps_one_bonus_signal_per_index_when_icp_lists_bonus_intents() -> None:
    icp = dict(ICP, bonus_intents=["Hired a new CFO"])
    company = _company(
        "Alpha",
        _signal(0, "2026-08-30"),
        _signal(1, "2026-07-01", "https://bonus.example/old"),
        _signal(1, "2026-09-01", "https://bonus.example/new"),
    )

    result = pydantic_ai._harden_output(icp, [company])

    assert [(s["matched_icp_signal"], s["date"]) for s in result[0]["intent_signals"]] == [
        (0, "2026-08-30"),
        (1, "2026-09-01"),
    ]


def test_drops_company_whose_primary_event_is_outside_the_window() -> None:
    stale = _company("Stale", _signal(0, "2025-01-01"))
    future = _company("Future", _signal(0, "2026-12-01"))
    fresh = _company("Fresh", _signal(0, "2026-09-01"))

    result = pydantic_ai._harden_output(ICP, [stale, future, fresh])

    assert [company["company_name"] for company in result] == ["Fresh"]


def test_drops_excluded_example_off_band_and_duplicate_companies() -> None:
    banned = _company("Banned", _signal(0, "2026-09-01"))
    banned["company_website"] = "https://banned.example/"
    example = _company("Example Co", _signal(0, "2026-09-01"))
    off_band = _company("Big", _signal(0, "2026-09-01"), band="1,001-5,000")
    keep = _company("Keep", _signal(0, "2026-09-01"))
    duplicate = copy.deepcopy(keep)
    duplicate["company_website"] = "https://keep.example/news"

    result = pydantic_ai._harden_output(ICP, [banned, example, off_band, keep, duplicate])

    assert [company["company_name"] for company in result] == ["Keep"]


def test_band_comparison_is_canonical_on_both_sides() -> None:
    icp = dict(ICP, employee_count=["1001-5000", "51 - 200 employees"])
    big = _company("Big", _signal(0, "2026-09-01"), band="1,001-5,000")
    mid = _company("Mid", _signal(0, "2026-09-01"), band="51-200")
    small = _company("Small", _signal(0, "2026-09-01"), band="11-50")

    result = pydantic_ai._harden_output(icp, [big, mid, small])

    assert sorted(company["company_name"] for company in result) == ["Big", "Mid"]


def test_orders_companies_by_primary_event_recency() -> None:
    older = _company("Older", _signal(0, "2026-05-01"))
    newer = _company("Newer", _signal(0, "2026-09-05"))

    result = pydantic_ai._harden_output(ICP, [older, newer])

    assert [company["company_name"] for company in result] == ["Newer", "Older"]


def test_hardened_output_still_validates_against_the_public_contract() -> None:
    company = _company("Alpha", _signal(0, "2026-08-30"), _signal(0, "2026-06-15"))

    result = pydantic_ai._harden_output(ICP, [company])

    validated = validate_companies(result, 5)

    assert len(validated) == 1
    assert validated[0]["intent_signals"] == result[0]["intent_signals"]


def test_finalization_is_due_once_the_wall_clock_reserve_is_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = type("Usage", (), {"input_tokens": 0, "requests": 0, "tool_calls": 0})()
    monkeypatch.setattr(pydantic_ai, "_FINALIZE_ELAPSED_SECONDS", 100.0)

    monkeypatch.setattr(pydantic_ai, "_RUN_STARTED_AT", None)
    assert pydantic_ai._finalization_due(usage) is False

    monkeypatch.setattr(pydantic_ai, "_RUN_STARTED_AT", pydantic_ai.time.monotonic() - 101.0)
    assert pydantic_ai._finalization_due(usage) is True


def test_status_line_is_refreshed_not_accumulated(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from pydantic_ai import messages

    monkeypatch.setattr(pydantic_ai, "_RUN_STARTED_AT", pydantic_ai.time.monotonic() - 42.0)
    monkeypatch.setattr(pydantic_ai, "_FINALIZE_ELAPSED_SECONDS", 200.0)
    context = SimpleNamespace(usage=SimpleNamespace(input_tokens=0, requests=3, tool_calls=4))
    history = [
        messages.ModelRequest.user_text_prompt("Find matching companies"),
        messages.ModelResponse(parts=[messages.ToolCallPart("search_web", {"query": "x"}, tool_call_id="c1")]),
        messages.ModelRequest(parts=[messages.ToolReturnPart("search_web", {"results": []}, tool_call_id="c1")]),
    ]

    once = pydantic_ai._process_history(context, history)
    twice = pydantic_ai._process_history(context, once)

    def status_parts(processed):
        return [
            part.content
            for message in processed
            if isinstance(message, messages.ModelRequest)
            for part in message.parts
            if isinstance(part, messages.UserPromptPart)
            and isinstance(part.content, str)
            and part.content.startswith(pydantic_ai._STATUS_MARKER)
        ]

    assert len(status_parts(once)) == 1
    assert len(status_parts(twice)) == 1
    line = status_parts(twice)[0]
    assert "elapsed 42s" in line
    assert "research stops in 158s" in line
    expected = min(pydantic_ai._FINALIZE_TOOL_CALLS - 4, pydantic_ai._FINALIZE_DEEPLINE_CALLS)
    assert f"{expected} provider calls left" in line
    # The original user prompt never carries a status line.
    first = twice[0]
    assert isinstance(first, messages.ModelRequest)
    assert all(not (isinstance(p, messages.UserPromptPart) and str(p.content).startswith(pydantic_ai._STATUS_MARKER)) for p in first.parts)
