"""A company's own "what's new" page as the product-launch evidence, read free over the web-egress bridge.

Why this lane exists. On arena-2026-10-14 ICP 5 -- the ICP that carried that whole round at 47% -- fifteen
companies qualified, and **eight of them cited the company's own site**, not a news story: Gradial's changelog
("New in Gradial: Task UI Updates, Skills Diff Viewer..."), Writer's release notes on support.writer.com,
Uniphore's, Spectro Cloud's and Crusoe's own blogs, sima.ai, caddi.com, skan.ai. Our two discovery lanes both
search a NEWS index (`search_web(category="news")` and `search_news(domain)`), and a company's own changelog
is not news, so those eight companies could never appear in our candidate list however many queries we wrote.
Across three live runs of that ICP our 18-21 candidates contained none of them but Cresta.

The lane is free and bounded: it reads a roster company's own index page over the bridge (no provider call,
not counted in the 200-per-ICP quota), takes the links whose own text reads like a launch, reads at most two
of those posts, and keeps one whose date sits inside the ICP's window. Nothing here invents a date or a
sentence -- both come from the fetched page -- and a company whose pages do not read is simply skipped.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

# The paths a company keeps its own announcements under, most specific first. "/blog" is last of the common
# ones because a blog index is mostly thought-leadership, while a changelog or release-notes page is dated
# product news by construction.
INDEX_PATHS = ("/changelog", "/whats-new", "/release-notes", "/releases", "/product/updates", "/product-updates",
               "/updates", "/news", "/newsroom", "/press", "/press-releases", "/media", "/blog")

# Which categories this lane is worth running for, measured over the 6,550 published rows of
# 2026-10-01..10-14 by whether a QUALIFIED row's primary intent URL sat on the company's own domain:
#
#   category              own-site share   own -> qualified   third party -> qualified
#   LEADERSHIP_CHANGE          67%              23.5%                 8.3%
#   PRODUCT_LAUNCH             62%              18.5%                 9.3%
#   PARTNERSHIP                59%              14.5%                 4.5%
#   FUNDING                    34%              18.1%                16.4%     (no difference; free, so kept)
#   MARKET_EXPANSION           48%               2.8%                 7.8%     <- own site is WORSE
#   FACILITY_OPENING           49%               1.5%                 7.8%     <- own site is WORSE
#   REGULATORY_CLEARANCE       38%              12.3%                23.6%
#   HIRING                     10%               0.0%                 6.4%
#   ACQUISITION                22%               3.3%                 0.0%
#
# An expansion or a facility opening is reported by the local press and a trade title, not by the company's
# own newsroom, and the hiring lane has its own ATS reader. So the lane runs for the first four only.
CATEGORIES = ("PRODUCT_LAUNCH", "LEADERSHIP_CHANGE", "PARTNERSHIP", "FUNDING")
MAX_COMPANIES = 12          # roster companies asked, best first
DEEP_COMPANIES = 20         # ... in an ICP whose kind has historically paid (yieldmap.pays)
MAX_INDEX_READS = 2         # index pages tried per company before giving up on it
MAX_POST_READS = 2          # posts read per company
MAX_LINKS = 6               # launch-looking links considered per index page
_LAUNCH_RE = re.compile(r"launch|unveil|introduc|releas|debut|rolls? out|announc|now available|now generally available|"
                        r"new in |what'?s new|new (?:product|platform|feature|capabilit|agent|model)", re.I)
_DATED_PATH_RE = re.compile(r"/20\d\d[-/]\d\d|[-/]20\d\d[-/]?\d?\d?(?:/|$)")
_SKIP_LINK_RE = re.compile(r"/(?:tag|tags|category|categories|author|page|archive|feed|rss|subscribe|login|pricing)(?:/|$)"
                           r"|\.(?:xml|json|rss|pdf)$", re.I)


def _bare(host: Any) -> str:
    """A host without its "www." prefix. `lstrip("www.")` would not do: it strips a CHARACTER SET, so
    "writer.com" comes back as "riter.com" and support.writer.com stops being writer.com's own site."""

    text = str(host or "").strip().lower().rstrip(".")
    return text[4:] if text.startswith("www.") else text


def _host(url: Any) -> str:
    try:
        return _bare(urlsplit(str(url or "")).hostname or "")
    except ValueError:
        return ""


def same_site(url: Any, domain: str) -> bool:
    """True when this URL is on the company's own domain, its www alias or one of its subdomains."""

    host, base = _host(url), _bare(domain)
    return bool(host and base and (host == base or host.endswith("." + base)))


# A changelog, a release-notes page or a "what's new" page is product news by construction, so a post linked
# from one needs no launch word in its path: Gradial's qualifying entry on 2026-10-14 was "New in Gradial: Task
# UI Updates, Skills Diff Viewer..." under a dated slug, which a word filter on the path throws away. A /blog
# or /news index carries thought-leadership beside announcements, so there the word filter stays.
PRODUCT_INDEX_PATHS = ("/changelog", "/whats-new", "/release-notes", "/releases", "/product/updates",
                       "/product-updates", "/updates")


