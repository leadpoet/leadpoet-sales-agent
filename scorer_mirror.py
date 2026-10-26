"""Deterministic mirrors of the Arena scorer's pre-LLM gates and contracts.

Every rule here is transcribed from the platform at repo HEAD (LAB-LOG #191):
  qualification/scoring/verification_helpers.py   text/snippet/grounding checks
  qualification/scoring/intent_signal_gate.py     URL structure, anti-bot, freshness
  qualification/scoring/lead_scorer.py            stage, untrusted TLDs, negation,
                                                  source multipliers, intent caps
  qualification/scoring/competition.py            source inference, ICP normalization
  qualification/employee_buckets.py               LinkedIn buckets
  qualification/competition_models.py             the output contract
  qualification/scoring/company_fit_decision.py   identity name/domain/slug keys

The point of mirroring is that a harness can apply the scorer's own rejection
rules to its own output BEFORE submitting, instead of hoping the LLM got it
right.  tests/test_mirror_parity.py asserts these functions agree with the
platform code, so drift shows up as a failing test rather than a zero score.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator



def normalize_text(text: str) -> str:
    t = str(text or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def snippet_overlap(snippet: str, content: str) -> float:
    """Fraction of the snippet's 4-word n-grams present in the content."""

    snippet_words = normalize_text(snippet).split()
    if len(snippet_words) < 4:
        return 1.0
    content_words = normalize_text(content).split()
    grams = {tuple(content_words[i : i + 4]) for i in range(len(content_words) - 3)}
    total = len(snippet_words) - 3
    matches = sum(1 for i in range(total) if tuple(snippet_words[i : i + 4]) in grams)
    return matches / total if total > 0 else 1.0


_NAME_SUFFIXES = {
    "inc", "inc.", "llc", "llc.", "ltd", "ltd.", "corp", "corp.", "co", "co.",
    "company", "group", "holdings", "partners", "lp", "l.p.",
}


def company_in_content(company_name: str, text: str) -> bool:
    if not company_name or not text:
        return False
    name_lower = company_name.lower().strip()
    text_lower = text.lower()
    if name_lower in text_lower:
        return True
    words = [w for w in name_lower.split() if w not in _NAME_SUFFIXES and len(w) >= 3]
    if not words:
        return False
    if len(words) == 1:
        return bool(re.search(r"\b" + re.escape(words[0]) + r"\b", text_lower))
    if not all(w in text_lower for w in words):
        return False
    for a, b in zip(words, words[1:]):
        if re.search(re.escape(a) + r"\W+" + re.escape(b), text_lower):
            return True
    return False


_STOP_WORDS = {
    "about", "after", "being", "between", "could", "during", "every", "first",
    "their", "these", "those", "through", "under", "using", "which", "while",
    "would", "other", "there", "where", "should", "company", "business",
    "service", "services", "solution", "solutions", "based", "including",
    "across", "within",
}


def description_grounding(description: str, content: str) -> float:
    content_words = set(normalize_text(content).split())
    words = [w for w in normalize_text(description).split() if len(w) >= 5 and w not in _STOP_WORDS]
    if len(words) < 3:
        return 1.0
    return sum(1 for w in words if w in content_words) / len(words)


SIGNAL_WORDS = frozenset(
    {
        "launched", "announced", "expanded", "expanding", "partnered", "partnership",
        "merged", "acquisition", "acquired",
        "hired", "hiring", "recruited", "recruiting", "opening", "openings",
        "funding", "funded", "raised", "secured", "closed", "obtained", "invested",
        "investment", "seed", "series",
    }
)


def signal_word_grounding(text: str, content: str) -> tuple[int, int, list[str]]:
    content_words = set(normalize_text(content).split())
    words = set(normalize_text(text).split()) & SIGNAL_WORDS
    if not words:
        return 0, 0, []
    grounded = words & content_words
    return len(grounded), len(words), sorted(words - content_words)



_INVALID_URL_RE = re.compile(
    "|".join(
        [
            r"/alternatives(?:\b|/|\?|$)",
            r"/competitors(?:\b|/|\?|$)",
            r"indeed\.com/hire/job-description/",
            r"github\.com/[^/]+/[^/]+/labels(?:/|$)",
            r"github\.com/[^/]+/[^/]+/discussions/\d+(?:/|$)",
        ]
    ),
    re.IGNORECASE,
)


