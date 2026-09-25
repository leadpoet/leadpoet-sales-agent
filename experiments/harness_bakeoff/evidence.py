"""Check submitted evidence the way the Arena judge will, before submitting.

The judge re-fetches every intent URL and checks three things a model
routinely gets slightly wrong: that the company name appears on the page,
that the snippet is verbatim page text, and that the event words in the
description are on the page. A signal that fails any of them scores zero,
and a company whose required (index 0) signal fails is penalised. Everything
here is deterministic string work over pages the run has usually already
fetched, so it costs no model tokens and few, if any, provider calls.

Domain keys follow the judge's own rule: the last two labels of the host, so
``blog.acme.com`` and ``acme.com`` are one domain, and only signals on
different domains raise the per-company cap (60 -> 80 -> 88).
"""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

MIN_SNIPPET_WORDS = 8
WINDOW_WORDS = 34
MAX_SNIPPET_CHARS = 600
MAX_DESCRIPTION_CHARS = 350
MAX_SIGNALS_PER_INDEX = 3

_NAME_SUFFIXES = {
    "inc", "inc.", "llc", "llc.", "ltd", "ltd.", "corp", "corp.", "co", "co.",
    "company", "group", "holdings", "partners", "lp", "l.p.", "plc", "gmbh", "ag",
}
_SIGNAL_WORDS = frozenset({
    "launched", "launches", "launch", "announced", "announces", "expanded", "expanding",
    "expansion", "partnered", "partnership", "merged", "acquisition", "acquired", "acquires",
    "hired", "hiring", "recruited", "recruiting", "opening", "openings", "opened", "opens",
    "funding", "funded", "raised", "raises", "secured", "secures", "closed", "closes",
    "obtained", "invested", "investment", "seed", "series", "appointed", "appoints",
    "cleared", "clearance", "approved", "approval", "certified", "certification",
})
_INVALID_URL_RE = re.compile(
    r"/alternatives(?:\b|/|\?|$)|/competitors(?:\b|/|\?|$)|indeed\.com/hire/job-description/",
    re.IGNORECASE,
)
_ANTIBOT_RE = re.compile(
    r"access denied|verifying your connection|verifying.{0,30}browser|just a moment|"
    r"enable javascript|please enable js|verifying you are human|sign in to (?:linkedin|see|join|view|continue)|"
    r"join linkedin to|page can.?t be found|this page (?:doesn.?t|does not) exist|"
    r"403\s*[-:|]?\s*forbidden|404\s*[-:|]?\s*(?:not\s*found|page.*not.*found)|this content isn.?t available",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"\bno\s+longer\s+(?:open|available|accepting|listed|active)\b|"
    r"\bnot\s+(?:currently|available|accepting|open|listed|hiring)\b|"
    r"\b(?:page|posting|position)\s+(?:not\s+found|no\s+longer\s+exists|expired|removed)\b|"
    r"\bunable\s+to\s+(?:verify|find|access|locate)\b|\b404\b",
    re.IGNORECASE,
)
_FABRICATED_TLDS = frozenset({
    "beauty", "auction", "mom", "blog", "site", "fun", "click", "sbs", "cyou",
    "rest", "icu", "top", "lol", "quest",
})
_DATE_TOKEN_RE = re.compile(r"\b(?:20\d\d|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b", re.IGNORECASE)


