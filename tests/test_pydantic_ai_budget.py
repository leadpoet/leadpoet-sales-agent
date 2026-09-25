from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

from pydantic_ai import messages
from pydantic_ai.usage import RunUsage

from experiments.harness_bakeoff.adapters import pydantic_ai


def _context(*, input_tokens: int = 0, requests: int = 0, tool_calls: int = 0):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            requests=requests,
            tool_calls=tool_calls,
        )
    )


def _large_result(company: str, suffix: str) -> dict:
    tracking = "tracking-segment/" * 12
    return {
        "results": [
            {
                "company_name": f"{company} {index}",
                "domain": f"{company.lower()}{index}.example",
                "date": f"2026-08-{index + 10:02d}",
                "url": (
                    f"https://news.example/{company.lower()}/{index}/{tracking}{suffix}"
                ),
                "quote": f"{company} launched a verified product. " + (suffix * 300),
                "irrelevant_blob": suffix * 900,
            }
            for index in range(5)
        ]
    }


def _history() -> list[messages.ModelMessage]:
    return [
        messages.ModelRequest.user_text_prompt("Find matching companies"),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "search_web", {"query": "Acme launch"}, tool_call_id="call-1"
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "search_web",
                    _large_result("Acme", "a"),
                    tool_call_id="call-1",
                )
            ]
        ),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "search_web", {"query": "Beta launch"}, tool_call_id="call-2"
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "search_web",
                    _large_result("Beta", "b"),
                    tool_call_id="call-2",
                )
            ]
        ),
    ]


def test_prior_tool_payload_is_bounded_but_latest_remains_full() -> None:
    original = _history()
    processed = pydantic_ai._process_history(_context(), original)

    old_return = processed[2].parts[0]
    latest_return = processed[4].parts[0]
    assert isinstance(old_return, messages.ToolReturnPart)
    assert isinstance(latest_return, messages.ToolReturnPart)
    assert len(pydantic_ai._json_bytes(old_return.content)) <= 1_200
    compact_json = json.dumps(old_return.content)
    assert "2026-08-10" in compact_json
    expected_url = original[2].parts[0].content["results"][0]["url"]
    assert expected_url in compact_json
    assert "Acme launched a verified product" in compact_json
    assert latest_return.content == original[4].parts[0].content


def test_prior_company_profile_keeps_fit_and_latest_financing_evidence() -> None:
    attributes = {
        "amount": "10000000",
        "amount_normalized": "$10M",
        "article_sentence": "Example raised Series B funding. " + ("context " * 80),
        "categories": ["funding", "venture_capital"],
        "category": "venture",
        "confidence": 0.99,
        "event": "funding",
        "financing_type": "Series B",
        "financing_type_normalized": "series_b",
        "first_seen_at": "2026-06-23T11:00:00Z",
        "found_at": "2026-06-23T11:00:00Z",
        "summary": "Example completed its latest financing. " + ("detail " * 80),
        "title": "Series B",
    }
    event = {
        "type": "financing_event",
        "attributes": attributes,
        "related": {
            "article": {
                "title": "Example raises Series B",
                "url": "https://example.com/news/series-b",
                "published_at": "2026-06-23",
                "author": "Reporter",
            }
        },
    }
    profile = {
        "domain": "example.com",
        "company": {
            "normalized_domain": "example.com",
            "domain": "example.com",
            "company_name": "Example",
            "industry": "Hardware",
            "location": "Austin, Texas, United States",
            "linkedin_url": "https://linkedin.com/company/example",
            "employee_count": "201-500",
            "year_founded": 2015,
            "updated_at": "2026-09-01T00:00:00Z",
        },
        "latest_financing_events": [
            {
                "source": "predictleads_company_financing_events",
                "data": {
                    "items": [event, event, event],
                    "returned_count": 3,
                    "available_count": 4,
                },
            }
        ],
        "linkedin_profile_evidence": {
            "url": "https://www.linkedin.com/company/example",
            "title": "Example | LinkedIn",
            "employee_count": "201-500",
            "quote": "Company size\n201-500 employees",
        },
        "errors": [],
    }
    history = [
        messages.ModelRequest.user_text_prompt("Verify Example"),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "get_company_profile",
                    {"domain": "example.com"},
                    tool_call_id="profile-1",
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "get_company_profile",
                    profile,
                    tool_call_id="profile-1",
                )
            ]
        ),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "search_web",
                    {"query": "another company"},
                    tool_call_id="search-2",
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "search_web",
                    _large_result("Beta", "b"),
                    tool_call_id="search-2",
                )
            ]
        ),
    ]

    processed = pydantic_ai._process_history(_context(), history)
    compact_profile = processed[2].parts[0].content

    assert len(pydantic_ai._json_bytes(profile)) > 1_200
    assert len(pydantic_ai._json_bytes(compact_profile)) <= 1_200
    assert compact_profile["company"] == profile["company"]
    financing = compact_profile["latest_financing_events"][0]
    assert financing["source"] == "predictleads_company_financing_events"
    latest = financing["data"]["items"][0]
    assert latest["attributes"]["financing_type"] == "Series B"
    assert latest["attributes"]["found_at"] == "2026-06-23T11:00:00Z"
    assert latest["related"]["article"]["url"] == (
        "https://example.com/news/series-b"
    )
    linkedin = compact_profile["linkedin_profile_evidence"]
    assert linkedin["url"] == "https://www.linkedin.com/company/example"
    assert linkedin["employee_count"] == "201-500"
    assert linkedin["quote"] == "Company size\n201-500 employees"


