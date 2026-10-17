"""Render a Leadpoet ICP as the same request text and host bindings a local run uses.

The model interprets this text with tyche_start exactly as it does locally. The host
binds only what the caller owns: the original text, exclusion list, total budget
and research deadline (request-exclusions.json and request-options.json).
"""

import json

SIGNAL_PREFIX = "Intent signal "
STAGE_PREFIX = "Required company stage: "
USD_PER_COMPANY = 0.8
_UNSET = {"", "any", "all", "unknown", "n/a", "na", "not specified"}
# What each stage means to Leadpoet's verifier (qualification/scoring/company_evidence_investigator.py), so the
# worker judges the stage Leadpoet scores. On 2026-10-08 a worker read "Private Equity" as "is a private-equity
# firm" and published TPG, KKR, Carlyle and Ares, which Leadpoet's definition rejects. On 2026-10-09 workers read
# "an IPO supersedes it" as meeting the stage and passed Nuix, listed on the ASX since 2020, as Series C+ in all
# four arms of two rounds; Leadpoet's verifier takes the latest completed event, so a listed company is Public.
# Leadpoet keeps the newest completed event it finds. On 2026-10-12 reviews held Navro (Series B, May 2025) and Origis
# (PE-owned since January 2025), both Leadpoet-qualified, for lacking a newer page; no extra lookup is asked for.
_CURRENT = ("; the newest such event in captured sources stays current unless a captured source shows a later round, "
            "sale or listing, with no need for a newer page restating it")
_ROUND = ("its latest completed priced-equity round is {}; debt, grants and planned rounds do not count{}; a company "
          "acquired by another company or listed after an IPO is at the Acquired or Public stage instead and fails "
          "this one" + _CURRENT)  # One sentence, so the worker saves the line whole and the host still matches it.
_LATER = ", and a later completed round moves it to that round's stage"
STAGE_MEANINGS = {
    "seed": _ROUND.format("a seed round", _LATER),
    "series a": _ROUND.format("Series A", _LATER),
    "series b": _ROUND.format("Series B", _LATER),
    "series c+": _ROUND.format("Series C or a later series", ""),
    "private equity": ("a private-equity firm currently holds controlling ownership of this company; being a "
                       "private-equity investor, managing funds or investing in other companies does not count; a "
                       "captured acquisition or majority stake by that firm stays current unless a captured source "
                       "shows a later sale, listing or exit, with no need for a newer page restating it"),
    "public": ("this company's shares are currently listed, shown by a current exchange and ticker; a 'Public "
               "Company' label, a planned IPO or an old listing does not count"),
}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else ""


def intent_signals(icp):
    """The ICP's own signal list, in the order matched_icp_signal indexes."""
    signals = icp.get("intent_signals")
    if not signals and _text(icp.get("intent_signal")):
        signals = [icp["intent_signal"]]
    if not isinstance(signals, list) or not signals or any(not _text(value) for value in signals):
        raise ValueError("ICP intent_signals must be a nonempty list of text")
    return [value.strip() for value in signals]


def intent_signal_ages(icp):
    """Per-signal bonus age overrides, with the caller's explicit primary fallback."""
    primary = icp.get("intent_max_age_days")
    primary = primary if type(primary) is int and primary > 0 else None
    bonus = {
        _text(item.get("intent_signal") or item.get("signal") or item.get("text")): item
        for item in (icp.get("bonus_intents") or []) if isinstance(item, dict)
    }
    ages = []
    for signal in intent_signals(icp):
        item = bonus.get(signal, {})
        raw = item.get("max_age_days")
        if raw is None:
            raw = item.get("intent_max_age_days")
        try:
            age = int(raw)
        except (TypeError, ValueError):
            age = 0
        ages.append(age if age > 0 else primary)
    return ages


def company_stage(icp):
    """The ICP's requested funding stage, or '' when none is required."""
    stage = icp.get("company_stage")
    if isinstance(stage, list):
        stage = next((_text(value) for value in stage if _text(value)), "")
    stage = _text(stage)
    return "" if stage.casefold() in _UNSET else stage


