"""Which verified companies to submit, and in what order.

Per ICP the judge scores S = (X - c*N) when a company qualifies, else 0 (c = 1000 / (goal * cap)); a qualifier earns
12.75-20 points and a penalty costs about 2.5, so admission is by expected value with no typed-safe precondition:
up to 5 companies on list-type ICPs and 3 otherwise, ranked; a row leaves only on a proven conflict
(``_flags['conflict']``).  On an ICP that names a stage, rows with company_stage_evidence rank first and a row without
it is sent only alone.  ``safety_gaps`` is a diagnostic only.
"""

from __future__ import annotations

import itertools
from typing import Any, Mapping, Optional

from . import criteria
from . import gates
from . import scorer_mirror as sm

DEFAULT_CAP = 3
PUBLIC_CAP = 5
LIST_CAP = 5
LIST_CATEGORIES = ("FUNDING", "PRODUCT_LAUNCH", "HIRING", "JOBS", "LEADERSHIP_CHANGE")
SEND_TIERS = ("A", "B", "C")
# 09-28: a stage literal without evidence passed 56/97, with evidence 42/50.
STAGE_LITERAL_Q = 0.7
MAX_POOL = 8
MAX_INTENT_URLS = 5
LONE_MIN_QV = 0.55
PUBLIC_NO_JUDGE_PASS_Q = 0.85
PREFLIGHT_EXA_EPS = 0.03
PREFLIGHT_EXA_UNKNOWN_EPS = 0.08
PREFLIGHT_EXA_UNKNOWN_Q = 0.8
CRITICAL_GATES = ("country", "stage_evidence", "normalized_company", "output_document", "exclusion", "evidence_source")


def goal(icp: Mapping[str, Any]) -> int:
    return gates.company_goal(icp) or sm.icp_company_goal(icp)


def score_cap(icp: Mapping[str, Any]) -> float:
    return gates.intent_cap(icp) or (60.0 if criteria.count(icp) <= 1 else 80.0)


def raw_points(row: Mapping[str, Any], icp: Mapping[str, Any]) -> float:
    best: dict[int, int] = {}
    for signal in row.get("intent_signals") or []:
        index = int(signal.get("matched_icp_signal") or 0)
        best[index] = max(best.get(index, 0), criteria.url_points(signal.get("url"), row.get("company_website")))
    primary = best.get(0, 0)
    bonus = sum(v for k, v in best.items() if k > 0)
    return min(score_cap(icp), primary + 0.5 * bonus)


def value(row: Mapping[str, Any], icp: Mapping[str, Any]) -> float:
    return raw_points(row, icp) * 100.0 / (goal(icp) * score_cap(icp))


def estimates(row: Mapping[str, Any], icp: Mapping[str, Any]) -> dict[str, float]:
    """(q, r, eps): chance to qualify, chance of a penalty when not qualified, chance of an untyped failure."""

    flags = row.get("_flags") or {}
    tier = str(flags.get("fit_tier") or (row.get("_fit_proof") or {}).get("tier") or "")
    q = 0.30 * {"A": 1.0, "B": 0.85, "C": 0.35}.get(tier, 0.15)
    r = {"A": 0.2, "B": 0.25, "C": 0.5}.get(tier, 0.7)
    want = sm.normalize_stage(icp.get("company_stage"))
    if want:
        q *= 1.0 if flags.get("stage_proven") else STAGE_LITERAL_Q
        r += 0.0 if flags.get("stage_proven") else 0.05
        if want == "public" and flags.get("stage_proven") and not flags.get("stage_judge_pass", True):
            q *= PUBLIC_NO_JUDGE_PASS_Q
    q *= 1.0 if flags.get("anchored") else 0.8
    q *= 1.0 if flags.get("size_source", "linkedin") == "linkedin" else 0.8
    q *= 1.0 if flags.get("hq_established", True) else 0.8
    eps = 0.04 + (0.0 if flags.get("hq_established", True) else 0.03) + (0.0 if flags.get("anchored") else 0.02)
    primary = [str(v) for v in flags.get("preflight_primary") or []]
    if primary and all(v == "exa_fallback" or v.startswith("exa_unknown") for v in primary):
        if all(v.startswith("exa_unknown") for v in primary):
            q *= PREFLIGHT_EXA_UNKNOWN_Q
            eps += PREFLIGHT_EXA_UNKNOWN_EPS
        else:
            eps += PREFLIGHT_EXA_EPS
    return {"q": max(0.01, min(0.6, q)), "r": max(0.0, min(0.9, r)), "eps": min(0.3, eps)}


