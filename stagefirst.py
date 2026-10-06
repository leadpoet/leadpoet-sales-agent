"""Loop 20260923T0503Z s15 "stage-first": companies found through their OWN funding-round announcement at the
ICP's stage, so the stage quote is a press-release headline and never a guess.

Why (official arena-2026-09-24 field + our d13 dress rehearsal of the same ICPs):
  * every company that qualified anywhere in the field carried a company_stage_evidence quote that was a funding
    press-release headline ("Levanta Raises $22M Series B.", "Rundoo Raises $30M Series B ..."); "stage:
    unavailable" was the field's most common failure (9 companies) and ours (Bombas via startupintros, Faith
    Technologies via a quiz site, RealPage via a "to be acquired" announcement);
  * our event-first lanes reach the stage step with companies of any stage: d13 ICP 001 dropped 12 of 32
    candidates as later-stage, ICP 003 seven, and drafted one wrong-stage company per ICP;
  * probe x11 (09-22 bank) showed funding queries return 1-7 stage-matched companies per venture ICP but its
    event side (the free per-company news lookup + one classifier call) found zero events.

Stage-first = funding queries for the ICP's exact round in its region and sub-industry (paid Exa, news category,
15 months back) -> the extraction call names the companies that announced their own round -> keep those whose
LATEST named round IS the ICP's stage -> the event: the funding article itself for FUNDING criteria, else the
free per-company news PLUS one recency-filtered Exa search per company for the ICP's event kind, one classifier
call -> scout's resolve -> fits -> stage (the round article is the stage hint) -> draft path.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping, Optional

from . import scorer_mirror as sm
from .arena_tools import STRATEGY, BudgetExhausted, fetchable

RECENCY_DAYS = int(STRATEGY.get("stage_first_recency_days") or 365)
MAX_QUERIES = int(STRATEGY.get("stage_first_queries") or 6)
MAX_COMPANIES = int(STRATEGY.get("stage_first_companies") or 8)
EVENT_WORKERS = 4
_STAGE_PHRASES = {"seed": ("seed round", "seed funding"), "series a": ("Series A",), "series b": ("Series B",),
                  "series c+": ("Series C", "Series D")}
_ORDER = ["pre seed", "seed"] + [f"series {c}" for c in "abcdefgh"]
_ACCEPT = {"seed": {"seed"}, "series a": {"series a"}, "series b": {"series b"},
           "series c+": {f"series {c}" for c in "cdefgh"}}
_PRE_SEED = r"pre(?:\s*[-\u2010\u2011\u2012\u2013\u2014\u2212]\s*|\s+)seed"
_ROUND_RE = re.compile(r"\b(" + _PRE_SEED + r"|seed|series\s+[a-h])\b", re.I)


def _label(raw: str) -> str:
    text = " ".join(str(raw or "").casefold().split())
    return "pre seed" if re.fullmatch(_PRE_SEED, text) else text


NEWS_HOSTS = ("prnewswire.com", "globenewswire.com", "businesswire.com", "accesswire.com", "newswire.ca", "einpresswire.com",
              "techcrunch.com", "betakit.com", "finsmes.com", "crunchbase.com", "pulse2.com", "axios.com", "startupdaily.net",
              "smartcompany.com.au", "technode.global", "e27.co", "dealstreetasia.com", "sifted.eu", "tech.eu", "uktn.co.uk",
              "eu-startups.com", "medium.com", "substack.com", "linkedin.com", "yahoo.com", "bloomberg.com", "reuters.com")


def _host_names(domain: str, key: str) -> bool:
    return bool(key) and key.replace(" ", "") in domain.replace("-", "").replace(".", "")
_EVENT_QUERIES = {
    "PRODUCT_LAUNCH": "{name} launches new product platform feature announcement",
    "ACQUISITION": "{name} acquires acquisition completed",
    "PARTNERSHIP": "{name} announces partnership partners with",
    "FACILITY_OPENING": "{name} opens new facility plant manufacturing site",
    "REGULATORY_CLEARANCE": "{name} FDA clearance certification approval",
    "MARKET_EXPANSION": "{name} expands into new market opens office",
    "LEADERSHIP_CHANGE": "{name} appoints chief officer",
}


def stage_key(icp: Mapping[str, Any]) -> str:
    return str(sm.normalize_stage(icp.get("company_stage")) or "")


def venture_stage(icp: Mapping[str, Any]) -> bool:
    """True for Seed / Series A / Series B / Series C+ (the stages a funding announcement proves)."""

    return stage_key(icp) in _STAGE_PHRASES


def latest_round(text: str) -> str:
    """The latest priced round the text names ('' when none): 'seed ... now a Series A' -> 'series a'."""

    labels = [_label(m.group(1)) for m in _ROUND_RE.finditer(str(text or ""))]
    labels = [label for label in labels if label in _ORDER]
    return max(labels, key=_ORDER.index) if labels else ""


def round_accepted(label: str, icp: Mapping[str, Any]) -> bool:
    return bool(label) and label in _ACCEPT.get(stage_key(icp), set())


def funding_queries(icp: Mapping[str, Any]) -> list[str]:
    """Up to MAX_QUERIES news queries for funding rounds of the ICP's own stage in its region and sub-industry;
    [] for Public / Private Equity / no stage."""

    from .scout import region_places

    phrases = _STAGE_PHRASES.get(stage_key(icp))
    if not phrases:
        return []
    sub = " ".join(str(icp.get("sub_industry") or icp.get("industry") or "").split(",")[0].split())[:80]
    pieces = [p.strip() for p in re.split(r"\s+and\s+|\s*/\s*", sub) if p.strip()] or [sub]
    country = " ".join(str(icp.get("country") or "").split())
    places = region_places(icp) or [country or " ".join(str(icp.get("geography") or "").split())]
    product = re.sub(r"^(?:an?|the)\s+", "", " ".join(str(icp.get("product_service") or "").split()), flags=re.I)
    product = " ".join(re.split(r"\s+(?:that|which|used|for|to|helps?)\b", product, maxsplit=1)[0].split()[:6])
    out: list[str] = []
    for phrase in phrases:
        for query in (f"{pieces[0]} startup raises {phrase} {places[0]}",
                      f"{product or pieces[-1]} startup raises {phrase} {places[-1]}",
                      f"{places[0]} {sub} company announces {phrase} funding",
                      f"{pieces[-1]} {phrase} round {places[0]}"):
            query = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", " ".join(query.split()), flags=re.I)[:200]
            if query.lower() not in {q.lower() for q in out}:
                out.append(query)
    return out[:MAX_QUERIES]


def funded_companies(cands: list[Mapping[str, Any]], icp: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Extracted funding-news subjects whose latest named round IS the ICP's stage, one row per company."""

    excluded = [str(x).strip().lower() for x in icp.get("excluded_companies") or [] if str(x).strip()]
    banned = {sm.company_name_key(x) for x in excluded} | set(excluded)
    kept: list[dict[str, Any]] = []
    dropped: dict[str, str] = {}
    seen: set[str] = set()
    for cand in cands:
        name = " ".join(str(cand.get("company_name") or "").split())[:120]
        domain = str(cand.get("domain") or "").strip().lower()
        key = sm.company_name_key(name)
        if not name or not key or key in seen or (domain and domain in seen):
            continue
        if key in banned or (domain and domain in banned):
            dropped[name] = "excluded company"
            continue
        label = latest_round(f"{cand.get('event') or ''} {cand.get('snippet') or ''}")
        if not label:
            dropped[name] = "no round named"
            continue
        if not round_accepted(label, icp):
            dropped[name] = f"latest round named is {label}"
            continue
        article_host = sm.registrable_host(str(cand.get("url") or ""))
        if domain and (any(domain == h or domain.endswith("." + h) for h in NEWS_HOSTS) or
                       (article_host and domain == article_host and not _host_names(domain, key))):
            domain = ""
        seen.update({key} | ({domain} if domain else set()))
        kept.append({"company_name": name, "domain": domain, "round": label, "round_url": str(cand.get("url") or ""),
                     "round_date": cand.get("date"), "round_snippet": str(cand.get("snippet") or "")[:600]})
    return kept, dropped