def url_structural_reason(url: str) -> Optional[str]:
    if not url:
        return None
    match = _INVALID_URL_RE.search(url)
    return f"URL path '{match.group()}' cannot carry intent evidence" if match else None


_ANTIBOT_RE = re.compile(
    "|".join(
        [
            r"access denied", r"verifying your connection", r"verifying.{0,30}browser",
            r"just a moment", r"enable javascript", r"please enable js",
            r"additional verification required", r"verifying you are human",
            r"sign in to (?:linkedin|see|join|view|continue)", r"join linkedin to",
            r"create an account to (?:see|join)", r"page can.?t be found",
            r"this page (?:doesn.?t|does not) exist", r"403\s*[-:|—]?\s*forbidden",
            r"404\s*[-:|—]?\s*(?:not\s*found|page.*not.*found)",
            r"this content isn.?t available",
        ]
    ),
    re.IGNORECASE,
)
_ANTIBOT_MAX_LEN = 4000


def antibot_reason(content: str) -> Optional[str]:
    if not content:
        return None
    match = _ANTIBOT_RE.search(content[:5000].lower())
    if match and len(content) < _ANTIBOT_MAX_LEN:
        return f"anti-bot / login wall ({match.group()[:40]!r})"
    return None


FRESHNESS_WINDOWS = {
    "in the last few weeks": 60, "in the last 30 days": 45, "in the last 60 days": 75,
    "in the last 90 days": 105, "in the last 6 months": 200, "in the last 12 months": 400,
    "in the past few weeks": 60, "in the past 30 days": 45, "in the past 60 days": 75,
    "in the past 90 days": 105, "in the past 6 months": 200, "in the past 12 months": 400,
    "last few weeks": 60, "last 30 days": 45, "last 60 days": 75, "last 90 days": 105,
    "last 6 months": 200, "last 12 months": 400, "past 30 days": 45, "past 60 days": 75,
    "past 90 days": 105, "past 6 months": 200, "past 12 months": 400, "recently": 180,
}


def claim_max_age_days(claim_text: str) -> Optional[int]:
    lowered = str(claim_text or "").lower()
    best: Optional[int] = None
    for phrase, days in FRESHNESS_WINDOWS.items():
        if phrase in lowered and (best is None or days < best):
            best = days
    return best


def parse_signal_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.year < 2000:
        return None
    return parsed.date()


def freshness_reason(claim_text: str, signal_date: Any, buyer_cap_days: Optional[int], today: date) -> Optional[str]:
    """Mirror of check_evidence_freshness with the Arena's deterministic cap."""

    max_age = buyer_cap_days if buyer_cap_days is not None else claim_max_age_days(claim_text)
    if max_age is None:
        return None
    parsed = parse_signal_date(signal_date)
    if parsed is None:
        return f"buyer requires evidence within {max_age} days but the signal has no valid date"
    age = (today - parsed).days
    if age > max_age:
        return f"evidence is {age} days old; cap is {max_age} days"
    return None


def future_date_reason(signal_date: Any, today: date) -> Optional[str]:
    parsed = parse_signal_date(signal_date)
    if parsed is not None and parsed > today:
        return f"signal date {parsed.isoformat()} is in the future"
    return None



FABRICATED_TLDS = frozenset(
    {"beauty", "auction", "mom", "blog", "site", "fun", "click", "sbs", "cyou",
     "rest", "icu", "top", "lol", "quest"}
)


def registrable_host(url: str) -> str:
    raw = str(url or "").strip().lower()
    if raw and "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlsplit(raw).hostname or "").rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def extract_domain(url: str) -> str:
    """lead_scorer.py::_extract_domain — the LAST TWO labels of the host.

    The scorer dedups a company's signals and exempts company-owned evidence
    by this key, so ``blog.acme.com`` and ``acme.com`` are the SAME domain to
    it (and ``acme.co.uk`` collapses to ``co.uk``, faithfully).
    """

    try:
        clean = str(url or "").strip()
        if not clean.lower().startswith(("http://", "https://")):
            clean = "https://" + clean
        hostname = (urlsplit(clean).hostname or "").lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        parts = hostname.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else hostname
    except Exception:
        return str(url or "").lower().strip()


