"""Pure normalization and evidence helpers for the sourcing pipeline."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any
from agent.safe_urls import urlsplit

BUCKETS = ("0-1", "2-10", "11-50", "51-200", "201-500", "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+")
_BUCKET_RANGES = ((0, 1), (2, 10), (11, 50), (51, 200), (201, 500), (501, 1000), (1001, 5000), (5001, 10000), (10001, 10**9))
_COUNTRY_ALIASES = {
    "united states": ("united states", "united states of america", "usa", "u.s.a.", "u.s."),
    "united kingdom": ("united kingdom", "england", "scotland", "wales", "great britain"),
}
_US_STATES = ("Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut","Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa","Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan","Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada","New Hampshire","New Jersey","New Mexico","New York","North Carolina","North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island","South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont","Virginia","Washington","West Virginia","Wisconsin","Wyoming")
_US_STATE_RE = re.compile(r"\b(" + "|".join(_US_STATES) + r")\b|\b[A-Z][a-z]+, (AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b")
_LEGAL_SUFFIX_RE = re.compile(r"[,\s]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co\.|plc|gmbh|s\.a\.|ag|pty|pty\.|holdings)\s*$", re.I)
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"


def _clean(text: str) -> str:
    """Remove schema-forbidden invisible characters and collapse whitespace."""
    safe = "".join(
        " " if unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} else ch
        for ch in str(text or "")
    )
    return re.sub(r"\s+", " ", safe).strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(text).lower()).strip()


def _host(url: str) -> str:
    try:
        host = urlsplit(url if "://" in url else "https://" + url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def _linkedin_identity_ok(name: str, domain: str, linkedin: str) -> bool:
    """Reject a populated LinkedIn URL only when it clearly names another brand."""
    if not linkedin:
        return True
    slug = "".join(_norm(urlsplit(linkedin if "://" in linkedin else "https://" + linkedin).path.rsplit("/", 1)[-1]).split())
    brand_words = sorted({
        word for word in _norm(f"{name} {str(domain).split('.')[0]}").split()
        if len(word) >= 3 and word not in {"company", "global", "group", "holdings"}
    }, key=len, reverse=True)
    residual = slug
    for word in brand_words:
        residual = residual.replace(word, "")
    for generic in ("corporation", "technologies", "financial", "software", "official", "limited", "company", "tech", "corp", "ltd", "llc", "inc", "co", "ai"):
        residual = residual.replace(generic, "")
    return bool(slug and len(residual) == 0 and any(word in slug for word in brand_words))


def _country_from_context(page_text: str, country: str) -> tuple[bool | None, str]:
    if _clean(country).lower() != "united states":
        return None, ""
    match = _US_STATE_RE.search(page_text[:2500])
    return (True, match.group(1) or match.group(2)) if match else (None, "")


def _display_name(name: str, profile_name: str = "") -> str:
    n = _clean(re.sub(r"\s*\([^)]*\)\s*", " ", _clean(name))).rstrip(",.")
    stripped = _LEGAL_SUFFIX_RE.sub("", n).strip()
    if len(stripped) >= 3:
        n = stripped
    profile = _clean(profile_name)
    if profile and len(profile) >= 3 and profile.lower() != profile:
        return profile[:200]
    return n[:200]


def _bucket_for(count: Any) -> str:
    try:
        number = int(float(str(count).replace(",", "").replace("+", "")))
    except (TypeError, ValueError):
        return ""
    for bucket, (lower, upper) in zip(BUCKETS, _BUCKET_RANGES):
        if lower <= number <= upper:
            return bucket
    return ""


def _neighbor_buckets(bucket: str) -> set[str]:
    if bucket not in BUCKETS:
        return set()
    index = BUCKETS.index(bucket)
    return {BUCKETS[i] for i in (index - 1, index, index + 1) if 0 <= i < len(BUCKETS)}


def _country_ok(location: str, country: str) -> bool | None:
    location_norm = _clean(location).lower()
    if not location_norm:
        return None
    wanted = _clean(country).lower()
    aliases = _COUNTRY_ALIASES.get(wanted, (wanted,))
    if any(re.search(r"(^|[^a-z])" + re.escape(alias) + r"([^a-z]|$)", location_norm) for alias in aliases):
        return True
    if wanted == "united states" and re.search(r"(^|[^a-z])(canada|united kingdom|australia|germany|france|india|israel|singapore|netherlands|ireland|spain|italy|japan|china|brazil|mexico|sweden|switzerland)([^a-z]|$)", location_norm):
        return False
    return None


def _parse_date(value: Any) -> date | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 6] if "T" in fmt else text, fmt).date()
        except ValueError:
            continue
    relative = re.match(r"(\d+)\s+(day|week|month|hour)s?\s+ago", text.lower())
    if relative:
        number, unit = int(relative.group(1)), relative.group(2)
        days = number if unit == "day" else number * 7 if unit == "week" else number * 30 if unit == "month" else 0
        base = _parse_date(os.environ.get("LAB_ARENA_EVALUATION_DATE") or os.environ.get("BAKEOFF_EVALUATION_DATE")) or date.today()
        return base - timedelta(days=days)
    match = re.search(r"(20\d\d)-(\d\d)-(\d\d)", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _date_in_text(text: str) -> date | None:
    head = _clean(text)[:1500]
    match = re.search(r"\b(" + _MONTHS + r")\.? (\d{1,2}),? (20\d\d)\b", head, re.I)
    if match:
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                month = match.group(1)[:3] if fmt == "%b %d %Y" else match.group(1)
                return datetime.strptime(f"{month} {match.group(2)} {match.group(3)}", fmt).date()
            except ValueError:
                continue
    match = re.search(r"\b(\d{1,2}) (" + _MONTHS + r")\.? (20\d\d)\b", head, re.I)
    if match:
        try:
            return datetime.strptime(f"{match.group(2)[:3]} {match.group(1)} {match.group(3)}", "%b %d %Y").date()
        except ValueError:
            pass
    return _parse_date(head)


def _snippet_on_page(snippet: str, page: str) -> str:
    snippet_clean, page_clean = _clean(snippet), _clean(page)
    if not snippet_clean or not page_clean:
        return ""
    if snippet_clean in page_clean:
        return snippet_clean
    table = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
    snippet_relaxed, page_relaxed = snippet_clean.translate(table), page_clean.translate(table)
    if snippet_relaxed in page_relaxed:
        return snippet_relaxed
    for part in sorted(re.split(r"(?<=[.!?])\s+", snippet_relaxed), key=len, reverse=True):
        if len(part) >= 40 and part in page_relaxed:
            return part
    return ""


def _plain_query(text: Any) -> str:
    query = str(text or "")
    query = re.sub(r"(?<!\w)-\S+", " ", query)
    query = re.sub(r"(?i)\b(site|inurl|intitle|after|before):\S*", " ", query)
    query = re.sub(r"(?<!\w)-(?!\w)", " ", query)
    query = re.sub(r"[()\"'“”]", " ", query)
    query = re.sub(r"(?i)\bOR\b|\bAND\b", " ", query)
    query = re.sub(r"(?i)\b(past|last) (12 months|year)\b", " ", query)
    return " ".join(_clean(query).split()[:14])


__all__ = [
    "BUCKETS", "_bucket_for", "_clean", "_country_from_context", "_country_ok",
    "_date_in_text", "_display_name", "_host", "_linkedin_identity_ok", "_neighbor_buckets", "_norm",
    "_parse_date", "_plain_query", "_snippet_on_page",
]
