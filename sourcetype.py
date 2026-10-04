"""Proof-source admissibility and press-release datelines, written from the judge's stated rules."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

WIRE_HOSTS = ("prnewswire.com", "businesswire.com", "globenewswire.com", "newswire.ca", "accesswire.com",
              "einpresswire.com", "prweb.com", "newsfilecorp.com", "marketwired.com", "prlog.org", "newswire.com",
              "issuewire.com", "24-7pressrelease.com", "openpr.com", "prunderground.com", "send2press.com",
              "releasewire.com", "mynewsdesk.com", "cision.com", "prnewswire.co.uk", "prnasia.com", "presswire.com")
ATS_HOSTS = ("greenhouse.io", "ashbyhq.com", "lever.co", "workable.com", "smartrecruiters.com", "myworkdayjobs.com",
             "bamboohr.com", "jobvite.com", "icims.com", "recruitee.com", "teamtailor.com", "breezy.hr", "personio.de",
             "personio.com", "jazzhr.com", "applytojob.com", "dover.com", "rippling.com", "pinpointhq.com")
NEWS_HOSTS = ("finsmes.com", "tracxn.com", "crunchbase.com", "dealroom.co", "pitchbook.com", "cbinsights.com",
              "techcrunch.com", "venturebeat.com", "businessinsider.com", "reuters.com", "bloomberg.com", "forbes.com",
              "fortune.com", "wsj.com", "ft.com", "cnbc.com", "axios.com", "yahoo.com", "msn.com", "betakit.com",
              "thelogic.co", "siliconangle.com", "fiercehealthcare.com", "medcitynews.com", "mobihealthnews.com",
              "healthcareitnews.com", "adexchanger.com", "martechseries.com", "techfundingnews.com", "eu-startups.com",
              "tech.eu", "sifted.eu", "startupdaily.net", "smartcompany.com.au", "itnews.com.au", "zdnet.com",
              "theregister.com", "computerweekly.com", "arstechnica.com", "wired.com", "theverge.com", "engadget.com",
              "fastcompany.com", "inc.com", "entrepreneur.com", "bizjournals.com", "geekwire.com", "thenextweb.com",
              "startupintros.com", "trysignalbase.com", "startupranking.com", "salestools.io", "growjo.com",
              "citybiz.co", "technical.ly", "theaiinsider.tech", "theoutpost.ai", "dallasinnovates.com", "inman.com",
              "commercialobserver.com", "therealdeal.com", "globest.com", "ecommercenews.com.au", "adnews.com.au",
              "startuprise.org", "ua.news", "jpost.com", "calcalistech.com", "globes.co.il", "moneymag.com.au",
              "multiples.vc", "public.com", "craft.co", "owler.com", "zoominfo.com", "builtin.com", "siliconrepublic.com",
              "unite.ai", "ventureburn.com", "finextra.com", "fintechfutures.com", "crowdfundinsider.com", "pymnts.com",
              "thepaypers.com", "fintech.global", "datacenterdynamics.com", "datacenterknowledge.com", "hpcwire.com",
              "techtarget.com", "infoq.com", "analyticsindiamag.com", "aibusiness.com", "maginative.com", "techround.co.uk",
              "techstartups.com", "pehub.com", "privateequitywire.co.uk", "americanbanker.com", "retaildive.com",
              "healthcaredive.com", "ciodive.com", "supplychaindive.com", "hrdive.com", "constructiondive.com", "biopharmadive.com",
              "medtechdive.com", "fiercebiotech.com", "fiercepharma.com", "statnews.com", "healthleadersmedia.com")
_NEWS_TOKENS = ("news", "times", "journal", "herald", "tribune", "daily", "gazette", "magazine", "insider", "today",
                "weekly", "observer", "chronicle", "dispatch", "inquirer", "reporter", "bulletin", "digest", "post")
_ARTICLE_PATH_RE = re.compile(r"/(?:19|20)\d{2}/(?:\d{1,2}/)?|/(?:news|article|articles|story|stories)/", re.I)
_REGULATOR_SUFFIXES = (".gov", ".gov.au", ".gov.uk", ".gc.ca", ".gov.sg", ".govt.nz", ".europa.eu", ".gouv.fr")

_CLAUSE_RE = re.compile(r"\b(?:with proof in|proof in|per|as shown in|evidenced by|according to)\s+(.+)$", re.I)
_OPEN_RE = re.compile(r"coverage|\bnews\b|trade press|reporting|\barticles?\b|\bmedia\b|publications?|database|"
                      r"third[- ]party|independent", re.I)
_FIRST_PARTY_RE = re.compile(r"press release|press announcement|company release|announcement|newsroom|blog|"
                             r"product (?:page|update|release)|changelog|release notes|launch|board update|"
                             r"company (?:post|update|website)|partner page|website", re.I)
_REGULATOR_RE = re.compile(r"regulat|\bfda\b|notice|filing|certification|listing|state announcement|government|"
                           r"registry|permit", re.I)
_ATS_RE = re.compile(r"job postings?|careers? page|job board", re.I)

_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"), 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_DATELINE_RE = re.compile(r"(?:^|\n)[^\n]{0,120}?\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) +
                          r")\.?\s+(\d{1,2}),\s+(\d{4})\s*[-–—]", re.I)


def host_of(url: Any) -> str:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def proof_clause(signal: Any) -> str:
    """The proof-source list of an ICP intent signal ('' when it names none)."""

    text = " ".join(str(signal or "").split())
    match = None
    for match in _CLAUSE_RE.finditer(text):
        pass
    return match.group(1).rstrip(" .") if match else ""


def allowed_kinds(signal: Any) -> Optional[frozenset]:
    """None when the signal admits independent coverage (or names no sources); else the admissible source classes."""

    clause = proof_clause(signal)
    if not clause or _OPEN_RE.search(clause):
        return None
    kinds: set[str] = set()
    if _FIRST_PARTY_RE.search(clause):
        kinds |= {"first_party", "wire", "linkedin"}
    if re.search(r"investor", clause, re.I):
        kinds |= {"first_party", "wire", "unknown"}
    if _REGULATOR_RE.search(clause):
        kinds |= {"regulator"}
    if _ATS_RE.search(clause):
        kinds |= {"ats", "first_party"}
    return frozenset(kinds) if kinds else None


def _name_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def source_kind(url: Any, company_domain: Any = "", company_name: Any = "") -> str:
    host = host_of(url)
    if not host:
        return "unknown"
    domain = host_of("https://" + str(company_domain or "").strip().lstrip("/")) if company_domain else ""
    if domain and (host == domain or host.endswith("." + domain)):
        return "first_party"
    if _is(host, WIRE_HOSTS):
        return "wire"
    if _is(host, ("linkedin.com",)):
        path = urlsplit(str(url)).path.lower()
        slug = re.match(r"/(?:company|posts)/([^/_?#]+)", path)
        key = _name_key(company_name)
        return "linkedin" if slug and key and (key in _name_key(slug.group(1)) or _name_key(slug.group(1)) in key) else "news"
    if _is(host, ATS_HOSTS):
        return "ats"
    if host.endswith(_REGULATOR_SUFFIXES) or _is(host, ("fda.gov", "sec.gov")):
        return "regulator"
    if _is(host, NEWS_HOSTS):
        return "news"
    label = host.split(".")[-2] if host.count(".") >= 1 else host
    if any(token in label for token in _NEWS_TOKENS):
        return "news"
    if _ARTICLE_PATH_RE.search(urlsplit(str(url)).path or ""):
        return "news"
    return "unknown"


def admissible(url: Any, company_domain: Any, company_name: Any, signal: Any) -> bool:
    """False only when the ICP names its proof sources, independent coverage is not among them, and the URL is a known."""

    kinds = allowed_kinds(signal)
    if kinds is None:
        return True
    kind = source_kind(url, company_domain, company_name)
    if kind == "news":
        return False
    if kind in ("regulator", "ats") and kind not in kinds:
        return False
    return True


def prefer_own_source(url: Any, company_domain: Any, company_name: Any, signal: Any) -> bool:
    """On a source-restricted."""

    return allowed_kinds(signal) is not None and source_kind(url, company_domain, company_name) == "unknown"


def body_dateline(text: Any) -> Optional[str]:
    """The first press-release dateline in the opening of a page body ('City, ST - April 22, 2025 - ...')."""

    for match in _DATELINE_RE.finditer(str(text or "")[:1500]):
        try:
            return date(int(match.group(3)), _MONTHS[match.group(1).lower()], int(match.group(2))).isoformat()
        except (KeyError, ValueError):
            continue
    return None


_EVENT_WORDS = {"FUNDING": "raises funding round", "PRODUCT_LAUNCH": "launches", "ACQUISITION": "acquires",
                "PARTNERSHIP": "partnership", "MARKET_EXPANSION": "expands", "FACILITY_OPENING": "opens facility",
                "REGULATORY_CLEARANCE": "clearance approval", "LEADERSHIP_CHANGE": "appoints", "HIRING": "hiring"}


def first_party_row(tools: Any, name: str, domain: str, category: str, *, window_days: int,
                    category_pattern: Optional[str] = None, own_only: bool = False) -> Optional[dict[str, Any]]:
    """One bounded search on the company's own domain plus the press wires for the same kind of event; the first row.
    ``own_only`` searches (and accepts) the company's own domain alone."""

    key = _name_key(name)
    if not key or not domain:
        return None
    query = f"{name} {_EVENT_WORDS.get(category, 'announces')}"
    data = tools.search_web(query, recency_days=max(30, int(window_days)), limit=4,
                            include_domains=[domain] + ([] if own_only else list(WIRE_HOSTS[:10])), category="")
    pattern = re.compile(category_pattern, re.I) if category_pattern else None
    for row in (data or {}).get("results") or []:
        url = str(row.get("url") or "")
        if source_kind(url, domain, name) not in (("first_party",) if own_only else ("first_party", "wire")):
            continue
        text = f"{row.get('title') or ''} {row.get('text') or row.get('excerpt') or ''}"
        if key not in _name_key(text[:3000]):
            continue
        if pattern and not pattern.search(text[:4000]):
            continue
        return dict(row)
    return None


__all__ = ["allowed_kinds", "admissible", "prefer_own_source", "source_kind", "body_dateline", "first_party_row", "proof_clause",
           "WIRE_HOSTS", "NEWS_HOSTS", "ATS_HOSTS"]
