"""Loop 20260923T0503Z s8 "roster": company-first discovery beside scout's event-first search.

Why (d7: s7 on the 09-23 ICPs under the company-only judge, upstream 52562079):
  * event-first search returned companies on 4 of 10 ICPs; the others found no in-bucket subject of an event;
  * the platform's contextdev_post_news_search is a per-COMPANY lookup (x3): one domain's dated primary stories
    (title, excerpt, url, published_at) at $0 -- e.g. Moov's Business Wire launch dated the day before the round;
  * every ICP names one excluded company, the exemplar the ICP was written from (atturra.com for the Sydney
    consulting ICP, finexio.com for the payments one), so its peers are the pool to search.

Roster asks one strong model for up to ROSTER_SIZE real companies inside the ICP's hard buckets (the exemplar's
peers first), reads each company's dated news for free, keeps the in-window primary stories whose headline or
excerpt carries the ICP's intent category, lets one cheap model call confirm which story reports the company's
own qualifying event, and hands those companies to scout's resolve -> fits -> stage -> draft path.
"""

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
# Companies the free lookup said nothing about are searched too, after the saturated ones and only out of
# spare call budget.  See ``searched_rows``.
EVENT_THIN = bool(STRATEGY.get("roster_event_thin", 0))
EVENT_THIN_RESERVE = int(STRATEGY.get("roster_event_thin_reserve") or 60)
# What the rest of the ICP still needs after research: verification fetches and the contact-free checks.
EVENT_THIN_SPEND_RESERVE_USD = float(STRATEGY.get("roster_event_thin_reserve_usd") or 0.12)
CLASSIFY_BATCH_CHARS = 16_000

OWN_NEWS = bool(STRATEGY.get("own_news", 1))
FOUND_NEWS = bool(STRATEGY.get("found_news", 0))


def _ownnews_categories() -> tuple:
    """The intent categories whose announcements live on the company's own site (ownnews.CATEGORIES)."""

    try:
        from . import ownnews

        return tuple(ownnews.CATEGORIES)
    except Exception:  # noqa: BLE001 - without the module the lane simply does not run
        return ()

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
        "owned by a private-equity firm; Public = listed on a stock exchange). %sPrefer companies likely to have shown "
        "the intent signal within intent_max_age_days before %s. Every company is verified afterwards, so include "
        "likely fits rather than stopping early, but never invent a company. Give each company's primary website "
        "domain exactly (e.g. acme.com). Return {\"companies\": [{\"name\": \"...\", \"domain\": \"...\"}]}."
        "\n\nICP: %s" % (ROSTER_SIZE, seed, sm.evaluation_date().isoformat(), json.dumps(brief, default=str)[:3000]))