def untrusted_source_reason(url: str, company_website: str) -> Optional[str]:
    dom = extract_domain(url)
    if not dom:
        return None
    co = extract_domain(company_website)
    if co and (dom == co or dom.endswith("." + co)):
        return None
    if co and "." in dom and "." in co and dom.split(".")[0] == co.split(".")[0]:
        return None
    if dom.endswith(".gov") or ".gov." in dom or dom.endswith(".edu") or ".edu." in dom:
        return None
    tld = dom.rsplit(".", 1)[-1]
    return f"fabricated-source TLD .{tld}" if tld in FABRICATED_TLDS else None


NEGATION_RE = re.compile(
    "|".join(
        [
            r"\b0\s+(open|available|current|listed|active)\b",
            r"\bno\s+(open|current|active|listed|available)\s+(position|opening|job|hire|role)",
            r"\bno\s+longer\s+(open|available|accepting|listed|active)\b",
            r"\bnot\s+(currently|available|accepting|open|listed|hiring)\b",
            r"\bjob\s+(no\s+longer|is\s+(no\s+longer|not)\s+(open|available))",
            r"\b(page|posting|position)\s+(not\s+found|no\s+longer\s+exists|expired|removed)\b",
            r"\bunable\s+to\s+(verify|find|access|locate)\b",
            r"\bno\s+evidence\b",
            r"\b404\b",
        ]
    ),
    re.IGNORECASE,
)


def negation_reason(description: str, snippet: str) -> Optional[str]:
    match = NEGATION_RE.search(" ".join(filter(None, [description or "", snippet or ""])))
    return f"self-contradicting evidence ({match.group(0)!r})" if match else None


SOURCE_MULTIPLIERS = {
    "linkedin": 1.0, "job_board": 1.0, "github": 1.0, "news": 0.9,
    "company_website": 0.85, "social_media": 0.8, "review_site": 0.75,
    "wikipedia": 0.6, "other": 0.3,
}


def evidence_source(url: str, company_website: str) -> str:
    """competition.py::_evidence_source — the scorer's source class from the URL."""

    hostname = (urlsplit(str(url)).hostname or "").lower().removeprefix("www.")
    company_hostname = (urlsplit(str(company_website)).hostname or "").lower().removeprefix("www.")
    path = (urlsplit(str(url)).path or "").lower()
    if hostname == "linkedin.com" or hostname.endswith(".linkedin.com"):
        return "linkedin"
    if hostname == "github.com" or hostname.endswith(".github.com"):
        return "github"
    if any(marker in path for marker in ("/jobs", "/job/", "/careers")):
        return "job_board"
    if company_hostname and (hostname == company_hostname or hostname.endswith("." + company_hostname)):
        return "company_website"
    return "news"


INTENT_CAP_BY_SIGNAL_COUNT = {1: 60.0, 2: 80.0, 3: 88.0, 4: 92.0, 5: 96.0, 6: 100.0}
MAX_FIT = 40.0


def intent_total(per_signal_scores: Iterable[float]) -> float:
    positives = sorted((s for s in per_signal_scores if s > 0), reverse=True)[:6]
    if not positives:
        return 0.0
    return min(sum(positives), INTENT_CAP_BY_SIGNAL_COUNT[len(positives)])


def company_score_estimate(fit_estimate: float, per_signal_scores: Iterable[float]) -> float:
    return max(0.0, min(100.0, min(fit_estimate, MAX_FIT) + intent_total(per_signal_scores)))



LINKEDIN_BUCKETS = ("0-1", "2-10", "11-50", "51-200", "201-500", "501-1,000",
                    "1,001-5,000", "5,001-10,000", "10,001+")
_LEGACY_BUCKETS = {
    "1-10": "2-10", "10-50": "11-50", "50-200": "51-200", "200-500": "201-500",
    "500-1000": "501-1,000", "501-1000": "501-1,000", "1000-5000": "1,001-5,000",
    "1001-5000": "1,001-5,000", "5000-10000": "5,001-10,000", "5001-10000": "5,001-10,000",
    "10000+": "10,001+", "10001+": "10,001+",
}
_OBSERVED_INTERVALS = ((1, "0-1"), (10, "2-10"), (50, "11-50"), (200, "51-200"), (500, "201-500"),
                       (1_000, "501-1,000"), (5_000, "1,001-5,000"), (10_000, "5,001-10,000"))