def exclusions(icp):
    values = icp.get("excluded_companies") or []
    if not isinstance(values, list) or any(not _text(value) for value in values):
        raise ValueError("ICP excluded_companies must be a list of text")
    return [value.strip() for value in values]


# How Leadpoet's judge checks a returned company (qualification/scoring/intent_signal_gate.py and
# company_evidence_investigator.py), so the worker accepts what Leadpoet will score. Blind review of the
# 2026-10-08 benchmark found stale or recapped signals, careers pages taken as hiring, and HQ errors. On 2026-10-13
# the most common holds were an unknown stage (46) and an unknown signal (35); the stage and date sentences follow
# the judge (intent_signal_gate: passage, then URL, then byline date; a page with none fails) and the stage lines.
JUDGING = ("Before detailed signal research, check any required current company stage. Reuse current captured "
           "evidence; otherwise search the company's latest funding or ownership without filtering the query "
           "to the requested stage. Prefer a company or investor source with a contiguous passage naming the "
           "company and its current stage or completed funding or ownership event. Check for a later completed priced-equity "
           "round, IPO or acquisition that supersedes it. Plans and debt do not establish a completed round. "
           "The newest completed round, sale, listing or ownership event in captured sources is the current stage "
           "unless a captured source shows a later one; a bare stage label without such an event does not establish it. "
           "Leadpoet's judge accepts an intent signal only when one page names the company and a verbatim passage "
           "of at most 200 characters directly shows the signal's event inside the signal's age limit, dated by a date "
           "in that passage first, else the date in the page URL, else its byline. A page showing none of these "
           "dates fails even when the event is current, such as a job posting with no posted date, and the date you "
           "checked a page never dates it. When dates differ, check the original announcement. An article that "
           "recaps an older event does not renew it. A planned or announced-only event when the signal needs it done, a careers page without "
           "a matching open listing, a posting for a role other than the one the signal names, or a list of minor "
           "improvements when the signal needs a major launch does not show it. It accepts country and state only from a passage that names "
           "the location as the company's headquarters or principal executive office (a LinkedIn record's "
           "headquarters field counts); a state of incorporation (such as Delaware), a planned or sales office, or "
           "an entry in a list of office locations is not headquarters, and when a captured source calls the company "
           "based in another country, its headquarters is not proven. "
           "Arena publishes only the required attribute's first valid captured URL/quote as its primary passage. "
           "Choose that source against the complete condition. When it combines product fit with a recent event, "
           "prefer an announcement that names the company, describes its business capability and dates the qualifying event. "
           "A homepage product description alone does not prove the event, even when the intent fields contain it. "
           "Put the strongest direct passage first; omit changing relative dates such as 'Posted 2 days ago'. "
           "Retain captured evidence for every other required part; an ordinary attribute check may cite several "
           "passages that jointly prove it. A missing part stays unknown. Other saved support is not automatically submitted or fetched. "
           "Review the actual primary, intent-signal and stage URLs in the Arena submission preview. "
           "Follow any stricter source constraint in the original request or saved criterion guidance.")


def stage_line(stage):
    """The request's stage line: the stage, with Leadpoet's meaning when it defines one."""
    meaning = STAGE_MEANINGS.get(stage.casefold())
    return STAGE_PREFIX + stage + (" (" + meaning + ")" if meaning else "")


def required_attributes(icp):
    """Exact standalone must-haves emitted in request.txt and bound at startup."""
    value = icp.get("required_attribute")
    if isinstance(value, list):
        value = ", ".join(_text(item) for item in value if _text(item))
    attribute = _text(value)
    attributes = [attribute] if attribute and attribute.lower() not in {"any", "all", "n/a", "not specified"} else []
    stage = company_stage(icp)
    if stage:
        attributes.append(stage_line(stage))
    sub_industry = icp.get("sub_industry")
    if isinstance(sub_industry, str):
        sub_industry = _text(sub_industry)
        if sub_industry and sub_industry.casefold() not in _UNSET:
            attributes.append(sub_industry)
    return list(dict.fromkeys(attributes))


