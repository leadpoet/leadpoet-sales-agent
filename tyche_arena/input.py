
import json
import re
from datetime import timedelta

SIGNAL_PREFIX = "Intent signal "
STAGE_PREFIX = "Required company stage: "
USD_PER_COMPANY = 1.1
# TYCHE starts a new run with a zero ScrapingDog credit cap unless a limit is given. Leadpoet bills ScrapingDog at
# $0.00005 per credit: 20,000 credits is at most $1 of the ICP.
SCRAPINGDOG_CREDITS = 20_000
SCRAPINGDOG_USD_PER_CREDIT = 0.00005
SCRAPINGDOG_ALLOWANCE_FILE = "scrapingdog-allowance.json"  # Read by tyche_arena.tools at tyche_start.
_UNSET = {"", "any", "all", "unknown", "n/a", "na", "not specified"}
_CURRENT = ("; the newest such event in captured sources stays current unless a captured source shows a later round, "
            "sale or listing, with no need for a newer page restating it")
_ROUND = ("its latest completed priced-equity round is {}; debt, grants and planned rounds do not count{}; a company "
          "acquired by another company or listed after an IPO is at the Acquired or Public stage instead and fails "
          "this one" + _CURRENT)
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
               "Company' label, a planned IPO or an old listing does not count. A company currently wholly or "
               "majority owned by a parent whose shares are currently listed also counts, shown by one sentence that "
               "names both the company and that parent and states the ownership, plus a current exchange and ticker "
               "for that same parent; a bare parent ticker or a minority stake does not count"),
}


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else ""


def intent_signals(icp):
    signals = icp.get("intent_signals")
    if not signals and _text(icp.get("intent_signal")):
        signals = [icp["intent_signal"]]
    if not isinstance(signals, list) or not signals or any(not _text(value) for value in signals):
        raise ValueError("ICP intent_signals must be a nonempty list of text")
    return [value.strip() for value in signals]


def intent_signal_ages(icp):
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
    stage = icp.get("company_stage")
    if isinstance(stage, list):
        stage = next((_text(value) for value in stage if _text(value)), "")
    stage = _text(stage)
    return "" if stage.casefold() in _UNSET else stage


def offering(icp):
    value = _text(icp.get("product_service"))
    return "" if value.casefold() in _UNSET else value


def exclusions(icp):
    values = icp.get("excluded_companies") or []
    if not isinstance(values, list) or any(not _text(value) for value in values):
        raise ValueError("ICP excluded_companies must be a list of text")
    return [value.strip() for value in values]


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
           "in that passage first, else the date in the page URL, else its byline; a page showing none of these "
           "dates fails even when the event is current, and the date you checked a page never dates it. Hiring is the "
           "one exception: an open posting for the requested role family counts with no printed date, because the judge "
           "reads the posting itself and does not penalize a missing posting age. When dates differ, check the original announcement. An article that "
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
    meaning = STAGE_MEANINGS.get(stage.casefold())
    return STAGE_PREFIX + stage + (" (" + meaning + ")" if meaning else "")


