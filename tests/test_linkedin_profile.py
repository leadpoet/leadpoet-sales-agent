from __future__ import annotations

import json

import pytest

from experiments.harness_bakeoff.linkedin_profile import (
    linkedin_company_profile_url,
    project_linkedin_profile_evidence,
)


PROFILE_URL = "https://www.linkedin.com/company/example"


def _result(text: str, **updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "url": "https://linkedin.com/company/example/",
        "title": "Unrelated name | LinkedIn",
        "text": text,
    }
    result.update(updates)
    return result


def test_extracts_only_explicit_company_size_from_about_section() -> None:
    text = """Example | LinkedIn

## About
Example builds business software and has 500 employees worldwide.

Company size
11-50 employees
Headquarters
Across Massachusetts
89 associated members
View all 89 employees

## Employees at Example
501-1,000 employees

## Updates
"""

    evidence = project_linkedin_profile_evidence(PROFILE_URL, _result(text))

    assert evidence == {
        "url": "https://linkedin.com/company/example/",
        "title": "Unrelated name | LinkedIn",
        "employee_count": "11-50",
        "quote": "Company size\n11-50 employees",
        "listed_headquarters": "Across Massachusetts",
        "headquarters_quote": "Headquarters\nAcross Massachusetts",
    }
    assert set(evidence) == {
        "url",
        "title",
        "employee_count",
        "quote",
        "listed_headquarters",
        "headquarters_quote",
    }
    assert json.loads(json.dumps(evidence)) == evidence


@pytest.mark.parametrize(
    "text",
    [
        "## About\n89 associated members\n## Updates",
        "## About\nView all 89 employees\n## Updates",
        "## About\nFollowed by 51-200 people\n## Updates",
        "## About\nWe have 51-200 employees worldwide.\n## Updates",
        "## About\n## Employees at Example\nCompany size\n51-200 employees",
        "Company size\n51-200 employees\n## Updates",
    ],
)
def test_rejects_counts_without_about_section_company_size_label(text: str) -> None:
    with pytest.raises(ValueError, match="Company size band is missing"):
        project_linkedin_profile_evidence(PROFILE_URL, _result(text))


@pytest.mark.parametrize(
    "value",
    [
        "http://www.linkedin.com/company/example",
        "https://www.linkedin.com/in/example",
        "https://example.com/company/example",
        "https://www.linkedin.com/company/example/posts",
        "https://www.linkedin.com/company/example?trk=test",
        "https://127.0.0.1/company/example",
        " linkedin.com/company/example",
        "linkedin.com/company/example ",
        "linkedin.com/company/exam\nple",
        "linkedin.com@evil.example/company/example",
        "https://user@linkedin.com/company/example",
        42,
    ],
)
def test_rejects_noncanonical_or_nonpublic_profile_urls(value: object) -> None:
    with pytest.raises(ValueError, match="stored LinkedIn profile URL is invalid"):
        linkedin_company_profile_url(value)


def test_absent_profile_url_requires_no_lookup() -> None:
    assert linkedin_company_profile_url(None) is None
    assert linkedin_company_profile_url("") is None


@pytest.mark.parametrize(
    ("stored", "normalized"),
    [
        (
            "linkedin.com/company/nium-global",
            "https://linkedin.com/company/nium-global",
        ),
        (
            "www.linkedin.com/company/city-therapeutics",
            "https://www.linkedin.com/company/city-therapeutics",
        ),
    ],
)
def test_normalizes_exact_schemeless_linkedin_company_urls(
    stored: str, normalized: str
) -> None:
    assert linkedin_company_profile_url(stored) == normalized


def test_rejects_result_from_another_linkedin_company() -> None:
    with pytest.raises(ValueError, match="does not match"):
        project_linkedin_profile_evidence(
            PROFILE_URL,
            _result(
                "## About\nCompany size\n51-200 employees\n## Updates",
                url="https://www.linkedin.com/company/another-company",
            ),
        )

    with pytest.raises(ValueError, match="does not match"):
        project_linkedin_profile_evidence(
            "not a profile",
            _result(
                "## About\nCompany size 51-200 employees\n## Updates",
                url="also not a profile",
            ),
        )


def test_title_and_source_quote_have_fixed_output_limits() -> None:
    evidence = project_linkedin_profile_evidence(
        PROFILE_URL,
        _result(
            "## About\n**Company size**:\t10,001+ employees\n## Updates",
            title="T" * 600,
        ),
    )

    assert evidence["title"] == "T" * 300
    assert evidence["employee_count"] == "10,001+"
    assert evidence["quote"] == "**Company size**:\t10,001+ employees"
    assert not any("date" in key or "identity" in key for key in evidence)


def test_extracts_live_inline_company_size_format() -> None:
    evidence = project_linkedin_profile_evidence(
        "linkedin.com/company/nium-global",
        {
            "url": "https://www.linkedin.com/company/nium-global",
            "title": "Nium | LinkedIn",
            "text": (
                "## About us\n\nGlobal payments infrastructure.\n\n"
                "Company size 501-1,000 employees\n\n"
                "Headquarters San Francisco, California\n\n"
                "Type Privately Held\n\nFounded 2016\n\n## Employees at Nium"
            ),
        },
    )

    assert evidence["employee_count"] == "501-1,000"
    assert evidence["quote"] == "Company size 501-1,000 employees"
    assert evidence["listed_headquarters"] == "San Francisco, California"
    assert evidence["headquarters_quote"] == "Headquarters San Francisco, California"


@pytest.mark.parametrize(
    "headquarters_text",
    [
        "Across Massachusetts",
        "Headquarters\nType\nPrivately Held",
        "Headquarters\nType Privately Held",
        "Headquarters\nCompany size 11-50 employees",
        "Headquarters " + ("A" * 301),
        "Headquarters Across\tMassachusetts",
        "## Updates\nHeadquarters After Updates",
    ],
)
def test_omits_missing_or_invalid_headquarters_without_losing_size(
    headquarters_text: str,
) -> None:
    evidence = project_linkedin_profile_evidence(
        PROFILE_URL,
        _result(
            "## About\nCompany size 11-50 employees\n"
            + headquarters_text
            + "\n## Updates"
        ),
    )

    assert evidence["employee_count"] == "11-50"
    assert "listed_headquarters" not in evidence
    assert "headquarters_quote" not in evidence


def test_ignores_headquarters_outside_about_section_and_stops_at_next_field() -> None:
    evidence = project_linkedin_profile_evidence(
        PROFILE_URL,
        _result(
            "Headquarters Before About\n## About\nCompany size 11-50 employees\n"
            "Headquarters Boston, Massachusetts Type Privately Held\n"
            "## Updates\nHeadquarters After Updates"
        ),
    )

    assert evidence["listed_headquarters"] == "Boston, Massachusetts"
    assert evidence["headquarters_quote"] == "Headquarters Boston, Massachusetts"
