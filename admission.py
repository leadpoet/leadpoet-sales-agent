"""Which verified companies to submit, and in what order.

Per ICP the judge scores S = (X - c*N) when a company qualifies, else 0 (c = 1000 / (goal * cap)); a qualifier earns
12.75-20 points and a penalty costs about 2.5, so admission is by expected value with no typed-safe precondition:
up to 5 companies on list-type ICPs and 3 otherwise, ranked; a row leaves only on a proven conflict
(``_flags['conflict']``).  On an ICP that names a stage, rows with company_stage_evidence rank first and a row without
it is sent only alone.  ``safety_gaps`` is a diagnostic only.

v91 (two rules, both from the arena-2026-10-05 verdicts):
  * FUNDING ICP on a venture stage: a row whose own primary signal names a round of the ICP's stage is not "bare" --
    the judge reads the stage from that announcement.  One company qualified 13 times on the Seed FUNDING ICP and 11
    of those rows carried no company_stage_evidence; this code had verified the same company and withheld it.
  * LEADERSHIP_CHANGE ICP: a row whose only event is a board-of-directors appointment ranks last and no longer
    counts as a strong row.  Such rows were penalised in 38 of 45 cases (the judge: "board appointments alone do not
    prove that leadership-change condition") and qualified in 4.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Mapping, Optional

from . import criteria
from . import gates
from . import scorer_mirror as sm

# v92: 5 on every ICP (was 3 off the list categories).  10-06: the entries that sent 5 evidenced rows on the
# REGULATORY_CLEARANCE ICP qualified 2-3 of them; this code sent 2 and qualified 1.
DEFAULT_CAP = 5
EVIDENCED_FOR_FILL = 3  # v95: evidenced rows admitted before unproven fit-proven rows may fill the remaining slots
FILL_TIERS = ("A", "B")  # v95: only rows whose fit is proven on their own site may fill (penalties come from fit)
PUBLIC_CAP = 5
LIST_CAP = 5
LIST_CATEGORIES = ("FUNDING", "PRODUCT_LAUNCH", "HIRING", "JOBS", "LEADERSHIP_CHANGE")
SEND_TIERS = ("A", "B", "C")
# 09-28: a stage literal without evidence passed 56/97, with evidence 42/50.
STAGE_LITERAL_Q = 0.7
MAX_POOL = 8
# v98: was 5, so an answer of five companies lost every bonus row in trim_urls.  On-chain 10-07 (first round where
# each ICP has a bonus criterion, cap 80 for two verified signals): the same Amplitude row scored 54 with its launch
# alone and 80 with a native job posting beside it.  Eight keeps one primary per company plus three bonus rows.
MAX_INTENT_URLS = 8
LONE_MIN_QV = 0.55
PUBLIC_NO_JUDGE_PASS_Q = 0.85
PREFLIGHT_EXA_EPS = 0.03
PREFLIGHT_EXA_UNKNOWN_EPS = 0.08
PREFLIGHT_EXA_UNKNOWN_Q = 0.8
CRITICAL_GATES = ("country", "stage_evidence", "normalized_company", "output_document", "exclusion", "evidence_source")
# v91: stages whose proof is a priced-round announcement.
VENTURE_STAGES = ("pre seed", "seed", "series a", "series b", "series c", "series d", "series e", "series c+")
# v110 (eventgrade): published 2026-10-12, the paying ICP - rows graded strong qualified 73%, weak 54%, none 11%,
# 'not such an event' 0 of 8; over all rows 46%.  Two earlier rounds: intent passed 68% vs 15% by event_ok.
EVENT_NOT_OK_Q = 0.15
EVENT_NOT_OK_R = 0.8
CHANGE_GRADE_Q = {"strong": 1.5, "weak": 1.1, "none": 0.4}
CHANGE_NONE_R = 0.6
BOARD_ONLY_Q = 0.3
BOARD_ONLY_R = 0.85
_BOARD_RE = re.compile(
    r"\bboard\s+(?:of\s+directors|appointments?|members?|seats?|director)\b"
    r"|\b(?:joins?|joined|to|elected\s+to|named\s+to|appointed\s+to)\s+(?:the\s+|its\s+|our\s+)?board\b"
    r"|\bindependent\s+director\b", re.I)
_EXEC_ROLE_RE = re.compile(
    r"\b(?:as|new|next|interim)\s+(?:its\s+|the\s+|global\s+|group\s+)?"
    r"(?:chief\b|ceo\b|cfo\b|cto\b|coo\b|cmo\b|cro\b|cio\b|ciso\b|cpo\b|president\b|general\s+manager\b|"
    r"managing\s+director\b|executive\s+vice\s+president\b|senior\s+vice\s+president\b|evp\b|svp\b)"
    r"|\b(?:ceo|cfo|cto|coo)\s+(?:transition|succession|appointment)\b", re.I)
_HEAD_CHARS = 160
# v109: the board-only discount applies only when the criterion's own words ask for an executive role.
_EXEC_CRITERION_RE = re.compile(
    r"\b(?:c[- ]?level|c[- ]?suite|chief|ceo|cfo|cto|coo|cmo|cro|cio|ciso|executive|officer|vice\s+president|\bvp\b|"
    r"head\s+of|president|managing\s+director|general\s+manager)\b", re.I)


def _primary_signals(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    signals = [s for s in row.get("intent_signals") or [] if isinstance(s, Mapping)]
    primary = []
    for signal in signals:
        try:
            if int(signal.get("matched_icp_signal") or 0) == 0:
                primary.append(signal)
        except (TypeError, ValueError):
            continue
    return primary or signals


def _slug_words(url: Any) -> str:
    return re.sub(r"[-_/+.%]+", " ", str(url or ""))


def funding_round_named(row: Mapping[str, Any], icp: Mapping[str, Any]) -> bool:
    """FUNDING criterion on a venture-stage ICP: a primary signal (its URL or description) names a round of the
    ICP's stage.  False on every other ICP."""

    try:
        if criteria.category(icp, 0) != "FUNDING":
            return False
        want = sm.normalize_stage(icp.get("company_stage"))
        if want not in VENTURE_STAGES:
            return False
        from .scout import _ROUND_RE, round_label

        for signal in _primary_signals(row):
            text = _slug_words(signal.get("url")) + " " + re.sub(r"[-\u2010-\u2015]+", " ", str(signal.get("description") or ""))
            for match in _ROUND_RE.finditer(text):
                if sm.stage_matches(round_label(match.group(1)), want):
                    return True
    except Exception:  # noqa: BLE001 - an unreadable row is simply not treated as round-named
        return False
    return False