def required_attributes(icp):
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
    if not isinstance(icp, dict):
        raise ValueError("ICP must be an object")
    if not 1 <= limit <= 5:
        raise ValueError("company limit must be 1 through 5")
    lines = [f"Find {limit} companies that match this ideal customer profile; {limit} is the target and an unfilled "
             "slot scores nothing. "
             f"Budget: USD {USD_PER_COMPANY:.2f} per requested company, USD {USD_PER_COMPANY * limit:.2f} total for this run. "
             "Intent signal 0 is required; later intent signals are optional bonus points: research one only as the strategy's "
             "bonus rule allows. "
             "Each company's employee count must be one of the "
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
    from .output import requested_us_states  # late import: output imports this module
    allowed = requested_us_states(icp)
    if allowed and len(allowed) > 1:
        lines.append("Headquarters states Leadpoet's judge counts for this geography: " + ", ".join(sorted(allowed))
                     + "; a company headquartered in any other state fails geography, wherever its event happened.")
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
        if offering(icp):
            lines.append("Discovery hint: the 'What the companies offer' line usually describes the excluded company. "
                         "Its direct competitors and lookalikes that meet every requirement are the strongest candidates; "
                         "never return the excluded company itself.")
    example = _text(icp.get("verified_example_company"))
    if example:
        lines.append("Example of a matching company (do not return it): " + example)
    return "\n".join(lines) + "\n"


OPENING_TOOL = "exa_search"
_SIGNAL_TAILS = (" in the last", " within the last", " within ", ", per ", ", with ", ", as shown", " per ", " (")
STAGE_SEARCH_DAYS = 3 * 365
STAGE_FREE_RESULTS = 50
COMPANY_LIST_RESULTS = 50
_STAGE_QUERIES = {
    "seed": "raises seed round",
    "series a": "raises Series A",
    "series b": "raises Series B",
    "series c+": "raises Series C OR \"Series D\" OR \"Series E\"",
    "private equity": "acquired by private equity OR \"majority investment\"",
    "public": "publicly traded NYSE OR Nasdaq investor relations",
}
_STAGE_WORDS = {"seed": "seed", "series a": "\"Series A\"", "series b": "\"Series B\"",
                "series c+": "\"Series C\" OR \"Series D\"", "private equity": "\"private equity\"",
                "public": "(NYSE OR Nasdaq)"}
_STAGE_EVENTS = {"public": "listed on NYSE OR Nasdaq", "private equity": "majority stake acquired by private equity firm"}


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


_ANNOUNCED_FACILITY = re.compile(r"\bor announced\b|\bannounced\b[^.;()]*\bexpansion\b", re.I)


def _press_words(signal):
    folded = signal.casefold()
    words = next((words for keys, words in _PRESS_WORDS if any(key in folded for key in keys)), "")
    if words.startswith('(opens OR "opened" OR unveils) new (facility') and _ANNOUNCED_FACILITY.search(signal):
        words = words.replace("unveils)", "unveils OR announces)", 1)
    return words


def _signal_core(signal):
    text = " ".join(signal.split())
    return text[:min((text.find(tail) for tail in _SIGNAL_TAILS if text.find(tail) > 0), default=len(text))]


HIRING = re.compile(r"\bhiring\b", re.I)


def _signal_event(signal):
    event = _signal_core(signal).split(",")[0].rstrip(" ,.;")
    return event[0].lower() + event[1:] if event else ""


_GOOGLE_COUNTRY = {"united states": "us", "usa": "us", "united kingdom": "uk", "uk": "uk", "canada": "ca", "australia": "au",
                   "germany": "de", "france": "fr", "india": "in", "netherlands": "nl", "spain": "es", "italy": "it"}


def google_country(icp):
    """ScrapingDog's two-letter Google country for the ICP's country, or None."""
    country = _text(icp.get("country")).casefold()
    return _GOOGLE_COUNTRY.get(country) or next((code for name, code in _GOOGLE_COUNTRY.items() if name in country), None)


def opening_searches(icp, as_of):
    what = _text(icp.get("sub_industry")) or _text(icp.get("industry"))
    where = _text(icp.get("geography")) or _text(icp.get("country"))
    if where.casefold() in _UNSET:
        where = ""
    signal = intent_signals(icp)[0]
    plain_event, plain_stage = _signal_event(signal), company_stage(icp)
    event = _press_words(_signal_event(signal)) or _press_words(_signal_core(signal)) or _signal_event(signal)
    stage = company_stage(icp).casefold()
    age = intent_signal_ages(icp)[0] or 365
    options = {"category": "news", "type": "auto", "numResults": 20,
               "startPublishedDate": (as_of - timedelta(days=age)).isoformat() + "T00:00:00Z",
               "endPublishedDate": as_of.isoformat() + "T23:59:59Z"}
    place = (" " + where) if where else ""
    searches = []
    stage_options = {**options, "startPublishedDate": (as_of - timedelta(days=STAGE_SEARCH_DAYS)).isoformat() + "T00:00:00Z"}

    def search(purpose, query, inputs=options):
        searches.append({"purpose": purpose, "tool": OPENING_TOOL, "inputs": {"query": " ".join(query.split()), **inputs}})
    # "headquartered in": a bare "in <place>" pulls events located there (a plant opened in the region by an
    # out-of-region company) instead of companies based there (Nov 10 ICP0: 3 of 5 rival-only finds).
    company = f"{what} company" + (f" headquartered in {where}" if where else "")
    if what and plain_event:
        search("Companies named by dated news about intent signal 0", f"{company} {plain_event}")
    if offering(icp) and plain_event:
        search("Companies offering the requested product or service named by dated news about intent signal 0",
               f"{offering(icp)}{place} {plain_event}")
    if what and plain_event and stage in _STAGE_WORDS:
        search("Companies at the requested stage named by dated news about intent signal 0",
               f"{plain_stage} {company} {plain_event}")
    if what and stage in _STAGE_WORDS:
        search("Companies named by dated news about reaching the requested stage",
               f"{company} {_STAGE_EVENTS.get(stage, f'raised {plain_stage} funding')}", stage_options)
    if what and event and stage in _STAGE_WORDS:
        search("Companies named by a dated page showing intent signal 0 and the requested stage",
               f"{what}{place} {event} {_STAGE_WORDS[stage]}")
    if what and event:
        # No place words: the event may happen anywhere; headquarters is checked from each company's own record.
        search("Companies named by a dated page showing intent signal 0", f"{what} {event}")
    if what and stage in _STAGE_QUERIES:
        search("Companies named by a dated page showing the requested stage", f"{what}{place} company {_STAGE_QUERIES[stage]}",
               stage_options)
    if what and HIRING.search(signal):
        searches.append({"purpose": "Companies with an open posting for intent signal 0", "tool": OPENING_TOOL,
                         "inputs": {"query": " ".join(f"{what}{place} {_signal_core(signal)} job opening".split()),
                                    "type": "auto", "numResults": STAGE_FREE_RESULTS}})
    if stage not in _STAGE_WORDS:
        for item in searches:
            item["inputs"]["numResults"] = STAGE_FREE_RESULTS
    # ScrapingDog Google searches beside Exa: Google ranks exact words, so the sub-industry, the place and the event's
    # press wording surface press releases Exa's semantic ranking buries, and a hiring signal gets Google Jobs, whose
    # rows name the company, role and posting. Results are candidates only; pages are still captured.
    if what:
        country = google_country(icp)
        google = {"results": 10, **({"country": country} if country else {})}
        if plain_event and not HIRING.search(signal):
            searches.append({"purpose": "Google: press releases naming companies with intent signal 0",
                             "provider": "scrapingdog",
                             "inputs": {"operation": "google_search", **google,
                                        "query": " ".join(f"{what}{place} {plain_event} press release".split())}})
        if HIRING.search(signal):
            searches.append({"purpose": "Google Jobs: open postings for intent signal 0", "provider": "scrapingdog",
                             "inputs": {"operation": "google_jobs", **({"country": country} if country else {}),
                                        "query": " ".join(f"{what}{place} {_signal_core(signal)}".split())}})
    return searches


SEED_TOOL = "contextdev_get_web_competitors"
_SEED_DOMAIN = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)/?$", re.I)


