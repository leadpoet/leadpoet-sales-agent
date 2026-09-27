"""Fail-open delivery of model-authored Arena decision summaries."""

from __future__ import annotations

import json
from typing import Any

from experiments.harness_bakeoff.models import DecisionSummary


def record_model_decision(value: Any) -> bool:
    """Forward the model's disclosed summary without affecting research."""

    try:
        summary = (
            value
            if isinstance(value, DecisionSummary)
            else DecisionSummary.model_validate(value)
        )
        import lab_arena_checkpoint

        return bool(
            lab_arena_checkpoint.log_decision(
                **summary.model_dump(mode="json", exclude_none=True)
            )
        )
    except Exception:  # private observability is fail-open
        return False


def research_call(
    budget: Any,
    tool_name: str,
    arguments: dict[str, Any],
    decision_summary: Any,
) -> Any:
    """Record the model decision, then send only provider tool arguments."""

    record_model_decision(decision_summary)
    return budget.call(tool_name, arguments)


def strip_decision_summary(arguments: Any) -> Any:
    """Remove a recorded summary from one historical model tool call."""

    if isinstance(arguments, dict):
        if "decision_summary" not in arguments:
            return arguments
        projected = dict(arguments)
        projected.pop("decision_summary", None)
        return projected
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return arguments
        if not isinstance(parsed, dict) or "decision_summary" not in parsed:
            return arguments
        parsed.pop("decision_summary", None)
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return arguments


__all__ = ["record_model_decision", "research_call", "strip_decision_summary"]
