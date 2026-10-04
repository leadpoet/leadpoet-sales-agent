"""S8 "roster": company-first discovery beside scout's event-first search."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping, Optional

from . import scorer_mirror as sm
from .arena_tools import STRATEGY, BudgetExhausted, _domain

ROSTER_MODEL = str(STRATEGY.get("roster_model") or "anthropic/claude-sonnet-4.5")
ROSTER_SIZE = int(STRATEGY.get("roster_size") or 30)
COMPANY_SEARCH = bool(STRATEGY.get("roster_company_search", 1))
MAX_ROSTER = 45
NEWS_PER_COMPANY = 10
NEWS_WORKERS = 4
EVENT_SEARCH = bool(STRATEGY.get("roster_event_search", 1))
EVENT_COMPANIES = int(STRATEGY.get("roster_event_companies") or 16)
SATURATED_ROWS = 8
CLASSIFY_BATCH_CHARS = 16_000

CATEGORY_WORDS = {
    "FUNDING": r"rais|fund|series [a-h]|seed|invest|financ|round|capital|backed|valuation",
    "LEADERSHIP_CHANGE": r"appoint|\bnames?\b|\bnamed\b|hires?\b|\bhired\b|join|promot|chief|\bceo\b|\bcfo\b|\bcto\b|\bcoo\b|"
                         r"\bcmo\b|\bcro\b|\bcpo\b|\bciso\b|president|\bvp\b|vice president|head of|board|leadership|succe|steps down",
    "PRODUCT_LAUNCH": r"launch|unveil|introduc|releas|debut|rolls? out|announc|now available|new (?:product|platform|feature|capabilit)",
    "PARTNERSHIP": r"partner|collaborat|alliance|integrat|teams? up|joins forces|agreement|selects|signs|deal",
    "MARKET_EXPANSION": r"expan|enter|launch(?:es|ed)? in|new market|new office|opens?\b|opened|arriv|footprint|region|international",
    "FACILITY_OPENING": r"open|facility|plant|factory|\blabs?\b|laborator|headquarter|office|site|manufactur|warehouse|campus|"
                        r"cent(?:er|re)|expan",
    "ACQUISITION": r"acqui|merg|buys|purchas|takeover|combin",
    "REGULATORY_CLEARANCE": r"clearance|approv|certif|fedramp|soc ?2|iso ?\d|fda|authori[sz]|complian|accredit|licen[cs]|cleared",
    "HIRING": r"hir|jobs?\b|recruit|headcount|talent|team|workforce|careers",
}
_KIND_CATEGORY = {"leadership": "LEADERSHIP_CHANGE", "hiring": "HIRING", "funding": "FUNDING", "acquisition": "ACQUISITION",
                  "partnership": "PARTNERSHIP", "launch": "PRODUCT_LAUNCH", "expansion": "MARKET_EXPANSION"}


def intent_category(icp: Mapping[str, Any], kind: str) -> str:
    category = str(icp.get("intent_category") or "").strip().upper()
    return category if category in CATEGORY_WORDS else _KIND_CATEGORY.get(kind, "")


def roster_prompt(icp: Mapping[str, Any]) -> str:
    excluded = [str(x).strip() for x in icp.get("excluded_companies") or [] if str(x).strip()]
    brief = {k: icp.get(k) for k in ("industry", "sub_industry", "product_service", "required_attribute", "company_stage",
                                     "employee_count", "geography", "country", "intent_signals", "intent_max_age_days")}
    seed = ("The profile was written from %s, which is EXCLUDED (never list it); start with its closest peers and "
            "competitors that also meet every criterion. " % ", ".join(excluded)) if excluded else ""
    return (
        "List %d REAL companies that meet EVERY hard criterion of this ideal customer profile: they sell what "
        "product_service and required_attribute describe; their HEADQUARTERS is inside the geography (a named US region "
        "or city means the headquarters is there -- an office there is not enough); their LinkedIn employee count is "
        "inside employee_count; their stage is company_stage (Seed / Series A / Series B = the latest priced round, so "
        "young startups; Series C+ = raised Series C or later and still private and venture-backed; Private Equity = "
        "owned by a private-equity firm; Public = listed on a stock exchange). %s"
        "When required_attribute says the company operates or runs a business (a school, carrier, lender, clinic, "
        "energy producer, payments processor, consultancy), list only companies that ARE that business: a software "
        "or service vendor selling to such businesses does not qualify unless product_service or required_attribute "
        "itself asks for software or a platform. Prefer companies likely to have shown "
        "the intent signal within intent_max_age_days before %s. Every company is verified afterwards, so include "
        "likely fits rather than stopping early, but never invent a company, and list only companies operating today "
        "under that name. Give each company's primary website domain exactly (e.g. acme.com), its current stock "
        "ticker as EXCHANGE: SYMBOL (\"\" when not listed), and core = false when its main business is not "
        "sub_industry (an adjacent business, e.g. a residential brokerage for a commercial real estate profile). "
        "Return {\"companies\": [{\"name\": \"...\", \"domain\": \"...\", \"ticker\": \"...\", \"core\": true}]}."
        "\n\nICP: %s" % (ROSTER_SIZE, seed, sm.evaluation_date().isoformat(), json.dumps(brief, default=str)[:3000]))


def roster(icp: Mapping[str, Any], *, llm_json, http_client_factory=None, notes: Optional[dict] = None) -> list[dict[str, Any]]:
    """The model's peer list.  Every roster company costs a news lookup from the per-ICP Deepline call quota, so an
    entry the model marks off-core, or (for a Public ICP, once the model gives tickers at all) one without a ticker,
    is skipped before any lookup."""

    parsed = llm_json(roster_prompt(icp), http_client_factory=http_client_factory, max_tokens=3000, model=ROSTER_MODEL)
    raw = parsed.get("companies") if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
    raw = [item for item in (raw if isinstance(raw, list) else []) if isinstance(item, dict)]
    excluded = [str(x).strip().lower() for x in icp.get("excluded_companies") or [] if str(x).strip()]
    banned = {sm.company_name_key(x) for x in excluded} | {_domain(x) for x in excluded if "." in x}
    public = sm.normalize_stage(icp.get("company_stage")) == "public"
    tickers = any(str(item.get("ticker") or "").strip() for item in raw)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if item.get("core") is False or (public and tickers and not str(item.get("ticker") or "").strip()):
            if notes is not None:
                notes.setdefault("skipped", []).append(str(item.get("name") or "")[:60])
            continue
        name = " ".join(str(item.get("name") or item.get("company_name") or "").split())[:120]
        domain = _domain(str(item.get("domain") or ""))
        key = sm.company_name_key(name)
        if not name or not domain or not key or key in banned or domain in banned or key in seen or domain in seen:
            continue
        seen.update({key, domain})
        out.append({"company_name": name, "domain": domain})
    return out[:ROSTER_SIZE]


_STAGE_WORDS = {"seed": "seed-stage startup", "pre seed": "seed-stage startup", "series a": "Series A startup",
                "series b": "venture-backed startup", "series c+": "late-stage venture-backed company",
                "private equity": "private equity backed company", "public": "publicly traded company"}
_NOT_COMPANY_HOSTS = ("linktr.ee", "linkedin.", "facebook.", "crunchbase.", "wikipedia.", "medium.", "youtube.",
                      "instagram.", "twitter.", "x.com", "github.", "google.", "apple.com")


def company_search_queries(icp: Mapping[str, Any]) -> list[str]:
    """Two Exa company-category queries."""

    from .scout import region_places

    sub = " ".join(str(icp.get("sub_industry") or icp.get("industry") or "").split())[:80]
    product = " ".join(str(icp.get("product_service") or "").split())[:140]
    stage = _STAGE_WORDS.get(sm.normalize_stage(icp.get("company_stage")), "company")
    geography = " ".join(str(icp.get("geography") or icp.get("country") or "").split())
    places = region_places(icp) or [geography]
    first = f"{sub} {stage} headquartered in {places[0]}".strip()
    second = f"{product or sub} company headquartered in {places[1] if len(places) > 1 else places[0]}".strip()
    return [q for q in (first, second) if q]


def company_search(tools: Any, icp: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Company homepages from Exa's company category (title = name, url = the site); $0.01 per query."""

    from .arena_tools import _result_data

    out: list[dict[str, Any]] = []
    for query in company_search_queries(icp):
        try:
            data = _result_data(tools._deepline("exa_search", {"query": query[:500], "type": "auto", "numResults": 10,
                                                               "category": "company"}))
        except BudgetExhausted:
            raise
        except Exception:
            continue
        for row in data.get("results") or []:
            if not isinstance(row, dict):
                continue
            domain = _domain(str(row.get("url") or ""))
            name = re.split(r"\s+[-|–—:]\s+|:\s+", " ".join(str(row.get("title") or "").split()))[0].strip()[:120]
            if not domain or not name or any(bad in domain for bad in _NOT_COMPANY_HOSTS):
                continue
            out.append({"company_name": name, "domain": domain, "source": "exa_company"})
    return out


