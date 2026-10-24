
from __future__ import annotations

import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

INDEX_PATHS = ("/changelog", "/whats-new", "/release-notes", "/releases", "/product/updates", "/product-updates",
               "/updates", "/news", "/newsroom", "/press", "/press-releases", "/media", "/blog")

CATEGORIES = ("PRODUCT_LAUNCH", "LEADERSHIP_CHANGE", "PARTNERSHIP", "FUNDING")
MAX_COMPANIES = 12
DEEP_COMPANIES = 20
MAX_INDEX_READS = 2
MAX_POST_READS = 2
MAX_LINKS = 6
_LAUNCH_RE = re.compile(r"launch|unveil|introduc|releas|debut|rolls? out|announc|now available|now generally available|"
                        r"new in |what'?s new|new (?:product|platform|feature|capabilit|agent|model)", re.I)
_DATED_PATH_RE = re.compile(r"/20\d\d[-/]\d\d|[-/]20\d\d[-/]?\d?\d?(?:/|$)")
_SKIP_LINK_RE = re.compile(r"/(?:tag|tags|category|categories|author|page|archive|feed|rss|subscribe|login|pricing)(?:/|$)"
                           r"|\.(?:xml|json|rss|pdf)$", re.I)


def _bare(host: Any) -> str:

    text = str(host or "").strip().lower().rstrip(".")
    return text[4:] if text.startswith("www.") else text


def _host(url: Any) -> str:
    try:
        return _bare(urlsplit(str(url or "")).hostname or "")
    except ValueError:
        return ""


def same_site(url: Any, domain: str) -> bool:

    host, base = _host(url), _bare(domain)
    return bool(host and base and (host == base or host.endswith("." + base)))


PRODUCT_INDEX_PATHS = ("/changelog", "/whats-new", "/release-notes", "/releases", "/product/updates",
                       "/product-updates", "/updates")


def launch_links(page: Any, domain: str, index_url: Any = "", words=None) -> list[str]:

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
            continue
        if url in out:
            continue
        dated_path = bool(_DATED_PATH_RE.search(path))
        worded = bool(pattern.search(path.replace("-", " ")))
        under_product_index = product_index and bool(index_path) and path.lower().startswith(index_path + "/")
        if under_product_index or dated_path or worded:
            out.append(url)
        if len(out) >= MAX_LINKS:
            break
    return out


def dated_launch(text: str, url: str, *, find_date, window: int, age_days, words=None) -> Optional[dict[str, Any]]:

    body = " ".join(str(text or "").split())
    if len(body) < 200:
        return None
    pattern = words or _LAUNCH_RE
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

    window = int(icp.get("intent_max_age_days") or 365)
    cap = MAX_COMPANIES
    try:
        from . import yieldmap

        if yieldmap.pays(icp):
            cap = DEEP_COMPANIES
    except Exception:
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
        except Exception:
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
        except Exception:
            return None
        if not got or not got.get("ok"):
            return None
        return Read(str(got.get("text") or ""), list(got.get("links") or []))

    return reader


__all__ = ["own_rows", "found_rows", "launch_links", "dated_launch", "same_site", "INDEX_PATHS",
           "MAX_COMPANIES", "MAX_FOUND_HITS",
           "DEEP_COMPANIES",
           "CATEGORIES", "PRODUCT_INDEX_PATHS"]