def test_prior_fetch_page_keeps_full_quote_and_url() -> None:
    exact_url = "https://example.com/news/verified-launch?source=company"
    exact_quote = "The company launched its verified platform on August 20, 2026."
    page = {
        "url": exact_url,
        "text": ("Relevant page context. " * 180) + exact_quote,
    }
    history = [
        messages.ModelRequest.user_text_prompt("Verify the launch"),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "fetch_page", {"url": exact_url}, tool_call_id="fetch-1"
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "fetch_page", page, tool_call_id="fetch-1"
                )
            ]
        ),
        messages.ModelResponse(
            parts=[
                messages.ToolCallPart(
                    "search_web", {"query": "another company"}, tool_call_id="call-2"
                )
            ]
        ),
        messages.ModelRequest(
            parts=[
                messages.ToolReturnPart(
                    "search_web",
                    _large_result("Beta", "b"),
                    tool_call_id="call-2",
                )
            ]
        ),
    ]

    processed = pydantic_ai._process_history(_context(), history)
    fetch_return = processed[2].parts[0]

    assert isinstance(fetch_return, messages.ToolReturnPart)
    assert fetch_return.content == page
    assert fetch_return.content["url"] == exact_url
    assert exact_quote in fetch_return.content["text"]


def test_budget_reserve_warns_once_and_hides_only_research_tools() -> None:
    context = _context(input_tokens=pydantic_ai._FINALIZE_INPUT_TOKENS, requests=12, tool_calls=12)
    processed = pydantic_ai._process_history(context, _history())
    processed_again = pydantic_ai._process_history(context, processed)

    warnings = [
        part.content
        for message in processed_again
        if isinstance(message, messages.ModelRequest)
        for part in message.parts
        if isinstance(part, messages.UserPromptPart)
        and isinstance(part.content, str)
        and pydantic_ai._FINALIZE_MARKER in part.content
    ]
    assert len(warnings) == 1
    assert "do not invent" in warnings[0].lower()
    tool_definitions = [SimpleNamespace(name="search_web")]
    assert pydantic_ai._prepare_research_tools(context, tool_definitions) == []

    below_reserve = _context(
        input_tokens=pydantic_ai._FINALIZE_INPUT_TOKENS - 1,
        requests=pydantic_ai._FINALIZE_REQUESTS - 1,
        tool_calls=pydantic_ai._FINALIZE_TOOL_CALLS - 1,
    )
    assert (
        pydantic_ai._prepare_research_tools(below_reserve, tool_definitions)
        == tool_definitions
    )


def test_arena_per_request_output_cap_is_not_the_cumulative_run_limit() -> None:
    limits = pydantic_ai._run_usage_limits()

    assert pydantic_ai._ARENA_REQUEST_OUTPUT_TOKENS == 4_096
    assert limits.output_tokens_limit == 15_000
    assert limits.input_tokens_limit == 260_000
    assert limits.request_limit == 30
    assert limits.tool_calls_limit == 30
    assert limits.cost_limit == pydantic_ai._RUN_COST_LIMIT_USD
    limits.check_tokens(
        RunUsage(
            input_tokens=89_296,
            output_tokens=4_384,
            requests=16,
            tool_calls=15,
        )
    )