def stage_hint(company: Mapping[str, Any]) -> dict[str, Any]:
    return {"round": company.get("round") or "", "url": str(company.get("round_url") or "").split("#")[0],
            "date": company.get("round_date"), "snippet": company.get("round_snippet") or ""}


def round_news_row(company: Mapping[str, Any]) -> dict[str, Any]:
    """The funding article as a company-news row: the round press release often narrates the ICP's event too
    (09-24 field: Medici Brands' launch clause sat inside its Series B release), and news_stage quotes it for stage."""

    snippet = str(company.get("round_snippet") or "")
    return {"url": str(company.get("round_url") or "").split("#")[0], "title": snippet[:200],
            "published_date": company.get("round_date"), "excerpt": snippet[:400], "match": "primary"}


def seed_round_news(tools: Any, news: dict[str, list[dict[str, Any]]], companies: list[Mapping[str, Any]]) -> None:
    """Put each company's own funding article first in its news rows and in the shared cache (roster.company_news)."""

    cache = tools.__dict__.setdefault("_roster_news", {}) if hasattr(tools, "__dict__") else {}
    for company in companies:
        row = round_news_row(company)
        if not row["url"].startswith("http"):
            continue
        rows = [r for r in news.get(company["domain"]) or [] if r.get("url") != row["url"]]
        news[company["domain"]] = [row] + rows
        cache[company["domain"]] = news[company["domain"]]