def normalize_bucket(value: Any) -> str:
    """LinkedIn bucket for a declared range string; '' when unrecognised."""

    if isinstance(value, (list, tuple, set)):
        for item in value:
            found = normalize_bucket(item)
            if found:
                return found
        return ""
    raw = " ".join(str(value or "").strip().split())
    if raw in LINKEDIN_BUCKETS:
        return raw
    cleaned = raw.lower().replace("employees", "").replace("employee", "").replace(",", "").replace(" ", "").strip()
    for bucket in LINKEDIN_BUCKETS:
        if cleaned == bucket.lower().replace(",", "").replace(" ", ""):
            return bucket
    return _LEGACY_BUCKETS.get(cleaned) or _LEGACY_BUCKETS.get(raw) or ""


def observed_bucket(value: Any) -> str:
    """LinkedIn bucket for an observed headcount (int or digit string)."""

    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value):
        if len(value) > 5:
            return "10,001+"
        count = int(value)
    else:
        return ""
    if count < 0:
        return ""
    for maximum, bucket in _OBSERVED_INTERVALS:
        if count <= maximum:
            return bucket
    return "10,001+"


def any_bucket(value: Any) -> str:
    """Declared range first, then observed headcount, then a 'min-max' or 'min+' string."""

    found = normalize_bucket(value) or observed_bucket(value)
    if found:
        return found
    text = str(value or "").replace(",", "").strip()
    match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", text)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return observed_bucket((low + high) // 2) if high >= low else ""
    match = re.fullmatch(r"(\d+)\s*\+", text)
    if match:
        return observed_bucket(int(match.group(1)) + 1)
    return ""


def icp_buckets(icp: Mapping[str, Any]) -> list[str]:
    raw = icp.get("employee_count")
    values = list(raw) if isinstance(raw, (list, tuple)) else str(raw or "").replace(";", "|").split("|")
    out: list[str] = []
    for value in values:
        bucket = normalize_bucket(value)
        if bucket and bucket not in out:
            out.append(bucket)
    return out



_SERIES_C_PLUS = frozenset({"series c+", "series c", "series d", "series e", "series f", "series g", "series h"})


def normalize_stage(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"any", "all", "unknown", "n/a", "na", "not specified"}:
        return ""
    if re.fullmatch(r"series\s*c\s*\+", text):
        return "series c+"
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def stage_matches(observed: str, requested: str) -> bool:
    return observed == requested or (requested == "series c+" and observed in _SERIES_C_PLUS)



_LEGAL_SUFFIXES = frozenset(
    {"incorporated", "corporation", "company", "limited", "holdings", "group", "inc",
     "corp", "co", "llc", "ltd", "plc", "gmbh", "ag", "sa", "nv", "bv", "oy", "ab",
     "as", "pty", "pte", "kk", "srl", "spa"}
)


def company_name_key(value: Any) -> str:
    words = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return "".join(words)


def canonical_domain(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or any(c.isspace() for c in raw):
        return ""
    if "://" not in raw and not raw.startswith("//"):
        raw = f"//{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.username or parsed.password:
        return ""
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not host or "." not in host or host.endswith(".") or ":" in host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def canonical_linkedin(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    if raw and "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = str(parsed.hostname or "").casefold().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.username is not None or parsed.password is not None
        or not (host == "linkedin.com" or host.endswith(".linkedin.com"))
        or len(parts) < 2 or parts[0].casefold() != "company"
    ):
        return ""
    slug = parts[1].casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._%+-]{0,99}", slug):
        return ""
    return f"https://www.linkedin.com/company/{slug}"



import unicodedata
from urllib.parse import unquote

_GATEWAY_LINKEDIN_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")
_GATEWAY_LINKEDIN_SLUG_RE_NO_DOTS = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")