def launch_links(page: Any, domain: str, index_url: Any = "", words=None) -> list[str]:
    """The links on an index page that could be a dated announcement post, best first."""

    pattern = words or _LAUNCH_RE
    index_path = (urlsplit(str(index_url or "")).path or "/").rstrip("/").lower()
    product_index = index_path in PRODUCT_INDEX_PATHS
    out: list[str] = []
    for link in list(getattr(page, "links", None) or []):
        url = str(link or "").split("#")[0]
        if not url or not same_site(url, domain) or _SKIP_LINK_RE.search(url):
            continue
        path = urlsplit(url).path or "/"
        if len([p for p in path.split("/") if p]) < 2:
            continue                                    # an index or a section, not a post
        if url in out:
            continue
        # Under a changelog or release-notes index, every entry is product news by construction, whatever its
        # slug says: Gradial's qualifying entry on 2026-10-14 sat under a dated slug with no launch word in it.
        # Under a /blog or /news index the slug has to earn its place -- a launch word or a date -- or the
        # index hands us its own navigation and every culture post it ever wrote.
        dated_path = bool(_DATED_PATH_RE.search(path))
        worded = bool(pattern.search(path.replace("-", " ")))
        under_product_index = product_index and bool(index_path) and path.lower().startswith(index_path + "/")
        if under_product_index or dated_path or worded:
            out.append(url)
        if len(out) >= MAX_LINKS:
            break
    return out


def dated_launch(text: str, url: str, *, find_date, window: int, age_days, words=None) -> Optional[dict[str, Any]]:
    """{date, title, excerpt} when this page is a launch post inside the window, else None.

    The date comes from the page itself through the caller's `find_date`, never from the URL alone and never
    from today: a post we cannot date is not evidence the judge can age.
    """

    body = " ".join(str(text or "").split())
    if len(body) < 200:
        return None
    pattern = words or _LAUNCH_RE
    # The announcement itself, not the top of the page. An index page opens with its navigation, and handing
    # that to the event classifier (or later to the quote picker) is how a menu run becomes "evidence".
    found = pattern.search(body)
    if not found:
        return None
    start = max(0, found.start() - 400)
    around = body[start:found.end() + 600]
    date = find_date(around, url) or find_date(body, url)
    if not date:
        return None
    age = age_days(date)
    if age is None or not 0 <= age <= max(1, int(window or 365)):
        return None
    sentence = ""
    for part in re.split(r"(?<=[.!?])\s+", around):
        if pattern.search(part):
            sentence = part.strip()
            break
    sentence = sentence or around[400:600].strip()
    return {"date": str(date)[:10], "title": sentence[:200], "excerpt": sentence[:300] or around[:300]}


def own_rows(icp: Mapping[str, Any], tools: Any, companies: list, *, find_date, age_days, deadline: float, clock,
             read_page=None, last: Optional[dict] = None, words=None) -> list[dict[str, Any]]:
    """Launch rows from the companies' own sites, in `roster.story_rows` shape. Free; never raises.

    `read_page` reads one URL and returns an object with `.text` and `.links`, or None. The default reads only
    over the free bridge: a company whose own site does not answer a plain GET costs nothing and is skipped.
    """

    window = int(icp.get("intent_max_age_days") or 365)
    # How deep to dig. The board's kinds are a fixed vocabulary and 6,550 judged rows say what each kind is
    # worth, so an ICP whose kind has historically paid (13.0-15.6% of rows against 2.5-3.8%) is worth reading
    # eight more companies for. Every read here is free, so the only cost is wall clock.
    cap = MAX_COMPANIES
    try:
        from . import yieldmap

        if yieldmap.pays(icp):
            cap = DEEP_COMPANIES
    except Exception:  # noqa: BLE001 - without the map the lane digs to its ordinary depth
        pass
    reader = read_page or _bridge_reader(tools)
    if reader is None:
        if last is not None:
            last["own_news"] = {"reader": "unavailable"}
        return []
    rows: list[dict[str, Any]] = []
    tried: list[str] = []
    for company in list(companies)[:cap]:
        if clock() >= deadline:
            break
        domain = str((company or {}).get("domain") or "").strip().lower()
        name = str((company or {}).get("company_name") or "").strip()
        if not domain or not name:
            continue
        indexes = 0
        found = False
        for path in INDEX_PATHS:
            if found or indexes >= MAX_INDEX_READS or clock() >= deadline:
                break
            index = reader("https://%s%s" % (domain, path))
            if index is None:
                continue
            indexes += 1
            tried.append(domain + path)
            index_url = "https://%s%s" % (domain, path)
            inline = dated_launch(getattr(index, "text", "") or "", index_url, find_date=find_date,
                                  window=window, age_days=age_days, words=words)
            if inline:
                rows.append({"id": len(rows), "company": name, "domain": domain, "url": index_url,
                             "date": inline["date"], "title": inline["title"], "excerpt": inline["excerpt"]})
                found = True
                break
            posts = 0
            for url in launch_links(index, domain, "https://%s%s" % (domain, path), words=words):
                if posts >= MAX_POST_READS or clock() >= deadline:
                    break
                post = reader(url)
                if post is None:
                    continue
                posts += 1
                hit = dated_launch(getattr(post, "text", "") or "", url, find_date=find_date, window=window,
                                   age_days=age_days, words=words)
                if hit:
                    rows.append({"id": len(rows), "company": name, "domain": domain, "url": url,
                                 "date": hit["date"], "title": hit["title"], "excerpt": hit["excerpt"]})
                    found = True
                    break
    if last is not None:
        last["own_news"] = {"companies": min(len(list(companies)), cap), "cap": cap, "indexes_tried": tried[:24],
                            "rows": len(rows), "found": sorted({r["company"] for r in rows})}
    return rows