def seed_domains(icp):
    found = []
    for name in exclusions(icp) + [_text(icp.get("verified_example_company"))]:
        match = _SEED_DOMAIN.match(name.strip()) if name else None
        if match and match.group(1).lower() not in found:
            found.append(match.group(1).lower())
    return found[:2]


def seed_step(icp):
    checks = [{"target": "discovery", "purpose": f"seed competitors {index + 1}: direct competitors of {domain}",
               "phase": "account_discovery", "provider": "deepline", "tool": SEED_TOOL,
               "inputs": {"domain": domain, "numCompetitors": 10}} for index, domain in enumerate(seed_domains(icp))]
    if not checks:
        return ""
    return ("0. First run each check below as its own tyche_lookup discovery pilot. It lists the direct competitors of "
            "the company this profile was written from, with their domains. Put the ones that plausibly fit the size, "
            "headquarters and offering at the top of your list; skip very large multi-product groups and excluded "
            "companies. If a lookup is refused, times out or returns nothing, go on with step 1 and never retry it.\n"
            + "\n".join(json.dumps(check) for check in checks) + "\n")


def company_list_searches(icp):
    what = _text(icp.get("sub_industry")) or _text(icp.get("industry"))
    if not what:
        return []
    where = _text(icp.get("geography")) or _text(icp.get("country"))
    place = "" if where.casefold() in _UNSET else f" headquartered in {where}"
    queries = [("Companies offering the requested product or service", f"{offering(icp)}{place}")] if offering(icp) else []
    queries += [("Companies in the requested sub-industry", f"{what} company{place}")]
    queries += [("Companies with the required attribute", f"{attribute}{place}") for attribute in required_attributes(icp)[:1]]
    return [{"target": "discovery", "purpose": f"company list {index + 1}: {purpose}", "phase": "account_discovery",
             "provider": "deepline", "tool": OPENING_TOOL,
             "inputs": {"query": " ".join(query.split()), "category": "company", "type": "auto",
                        "numResults": COMPANY_LIST_RESULTS}} for index, (purpose, query) in enumerate(queries)]


