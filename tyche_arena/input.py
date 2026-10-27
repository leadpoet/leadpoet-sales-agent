"""Render a Leadpoet ICP as the same request text and host bindings a local run uses.

The model interprets this text with tyche_start exactly as it does locally. The host
binds only what the caller owns: the original text, exclusion list, total budget
and research deadline (request-exclusions.json and request-options.json).
"""

import json
from datetime import timedelta

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
           "For a Public stage, the judge confirms a listing most reliably from a current company or newswire passage "
           "naming the company with its exchange and ticker, such as 'Acme Inc. (NASDAQ: ACME) today announced'; "
           "filing-table rows, stock quote pages and sec.gov pages often stay unconfirmed. "
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


# Round arena-2026-10-15: the qualified companies TYCHE never considered (CopilotKit, usecure) were each named by one
# dated page about the requested event, often stating the stage too; TYCHE's category and database searches missed
# them. The opening searches look for those pages first. On TYCHE's round 10-17/10-18 opening queries, Exa's news
# search within the signal's window named 18 field-qualified companies where the free web search named 3-5
# (2026-10-20, $0.01 a call); Deepline now publishes its rate, so provider_pricing bounds it.
OPENING_TOOL = "exa_search"
_SIGNAL_TAILS = (" in the last", " within the last", " within ", ", per ", ", with ", ", as shown", " per ", " (")
_STAGE_QUERIES = {
    "seed": "raises seed round",
    "series a": "raises Series A",
    "series b": "raises Series B",
    "series c+": "raises Series C OR \"Series D\" OR \"Series E\"",
    "private equity": "acquired by private equity OR \"majority investment\"",
}
_STAGE_WORDS = {"seed": "seed", "series a": "\"Series A\"", "series b": "\"Series B\"",
                "series c+": "\"Series C\" OR \"Series D\"", "private equity": "\"private equity\""}


# How press releases word the events Leadpoet's ICP signals name; matched on the signal's own words, first match wins.
_PRESS_WORDS = (
    (("certification", "attestation", "accreditation", "compliance"),
     '(achieves OR earns OR awarded) ("ISO 27001" OR "SOC 2" OR "Cyber Essentials" OR certification)'),
    (("funding", "financing", "capital raise", "raise"), "(raises OR secures OR closes) funding"),
    (("partnership", "partner"), '(partners OR "partnership with" OR "strategic partnership")'),
    (("facility", "plant", "terminal", "depot", "warehouse", "hub", "production line"),
     '(opens OR "opened" OR unveils) new (facility OR plant OR terminal OR warehouse)'),
    (("new office", "metro", "new market", "new country", "new region", "expanded into", "expansion"),
     '(opens OR expands OR "new office" OR launches) in'),
    (("launched", "launch", "new product", "new model", "capability", "release"),
     "(launches OR introduces OR unveils OR announces) new"),
    (("hiring", "job", "roles"), "hiring"),
)


def _press_words(signal):
    """Press-release wording for a signal's event, or '' when no common type matches."""
    folded = signal.casefold()
    return next((words for keys, words in _PRESS_WORDS if any(key in folded for key in keys)), "")


def _signal_core(signal):
    """The signal without its age limit or evidence clause."""
    text = " ".join(signal.split())
    return text[:min((text.find(tail) for tail in _SIGNAL_TAILS if text.find(tail) > 0), default=len(text))]


def _signal_event(signal):
    """The event a signal names, without its age limit, evidence clause or later alternatives."""
    event = _signal_core(signal).split(",")[0].rstrip(" ,.;")
    return event[0].lower() + event[1:] if event else ""


def opening_searches(icp, as_of):
    """Up to six dated news searches for pages that name a company and show intent signal 0 and the stage.

    Leadpoet only. The searches change where discovery starts, never what TYCHE checks or accepts.
    """
    what = _text(icp.get("sub_industry")) or _text(icp.get("industry"))
    where = _text(icp.get("country")) or _text(icp.get("geography"))
    if where.casefold() in _UNSET:
        where = ""
    signal = intent_signals(icp)[0]
    plain_event, plain_stage = _signal_event(signal), company_stage(icp)
    # Match the event, never its evidence clause: "per a press release" once turned a leadership-change signal into
    # product-launch wording (round 10-18). The first alternative goes first, so "hiring for engineering, ...,
    # partnerships ... roles" stays hiring (round 10-17); the whole list still finds "SOC 2, ISO 27001 or certification".
    event = _press_words(_signal_event(signal)) or _press_words(_signal_core(signal)) or _signal_event(signal)
    stage = company_stage(icp).casefold()
    age = intent_signal_ages(icp)[0] or 365
    options = {"category": "news", "type": "auto", "numResults": 10,
               "startPublishedDate": (as_of - timedelta(days=age)).isoformat() + "T00:00:00Z",
               "endPublishedDate": as_of.isoformat() + "T23:59:59Z"}
    place = (" " + where) if where else ""
    searches = []

    def search(purpose, query):
        searches.append({"purpose": purpose, "tool": OPENING_TOOL, "inputs": {"query": " ".join(query.split()), **options}})
    # Exa ranks by meaning: the ICP's own words named 14 field-qualified round 10-17/10-18 companies, the press-release
    # wording below 11, and both together 18 (2026-10-20). Plain wording runs first.
    company = f"{what} company" + (f" in {where}" if where else "")
    if what and plain_event:
        search("Companies named by dated news about intent signal 0", f"{company} {plain_event}")
    if what and plain_event and stage in _STAGE_WORDS:
        search("Companies at the requested stage named by dated news about intent signal 0",
               f"{plain_stage} {company} {plain_event}")
    if what and stage in _STAGE_WORDS:
        search("Companies named by dated news about raising the requested stage", f"{company} raised {plain_stage} funding")
    if what and event and stage in _STAGE_WORDS:
        search("Companies named by a dated page showing intent signal 0 and the requested stage",
               f"{what}{place} {event} {_STAGE_WORDS[stage]}")
    if what and event:
        search("Companies named by a dated page showing intent signal 0", f"{what}{place} {event}")
    if what and stage in _STAGE_QUERIES:
        search("Companies named by a dated page showing the requested stage", f"{what}{place} company {_STAGE_QUERIES[stage]}")
    return searches


def opening_plan(icp, as_of):
    """The task-prompt section that starts discovery with the opening searches; empty when none apply."""
    searches = opening_searches(icp, as_of)
    if not searches:
        return ""
    # "discovery" is the only target a research worker may search under; the number goes in the purpose.
    checks = [json.dumps({"target": "discovery", "purpose": "opening search " + str(index + 1) + ": " + item["purpose"],
                          "phase": "account_discovery", "provider": "deepline", "tool": item["tool"],
                          "inputs": item["inputs"]}) for index, item in enumerate(searches)]
    return ("\n\nOpening discovery: once the run is ready and before any other discovery, run each check below as "
            "its own tyche_lookup discovery pilot, in order (skip one the run already shows as done). You may "
            "reword a query the way a press release would state intent signal 0's event. Take the companies those "
            "pages name and date, best match to intent signal 0 and the stage first, and verify each before "
            "searching more broadly.\n" + "\n".join(checks) + "\n")


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
