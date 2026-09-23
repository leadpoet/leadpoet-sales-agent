"""Company-only Arena adapter acceptance contracts."""

from __future__ import annotations

import json
import time

import pytest

from tyche_arena import output
from tyche_arena.broker import Broker, _SCRAPINGDOG_ROUTES
from tyche_arena.input import company_only_icp, request_for


LEGACY_ICP = {
    "intent_details_policy": "intent_details_v1",
    "contact_policy": "contacts_v1",
    "prompt": (
        "Find software companies with a recent product launch. "
        "Target contacts: VP Engineering (VP+)."
    ),
    "industry": "Software",
    "geography": "United States",
    "required_attribute": "Sells subscription software",
    "intent_signals": ["Launched a major product capability"],
    "intent_max_age_days": 365,
    "target_roles": ["VP Engineering"],
    "target_seniority": "VP+",
    "contact_geography": {"countries": ["US"]},
}

COMPANY_TOOL_IDS = {
    "contextdev_get_web_scrape_markdown",
    "contextdev_post_news_search",
    "contextdev_post_web_search",
    "exa_answer",
    "exa_company_search",
    "exa_contents",
    "exa_search",
    "firecrawl_scrape",
    "free_simple_company_search",
    "generic_http_request",
    "harvestapi_get_company",
    "harvestapi_get_job",
    "harvestapi_get_post",
    "hunter_discover",
    "predictleads_company_financing_events",
    "predictleads_company_job_openings",
    "predictleads_company_news_events",
    "twitterapi_tweets_by_ids",
}


def test_request_strips_legacy_contact_requirements_before_model_or_runtime():
    request = request_for(LEGACY_ICP, 3, 600)
    visible = json.loads(request["original_text"])

    assert visible == company_only_icp(LEGACY_ICP)
    assert visible["prompt"] == "Find software companies with a recent product launch."
    assert not set(visible) & {
        "contact_policy", "target_roles", "target_seniority", "contact_geography"
    }
    assert not set(request) & {
        "requested_roles", "contact_fields", "contacts_per_company",
        "min_contacts_per_company", "target_contacts_per_company",
        "contact_role_groups",
    }
    assert request["target_count"] == 3
    assert request["icp"]["industries"] == ["Software"]
    assert request["icp"]["geographies"] == ["United States"]
    assert request["icp"]["required_attributes"] == [
        "Sells subscription software"
    ]


def test_broker_exposes_only_company_research_catalog(tmp_path):
    broker = Broker(tmp_path / "worker.sock", time.monotonic() + 30)

    assert set(broker.catalog) == COMPANY_TOOL_IDS
    assert "linkedin_person" not in _SCRAPINGDOG_ROUTES
    assert not set(broker.catalog) & {
        "exa_people_search",
        "harvestapi_get_profile",
        "harvestapi_search_leads",
        "hunter_email_finder",
        "limadata_find_work_email",
        "datagma_find_email",
        "leadmagic_email_finder",
        "zerobounce_validate",
        "bounceban_verify_single",
        "bounceban_get_single_status",
    }
    exa_category = broker.catalog["exa_search"]["inputSchema"]["jsonSchema"][
        "properties"
    ]["category"]["enum"]
    assert "people" not in exa_category and "personal site" not in exa_category


def test_company_receipt_keeps_missing_field_diagnostics():
    from run_attempt import _harvest_display

    row = {"entity_type": "company", "company": "Acme", "employee_range": None,
           "missing_fields": ["employee_range"]}
    shown = _harvest_display(row)
    assert shown["missing_fields"] == ["employee_range"]
    assert shown["employee_range"] is None


def test_scrapingdog_person_lookup_is_retired_but_company_lookup_remains():
    from run_attempt import company_stage_refusal

    assert company_stage_refusal({}, provider="scrapingdog", phase="account_verification",
                                 tool="linkedin_person")
    assert company_stage_refusal({}, provider="scrapingdog", phase="account_verification",
                                 tool="linkedin_company") is None


def _document(icp):
    return {
        "request": {
            "original_text": json.dumps(icp),
            "target_count": 1,
            "buying_signals": [{
                "kind": "arena_signal_0",
                "query": "Launched a major product capability",
                "importance": "required",
                "max_age_days": 365,
            }],
        },
        "accepted": [{
            "company": {
                "canonical_name": "Acme",
                "domain": "acme.example",
                "website": "https://acme.example",
                "linkedin_url": "https://linkedin.com/company/acme",
                "industry": "Software",
                "employee_range": "51-200",
                "company_stage": "Series A",
                "hq_country": "United States",
                "hq_state": "California",
            },
            "qualification_checks": [{
                "status": "pass",
                "signal": "arena_signal_0",
                "claim": "Acme launched a major product capability.",
                "evidence": [{
                    "evidence_url": "https://acme.example/news/launch",
                    "evidence_text": "Acme launched a major product capability.",
                    "event_date": "2026-09-01",
                }],
            }, {
                "status": "pass",
                "criterion": "Sells subscription software",
                "claim": "Acme sells subscription software.",
                "evidence": [{
                    "evidence_url": "https://acme.example/product",
                    "evidence_text": "Subscription plans are available.",
                }],
            }],
            "intent_details": (
                "Acme launched a major product capability on September 1, 2026, "
                "which matches the requested intent signal."
            ),
        }],
    }


def test_projection_is_exact_company_only_v6_shape(monkeypatch, tmp_path):
    icp = company_only_icp({**LEGACY_ICP, "company_stage": "Series A"})
    document = _document(icp)
    monkeypatch.setattr(output, "accepted_preflight", lambda *_args, **_kwargs: [])

    rows = output._project_companies(
        tmp_path / "results.json", document, icp, require_review=False
    )

    assert rows == [{
        "company_name": "Acme",
        "company_website": "https://acme.example",
        "company_linkedin": "https://linkedin.com/company/acme",
        "industry": "Software",
        "employee_count": "51-200",
        "company_stage": "Series A",
        "country": "United States",
        "state": "California",
        "intent_details": (
            "Acme launched a major product capability on September 1, 2026, "
            "which matches the requested intent signal."
        ),
        "intent_signals": [{
            "matched_icp_signal": 0,
            "description": "Acme launched a major product capability.",
            "date": "2026-09-01",
            "url": "https://acme.example/news/launch",
        }],
        "company_stage_evidence": [
            {
                "url": "https://acme.example/news/launch",
                "quote": "Acme launched a major product capability.",
            },
            {
                "url": "https://acme.example/product",
                "quote": "Subscription plans are available.",
            },
        ],
        "required_attribute": {
            "text": "Sells subscription software",
            "passed": True,
            "evidence_url": "https://acme.example/product",
            "evidence_quote": "Subscription plans are available.",
            "explanation": "Acme sells subscription software.",
        },
    }]
    assert "contact" not in rows[0]


def test_projection_keeps_company_stage_gate(monkeypatch, tmp_path):
    icp = company_only_icp({**LEGACY_ICP, "company_stage": "Series B"})
    document = _document(icp)
    monkeypatch.setattr(output, "accepted_preflight", lambda *_args, **_kwargs: [])

    with pytest.raises(ValueError, match="company_stage does not satisfy"):
        output._project_companies(
            tmp_path / "results.json", document, icp, require_review=False
        )
