"""Pure validation and projection for public LinkedIn company profile evidence."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


_CANONICAL_EMPLOYEE_BANDS = (
    "0-1",
    "2-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1,000",
    "1,001-5,000",
    "5,001-10,000",
    "10,001+",
)
_LINKEDIN_COMPANY_PATH_RE = re.compile(
    r"^/company/(?P<slug>[A-Za-z0-9][A-Za-z0-9._~-]{0,99})/?$"
)
_ABOUT_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*{1,2})?about(?: us)?(?:\*{1,2})?[ \t]*$"
)
_ABOUT_END_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*{1,2})?"
    r"(?:employees(?: at\b[^\r\n]*)?|updates)(?:\*{1,2})?[ \t]*$"
)
_BAND_PATTERN = "|".join(
    re.escape(value) for value in sorted(_CANONICAL_EMPLOYEE_BANDS, key=len, reverse=True)
)
_COMPANY_SIZE_RE = re.compile(
    rf"(?im)^[ \t]*(?:\*{{1,2}})?company size(?:\*{{1,2}})?[ \t]*"
    rf"(?::[ \t]*(?:\r?\n[ \t]*)*|[ \t]+|(?:[ \t]*\r?\n)+[ \t]*)"
    rf"(?P<band>{_BAND_PATTERN})"
    rf"(?:[ \t]+employees?)?[ \t]*$"
)
_MAX_TITLE_CHARS = 300
_MAX_HEADQUARTERS_CHARS = 300
_ABOUT_FIELD_LABELS = (
    "company size",
    "founded",
    "headquarters",
    "industry",
    "locations",
    "specialties",
    "type",
    "website",
)
_ABOUT_FIELD_PATTERN = "|".join(
    re.escape(value) for value in sorted(_ABOUT_FIELD_LABELS, key=len, reverse=True)
)
_HEADQUARTERS_RE = re.compile(
    rf"(?im)^[ \t]*(?:\*{{1,2}})?headquarters(?:\*{{1,2}})?[ \t]*"
    rf"(?::[ \t]*(?:\r?\n[ \t]*)*|[ \t]+|(?:[ \t]*\r?\n)+[ \t]*)"
    rf"(?P<value>[^\r\n]{{1,{_MAX_HEADQUARTERS_CHARS}}}?)[ \t]*"
    rf"(?=(?:[ \t]+(?:{_ABOUT_FIELD_PATTERN})(?:[ \t:]+|$))|$)"
)


def linkedin_company_profile_url(value: Any) -> str | None:
    """Return a validated public LinkedIn company profile URL, or None if absent."""

    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("stored LinkedIn profile URL is invalid")
    if value != value.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError("stored LinkedIn profile URL is invalid")
    url = value
    if re.match(r"(?i)^(?:www\.)?linkedin\.com/", url):
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("stored LinkedIn profile URL is invalid") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or host not in {"linkedin.com", "www.linkedin.com"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or _LINKEDIN_COMPANY_PATH_RE.fullmatch(parsed.path) is None
    ):
        raise ValueError("stored LinkedIn profile URL is invalid")
    return url


def exa_reported_error(payload: Any, *, depth: int = 0) -> bool:
    """Detect failed Exa replies that arrive inside a successful transport response."""

    if not isinstance(payload, dict) or depth > 5:
        return False
    if payload.get("error") not in (None, "", False, [], {}):
        return True
    if payload.get("errors") not in (None, "", False, [], {}):
        return True
    if str(payload.get("status") or "").casefold() in {
        "error",
        "failed",
        "failure",
    }:
        return True
    for key in ("toolResponse", "result", "data", "raw", "rawV2"):
        if exa_reported_error(payload.get(key), depth=depth + 1):
            return True
    for key in ("statuses", "results"):
        items = payload.get(key)
        if isinstance(items, list) and any(
            exa_reported_error(item, depth=depth + 1) for item in items[:100]
        ):
            return True
    return False


def _profile_key(value: Any) -> tuple[str, str] | None:
    try:
        url = linkedin_company_profile_url(value)
    except ValueError:
        return None
    if url is None:
        return None
    parsed = urlsplit(url)
    match = _LINKEDIN_COMPANY_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return ("linkedin.com", match.group("slug").casefold())


def _about_section(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    about = _ABOUT_HEADING_RE.search(text)
    if about is None:
        return None
    end = _ABOUT_END_RE.search(text, about.end())
    section_end = end.start() if end is not None else len(text)
    return text[about.end() : section_end]


def _company_size_from_about(text: Any) -> tuple[str, str] | None:
    section = _about_section(text)
    if section is None:
        return None
    match = _COMPANY_SIZE_RE.search(section)
    if match is None:
        return None
    return match.group("band"), match.group(0)


def _headquarters_from_about(text: Any) -> tuple[str, str] | None:
    section = _about_section(text)
    if section is None:
        return None
    match = _HEADQUARTERS_RE.search(section)
    if match is None:
        return None
    value = match.group("value").strip()
    normalized_value = value.casefold()
    if (
        not value
        or len(value) > _MAX_HEADQUARTERS_CHARS
        or any(
            normalized_value == label
            or normalized_value.startswith(label + " ")
            or normalized_value.startswith(label + ":")
            for label in _ABOUT_FIELD_LABELS
        )
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value, match.group(0).strip()


def project_linkedin_profile_evidence(
    requested_url: str, result: Any
) -> dict[str, str]:
    """Project an explicit LinkedIn About-section company-size label."""

    if not isinstance(result, dict):
        raise ValueError("LinkedIn profile result is missing")
    result_url = result.get("url")
    requested_key = _profile_key(requested_url)
    if requested_key is None or _profile_key(result_url) != requested_key:
        raise ValueError("LinkedIn profile result URL does not match the request")
    extracted = _company_size_from_about(result.get("text"))
    if extracted is None:
        raise ValueError("explicit LinkedIn Company size band is missing")
    employee_count, quote = extracted
    title = result.get("title")
    evidence = {
        "url": str(result_url),
        "title": str(title).strip()[:_MAX_TITLE_CHARS]
        if isinstance(title, str)
        else "",
        "employee_count": employee_count,
        "quote": quote,
    }
    headquarters = _headquarters_from_about(result.get("text"))
    if headquarters is not None:
        evidence["listed_headquarters"], evidence["headquarters_quote"] = headquarters
    return evidence


__all__ = [
    "exa_reported_error",
    "linkedin_company_profile_url",
    "project_linkedin_profile_evidence",
]
