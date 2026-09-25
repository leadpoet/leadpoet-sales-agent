"""Focused checks for the model-side explicit company-stage filter."""

from __future__ import annotations

import copy
import json

from experiments.harness_bakeoff.adapters.pydantic_ai import (
    _filter_explicit_stage_conflicts,
)
from experiments.harness_bakeoff.models import validate_companies


def _company(name: str, stage: str = "") -> dict[str, object]:
    return {
        "company_name": name,
        "company_website": f"https://{name.lower()}.example",
        "industry": "Software",
        "employee_count": "51-200",
        "company_stage": stage,
        "country": "United States",
        "fit_summary": "Matches the requested ICP.",
        "fit_evidence_urls": [f"https://{name.lower()}.example/about"],
        "intent_signals": [
            {
                "matched_icp_signal": 0,
                "description": "The company announced a current business event.",
                "date": "2026-08-20",
                "why_now": "The event gives a timely contact reason.",
                "url": f"https://{name.lower()}.example/news/event",
                "snippet": "The company announced the event.",
            }
        ],
    }


def test_public_requirement_drops_conflicting_c_plus_output() -> None:
    companies = [_company("Alpha", "Public"), _company("Beta", "Series C+")]

    assert _filter_explicit_stage_conflicts(
        {"company_stage": "Public"}, companies
    ) == [companies[0]]


def test_seed_requirement_drops_conflicting_series_b_output() -> None:
    companies = [_company("Alpha", "Seed"), _company("Beta", "Series B")]

    assert _filter_explicit_stage_conflicts(
        {"company_stage": "Seed"}, companies
    ) == [companies[0]]


def test_c_plus_requirement_drops_public_but_keeps_later_venture_round() -> None:
    companies = [_company("Alpha", "Public"), _company("Beta", "Series D")]

    assert _filter_explicit_stage_conflicts(
        {"company_stage": "Series C+"}, companies
    ) == [companies[1]]


def test_unrecognized_requirements_do_not_filter_known_company_stages() -> None:
    companies = [_company("Alpha", "Public"), _company("Beta", "Seed")]
    for requirement in (None, "", "Any", "Series B / Series C", ["Series B"]):
        assert _filter_explicit_stage_conflicts(
            {"company_stage": requirement}, companies
        ) == companies


def test_matching_synonyms_and_later_rounds_are_eligible() -> None:
    companies = [
        _company("Alpha", "publicly traded"),
        _company("Beta", "Series D"),
        _company("Gamma", "Series E"),
    ]

    assert _filter_explicit_stage_conflicts(
        {"company_stage": "listed company"}, companies[:1]
    ) == companies[:1]
    assert _filter_explicit_stage_conflicts(
        {"company_stage": "series c or later"}, companies[1:]
    ) == companies[1:]


def test_unknown_or_missing_requirement_and_output_are_preserved() -> None:
    companies = [
        _company("Unknown", "growth stage"),
        _company("Missing", ""),
        _company("Compound", "Series B / Series C"),
    ]

    assert _filter_explicit_stage_conflicts(
        {"company_stage": "growth stage"}, companies
    ) == companies
    assert _filter_explicit_stage_conflicts({}, companies) == companies
    assert _filter_explicit_stage_conflicts(
        {"company_stage": "Public"}, companies
    ) == companies


def test_mixed_results_keep_order_and_do_not_mutate_input() -> None:
    companies = [
        _company("Keep First", "Series A"),
        _company("Drop", "Public"),
        _company("Keep Unknown", "unverified stage"),
        _company("Keep Last", "Series A round"),
    ]
    original = copy.deepcopy(companies)

    filtered = _filter_explicit_stage_conflicts(
        {"company_stage": "Series A"}, companies
    )

    assert [company["company_name"] for company in filtered] == [
        "Keep First",
        "Keep Unknown",
        "Keep Last",
    ]
    assert companies == original


def test_empty_filtered_result_remains_ordinary_json_valid() -> None:
    filtered = _filter_explicit_stage_conflicts(
        {"company_stage": "Public"}, [_company("Only", "Series C+")]
    )

    assert filtered == []
    assert json.loads(json.dumps(filtered)) == []
    assert validate_companies(filtered) == []