def safety_gaps(row: Mapping[str, Any], icp: Optional[Mapping[str, Any]] = None) -> list[str]:
    """Why a row is not typed-safe ([] when it is): fit tier A/B, a required_attribute whose quoted sentence was
    verified (passed), headquarters established, the current-stage lookup done, an identity anchor, every critical
    gate known."""

    flags = row.get("_flags") or {}
    tier = str(flags.get("fit_tier") or (row.get("_fit_proof") or {}).get("tier") or "")
    anchor = bool(flags.get("anchored")) or bool((row.get("_fit_proof") or {}).get("ra_on_primary"))
    gaps: list[str] = []
    if tier not in ("A", "B"):
        gaps.append(f"fit tier {tier or 'none'}")
    claim = row.get("required_attribute")
    wants_attribute = bool(str((icp or {}).get("required_attribute") or "").strip()) or isinstance(claim, Mapping)
    if wants_attribute and not (isinstance(claim, Mapping) and claim.get("passed") is True):
        gaps.append("required_attribute not passed")
    if not flags.get("hq_established", False):
        gaps.append("headquarters not established")
    if not flags.get("stage_lookup", False):
        gaps.append("current-stage lookup not run")
    if not anchor:
        gaps.append("no identity anchor")
    if not gates.all_known(CRITICAL_GATES):
        gaps.append("a critical gate is unknown")
    return gaps


def typed_safe(row: Mapping[str, Any], icp: Optional[Mapping[str, Any]] = None) -> bool:
    return not safety_gaps(row, icp)


def explain(row: Mapping[str, Any], icp: Mapping[str, Any]) -> dict[str, Any]:
    """One row's admission inputs for the run report."""

    flags = row.get("_flags") or {}
    proof = row.get("_fit_proof") or {}
    claim = row.get("required_attribute") if isinstance(row.get("required_attribute"), Mapping) else {}
    est = estimates(row, icp)
    return {"company": str(row.get("company_name") or "")[:60], "tier": str(flags.get("fit_tier") or proof.get("tier") or ""),
            "fit_reason": str(proof.get("reason") or "")[:160], "q": round(est["q"], 3), "value": round(value(row, icp), 2),
            "gaps": safety_gaps(row, icp), "stage_proven": bool(flags.get("stage_proven")),
            "hq": bool(flags.get("hq_established")), "anchored": bool(flags.get("anchored")),
            "ra_passed": claim.get("passed") if claim else None, "ra_quote": str(claim.get("evidence_quote") or "")[:160],
            "source": str(flags.get("source") or ""), "conflict": conflict(row),
            "rank_last": bool(flags.get("rank_last"))}


def expected_score(rows: list[Mapping[str, Any]], icp: Mapping[str, Any]) -> float:
    if not rows:
        return 0.0
    c = 1000.0 / (goal(icp) * score_cap(icp))
    stats = [(estimates(r, icp), value(r, icp)) for r in rows]
    total = 0.0
    for outcome in itertools.product((0, 1, 2), repeat=len(rows)):
        p, x, n = 1.0, 0.0, 0
        for (est, v), o in zip(stats, outcome):
            if o == 0:
                p *= est["q"]
                x += v
            elif o == 1:
                p *= (1.0 - est["q"]) * est["r"]
                n += 1
            else:
                p *= (1.0 - est["q"]) * (1.0 - est["r"])
        if x > 0:
            total += p * (x - c * n)
    survive = 1.0
    for est, _v in stats:
        survive *= 1.0 - est["eps"]
    return survive * total


def list_type(icp: Mapping[str, Any]) -> bool:
    """A FUNDING / PRODUCT_LAUNCH / HIRING / LEADERSHIP_CHANGE primary criterion, or a Public ICP."""

    return criteria.category(icp, 0) in LIST_CATEGORIES or sm.normalize_stage(icp.get("company_stage")) == "public"


def cap_for(icp: Mapping[str, Any], rows: Optional[list[Mapping[str, Any]]] = None) -> int:
    """5 on a list-type ICP, else 3."""

    return LIST_CAP if list_type(icp) else DEFAULT_CAP


def conflict(row: Mapping[str, Any]) -> str:
    """The proven conflict recorded for this row upstream ('' when none)."""

    return str((row.get("_flags") or {}).get("conflict") or "")