def gateway_linkedin_slug(value: Any, *, allow_dots: bool = True) -> str:
    """The slug candidate_linkedin_prompt_slug would accept, or '' if it would raise."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    if any(c.isspace() for c in raw) or "\\" in raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = str(parsed.hostname or "").casefold()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    parts = [unquote(part).casefold() for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] not in {"company", "in"}:
        return ""
    pattern = _GATEWAY_LINKEDIN_SLUG_RE if allow_dots else _GATEWAY_LINKEDIN_SLUG_RE_NO_DOTS
    return parts[1] if pattern.fullmatch(parts[1]) else ""


INJECTION_PATTERNS = [
    re.compile(r"\b(?:ignore|disregard|forget|skip|bypass|override|nullify|cancel)\s+"
               r"(?:all\s+|any\s+|the\s+|every\s+|whatever\s+|what\s+(?:was\s+)?)?"
               r"(?:previous|prior|above|earlier|preceding|former|original|initial)\b", re.IGNORECASE),
    re.compile(r"\b(?:ignore|disregard)\s+(?:everything|all)\b", re.IGNORECASE),
    re.compile(r"\bforget\s+(?:everything|all|what|that)\b", re.IGNORECASE),
    re.compile(r"\b(?:new|updated?|revised?|fresh|different)\s+"
               r"(?:instructions?|task|prompt|rules?|directives?|orders?|guidelines?)\s*"
               r"(?:[:.]|are|is|to|that)", re.IGNORECASE),
    re.compile(r"<\|(?:im_(?:start|end)|endoftext|fim_[a-z]+|begin_of_text|end_of_text)\|>", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*(?:system|assistant|user)\s*[:>]", re.IGNORECASE),
    re.compile(r"\b(?:return|respond|reply|output|give|set|make|use|score|assign)\s+"
               r"(?:with\s+|this\s+|a\s+|the\s+)?(?:score|value|rating)?\s*"
               r"(?:of\s+|=\s*|:\s*|to\s+)?\s*(?:5\d|60)\b", re.IGNORECASE),
    re.compile(r"\bscore\s*[:=]\s*(?:5\d|60)\b", re.IGNORECASE),
    re.compile(r"\bmatched_icp_signal_idx\s*[:=]", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(?:a\s+)?(?:different|new)", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(?:a\s+)?(?:different|new)", re.IGNORECASE),
    re.compile(r"\bfollow\s+(?:these|the)\s+new\b", re.IGNORECASE),
]


def injection_match(text: str) -> Optional[str]:
    """The phrase _scan_for_prompt_injection would reject, or None."""

    if not text:
        return None
    for rx in INJECTION_PATTERNS:
        m = rx.search(text)
        if m:
            return m.group(0)[:60]
    return None


def strip_prompt_controls(text: str) -> str:
    """Remove Unicode control/format characters the gateway refuses outright."""

    return "".join(c for c in str(text or "") if not unicodedata.category(c).startswith("C") or c in "\n\t")


GATEWAY_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def contains_prompt_control(text: Any) -> bool:
    """True when the gateway would refuse this text outright."""

    return any(unicodedata.category(c) in GATEWAY_REJECTED_CATEGORIES for c in str(text or ""))


def strip_gateway_controls(text: Any) -> str:
    """Drop every character `validate_candidate_prompt_text` refuses.

    ⛳️ LAB-LOG #237.  A scraped page can carry SOFT HYPHENS (U+00AD, category
    Cf) inside words -- Endpoints News hyphenates that way -- and `best_window`
    re-cuts the snippet straight out of the page text, AFTER the one
    `strip_prompt_controls` pass, so they reached the wire.  Upstream ff17a8df
    now turns a company the judge model cannot parse into a zero breakdown
    (`failure_class model_contract_incompatible`, competition.py:307-311)
    instead of letting the ValidationError escape: no -10, but the breakdown is
    APPENDED, so the company occupies one of the five slots and scores nothing.
    Exactly one of the 133 companies we have ever emitted was rejected this way.

    A control that is also whitespace becomes a space, so removing it cannot
    weld two words together; everything else (the soft hyphen included) is simply
    dropped, which is what un-hyphenates ``sit\xadu\xada\xadtion`` back into
    ``situation``.  Callers collapse whitespace afterwards.
    """

    out = []
    for character in str(text or ""):
        if unicodedata.category(character) not in GATEWAY_REJECTED_CATEGORIES:
            out.append(character)
        elif character.isspace():
            out.append(" ")
    return "".join(out)



_COUNTRY_ALIASES = {
    "united states": "united states", "usa": "united states", "us": "united states",
    "u.s.": "united states", "u.s.a.": "united states", "united states of america": "united states",
    "america": "united states",
    "united kingdom": "united kingdom", "uk": "united kingdom", "u.k.": "united kingdom",
    "great britain": "united kingdom", "england": "united kingdom", "britain": "united kingdom",
    "canada": "canada", "australia": "australia", "germany": "germany", "deutschland": "germany",
    "france": "france", "netherlands": "netherlands", "the netherlands": "netherlands",
    "holland": "netherlands", "ireland": "ireland", "india": "india", "singapore": "singapore",
    "spain": "spain", "italy": "italy", "sweden": "sweden", "switzerland": "switzerland",
    "israel": "israel", "japan": "japan", "brazil": "brazil", "mexico": "mexico",
    "new zealand": "new zealand", "denmark": "denmark", "norway": "norway", "finland": "finland",
    "belgium": "belgium", "austria": "austria", "poland": "poland", "portugal": "portugal",
    "south korea": "south korea", "korea": "south korea", "china": "china", "hong kong": "hong kong",
    "united arab emirates": "united arab emirates", "uae": "united arab emirates",
}
_CONTINENTS = {"europe", "north america", "south america", "asia", "africa", "oceania",
               "apac", "emea", "latam", "eu", "european union", "worldwide", "global"}
_GEO_SPLIT = re.compile(r",|/|&|\bor\b|\band\b", re.IGNORECASE)
_US_STATE_HINTS = {
    "california", "new york", "texas", "florida", "washington", "massachusetts", "illinois",
    "colorado", "georgia", "virginia", "north carolina", "new jersey", "pennsylvania", "ohio",
    "arizona", "oregon", "utah", "michigan", "minnesota", "maryland", "tennessee", "nevada",
    "west coast", "east coast", "midwest", "northeast", "bay area", "silicon valley",
}


def normalize_country(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return _COUNTRY_ALIASES.get(text, text)


def allowed_countries(icp_geography: Any) -> tuple[frozenset[str], dict[str, str]]:
    """(allowed canonical countries, canonical -> the ICP's own token)."""

    allowed: dict[str, str] = {}
    deferred = False
    for raw in _GEO_SPLIT.split(str(icp_geography or "")):
        token = raw.strip()
        if not token:
            continue
        lowered = token.lower()
        if lowered in _CONTINENTS:
            deferred = True
            continue
        if lowered in _COUNTRY_ALIASES:
            allowed.setdefault(_COUNTRY_ALIASES[lowered], token)
        elif lowered in _US_STATE_HINTS:
            allowed.setdefault("united states", "United States")
    if not allowed and deferred:
        return frozenset(), {}
    return frozenset(allowed), allowed




def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("intent_signal") or value.get("signal") or value.get("text") or "").strip()
    return str(value or "").strip()