MAX_FOUND_HITS = 4
_SKIP_HOSTS = ("linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
               "crunchbase.com", "pitchbook.com", "bloomberg.com", "sec.gov", "wikipedia.org")


def found_rows(icp: Mapping[str, Any], tools: Any, companies: list, *, find_date, age_days, deadline: float,
               clock, make_query, read_page=None, last: Optional[dict] = None, words=None,
               cap: Optional[int] = None) -> list[dict[str, Any]]:
    """Event rows for the companies both free lookups were silent about.  Free; never raises.

    ``own_rows`` reads a company's own site at known index paths.  This reads what a free web search points at,
    which is the only lane left for a company whose own site has no newsroom and whose free news search returned
    nothing -- on the Education ICP of 2026-10-15 that was 14 of the 28 companies on the roster, and none of them
    was ever looked at.

    Nothing here spends.  ``search_web(free_only=True)`` is the contextdev web search at zero credits and the
    pages come over the bridge, so the lane costs wall clock and free connections only -- which matters because
    an ICP's real ceiling is money, not time: the 2026-10-15 run stopped with 384 of its 900 seconds unused and
    $0.52 of its $0.64 research allowance gone.

    The free search dates nothing, so every hit must be dated from its own page through ``dated_launch``, exactly
    as the own-site lane does.  A page that cannot be dated is not a row.
    """

    window = int(icp.get("intent_max_age_days") or 365)
    reader = read_page or _bridge_reader(tools)
    searcher = getattr(tools, "search_web", None)
    if reader is None or not callable(searcher):
        if last is not None:
            last["found_news"] = {"reader": "unavailable" if reader is None else "ok",
                                  "search": "unavailable" if not callable(searcher) else "ok"}
        return []
    limit = MAX_COMPANIES if cap is None else max(0, int(cap))
    rows: list[dict[str, Any]] = []
    tried: list[str] = []
    for company in list(companies)[:limit]:
        if clock() >= deadline:
            break
        domain = str((company or {}).get("domain") or "").strip().lower()
        name = str((company or {}).get("company_name") or "").strip()
        if not domain or not name:
            continue
        try:
            hits = (searcher(make_query(name), free_only=True, limit=MAX_FOUND_HITS) or {}).get("results") or []
        except Exception:  # noqa: BLE001 - a silent search leaves the company where it was
            continue
        tried.append(name)
        for hit in hits[:MAX_FOUND_HITS]:
            if clock() >= deadline:
                break
            url = str((hit or {}).get("url") or "")
            host = re.sub(r"^www\.", "", (urlsplit(url).hostname or "").lower())
            if not url.startswith("http") or any(host == bad or host.endswith("." + bad) for bad in _SKIP_HOSTS):
                continue
            page = reader(url)
            if page is None:
                continue
            got = dated_launch(getattr(page, "text", "") or "", url, find_date=find_date, window=window,
                               age_days=age_days, words=words)
            if got:
                rows.append({"id": len(rows), "company": name, "domain": domain, "url": url,
                             "date": got["date"], "title": got["title"], "excerpt": got["excerpt"]})
                break
    if last is not None:
        last["found_news"] = {"companies": min(len(list(companies)), limit), "cap": limit, "searched": tried[:24],
                              "rows": len(rows), "found": sorted({r["company"] for r in rows})}
    return rows


def _bridge_reader(tools: Any):
    """A reader that only ever uses the free direct lane, or None when that lane is not available."""

    direct = getattr(tools, "_direct", None)
    read = getattr(direct, "read", None)
    available = getattr(direct, "available", None)
    if not callable(read) or (callable(available) and not available()):
        return None

    class Read:
        def __init__(self, text: str, links: list):
            self.text, self.links = text, links

    def reader(url: str):
        try:
            got = read(url, 24_000, deadline=getattr(tools, "deadline", None))
        except Exception:  # noqa: BLE001 - a page that will not read is simply absent
            return None
        if not got or not got.get("ok"):
            return None
        return Read(str(got.get("text") or ""), list(got.get("links") or []))

    return reader


__all__ = ["own_rows", "found_rows", "launch_links", "dated_launch", "same_site", "INDEX_PATHS",
           "MAX_COMPANIES", "MAX_FOUND_HITS",
           "DEEP_COMPANIES",
           "CATEGORIES", "PRODUCT_INDEX_PATHS"]