def board_discounted(icp: Mapping[str, Any]) -> bool:
    """Does the primary criterion name an executive role (so that a director's appointment does not meet it)?"""

    try:
        return bool(_EXEC_CRITERION_RE.search(str(criteria.signals(icp)[0].get("text") or "")))
    except Exception:  # noqa: BLE001
        return False


def board_only_event(row: Mapping[str, Any], icp: Mapping[str, Any]) -> bool:
    """LEADERSHIP_CHANGE criterion: every primary signal is a board-of-directors appointment (its URL or the head
    of its description says so) that names no executive role being taken up.  False on every other ICP."""

    try:
        if criteria.category(icp, 0) != "LEADERSHIP_CHANGE":
            return False
        primary = _primary_signals(row)
        if not primary:
            return False
        for signal in primary:
            text = _slug_words(signal.get("url")) + " | " + str(signal.get("description") or "")[:_HEAD_CHARS]
            if not _BOARD_RE.search(text) or _EXEC_ROLE_RE.search(text):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def stage_backed(row: Mapping[str, Any], icp: Mapping[str, Any]) -> bool:
    """Stage evidence attached, or (FUNDING ICP) the signal itself is the matching round's announcement, or (v92) a
    Public ICP: the judge researches a listing itself, so a listed company without a quoted line is not "bare"."""

    return (has_stage_evidence(row) or funding_round_named(row, icp)
            or sm.normalize_stage(icp.get("company_stage")) == "public")


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
    if flags.get("event_ok") is False:
        q *= EVENT_NOT_OK_Q
        r = max(r, EVENT_NOT_OK_R)
    grade = str(flags.get("change_grade") or "")
    if grade in CHANGE_GRADE_Q:
        q *= CHANGE_GRADE_Q[grade]
        if grade == "none":
            r = max(r, CHANGE_NONE_R)
    if board_only_event(row, icp) and board_discounted(icp):
        q *= BOARD_ONLY_Q
        r = max(r, BOARD_ONLY_R)
    return {"q": max(0.01, min(0.9, q)), "r": max(0.0, min(0.9, r)), "eps": min(0.3, eps)}


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
            "rank_last": bool(flags.get("rank_last")), "round_named": funding_round_named(row, icp),
            "board_only": board_only_event(row, icp), "event_ok": flags.get("event_ok"),
            "change": flags.get("change_grade")}


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
    """Estimated quality; on a staged ICP rows with stage evidence first, then round-named FUNDING rows; a contested
    row (``rank_last``) and a board-only leadership row last."""

    est = estimates(row, icp)
    return (bool((row.get("_flags") or {}).get("rank_last")), (row.get("_flags") or {}).get("event_ok") is False,
            board_only_event(row, icp) and board_discounted(icp),
            staged(icp) and not stage_backed(row, icp), staged(icp) and not has_stage_evidence(row),
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
        strong = (_tier(row) in SEND_TIERS and est["q"] * value(row, icp) >= LONE_MIN_QV
                  and not (board_only_event(row, icp) and board_discounted(icp)))
        if strong or ev > best_ev + 1e-9:
            chosen, best_ev = trial, ev
    return chosen, best_ev


# crown18 (ARENA_LIVE_CELL_FILTER): a failed row beside a qualifier costs 2.5 cell points; alone it costs nothing (the
# cell floors at zero).  Published 2026-10-14, ICP 5 (the one paying cell): fal, a lowercase one-token name with no
# LinkedIn record, failed identity for all 5 senders (Loop, Kong and Liner, one-token names WITH a LinkedIn record,
# qualified or failed on other gates).  Three of our entries sent fal beside Gradial / SkanAI.  Checked against all 345
# rows of that round before use: the rule touches 0 qualified rows.  A "personal LinkedIn post as primary signal" rule
# was tried and dropped: it would have flagged 11 qualified rows (Knowledgehook 6, Atomic Industries 5).  The filter
# runs on the admission POOL, so _admit refills the slot from the next-best row; a risky row alone still goes.
_LOWER_TOKEN_RE = re.compile(r"[a-z0-9]{2,6}")


def live_cell_filter_enabled() -> bool:
    import os
    return str(os.environ.get("ARENA_LIVE_CELL_FILTER", "1")).strip().lower() not in ("0", "false", "no")


def penalty_risk(row: Mapping[str, Any]) -> str:
    """Why this row is a likely penalty beside a qualifier ('' when it is not)."""

    name = str(row.get("company_name") or "").strip()
    if _LOWER_TOKEN_RE.fullmatch(name) and not str(row.get("company_linkedin") or "").strip():
        return "lowercase one-token name with no LinkedIn record"
    return ""


def withhold_penalty_rows(pool: list, icp: Mapping[str, Any]) -> tuple[list, list[str]]:
    """(rows kept, 'name: why' for each row withheld).  Nothing is withheld unless a strong, risk-free row stays."""

    judged = [(r, penalty_risk(r)) for r in pool]
    strong = [r for r, why in judged if not why and _tier(r) in SEND_TIERS and (not staged(icp) or stage_backed(r, icp))]
    if not strong:
        return list(pool), []
    return [r for r, why in judged if not why], [f"{str(r.get('company_name') or '')[:40]}: {why}" for r, why in judged if why]


def select(rows: list[Mapping[str, Any]], icp: Mapping[str, Any], *, limit: int = 5) -> tuple[list[dict], dict]:
    """(chosen rows in rank order, notes): a fit-proven row (tier A/B/C) with q*v >= LONE_MIN_QV always goes in, any
    other row only when it raises the expected score, up to the cap.  On a staged ICP a row without stage evidence
    goes only alone, when no row with evidence is admitted (``notes['no_stage_evidence']`` lists the rest); a FUNDING
    row whose signal names the ICP's round counts as evidenced (``notes['round_named']``)."""

    dropped = {str(r.get("company_name")): conflict(r) for r in rows if conflict(r)}
    dropped.update({str(r.get("company_name")): "company_website is a publisher's site" for r in rows
                    if not conflict(r) and publisher_site(r)})
    ranked = sorted([r for r in rows if not conflict(r) and not publisher_site(r)], key=lambda r: rank_key(r, icp))
    off_category = [r for r in ranked if headline_off_category(r, icp)]
    if off_category and len(off_category) < len(ranked):
        dropped.update({str(r.get("company_name")): "signal headline is not the ICP's kind of event" for r in off_category})
        ranked = [r for r in ranked if not any(r is f for f in off_category)]
    unique, twins = one_per_site(ranked)
    pool = unique[:MAX_POOL]
    cap = max(1, min(cap_for(icp, pool), int(limit or 1)))
    notes: dict[str, Any] = {"pool": [str(r.get("company_name")) for r in pool], "cap": cap}
    if dropped:
        notes["conflicts"] = dropped
    if twins:
        notes["same_site"] = twins
    if live_cell_filter_enabled() and len(pool) >= 2:            # crown18: see withhold_penalty_rows
        pool, held = withhold_penalty_rows(pool, icp)
        if held:
            notes["withheld_live_cell"] = held
    bare = [r for r in pool if staged(icp) and not stage_backed(r, icp)]
    named = [str(r.get("company_name")) for r in pool if staged(icp) and not has_stage_evidence(r) and stage_backed(r, icp)]
    if named:
        notes["round_named"] = named
    chosen, best_ev = _admit([r for r in pool if not any(r is b for b in bare)], icp, cap)
    if bare and not chosen:
        chosen, best_ev = _admit(bare, icp, 1)
    elif bare and len(chosen) >= EVIDENCED_FOR_FILL and len(chosen) < cap:
        # v95: three evidenced rows mean the judge can verify this ICP (on-chain 10-02/03/05/06: such ICPs paid 72% of
        # the time, and the unproven rows sent beside them were worth +2.3 points each; beside two evidenced rows
        # +0.3, beside fewer -1.3).  Fit-proven unproven rows then fill the free slots, best first.
        fill = [r for r in bare if _tier(r) in FILL_TIERS and not board_only_event(r, icp)][: cap - len(chosen)]
        if fill:
            chosen = chosen + fill
            notes["filled_without_stage_evidence"] = [str(r.get("company_name")) for r in fill]
    skipped = [str(r.get("company_name")) for r in bare if not any(r is c for c in chosen)]
    if skipped:
        notes["no_stage_evidence"] = skipped
    out = trim_urls([dict(r) for r in chosen])
    notes.update(chosen=[str(r.get("company_name")) for r in out], expected=round(best_ev, 3))
    return out, notes


# v92: signal-headline gate for REGULATORY_CLEARANCE (the one category where it holds on on-chain rows).  A row is
# flagged when neither the headline of its primary signal nor the slug of its URL carries a word of the category's
# vocabulary (roster.CATEGORY_WORDS).  10-06: "Completes Enrollment in STRIDE Pivotal Trial" was sent as a clearance and
# failed intent 5 of 5.  "Prior authorization" is a payer's approval of a prescription, not a regulator's of a product.
# v96: PARTNERSHIP joins the gate.  Local 10-06 judge runs, every category: the gate flags all 3 rows whose primary intent
# failed (CereVasc, Forus, and ACI Worldwide's product launch sent as a partnership: -10) and 1 of 55 qualified rows.
HEADLINE_GATE_CATEGORIES = ("REGULATORY_CLEARANCE", "PARTNERSHIP")
_PARTNER_EXTRA = re.compile(r"\btaps\b|\bchooses\b|\bjoint\b|\bmemorandum\b|\bMOU\b|\bco-?develop|"
                            r"\b(?:work|team)(?:s|ed|ing)?\s+with\b|\btogether\s+with\b|\bpowered\s+by\b", re.I)


# v96: an IT-security / financial-controls attestation is not a regulator's clearance of a regulated product.  10-06
# local judge runs: Candid Health ("achieves Type 1 SOC 1 certification", a billing-software company) was sent three
# times on a regulated health-care product ICP and penalised three times (required_attribute unavailable), once in
# place of Click Therapeutics, which qualified 4 of 5 times.
_IT_ATTESTATION = re.compile(r"\bSOC\s?[123]\b|\bISO(?:/IEC)?\s?27\d{3}\b|\bHITRUST\b|\bPCI[- ]DSS\b|\bFedRAMP\b|"
                             r"\bStateRAMP\b|\bTX-RAMP\b|\bCMMC\b|\bGDPR\b|\bCyber\s+Essentials\b", re.I)
_PRODUCT_REGULATOR = re.compile(r"\bFDA\b|510\(k\)|\bDe\s+Novo\b|\bPMA\b|\bCE[- ]mark|\bMDR\b|\bIVDR\b|\bHealth\s+Canada\b|"
                                r"\bTGA\b|\bNMPA\b|\bPMDA\b|\bMHRA\b|\bEMA\b|\bCLIA\b|\bJoint\s+Commission\b", re.I)
_IT_ICP = re.compile(r"secur|complian|cloud|software|saas|\bdata\b|cyber|govern|fintech|payment|financ|bank|insur|"
                     r"\bIT\b|information technology", re.I)


def attestation_only(headline: str, icp: Mapping[str, Any]) -> bool:
    """True when the ICP is about a regulated product outside IT / security / finance and the headline names only an
    IT-security or controls attestation (SOC 1/2, ISO 27001, HITRUST ...), no product regulator."""

    described = " ".join(str(icp.get(k) or "") for k in ("required_attribute", "product_service", "sub_industry", "industry"))
    if not described.strip() or _IT_ICP.search(described):
        return False
    return bool(_IT_ATTESTATION.search(headline)) and not _PRODUCT_REGULATOR.search(headline)


_PARTNER_BADGE = re.compile(r"\b(?:preferred|certified|premier|elite|gold|platinum|silver|authori[sz]ed|select|registered)\s+"
                            r"(?:\w+\s+)?partner\b|\bpartner\s+(?:of\s+the\s+year|program(?:me)?|network|status|award|"
                            r"tier|badge|designation)\b", re.I)
_PARTNERSHIP_EVENT = re.compile(r"\bpartnership\b|\bpartners?\s+with\b|\bpartnered\s+with\b|\bcollaborat|\balliance\b|"
                                r"\bagreement\b|\bteams?\s+up\b|\bjoins\s+forces\b|\bjoint\b", re.I)
_PARTNERSHIP_ASIDE = re.compile(r"\b(?:to|of|in|through|under|from|within)\s+(?:the|this|that|their|our)\s+"
                                r"(?:partnership|collaboration|alliance)\b", re.I)
_PARTNERSHIP_ANNOUNCED = re.compile(r"\bannounc|\bpartners?\s+with\b|\bpartnered\b|\brenew|\bexpand|\bextend|\bdeepen|"
                                    r"\bsign(?:s|ed)?\b|\benter(?:s|ed)?\b|\bform(?:s|ed)?\b|\blaunch|\bagreement\b|"
                                    r"\bteams?\s+up\b|\bselects?\b|\btaps\b|\bjoint\b|\bstrategic\b", re.I)
_REGULATED_ADJECTIVE = re.compile(r"\b(?:FDA|CE|EU|MDR|UKCA|Health\s+Canada)[- ](?:approved|cleared|authori[sz]ed|marked|"
                                  r"registered|certified|granted)\b", re.I)
_TRIAL_NEWS = re.compile(r"\bpublished\s+in\b|\b(?:completes?|completed|begins?|began|starts?|started|initiat\w+)\s+"
                         r"(?:patient\s+)?enrol+ment\b|\bfirst\s+patient\b|"
                         r"\b(?:pivotal|clinical|multi-?cent(?:er|re))\s+(?:trial|study)\b[^.]{0,90}\b(?:published|results|"
                         r"data|enrol+\w*|completes?|completed|begins?|initiat\w+|evaluating)\b", re.I)


def _joint_subject(name: Any, headline: str) -> bool:
    """'<Company> and <Other> ...' or '<Other> and <Company> ...': two companies announcing one thing."""

    label = re.sub(r"[\s,]+(?:inc|corp|corporation|llc|ltd|plc|co|holdings|group)\.?$", "", " ".join(str(name or "").split()),
                   flags=re.I)
    if len(label) < 2:
        return False
    source = re.escape(label)
    return bool(re.search(r"\b" + source + r"\b(?:,?\s+Inc\.?)?\s+(?:and|&)\s+[A-Z0-9]", headline, re.I) or
                re.search(r"[A-Za-z0-9.]\s+(?:and|&)\s+" + source + r"\b", headline, re.I))
_ABBREVIATION_END = re.compile(r"(?:\b(?:[A-Za-z]\.){2,4}|\b(?:Inc|Corp|Co|Ltd|Dr|Mr|Ms|Mrs|St|No|vs|Jr|Sr)\.)$")
_NOT_A_CLEARANCE = re.compile(r"\bprior[- ]authori[sz]ations?\b", re.I)
# v96: a step before the regulator's decision is not the decision (10-06 local: "FDA Advisory Committee Votes In Favor of
# Approval of GRAIL's Galleri" was sent as a clearance).
_PENDING_CLEARANCE = re.compile(r"\badvisory\s+(?:committee|panel)\b|\bvotes?\s+in\s+favou?r\b|\bsubmi(?:ts|tted|ssion)\b|"
                                r"\bfil(?:es|ed|ing)\s+(?:for|with)\b|\bapplication\s+(?:accepted|submitted)\b|"
                                r"\baccept(?:s|ed)\b[^.]{0,40}\bfor\s+(?:review|filing)\b|\bunder\s+review\b", re.I)
_GRANTED_CLEARANCE = re.compile(r"\b(?:clears|cleared|approves|approved|grants|granted|receives|received|earns|earned|"
                                r"achieves|achieved|obtains|obtained|secures|secured|awarded|wins|won|gains|gained)\b", re.I)


def signal_headline(description: Any) -> str:
    """The text before the first " | ", else the first sentence (a split after "U.S." or "Inc." is not a sentence end)."""

    text = " ".join(str(description or "").split())
    if " | " in text:
        return text.split(" | ", 1)[0]
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = parts[0]
    for part in parts[1:]:
        if not _ABBREVIATION_END.search(out):
            break
        out += " " + part
    return out


def headline_off_category(row: Mapping[str, Any], icp: Mapping[str, Any]) -> bool:
    """True only on a gated category whose vocabulary appears in neither the primary signal's headline nor its URL slug."""

    category = str(icp.get("intent_category") or "").strip().upper()
    if category not in HEADLINE_GATE_CATEGORIES:
        return False
    try:
        from urllib.parse import unquote, urlsplit
        from .roster import CATEGORY_WORDS
    except Exception:  # noqa: BLE001
        return False
    signals = [s for s in row.get("intent_signals") or [] if isinstance(s, Mapping)]
    primary = [s for s in signals if str(s.get("matched_icp_signal", 0)) in ("0", "0.0")] or signals
    if not primary or category not in CATEGORY_WORDS:
        return False
    try:
        slug = re.sub(r"[-_/+.]", " ", unquote(urlsplit(str(primary[0].get("url") or "")).path))
    except ValueError:
        slug = ""
    headline = signal_headline(primary[0].get("description"))
    if category == "PARTNERSHIP":
        text = f"{headline} {slug}"
        # v97: a partner-programme badge ("is now a Nacha Preferred Partner") is not a partnership announcement
        # (10-06 local: Alkami, primary intent failed, -10).
        if _PARTNER_BADGE.search(headline) and not _PARTNERSHIP_EVENT.search(headline):
            return True
        # A body sentence about an existing partnership ("... brings significant enterprise scale to the partnership")
        # is not its announcement (10-06 local: Paymentus sent twice, primary intent failed twice).
        if _PARTNERSHIP_ASIDE.search(headline) and not _PARTNERSHIP_ANNOUNCED.search(headline):
            return True
        return not (re.search(CATEGORY_WORDS[category], text, re.I) or _PARTNER_EXTRA.search(text)
                    or _joint_subject(row.get("company_name"), headline))
    # v97: "the first FDA-approved stent" describes a product; a trial, its enrolment or its publication is not the
    # regulator's decision (10-06 local: Renata Medical's trial publication sent as a clearance, -10).
    plain = _REGULATED_ADJECTIVE.sub(" ", headline)
    if _TRIAL_NEWS.search(headline) and not _GRANTED_CLEARANCE.search(plain):
        return True
    if _PENDING_CLEARANCE.search(headline) and not _GRANTED_CLEARANCE.search(headline):
        return True
    if attestation_only(headline, icp):
        return True
    text = _NOT_A_CLEARANCE.sub(" ", f"{headline} {slug}")
    # v97: a product regulator's mark named in the headline ("receives EU MDR Class IIb CE mark") is the event's own
    # vocabulary even without "clearance" or "approval".
    return not (re.search(CATEGORY_WORDS[category], text, re.I) or _PRODUCT_REGULATOR.search(text))


# v92: news and newswire sites must never be a company's website.  10-06: a row for Emboline carried
# company_website = biospace.com (the publisher of the article) and cost a penalty.
_PUBLISHER_HOSTS = ("biospace.com", "genengnews.com", "endpts.com", "massdevice.com", "medpath.com", "thefabricator.com",
                    "pharmaceutical-technology.com", "medicaldevice-network.com", "finovate.com", "soapbox.vc",
                    "sdbj.com", "sfbn.org", "vctavern.com", "nasdaq.com", "marketscreener.com", "stocktitan.net",
                    "seekingalpha.com", "benzinga.com", "morningstar.com", "marketwatch.com", "investing.com",
                    "linkedin.com", "medium.com", "substack.com", "youtube.com", "wikipedia.org", "github.com")


def publisher_site(row: Mapping[str, Any]) -> bool:
    """The row's company_website is a known news, newswire, directory or social host."""

    try:
        from .sourcetype import NEWS_HOSTS, WIRE_HOSTS
        host = sm.registrable_host(str(row.get("company_website") or "")) or ""
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in WIRE_HOSTS + NEWS_HOSTS + _PUBLISHER_HOSTS)


