
from __future__ import annotations

import json
import re
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlsplit

FTS = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK%010d.json"
MAX_SEARCHES = 14
MAX_FACT_LOOKUPS = 40
GATE_LOOKUPS = 24
HITS_PER_QUERY = 25
JSON_LIMIT = 400_000

_DISPLAY_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<ticker>[A-Z][A-Z0-9.\-]{0,6})\)\s*\(CIK\s*(?P<cik>\d{10})\)")
EVENT_FORMS = "8-K"

PHRASES: dict[str, tuple[str, ...]] = {
    "FACILITY_OPENING": ("new manufacturing facility", "new distribution center", "grand opening",
                         "ribbon cutting", "new store", "store openings", "opened its first",
                         "new retail location", "new office in"),
    "MARKET_EXPANSION": ("expansion into", "expands into", "entered the market", "new market entry",
                         "first location in", "began operations in"),
    "PRODUCT_LAUNCH": ("announced the launch", "launched a new", "general availability",
                       "commercial launch", "unveiled", "introduced a new"),
    "PARTNERSHIP": ("strategic partnership", "entered into a partnership", "partnership agreement",
                    "collaboration agreement", "joint venture", "has partnered with"),
    "FUNDING": ("public offering", "private placement", "credit facility", "has closed a",
                "registered direct offering"),
    "HIRING": ("appointed", "named chief", "joins as", "has hired", "expanding its team"),
    "LEADERSHIP_CHANGE": ("appointed chief executive", "named chief executive", "has been appointed",
                          "succeeds", "steps down", "will retire", "new chief financial officer"),
    "REGULATORY_CLEARANCE": ("FDA clearance", "510(k)", "received approval", "regulatory approval",
                             "CE mark", "premarket approval", "granted clearance"),
    "ACQUISITION": ("has acquired", "definitive agreement to acquire", "completed the acquisition",
                    "agreement to purchase"),
}
_DEFAULT_PHRASES = ("announced today",)
_STOP_LEAD = frozenset({"a", "an", "the", "its", "their", "new" if False else "",
                        "opened", "open", "opens", "announced", "announces", "announce",
                        "launched", "launches", "entered", "enters", "completed",
                        "received", "receives", "appointed", "appoints", "expanded",
                        "expands", "has", "have", "in", "into", "of", "or"}) - {""}