def _category(value: Any) -> Optional[str]:
    if not isinstance(value, Mapping):
        return None
    text = str(value.get("intent_category") or value.get("category") or value.get("evidence_type") or "").strip().upper()
    return text or None


def icp_signals(icp: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Ordered [{text, category, max_age_days}] exactly as the scorer indexes them."""

    primary_category = str(icp.get("intent_category") or "").strip().upper() or None
    bonus = {_text(i): _category(i) for i in (icp.get("bonus_intents") or []) if isinstance(i, Mapping) and _text(i)}
    raw = icp.get("intent_signals") or [icp.get("intent_signal")]
    if isinstance(raw, (str, Mapping)):
        raw = [raw]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw or []):
        text = _text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        category = _category(item) or bonus.get(text) or (primary_category if index == 0 else None)
        max_age = None
        if isinstance(item, Mapping):
            raw_age = item.get("intent_max_age_days", item.get("max_age_days"))
            if isinstance(raw_age, int) and not isinstance(raw_age, bool):
                max_age = raw_age
        out.append({"text": text, "category": category, "max_age_days": max_age})
    for item in icp.get("bonus_intents") or []:
        text = _text(item)
        if text and text not in seen:
            seen.add(text)
            raw_age = item.get("intent_max_age_days", item.get("max_age_days")) if isinstance(item, Mapping) else None
            out.append({"text": text, "category": _category(item),
                        "max_age_days": raw_age if isinstance(raw_age, int) and not isinstance(raw_age, bool) else None})
        elif text in seen:
            for row in out:
                if row["text"] == text and row["max_age_days"] is None and isinstance(item, Mapping):
                    raw_age = item.get("intent_max_age_days", item.get("max_age_days"))
                    if isinstance(raw_age, int) and not isinstance(raw_age, bool):
                        row["max_age_days"] = raw_age
                    if row["category"] is None:
                        row["category"] = _category(item)
    if not out:
        for item in icp.get("required_intents") or []:
            text = _text(item)
            if text and text not in seen:
                seen.add(text)
                out.append({"text": text, "category": _category(item), "max_age_days": (item or {}).get("max_age_days")})
    return out


def icp_max_age_days(icp: Mapping[str, Any]) -> int:
    try:
        return max(1, int(icp.get("intent_max_age_days") or 365))
    except (TypeError, ValueError):
        return 365


def icp_company_goal(icp: Mapping[str, Any]) -> int:
    try:
        return max(1, min(5, int(icp.get("max_companies", 5))))
    except (TypeError, ValueError):
        return 5


def evaluation_date() -> date:
    import os

    for name in ("LAB_ARENA_EVALUATION_DATE", "LEADPOET_COMPETITION_EVALUATION_DATE", "BAKEOFF_EVALUATION_DATE"):
        raw = str(os.environ.get(name) or "").strip()
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                continue
    return datetime.now(timezone.utc).date()




def public_http_url(value: Any, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("must be an absolute public HTTP URL")
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
        parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("must be a public URL") from exc
    if hostname == "localhost" or hostname.endswith((".internal", ".invalid", ".local", ".localhost", ".onion", ".test")):
        raise ValueError("must be a public URL")
    try:
        address = ipaddress.ip_address(ascii_hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("must be a public URL")
    if address is None:
        labels = ascii_hostname.split(".")
        if len(labels) < 2 or not any(c.isalpha() for c in labels[-1]):
            raise ValueError("must be a public URL")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


GATEWAY_DESCRIPTION_MAX = 350
GATEWAY_SNIPPET_MAX = 600
GATEWAY_NAME_MAX = 200
GATEWAY_WHY_NOW_MAX = 600
GATEWAY_CLAIM_TEXT_MAX = 2000


def _reject_prompt_controls(value: str, field_name: str) -> str:
    """gateway/qualification/models.py:244-252 `validate_candidate_prompt_text`.

    LAB-LOG #237: the platform raises here, the ValidationError is caught at
    competition.py:309, and the company becomes a zero breakdown that still
    spends one of the five slots.  Catching it locally is the difference between
    one company repaired before we submit and one slot silently forfeited.
    """

    if contains_prompt_control(value):
        raise ValueError("%s contains control or format characters" % field_name)
    return value


class IntentSignalOut(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    matched_icp_signal: int = Field(ge=0)
    description: str = Field(min_length=1, max_length=GATEWAY_DESCRIPTION_MAX)
    date: date
    why_now: str = Field(min_length=1, max_length=GATEWAY_WHY_NOW_MAX)
    url: str
    snippet: str = Field(min_length=1, max_length=GATEWAY_SNIPPET_MAX)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return public_http_url(value)

    @field_validator("description")
    @classmethod
    def _description_controls(cls, value: str) -> str:
        return _reject_prompt_controls(value, "description")

    @field_validator("snippet")
    @classmethod
    def _snippet_controls(cls, value: str) -> str:
        return _reject_prompt_controls(value, "snippet")


class RequiredAttributeOut(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=GATEWAY_CLAIM_TEXT_MAX)
    passed: bool
    evidence_url: str
    evidence_quote: str = Field(min_length=1, max_length=GATEWAY_CLAIM_TEXT_MAX)
    explanation: str = Field(min_length=1, max_length=GATEWAY_CLAIM_TEXT_MAX)

    @field_validator("evidence_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return public_http_url(value)


class CompanyOut(BaseModel):
    """Exactly the platform's CompetitionCompany, plus the gateway length caps."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    company_name: str = Field(min_length=1, max_length=GATEWAY_NAME_MAX)
    company_website: str
    company_linkedin: str = ""
    industry: str
    employee_count: str
    company_stage: str = ""
    country: str
    state: str = ""
    fit_summary: str = Field(min_length=1, max_length=500)
    fit_evidence_urls: list[str]
    intent_signals: list[IntentSignalOut] = Field(min_length=1)
    required_attribute: Optional[RequiredAttributeOut] = None

    @field_validator("company_name")
    @classmethod
    def _name_controls(cls, value: str) -> str:
        return _reject_prompt_controls(value, "company_name")

    @field_validator("company_website")
    @classmethod
    def _website(cls, value: str) -> str:
        return public_http_url(value)

    @field_validator("company_linkedin")
    @classmethod
    def _linkedin(cls, value: str) -> str:
        return public_http_url(value, allow_empty=True)

    @field_validator("fit_evidence_urls")
    @classmethod
    def _fit_urls(cls, values: list[str]) -> list[str]:
        return [public_http_url(v) for v in values]


SCHEMA_V1 = "leadpoet.lab_arena.output.v1"
_DateType = date
SCHEMA_V5 = "leadpoet.lab_arena.output.v5"
SCHEMA_V6 = "leadpoet.lab_arena.output.v6"


def v5_family(schema_version) -> bool:
    return str(schema_version or "").endswith((".v5", ".v6"))
INTENT_DETAILS_MAX = 2000
STAGE_EVIDENCE_MAX = 3


def validate_intent_details_text(value: Any) -> str:
    """qualification/intent_details.py: one plain prose paragraph, <= 2000 chars."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("intent_details must be a non-empty paragraph")
    if len(value) > INTENT_DETAILS_MAX:
        raise ValueError("intent_details exceeds 2000 characters")
    if re.search(r"\n[ \t\r]*\n", value):
        raise ValueError("intent_details must be one paragraph")
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} and ch not in "\r\n\t" for ch in value):
        raise ValueError("intent_details contains unsupported control characters")
    if re.search(r"(?:^|\n)\s*(?:#{1,6}\s|[-*\u2022]\s|\d+[.)]\s|>)", value):
        raise ValueError("intent_details must be prose, not headings or a list")
    if "```" in value:
        raise ValueError("intent_details must be prose, not a code block")
    return " ".join(value.split())


class IntentSignalOutV5(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    matched_icp_signal: int = Field(ge=0)
    description: str = Field(min_length=1, max_length=GATEWAY_DESCRIPTION_MAX)
    date: Optional[_DateType] = None
    url: str

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return public_http_url(value)

    @field_validator("description")
    @classmethod
    def _description_controls(cls, value: str) -> str:
        return _reject_prompt_controls(value, "description")


class StageEvidenceOut(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    url: str = Field(max_length=2048)
    quote: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return public_http_url(value)


class CompanyOutV5(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    company_name: str = Field(min_length=1, max_length=GATEWAY_NAME_MAX)
    company_website: str
    company_linkedin: Any = None
    industry: str
    employee_count: str
    company_stage: str = ""
    country: str
    state: Any = None
    intent_details: str = Field(min_length=1, max_length=INTENT_DETAILS_MAX)
    intent_signals: list[IntentSignalOutV5] = Field(min_length=1)
    company_stage_evidence: list[StageEvidenceOut] = Field(default_factory=list, max_length=STAGE_EVIDENCE_MAX)
    required_attribute: Optional[RequiredAttributeOut] = None
    contact: Any = None

    @field_validator("company_name")
    @classmethod
    def _name_controls(cls, value: str) -> str:
        return _reject_prompt_controls(value, "company_name")

    @field_validator("company_website")
    @classmethod
    def _website(cls, value: str) -> str:
        return public_http_url(value)

    @field_validator("intent_details")
    @classmethod
    def _details(cls, value: str) -> str:
        return validate_intent_details_text(value)


def validate_output(companies: Any, *, max_companies: int, schema_version: str = SCHEMA_V1) -> list[dict[str, Any]]:
    if isinstance(companies, Mapping) and "companies" in companies:
        companies = companies["companies"]
    if not isinstance(companies, list):
        raise ValueError("companies must be a list")
    if len(companies) > int(max_companies):
        raise ValueError("too many companies")
    model = CompanyOutV5 if v5_family(schema_version) else CompanyOut
    v6 = str(schema_version or "").endswith(".v6")
    if v6:
        companies = [{k: v for k, v in c.items() if k != "contact"} if isinstance(c, Mapping) else c for c in companies]
    rows = [model.model_validate(c).model_dump(mode="json") for c in companies]
    if v6:
        for row in rows:
            row.pop("contact", None)
    return rows


__all__ = [name for name in dir() if not name.startswith("__")]