def company_first_lane(icp, as_of):
    signal = intent_signals(icp)[0]
    event = _signal_core(signal).rstrip(" ,.;")
    age = intent_signal_ages(icp)[0] or 365
    own_site = {"target": "DOMAIN", "purpose": "intent signal 0: the company's own dated pages",
                "phase": "account_verification", "provider": "deepline", "tool": OPENING_TOOL,
                "inputs": {"query": event, "includeDomains": ["DOMAIN"], "type": "auto", "numResults": 5,
                           "startPublishedDate": (as_of - timedelta(days=age)).isoformat() + "T00:00:00Z",
                           "endPublishedDate": as_of.isoformat() + "T23:59:59Z"}}
    found = ("A page of its own that shows the event, dated inside the age limit, sends the company through the normal "
             "qualification. If that lookup and one news search naming the company find no dated event, hold the company "
             "with that gap at once and take the next name.\n")
    if HIRING.search(signal):
        own_site = {**own_site, "purpose": "intent signal 0: an open posting at the company",
                    "inputs": {"query": f"COMPANY {event} job opening", "type": "auto", "numResults": 8}}
        found = ("In the query, put the company's name in place of COMPANY. An open posting for the requested role family "
                 "at this company, on any site, sends it through the normal qualification. If that lookup finds none, "
                 "hold the company with that gap and take the next name.\n")
    checks = "\n".join(json.dumps(check) for check in company_list_searches(icp))
    return ("Discovery lane: company-first. Worker-1 runs the Opening discovery checks, so skip that section; where the "
            "strategy says to discover event-first, you start from companies instead. Every other rule, requirement "
            "and evidence standard still applies.\n" + seed_step(icp) +
            "1. Before claiming anything, run each check below as its own tyche_lookup discovery pilot:\n" + checks + "\n"
            "2. Make a list of at least ten companies. Each row gives headcount, headquarters and latest funding round: "
            "take the rows whose description plainly states the requested offering or sub-industry and whose facts fit the "
            "request, rows matching the offering line first. "
            "Add the established companies you know fit, long-standing leaders as well as young companies, and leave "
            "out excluded ones. These facts only rank candidates: each requirement is still proved on a cited page.\n"
            "2b. Before claiming, screen the list for free in one discovery check: tyche_lookup with provider deepline, "
            "tool free_simple_company_search and inputs {\"sql\": \"SELECT normalized_domain, employee_count, location "
            "FROM companies WHERE normalized_domain IN ('a.com','b.com') LIMIT 50\"} with the list's domains. "
            "employee_count is the upper bound of the company's size bucket (200 means 51-200, 500 means 201-500, 1000 "
            "means 501-1,000, 5000 means 1,001-5,000). Move a company whose bucket is outside the requested bands, or "
            "whose location names another country or a state the request does not allow, to the end of the list; a "
            "domain with no row is often not the company's own site, so check that domain first. The LinkedIn record "
            "still decides size, so never reject a company on this screen alone.\n"
            "3. Work down the list, best fit first. Claim the domain, then run this lookup first, with the company's "
            "domain in place of DOMAIN:\n" + json.dumps(own_site) + "\n"
            + found +
            "4. When the list runs out, discover from news as worker-1 does.")


