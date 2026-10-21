"""Per-ICP research depth and spend from the ICP's kind.

Qualified companies concentrate in a few ICP kinds: the paying intent kinds (``PAYING_INTENTS``) at Series A/B/C+ or
Public outside three weak industries (Lending and Investments, Real Estate, Sales and Marketing).  Discovery stops
after ``draft_limit`` fit-proven drafts, so on those kinds a deeper search, more drafts and a wider admission pool let
more of the five slots fill.

``plan_for`` puts an ICP in one of three tiers:

* ``deep``: a paying intent kind on Series A/B/C+ or Public outside the weak industries.  More discovery queries,
  extracted search rows and candidates, more drafts, a second event pass below five proven drafts, a longer discovery
  window, a wider admission pool ranked by evidence, and a larger reserve for the work after discovery, so the five
  slots can fill.
* ``standard``: a paying intent kind on Seed or in a weak industry.  The promoted strategy's depth.
* ``light``: every other ICP.  The promoted strategy's depth under a lower spend cap.

The deep values need the flat host allowance (``FLAT_MIN_ALLOWANCE_USD`` or more).  Under a lower allowance the
discovery budget is the promoted one ($0.60 at the $0.80 legacy allowance), and ``legacy_plan`` gives the deep tier
the promoted depth, draft limit, pool, call ceiling, second-pass threshold, discovery window and reserve; it keeps the
deep spend cap, the evidence order and the own-site round search.  The harness takes ``legacy_plan`` as well when the
earlier attempts' spend is unknown or still settling, because the governor then runs under its lean or retry cap.

``apply`` sets the module constants the discovery lanes and admission read at call time and returns the values set.
The harness applies ``legacy_plan`` before the governor starts (the first quota snapshot adopts the call ceiling),
then the final plan once the governor knows the host allowance and the earlier attempts' spend; the tier's spend cap
goes to ``Governor.set_mode_cap`` (the host allowance still bounds it).
"""

from __future__ import annotations

from typing import Any, Mapping

PAYING_INTENTS = ("FUNDING", "FINANCING", "PRODUCT_LAUNCH", "LEADERSHIP_CHANGE", "REGULATORY_CLEARANCE")
DEEP_STAGES = ("series a", "series b", "series c+", "public")
WEAK_INDUSTRIES = ("lending and investments", "real estate", "sales and marketing")
FLAT_MIN_ALLOWANCE_USD = 2.0

# ``discovery_max_s``: on a full (uncompressed) clock, discovery may run until half the wall clock, at most this many
# seconds, when that is later than the clock's own discovery end; None keeps the clock's end.
TIERS: dict[str, dict[str, Any]] = {
    "deep": {"depth": 1.7, "draft_limit": 14, "max_pool": 12, "call_budget": 800, "cap_usd": 2.6,
             "second_round_below": 5, "post_scout_reserve_usd": 0.40, "discovery_max_s": 1800.0,
             "evidence_rank": True, "own_round_search": True},
    "standard": {"depth": 1.0, "draft_limit": 8, "max_pool": 8, "call_budget": 360, "cap_usd": 1.6,
                 "second_round_below": 3, "post_scout_reserve_usd": 0.10, "discovery_max_s": None,
                 "evidence_rank": False, "own_round_search": False},
    "light": {"depth": 1.0, "draft_limit": 8, "max_pool": 8, "call_budget": 360, "cap_usd": 0.9,
              "second_round_below": 3, "post_scout_reserve_usd": 0.10, "discovery_max_s": None,
              "evidence_rank": False, "own_round_search": False},
}
# The deep tier under a lower host allowance: the promoted strategy's values.
LEGACY_DEEP = {"depth": 1.0, "draft_limit": 8, "max_pool": 8, "call_budget": 360, "second_round_below": 3,
               "post_scout_reserve_usd": 0.10, "discovery_max_s": None}

# (module, attribute) pairs scaled by the tier's depth; defaults are captured once, before any ICP changes them.
DEPTH_KNOBS = (
    ("scout", "QUERY_COUNT"),
    ("scout", "QUERY_COUNT_VENTURE"),
    ("scout", "MAX_CANDIDATES"),
    ("scout", "ROSTER_WITH_STAGE_FIRST"),
    ("scout", "EXTRACT_MAX_ROWS"),
    ("roster", "ROSTER_SIZE"),
    ("roster", "EVENT_COMPANIES"),
    ("roster", "FEED_COMPANIES"),
    ("stagefirst", "MAX_QUERIES"),
    ("stagefirst", "MAX_COMPANIES"),
)
# (module, attribute, plan key) set to the plan's value as given; a plan without the key restores the default.
ABSOLUTE_KNOBS = (
    ("scout", "SECOND_ROUND_BELOW", "second_round_below"),
    ("scout", "OWN_ROUND_SEARCH", "own_round_search"),
    ("admission", "EVIDENCE_RANK", "evidence_rank"),
)
_DEFAULTS: dict[tuple[str, str], Any] = {}