def phrases_for(icp: Mapping[str, Any]) -> list[str]:

    out: list[str] = []
    signal = " ".join(str(icp.get("intent_signal") or "").split())
    out.extend(re.findall(r'"([^"]{4,60})"', signal))
    clause = re.split(r",| in the last | within the last | over the past | per a | per an ",
                      signal, maxsplit=1)[0]
    for part in re.split(r"\bor\b", clause):
        words = [w for w in part.strip(" .;:").split() if w]
        if 2 <= len(words) <= 6:
            out.append(" ".join(words).lower())
        tail = [w for w in words if w.lower() not in _STOP_LEAD]
        if 2 <= len(tail) <= 3 and len(tail) < len(words):
            out.append(" ".join(tail).lower())
    out.extend(PHRASES.get(str(icp.get("intent_category") or "").upper(), _DEFAULT_PHRASES))
    seen, unique = set(), []
    for phrase in out:
        key = re.sub(r"[^a-z0-9 ]", "", phrase.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(phrase)
    return unique[:MAX_SEARCHES]


def _json(direct: Any, url: str, deadline: Optional[float] = None) -> Optional[Any]:

    if deadline is not None and time.monotonic() >= deadline:
        return None
    try:
        got = direct.read(url, JSON_LIMIT, deadline=deadline)
    except Exception:
        return None
    if not got or not got.get("ok"):
        return None
    body = got.get("raw") or ""
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def search(direct: Any, phrase: str, *, start: str, end: str, forms: str = EVENT_FORMS,
           deadline: Optional[float] = None) -> list[dict[str, Any]]:

    url = ("%s?q=%s&forms=%s&dateRange=custom&startdt=%s&enddt=%s"
           % (FTS, quote('"%s"' % phrase.replace('"', " ").strip()), quote(forms), quote(start), quote(end)))
    doc = _json(direct, url, deadline)
    hits = ((doc or {}).get("hits") or {}).get("hits") or []
    out: list[dict[str, Any]] = []
    for hit in hits[:HITS_PER_QUERY]:
        source = hit.get("_source") or {}
        display = (source.get("display_names") or [""])[0]
        match = _DISPLAY_RE.match(" ".join(str(display).split()))
        if not match:
            continue
        ident = str(hit.get("_id") or "")
        accession, _, filename = ident.partition(":")
        out.append({
            "name": match.group("name").strip().rstrip(","),
            "ticker": match.group("ticker"),
            "cik": int(match.group("cik")),
            "date": str(source.get("file_date") or "")[:10],
            "form": str(source.get("file_type") or ""),
            "accession": accession.replace("-", ""),
            "filename": filename,
            "phrase": phrase,
            "sics": [str(x) for x in (source.get("sics") or [])],
            "location": (source.get("biz_locations") or [""])[0],
            "state": (source.get("biz_states") or [""])[0],
            "items": [str(x) for x in (source.get("items") or [])],
        })
    return out


def facts(direct: Any, cik: int, deadline: Optional[float] = None) -> dict[str, Any]:

    doc = _json(direct, SUBMISSIONS % int(cik), deadline)
    if not isinstance(doc, Mapping):
        return {}
    business = (doc.get("addresses") or {}).get("business") or {}
    website = str(doc.get("website") or "").strip()
    return {
        "name": str(doc.get("name") or ""),
        "tickers": [str(t) for t in (doc.get("tickers") or [])],
        "exchanges": [str(x) for x in (doc.get("exchanges") or []) if x],
        "sic": str(doc.get("sic") or ""),
        "industry": str(doc.get("sicDescription") or ""),
        "city": str(business.get("city") or ""),
        "region": str(business.get("stateOrCountry") or ""),
        "country": "United States" if len(str(business.get("stateOrCountry") or "")) == 2 else "",
        "website": website,
        "forms": list((doc.get("filings") or {}).get("recent", {}).get("form") or [])[:200],
    }


def listed(record: Mapping[str, Any]) -> bool:

    exchanges = [str(x).strip().lower() for x in (record.get("exchanges") or []) if str(x).strip()]
    return any(x in ("nasdaq", "nyse", "nyse american", "cboe", "nyse arca") for x in exchanges)



_TERM_STOP = frozenset({"and", "the", "for", "with", "other", "services", "service",
                        "solutions", "technology", "technologies", "company", "companies",
                        "products", "general", "related", "misc", "nec"})
_DANGLING = frozenset({"that", "which", "who", "to", "of", "in", "on", "by", "from", "a", "an"})
TERM_MAX_HITS = 3_000
TERM_MIN_HITS = 8
LAST_TERMS: list = []


def industry_terms(icp: Mapping[str, Any]) -> list:

    out = []
    for field in ("sub_industry", "industry", "product_service"):
        text = " ".join(str(icp.get(field) or "").split())
        if not text:
            continue
        for part in re.split(r"\s*(?:,|/|\band\b|\bor\b)\s*", text):
            words = [w for w in re.split(r"[^A-Za-z0-9]+", part) if w and w.lower() not in _TERM_STOP]
            if not words:
                continue
            head = words[:3]
            while head and head[-1].lower() in _DANGLING:
                head = head[:-1]
            phrase = " ".join(head).lower()
            if len(phrase) >= 5 and phrase not in out:
                out.append(phrase)
    phrases = [t for t in out if " " in t]
    return (phrases or out)[:6]


def speaks_the_industry(blurb: str, terms: list) -> int:

    text = " ".join(str(blurb or "").lower().split())
    if not text:
        return 0
    words = set()
    for phrase, *_ in terms or ():
        for w in re.split(r"[^a-z0-9]+", str(phrase).lower()):
            if len(w) > 4 and w not in _TERM_STOP:
                words.add(w)
    return sum(1 for w in words if w in text)


def industry_first(direct: Any, icp: Mapping[str, Any], *, deadline: Optional[float] = None,
                   limit: int = 40) -> list:

    seen: dict = {}
    used: list = []
    for phrase in industry_terms(icp):
        if deadline is not None and time.monotonic() >= deadline:
            break
        doc = _json(direct, "%s?q=%s&forms=10-K" % (FTS, quote('"%s"' % phrase)), deadline)
        if not doc:
            continue
        hits = int((((doc.get("hits") or {}).get("total") or {}).get("value")) or 0)
        if hits > TERM_MAX_HITS or hits < TERM_MIN_HITS:
            continue
        used.append((phrase, hits))
        buckets = ((doc.get("aggregations") or {}).get("entity_filter") or {}).get("buckets") or []
        for rank, bucket in enumerate(buckets[:30]):
            display = " ".join(str(bucket.get("key") or "").split())
            filer = _DISPLAY_RE.match(display)
            if not filer:
                continue
            cik = int(filer.group("cik"))
            if cik in seen:
                seen[cik]["weight"] += 1
                continue
            seen[cik] = {"name": brand_name(filer.group("name")), "ticker": filer.group("ticker"),
                         "cik": cik, "phrase": phrase, "rank": rank, "weight": 1,
                         "date": "", "form": "10-K", "sics": [], "state": "", "location": "",
                         "items": [], "accession": "", "filename": ""}
        if len(seen) >= limit:
            break
    LAST_TERMS.clear()
    LAST_TERMS.extend(used)
    return sorted(seen.values(), key=lambda r: (-r["weight"], r["rank"]))[:limit]


def candidates(direct: Any, icp: Mapping[str, Any], *, start: str, end: str,
               deadline: Optional[float] = None, limit: int = 40,
               state_ok: Any = None) -> list[dict[str, Any]]:

    seen: dict[int, dict[str, Any]] = {}
    for phrase in phrases_for(icp):
        if deadline is not None and time.monotonic() >= deadline:
            break
        for hit in search(direct, phrase, start=start, end=end, deadline=deadline):
            seen.setdefault(hit["cik"], hit)
        if len(seen) >= limit:
            break
    out: list[dict[str, Any]] = []
    ordered = sorted(seen.values(), key=lambda h: h["date"], reverse=True)
    if callable(state_ok):
        ordered = [h for h in ordered if not h.get("state") or state_ok(h["state"])]
    for lookups, hit in enumerate(ordered):
        cik = hit["cik"]
        if lookups >= MAX_FACT_LOOKUPS or (deadline is not None and time.monotonic() >= deadline):
            break
        known = facts(direct, cik, deadline)
        if not known or not listed(known):
            continue
        row = dict(hit)
        row.update({k: v for k, v in known.items() if k != "name"})
        row["edgar_name"] = known["name"]
        out.append(row)
    return out


def host_of(url: str) -> str:
    return (urlsplit(str(url or "")).hostname or "").lower()



_LEGAL_TAIL = re.compile(
    r"\s*(?:,\s*)?\b(?:inc|inc\.|incorporated|corp|corp\.|corporation|co|co\.|company|"
    r"holdings?|holding|group|plc|llc|l\.l\.c\.|lp|l\.p\.|ltd|ltd\.|limited|pbc|"
    r"sa|nv|ag|se|trust|the)\b\.?\s*$", re.I)
_FILER_TAG = re.compile(r"\s*/[A-Z]{2,4}/\s*$")
_ALL_CAPS = re.compile(r"^[A-Z0-9&.,'\- ]+$")


def brand_name(registrant: str) -> str:

    name = " ".join(str(registrant or "").split())
    name = _FILER_TAG.sub("", name)
    for _ in range(3):
        shorter = _LEGAL_TAIL.sub("", name).strip(" ,")
        if not shorter or shorter == name:
            break
        name = shorter
    if name and _ALL_CAPS.match(name) and len(name) > 3:
        name = " ".join(w if len(w) <= 3 and w.isupper() and w.isalpha() and w in ("AND", "OF", "THE")
                        else w.capitalize() for w in name.split())
    return name or " ".join(str(registrant or "").split())


def _free_rows(tools: Any, query: str) -> list[dict[str, Any]]:
    try:
        rows = tools._free_search(query)
    except Exception:
        return []
    return [r for r in rows or [] if isinstance(r, Mapping)]


_NOT_A_COMPANY_HOST = ("sec.gov", "wikipedia.org", "linkedin.com", "x.com", "twitter.com",
                       "facebook.com", "youtube.com", "glassdoor.com", "indeed.com")
_TWO_LEVEL = ("co.uk", "com.au", "co.jp", "com.br", "co.nz", "com.mx", "co.za", "com.sg")


def registrable(host: str) -> str:

    parts = [x for x in str(host or "").lower().split(".") if x]
    if len(parts) < 2:
        return ""
    tail = ".".join(parts[-2:])
    if tail in _TWO_LEVEL and len(parts) >= 3:
        return ".".join(parts[-3:])
    return tail


def _tokens(text: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if len(t) > 2}


def _squash(text: str) -> str:

    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _same_company(name: str, other: str) -> bool:

    words, theirs = _tokens(name), _tokens(other)
    if words and theirs and (words & theirs):
        return True
    here, there = _squash(name), _squash(other)
    return bool(here) and bool(there) and (here == there or here in there or there in here)


def names_the_company(name: str, domain: str) -> bool:

    label = registrable(domain).split(".")[0]
    words = _tokens(name)
    if not label or not words:
        return False
    squashed = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    return (label in words or label == squashed
            or (len(label) > 4 and label in squashed)
            or any(len(w) > 4 and w in label for w in words))


def listed_as(direct: Any, name: str, deadline: Optional[float] = None) -> Optional[dict]:

    key = _squash(name)
    if not key:
        return None
    doc = _json(direct, "%s?q=%s" % (FTS, quote('"%s"' % " ".join(str(name).split()))), deadline)
    if doc is None:
        return None
    buckets = ((doc.get("aggregations") or {}).get("entity_filter") or {}).get("buckets") or []
    seen = False
    for bucket in buckets[:30]:
        display = " ".join(str(bucket.get("key") or "").split())
        filer = _DISPLAY_RE.match(display)
        if not filer:
            continue
        ticker = _squash(filer.group("ticker"))
        registrant = brand_name(filer.group("name"))
        if ticker != key and not _same_company(name, registrant):
            continue
        seen = True
        known = facts(direct, int(filer.group("cik")), deadline)
        if known and listed(known):
            return known
    return {} if seen else None


_DESCRIBES = re.compile(
    r"\b(?:is|are|operates|provides|offers|specializ\w*|serves|designs|manufactur\w*|"
    r"develops|sells|distributes|owns)\b", re.I)
_MENU_RUN = re.compile(r"(?:\b[A-Z][a-z]+\b[ ]){6,}")
ABOUT_SCAN = 60_000
_ABBREV = ("Inc.", "Corp.", "Co.", "Ltd.", "L.P.", "L.L.C.", "U.S.", "U.K.", "St.", "No.")


def _sentences(text: str, name: str) -> list:
    probe = text
    for token in [t for t in (name, *_ABBREV) if t and "." in t]:
        for match in re.finditer(re.escape(token), text):
            start, end = match.span()
            probe = probe[:start] + probe[start:end].replace(".", "\u2024") + probe[end:]
    out, prev = [], 0
    for gap in re.finditer(r"(?<=[.!?])\s+", probe):
        out.append(text[prev:gap.start()])
        prev = gap.end()
    out.append(text[prev:])
    return out


def _names_exactly(text: str, name: str) -> bool:

    words = [w for w in re.split(r"[^A-Za-z0-9]+", str(name or "")) if w]
    if not words:
        return False
    pattern = r"[^A-Za-z0-9]{0,3}".join(re.escape(w) for w in words[:3])
    return re.search(r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])", str(text or ""), re.I) is not None


