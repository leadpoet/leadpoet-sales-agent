"""Model-authored decision summaries stay private and out of scored output."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from experiments.harness_bakeoff.decision_logging import (
    research_call,
    strip_decision_summary,
)
from experiments.harness_bakeoff.models import (
    CompaniesResult,
    DecisionSummary,
    validate_companies,
)
from experiments.harness_bakeoff.tool_contract import tool_input_schema


SUMMARY = {
    "objective": "Verify Acme's current funding event.",
    "evidence": ["https://example.com/acme-series-b"],
    "rationale": "The dated article names Acme and a Series B round.",
    "next_action": "Fetch the article to verify its exact wording.",
    "decision": "investigate",
    "candidate": "Acme",
}


@pytest.mark.parametrize(
    "name",
    [
        "search_companies",
        "get_company_profile",
        "get_company_events",
        "search_web",
        "fetch_page",
    ],
)
def test_research_tool_schema_requires_bounded_decision_summary(name: str) -> None:
    schema = tool_input_schema(name)
    assert "decision_summary" in schema["required"]
    decision = schema["properties"]["decision_summary"]
    assert decision["additionalProperties"] is False
    assert decision["properties"]["evidence"]["maxItems"] == 5
    assert decision["properties"]["objective"]["maxLength"] == 500
    assert decision["properties"]["decision"]["enum"] == [
        "investigate",
        "accept",
        "reject",
        "defer",
        "finish",
    ]


def test_decision_summary_rejects_unbounded_or_unknown_content() -> None:
    assert DecisionSummary.model_validate(SUMMARY).candidate == "Acme"
    with pytest.raises(ValidationError):
        DecisionSummary.model_validate({**SUMMARY, "evidence": ["fact"] * 6})
    with pytest.raises(ValidationError):
        DecisionSummary.model_validate({**SUMMARY, "rationale": "x" * 501})
    with pytest.raises(ValidationError):
        DecisionSummary.model_validate({**SUMMARY, "private_reasoning": "hidden"})


def test_research_call_logs_exact_model_fields_without_provider_arg_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict] = []
    monkeypatch.setitem(
        sys.modules,
        "lab_arena_checkpoint",
        SimpleNamespace(log_decision=lambda **value: recorded.append(value) or True),
    )

    class Budget:
        calls: list[tuple[str, dict]] = []

        def call(self, name: str, arguments: dict) -> dict:
            self.calls.append((name, arguments))
            return {"ok": True}

    budget = Budget()
    provider_arguments = {"query": "Acme Series B", "mode": "news"}
    assert research_call(
        budget, "search_web", provider_arguments, SUMMARY
    ) == {"ok": True}
    assert recorded == [SUMMARY]
    assert budget.calls == [("search_web", provider_arguments)]
    assert "decision_summary" not in budget.calls[0][1]


def test_decision_logging_failure_does_not_block_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**_value: object) -> bool:
        raise RuntimeError("private log unavailable")

    monkeypatch.setitem(
        sys.modules,
        "lab_arena_checkpoint",
        SimpleNamespace(log_decision=fail),
    )

    class Budget:
        def call(self, name: str, arguments: dict) -> tuple[str, dict]:
            return name, arguments

    assert research_call(Budget(), "fetch_page", {"url": "https://example.com"}, SUMMARY) == (
        "fetch_page",
        {"url": "https://example.com"},
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"query": "Acme", "decision_summary": SUMMARY},
            {"query": "Acme"},
        ),
        (
            '{"query":"Acme","decision_summary":{"decision":"investigate"}}',
            '{"query":"Acme"}',
        ),
        ({"query": "Acme"}, {"query": "Acme"}),
        ("not-json", "not-json"),
    ],
)
def test_recorded_summary_is_removed_from_later_model_context(
    arguments: object, expected: object
) -> None:
    assert strip_decision_summary(arguments) == expected


def test_final_summary_is_finish_only_and_stripped_from_scored_output() -> None:
    output = CompaniesResult.model_validate(
        {
            "companies": [],
            "decision_summary": {
                **SUMMARY,
                "decision": "finish",
                "next_action": "Submit the verified companies.",
            },
        }
    ).model_dump(mode="json")
    summary = output.pop("decision_summary")
    assert summary["decision"] == "finish"
    assert output == {"companies": []}
    assert validate_companies(output) == []

    with pytest.raises(ValidationError):
        CompaniesResult.model_validate(
            {"companies": [], "decision_summary": SUMMARY}
        )