def _norm(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return " ".join(str(value or "").replace("_", " ").split()).strip().lower()


def tier_for(icp: Mapping[str, Any]) -> str:
    intent = str(icp.get("intent_category") or "").strip().upper()
    if intent not in PAYING_INTENTS:
        return "light"
    stage = _norm(icp.get("company_stage"))
    if stage in ("series c", "series d", "series e", "series c plus"):
        stage = "series c+"
    if stage == "private equity":
        return "light"
    industry = _norm(icp.get("industry"))
    if stage in DEEP_STAGES and industry not in WEAK_INDUSTRIES:
        return "deep"
    return "standard"


def plan_for(icp: Mapping[str, Any]) -> dict[str, Any]:
    tier = tier_for(icp)
    return {"tier": tier, **TIERS[tier]}


def regime_for(allowance_usd: Any) -> str:
    """'flat' when the host allowance is at least FLAT_MIN_ALLOWANCE_USD, else 'legacy'."""

    try:
        allowance = float(allowance_usd)
    except (TypeError, ValueError):
        return "legacy"
    return "flat" if allowance >= FLAT_MIN_ALLOWANCE_USD else "legacy"


def legacy_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The plan under a lower host allowance: the deep tier takes LEGACY_DEEP (its spend cap, evidence order and
    own-site round search stay); every other tier is returned unchanged."""

    if plan.get("tier") != "deep":
        return dict(plan)
    return dict(plan, **LEGACY_DEEP, regime="legacy")


def for_allowance(plan: Mapping[str, Any], allowance_usd: Any) -> dict[str, Any]:
    """The plan for this host allowance, with its ``regime``."""

    regime = regime_for(allowance_usd)
    final = legacy_plan(plan) if regime == "legacy" else dict(plan)
    final["regime"] = regime
    return final


def apply(plan: Mapping[str, Any], modules: Mapping[str, Any]) -> dict[str, Any]:
    """Set the depth knobs for this ICP on ``modules`` ({"scout": module, ...}); returns the values set."""

    depth = float(plan.get("depth") or 1.0)
    applied: dict[str, Any] = {}
    for module_name, attribute in DEPTH_KNOBS:
        module = modules.get(module_name)
        if module is None or not hasattr(module, attribute):
            continue
        key = (module_name, attribute)
        if key not in _DEFAULTS:
            _DEFAULTS[key] = int(getattr(module, attribute))
        value = max(1, int(round(_DEFAULTS[key] * depth)))
        setattr(module, attribute, value)
        applied[f"{module_name}.{attribute}"] = value
    for module_name, attribute, plan_key in ABSOLUTE_KNOBS:
        module = modules.get(module_name)
        if module is None or not hasattr(module, attribute):
            continue
        key = (module_name, attribute)
        if key not in _DEFAULTS:
            _DEFAULTS[key] = getattr(module, attribute)
        default = _DEFAULTS[key]
        raw = plan.get(plan_key)
        raw = default if raw is None else raw
        value = bool(raw) if isinstance(default, bool) else int(raw)
        setattr(module, attribute, value)
        applied[f"{module_name}.{attribute}"] = value
    admission = modules.get("admission")
    if admission is not None and hasattr(admission, "MAX_POOL"):
        admission.MAX_POOL = int(plan.get("max_pool") or admission.MAX_POOL)
        applied["admission.MAX_POOL"] = admission.MAX_POOL
    tools_module = modules.get("arena_tools")
    if tools_module is not None and hasattr(tools_module, "UNCAPPED_CALL_BUDGET"):
        tools_module.UNCAPPED_CALL_BUDGET = int(plan.get("call_budget") or tools_module.UNCAPPED_CALL_BUDGET)
        applied["arena_tools.UNCAPPED_CALL_BUDGET"] = tools_module.UNCAPPED_CALL_BUDGET
    return applied


__all__ = ["PAYING_INTENTS", "TIERS", "LEGACY_DEEP", "DEPTH_KNOBS", "ABSOLUTE_KNOBS", "FLAT_MIN_ALLOWANCE_USD",
           "tier_for", "plan_for", "regime_for", "legacy_plan", "for_allowance", "apply"]
