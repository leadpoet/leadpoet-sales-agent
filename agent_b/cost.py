
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

PENALIZED_FAILURE_POINTS = 3.4

QUALIFIED_POINTS = 13.0

FREE_FAILURE_CHECKS = ("stage", "industry", "employee_size", "geography", "intent", "intent_details")

PENALIZED_FAILURE_CHECKS = ("required_attribute", "identity")

OBSERVED_PENALTY_RATE = 0.568


def row_value(pass_probability: float, *, penalty_probability: Optional[float] = None,
              qualified_beside: int = 0) -> float:

    p = max(0.0, min(1.0, float(pass_probability)))
    k = OBSERVED_PENALTY_RATE if penalty_probability is None else max(0.0, min(1.0, float(penalty_probability)))
    cost = (PENALIZED_FAILURE_POINTS, PENALIZED_FAILURE_POINTS, 0.5, 0.1)[min(max(int(qualified_beside), 0), 3)]
    return p * QUALIFIED_POINTS - (1.0 - p) * cost * k


def break_even(penalty_probability: Optional[float] = None, *, qualified_beside: int = 0) -> float:

    k = OBSERVED_PENALTY_RATE if penalty_probability is None else max(0.0, min(1.0, float(penalty_probability)))
    cost = (PENALIZED_FAILURE_POINTS, PENALIZED_FAILURE_POINTS, 0.5, 0.1)[min(max(int(qualified_beside), 0), 3)]
    return (cost * k) / (QUALIFIED_POINTS + cost * k) if (QUALIFIED_POINTS + cost * k) else 0.0


def penalized(checks: Mapping[str, Any], *, duplicate: bool = False) -> Optional[bool]:

    if not isinstance(checks, Mapping):
        return None
    if duplicate:
        return True
    for name in PENALIZED_FAILURE_CHECKS:
        if str(checks.get(name)) in ("failed", "unavailable"):
            return True
    return str(checks.get("geography")) == "failed"