def worker_lanes(icp, as_of, count):
    return {2: company_first_lane(icp, as_of)} if count >= 2 else {}


def opening_plan(icp, as_of):
    searches = opening_searches(icp, as_of)
    if not searches:
        return ""
    checks = [json.dumps({"target": "discovery", "purpose": "opening search " + str(index + 1) + ": " + item["purpose"],
                          "phase": "account_discovery", "provider": item.get("provider", "deepline"),
                          **({"tool": item["tool"]} if item.get("provider", "deepline") == "deepline" else {}),
                          "inputs": item["inputs"]}) for index, item in enumerate(searches)]
    return ("\n\nOpening discovery: once the run is ready and before any other discovery, run each check below as "
            "its own tyche_lookup discovery pilot, in order (skip one the run already shows as done). You may "
            "reword a query the way a press release would state intent signal 0's event. Take the companies those "
            "pages name and date, best match to intent signal 0 and the stage first, and verify each before "
            "searching more broadly. A stage search reaches back three years: a company it "
            "names must prove intent signal 0 inside that signal's own age limit, and its own blog, newsroom or "
            "changelog often shows it, so search with includeDomains set to its domain before dropping it.\n"
            + "\n".join(checks) + "\n"
            + "After these searches and before claiming, screen the named companies' domains for free in one discovery "
              "check: tyche_lookup with provider deepline, tool free_simple_company_search and inputs {\"sql\": \"SELECT "
              "normalized_domain, employee_count, location FROM companies WHERE normalized_domain IN ('a.com','b.com') "
              "LIMIT 50\"}. employee_count is the upper bound of the size bucket (200 means 51-200, 500 means 201-500). "
              "Move a company whose bucket is outside the requested bands, or located in another country or a state the "
              "request does not allow, to the end of the list; a domain with no row is often not the company's own site. "
              "The LinkedIn record still decides size, so never reject a company on this screen alone.\n")


def prepare(run_dir, icp, limit, research_seconds):
    options_file = run_dir / "request-options.json"
    prior = json.loads(options_file.read_text(encoding="utf-8")) if options_file.exists() else {}
    if not isinstance(prior, dict):
        raise ValueError("request-options.json must be a JSON object")
    request_file = run_dir / "request.txt"
    request_file.write_text(request_text(icp, limit), encoding="utf-8")
    excluded = exclusions(icp)
    example = _text(icp.get("verified_example_company"))
    if example and example not in excluded:
        excluded.append(example)
    (run_dir / "request-exclusions.json").write_text(json.dumps(excluded), encoding="utf-8")
    options = {"max_duration_seconds": int(research_seconds), "max_usd": round(USD_PER_COMPANY * limit, 2)}
    # The allowance lives in its own file because request-options.json admits only TYCHE's keys.
    (run_dir / SCRAPINGDOG_ALLOWANCE_FILE).write_text(json.dumps(
        {"scrapingdog_credits": SCRAPINGDOG_CREDITS, "scrapingdog_usd_per_credit": SCRAPINGDOG_USD_PER_CREDIT}), encoding="utf-8")
    if attributes := required_attributes(icp):
        options["required_attributes"] = attributes
    if "single_source_attributes" in prior:
        options["single_source_attributes"] = prior["single_source_attributes"]
    options_file.write_text(json.dumps(options), encoding="utf-8")
    return request_file
