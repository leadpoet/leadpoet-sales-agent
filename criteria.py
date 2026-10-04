"""The ICP's intent criteria as the judge indexes them, with per-criterion windows and URL rules."""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from . import gates
from . import scorer_mirror as sm

_JOB_PATH_RE = re.compile(r"/(?:jobs?|careers?)(?:/|$)|[?&]gh_jid=", re.I)


def signals(icp: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sm.icp_signals(icp)


def count(icp: Mapping[str, Any]) -> int:
    return max(1, len(signals(icp)))


def category(icp: Mapping[str, Any], index: int) -> str:
    rows = signals(icp)
    if 0 <= index < len(rows):
        value = str(rows[index].get("category") or "").upper()
        if value:
            return value
    return str(icp.get("intent_category") or "").upper() if index == 0 else ""


def is_hiring(icp: Mapping[str, Any], index: int) -> bool:
    if category(icp, index) in ("HIRING", "JOBS"):
        return True
    rows = signals(icp)
    text = str(rows[index].get("text") or "") if 0 <= index < len(rows) else ""
    return bool(re.search(r"\bhiring\b|job postings?|open roles?|careers page", text, re.I))


def max_age(icp: Mapping[str, Any], index: int) -> int:
    rows = signals(icp)
    row = rows[index] if 0 <= index < len(rows) else {}
    days = row.get("max_age_days") or icp.get("intent_max_age_days") or 365
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = 365
    claim = gates.claim_max_age(row.get("text"))
    if claim is None:
        claim = sm.claim_max_age_days(str(row.get("text") or ""))
    return min(days, int(claim)) if claim else days


def window(icp: Mapping[str, Any], index: int) -> int:
    """Usable age in days: the criterion window minus a margin (3 days up to 90-day windows, else 7)."""

    days = max_age(icp, index)
    return max(1, days - (3 if days <= 120 else 7))


def age_days(date: Any, today: Optional[_dt.date] = None) -> Optional[int]:
    try:
        day = _dt.date.fromisoformat(str(date or "")[:10])
    except ValueError:
        return None
    return ((today or sm.evaluation_date()) - day).days


def in_window(icp: Mapping[str, Any], index: int, date: Any) -> bool:
    age = age_days(date)
    return age is not None and 0 <= age <= window(icp, index)


def job_shaped(url: Any, website: Any = "") -> bool:
    """A URL the judge treats as a job page: its job-board hosts, or a /jobs, /job/ or /careers path."""

    text = str(url or "")
    board = gates.is_job_board_url(text)
    if board:
        return True
    source = gates.evidence_source(text, website)
    if source == "job_board":
        return True
    try:
        path = urlsplit(text).path or ""
        query = urlsplit(text).query or ""
    except ValueError:
        return False
    return bool(_JOB_PATH_RE.search(path) or _JOB_PATH_RE.search("?" + query))


def url_points(url: Any, website: Any) -> int:
    """The judge's raw points for one verified row by source class (60 job page / 54 news / 51 own site)."""

    source = gates.evidence_source(url, website) or sm.evidence_source(str(url or ""), str(website or ""))
    return {"job_board": 60, "linkedin": 60, "github": 60, "company_website": 51}.get(source, 54)


def url_admissible(icp: Mapping[str, Any], index: int, url: Any, website: Any) -> str:
    """'' when the URL may be cited for this criterion, else why not."""

    text = str(url or "")
    if not text.startswith("https://"):
        return "not https"
    untrusted = gates.untrusted_source(text, website)
    if untrusted:
        return untrusted
    if untrusted is None and sm.untrusted_source_reason(text, str(website or "")):
        return "untrusted source"
    if not is_hiring(icp, index) and job_shaped(text, website):
        return "job-shaped URL on a non-hiring criterion"
    if is_hiring(icp, index) and not (job_shaped(text, website) or posting_url(text)):
        return "a hiring criterion needs a job posting or careers page"
    return ""


def posting_url(url: Any) -> bool:
    """A job posting on an applicant-tracking host or a careers/jobs path (wider than job_shaped, which mirrors the
    judge's job-board URL classes)."""

    from .intent_details import is_posting_url
    return is_posting_url(url)


__all__ = ["signals", "count", "category", "is_hiring", "max_age", "window", "age_days", "in_window", "job_shaped",
           "posting_url",
           "url_points", "url_admissible"]
