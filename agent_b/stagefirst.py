
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable, Mapping, Optional

from . import scorer_mirror as sm
from .arena_tools import STRATEGY, BudgetExhausted, fetchable, paced

RECENCY_DAYS = int(STRATEGY.get("stage_first_recency_days") or 365)
MAX_QUERIES = int(STRATEGY.get("stage_first_queries") or 6)
BROAD_STAGE_QUERIES = bool(STRATEGY.get("stage_first_broad", 0))
STAGE_RESULTS = max(1, min(25, int(STRATEGY.get("stage_first_results") or 6)))
WIRE_SWEEP = bool(STRATEGY.get("stage_first_wire", 0))
STAGE_CANDIDATES = max(0, min(40, int(STRATEGY.get("stage_first_candidates") or 0)))
WIDE_CATEGORIES = frozenset(c.strip().upper() for c in str(STRATEGY.get("stage_first_wide_categories") or "").split(",")
                            if c.strip())
EVENT_RESULTS = max(1, min(10, int(STRATEGY.get("stage_event_results") or 4)))
WIRE_DOMAINS = ["prnewswire.com", "globenewswire.com", "businesswire.com", "finsmes.com", "accessnewswire.com"]
MAX_COMPANIES = int(STRATEGY.get("stage_first_companies") or 8)
_RECENCY_KEYS = {"seed": ("stage_first_recency_seed", 365), "series a": ("stage_first_recency_a", 540),
                 "series b": ("stage_first_recency_b", 730), "series c+": ("stage_first_recency_c", 1460)}
_LATE_STAGES = ("series b", "series c+")
LATE_QUERIES = paced("stage_first_queries_late", 9, 0, 12)
LATE_COMPANIES = paced("stage_first_companies_late", 12, 0, 16)
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

    return stage_key(icp) in _STAGE_PHRASES


def latest_round(text: str) -> str:

    labels = [_label(m.group(1)) for m in _ROUND_RE.finditer(str(text or ""))]
    labels = [label for label in labels if label in _ORDER]
    return max(labels, key=_ORDER.index) if labels else ""


def round_accepted(label: str, icp: Mapping[str, Any]) -> bool:
    return bool(label) and label in _ACCEPT.get(stage_key(icp), set())


def recency_days(icp: Mapping[str, Any]) -> int:

    key, default = _RECENCY_KEYS.get(stage_key(icp), ("", 0))
    return (paced(key, default, 0, 3650) if key else 0) or RECENCY_DAYS


def lane_caps(icp: Mapping[str, Any]) -> tuple[int, int]:

    if stage_key(icp) in _LATE_STAGES:
        return max(MAX_QUERIES, LATE_QUERIES), max(MAX_COMPANIES, LATE_COMPANIES)
    return MAX_QUERIES, MAX_COMPANIES


def funding_queries(icp: Mapping[str, Any], late: bool = True) -> list[str]:

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
    broad = broad_stage_queries(icp, phrases, places) if BROAD_STAGE_QUERIES else []
    if broad:
        out = out[:1] + [q for q in broad if q.lower() not in {x.lower() for x in out}] + out[1:]
    cap = (lane_caps(icp)[0] if late else MAX_QUERIES) + len(broad)
    if cap <= MAX_QUERIES + len(broad):
        return out[:MAX_QUERIES + len(broad)]
    base, spare = out[:MAX_QUERIES], out[MAX_QUERIES:]
    try:
        today = sm.evaluation_date()
        oldest = (today - timedelta(days=recency_days(icp))).year
        years = range(today.year - 1, max(oldest, today.year - 6) - 1, -1)
        for template in ("{piece} company raises {phrase} {year} {place}", "{place} {piece} {phrase} funding announced {year}"):
            for year in years:
                for phrase in phrases:
                    query = template.format(piece=pieces[0], phrase=phrase, year=year, place=places[0])
                    query = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", " ".join(query.split()), flags=re.I)[:200]
                    if query.lower() not in {q.lower() for q in base}:
                        base.append(query)
    except Exception:
        pass
    return (base + spare)[:cap]


def broad_stage_queries(icp: Mapping[str, Any], phrases: tuple, places: list) -> list[str]:

    industry = " ".join(str(icp.get("industry") or "").split())[:60]
    if not industry or not phrases:
        return []
    place = places[0] if places else ""
    out = []
    for query in (f"{place} {industry} startup raises {phrases[0]}", f"{industry} company raises {phrases[0]} {place}"):
        query = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", " ".join(query.split()), flags=re.I)[:200]
        if query and query.lower() not in {q.lower() for q in out}:
            out.append(query)
    return out


