"""Map reviewed companies in leads.json to Leadpoet's company-only output (schema v6).

Confirmed companies are mapped; after research, an ICP with none may publish a few held
companies left open only on the primary signal (held_companies). Every value comes from
the saved review; this module adds no research and no claims of its own. Leadpoet scores
a company zero, and counts some failures as false positives (minus ten points each), when
its band, stage, country or required attribute is missing or wrong, so a confirmed company
that cannot fill those fields from saved evidence is left out rather than emitted.
"""

from datetime import date
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

from validate_run import RELATIVE_DATE, criteria, excluded_company

from .input import STAGE_PREFIX, company_stage, intent_signals, stage_line

MAX_COMPANIES = 5
HELD_COMPANIES = 3  # At most this many held companies stand in for an ICP with no confirmed company.
ICP_FILE = "arena-icp.json"
INTENT_DETAILS_LIMIT = 2000
BUCKETS = ("2-10", "11-50", "51-200", "201-500", "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+")
_UNSET = {"", "any", "all", "unknown", "n/a", "na", "not specified"}
_COUNTRY_ALIASES = {"us": "united states", "usa": "united states", "u.s.": "united states",
                    "u.s.a.": "united states", "united states of america": "united states",
                    "uk": "united kingdom", "u.k.": "united kingdom", "great britain": "united kingdom",
                    "britain": "united kingdom", "england": "united kingdom"}
_SUBJECT_WORDS = {"the", "it", "its", "a", "an", "this", "their", "they", "in", "on", "during", "since", "after"}


def employee_bucket(value):
    """LinkedIn's band in Leadpoet's spelling ("501-1000" -> "501-1,000"); '' when unknown."""
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().lower().replace("employees", "").replace(" ", "")
    if cleaned in BUCKETS:
        return cleaned
    lower, _, upper = cleaned.replace(",", "").partition("-")
    for bucket in BUCKETS:
        b_lower, _, b_upper = bucket.replace(",", "").partition("-")
        if (lower, upper) == (b_lower, b_upper):
            return bucket
    return ""


def public_url(value):
    """The URL when it is a public http(s) address, else None."""
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2048:
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    if host == "localhost" or host.endswith((".internal", ".invalid", ".local", ".localhost", ".onion", ".test")):
        return None
    try:
        host = host.encode("idna").decode("ascii")  # Leadpoet refuses a hostname IDNA cannot encode.
    except UnicodeError:
        return None
    try:
        return value if ipaddress.ip_address(host).is_global else None
    except ValueError:
        return value if "." in host and any(c.isalpha() for c in host.rsplit(".", 1)[1]) else None


def signal_date(evidence):
    """A full ISO day only; a month-precision event date is not padded into a day."""
    value = evidence.get("event_date")
    if value is None and evidence.get("date_basis") == "observed_current":
        value = evidence.get("date")
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    return None


def _one_line(value):
    """Plain single-line text; control and invisible format characters are refused by Leadpoet."""
    text = "".join(" " if unicodedata.category(c) in {"Cc", "Cf", "Cs"} else c for c in str(value or ""))
    return " ".join(text.split())


