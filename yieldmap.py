"""What an ICP is worth before a single call is spent, from the kinds the organizer actually uses.

The board is a fixed vocabulary. Over the 130 ICPs of the thirteen published rounds of 2026-10-01..10-14 there
are exactly **20 industries, 6 company stages, 9 intent categories and 14 geographies**, and not one new value
has ever appeared. So an ICP's kind is known in advance, and so is what that kind has been worth: 6,550 judged
rows say an Artificial Intelligence ICP qualifies 24.5 per cent of its rows and a Real Estate one 0.0 (362
rows, not a single pass); a Public stage 22.4 and Private Equity 2.5; a regulatory clearance 19.2 and an
acquisition 0.7.

The tables below are those rates, Laplace-smoothed toward the 12.0 per cent base with a weight of four rows,
so a thin cell cannot shout. `prior()` multiplies the three lifts. Fitted on the first ten rounds and tested
on the last three, it separates two classes cleanly and does not rank finely inside them:

    predicted          rows   actual qualified
    under 3            556         3.8
    3 to 8             204         2.5
    8 to 15            676        13.0
    15 and over        443        15.6

So it is read as a two-class judgement -- below `PAYS` the ICP has historically been near-barren, at or above
it the rows qualify about four times as often -- and never as a precise number.

It is used to decide **how deep to dig for free**, never to refuse an ICP or to drop a row: the per-ICP floor
is zero, so an extra row in a barren ICP costs nothing, and 3.8 per cent of them still pass.
"""

from __future__ import annotations

from typing import Any, Mapping

BASE = 0.1198
PAYS = 0.08          # the class boundary the held-out rounds put at four times the yield

INDUSTRY = {
    "Artificial Intelligence":   0.2440,   # n=502
    "Payments":                  0.2407,   # n=480
    "Health Care":               0.2325,   # n=269
    "Information Technology":    0.1965,   # n=370
    "Energy":                    0.1939,   # n=179
    "Data and Analytics":        0.1767,   # n=310
    "Privacy and Security":      0.1513,   # n=495
    "Financial Services":        0.1309,   # n=397
    "Transportation":            0.1302,   # n=184
    "Commerce and Shopping":     0.1210,   # n=314
    "Lending and Investments":   0.0828,   # n=485
    "Sales and Marketing":       0.0787,   # n=91
    "Software":                  0.0757,   # n=306
    "Biotechnology":             0.0726,   # n=278
    "Advertising":               0.0708,   # n=271
    "Education":                 0.0393,   # n=288
    "Manufacturing":             0.0156,   # n=348
    "Professional Services":     0.0154,   # n=352
    "Hardware":                  0.0054,   # n=269
    "Real Estate":               0.0013,   # n=362
}

COMPANY_STAGE = {
    "Public":                    0.2239,   # n=1012
    "Series B":                  0.1364,   # n=1517
    "Series C+":                 0.1226,   # n=1077
    "Seed":                      0.0999,   # n=892
    "Series A":                  0.0919,   # n=1176
    "Private Equity":            0.0255,   # n=876
}

INTENT_CATEGORY = {
    "REGULATORY_CLEARANCE":      0.1915,   # n=317
    "LEADERSHIP_CHANGE":         0.1844,   # n=541
    "FUNDING":                   0.1695,   # n=1279
    "PRODUCT_LAUNCH":            0.1501,   # n=1498
    "PARTNERSHIP":               0.1042,   # n=826
    "HIRING":                    0.0582,   # n=382
    "MARKET_EXPANSION":          0.0543,   # n=741
    "FACILITY_OPENING":          0.0470,   # n=687
    "ACQUISITION":               0.0088,   # n=279
}


def _lift(table: Mapping[str, float], value: Any) -> float:
    rate = table.get(" ".join(str(value or "").split()))
    return (rate / BASE) if rate and BASE else 1.0


def prior(icp: Mapping[str, Any]) -> float:
    """The share of rows this ICP's kind has historically qualified. Never raises; BASE for an unknown kind."""

    if not isinstance(icp, Mapping):
        return BASE
    value = BASE
    value *= _lift(INDUSTRY, icp.get("industry"))
    value *= _lift(COMPANY_STAGE, icp.get("company_stage"))
    value *= _lift(INTENT_CATEGORY, icp.get("intent_category"))
    return max(0.0, min(1.0, value))


def pays(icp: Mapping[str, Any]) -> bool:
    """True when this kind of ICP has historically paid: 13.0-15.6 per cent of rows against 2.5-3.8 below."""

    return prior(icp) >= PAYS


__all__ = ["prior", "pays", "BASE", "PAYS", "INDUSTRY", "COMPANY_STAGE", "INTENT_CATEGORY"]