def wire_queries(icp: Mapping[str, Any]) -> list[str]:

    phrases = _STAGE_PHRASES.get(stage_key(icp)) or ()
    if not phrases:
        return []
    from .scout import region_places

    industry = " ".join(str(icp.get("industry") or "").split())[:60]
    head = re.split(r"[/,(]", " ".join(str(icp.get("sub_industry") or industry).split()))[0].strip()[:60]
    place = (region_places(icp) or [str(icp.get("country") or "")])[0]
    out = []
    for query in (f"{industry} startup announces {phrases[0]} funding {place}", f"{head} raises {phrases[0]}"):
        query = " ".join(query.split())[:200]
        if industry and query.lower() not in {q.lower() for q in out}:
            out.append(query)
    return out


def funded_companies(cands: list[Mapping[str, Any]], icp: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:

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

    snippet = str(company.get("round_snippet") or "")
    return {"url": str(company.get("round_url") or "").split("#")[0], "title": snippet[:200],
            "published_date": company.get("round_date"), "excerpt": snippet[:400], "match": "primary"}


def seed_round_news(tools: Any, news: dict[str, list[dict[str, Any]]], companies: list[Mapping[str, Any]]) -> None:

    cache = tools.__dict__.setdefault("_roster_news", {}) if hasattr(tools, "__dict__") else {}
    for company in companies:
        row = round_news_row(company)
        if not row["url"].startswith("http"):
            continue
        rows = [r for r in news.get(company["domain"]) or [] if r.get("url") != row["url"]]
        news[company["domain"]] = [row] + rows
        cache[company["domain"]] = news[company["domain"]]


def later_round_in_news(company: Mapping[str, Any], rows: list[Mapping[str, Any]], icp: Mapping[str, Any]) -> str:

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

    from .roster import CATEGORY_WORDS

    window = int(icp.get("intent_max_age_days") or 365)
    pattern = re.compile(CATEGORY_WORDS[category], re.I) if category in CATEGORY_WORDS else None
    todo = list(companies)[:MAX_COMPANIES if limit is None else max(0, int(limit))]

    def one(company: Mapping[str, Any]) -> list[dict[str, Any]]:
        if clock() >= deadline or over_budget():
            return []
        query = event_query(icp, category, str(company["company_name"]))
        try:
            data = tools.search_web(query, recency_days=window, limit=EVENT_RESULTS, company_website=f"https://{company['domain']}/")
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
    if category == "FUNDING" and len(queries) > MAX_QUERIES:
        queries = funding_queries(icp, late=False)
        feed["queries"] = queries
    try:
        stage_days, company_cap = recency_days(icp), lane_caps(icp)[1]
    except Exception:
        stage_days, company_cap = RECENCY_DAYS, MAX_COMPANIES
    recency = min(stage_days, window) if category == "FUNDING" else stage_days
    wide = not WIDE_CATEGORIES or category in WIDE_CATEGORIES
    feed["wide"] = wide
    rows = scout.harvest(tools, queries, recency_days=recency, kind="funding", deadline=deadline, started_spend=started_spend,
                         limit=STAGE_RESULTS if wide else 0)
    wires = wire_queries(icp) if WIRE_SWEEP and wide else []
    if wires:
        seen = {r["url"] for r in rows}
        extra = scout.harvest(tools, wires, recency_days=recency, kind="funding", deadline=deadline,
                              started_spend=started_spend, limit=25, include_domains=WIRE_DOMAINS)
        rows += [r for r in extra if r["url"] not in seen]
        feed["wire_queries"], feed["wire_rows"] = wires, len(extra)
    for i, row in enumerate(rows, 1):
        row["id"] = i
    funding_icp = dict(icp, intent_signals=[f"Announced its own {icp.get('company_stage')} funding round"],
                       intent_max_age_days=recency)
    cands = scout.extract(tools, rows, funding_icp, http_client_factory=http_client_factory,
                          max_candidates=STAGE_CANDIDATES if wide else 0)
    kept, dropped = funded_companies(cands, icp)
    kept.sort(key=lambda k: str(k.get("round_date") or ""), reverse=True)
    if company_cap > MAX_COMPANIES and category != "FUNDING":
        for company in kept[company_cap + 4:]:
            dropped[company["company_name"]] = "beyond the lane's company cap (older round)"
        kept = kept[:company_cap + 4]
    feed.update(rows=len(rows), extracted=[c.get("company_name") for c in cands], kept=[dict(k) for k in kept],
                dropped=dropped, category=category, recency_days=recency, company_cap=company_cap)
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
                                      over_budget=over, limit=company_cap)
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
           "later_round_in_news", "RECENCY_DAYS", "MAX_COMPANIES", "recency_days", "lane_caps"]