def one_per_site(ranked: list[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], list[str]]:
    """(rows, names left out): the best-ranked row per company website.  Two drafts of one company under two names
    (seen in a v91 run: 'Accipiter Bio' and 'Accipiter Biosciences', same site) must not both be sent."""

    seen: set[str] = set()
    kept: list[Mapping[str, Any]] = []
    twins: list[str] = []
    for row in ranked:
        try:
            site = sm.registrable_host(str(row.get("company_website") or "")) or ""
        except Exception:  # noqa: BLE001
            site = ""
        if site and site in seen:
            twins.append(str(row.get("company_name")))
            continue
        if site:
            seen.add(site)
        kept.append(row)
    return kept, twins


def _wire_url(url: Any) -> bool:
    try:
        from .sourcetype import source_kind
        return source_kind(url, "", "") == "wire"
    except Exception:  # noqa: BLE001
        return False


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
                # v92: when a newswire copy of the announcement is cited too, keep that one.  Rows whose company fit
                # passed on-chain (10-03, 10-05, 10-06): newswire primary 91 rows, 81% qualified, 3% paragraph
                # trouble, 54 points; own-site primary 192 rows, 74%, 18%, 51 points.
                wire = [s for s in primary if _wire_url(s.get("url"))]
                if wire and keep is not None and not _wire_url(keep.get("url")):
                    keep = wire[0]
                extra = [s for s in primary if s is not keep]
            else:
                extra = [s for s in signals if s.get("matched_icp_signal") != 0]
            if extra:
                row["intent_signals"] = [s for s in signals if not any(s is e for e in extra)]
                total -= len(extra)
    return rows


__all__ = ["select", "estimates", "expected_score", "typed_safe", "safety_gaps", "explain", "value", "raw_points",
           "cap_for", "list_type", "conflict", "rank_key", "has_stage_evidence", "funding_round_named",
           "board_only_event", "stage_backed", "one_per_site", "publisher_site"]
