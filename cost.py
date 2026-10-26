"""What a row is worth, and what it costs when it fails, from the scorer's own arithmetic.

Every emission decision in this bundle trades one thing against another: the upside of a row that
qualifies against the damage of one that does not.  Until now that trade used a single constant --
"a failed row costs 4.1 points" -- fitted to one ICP of one round.  It is wrong in a way that
matters: **most failed rows cost nothing at all.**

The scorer computes one ICP like this (``qualification.scoring.competition``)::

    penalty        = (gate_failures + unverified_primary) * fp_penalty_points / goal
    per_icp_score  = max(score_floor, normalized - penalty)

with ``fp_penalty_points = 10.0``, ``score_floor = 0.0`` and ``goal`` the ICP's company goal, so a
penalized row costs ``10/goal`` raw points -- 2.0 at the usual goal of five -- before the round's
available-intent-cap normalization scales the whole ICP to 100.  Measured over the 2,648 scored
positions of 2026-10-01..10-15 that scaling puts the realized cost at **3.4** points of 100, and the
ladder is clean: holding the qualified count at one, positions with 0/1/2/3/4 penalized failures
scored 16.91 / 12.35 / 8.86 / 5.83 / 3.32.

``count_penalizable_false_positives`` decides which failures are counted, and it counts far fewer
than "every row that did not qualify".  A row is penalized when a ``company_fit`` verifier receipt
says ``mismatch``, or when its failure reason contains one of::

    exclusion list        required_attribute      missing employee_count    missing company_stage
    country mismatch      missing country         duplicate company         data quality issue
    missing industry      company verification failed

and never when the reason carries ``error``, ``timeout``, ``provider`` or ``429``.  Notice what is
absent: a stage the judge could not verify, an industry it could not verify, an employee size or a
headquarters it could not verify.  Those failures are **free**.  Of our own 229 failed rows in
October, 99 were free and 130 were not -- and 117 of the 130 were the required attribute.

The floor is the other half of the asymmetry.  ``max(0.0, ...)`` means an ICP cannot go negative, so
the first qualified row carries the whole penalty while later ones are shielded: with one qualified
row the field's positions lost 3.4 per penalized failure, with two they lost 0.5, with three almost
nothing (28.47 -> 27.01 across three failures; 46.44 -> 46.28).

So the break-even pass rate for one more row is not a constant.  It depends on how the row would
fail if it failed, and on how many rows already qualify beside it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

# ``fp_penalty_points`` / ``goal``, scaled by the round's available-intent-cap normalization. The raw
# figure is 2.0 at a goal of five; 3.4 is what that became in the 2,648 scored positions of
# 2026-10-01..10-15, and it is the number to reason in because scores are reported on that scale.
PENALIZED_FAILURE_POINTS = 3.4

# A qualified row's realized contribution on the same 100-point scale: 12.97 at one qualified row in
# the position, 13.73 at two, 15.51 at three, 17.00 at five. The first one is the conservative choice
# for an emission decision, which is always about a row that might be the only one.
QUALIFIED_POINTS = 13.0

# Failures the scorer does not count, by the check that produced them. A row dropped for one of these
# costs nothing beyond the slot it occupies.
FREE_FAILURE_CHECKS = ("stage", "industry", "employee_size", "geography", "intent", "intent_details")

# Failures the scorer does count. ``geography`` appears in both: an unverifiable headquarters is free,
# a headquarters that contradicts the ICP is "country mismatch" and is not.
PENALIZED_FAILURE_CHECKS = ("required_attribute", "identity")

# Share of our own failed rows that were penalized, over the 276 scored rows we have in the published
# corpus. Used when a caller cannot say how a specific row would fail.
OBSERVED_PENALTY_RATE = 0.568


def row_value(pass_probability: float, *, penalty_probability: Optional[float] = None,
              qualified_beside: int = 0) -> float:
    """Points this row is worth in expectation, on the ICP's 100-point scale.

    ``penalty_probability`` is the chance that, if the row fails, it fails in a way the scorer counts.
    Pass ``0.0`` for a row that can only fail on stage or industry and the answer is never negative:
    such a row is a free lottery ticket.  ``qualified_beside`` is how many rows already qualify in
    this ICP -- past the first, the floor absorbs most of the penalty.
    """

    p = max(0.0, min(1.0, float(pass_probability)))
    k = OBSERVED_PENALTY_RATE if penalty_probability is None else max(0.0, min(1.0, float(penalty_probability)))
    # Measured shielding: 3.4 points per penalized failure beside one qualified row, 0.5 beside two,
    # ~0.1 beside three or more.
    cost = (PENALIZED_FAILURE_POINTS, PENALIZED_FAILURE_POINTS, 0.5, 0.1)[min(max(int(qualified_beside), 0), 3)]
    return p * QUALIFIED_POINTS - (1.0 - p) * cost * k


def break_even(penalty_probability: Optional[float] = None, *, qualified_beside: int = 0) -> float:
    """The pass rate at which one more row stops costing and starts paying."""

    k = OBSERVED_PENALTY_RATE if penalty_probability is None else max(0.0, min(1.0, float(penalty_probability)))
    cost = (PENALIZED_FAILURE_POINTS, PENALIZED_FAILURE_POINTS, 0.5, 0.1)[min(max(int(qualified_beside), 0), 3)]
    return (cost * k) / (QUALIFIED_POINTS + cost * k) if (QUALIFIED_POINTS + cost * k) else 0.0


def penalized(checks: Mapping[str, Any], *, duplicate: bool = False) -> Optional[bool]:
    """Would the scorer count this published verdict as a false positive?

    ``checks`` is a published ``company_diagnostics`` row's check map.  ``None`` when the row
    qualified, so there is nothing to count.  This predicate reproduced the score ladder above on the
    published corpus, which is the only test of it that exists.
    """

    if not isinstance(checks, Mapping):
        return None
    if duplicate:
        return True
    for name in PENALIZED_FAILURE_CHECKS:
        if str(checks.get(name)) in ("failed", "unavailable"):
            return True
    return str(checks.get("geography")) == "failed"