def _tier(row: Mapping[str, Any]) -> str:
    flags = row.get("_flags") or {}
    return str(flags.get("fit_tier") or (row.get("_fit_proof") or {}).get("tier") or "")


def staged(icp: Mapping[str, Any]) -> bool:
    return bool(str(icp.get("company_stage") or "").strip())


def has_stage_evidence(row: Mapping[str, Any]) -> bool:
    return bool(row.get("company_stage_evidence")) or bool((row.get("_flags") or {}).get("stage_evidence"))


def rank_key(row: Mapping[str, Any], icp: Mapping[str, Any]) -> tuple:
    """Estimated quality; on a staged ICP rows with stage evidence first; a contested row (``rank_last``) last."""

    est = estimates(row, icp)
    return (bool((row.get("_flags") or {}).get("rank_last")), staged(icp) and not has_stage_evidence(row),
            -est["q"] * value(row, icp), -est["q"])


def _admit(pool: list[Mapping[str, Any]], icp: Mapping[str, Any], cap: int) -> tuple[list[Mapping[str, Any]], float]:
    chosen: list[Mapping[str, Any]] = []
    best_ev = 0.0
    for row in pool:
        if len(chosen) >= cap:
            break
        est = estimates(row, icp)
        trial = chosen + [row]
        ev = expected_score(trial, icp)
        strong = _tier(row) in SEND_TIERS and est["q"] * value(row, icp) >= LONE_MIN_QV
        if strong or ev > best_ev + 1e-9:
            chosen, best_ev = trial, ev
    return chosen, best_ev


def select(rows: list[Mapping[str, Any]], icp: Mapping[str, Any], *, limit: int = 5) -> tuple[list[dict], dict]:
    """(chosen rows in rank order, notes): a fit-proven row (tier A/B/C) with q*v >= LONE_MIN_QV always goes in, any
    other row only when it raises the expected score, up to the cap.  On a staged ICP a row without stage evidence
    goes only alone, when no row with evidence is admitted (``notes['no_stage_evidence']`` lists the rest)."""

    dropped = {str(r.get("company_name")): conflict(r) for r in rows if conflict(r)}
    pool = sorted([r for r in rows if not conflict(r)], key=lambda r: rank_key(r, icp))[:MAX_POOL]
    cap = max(1, min(cap_for(icp, pool), int(limit or 1)))
    notes: dict[str, Any] = {"pool": [str(r.get("company_name")) for r in pool], "cap": cap}
    if dropped:
        notes["conflicts"] = dropped
    bare = [r for r in pool if staged(icp) and not has_stage_evidence(r)]
    chosen, best_ev = _admit([r for r in pool if not any(r is b for b in bare)], icp, cap)
    if bare and not chosen:
        chosen, best_ev = _admit(bare, icp, 1)
    skipped = [str(r.get("company_name")) for r in bare if not any(r is c for c in chosen)]
    if skipped:
        notes["no_stage_evidence"] = skipped
    out = trim_urls([dict(r) for r in chosen])
    notes.update(chosen=[str(r.get("company_name")) for r in out], expected=round(best_ev, 3))
    return out, notes


def trim_urls(rows: list[dict[str, Any]], max_urls: int = MAX_INTENT_URLS) -> list[dict[str, Any]]:
    """Keep the ICP's cited intent URLs within max_urls (judge time): drop second primary URLs, then bonus rows,
    from the lowest-ranked company up.  Each company keeps one primary URL, its own-site announcement if cited."""

    total = sum(len(r.get("intent_signals") or []) for r in rows)
    for want_index in (0, 1):
        for row in reversed(rows):
            if total <= max(max_urls, len(rows)):
                return rows
            signals = list(row.get("intent_signals") or [])
            if want_index == 0:
                primary = [s for s in signals if s.get("matched_icp_signal") == 0]
                site = sm.registrable_host(str(row.get("company_website") or ""))
                own = [s for s in primary if site and sm.registrable_host(str(s.get("url") or "")) == site]
                keep = own[0] if own else (primary[0] if primary else None)
                extra = [s for s in primary if s is not keep]
            else:
                extra = [s for s in signals if s.get("matched_icp_signal") != 0]
            if extra:
                row["intent_signals"] = [s for s in signals if not any(s is e for e in extra)]
                total -= len(extra)
    return rows


__all__ = ["select", "estimates", "expected_score", "typed_safe", "safety_gaps", "explain", "value", "raw_points",
           "cap_for", "list_type", "conflict", "rank_key", "has_stage_evidence"]