def about_sentence(text: str, name: str) -> str:

    body = " ".join(str(text or "").split())[:ABOUT_SCAN]
    if not body or not _squash(name):
        return ""
    for sentence in _sentences(body, name):
        if not 8 <= len(sentence.split()) <= 70:
            continue
        if _MENU_RUN.search(sentence):
            continue
        if not _names_exactly(sentence, name):
            continue
        if _DESCRIBES.search(sentence):
            return sentence
    return ""


def judge_readable(direct: Any, url: str, deadline: Optional[float] = None) -> tuple:

    if not url:
        return False, ""
    try:
        got = direct.read(url, 300_000, deadline=deadline)
    except Exception:
        return True, ""
    if got is None:
        return not _known_bad(direct, url), ""
    if not got.get("ok"):
        return False, ""
    return True, str(got.get("text") or "")


def _known_bad(direct: Any, url: str) -> bool:

    try:
        if direct.outcome.get(url) is False or url in direct.failed:
            return True
        host = host_of(url)
        if direct.strikes.get(host, 0) >= 1:
            return True
        return any(host == h or host.endswith("." + h) for h in direct.skip_hosts)
    except Exception:
        return False


def rank_evidence(direct: Any, items: list, *, name: str, extra_urls: list = (),
                  terms: list = (), deadline: Optional[float] = None, cap: int = 3) -> list:

    scored, seen = [], set()
    for item in list(items or []):
        url = str((item or {}).get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        readable, text = judge_readable(direct, url, deadline) if direct is not None else (True, "")
        describes = bool(about_sentence(text, name)) if text else False
        speaks = speaks_the_industry(text, terms) if (text and terms) else 0
        scored.append(((0 if readable else 1, 0 if describes else 1, -speaks), item))
    for url in list(extra_urls or []):
        if len(scored) >= cap:
            break
        url = str(url or "")
        if not url or url in seen:
            continue
        seen.add(url)
        if direct is None:
            continue
        readable, text = judge_readable(direct, url, deadline)
        quote = about_sentence(text, name) if readable and text else ""
        if not quote:
            continue
        scored.append(((0, 0, -speaks_the_industry(text, terms) if terms else 0),
                       {"url": url, "quote": quote[:2000]}))
    scored.sort(key=lambda pair: pair[0])
    return [item for _rank, item in scored][:cap]


def _bridge(tools: Any) -> Any:

    if not hasattr(tools, "_direct"):
        return None
    borrowed = getattr(tools, "_direct", None)
    try:
        if borrowed is not None and borrowed.available() and getattr(borrowed, "json_hosts", False):
            return borrowed
    except Exception:
        pass
    cached = getattr(tools, "_edgar_direct", None)
    if cached is not None:
        return cached if cached.available() else None
    try:
        from . import webfetch as _webfetch
    except ImportError:
        try:
            import importlib
            import os as _os
            _webfetch = importlib.import_module(
                "%s.webfetch" % _os.path.basename(_os.path.dirname(_os.path.abspath(__file__))))
        except Exception:
            return None
    try:
        own = _webfetch.Direct(enabled=True, local=True, timeout=12.0,
                               max_fetches=MAX_SEARCHES + MAX_FACT_LOOKUPS + 3 * GATE_LOOKUPS,
                               json_hosts=True)
    except Exception:
        return None
    try:
        setattr(tools, "_edgar_direct", own)
    except Exception:
        pass
    return own if own.available() else None


def domain_for(tools: Any, name: str, ticker: str = "") -> tuple:

    for query in ('"%s" investor relations' % name, "%s %s official site" % (name, ticker)):
        for row in _free_rows(tools, query):
            url = str(row.get("url") or "")
            host = host_of(url)
            if not host or any(host == bad or host.endswith("." + bad) for bad in _NOT_A_COMPANY_HOST):
                continue
            domain = registrable(host)
            if not domain or not names_the_company(name, domain):
                continue
            blurb = " ".join(("%s. %s" % (row.get("title") or "",
                                          row.get("description") or row.get("excerpt") or "")).split())
            return domain, url, blurb[:600]
    return "", "", ""


def public_lane(tools: Any, icp: Mapping[str, Any], *, start: str, end: str, industry_fit: Any,
                state_ok: Any = None, deadline: Optional[float] = None,
                want: int = 12) -> list[dict[str, Any]]:

    direct = _bridge(tools)
    if direct is None:
        return []
    requested = icp.get("industry")
    country = " ".join(str(icp.get("country") or "").split()).lower()
    ordered = list(industry_first(direct, icp, deadline=deadline, limit=want * 3))
    known_ciks = {row["cik"] for row in ordered}
    for row in candidates(direct, icp, start=start, end=end, deadline=deadline,
                          limit=want * 3, state_ok=state_ok):
        if row["cik"] not in known_ciks:
            ordered.append(row)

    checked: list = []
    for row in ordered:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if len(checked) >= want * 2:
            break
        known = row if row.get("exchanges") else facts(direct, row["cik"], deadline)
        if not known or not listed(known):
            continue
        region = str(known.get("region") or row.get("region") or "")
        if country and country.startswith("united states") and not region:
            continue
        if callable(state_ok) and not state_ok(region):
            continue
        merged = dict(row)
        merged.update({k: v for k, v in known.items() if k != "name" and v})
        try:
            fits, _ = industry_fit(requested, merged.get("industry"), None)
        except Exception:
            fits = False
        checked.append((bool(fits), merged))

    if requested and any(fits for fits, _ in checked):
        checked = [(fits, row) for fits, row in checked if fits]

    resolved: list = []
    for _fits, row in checked:
        if len(resolved) >= want + 6 or (deadline is not None and time.monotonic() >= deadline):
            break
        name = brand_name(row.get("name") or "")
        domain, page, blurb = domain_for(tools, name, row.get("ticker", ""))
        if not domain or not page:
            continue
        speaks = speaks_the_industry(blurb, LAST_TERMS)
        resolved.append((-speaks, -(row.get("weight") or 0), name, domain, page, row))
    resolved.sort(key=lambda r: (r[0], r[1]))

    out: list[dict[str, Any]] = []
    for _says, _w, name, domain, page, row in resolved:
        if len(out) >= want:
            break
        edgar_facts = {k: row.get(k) for k in ("ticker", "cik", "date", "industry", "city",
                                               "region", "exchanges", "accession")}
        if row.get("form") == "10-K" or not row.get("date"):
            out.append({"company_name": name, "domain": domain, "source": "edgar",
                        "edgar": edgar_facts})
            continue
        event = "%s filed %s on %s, matching %r" % (name, row.get("form") or "8-K",
                                                    row.get("date"), row.get("phrase"))
        out.append({"company_name": name, "domain": domain, "url": page,
                    "event": event[:300], "date": row.get("date"),
                    "snippet": event[:600], "fit": "likely", "source": "edgar",
                    "edgar": edgar_facts})
    return out


__all__ = ["PHRASES", "phrases_for", "search", "facts", "listed", "candidates", "host_of",
           "domain_for", "public_lane", "brand_name", "industry_terms", "industry_first", "speaks_the_industry", "registrable", "names_the_company", "listed_as", "about_sentence", "judge_readable", "rank_evidence",
           "FTS", "SUBMISSIONS", "EVENT_FORMS"]