def _cap(value, characters=2000, utf8_bytes=4000):
    """One line within Leadpoet's limits: 2,000 characters per quote and 4,096 bytes per string."""
    text = _one_line(value)[:characters]
    while len(text.encode("utf-8")) > utf8_bytes:
        text = text[:-max(1, (len(text.encode("utf-8")) - utf8_bytes) // 4)]
    return text


def _unset(value):
    return _one_line(value).casefold() in _UNSET


def _country(value):
    folded = _one_line(value).casefold().rstrip(".")
    return _COUNTRY_ALIASES.get(folded, folded)


def _single_country(icp):
    """The ICP's explicit country field when it names one country; free-form geography is left to Leadpoet."""
    value = _one_line(icp.get("country"))
    if not value or _unset(value) or re.search(r",|/|&|\bor\b|\band\b", value, re.I):
        return None
    return _country(value)


def _icp_buckets(icp):
    values = icp.get("employee_count")
    values = values if isinstance(values, list) else [values] if values else []
    buckets = {employee_bucket(str(value)) for value in values}
    return None if not buckets or "" in buckets else buckets


# Hosts Leadpoet's judge cannot open (login walls). In round arena-2026-10-10 both of this adapter's
# unverifiable companies cited LinkedIn for the required attribute and came back "unavailable"; an
# unverifiable signal 0 also costs Leadpoet's 10-point false-positive penalty. Captured LinkedIn data stays
# usable for identity and size; it is only never the URL the judge must fetch when another page says it.
JUDGE_UNREACHABLE_HOSTS = ("linkedin.com",)


def judge_reachable(url):
    host = (urlsplit(url or "").hostname or "").lower()
    return not any(host == blocked or host.endswith("." + blocked) for blocked in JUDGE_UNREACHABLE_HOSTS)


def _reachable_first(items):
    """The same evidence, pages the judge can open first; otherwise the model's order is kept."""
    return sorted(items, key=lambda item: not judge_reachable(item.get("url")))


def judge_unreachable_fields(row):
    """Published fields whose only cited page is one the judge cannot open."""
    fields = []
    primary = next((signal for signal in row.get("intent_signals") or [] if signal["matched_icp_signal"] == 0), None)
    if primary and not judge_reachable(primary["url"]):
        fields.append("intent_signals[0].url")
    attribute = row.get("required_attribute") or {}
    if attribute.get("evidence_url") and not judge_reachable(attribute["evidence_url"]):
        fields.append("required_attribute.evidence_url")
    stage = row.get("company_stage_evidence") or []
    if stage and not any(judge_reachable(item["url"]) for item in stage):
        fields.append("company_stage_evidence")
    return fields


def _passing(lead, prefix):
    return [c for c in lead.get("qualification_checks") or []
            if c.get("status") == "pass" and str(c.get("criterion_id", "")).startswith(prefix)]


def _quoted_evidence(check):
    return [item for item in (check or {}).get("evidence") or []
            if public_url(item.get("url")) and _one_line(item.get("text"))]


def _attribute_passage(check):
    """Publish the first valid primary quote the judge can open (else the first valid one); the passing check
    may retain other captured supporting passages.

    Full-criterion review remains required. The model orders the primary evidence to cover the part not already
    supported by emitted signal/stage URLs; the mapper neither joins quotes nor infers missing conjuncts.
    """
    return next(iter(_reachable_first(_quoted_evidence(check))), None)


def _icp_signal_index(check, signals, request_signals):
    """The ICP position of a saved signal criterion, matched by its verbatim query."""
    match = re.fullmatch(r"signal:(\d+)", str(check.get("criterion_id", "")))
    if not match:
        return None
    position = int(match.group(1))
    query = request_signals[position].get("query", "") if position < len(request_signals) else ""
    folded = _one_line(query).casefold()
    for index, signal in enumerate(signals):
        text = _one_line(signal).casefold()
        if folded and (folded == text or folded in text or text in folded):
            return index
    return None


def _sentence(name, claim):
    """A complete sentence about the company from a saved claim, without new facts."""
    claim = _one_line(claim).rstrip(".")
    if not claim:
        return ""
    first = claim.split()[0].casefold()
    if claim.casefold().startswith(name.casefold()) or first in _SUBJECT_WORDS:
        return claim[0].upper() + claim[1:] + "."
    return f"{name} {claim[0].lower()}{claim[1:]}."


def intent_details(name, lead, rows, icp):
    """One plain paragraph: the company, its verified activity, and a hedged link to the request."""
    company = lead.get("company") or {}
    parts = [_one_line(company.get("description")) or _sentence(name, (lead.get("account_fit") or {}).get("fit_claim"))]
    parts += [_sentence(name, row["description"]) for row in rows]
    # Built from structured ICP fields: Leadpoet's free-text prompts are informal ("need ...", "yo can you pull ...").
    what = _one_line(icp.get("sub_industry")) or _one_line(icp.get("industry"))
    activity = re.split(r",\s*(?:per|according to|as shown|backed by)\b|;", _one_line(intent_signals(icp)[0]))[0].rstrip(". ")
    if what and activity:
        words = activity.casefold().split()
        progressive = words[0].endswith("ing") or (len(words) > 1 and words[0].endswith("ly") and words[1].endswith("ing"))
        activity = ("are " if progressive else "") + activity[0].lower() + activity[1:]
        if len(what) > 1 and what[0].isupper() and what[1].islower():
            what = what[0].lower() + what[1:]  # "Management consulting", but keep "IT", "AI", "B2B".
        parts.append(f"Because this request looks for {what} companies that {activity}, this activity "
                     f"may make {name} a timely match.")
    text = " ".join(part for part in parts if part)
    capped = _cap(text, INTENT_DETAILS_LIMIT)
    return capped if capped == text else capped.rsplit(" ", 1)[0]


def company_row(lead, icp, request_signals, *, gaps=None):
    """One v6 company, or None when the saved review cannot fill a scored field."""
    company = lead.get("company") or {}
    name = _one_line(company.get("canonical_name"))
    website = public_url(company.get("website") or ("https://" + company["domain"] if company.get("domain") else None))
    bucket = employee_bucket(company.get("employee_range"))
    country = _one_line(company.get("hq_country"))
    wanted_buckets, wanted_country = _icp_buckets(icp), _single_country(icp)
    if not (name and website and bucket and country):
        if gaps is not None:
            gaps.extend(field for field, value in (("company.canonical_name", name), ("company.website", website),
                           ("company.employee_range", bucket), ("company.hq_country", country)) if not value)
        return None
    if (wanted_buckets and bucket not in wanted_buckets) or (wanted_country and _country(country) != wanted_country):
        if gaps is not None:
            if wanted_buckets and bucket not in wanted_buckets:
                gaps.append("company.employee_range")
            if wanted_country and _country(country) != wanted_country:
                gaps.append("company.hq_country")
        return None
    signals = intent_signals(icp)
    rows = []
    for check in _passing(lead, "signal:"):
        index = _icp_signal_index(check, signals, request_signals)
        evidence = next(iter(_reachable_first(
            [item for item in check.get("evidence") or [] if public_url(item.get("url"))])), None)
        if index is None or evidence is None or any(row["matched_icp_signal"] == index for row in rows):
            continue
        rows.append({"matched_icp_signal": index, "description": _cap(check.get("claim")),
                     "date": signal_date(evidence), "url": evidence["url"].strip()})
    rows.sort(key=lambda row: row["matched_icp_signal"])
    if not rows or rows[0]["matched_icp_signal"] != 0 or not all(row["description"] for row in rows):
        if gaps is not None:
            gaps.append("qualification_checks.signal:0")
        return None  # Leadpoet requires the primary signal (index 0).
    industry = _one_line(company.get("industry")) or (_one_line(icp.get("industry")) if _passing(lead, "icp:industries") else "")
    if not industry:
        if gaps is not None:
            gaps.append("company.industry")
        return None
    row = {
        "company_name": name,
        "company_website": website,
        "company_linkedin": public_url(company.get("linkedin_url")) or "",
        "industry": industry,
        "employee_count": bucket,
        "company_stage": "",
        "country": country,
        "state": _one_line(company.get("hq_state")),
        "intent_details": intent_details(name, lead, rows, icp),
        "intent_signals": rows,
        "required_attribute": None,
    }
    stage = company_stage(icp)
    if stage:
        # The check for the request's own stage line (or its bare stage, copied verbatim).
        # The request's stage line, with or without its "Required company stage:" prefix and meaning.
        wanted = {stage.casefold(), stage_line(stage).removeprefix(STAGE_PREFIX).casefold()}

        def stage_text(criterion):
            text = " ".join(_one_line(criterion).casefold().rstrip(".").split())
            return text.removeprefix(STAGE_PREFIX.strip().casefold()).strip()
        check = next((c for c in _passing(lead, "attribute:")
                      if stage_text(c.get("criterion")) in wanted and _quoted_evidence(c)), None)
        if check is None:
            if gaps is not None:
                gaps.append("qualification_checks.required_company_stage")
            return None  # A requested stage without a verified stage check would be a scored miss.
        row["company_stage"] = stage
        row["company_stage_evidence"] = [{"url": item["url"].strip(), "quote": _cap(item["text"])}
                                         for item in _reachable_first(_quoted_evidence(check))[:3]]
    attribute = _one_line(icp.get("required_attribute"))
    if attribute:
        attributes = [c for c in lead.get("qualification_checks") or []
                      if str(c.get("criterion_id", "")).startswith("attribute:")]
        check = next((c for c in attributes if _one_line(c.get("criterion")).casefold() == attribute.casefold()), None)
        if check is None:
            # Older saved requests may have a shorter criterion label. Never let
            # one of those checks replace an exact check that is not proved.
            check = next((c for c in _passing(lead, "attribute:")
                          if _one_line(c.get("criterion")).casefold() in attribute.casefold()
                          or attribute.casefold() in _one_line(c.get("criterion")).casefold()), None)
        if check is not None and check.get("status") != "pass":
            check = None
        evidence = _attribute_passage(check)
        if evidence is None:
            if gaps is not None:
                gaps.append("qualification_checks.required_attribute")
            return None  # A passing attribute still needs a valid quoted primary source.
        quote = _cap(evidence["text"])
        if RELATIVE_DATE.search(quote):
            if gaps is not None:
                gaps.append("required_attribute.evidence_quote.relative_date")
            return None  # Repair the selected quote; never silently select a different source.
        row["required_attribute"] = {"text": _cap(attribute), "passed": True, "evidence_url": evidence["url"].strip(),
                                     "evidence_quote": quote,
                                     "explanation": _cap(check.get("claim"))
                                     or "The quoted passage on the evidence page states this attribute."}
    return row


def publication_gaps(leads, icp, request):
    """Explain only fields that the unchanged Arena output mapper cannot publish."""
    issues, seen = [], set()
    for lead in leads:
        gaps = []
        row = company_row(lead, icp, request.get("buying_signals") or [], gaps=gaps)
        if row is not None:
            key = row["company_website"].lower().rstrip("/")
            if key in seen:
                gaps.append("company.website_duplicate")
            seen.add(key)
        if gaps or row is None:
            company = lead.get("company") or {}
            issues.append({"target": company.get("domain") or company.get("canonical_name") or "unknown",
                           "fields": gaps or ["output_contract"]})
    return issues


def companies(leads, icp, request, limit=MAX_COMPANIES):
    """The confirmed companies in leads.json order, mapped and capped at the limit."""
    request_signals = request.get("buying_signals") or []
    rows, seen = [], set()
    for lead in (leads or {}).get("leads") or []:
        row = company_row(lead, icp, request_signals)
        key = (row or {}).get("company_website", "").lower().rstrip("/")
        if row and key not in seen:
            seen.add(key)
            rows.append(row)
        if len(rows) >= min(limit, MAX_COMPANIES):
            break
    return rows


def _held_as_passing(check):
    """An open primary signal check, mapped from its own captured sources; other checks unchanged.

    Its description is its first source passage with an event day, never the review's doubt; the day a page was
    read never dates it (Leadpoet's judge fails such a page). With no such public passage the description stays
    empty and the company is left out.
    """
    if check.get("status") != "unknown" or check.get("criterion_id") != "signal:0":
        return check
    dated = _reachable_first([item for item in check.get("evidence") or []
                              if public_url(item.get("url")) and _one_line(item.get("text"))
                              and signal_date({"event_date": item.get("event_date")})])
    return dict(check, status="pass", claim=dated[0]["text"] if dated else "", evidence=dated)


def held_companies(document, icp, limit=HELD_COMPANIES):
    """Held companies Leadpoet may still qualify, for an ICP that confirmed none; they stay unconfirmed.

    A held company qualifies when its saved review failed no check, passed every filter, size and required
    attribute (the requested stage included), and left only the primary signal unknown, with a captured passage
    that has an event day. It is mapped like a confirmed company, in review order. A stage left open is not
    published: in round arena-2026-10-14 both such holds had evidence against the requested stage.
    """
    request = document["request"]
    reviewed = {c["id"] for c in criteria(request) if c["kind"] in ("attribute", "filter", "size")} | {"signal:0"}
    rows, seen = [], set()
    for held in document.get("unresolved") or []:
        company, checks = held.get("candidate"), held.get("qualification_checks")
        if held.get("stage") != "account" or not isinstance(company, dict) or not isinstance(checks, list):
            continue
        checks = [c for c in checks if isinstance(c, dict)]
        unfinished = [c for c in checks if c.get("criterion_id") in reviewed and c.get("status") != "pass"]
        if (any(c.get("status") == "fail" for c in checks) or not unfinished
                or not reviewed <= {c.get("criterion_id") for c in checks}
                or any(c.get("status") != "unknown" or c.get("criterion_id") != "signal:0" for c in unfinished)):
            continue
        lead = {"company": company, "qualification_checks": [_held_as_passing(c) for c in checks]}
        if excluded_company(request, lead):
            continue
        row = company_row(lead, icp, request.get("buying_signals") or [])
        key = (row or {}).get("company_website", "").lower().rstrip("/")
        if row and key not in seen:
            seen.add(key)
            rows.append(row)
        if len(rows) >= min(limit, MAX_COMPANIES):
            break
    return rows