def normalize_text(text: str) -> str:
    lowered = re.sub(r"[^\w\s]", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", lowered).strip()


def evidence_domain(url: str) -> str:
    """The judge's dedup key: the last two labels of the host."""

    raw = str(url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    host = (urlsplit(raw).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def snippet_overlap(snippet: str, content: str) -> float:
    """Fraction of the snippet's 4-word runs that occur verbatim in the page."""

    words = normalize_text(snippet).split()
    if len(words) < 4:
        return 1.0
    page = normalize_text(content).split()
    grams = {tuple(page[i:i + 4]) for i in range(max(0, len(page) - 3))}
    total = len(words) - 3
    hits = sum(1 for i in range(total) if tuple(words[i:i + 4]) in grams)
    return hits / total if total else 1.0


def company_in_content(name: str, text: str) -> bool:
    lowered_name = str(name or "").lower().strip()
    lowered = str(text or "").lower()
    if not lowered_name or not lowered:
        return False
    if lowered_name in lowered:
        return True
    words = [w for w in lowered_name.split() if w not in _NAME_SUFFIXES and len(w) >= 3]
    if not words:
        return False
    if len(words) == 1:
        return bool(re.search(r"\b" + re.escape(words[0]) + r"\b", lowered))
    return all(w in lowered for w in words) and any(
        re.search(re.escape(a) + r"\W+" + re.escape(b), lowered) for a, b in zip(words, words[1:])
    )


def signal_words_grounded(text: str, content: str) -> tuple[int, int]:
    page_words = set(normalize_text(content).split())
    words = set(normalize_text(text).split()) & _SIGNAL_WORDS
    return len(words & page_words), len(words)


def best_window(page_text: str, *, focus: list[str], company_name: str = "") -> str:
    """The 34-word page window carrying the most event words, a date, and the name."""

    words = str(page_text or "").split()
    if len(words) <= WINDOW_WORDS:
        return " ".join(words)
    focus_words = set()
    for item in focus:
        focus_words |= set(normalize_text(item).split()) & _SIGNAL_WORDS
    name_words = {w for w in normalize_text(company_name).split() if len(w) >= 3 and w not in _NAME_SUFFIXES}
    best, best_score = words[:WINDOW_WORDS], -1.0
    for start in range(0, len(words) - WINDOW_WORDS + 1, 4):
        window = words[start:start + WINDOW_WORDS]
        normalized = set(normalize_text(" ".join(window)).split())
        score = 3.0 * len(normalized & focus_words) + 2.0 * len(normalized & name_words)
        if _DATE_TOKEN_RE.search(" ".join(window)):
            score += 1.5
        if score > best_score:
            best, best_score = window, score
    return " ".join(best)


def url_reason(url: str, company_website: str) -> str | None:
    match = _INVALID_URL_RE.search(url or "")
    if match:
        return f"url path {match.group()!r} cannot carry intent evidence"
    domain = evidence_domain(url)
    site = evidence_domain(company_website)
    if domain and site and (domain == site or domain.endswith("." + site)):
        return None
    tld = domain.rsplit(".", 1)[-1] if domain else ""
    return f"fabricated-source tld .{tld}" if tld in _FABRICATED_TLDS else None


def antibot_reason(text: str) -> str | None:
    match = _ANTIBOT_RE.search((text or "")[:5000])
    return f"anti-bot or login wall ({match.group()[:40]!r})" if match and len(text or "") < 4000 else None


def _fit(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0]


def verify_signal(
    signal: Mapping[str, Any],
    *,
    company: Mapping[str, Any],
    page_text: str,
    focus: list[str],
) -> tuple[dict[str, Any] | None, str]:
    """Return (repaired signal, note) or (None, reason) when it would score zero."""

    name = str(company.get("company_name") or "")
    url = str(signal.get("url") or "")
    reason = antibot_reason(page_text)
    if reason:
        return None, reason
    domain = evidence_domain(url)
    site = evidence_domain(str(company.get("company_website") or ""))
    on_company_site = bool(site) and (domain == site or domain.endswith("." + site))
    path = (urlsplit(url).path or "").lower()
    is_job_page = any(marker in path for marker in ("/jobs", "/job/", "/careers"))
    if not on_company_site and not is_job_page and not company_in_content(name, page_text[:12000]):
        return None, "company name not on the evidence page"

    notes: list[str] = []
    snippet = " ".join(str(signal.get("snippet") or "").split())
    description = " ".join(str(signal.get("description") or "").split())
    if len(snippet.split()) < MIN_SNIPPET_WORDS or snippet_overlap(snippet, page_text) < 0.6:
        snippet = best_window(page_text, focus=[*focus, description], company_name=name)
        notes.append("snippet re-cut from page")
    snippet = _fit(snippet, MAX_SNIPPET_CHARS)
    if snippet_overlap(snippet, page_text) < 0.5 or len(snippet.split()) < 4:
        return None, "no verbatim snippet available"
    grounded, total = signal_words_grounded(snippet, page_text)
    if total and not grounded:
        return None, "snippet event words not on page"

    grounded, total = signal_words_grounded(description, page_text)
    if not description or (total and not grounded):
        description = _fit(f"{name}: {snippet}", MAX_DESCRIPTION_CHARS)
        notes.append("description rebuilt from snippet")
    if _NEGATION_RE.search(f"{description} {snippet}"):
        return None, "evidence text negates the event"

    repaired = dict(signal)
    # The intent-details schema (v5) has no signal snippet: the quote is only
    # used here to prove the page carries the event. Attach it back only for
    # the older schema that submits it.
    if "snippet" in signal:
        repaired["snippet"] = snippet
    repaired["description"] = _fit(description, MAX_DESCRIPTION_CHARS)
    return repaired, "; ".join(notes) or "ok"


def verify_companies(
    icp: Mapping[str, Any],
    companies: list[dict[str, Any]],
    fetch_page: Callable[[str], str | None],
    *,
    seconds_left: Callable[[], float],
    min_seconds: float = 25.0,
    report: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Verify every intent signal against its page; keep only what will score.

    ``fetch_page`` returns page text or ``None``. When time runs out the
    remaining companies are kept unverified rather than dropped: an unverified
    signal may still score, a dropped one never can.
    """

    log = report if report is not None else []
    focus = [str(icp.get("intent_signal") or "")] + [str(s) for s in (icp.get("intent_signals") or [])]
    kept: list[dict[str, Any]] = []
    for company in companies:
        if seconds_left() < min_seconds:
            log.append(f"{company.get('company_name')}: kept unverified (out of time)")
            kept.append(company)
            continue
        verified: list[dict[str, Any]] = []
        used_domains: set[str] = set()
        for signal in company.get("intent_signals") or []:
            url = str(signal.get("url") or "")
            reason = url_reason(url, str(company.get("company_website") or ""))
            if reason:
                log.append(f"{company.get('company_name')}: dropped {url}: {reason}")
                continue
            domain = evidence_domain(url)
            if domain in used_domains:
                log.append(f"{company.get('company_name')}: dropped {url}: same domain as an earlier signal")
                continue
            if seconds_left() < min_seconds:
                verified.append(dict(signal))
                used_domains.add(domain)
                continue
            text = fetch_page(url)
            if text is None or len(normalize_text(text).split()) < 40:
                # Our fetcher could not read it; the judge's fetcher may. Keep
                # the signal as submitted rather than turn a maybe into a zero.
                log.append(f"{company.get('company_name')}: {url}: kept unverified (page unreadable here)")
                verified.append(dict(signal))
                used_domains.add(domain)
                continue
            repaired, note = verify_signal(signal, company=company, page_text=text, focus=focus)
            if repaired is None:
                log.append(f"{company.get('company_name')}: dropped {url}: {note}")
                continue
            if note != "ok":
                log.append(f"{company.get('company_name')}: {url}: {note}")
            verified.append(repaired)
            used_domains.add(domain)
        if not any(int(s.get("matched_icp_signal", -1)) == 0 for s in verified):
            log.append(f"{company.get('company_name')}: dropped: no verified required signal")
            continue
        hardened = dict(company)
        hardened["intent_signals"] = verified
        kept.append(hardened)
    return kept


__all__ = [
    "MAX_SIGNALS_PER_INDEX", "antibot_reason", "best_window", "company_in_content",
    "evidence_domain", "normalize_text", "signal_words_grounded", "snippet_overlap",
    "url_reason", "verify_companies", "verify_signal",
]