def later_round_in_news(company: Mapping[str, Any], rows: list[Mapping[str, Any]], icp: Mapping[str, Any]) -> str:
    """The later round a company-news HEADLINE names ('' when none): 'Senra Systems Raises $65M in Series B' for a
    company kept as Series A (x11 critique).  Headlines only -- excerpts mention other companies' rounds."""

    from .scout import name_hit

    for row in rows:
        title = str(row.get("title") or "")
        match = _ROUND_RE.search(title)
        if match is None or not name_hit(str(company["company_name"]), title[:match.start()]):
            continue
        label = latest_round(title)
        if label and label != company.get("round") and not round_accepted(label, icp) and \
                _ORDER.index(label) > _ORDER.index(str(company.get("round") or "pre seed")):
            return label
    return ""


def event_query(icp: Mapping[str, Any], category: str, name: str) -> str:
    template = _EVENT_QUERIES.get(category) or "{name} announces"
    return " ".join(template.format(name=f'"{name}"').split())[:200]


def company_events(tools: Any, companies: list[Mapping[str, Any]], icp: Mapping[str, Any], category: str, *,
                   age_days: Callable[[Any], Optional[int]], deadline: float, clock: Callable[[], float],
                   over_budget: Callable[[], bool], limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Story rows (roster.story_rows shape) from one recency-filtered Exa search per company; the page text
    lands in tools.pages, so confirm_on_page later checks the sentence without another fetch."""

    from .roster import CATEGORY_WORDS

    window = int(icp.get("intent_max_age_days") or 365)
    pattern = re.compile(CATEGORY_WORDS[category], re.I) if category in CATEGORY_WORDS else None
    todo = list(companies)[:MAX_COMPANIES if limit is None else max(0, int(limit))]

    def one(company: Mapping[str, Any]) -> list[dict[str, Any]]:
        if clock() >= deadline or over_budget():
            return []
        query = event_query(icp, category, str(company["company_name"]))
        try:
            data = tools.search_web(query, recency_days=window, limit=4, company_website=f"https://{company['domain']}/")
        except BudgetExhausted:
            return []
        except Exception:
            return []
        rows: list[dict[str, Any]] = []
        for row in (data or {}).get("results") or []:
            date = str(row.get("published_date") or "")[:10]
            age = age_days(date)
            title, excerpt = str(row.get("title") or ""), str(row.get("excerpt") or "")
            if age is None or not 0 <= age <= window or not str(row.get("url") or "").startswith("http"):
                continue
            if not fetchable(row.get("url")):
                continue
            if pattern is not None and not pattern.search(f"{title} {excerpt}"):
                continue
            rows.append({"company": company["company_name"], "domain": company["domain"], "date": date,
                         "title": title[:200], "excerpt": excerpt[:300], "url": str(row.get("url") or "")})
        return rows

    with ThreadPoolExecutor(max_workers=EVENT_WORKERS) as pool:
        found = list(pool.map(one, todo))
    return [row for rows in found for row in rows]


def run_stage_first(icp: Mapping[str, Any], tools: Any, kind: str, *, llm_json, age_days, deadline: float, clock,
                    http_client_factory=None, last: dict[str, Any], started_spend: float = 0.0,
                    over_budget: Optional[Callable[[], bool]] = None) -> list[dict[str, Any]]:
    """Candidates in the roster lane's shape (company_name, domain, event, date, url, snippet, source) plus a
    stage_hint {round, url, date, snippet}; [] for Public / Private Equity ICPs."""

    from . import roster, scout

    feed: dict[str, Any] = {}
    last["stage_first"] = feed
    queries = funding_queries(icp)
    feed["queries"] = queries
    if not queries:
        return []
    over = over_budget or (lambda: False)
    category = roster.intent_category(icp, kind)
    window = int(icp.get("intent_max_age_days") or 365)
    recency = min(RECENCY_DAYS, window) if category == "FUNDING" else RECENCY_DAYS
    rows = scout.harvest(tools, queries, recency_days=recency, kind="funding", deadline=deadline, started_spend=started_spend)
    funding_icp = dict(icp, intent_signals=[f"Announced its own {icp.get('company_stage')} funding round"],
                       intent_max_age_days=recency)
    cands = scout.extract(tools, rows, funding_icp, http_client_factory=http_client_factory)
    kept, dropped = funded_companies(cands, icp)
    kept.sort(key=lambda k: str(k.get("round_date") or ""), reverse=True)
    feed.update(rows=len(rows), extracted=[c.get("company_name") for c in cands], kept=[dict(k) for k in kept],
                dropped=dropped, category=category)
    by_key = {sm.company_name_key(k["company_name"]): k for k in kept}
    if category == "FUNDING":
        events = []
        for cand in cands:
            company = by_key.get(sm.company_name_key(cand.get("company_name")))
            if company is None:
                continue
            age = age_days(cand.get("date")) if cand.get("date") else None
            if cand.get("date") and (age is None or not 0 <= age <= window):
                dropped[company["company_name"]] = f"round {cand.get('date')} outside the {window}-day window"
                continue
            events.append(dict(cand, stage_hint=stage_hint(company), lane="stage_first"))
    else:
        for company in kept:
            if not company.get("domain") and clock() < deadline:
                try:
                    company["domain"] = scout.find_domain(tools, company["company_name"])
                except BudgetExhausted:
                    break
                except Exception:
                    company["domain"] = ""
        for company in kept:
            if not company.get("domain"):
                dropped[company["company_name"]] = "no domain"
        kept = [c for c in kept if c.get("domain")]
        by_key = {sm.company_name_key(k["company_name"]): k for k in kept}
        companies = [{"company_name": c["company_name"], "domain": c["domain"]} for c in kept]
        if category == "HIRING":
            from .hiring import run_hiring
            events = run_hiring(icp, tools, companies, llm_json=llm_json, age_days=age_days, deadline=deadline, clock=clock,
                                http_client_factory=http_client_factory, last=last)
        elif companies:
            news = roster.read_news(tools, companies, deadline=deadline, clock=clock)
            superseded = {c["company_name"]: later_round_in_news(c, news.get(c["domain"]) or [], icp) for c in kept}
            for name, label in superseded.items():
                if label:
                    dropped[name] = f"news headline names a later round: {label}"
            kept = [c for c in kept if not superseded.get(c["company_name"])]
            companies = [c for c in companies if not superseded.get(c["company_name"])]
            by_key = {sm.company_name_key(k["company_name"]): k for k in kept}
            seed_round_news(tools, news, kept)
            story = roster.story_rows(icp, companies, news, category, age_days=age_days)
            searched = company_events(tools, companies, icp, category, age_days=age_days, deadline=deadline, clock=clock,
                                      over_budget=over)
            seen = {r["url"] for r in story}
            for row in searched:
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                story.append(dict(row, id=len(story)))
            for i, row in enumerate(story):
                row["id"] = i
            feed.update(with_news=sum(1 for c in companies if news.get(c["domain"])), story_rows=len(story),
                        searched_rows=len(searched))
            events = roster.pick_events(icp, story, llm_json=llm_json, http_client_factory=http_client_factory) if story else []
        else:
            events = []
        for cand in events:
            company = by_key.get(sm.company_name_key(cand.get("company_name")))
            if company is not None:
                cand["stage_hint"] = stage_hint(company)
            cand["lane"] = "stage_first"
    feed["events"] = [c.get("company_name") for c in events]
    return events


__all__ = ["run_stage_first", "funding_queries", "funded_companies", "latest_round", "round_accepted", "venture_stage",
           "company_events", "event_query", "stage_hint", "stage_key", "round_news_row", "seed_round_news",
           "later_round_in_news", "RECENCY_DAYS", "MAX_COMPANIES"]