def roster(icp: Mapping[str, Any], *, llm_json, http_client_factory=None) -> list[dict[str, Any]]:
    parsed = llm_json(roster_prompt(icp), http_client_factory=http_client_factory, max_tokens=2500, model=ROSTER_MODEL)
    raw = parsed.get("companies") if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
    excluded = [str(x).strip().lower() for x in icp.get("excluded_companies") or [] if str(x).strip()]
    banned = {sm.company_name_key(x) for x in excluded} | {_domain(x) for x in excluded if "." in x}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
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
    """Two Exa company-category queries (x4: they return in-region company homepages, recent startups included)."""

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
    """True when the free lookup is saturated (>= SATURATED_ROWS dated stories) and its oldest story is younger than
    half the ICP's window: the company's own events inside the window are mostly out of its reach."""

    ages = [a for a in (age_days(s.get("published_date")) for s in stories or []) if a is not None]
    return len(ages) >= SATURATED_ROWS and max(ages) < max(30, int(window or 365) // 2)


def unexamined_feed(stories: list[Mapping[str, Any]], *, age_days) -> bool:
    """True when the free lookup returned almost nothing dated about this company.

    ``short_feed`` is about a feed that is *too full* to see past; this is its opposite and the case that was
    falling through.  A company whose free lookup came back with fewer than SATURATED_ROWS dated stories is not
    covered by it -- it is simply unexamined, and an empty feed makes ``short_feed`` false for the same reason an
    overflowing one makes it true.  On the Education ICP of 2026-10-15 the roster listed 28 companies, 14 came
    back with no news at all, and the paid event search was offered to three.
    """

    ages = [a for a in (age_days(s.get("published_date")) for s in stories or []) if a is not None]
    return len(ages) < SATURATED_ROWS


def _thin_affordable(tools: Any) -> int:
    """How many unexamined companies the call budget can still afford one search each.

    ``tools.remaining()`` is the ceiling the gateway counts; everything past EVENT_THIN_RESERVE is spare. A tools
    object that cannot say returns 0, so an unknown budget never buys the tail."""

    try:
        calls = max(0, int(tools.remaining()) - EVENT_THIN_RESERVE)
    except Exception:  # noqa: BLE001 - an unknown budget buys nothing
        return 0
    if not calls:
        return 0
    try:
        cap = getattr(tools, "spend_cap_usd", None)
        if cap is None:
            return calls
        price = max(1e-6, float(tools.tool_price_usd("exa_search")))
        spare = float(cap) - float(tools.spend_usd()) - EVENT_THIN_SPEND_RESERVE_USD
        return max(0, min(calls, int(spare / price)))
    except Exception:  # noqa: BLE001 - an unreadable ledger buys nothing
        return 0


def searched_rows(icp: Mapping[str, Any], tools: Any, companies: list[dict[str, Any]], news: Mapping[str, list[dict[str, Any]]],
                  rows: list[dict[str, Any]], category: str, *, age_days, deadline: float, clock,
                  tried: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Story rows from one Exa event search per company whose own events the free lookup cannot show us.

    Loop s27 (d25): a feed row that only matched the category words ('CrowdStrike Named a Leader in ...') no longer
    exempts a company -- CrowdStrike, Fortinet and N-able were never searched although their releases qualified.

    Two populations qualify, in this order.  A ``short_feed`` company's feed is too full to see past, which is the
    case this loop was built for and which keeps its priority.  An ``unexamined_feed`` company's feed told us
    almost nothing, and until now it was never searched at all: the predicate needed eight dated stories to fire,
    so a company with none failed it exactly as a well-covered one did.  That is where the roster was leaking.  On
    the Education ICP of 2026-10-15, 28 companies listed, 14 with no news, 3 searched; the run finished having
    spent 516 of its 900 seconds and 151 of its 200 calls, and returned 2 companies for 5 slots.

    The tail is bought out of spare budget only, so a tight round behaves exactly as it did before: the saturated
    companies are offered first, and the unexamined ones fill what is left of EVENT_COMPANIES while at least
    EVENT_THIN_RESERVE calls remain for verification, which is where the rest of the ICP's calls go."""

    if not EVENT_SEARCH or category not in CATEGORY_WORDS or category == "HIRING":
        return []
    window = int(icp.get("intent_max_age_days") or 365)
    todo = [c for c in companies if short_feed(news.get(c["domain"]) or [], window, age_days=age_days)][:max(0, EVENT_COMPANIES)]
    if EVENT_THIN and len(todo) < max(0, EVENT_COMPANIES):
        chosen = {id(c) for c in todo}
        thin = [c for c in companies
                if id(c) not in chosen and unexamined_feed(news.get(c["domain"]) or [], age_days=age_days)]
        todo += thin[:min(max(0, EVENT_COMPANIES) - len(todo), _thin_affordable(tools))]
    if tried is not None:
        tried.extend(c["company_name"] for c in todo)
    if not todo:
        return []
    from .stagefirst import company_events
    return company_events(tools, todo, icp, category, age_days=age_days, deadline=deadline, clock=clock,
                          over_budget=lambda: False, limit=EVENT_COMPANIES)


def classify_prompts(icp: Mapping[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    listing = [dict({k: r[k] for k in ("id", "company", "date", "title", "excerpt")},
                    **({"company_domain": r["domain"]} if r.get("domain") else {})) for r in rows]
    batches: list[list[dict[str, Any]]] = [[]]
    for item in listing:
        if batches[-1] and len(json.dumps(batches[-1] + [item])) > CLASSIFY_BATCH_CHARS:
            batches.append([])
        batches[-1].append(item)
    signal = " ".join(str(s) for s in icp.get("intent_signals") or [icp.get("intent_signal") or ""])
    # 2026-10-18 ICP 6: our PixVerse row led with PixLab, "an invite-only creator partnership program", and the judge
    # failed its primary intent; the four submissions that qualified PixVerse led with its partnership with the UN AI
    # for Good summit. Published PARTNERSHIP rows agree: partner-portal, award and program evidence failed intent.
    partner_rule = (
        "For a partnership, the story must name the outside organization THIS company partnered with; a partner, "
        "creator, affiliate, reseller or referral program, a partner portal, a certification, an award or a "
        "marketplace listing is not a partnership. " if re.search(r"\bpartner", signal, re.I) else "")
    business = " / ".join(str(icp.get(k) or "").strip() for k in ("industry", "sub_industry") if str(icp.get(k) or "").strip())
    # 2026-10-18 replay: "Princeton Properties partnered with Whale" (linkedin.com/posts/gowhale, a New England
    # property-payments company) became the partnership of Whale (whale.sg, enterprise AI). The rows now carry the
    # company's domain, and the prompt says what business the company is in.
    namesake = ("Every company here is a candidate %s company; a story about a different business that only shares "
                "the name (another domain, another industry or market) is not about THIS company. " % business[:120]
                if business else "")
    return [(
        "Each row is a dated news story about the named company. Keep only the rows that report that THIS company "
        "itself did what the intent signal describes -- the event itself (e.g. its own funding round, its own "
        "executive appointment, its own launch, its own new office), not commentary, research findings, awards, "
        "customer stories, an investor's or partner's news, or a mere mention. An existing product made available "
        "in a new country or market is NOT a new product or capability; an acquisition counts only when the story "
        "says it closed or completed or the target now operates as part of the company, not an agreement or plan; "
        "a story where THIS company is acquired, bought, merged into another or taken private never qualifies (for an "
        "acquisition intent THIS company must be the acquirer); "
        "a job posting is not a hire. %s%sReturn EVERY row that reports such an event (several rows may cover the same "
        "company). Return {\"hits\": [{\"id\": <row id>, \"event\": \"<max 15 words>\"}]}.\n\n"
        "INTENT SIGNAL: %s\n\nROWS: %s" % (partner_rule, namesake, signal[:600], json.dumps(batch)))
        for batch in batches if batch]


_SOURCE_RANK = {"first_party": 0, "wire": 1, "news": 2, "linkedin": 4}

# Words that, glued to a one-word company name, make it another company's name: "Whale TV". Kept to words that are
# seldom the dropped half of the company's own legal name ("Scribe Therapeutics" and "Serefin Health" are the companies).
_BRAND_TAILS = {"tv", "media", "games", "gaming", "studio", "studios", "music", "mobile", "network", "networks", "pay",
                "bank", "motors", "foods", "realty", "homes", "insurance", "ventures"}
_LEAD_WORDS = {"the", "a", "an", "and", "with", "by", "for", "from", "at", "as", "of", "on", "in", "to", "how", "why",
               "new", "meet", "about", "inside", "exclusive", "introducing", "announcing", "welcome", "report", "update",
               "join", "joins"}


_DOT_TLDS = "io|ai|com|co|net|org|xyz|app|so|dev|gg|fi|finance|tech|sg|us|uk|de|fr|in|me|tv|cc|bet|casino|exchange"


_OWN_SLUG_LEADS = {"weare", "we", "hello", "hi", "team", "the", "its", "our", "official", "meet"}


def _host_label(host: str) -> str:
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def other_entity(row: Mapping[str, Any]) -> bool:
    """True when a story row is about a different company that only shares this company's one-word name.

    2026-10-18 ICP 6: the news search for Whale (whale.sg) returned "Teads Expands Global CTV Offering with Whale TV",
    a triplewhale.com post and "Triple Whale Launches Moby Automations", and eight submissions -- ours among them --
    led Whale's intent with them; all eight failed intent_details. Only a clear sign counts, so an unclear row still
    reaches the classifier: the page is on another site whose name embeds this one after a prefix (triplewhale.com;
    natwestgroup.com and marshmclennan.com start with the name and are the company's own group), or every mention
    in the title and excerpt is glued to another word -- a brand tail ("Whale TV") or a capitalised word opening the
    headline ("Triple Whale ..."). Multi-word names and the company's own pages are never touched. Checked on the
    published rows of 2026-10-01..18: 0 of 1,002 qualified evidence rows flagged."""

    words = re.findall(r"[A-Za-z0-9]+", str(row.get("company") or ""))
    domain = str(row.get("domain") or "").lower()
    if len(words) != 1 or len(words[0]) < 3 or not domain:
        return False
    word = words[0].lower()
    own = _host_label(domain)
    host = sm.registrable_host(str(row.get("url") or "")).lower()
    if host and (host == domain or host.endswith("." + domain)):
        return False
    if host:
        label = _host_label(host)
        if word in label and not label.startswith(word) and label != own:
            return True
        # The same test on a LinkedIn page's own slug: linkedin.com/posts/gowhale_... is a New England property-
        # payments company's post, not Whale's (whale.sg) -- the classifier took it as Whale's partnership.
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            path = str(row.get("url") or "").split("linkedin.com", 1)[-1].lower()
            found = re.match(r"/(?:posts|company)/([a-z0-9-]+?)(?:[_/?#]|$)", path)
            slug = found.group(1).replace("-", "") if found else ""
            lead = slug[:slug.find(word)] if word in slug else ""
            # "wearerunwise" is Runwise's own page; a self-introducing prefix keeps the slug the company's.
            if slug and lead and slug != own and lead not in _OWN_SLUG_LEADS:
                return True
    mentions = 0
    for text in (str(row.get("title") or ""), str(row.get("excerpt") or "")):
        for m in re.finditer(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(words[0]), text, re.I):
            mentions += 1
            after = re.match(r"\s+([A-Za-z]+)", text[m.end():])
            opener = re.fullmatch(r"\s*([A-Za-z]{3,})\s+", text[:m.start()])
            tail = after.group(1).lower() if after else ""
            lead = opener.group(1) if opener else ""
            # "Whale.io" on a whale.sg company is another company's domain written as its name (10-18 smoke run: a
            # crypto casino's token launch became Whale's second signal). One such mention settles it.
            dotted = re.match(r"\.(%s)\b" % _DOT_TLDS, text[m.end():], re.I)
            if dotted and "%s.%s" % (word, dotted.group(1).lower()) != domain:
                return True
            glued_tail = tail in _BRAND_TAILS and word + tail not in own
            glued_lead = lead[:1].isupper() and lead.lower() not in _LEAD_WORDS and lead.lower() + word not in own
            if not (glued_tail or glued_lead):
                return False
    return mentions > 0
    text = "%s %s" % (row.get("title") or "", row.get("excerpt") or "")
    hits = list(re.finditer(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(words[0]), text, re.I))
    if not hits:
        return False
    for m in hits:
        after = re.match(r"\s+([A-Za-z]+)", text[m.end():])
        before = re.search(r"(?:^|\s)([A-Za-z][A-Za-z]*)\s+$", text[:m.start()])
        tail = after.group(1).lower() if after else ""
        lead = before.group(1) if before else ""
        glued_tail = tail in _BRAND_TAILS and word + tail not in own
        glued_lead = lead[:1].isupper() and lead.lower() not in _LEAD_WORDS and lead.lower() + word not in own
        if not (glued_tail or glued_lead):
            return False
    return True
    text = "%s %s" % (row.get("title") or "", row.get("excerpt") or "")
    hits = list(re.finditer(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(words[0]), text, re.I))
    if not hits:
        return False
    own = domain.split(".")[0]
    for m in hits:
        before = re.findall(r"[A-Za-z0-9&'.-]+", text[max(0, m.start() - 40):m.start()])[-1:]
        after = re.findall(r"[A-Za-z0-9&'-]+", text[m.end():m.end() + 30])[:1]
        tail = after[0].lower() if after else ""
        lead = before[0] if before else ""
        glued_tail = tail in _BRAND_TAILS and (word + tail) not in own
        glued_lead = (lead[:1].isupper() and lead.lower().strip(".'") not in _LEAD_WORDS
                      and not lead.endswith(".") and (lead.lower() + word) not in own
                      and text[max(0, m.start() - 2):m.start()].strip() == "" and m.start() > 0
                      and not re.search(r"[.!?:|\-]\s*$", text[:m.start() - len(lead) - 1] if m.start() > len(lead) else ""))
        if not (glued_tail or glued_lead):
            return False
    return True


def _kind(row: Mapping[str, Any]) -> str:
    try:
        from .sourcetype import source_kind
        return source_kind(row.get("url"), row.get("domain"), row.get("company"))
    except Exception:
        return "unknown"


def pick_events(icp: Mapping[str, Any], rows: list[dict[str, Any]], *, llm_json, http_client_factory=None) -> list[dict[str, Any]]:
    rows = [r for r in rows if not other_entity(r)]
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
                    "date": row["date"], "url": row["url"], "snippet": row["title"], "fit": "likely", "source": "roster",
                    # The evidence is the company's own page. In a PRODUCT_LAUNCH ICP a first-party primary URL
                    # qualifies 18.5% of the time against 9.3% for a third-party one, so these go first.
                    "own_site": bool(row.get("own_site"))})
    out.sort(key=lambda c: c["date"], reverse=True)
    return out


def run_roster(icp: Mapping[str, Any], tools: Any, kind: str, *, llm_json, age_days, deadline: float, clock,
               http_client_factory=None, last: dict[str, Any], event_companies=(), max_companies: int = MAX_ROSTER,
               search_companies: bool = True) -> list[dict[str, Any]]:
    category = intent_category(icp, kind)
    listed = roster(icp, llm_json=llm_json, http_client_factory=http_client_factory)
    try:
        searched = company_search(tools, icp) if COMPANY_SEARCH and search_companies else []
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
    # A product launch mostly lives on the company's own site, not in a news index: of the fifteen companies
    # that qualified on the paying ICP of 2026-10-14, eight cited their own changelog, blog or release notes.
    # Those pages are free over the bridge, so they are read here beside the news lookup.
    if OWN_NEWS and category in _ownnews_categories():
        try:
            from . import ownnews
            from .scout import find_date

            words = re.compile(CATEGORY_WORDS[category], re.I) if category in CATEGORY_WORDS else None
            own = ownnews.own_rows(icp, tools, companies, find_date=find_date, age_days=age_days,
                                   deadline=deadline, clock=clock, last=last, words=words)
        except Exception as exc:  # noqa: BLE001 - a free lane that fails adds nothing and breaks nothing
            own, last["own_news_error"] = [], f"{type(exc).__name__}: {str(exc)[:120]}"
        seen_own = {r["url"] for r in rows}
        for row in own:
            if row["url"] not in seen_own:
                seen_own.add(row["url"])
                rows.append(dict(row, id=len(rows), own_site=True))
    # The companies both free lookups were silent about, read for free before anything is paid for them. An
    # ICP's binding budget is money, not time: on 2026-10-15 the run finished with 384 of 900 seconds unused and
    # $0.52 of a $0.64 research allowance gone, while 14 of 28 roster companies had never been looked at.
    if FOUND_NEWS and category in CATEGORY_WORDS:
        try:
            from . import ownnews
            from .scout import find_date
            from .stagefirst import event_query

            words = re.compile(CATEGORY_WORDS[category], re.I) if category in CATEGORY_WORDS else None
            silent = [c for c in companies if unexamined_feed(news.get(c["domain"]) or [], age_days=age_days)]
            got = ownnews.found_rows(icp, tools, silent, find_date=find_date, age_days=age_days, deadline=deadline,
                                     clock=clock, make_query=lambda name: event_query(icp, category, name),
                                     last=last, words=words)
        except Exception as exc:  # noqa: BLE001 - a free lane that fails adds nothing and breaks nothing
            got, last["found_news_error"] = [], f"{type(exc).__name__}: {str(exc)[:120]}"
        seen_found = {r["url"] for r in rows}
        for row in got:
            if row["url"] not in seen_found:
                seen_found.add(row["url"])
                rows.append(dict(row, id=len(rows), found_free=True))
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
                      "listed": len(listed), "searched": len(searched),
                      "with_news": sum(1 for c in companies if news.get(c["domain"])), "story_rows": len(rows),
                      "event_search_tried": tried, "event_search_rows": len(extra),
                      "event_search_companies": sorted({r["company"] for r in extra}),
                      "candidates": [c["company_name"] for c in cands]}
    return cands


__all__ = ["run_roster", "roster", "roster_prompt", "story_rows", "short_feed", "searched_rows", "classify_prompts", "pick_events", "intent_category",
           "company_news", "CATEGORY_WORDS", "ROSTER_MODEL", "ROSTER_SIZE"]