def request_text(icp, limit):
    """Plain lines, so each criterion stays verbatim for tyche_start's paraphrase check."""
    if not isinstance(icp, dict):
        raise ValueError("ICP must be an object")
    if not 1 <= limit <= 5:
        raise ValueError("company limit must be 1 through 5")
    lines = [f"Find up to {limit} companies that match this ideal customer profile. "
             f"Budget: USD 0.80 per requested company, USD {USD_PER_COMPANY * limit:.2f} total for this run. "
             "Intent signal 0 is required; any later intent "
             "signals are preferred, not required. Each company's employee count must be one of the "
             "listed LinkedIn bands, from its own LinkedIn company record; Leadpoet reads that record's current "
             "band, so the band is the company's current size even after an acquisition, merger or change of "
             "owner. Before confirming a company, "
             "save company.hq_country from its LinkedIn headquarters location or another captured "
             "headquarters source. Arena cannot publish a company with no HQ country. Do not infer "
             "headquarters from a press dateline or service area. " + JUDGING
             + (" Save the Required company stage line as its own required attribute, copied whole."
                if company_stage(icp) else ""),
             ""]
    prompt = _text(icp.get("prompt")) or _text(icp.get("buyer_description"))
    if prompt:
        lines.append("Buyer request: " + prompt)
    for key, label in (("industry", "Industry"), ("sub_industry", "Sub-industry"),
                       ("geography", "Geography"), ("country", "Country"), ("state", "State"),
                       ("product_service", "What the companies offer"),
                       ("required_attribute", "Required attribute")):
        value = icp.get(key)
        if isinstance(value, list):
            value = ", ".join(_text(item) for item in value if _text(item))
        if _text(value) and _text(value).lower() not in {"any", "all", "n/a", "not specified"}:
            lines.append(f"{label}: {_text(value)}")
    stage = company_stage(icp)
    if stage:
        lines.append(stage_line(stage))
    sizes = icp.get("employee_count")
    if isinstance(sizes, list) and sizes:
        lines.append("Employee count (LinkedIn bands): " + ", ".join(_text(v) for v in sizes if _text(v)))
    elif _text(sizes):
        lines.append("Employee count (LinkedIn bands): " + _text(sizes))
    for index, (signal, age) in enumerate(zip(intent_signals(icp), intent_signal_ages(icp))):
        lines.append(f"{SIGNAL_PREFIX}{index}: {signal}")
        if age is not None:
            lines.append(f"Intent signal {index} age limit: {age} days")
    if exclusions(icp):
        lines.append("Excluded companies: " + ", ".join(exclusions(icp)))
    example = _text(icp.get("verified_example_company"))
    if example:
        lines.append("Example of a matching company (do not return it): " + example)
    return "\n".join(lines) + "\n"


def prepare(run_dir, icp, limit, research_seconds):
    """Write the three files the local launcher also uses; return the request file."""
    options_file = run_dir / "request-options.json"
    prior = json.loads(options_file.read_text(encoding="utf-8")) if options_file.exists() else {}
    if not isinstance(prior, dict):
        raise ValueError("request-options.json must be a JSON object")
    request_file = run_dir / "request.txt"
    request_file.write_text(request_text(icp, limit), encoding="utf-8")
    excluded = exclusions(icp)
    example = _text(icp.get("verified_example_company"))
    if example and example not in excluded:
        excluded.append(example)  # The scorer treats the example as already known.
    (run_dir / "request-exclusions.json").write_text(json.dumps(excluded), encoding="utf-8")
    options = {"max_duration_seconds": int(research_seconds), "max_usd": round(USD_PER_COMPANY * limit, 2)}
    if attributes := required_attributes(icp):
        options["required_attributes"] = attributes
    # Arena submits one primary quote, but its judge can corroborate the complete attribute across fetched sources.
    # Keep an explicit or previously saved one-passage binding; preparing a run never loosens that contract.
    if "single_source_attributes" in prior:
        options["single_source_attributes"] = prior["single_source_attributes"]
    options_file.write_text(json.dumps(options), encoding="utf-8")
    return request_file
