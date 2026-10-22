"""Seed lane (s55): ask the model that writes the daily ICPs for the company the brief was written around.

The organizer's generator (gateway/tasks/icp_generator.py, OPENROUTER_MODEL = "perplexity/sonar-pro") must find ONE
real company on the web that fits every criterion -- `verified_example_company` -- before it may emit an ICP, and the
arena strips that field from what a bundle sees (lab_arena/integrity.py ICP_FIELDS). `excluded_companies` is the same
model's temperature-0 answer to the hard profile alone (industry, headquarters, size band, stage), so asking with the
attribute and intent as well reaches different companies.

Measured 2026-10-19 on the 60 ICPs of 10-14..10-19 (117 field-qualified companies, 46 of them ours): this framing named
16 of the 117 on 13 ICPs, 7 of which we never submitted (HEO, Citrin Cooperman, Quaise Energy, nesto, BrainCheck,
Airwallex, Clueso); a strict "verify every criterion" ask named 7 and mostly answered with an empty list. A call costs
$0.007-0.012. The names are candidates only: of the 45 named that some miner had submitted, 16 qualified, so every one
goes through the resolve loop's evidence and gates like any other lane's.

The answer moves between asks: re-asked twice on the 13 ICPs the first ask hit, it named 9 and 11 of those qualifiers
against the first ask's 16 (selection), two in all three, but 23 across the three. A name repeated across asks is only a
weak sign (qualified 21% at two of three, 10% at one, 6% at three -- the always-named are the obvious large companies),
so the lane pools SEED_ASKS asks and puts repeated names first rather than filtering on them.
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping

from . import scorer_mirror as sm
from .arena_tools import STRATEGY, _domain, fetchable

SEED_MODEL = str(STRATEGY.get("seed_model") or "perplexity/sonar-pro")
SEED_MAX = max(1, min(10, int(STRATEGY.get("seed_max") or 10)))          # names asked for per ask
SEED_ASKS = max(1, min(5, int(STRATEGY.get("seed_asks") or 3)))
SEED_KEEP = max(1, min(30, int(STRATEGY.get("seed_keep") or 15)))        # candidates handed on, after pooling
SEED_ASK_MIN_SECONDS = 600.0   # no further ask once the run has less than this left

# A short event phrase per intent category, for dated_event's fallback search when the cited page shows no event.
EVENT_WORDS = {
    "FUNDING": "raises funding round", "PRODUCT_LAUNCH": "launches", "PARTNERSHIP": "partnership",
    "HIRING": "hiring", "EXPANSION": "expands", "ACQUISITION": "acquires", "LEADERSHIP": "appoints",
    "EXECUTIVE_HIRE": "appoints", "REGULATORY": "approval", "AWARD": "award",
}


def seed_prompt(icp: Mapping[str, Any]) -> str:
    signals = [str(s) for s in (icp.get("intent_signals") or [icp.get("intent_signal")]) if s]
    days = int(icp.get("intent_max_age_days") or 365)
    excluded = ", ".join(str(x) for x in icp.get("excluded_companies") or [] if str(x).strip()) or "none"
    bands = icp.get("employee_count")
    bands = ", ".join(bands) if isinstance(bands, (list, tuple)) else str(bands or "")
    return (
        "Today is %s. The B2B prospecting brief below was written by first finding ONE real company on the web that "
        "fits every criterion, then describing that company's market. Name the companies most likely to be that "
        "example, then other real companies that fit the same brief. Prefer companies with a dated public "
        "announcement matching the intent signal within the last %d days. Partial verification is fine; rank by how "
        "well each fits.\n\n"
        "Brief: %s\n"
        "- Industry: %s (%s)\n- Headquarters: %s\n- Employee count (LinkedIn band): %s\n- Stage: %s\n"
        "- Sells: %s\n- Required attribute: %s\n- Intent signal: %s\n- Not these: %s\n\n"
        "List up to %d companies, best first. Return STRICT JSON only, no prose: "
        '[{"name": "<company>", "domain": "<bare homepage domain>", "evidence_url": "<best supporting page>"}]'
        % (sm.evaluation_date().isoformat(), days, icp.get("prompt", ""), icp.get("industry", ""),
           icp.get("sub_industry", ""), icp.get("geography") or icp.get("country", ""), bands,
           icp.get("company_stage", ""), icp.get("product_service", ""), icp.get("required_attribute", ""),
           " | ".join(signals), excluded, SEED_MAX))


def _excluded_keys(icp: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for raw in icp.get("excluded_companies") or []:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        keys.add(_domain(text) if "." in text else sm.company_name_key(text))
    return keys


def _ask_once(icp: Mapping[str, Any], *, llm_json, http_client_factory, deadline) -> list[dict[str, Any]]:
    parsed = llm_json(seed_prompt(icp), http_client_factory=http_client_factory, max_tokens=2000, model=SEED_MODEL,
                      deadline=deadline)
    if isinstance(parsed, dict):
        parsed = parsed.get("companies") or parsed.get("results") or []
    return [row for row in parsed if isinstance(row, dict)] if isinstance(parsed, list) else []


def seed_candidates(icp: Mapping[str, Any], *, llm_json, category: str = "", http_client_factory=None,
                    deadline=None, clock=time.monotonic) -> list[dict[str, Any]]:
    """The seed model's companies over SEED_ASKS asks, pooled, as resolve-loop candidates (source "sonar"): name,
    domain and the page it cited. Excluded companies and rows without a name are dropped; a cited page that cannot be
    fetched is left blank so the loop searches for a dated event instead. Names given in more asks come first, then
    by their best rank in any ask."""

    excluded = _excluded_keys(icp)
    pool: dict[str, dict[str, Any]] = {}
    for ask in range(SEED_ASKS):
        if ask and deadline is not None and clock() >= deadline - SEED_ASK_MIN_SECONDS:
            break
        for rank, row in enumerate(_ask_once(icp, llm_json=llm_json, http_client_factory=http_client_factory,
                                             deadline=deadline)[:SEED_MAX]):
            name = " ".join(str(row.get("name") or "").split())[:120]
            name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip() or name   # "RGP (Resources Connection, Inc.)" -> "RGP"
            key = sm.company_name_key(name)
            domain = _domain(str(row.get("domain") or ""))
            if not key or key in excluded or (domain and domain in excluded):
                continue
            hit = pool.get(key) or next((c for c in pool.values() if domain and c["domain"] == domain), None)
            url = str(row.get("evidence_url") or "").strip()
            url = url if re.match(r"https?://", url) and fetchable(url) else ""
            if hit is None:
                pool[key] = {"company_name": name, "domain": domain, "url": url, "date": None, "snippet": "",
                             "event": EVENT_WORDS.get(category, ""), "fit": "likely", "source": "sonar",
                             "_asks": set(), "_rank": rank}
                hit = pool[key]
            hit["_asks"].add(ask)
            hit["_rank"] = min(hit["_rank"], rank)
            if not hit["url"] and url:
                hit["url"] = url
            if not hit["domain"] and domain:
                hit["domain"] = domain
    ordered = sorted(pool.values(), key=lambda c: (-len(c["_asks"]), c["_rank"]))[:SEED_KEEP]
    out = []
    for c in ordered:
        c["seed_asks"] = len(c.pop("_asks"))
        c.pop("_rank", None)
        out.append(c)
    return out