def company_news(tools: Any, domain: str) -> list[dict[str, Any]]:
    """One company's dated stories from the free per-company news lookup (cached on the tools object)."""

    cache = tools.__dict__.setdefault("_roster_news", {})
    if domain in cache:
        return cache[domain]
    try:
        rows = (tools.search_news(domain, limit=NEWS_PER_COMPANY, company_website=f"https://{domain}/") or {}).get("results") or []
    except BudgetExhausted:
        raise
    except Exception:
        rows = []
    cache[domain] = [r for r in rows if isinstance(r, dict)]
    return cache[domain]


def read_news(tools: Any, companies: list[dict[str, Any]], *, deadline: float, clock) -> dict[str, list[dict[str, Any]]]:
    def one(company: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if clock() >= deadline:
            return company["domain"], []
        try:
            return company["domain"], company_news(tools, company["domain"])
        except BudgetExhausted:
            return company["domain"], []

    with ThreadPoolExecutor(max_workers=NEWS_WORKERS) as pool:
        return dict(pool.map(one, companies))


def story_rows(icp: Mapping[str, Any], companies: list[dict[str, Any]], news: Mapping[str, list[dict[str, Any]]],
               category: str, *, age_days) -> list[dict[str, Any]]:
    window = int(icp.get("intent_max_age_days") or 365)
    pattern = re.compile(CATEGORY_WORDS[category], re.I) if category in CATEGORY_WORDS else None
    rows: list[dict[str, Any]] = []
    for company in companies:
        for story in news.get(company["domain"]) or []:
            if story.get("match") not in (None, "primary"):
                continue
            age = age_days(story.get("published_date"))
            if age is None or not 0 <= age <= window:
                continue
            title, excerpt = str(story.get("title") or ""), str(story.get("excerpt") or "")
            if pattern is not None and not pattern.search(f"{title} {excerpt}"):
                continue
            rows.append({"id": len(rows), "company": company["company_name"], "domain": company["domain"],
                         "date": str(story.get("published_date"))[:10], "title": title[:200], "excerpt": excerpt[:300],
                         "url": str(story.get("url") or "")})
    return rows


def short_feed(stories: list[Mapping[str, Any]], window: int, *, age_days) -> bool:
    """True when the free lookup is saturated (>= SATURATED_ROWS dated stories) and its oldest story is younger than."""

    ages = [a for a in (age_days(s.get("published_date")) for s in stories or []) if a is not None]
    return len(ages) >= SATURATED_ROWS and max(ages) < max(30, int(window or 365) // 2)


def searched_rows(icp: Mapping[str, Any], tools: Any, companies: list[dict[str, Any]], news: Mapping[str, list[dict[str, Any]]],
                  rows: list[dict[str, Any]], category: str, *, age_days, deadline: float, clock,
                  tried: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Story rows from one Exa event search per short-feed company, in roster order, capped at EVENT_COMPANIES."""

    if not EVENT_SEARCH or category not in CATEGORY_WORDS or category == "HIRING":
        return []
    window = int(icp.get("intent_max_age_days") or 365)
    todo = [c for c in companies if short_feed(news.get(c["domain"]) or [], window, age_days=age_days)][:max(0, EVENT_COMPANIES)]
    if tried is not None:
        tried.extend(c["company_name"] for c in todo)
    if not todo:
        return []
    from .stagefirst import company_events
    return company_events(tools, todo, icp, category, age_days=age_days, deadline=deadline, clock=clock,
                          over_budget=lambda: False, limit=EVENT_COMPANIES)


def classify_prompts(icp: Mapping[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    listing = [{k: r[k] for k in ("id", "company", "date", "title", "excerpt")} for r in rows]
    batches: list[list[dict[str, Any]]] = [[]]
    for item in listing:
        if batches[-1] and len(json.dumps(batches[-1] + [item])) > CLASSIFY_BATCH_CHARS:
            batches.append([])
        batches[-1].append(item)
    from .scout import primary_signal
    signal = primary_signal(icp)
    return [(
        "Each row is a dated news story about the named company. Keep only the rows that report that THIS company "
        "itself did what the intent signal describes -- the event itself (e.g. its own funding round, its own "
        "executive appointment, its own launch, its own new office), not commentary, research findings, awards, "
        "customer stories, an investor's or partner's news, or a mere mention. An existing product made available "
        "in a new country or market is NOT a new product or capability; an acquisition counts only when the story "
        "says it closed or completed or the target now operates as part of the company, not an agreement or plan; "
        "a story where THIS company is acquired, bought, merged into another or taken private never qualifies (for an "
        "acquisition intent THIS company must be the acquirer); "
        "a job posting is not a hire. Return EVERY row that reports such an event (several rows may cover the same "
        "company). Return {\"hits\": [{\"id\": <row id>, \"event\": \"<max 15 words>\"}]}.\n\n"
        "INTENT SIGNAL: %s\n\nROWS: %s" % (signal[:600], json.dumps(batch)))
        for batch in batches if batch]


_SOURCE_RANK = {"first_party": 0, "wire": 1, "news": 2, "linkedin": 4}


def _kind(row: Mapping[str, Any]) -> str:
    try:
        from .sourcetype import source_kind
        return source_kind(row.get("url"), row.get("domain"), row.get("company"))
    except Exception:
        return "unknown"


def pick_events(icp: Mapping[str, Any], rows: list[dict[str, Any]], *, llm_json, http_client_factory=None) -> list[dict[str, Any]]:
    by_id = {r["id"]: r for r in rows}
    hits: list[dict[str, Any]] = []
    for prompt in classify_prompts(icp, rows):
        parsed = llm_json(prompt, http_client_factory=http_client_factory, max_tokens=3000)
        found = parsed.get("hits") if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
        hits.extend(h for h in (found if isinstance(found, list) else []) if isinstance(h, dict))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    picked = []
    for hit in hits:
        try:
            row = by_id.get(int(str(hit.get("id")).strip()))
        except (TypeError, ValueError):
            row = None
        if row is not None and row["url"].startswith("http"):
            picked.append((hit, row))
    picked.sort(key=lambda hr: (_SOURCE_RANK.get(_kind(hr[1]), 3), -int(str(hr[1].get("date") or "0").replace("-", "")[:8] or 0)))
    for hit, row in picked:
        if row["domain"] in seen:
            continue
        seen.add(row["domain"])
        out.append({"company_name": row["company"], "domain": row["domain"], "event": str(hit.get("event") or row["title"])[:300],
                    "date": row["date"], "url": row["url"], "snippet": row["title"], "fit": "likely", "source": "roster"})
    out.sort(key=lambda c: c["date"], reverse=True)
    return out


def run_roster(icp: Mapping[str, Any], tools: Any, kind: str, *, llm_json, age_days, deadline: float, clock,
               http_client_factory=None, last: dict[str, Any], event_companies=(), max_companies: int = MAX_ROSTER,
               search_companies: bool = True) -> list[dict[str, Any]]:
    category = intent_category(icp, kind)
    notes: dict[str, Any] = {}
    listed = roster(icp, llm_json=llm_json, http_client_factory=http_client_factory, notes=notes)
    # Exa's company category returns small private firms with no stage signal: for a Public ICP each would only spend
    # a news lookup before resolution drops it as privately held.
    public = sm.normalize_stage(icp.get("company_stage")) == "public"
    try:
        searched = company_search(tools, icp) if COMPANY_SEARCH and search_companies and not public else []
    except BudgetExhausted:
        searched = []
    companies, seen = [], set()
    for company in listed + searched + [dict(c) for c in event_companies if c.get("domain")]:
        keys = {company["domain"], sm.company_name_key(company["company_name"])}
        excluded = [str(x).strip().lower() for x in icp.get("excluded_companies") or []]
        if keys & seen or company["domain"] in excluded or sm.company_name_key(company["company_name"]) in {
                sm.company_name_key(x) for x in excluded}:
            continue
        seen.update(keys)
        companies.append(company)
    companies = companies[:max(1, min(int(max_companies or MAX_ROSTER), MAX_ROSTER))]
    if category == "HIRING":
        from .hiring import run_hiring
        cands = run_hiring(icp, tools, companies, llm_json=llm_json, age_days=age_days, deadline=deadline, clock=clock,
                           http_client_factory=http_client_factory, last=last)
        last["roster"] = {"category": category, "companies": [c["company_name"] for c in companies],
                          "listed": len(listed), "searched": len(searched), "candidates": [c["company_name"] for c in cands]}
        return cands
    news = read_news(tools, companies, deadline=deadline, clock=clock)
    rows = story_rows(icp, companies, news, category, age_days=age_days)
    tried: list[str] = []
    try:
        extra = searched_rows(icp, tools, companies, news, rows, category, age_days=age_days, deadline=deadline, clock=clock,
                              tried=tried)
    except BudgetExhausted:
        extra = []
    seen = {r["url"] for r in rows}
    for row in extra:
        if row["url"] not in seen:
            seen.add(row["url"])
            rows.append(dict(row, id=len(rows)))
    cands = pick_events(icp, rows, llm_json=llm_json, http_client_factory=http_client_factory) if rows else []
    last["roster"] = {"category": category, "companies": [c["company_name"] for c in companies],
                      "listed": len(listed), "searched": len(searched), "skipped": notes.get("skipped", []),
                      "with_news": sum(1 for c in companies if news.get(c["domain"])), "story_rows": len(rows),
                      "event_search_tried": tried, "event_search_rows": len(extra),
                      "event_search_companies": sorted({r["company"] for r in extra}),
                      "candidates": [c["company_name"] for c in cands]}
    return cands


__all__ = ["run_roster", "roster", "roster_prompt", "story_rows", "short_feed", "searched_rows", "classify_prompts", "pick_events", "intent_category",
           "company_news", "CATEGORY_WORDS", "ROSTER_MODEL", "ROSTER_SIZE"]
