#!/usr/bin/env python3
"""Validate a TYCHE company-sourcing run: reviewed company rows, evidence, accounting and stopping."""

from __future__ import annotations

import argparse
import calendar
import json
import math
import pathlib
import re
import sys
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

from budget_guard import UNKNOWN_CHARGE_BASIS
import arena_research_stop
import company_links
from linkedin_receipts import (_entity, _linkedin_url, company_record, employee_range_bounds, identity_page,
                               linkedin_receipt_errors)
from deepline import LINKEDIN_REDIRECTS, STRUCTURED_RECORD_TOOLS
from source_receipts import funding_record, read_receipt, web_passage
from run_coordination import company_key, host_key


ACTIONABLE_FRONTIER_STATES = {"untried", "continuable"}
FINAL_FRONTIER_STATES = {"exhausted", "blocked"}
PAID_PROVIDERS = {"deepline", "scrapingdog"}
# 2.1: every qualification check names its requested criterion by a stable criterion_id. Results written
# by an earlier revision, including contact-flow runs (1.x), are kept as saved but not read by this code.
RESULT_SCHEMA_VERSION = "2.1"
CONTACT_HANDOFF = ("Buyer roles, contacts per company, email, phone and person location belong to the "
                   "contact stage (DEMOS); the caller keeps them for the end-to-end target.")
# Contact-stage inputs a request may carry by mistake; named so the refusal says where they belong.
CONTACT_FIELDS = {"requested_roles", "contact_role_groups", "contact_fields", "contacts_per_company",
                  "min_contacts_per_company", "target_contacts_per_company"}
# The saved company record ($defs.company in the output contract); a held or rejected candidate may also keep its
# discovered name as "company". Anything else, such as people or contact data, is refused.
COMPANY_FIELDS = {"canonical_name", "domain", "website", "linkedin_url", "linkedin_url_evidence", "industry",
                  "sub_industry", "classification_note", "description", "hq_state", "hq_country", "employee_range",
                  "employee_range_evidence", "owner_group", "aliases", "discovery_source"}
# The other closed objects of a saved company row, as documented in the output contract.
LINKEDIN_EVIDENCE_FIELDS = {"evidence_url", "evidence_date", "evidence_date_basis", "evidence_text", "source"}
ACCOUNT_FIT_FIELDS = LINKEDIN_EVIDENCE_FIELDS | {"fit_claim", "event_date"}
DISCOVERY_SOURCE_FIELDS = {"source", "url"}
TEXT_COMPANY_FIELDS = (COMPANY_FIELDS - {"aliases", "employee_range_evidence", "linkedin_url_evidence", "discovery_source"}
                       | {"company"})
SIGNAL_EVIDENCE_FIELDS = LINKEDIN_EVIDENCE_FIELDS | {"criterion_id", "criterion", "signal", "event_date"}
CHECK_FIELDS = {"criterion_id", "criterion", "importance", "signal", "status", "claim", "evidence", "support",
                "support_reason"}  # support and support_reason: a likely trait signal (criterion_errors).
EVIDENCE_FIELDS = {"url", "date", "date_basis", "text", "event_date", "source"}
SOURCE_FIELDS = {"provider", "operation", "tool", "route_id", "result_index"}
DETERMINATE_PROVIDER_STATUSES = {"ok", "partial", "no_results"}
BLOCKING_PROVIDER_STATUSES = {
    "rate_limited",
    "auth_failed",
    "quota_exceeded",
    "timeout",
    "schema_error",
    "provider_error",
    "config_error",
}
ROUTE_OUTCOME_RECEIPT_STATUSES = {
    "provider_status": BLOCKING_PROVIDER_STATUSES,
    "budget_exhausted": {"quota_exceeded"},
    "route_not_connected": {"config_error"},
    "timeout_unknown": {"timeout"},
}
COST_BASES = {"actual", "estimated", "unknown"}
DEEPLINE_USD_PER_CREDIT = Decimal("0.10")
COST_OUTPUT_QUANTUM = Decimal("0.0001")
NEXT_LEAD_REVIEW_CREDITS = Decimal("5")


def sourcing_target_met(document: dict) -> bool:
    """The target counts unique reviewed qualified companies."""
    return len(document.get("accepted", [])) >= document["request"]["target_count"]


def _identity(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"^https?://", "", value.strip().casefold()).rstrip("/")
    value = re.sub(r"^www\.", "", value)
    value = unicodedata.normalize("NFKD", value)
    return "".join(c for c in value if c.isalnum())


def _exclusion_identities(request, row):
    company = row.get("company")
    company = company if isinstance(company, dict) else row.get("candidate", row)
    if not isinstance(company, dict):
        return [], []
    names = [company.get(k) for k in ("canonical_name", "company", "domain", "owner_group")]
    aliases = company.get("aliases", [])
    if isinstance(aliases, list):
        names.extend(aliases)
    icp = request.get("icp", {})
    exclusions = icp.get("exclusions", []) if isinstance(icp, dict) else []
    return names, list(exclusions) if isinstance(exclusions, list) else []


def excluded_company(request: dict, row: dict) -> bool:
    """Match known company/owner aliases; discovering ownership still needs evidence."""
    names, exclusions = _exclusion_identities(request, row)
    # A listed domain, URL or section host excludes the company it keys (de.example.com excludes example.com).
    keys = {company_domain(e) for e in exclusions if isinstance(e, str) and "." in e and " " not in e.strip()}
    return bool({_identity(n) for n in names} & ({_identity(n) for n in exclusions} - {""})) or _company_key(row) in keys


def exclusion_identity_errors(request, row, path):
    """Hold plausible name extensions for review; never fuzzy-reject a company."""
    names, exclusions = _exclusion_identities(request, row)
    def words(value):
        if not isinstance(value, str) or "://" in value or re.fullmatch(r"[\w.-]+\.[a-zA-Z]{2,}", value):
            return []  # Domains still match exactly through excluded_company.
        parts = re.findall(r"[^\W_]+", unicodedata.normalize("NFKD", value).casefold())
        while parts and parts[-1] in {"ltd", "limited", "llp", "plc", "inc", "incorporated", "llc", "corp", "corporation"}:
            parts.pop()
        return parts
    errors = []
    for index, excluded in enumerate(exclusions):
        right = words(excluded)
        for name in names:
            left = words(name)
            short, long = sorted((left, right), key=len)
            if not (short and (len(short) >= 2 or len(short[0]) >= 4)
                    and _identity(name) != _identity(excluded) and len(long) - len(short) <= 3
                    and (long[:len(short)] == short or long[-len(short):] == short)):
                continue
            checks = [c for c in row.get("qualification_checks", []) if isinstance(c, dict)
                      and c.get("criterion_id") == f"exclusion:{index}"]
            if len(checks) != 1 or checks[0].get("status") != "pass" or not checks[0].get("evidence"):
                errors.append(f"{path}: resolve company identity against exclusion {excluded!r} before acceptance. "
                              f"If the same company, save its excluded name in company.aliases and reject. "
                              f"If distinct, save a passing evidence-backed check for criterion_id 'exclusion:{index}'.")
            break
    return errors


def signal_window_start(as_of, signal, window):
    """Resolve the requested unit against the saved date, including month ends."""
    policy = signal if any(k in signal for k in ("max_age_days", "max_age_months")) else window
    if "max_age_days" in policy:
        return as_of - timedelta(days=policy["max_age_days"])
    if "max_age_months" in policy:
        year, month = divmod(as_of.year * 12 + as_of.month - 1 - policy["max_age_months"], 12)
        month += 1
        return as_of.replace(year=year, month=month, day=min(as_of.day, calendar.monthrange(year, month)[1]))
    return None


def signal_request_errors(request: dict) -> list[str]:
    """Reject malformed saved policy before using it for coverage or arithmetic."""
    if not isinstance(request, dict):
        return ["request must be an object"]
    if request.get("signal_match_mode", "any") not in ("any", "all"):
        return ["request.signal_match_mode must be any or all"]
    signals = request.get("buying_signals", [])
    if not isinstance(signals, list):
        return ["request.buying_signals must be an array"]
    window = request.get("time_window", {})
    if not isinstance(window, dict):
        return ["request.time_window must be an object; omit it when no shared age limit was requested"]
    seen, errors = set(), []
    def age_errors(policy, path):
        for field in ("max_age_days", "max_age_months"):
            if field in policy and (type(policy[field]) is not int or policy[field] <= 0):
                errors.append(f"{path}.{field} must be a positive integer; omit unrequested limits")
        if "max_age_days" in policy and "max_age_months" in policy:
            errors.append(f"{path}: choose max_age_days or max_age_months, not both")
    age_errors(window, "request.time_window")
    for index, signal in enumerate(signals):
        path = f"request.buying_signals[{index}]"
        if not isinstance(signal, dict) or not (key := _identity(signal.get("kind"))):
            errors.append(f"{path} requires a non-empty kind")
            continue
        if key in seen:
            errors.append(f"{path}.kind duplicates another requested signal")
        seen.add(key)
        if signal.get("importance", "required") not in ("required", "preferred"):  # Omitted means required.
            errors.append(f"{path}.importance must be required or preferred")
        age_errors(signal, path)
        minimum = signal.get("min_age_days", 0)
        maximum = signal.get("max_age_days", window.get("max_age_days") if "max_age_months" not in signal else None)
        # Absence preserves an unspecified window; explicit malformed bounds fail.
        if (type(minimum) is not int or minimum < 0 or
                maximum is None and "max_age_days" in signal or
                maximum is not None and (type(maximum) is not int or maximum <= 0 or minimum > maximum)):
            errors.append(f"{path} age bounds must be nonnegative integers with min_age_days <= max_age_days and a positive maximum")
    return errors


def criteria(request: dict) -> list[dict]:
    """Every requested criterion with its stable id: a field or position in the immutable saved request.

    Filters and required attributes must each pass for acceptance; signals follow signal_match_mode; an
    exclusion criterion is checked only when a candidate's name resembles that excluded company.
    """
    problems = signal_request_errors(request)
    icp = request.get("icp", {}) if isinstance(request, dict) else None
    attributes = icp.get("required_attributes", []) if isinstance(icp, dict) else None
    if not isinstance(attributes, list) or any(not isinstance(a, str) or not a.strip() for a in attributes):
        problems.append("request.icp must be an object with required_attributes as a list of non-empty strings")
    if isinstance(icp, dict):
        for field in ("company_types", "industries", "geographies", "exclusions"):
            values = icp.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                problems.append(f"request.icp.{field} must be a list of non-empty strings")
    if problems:
        raise ValueError("Invalid saved request: " + "; ".join(problems) + ". Restore the original saved request before resuming; do not reset the budget.")
    band = icp.get("company_size")
    traits = {_identity(kind) for kind in request.get("trait_signals") or [] if isinstance(kind, str)}
    return ([{"id": f"attribute:{index}", "kind": "attribute", "label": label, "importance": "required"}
             for index, label in enumerate(attributes)] +
            [{"id": f"icp:{field}", "kind": "filter", "label": f"{field}: {', '.join(icp[field])}", "importance": "required"}
             for field in ("company_types", "industries", "geographies") if icp.get(field)] +
            ([{"id": "icp:company_size", "kind": "size", "label": company_size_label(band), "importance": "required"}]
             if isinstance(band, dict) and band else []) +
            [{"id": f"signal:{index}", "kind": "signal", "label": signal["kind"], "importance": signal.get("importance", "required"),
              **{k: signal[k] for k in ("query", "min_age_days", "max_age_days", "max_age_months") if k in signal},
              # A time-bound event still needs a confirming, dated source: a signal with an age window is never a trait.
              **({"trait": True} if _identity(signal["kind"]) in traits and not AGE_WINDOW & signal.keys() else {})}
             for index, signal in enumerate(request.get("buying_signals", []))] +
            [{"id": f"exclusion:{index}", "kind": "exclusion", "label": "Distinct from excluded company: " + name,
              "importance": "required"} for index, name in enumerate(icp.get("exclusions", []))])


def company_size_label(band: dict) -> str:
    lower, upper = band.get("min_employees"), band.get("max_employees")
    if lower is not None and upper is not None:
        return f"company_size: {lower}-{upper} employees"
    return f"company_size: at least {lower} employees" if lower is not None else f"company_size: at most {upper} employees"


def _size_check(check: Any) -> bool:
    return isinstance(check, dict) and check.get("criterion_id") == "icp:company_size"


def _cited_company_records(check: dict) -> list[Optional[str]]:
    """Route ids of LinkedIn company records cited by a check's evidence."""
    return [e["source"].get("route_id") for e in check.get("evidence", [])
            if isinstance(e, dict) and isinstance(e.get("source"), dict)
            and _identity(e["source"].get("tool")).endswith("getcompany")]


def _linkedin_evidence(item: dict) -> bool:
    """Evidence from a LinkedIn company page or the LinkedIn company record."""
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    host = host_key(urlsplit(str(item.get("url") or item.get("evidence_url") or "")).hostname or "")
    return host == "linkedin.com" or host.endswith(".linkedin.com") or _identity(source.get("tool")).endswith("getcompany")


def company_size_scope_errors(row: dict, band: dict, bounds, path: str) -> list[str]:
    """A saved LinkedIn range never decides size by itself.

    Whose workforce a figure counts is the reviewer's judgment. Code checks that
    a size check citing a LinkedIn company record cites this company's selected
    record (whose range is saved and receipt-verified) and agrees with that range.
    """
    lower, upper = band.get("min_employees", 0), band.get("max_employees")
    if type(lower) is not int or not (upper is None or type(upper) is int):
        return []
    company = row.get("company", row.get("candidate", {}))
    company = company if isinstance(company, dict) else {}
    evidence = company.get("employee_range_evidence")
    selected = (evidence.get("source") or {}).get("route_id") if isinstance(evidence, dict) else None
    errors = []
    for check in row.get("qualification_checks", []):
        if not _size_check(check) or check.get("status") not in {"pass", "fail"}:
            continue
        items = [e for e in check.get("evidence", []) if isinstance(e, dict)]
        if check["status"] == "fail" and items and all(_linkedin_evidence(e) for e in items):
            # A brand page's band often counts a whole network; unknown is not a rejection.
            errors.append(f"{path}: LinkedIn evidence alone (a page, posts or the company record) never fails company "
                          "size; keep size unknown or cite the correct entity's own current figure from another source")
            continue
        cited = _cited_company_records(check)
        if not cited:
            continue
        if bounds is None or selected is None or any(rid != selected for rid in cited):
            errors.append(f"{path}: the company_size check cites a LinkedIn company record that is not this company's "
                          "selected record; select company.ref from that getter so its range is saved with the company, "
                          "cite a captured passage that states the central entity's own workforce, or keep size unknown")
            continue
        start, end = bounds
        fits = start >= lower and (upper is None or end is not None and end <= upper)
        outside = end is not None and end < lower or upper is not None and start > upper
        if check["status"] == "pass" and not fits or check["status"] == "fail" and not outside:
            errors.append(f"{path}: the company_size check cites the LinkedIn company record, whose range "
                          f"{company.get('employee_range')!r} does not support {check['status']!r} against "
                          f"{company_size_label(band)}; cite a captured passage that states the central entity's own "
                          "workforce, or keep size unknown")
    return errors


def saved_field_errors(row: dict, path: str, company_fields: set) -> list[str]:
    """Each object in a saved company row keeps only its documented fields, so people, contact data or a
    narrative cannot ride along into leads.json and the later stages."""
    name = "company" if "company" in row else "candidate"
    company = row.get(name) if isinstance(row.get(name), dict) else {}
    shapes = [(company, company_fields, name),
              (company.get("employee_range_evidence"), LINKEDIN_EVIDENCE_FIELDS, name + ".employee_range_evidence"),
              (company.get("linkedin_url_evidence"), LINKEDIN_EVIDENCE_FIELDS, name + ".linkedin_url_evidence"),
              (company.get("discovery_source"), DISCOVERY_SOURCE_FIELDS, name + ".discovery_source"),
              (row.get("account_fit"), ACCOUNT_FIT_FIELDS, "account_fit"),
              (row.get("signal_evidence"), SIGNAL_EVIDENCE_FIELDS, "signal_evidence")]
    for key in ("qualification_checks", "supporting_findings"):
        for index, item in enumerate(row.get(key) if isinstance(row.get(key), list) else []):
            if key == "qualification_checks":
                shapes.append((item, CHECK_FIELDS, f"{key}[{index}]"))
            evidence = item.get("evidence") if isinstance(item, dict) else None
            for offset, entry in enumerate(evidence if isinstance(evidence, list) else []):
                shapes.append((entry, EVIDENCE_FIELDS, f"{key}[{index}].evidence[{offset}]"))
    shapes += [(value.get("source"), SOURCE_FIELDS, where + ".source") for value, fields, where in shapes[1:]
               if isinstance(value, dict) and "source" in fields]
    errors = []
    aliases = company.get("aliases", [])
    wrong = sorted(key for key in TEXT_COMPANY_FIELDS & company.keys() if not isinstance(company[key], str))
    wrong += [] if isinstance(aliases, list) and all(isinstance(a, str) for a in aliases) else ["aliases"]
    if wrong:
        errors.append(f"{path}.{name}: {', '.join(wrong)} must be text (aliases: a list of text). {CONTACT_HANDOFF}")
    for index, (value, fields, where) in enumerate(shapes):
        if not isinstance(value, dict):
            continue
        if extra := sorted(set(value) - fields):
            errors.append(f"{path}.{where}: {', '.join(extra)} is not a {'company' if where == name else 'saved'} "
                          f"field; keep only {', '.join(sorted(fields))}. {CONTACT_HANDOFF}")
        # A missing value may be null; an object, list or number could hide a person.
        if index and fields is not CHECK_FIELDS and (untyped := sorted(key for key, item in value.items() if not (
                item is None or isinstance(item, str) or key == "result_index" and type(item) is int
                or key == "source" and isinstance(item, dict)))):
            errors.append(f"{path}.{where}: {', '.join(untyped)} must be text (source: a saved receipt reference). "
                          f"{CONTACT_HANDOFF}")
    return errors


def discovery_source_error(company: Any, document: dict, path: str, run_file=None) -> Optional[str]:
    """A saved discovery_source names an account-discovery result this run saved, and its URL comes from it."""
    value = company.get("discovery_source") if isinstance(company, dict) else None
    if value is None:
        return None
    source = value.get("source") if isinstance(value, dict) else None
    route = next((r for r in document.get("routes", []) if isinstance(r, dict) and isinstance(source, dict)
                  and r.get("route_id") == source.get("route_id")), None)
    if (route is None or route.get("phase") != "account_discovery" or type(source.get("result_index")) is not int
            or any(source.get(key) != route.get(key) for key in ("provider", "operation"))
            or "tool" in source and source["tool"] != route.get("tool")):
        return (f"{path}.discovery_source must be the saved account-discovery result it names; "
                "select its ref and let code save it")
    if run_file is not None and value.get("url") is not None:
        try:
            saved = json.dumps(read_receipt(run_file, source["route_id"])["result"])
        except (OSError, ValueError, KeyError, TypeError):
            saved = ""
        if json.dumps(str(value["url"]))[1:-1] not in saved:
            return f"{path}.discovery_source.url must come from its saved discovery result; select its ref"
    return None


def company_record_error(run_file, document: dict, company: Any, item: Any) -> Optional[str]:
    """A cited LinkedIn company record must be this company's own receipt-verified record."""
    source = item.get("source") if isinstance(item, dict) else None
    if run_file is None or not isinstance(source, dict) or not _identity(source.get("tool")).endswith("getcompany"):
        return None
    try:
        record = company_record(run_file, source, item.get("evidence_url", item.get("url")), document.get("routes", []))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return f"the cited LinkedIn company record could not be verified: {exc}"
    company = company if isinstance(company, dict) else {}
    target = _company_key({"company": company})
    recorded = _company_key({"company": {"domain": record.get("domain")}}) if record.get("domain") else None
    page = _entity(identity_page(company), "company")
    same_page = page is not None and page == _entity(record.get("company_linkedin_url"), "company")
    if recorded != target and not same_page:
        return (f"the cited LinkedIn company record identifies {record.get('company')!r} ({record.get('domain')!r}), "
                "not this company; cite this company's own record or keep the check unknown")
    return None


def required_attribute_errors(request: dict, row: dict, path: str) -> list[str]:
    """Require reviewed evidence for every filter, company size and required attribute, without interpreting it."""
    errors = []
    for criterion in criteria(request):
        if criterion["kind"] in {"signal", "exclusion"}:
            continue
        checks = [c for c in row.get("qualification_checks", []) if isinstance(c, dict)
                  and c.get("criterion_id") == criterion["id"]]
        if len(checks) != 1 or checks[0].get("status") != "pass" or not checks[0].get("evidence"):
            errors.append(f"{path}: required criterion {criterion['id']} ({criterion['label']!r}) needs one passing "
                          "evidence-backed check")
    return errors


def requested_signal(request: dict, label: str) -> dict:
    """Resolve a saved kind, never a guessed synonym or a broader date window."""
    if errors := signal_request_errors(request):
        raise ValueError("; ".join(errors))
    signals = request.get("buying_signals", [])
    matches = [s for s in signals if isinstance(s, dict) and _identity(s.get("kind")) == _identity(label)]
    if len(matches) != 1:
        choices = ", ".join(str(s.get("kind")) for s in signals if isinstance(s, dict))
        raise ValueError(f"signal {label!r} must identify one saved request kind: {choices}. Select its criterion_id; never guess a synonym.")
    return matches[0]


def signals_optional(request: dict) -> bool:
    signals = request.get("buying_signals")
    return (isinstance(signals, list) and bool(signals)
            and all(isinstance(s, dict) and s.get("importance") == "preferred" for s in signals))


def signal_checks(row: dict) -> list[dict]:
    return [c for c in row.get("qualification_checks", []) if isinstance(c, dict) and c.get("signal")]


def signal_coverage_errors(request: dict, row: dict, path: str) -> list[str]:
    """Check saved requirement coverage, not whether source prose proves a claim."""
    if errors := signal_request_errors(request):
        return errors
    signals = request.get("buying_signals", [])
    errors, by_kind = [], {}
    for check in signal_checks(row):
        try:
            signal = requested_signal(request, check["signal"])
        except ValueError as exc:
            errors.append(f"{path}: {exc}")
            continue
        key = _identity(check["signal"])
        if key in by_kind:
            errors.append(f"{path}: combine evidence into one current check for signal {check['signal']!r}")
        by_kind[key] = check
        if check.get("importance") != signal.get("importance", "required"):
            errors.append(f"{path}: signal {signal['kind']!r} importance contradicts the saved request")
    required = [s for s in signals if isinstance(s, dict) and s.get("importance", "required") == "required"]
    if required:
        passed = [s for s in required if (by_kind.get(_identity(s.get("kind")), {}).get("status") == "pass"
                                         and by_kind[_identity(s.get("kind"))].get("evidence"))]
        mode = request.get("signal_match_mode", "any")
        if mode == "all" and len(passed) != len(required) or mode == "any" and not passed:
            missing = ", ".join(s["kind"] for s in required if s not in passed)
            errors.append(f"{path}: required signal coverage ({mode}) is incomplete: {missing}; keep the account unresolved")
    return errors


def event_date_bounds(value):
    """Preserve source precision; a month/year is an interval, never a guessed day."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}){0,2}", value):
        raise ValueError("event_date requires YYYY, YYYY-MM or YYYY-MM-DD")
    parts = [int(part) for part in value.split("-")]
    year, month = parts[0], parts[1] if len(parts) > 1 else 1
    start = datetime(year, month, parts[2] if len(parts) > 2 else 1)
    end_month = month if len(parts) > 1 else 12
    end = start if len(parts) == 3 else datetime(year, end_month, calendar.monthrange(year, end_month)[1])
    return start, end


def company_website(company):
    """Normalize a known LinkedIn wrapper locally; never follow or guess a domain."""
    if not isinstance(company, dict):
        raise ValueError("company must be an object")
    def parse(value):
        value = value.strip() if isinstance(value, str) else value
        if not isinstance(value, str) or not value or re.search(r"[\s\\]", value):
            raise ValueError("company.website requires a direct HTTP/HTTPS company URL")
        parsed = urlsplit(value if ":" in value else "https://" + value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or "." not in parsed.hostname
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            raise ValueError("company.website requires a direct HTTP/HTTPS company URL")
        return parsed
    expected = host_key(parse(company.get("domain", "")).hostname)
    parsed = parse(company.get("website") or "https://" + expected)
    host = host_key(parsed.hostname)
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) and parsed.path in LINKEDIN_REDIRECTS:
        urls = parse_qs(parsed.query).get("url", [])
        if len(urls) != 1:
            raise ValueError("company.website redirect has no unique destination; select the verified company URL")
        parsed = parse(urls[0])
        host = host_key(parsed.hostname)
    if host == "linkedin.com" or host.endswith(".linkedin.com") or not (host == expected or host.endswith("." + expected)):
        raise ValueError("company.website destination differs from company.domain; reconcile the company identity using saved evidence")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment))


def signal_age_errors(request: dict, row: dict, path: str) -> list[str]:
    """Check date arithmetic for reviewed signals; interpreting the event stays with the LLM."""
    if errors := signal_request_errors(request):
        return errors
    window = request.get("time_window", {})
    window = window if isinstance(window, dict) else {}
    try:
        as_of = datetime.strptime(window.get("as_of_date", request.get("as_of_date", "")), "%Y-%m-%d")
    except (ValueError, TypeError):
        return []  # The request contract handles an absent/malformed clock.
    evidence = [(row.get("signal_evidence", {}), path + ".signal_evidence")]
    for check_index, check in enumerate(row.get("qualification_checks", [])):
        if isinstance(check, dict) and check.get("signal") and check.get("status") == "pass":
            items = check.get("evidence", [])
            evidence.extend(({**item, "signal": check["signal"]}, f"{path}.qualification_checks[{check_index}].evidence[{index}]")
                            for index, item in enumerate(items if isinstance(items, list) else []) if isinstance(item, dict))
    errors = []
    for item, label in evidence:
        if not isinstance(item, dict):
            continue
        if not item:
            continue
        try:
            signal = requested_signal(request, item.get("signal")) or {}
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        minimum = signal.get("min_age_days", 0)
        try:
            cutoff = signal_window_start(as_of, signal, window)
            latest = as_of - timedelta(days=minimum)
        except (ValueError, OverflowError) as exc:
            errors.append(f"{label}: invalid signal window: {exc}")
            continue
        if cutoff is None and minimum == 0:
            continue
        event_date = item.get("event_date")
        if event_date is None:
            errors.append(f"{label}.event_date is required for a dated signal; preserve the source date and select the supported activity date (YYYY, YYYY-MM or YYYY-MM-DD), or keep the signal unknown. A recap's publication date does not date the event; a news story reporting it as new dates it to the article, and a listing still open when captured is dated by the capture.")
            continue
        try:
            first, last = event_date_bounds(event_date)
        except (ValueError, TypeError) as exc:
            errors.append(f"{label}.event_date is invalid: {exc}")
            continue
        if last > latest or cutoff is not None and first < cutoff:
            outside = first > latest or cutoff is not None and last < cutoff
            errors.append(f"{label}: event_date {event_date} is not wholly within the requested window "
                          f"{cutoff.date() if cutoff else 'unbounded'} through {latest.date()}; "
                          + ("this passage dates the event wholly outside the window, so it cannot pass: remove it or "
                             "cite a passage dated inside the window; when no saved passage dates it inside, record the check as failed"
                             if outside else "its date range straddles the window's edge: narrow its date from evidence, "
                             "remove the passage or keep the check unknown, never rejected")
                          + ", before acceptance or delivery.")
    return errors


def _folded(value: Any) -> str:
    """Text compared as a reader would: compatibility forms, case, curly quotes, dashes and spacing folded."""
    text = unicodedata.normalize("NFKC", value if isinstance(value, str) else "").casefold()
    text = text.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                                         "\u2013": "-", "\u2014": "-"}))
    return " ".join(text.split())


def single_passage_attribute(request: dict, criterion: dict) -> bool:
    """Whether the host publishes this required attribute as one quote (request-options.json single_source_attributes,
    the attribute texts it names; Leadpoet's required_attribute), so a passing check on it cites only that passage.
    A stage line or other attribute it publishes differently keeps the ordinary rules."""
    named = request.get("single_source_attributes")
    label = _folded(criterion.get("label"))
    if not isinstance(named, list) or not label or criterion.get("kind") != "attribute" or criterion.get("importance") != "required":
        return False
    return any(text and (text in label or label in text) for text in map(_folded, named))


# A relative date on a page ("Posted 2 Days Ago") changes before a scorer re-reads the quote: on 2026-10-10 the only
# published attribute quote that failed a judge-rule review no longer occurred on its page for that reason.
RELATIVE_DATE = re.compile(r"\b(?:\d+\+?|an?|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
                           r"(?:second|minute|hour|day|week)s?\s+ago\b"
                           r"|\b(?:posted|updated|added)\s+(?:today|yesterday)\b", re.IGNORECASE)


def criterion_errors(request: dict, row: dict, path: str) -> list[str]:
    """Each check names one requested criterion; code-owned fields must match it."""
    known = {c["id"]: c for c in criteria(request)}
    errors, seen = [], set()
    for index, check in enumerate(row.get("qualification_checks", [])):
        label = f"{path}.qualification_checks[{index}]"
        criterion = known.get(check.get("criterion_id")) if isinstance(check, dict) else None
        if criterion is None:
            errors.append(f"{label} must select a requested criterion_id; other facts belong in supporting_findings")
            continue
        if check["criterion_id"] in seen:
            errors.append(f"{label} repeats {check['criterion_id']}; keep one current check per criterion")
        seen.add(check["criterion_id"])
        expected = {"criterion": criterion["label"], "importance": criterion["importance"],
                    "signal": criterion["label"] if criterion["kind"] == "signal" else None}
        if any(check.get(key) != value for key, value in expected.items()):
            errors.append(f"{label} criterion, importance and signal must match {check['criterion_id']} in the saved request")
        if check.get("status") not in {"pass", "fail", "unknown"} or not _nonempty_text(check.get("claim")):
            errors.append(f"{label} needs status pass, fail or unknown and a claim giving the reason")
        elif check["status"] != "unknown" and not check.get("evidence"):
            errors.append(f"{label}: pass and fail need evidence; missing support is unknown")
        if "support" in check or "support_reason" in check:
            reason = check.get("support_reason")
            if (check.get("support") != "likely" or not criterion.get("trait") or criterion["importance"] != "required"
                    or check.get("status") != "unknown"):
                errors.append(f"{label}: support likely belongs only on an unknown required trait signal "
                              "(request-options.json trait_signals); an attribute, filter or event, including a signal "
                              "with an age window, needs a confirming source")
            elif not _nonempty_text(reason) or len(reason) > LIKELY_REASON_LIMIT or reason.splitlines() != [reason]:
                errors.append(f"{label}: support likely needs support_reason, one line of at most {LIKELY_REASON_LIMIT} "
                              "characters stating the inference from this company's cited facts")
            elif not check.get("evidence"):
                errors.append(f"{label}: support likely needs at least one quoted, captured passage about this company")
        if check.get("status") == "pass" and single_passage_attribute(request, criterion):
            if len(check.get("evidence") or []) > 1:
                errors.append(f"{label}: this run publishes this attribute as one passage: cite only the captured "
                              "passage that states its distinctive part (how the company sells, whom it serves or the "
                              "activity the attribute asks for)")
            if dated := next((m.group(0) for item in check.get("evidence") or [] if isinstance(item, dict)
                              for m in [RELATIVE_DATE.search(str(item.get("text") or ""))] if m), None):
                errors.append(f"{label}: the cited passage contains the relative date {dated!r}, which changes on the "
                              "page before the scorer re-reads the quote; cite a span without it")
    return errors


def qualification_errors(document: dict, *, run_file=None) -> list[str]:
    request = document.get("request", {})
    try:
        criteria(request)
    except ValueError as exc:
        return [str(exc)]
    errors = []
    owners = set()
    icp = request.get("icp", {})
    band = icp.get("company_size") or {}
    for state in ("accepted", "rejected", "unresolved"):
        for index, row in enumerate(document.get(state, [])):
            if not isinstance(row, dict):
                continue
            path = f"{state}[{index}]"
            checks = row.get("qualification_checks", [])
            if not isinstance(checks, list):
                errors.append(f"{path}.qualification_checks must be an array")
                continue
            if row.get("stage") == "route":
                continue  # A provider outcome, not a company.
            if state != "accepted" and row.get("stage") != "account":
                errors.append(f"{path}: a held or rejected company has stage account; {CONTACT_HANDOFF}")
            errors.extend(criterion_errors(request, row, path))
            company = row.get("company", row.get("candidate", {}))
            if state != "unresolved" and any(isinstance(c, dict) and {"support", "support_reason"} & c.keys() for c in checks):
                errors.append(f"{path}: support likely belongs only on a held company's check; an accepted or rejected "
                              "company carries none")
            if state == "unresolved":
                for check in (c for c in checks if isinstance(c, dict) and c.get("support") == "likely"):
                    for item in (check.get("evidence") if isinstance(check.get("evidence"), list) else []):
                        if error := qualification_evidence_error(item, path + ".qualification_checks." + str(check.get("criterion_id")),
                                                                 document, company, check, run_file):
                            errors.append(error)
            errors.extend(saved_field_errors(row, path, COMPANY_FIELDS | ({"company"} if state != "accepted" else set())))
            if error := discovery_source_error(row.get("company", row.get("candidate")), document, path, run_file):
                errors.append(error)
            employee_range = company.get("employee_range") if isinstance(company, dict) else None
            bounds = employee_range_bounds(employee_range)
            if employee_range is not None and bounds is None:
                errors.append(f"{path}: employee_range must be a LinkedIn range")
            elif band:
                # Size is a reviewed check; a captured passage can decide it without a LinkedIn range.
                errors.extend(company_size_scope_errors(row, band, bounds, path))
            required = [c for c in checks if isinstance(c, dict) and c.get("importance") == "required"]
            ordinary = [c for c in required if not c.get("signal")]  # Includes a present exclusion check.
            failed = [c for c in required if not c.get("signal") and c.get("status") == "fail" and c.get("evidence")]
            if state == "accepted":
                errors.extend(required_attribute_errors(request, row, path))
                errors.extend(signal_coverage_errors(request, row, path))
                errors.extend(signal_age_errors(request, row, path))
                for check in required:
                    for item in (check.get("evidence") if isinstance(check.get("evidence"), list) else []):
                        if error := qualification_evidence_error(item, path + ".qualification_checks." + str(check.get("criterion_id")),
                                                                 document, company, check, run_file):
                            errors.append(error)
                if excluded_company(request, row):
                    errors.append(f"{path}: excluded company cannot pass the account gate")
                else:
                    errors.extend(exclusion_identity_errors(request, row, path))
                if any(c.get("status") != "pass" or not c.get("evidence") for c in ordinary):
                    errors.append(f"{path}: missing or failed required evidence must remain account-unresolved")
            if state == "rejected" and not failed:
                by_kind = {_identity(c["signal"]): c for c in signal_checks(row)}
                requested = [s for s in request.get("buying_signals", []) if s.get("importance", "required") == "required"]
                negatives = [s for s in requested if by_kind.get(_identity(s["kind"]), {}).get("status") == "fail"
                             and by_kind[_identity(s["kind"])].get("evidence")]
                if negatives and (request.get("signal_match_mode", "any") == "all" or len(negatives) == len(requested)):
                    failed = [by_kind[_identity(s["kind"])] for s in negatives]
                # A run that exports signal candidates offers a company that fits everything else downstream when its
                # required signals are not found, an event dated outside the window included; rejecting it would drop it.
                # An excluded company is never a candidate, so its rejection stands.
                fit_ids = [c["id"] for c in criteria(request) if c["kind"] in ("filter", "size", "attribute")]
                if failed and request.get("signal_candidates_max") and fit_ids and not excluded_company(request, row) and all(
                        {c.get("status") for c in checks if isinstance(c, dict) and c.get("criterion_id") == cid} == {"pass"}
                        for cid in fit_ids):
                    errors.append(f"{path}: this company passes every filter, size and attribute check and only required "
                                  "signals failed; hold it (hold_account) with its failed signals so it is offered as a "
                                  "signal candidate. Reject it only for another evidenced mismatch: save that filter, size, "
                                  "attribute or exclusion check as failed with its evidence")
            if state == "rejected":
                for check in failed:  # A rejection's evidence is quote-checked like an acceptance's.
                    for item in (check.get("evidence") if isinstance(check.get("evidence"), list) else []):
                        if error := qualification_evidence_error(item, path + ".qualification_checks." + str(check.get("criterion_id")),
                                                                 document, company, check, run_file, quote_failure=True):
                            errors.append(error)
            if state == "rejected" and row.get("reason_code") == "not_icp_fit" and not failed:
                errors.append(f"{path}: not_icp_fit requires an evidenced required failure; unknown is unresolved")
            if state == "accepted":
                company = row.get("company", {})
                owner = _identity(company.get("owner_group")) if isinstance(company, dict) else ""
                if owner and owner in owners:
                    errors.append(f"{path}: duplicate owner group")
                owners.add(owner)
    return errors


def progress_snapshot(document: dict) -> list[str]:
    """Stable verified milestones, not returned rows or model-written progress totals."""
    facts = set()
    for state in ("accepted", "unresolved", "rejected"):
        for row in document.get(state, []):
            if not isinstance(row, dict) or not (key := _company_key(row)):
                continue
            if state == "accepted":
                facts.update((f"{key}:lead", f"{key}:account"))
            checks = row.get("qualification_checks", [])
            for check in checks if isinstance(checks, list) else []:
                if (isinstance(check, dict) and check.get("importance") == "required"
                        and check.get("status") in {"pass", "fail"} and check.get("evidence")):
                    facts.add(f"{key}:evidence:{check.get('criterion_id')}")
    return sorted(facts)


def _research_key(value: dict) -> Optional[tuple]:
    scope = _route_scope(value) or "discovery"
    phase = value.get("phase") or ("account_discovery" if scope == "discovery" else "account_verification")
    return (scope, phase) if isinstance(scope, str) and isinstance(phase, str) else None


def stalled_approaches(document: dict, action: Optional[dict] = None) -> set[str]:
    """Compare research in the same scope/phase, not independent verification work."""
    groups = {}
    for route in [r for r in document.get("routes", []) if isinstance(r, dict)
                and r.get("entity_type") != "tool_catalog" and isinstance(r.get("approach"), str)
                and isinstance(r.get("progress_before"), list)
                and all(isinstance(k, str) for k in r["progress_before"])
                and r.get("provider_status") in {"ok", "no_results"}]:
        key = _research_key(route)
        if key is None or (action is not None and key != _research_key(action)):
            continue
        groups.setdefault(key, []).append(route)
    current, stalled = progress_snapshot(document), set()
    for (scope, _), attempts in groups.items():
        if len(attempts) < 2:
            continue
        first, second = attempts[-2:]

        def facts(snapshot):
            return {f for f in snapshot if scope == "discovery" or f.startswith(scope + ":")}

        if not (facts(second["progress_before"]) - facts(first["progress_before"])
                or facts(current) - facts(second["progress_before"])):
            stalled.update((first["approach"], second["approach"]))
    return stalled


def _route_scope(route: dict) -> Optional[str]:
    return route.get("scope") or ("discovery" if route.get("phase") == "account_discovery" else _company_key(route))


def _approach_key(value: str) -> str:
    """A new version label does not make a new research strategy."""
    value = re.sub(r"(^|[-_\s])v\d+(?=$|[-_\s])", r"\1", value.casefold())
    return re.sub(r"[-_\s]+", "-", value).strip("-")


def _reviewed_company_scopes(document: dict) -> set[str]:
    """Park reviewed gaps using existing receipts and frontier, without a new state."""
    frontier = document.get("stop_audit", {}).get("route_frontier", [])
    completed = {r.get("route_id") for r in frontier if isinstance(r, dict)
                 and _nonempty_text(r.get("route_id")) and r.get("state") == "exhausted"
                 and _nonempty_text(r.get("reason")) and _nonempty_text(r.get("exhaustion_basis"))}
    latest = {}
    for route in document.get("routes", []):
        if (isinstance(route, dict) and route.get("entity_type") != "tool_catalog"
                and _nonempty_text(_route_scope(route))):
            latest[_route_scope(route)] = route
    rows_by_scope = {}
    for row in document.get("unresolved", []):
        if isinstance(row, dict) and row.get("stage") == "account" and (scope := _company_key(row)):
            rows_by_scope.setdefault(scope, []).append(row)
    reviewed = set()
    for scope, rows in rows_by_scope.items():
        route = latest.get(scope, {})
        if (all(_nonempty_text(r.get("reason_text")) for r in rows)
                and route.get("phase") in {"account_discovery", "account_verification"}
                and _nonempty_text(route.get("route_id")) and route["route_id"] in completed
                and route.get("provider_status") in {"ok", "no_results"}):
            reviewed.add(scope)
    return reviewed


def _blocker_error(action: dict, document: dict) -> Optional[str]:
    blocker = action["blocker"]
    rows = document.get("routes", []) + [r for r in document.get("unresolved", []) if isinstance(r, dict) and r.get("stage") == "route"]
    evidence = next((r for r in rows if isinstance(r, dict) and r.get("route_id") == blocker.get("evidence_route_id")), None)
    if (not _nonempty_text(blocker.get("reason")) or not evidence
            or evidence.get("provider_status") not in BLOCKING_PROVIDER_STATUSES):
        return "blocker needs a reason and blocking receipt/outcome evidence_route_id"
    candidate = evidence.get("candidate", {})
    provider = evidence.get("provider") or (candidate.get("provider") if isinstance(candidate, dict) else None)
    if provider != action.get("provider") or _route_scope(evidence) != action.get("scope"):
        return "blocker receipt must match this provider and scope"
    if action.get("tool") and evidence.get("tool", evidence.get("operation")) != action["tool"]:
        return "blocker receipt must match this tool"
    frontier = document.get("stop_audit", {}).get("route_frontier", [])
    descendants = {evidence["route_id"]}
    for _ in frontier:
        for route in frontier:
            if route.get("route_id") in descendants:
                descendants.update(route.get("continuation_route_ids", []))
    # Pre-dispatch failures can exist only as route outcomes. Their position in
    # the append-only frontier still establishes whether a recovery came later.
    positions = {r.get("route_id"): i for i, r in enumerate(frontier)}
    outcome_only = not any(r.get("route_id") == evidence["route_id"] for r in document.get("routes", []))
    evidence_position = positions.get(evidence["route_id"])
    later = False
    for route in document.get("routes", []):
        if route.get("route_id") == evidence["route_id"]:
            later = True
            continue
        same_tool = (route.get("provider") == provider and _route_scope(route) == _route_scope(evidence)
                     and route.get("tool", route.get("operation")) == evidence.get("tool", evidence.get("operation")))
        later_outcome = outcome_only and (route.get("route_id") in descendants or (
            evidence_position is not None and positions.get(route.get("route_id"), -1) > evidence_position))
        if ((later or later_outcome) and (same_tool or route.get("route_id") in descendants)
                and route.get("provider_status") in DETERMINATE_PROVIDER_STATUSES):
            return "recovered error cannot justify stopping"
    return None


def _missing_catalog_review(document: dict, scopes: set[str]) -> list[str]:
    """Reuse this run's capability review; ordinary research does not expire it."""
    routes = [r for r in document.get("routes", []) if isinstance(r, dict)]
    refs = document.get("stop_check", {}).get("catalog_review_route_ids", [])
    if not isinstance(refs, list):
        return sorted(scopes)
    reviewed = {_route_scope(r) for r in routes
                if r.get("route_id") in refs
                and r.get("provider") == "deepline" and r.get("entity_type") == "tool_catalog"
                and r.get("operation") == "search"
                and r.get("provider_status") in {"ok", "no_results"} | BLOCKING_PROVIDER_STATUSES}
    # Capabilities are run-wide; do not repeat an identical catalog query for
    # every company. Recovery actions and blocker evidence remain scope-specific.
    return [] if "discovery" in reviewed else sorted(scopes - reviewed)


def company_domain(value: str) -> str:
    """The saved company key: the claim key (run_coordination.company_key); a name, or a name: key, stays itself."""
    try:
        return company_key(value)
    except ValueError:
        text = value.strip().lower()
        return text[4:] if text.startswith("www.") else text


def _company_key(row: Any) -> Optional[str]:
    if not isinstance(row, dict):
        return None
    company = row.get("company")
    if isinstance(company, dict):
        domain = company.get("domain")
        name = company.get("canonical_name")
    else:
        candidate = row.get("candidate", {})
        if not isinstance(candidate, dict):
            return None
        domain = candidate.get("domain")
        name = candidate.get("company")
    if isinstance(domain, str) and domain.strip():
        return company_domain(domain)
    if isinstance(name, str) and name.strip():
        return "name:" + " ".join(name.lower().split())
    return None


def _account_outcomes(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for state in ("rejected", "unresolved"):
        values = document.get(state, [])
        if isinstance(values, list):
            rows.extend(
                row
                for row in values
                if isinstance(row, dict) and row.get("stage") == "account"
            )
    return rows


def calculate_review_counts(document: dict[str, Any]) -> dict[str, int]:
    """Derive review counts from outcomes, without inferring qualification."""
    accepted = document.get("accepted", [])
    account_rows = _account_outcomes(document)
    accepted_keys = {key for key in map(_company_key, accepted) if key is not None}
    reviewed_keys = {key for key in map(_company_key, accepted + account_rows) if key is not None}
    reasons_by_key: dict[str, set[str]] = {}
    for row in account_rows:
        key, reason = _company_key(row), row.get("reason_code")
        if key is not None and isinstance(reason, str):
            reasons_by_key.setdefault(key, set()).add(reason)
    exclusion_keys = {key for key, reasons in reasons_by_key.items()
                      if reasons == {"explicit_exclusion"} and key not in accepted_keys}
    return {
        "candidate_companies_reviewed": len(reviewed_keys),
        "exclusion_only_rejections": len(exclusion_keys),
        "substantive_account_reviews": len(reviewed_keys - exclusion_keys),
        "duplicate_candidates": sum(row.get("reason_code") == "duplicate_domain" for row in account_rows),
    }


def _nonempty_text(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _decimal(value: Any) -> Optional[Decimal]:
    if not _number(value):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _json_decimal(value: Decimal, quantum: Optional[Decimal] = None) -> int | float:
    """Return a stable JSON number, with optional deterministic rounding."""

    if quantum is not None:
        value = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def calculate_cost_summary(document: dict[str, Any]) -> dict[str, Any]:
    """Calculate provider cost bounds from route receipts; a route without a known cost stays unknown."""

    if document.get("budget", {}).get("policy") == "actual_cost":
        providers = {}
        for provider in sorted(PAID_PROVIDERS):
            calls = [r for r in document.get("routes", []) if r.get("provider") == provider and r.get("paid_calls", 0)]
            known = sum((Decimal(str(r["cost_credits"])) for r in calls if r.get("cost_credits") is not None), Decimal(0))
            held = {"documented_tariff_hold", UNKNOWN_CHARGE_BASIS}
            providers[provider] = {"confirmed_credits": float(known),
                                   "pending_calls": sum(r.get("cost_credits") is None and r.get("cost_usd") is None
                                                        and r.get("billing_basis") not in held for r in calls)}
            tariff_calls = [r for r in calls if r.get("billing_basis") and r["billing_basis"] != UNKNOWN_CHARGE_BASIS]
            unknown = [r for r in calls if r.get("billing_basis") == UNKNOWN_CHARGE_BASIS]
            if tariff_calls:
                providers[provider]["documented_tariff_calls"] = len(tariff_calls)
            if unknown:
                providers[provider]["unknown_charge_calls"] = len(unknown)
            if tariff_calls or unknown:
                providers[provider]["held_credits"] = float(sum((Decimal(str(r["cost_upper_bound_credits"]))
                    for r in calls if r.get("billing_basis") in held), Decimal(0)))
            if provider == "deepline":
                providers[provider]["confirmed_usd"] = float(sum((
                    Decimal(str(r["cost_usd"])) if r.get("cost_usd") is not None else
                    Decimal(str(r.get("cost_credits") or 0)) * DEEPLINE_USD_PER_CREDIT for r in calls), Decimal(0)))
        return {"status": "incomplete" if any(p["pending_calls"] or p.get("held_credits") for p in providers.values()) else "calculated", **providers}

    accepted = document.get("accepted", [])
    accepted_companies = len(accepted) if isinstance(accepted, list) else 0

    provider_totals: dict[str, dict[str, Any]] = {
        provider: {
            "confirmed": Decimal("0"),
            "maximum": Decimal("0"),
            "unknown": False,
        }
        for provider in PAID_PROVIDERS
    }
    has_estimated = False
    has_unknown = False
    routes = document.get("routes", [])
    if not isinstance(routes, list):
        routes = []

    for route in routes:
        if not isinstance(route, dict):
            continue
        provider = route.get("provider")
        paid_calls = route.get("paid_calls")
        if provider not in PAID_PROVIDERS or not isinstance(paid_calls, int) or paid_calls < 1:
            continue

        cost = _decimal(route.get("cost_credits"))
        upper = _decimal(route.get("cost_upper_bound_credits"))
        basis = route.get("cost_basis")
        if basis not in COST_BASES:
            basis = "unknown"

        totals = provider_totals[provider]
        if basis == "actual" and cost is not None and cost >= 0:
            totals["confirmed"] += cost
            totals["maximum"] += cost
        elif basis == "estimated" and upper is not None and upper >= 0:
            totals["maximum"] += upper
            has_estimated = True
        else:
            totals["unknown"] = True
            has_unknown = True

    if has_unknown:
        status = "unknown"
    elif has_estimated:
        status = "estimated_range"
    else:
        status = "exact"

    providers: dict[str, dict[str, int | float | None]] = {}
    for provider in sorted(PAID_PROVIDERS):
        totals = provider_totals[provider]
        providers[provider] = {
            "confirmed_credits": _json_decimal(totals["confirmed"]),
            "maximum_credits": None
            if totals["unknown"]
            else _json_decimal(totals["maximum"]),
        }

    deepline = providers["deepline"]
    deepline_confirmed_usd = (
        Decimal(str(deepline["confirmed_credits"])) * DEEPLINE_USD_PER_CREDIT
    )
    deepline_maximum_usd = (
        None
        if deepline["maximum_credits"] is None
        else Decimal(str(deepline["maximum_credits"])) * DEEPLINE_USD_PER_CREDIT
    )
    deepline_summary: dict[str, int | float | None] = {
        "usd_per_credit": _json_decimal(DEEPLINE_USD_PER_CREDIT),
        "confirmed_credits": deepline["confirmed_credits"],
        "maximum_credits": deepline["maximum_credits"],
        "confirmed_usd": _json_decimal(deepline_confirmed_usd, COST_OUTPUT_QUANTUM),
        "maximum_usd": None
        if deepline_maximum_usd is None
        else _json_decimal(deepline_maximum_usd, COST_OUTPUT_QUANTUM),
    }

    if accepted_companies == 0:
        per_company = {"minimum": None, "maximum": None}
    else:
        denominator = Decimal(accepted_companies)
        per_company = {
            "minimum": _json_decimal(
                deepline_confirmed_usd / denominator, COST_OUTPUT_QUANTUM
            ),
            "maximum": None
            if deepline_maximum_usd is None
            else _json_decimal(
                deepline_maximum_usd / denominator, COST_OUTPUT_QUANTUM
            ),
        }

    return {
        "status": status,
        "accepted_companies": accepted_companies,
        "deepline": deepline_summary,
        "scrapingdog": providers["scrapingdog"],
        "deepline_cost_per_company_usd": per_company,
    }


def _validate_cost_accounting(document: dict[str, Any], errors: list[str]) -> None:
    """Every route carries its cost fields, and cost_summary is derived from them."""

    summary = document.get("summary", {})
    counted = summary.get("accepted_companies") if isinstance(summary, dict) else None
    if not isinstance(counted, int) or isinstance(counted, bool) or counted < 0:
        errors.append("summary.accepted_companies must be a non-negative integer for cost accounting")
    elif isinstance(document.get("accepted"), list) and counted != len(document["accepted"]):
        errors.append("summary.accepted_companies must equal len(accepted) for cost accounting")

    routes = document.get("routes", [])
    if not isinstance(routes, list):
        errors.append("routes must be an array for cost accounting")
        routes = []
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        path = f"routes[{index}]"
        missing = [
            field
            for field in ("cost_credits", "cost_upper_bound_credits", "cost_basis")
            if field not in route
        ]
        if missing:
            errors.append(f"{path} requires cost fields: {', '.join(missing)}")
            continue

        paid_calls = route.get("paid_calls")
        provider = route.get("provider")
        cost = _decimal(route.get("cost_credits"))
        upper = _decimal(route.get("cost_upper_bound_credits"))
        basis = route.get("cost_basis")
        if basis not in COST_BASES:
            errors.append(f"{path}.cost_basis must be actual, estimated, or unknown")
            continue
        if not isinstance(paid_calls, int) or isinstance(paid_calls, bool) or paid_calls < 0:
            continue
        if provider == "public_web" and paid_calls > 0:
            errors.append(f"{path}.paid_calls must be 0 for public_web")

        if paid_calls == 0:
            if basis != "actual" or cost != Decimal("0") or upper != Decimal("0"):
                errors.append(
                    f"{path} with no paid call must use actual cost with 0 actual and upper-bound credits"
                )
        elif basis == "actual":
            if cost is None or cost < 0:
                errors.append(f"{path}.cost_credits must be non-negative for actual cost")
            if upper is None or upper < 0:
                errors.append(
                    f"{path}.cost_upper_bound_credits must be non-negative for actual cost"
                )
            elif cost is not None and upper != cost:
                errors.append(
                    f"{path}.cost_upper_bound_credits must equal actual cost_credits"
                )
        elif basis == "estimated":
            if route.get("cost_credits") is not None:
                errors.append(f"{path}.cost_credits must be null for estimated cost")
            if upper is None or upper < 0:
                errors.append(
                    f"{path}.cost_upper_bound_credits must be a non-negative estimate"
                )
        else:
            if route.get("cost_credits") is not None:
                errors.append(f"{path}.cost_credits must be null for unknown cost")
            if route.get("cost_upper_bound_credits") is not None:
                errors.append(
                    f"{path}.cost_upper_bound_credits must be null for unknown cost"
                )

    expected = calculate_cost_summary(document)
    actual = document.get("cost_summary")
    if actual != expected:
        errors.append(
            "cost_summary must equal calculated route cost summary: "
            + json.dumps(expected, sort_keys=True)
        )

    budget = document.get("budget")
    limits = budget.get("limits") if isinstance(budget, dict) else None
    spent = budget.get("spent") if isinstance(budget, dict) else None
    if isinstance(limits, dict) and budget.get("policy") != "actual_cost":
        for provider in sorted(PAID_PROVIDERS):
            limit = _decimal(limits.get(f"{provider}_credits"))
            maximum = _decimal(expected[provider]["maximum_credits"])
            actual_spend = (
                _decimal(spent.get(f"{provider}_credits"))
                if isinstance(spent, dict)
                else None
            )
            if (
                limit is not None
                and maximum is not None
                and maximum > limit
                and not (actual_spend is not None and actual_spend > limit)
            ):
                errors.append(
                    f"cost_summary.{provider}.maximum_credits exceeds "
                    f"budget.limits.{provider}_credits {limit}"
                )


def _validate_budget_accounting(document: dict[str, Any], errors: list[str]) -> None:
    """Validate exact route accounting when the output budget is present."""

    budget = document.get("budget")
    if budget is None:
        return
    if not isinstance(budget, dict):
        errors.append("budget must be an object")
        return
    routes = document.get("routes", [])
    if not isinstance(routes, list):
        errors.append("routes must be an array")
        return

    route_paid_calls = 0
    known_costs = {provider: Decimal("0") for provider in PAID_PROVIDERS}
    unknown_cost_providers: set[str] = set()
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        paid_calls = route.get("paid_calls")
        if not isinstance(paid_calls, int) or isinstance(paid_calls, bool) or paid_calls < 0:
            errors.append(f"routes[{index}].paid_calls must be a non-negative integer")
            continue
        route_paid_calls += paid_calls
        provider = route.get("provider")
        if provider not in PAID_PROVIDERS:
            continue
        cost = route.get("cost_credits")
        if paid_calls > 0 and cost is None:
            unknown_cost_providers.add(provider)
        elif cost is not None:
            value = _decimal(cost)
            if value is None or value < 0:
                errors.append(f"routes[{index}].cost_credits must be a non-negative number or null")
            else:
                known_costs[provider] += value

    paid_calls = budget.get("paid_calls")
    if not isinstance(paid_calls, int) or isinstance(paid_calls, bool) or paid_calls < 0:
        errors.append("budget.paid_calls must be a non-negative integer")
    elif paid_calls != route_paid_calls:
        errors.append(
            f"budget.paid_calls must equal route paid-call sum ({route_paid_calls})"
        )

    spent = budget.get("spent")
    if not isinstance(spent, dict):
        errors.append("budget.spent must be an object")
        spent = {}
    for provider in sorted(PAID_PROVIDERS):
        actual = spent.get(f"{provider}_credits")
        if provider in unknown_cost_providers:
            if actual is not None:
                errors.append(
                    f"budget.spent.{provider}_credits must be null when a paid route cost is unknown"
                )
        else:
            expected = known_costs[provider]
            actual_decimal = _decimal(actual)
            if actual_decimal is None:
                errors.append(
                    f"budget.spent.{provider}_credits must equal known route cost sum {expected}"
                )
            elif actual_decimal != expected:
                errors.append(
                    f"budget.spent.{provider}_credits must equal known route cost sum {expected}"
                )

    status = budget.get("status")
    if unknown_cost_providers:
        if status != "unknown":
            errors.append("budget.status must be unknown when any paid route cost is unknown")
    elif status == "unknown":
        errors.append("budget.status cannot be unknown when all paid route costs are known")
    if status == "within_budget":
        if unknown_cost_providers or any(
            _decimal(spent.get(f"{provider}_credits")) is None
            for provider in PAID_PROVIDERS
        ):
            errors.append("within_budget requires known actual provider spend")

    limits = budget.get("limits")
    if not isinstance(limits, dict):
        errors.append("budget.limits must be an object")
        limits = {}
    for provider in sorted(PAID_PROVIDERS):
        limit = _decimal(limits.get(f"{provider}_credits"))
        actual = _decimal(spent.get(f"{provider}_credits"))
        # An unknown total cannot erase charges that are already confirmed.
        if budget.get("policy") != "actual_cost" and limit is not None and max(known_costs[provider], actual or Decimal("0")) > limit:
            errors.append(
                f"budget.spent.{provider}_credits exceeds limit {limit}"
            )


def _validate_next_lead_budget(document: dict[str, Any], errors: list[str]) -> None:
    """Enforce the optional Deepline allowance for finding the next lead.

    The allowance is grouped by the number of accepted leads before a call.
    This makes route changes and rejected candidates part of the same budget
    window. This is an explicitly requested hard limit, never a default.
    The default strategy-review warning is reported separately in progress.
    """

    if document.get("budget", {}).get("policy") == "actual_cost":
        return  # Dispatch checks observed spend; the last call may cross the threshold.

    request = document.get("request")
    request_budget = request.get("budget") if isinstance(request, dict) else None
    output_budget = document.get("budget")
    output_limits = output_budget.get("limits") if isinstance(output_budget, dict) else None

    configured: list[tuple[str, Decimal]] = []
    for path, source in (
        ("request.budget.max_deepline_credits_per_next_lead", request_budget),
        ("budget.limits.max_deepline_credits_per_next_lead", output_limits),
    ):
        if not isinstance(source, dict) or path.rsplit(".", 1)[-1] not in source:
            continue
        value = _decimal(source.get(path.rsplit(".", 1)[-1]))
        if value is None or value < 0:
            errors.append(f"{path} must be a non-negative number")
            continue
        configured.append((path, value))

    if not configured:
        return
    if len(configured) == 2 and configured[0][1] != configured[1][1]:
        errors.append(
            "request.budget.max_deepline_credits_per_next_lead must match "
            "budget.limits.max_deepline_credits_per_next_lead"
        )
    limit = configured[0][1]

    routes = document.get("routes", [])
    if not isinstance(routes, list):
        return
    grouped_costs: dict[int, Decimal] = {}
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        if route.get("provider") != "deepline":
            continue
        paid_calls = route.get("paid_calls")
        if not isinstance(paid_calls, int) or isinstance(paid_calls, bool) or paid_calls <= 0:
            continue

        accepted_before = route.get("accepted_leads_before_call")
        if not isinstance(accepted_before, int) or isinstance(accepted_before, bool) or accepted_before < 0:
            errors.append(
                f"routes[{index}].accepted_leads_before_call is required when the "
                "next-lead Deepline allowance is active"
            )
            continue
        basis = route.get("cost_basis")
        if basis == "actual":
            charge = _decimal(route.get("cost_credits"))
        elif basis == "estimated":
            charge = _decimal(route.get("cost_upper_bound_credits"))
        else:
            charge = None
        if charge is None or charge < 0:
            errors.append(
                f"routes[{index}] has unknown Deepline cost and cannot prove the "
                "next-lead allowance"
            )
            continue
        grouped_costs[accepted_before] = grouped_costs.get(accepted_before, Decimal("0")) + charge

        # Check each dispatch against all spend recorded so far at its count
        # or higher. Later demotions do not invalidate previously legal calls.
        total = sum(cost for group, cost in grouped_costs.items() if group >= accepted_before)
        if total > limit:
            errors.append(
                "Deepline next-lead allowance exceeded for "
                f"routes[{index}] at accepted_leads_before_call={accepted_before}: {total} > {limit} credits"
            )


def calculate_progress(document: dict[str, Any]) -> dict[str, Any]:
    """Derive a read-only work summary; warnings do not change completion rules."""
    accepted = document.get("accepted", [])
    accepted = accepted if isinstance(accepted, list) else []
    accepted_keys = {_company_key(row) for row in accepted} - {None}
    held: dict[str, dict[str, Any]] = {}
    failures: set[str] = set()
    rows = document.get("unresolved", [])
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        stage = row.get("stage")
        if stage == "route":
            failures.add(_nonempty_text(row.get("route_id")) or f"outcome:{index}")
            continue
        key = _company_key(row)
        if stage != "account" or key is None or key in accepted_keys:
            continue
        entry = held.setdefault(key, {
            "candidate": row.get("candidate", {}), "reasons": [],
            "qualification_checks": [],
        })
        entry["reasons"].append({"code": row.get("reason_code"), "detail": row.get("reason_text")})
        entry["qualification_checks"].extend(row.get("qualification_checks", []) or [])
    confirmed = Decimal("0")
    maximum = Decimal("0")
    unknown = False
    routes = document.get("routes", [])
    for index, route in enumerate(routes if isinstance(routes, list) else []):
        if not isinstance(route, dict):
            continue
        if route.get("provider_status") in BLOCKING_PROVIDER_STATUSES:
            failures.add(_nonempty_text(route.get("route_id")) or f"receipt:{index}")
        if route.get("provider") != "deepline" or not isinstance(route.get("paid_calls"), int) or route["paid_calls"] < 1:
            continue
        group = route.get("accepted_leads_before_call")
        if type(group) is not int or group < 0:
            unknown = True
            continue
        if group < len(accepted):
            continue
        basis = route.get("cost_basis")
        amount = _decimal(route.get("cost_credits") if basis == "actual" else route.get("cost_upper_bound_credits"))
        if basis not in {"actual", "estimated"} or amount is None or amount < 0:
            unknown = True
            continue
        maximum += amount
        if basis == "actual":
            confirmed += amount
    warnings = []
    review_due = maximum >= NEXT_LEAD_REVIEW_CREDITS
    if review_due:
        warnings.append("Review the sourcing strategy: at least 5 Deepline credits are charged or reserved since the last completed lead. This warning is not a spending cap or a stop reason.")
    if unknown:
        warnings.append("Next-company cost is incomplete because some paid routes lack a usable cost or accepted-company count. Unknown charges are not free.")
    return {
        "accepted_companies": len(accepted_keys),
        "account_evidence_missing": len(held),
        "provider_or_route_failures": len(failures),
        "unresolved_accounts": list(held.values()),
        "deepline_since_last_lead": {
            "confirmed_credits": _json_decimal(confirmed),
            **({} if document.get("budget", {}).get("policy") == "actual_cost" else {"maximum_credits": None if unknown else _json_decimal(maximum)}),
            "strategy_review_due": True if review_due else (None if unknown else False),
        },
        "warnings": warnings,
    }


def validate_continuations(frontier: dict[str, dict[str, Any]], errors: list[str]) -> None:
    graph: dict[str, list[str]] = {}
    for route_id, item in frontier.items():
        refs = item.get("continuation_route_ids", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            errors.append(f"route {route_id} continuation_route_ids must be an array of route IDs")
            continue
        if len(refs) != len(set(refs)):
            errors.append(f"route {route_id} has duplicate continuation_route_ids")
        graph[route_id] = refs
        if item.get("state") == "exhausted" and item.get("exhaustion_basis") in {"continuation_exhausted", "query_family_exhausted"} and not refs:
            errors.append(f"exhausted route {route_id} must reference its continuation attempts")
        for ref in refs:
            if ref not in frontier:
                errors.append(f"route {route_id} references missing continuation {ref}")
            elif item.get("state") == "exhausted" and frontier[ref].get("state") not in FINAL_FRONTIER_STATES:
                errors.append(f"exhausted route {route_id} has actionable continuation {ref}")
    # Iterative traversal also catches mutually exhausted routes supporting each other.
    finished: set[str] = set()
    for start in graph:
        active: set[str] = set()
        stack = [(start, False)]
        while stack:
            route_id, leaving = stack.pop()
            if leaving:
                active.discard(route_id)
                finished.add(route_id)
            elif route_id in active:
                errors.append(f"continuation cycle includes route {route_id}")
                break
            elif route_id not in finished:
                active.add(route_id)
                stack.append((route_id, True))
                stack.extend((ref, False) for ref in graph.get(route_id, []) if ref in graph)


def industry_taxonomy() -> dict:
    taxonomy_path = pathlib.Path(__file__).resolve().parents[1] / "assets" / "leadpoet_industry_taxonomy.json"
    return json.loads(taxonomy_path.read_text(encoding="utf-8"))


def _validate_client_output(accepted: list, errors: list[str]) -> None:
    taxonomy = industry_taxonomy()
    for index, row in enumerate(accepted):
        if not isinstance(row, dict):
            continue
        path = f"accepted[{index}]"
        company = row.get("company")
        if not isinstance(company, dict):
            continue
        if not isinstance(company.get("description"), str) or not company["description"].strip():
            errors.append(f"{path}.company.description is required; write exactly two factual sentences")
        note = company.get("classification_note")
        if "classification_note" in company and (not isinstance(note, str) or not note.strip()):
            errors.append(f"{path}.company.classification_note must be a non-empty string")
        industry, subindustry = company.get("industry"), company.get("sub_industry")
        if (
            not isinstance(industry, str)
            or not isinstance(subindustry, str)
            or industry not in taxonomy["parent_industries"]
            or industry not in taxonomy["subindustry_parents"].get(subindustry, [])
        ):
            if isinstance(subindustry, str) and subindustry in taxonomy["subindustry_parents"]:
                choices = f"Valid parents for {subindustry!r}: {taxonomy['subindustry_parents'][subindustry]}."
            elif isinstance(industry, str) and industry in taxonomy["parent_industries"]:
                choices = f"Get valid subindustries with tyche_inspect(field={('taxonomy.' + industry)!r})."
            else:
                choices = "Get canonical parent industries with tyche_inspect(field='taxonomy'), then inspect taxonomy.<industry>."
            errors.append(f"{path}.company requires an exact canonical industry/sub_industry pair; "
                          f"received {industry!r} / {subindustry!r}. {choices} Select from evidence; no classification was changed.")


def _validate_company_record(evidence: Any, linkedin: Any, path: str, routes: dict, errors: list[str]) -> None:
    """A saved LinkedIn company fact must come from this run's successful HarvestAPI company getter."""
    evidence = evidence if isinstance(evidence, dict) else {}
    url = evidence.get("evidence_url")
    if not _linkedin_url(url, "company"):
        errors.append(f"{path}.evidence_url requires the LinkedIn /company/ source")
    elif _nonempty_text(linkedin):
        # The saved link is what gets exported: it must be this evidence's LinkedIn company page, not another page.
        slug = lambda value: re.search(r"/company/([^/?#]+)", value, re.IGNORECASE)[1].casefold()
        if not _linkedin_url(linkedin, "company") or slug(url) != slug(linkedin):
            errors.append(f"{path}.evidence_url and the saved LinkedIn link must be the same LinkedIn /company/ URL")
    try:
        date = evidence.get("evidence_date")
        if not isinstance(date, str) or datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
            raise ValueError
    except ValueError:
        errors.append(f"{path}.evidence_date requires an ISO calendar date")
    if evidence.get("evidence_date_basis") != "observed_current" or not _nonempty_text(evidence.get("evidence_text")):
        errors.append(f"{path} requires observed_current evidence and the LinkedIn source text")
    source = evidence.get("source")
    source = source if isinstance(source, dict) else {}
    tool = source.get("tool")
    route_id = source.get("route_id")
    matching = routes.get(route_id.strip(), []) if isinstance(route_id, str) else []
    if (source.get("provider") != "deepline" or source.get("operation") != "execute"
            or not isinstance(tool, str) or "harvestapi" not in tool.casefold()
            or not _identity(tool).endswith("getcompany")
            or len(matching) != 1
            or matching[0].get("provider") != "deepline"
            or matching[0].get("operation") != "execute"
            or matching[0].get("tool") != tool
            or matching[0].get("provider_status") not in {"ok", "partial"}):
        errors.append(f"{path}.source requires a matching successful HarvestAPI execute route")


def linkedin_field_errors(document: dict) -> list[str]:
    """LinkedIn company facts are optional enrichment; when saved they must come from the company's receipt."""
    errors = []
    routes: dict[str, list[dict]] = {}
    for route in document.get("routes", []):
        if isinstance(route, dict) and isinstance(route.get("route_id"), str):
            routes.setdefault(route["route_id"].strip(), []).append(route)
    for index, row in enumerate(document.get("accepted", [])):
        company = row.get("company") if isinstance(row, dict) else None
        company = company if isinstance(company, dict) else {}
        path = f"accepted[{index}].company"
        if (_nonempty_text(company.get("linkedin_url")) and "employee_range_evidence" not in company
                and "linkedin_url_evidence" not in company):
            errors.append(f"{path}.linkedin_url requires this company's saved HarvestAPI receipt or a page on its own "
                          "site that links it; select company.ref from its getter or omit the link")
        if "linkedin_url_evidence" in company and not _linkedin_url(company.get("linkedin_url"), "company"):
            errors.append(f"{path}.linkedin_url_evidence requires the LinkedIn /company/ link it proves in linkedin_url")
        if "employee_range" in company and employee_range_bounds(company.get("employee_range")) is None:
            errors.append(f"{path}.employee_range requires the LinkedIn employee range")
        if "employee_range" in company or "employee_range_evidence" in company:
            _validate_company_record(company.get("employee_range_evidence"), company.get("linkedin_url"),
                                     f"{path}.employee_range_evidence", routes, errors)
    return errors


EVIDENCE_TEXT_LIMIT = 1000
LIKELY_REASON_LIMIT = 200  # ARETE's 'Likely' contract: support_reason is one line of at most 200 characters.
AGE_WINDOW = {"min_age_days", "max_age_days", "max_age_months"}
RECORD_TEXT_LIMIT = 2000  # A structured record's own text, such as a LinkedIn description (at most 2,000 characters).


def source_evidence_error(item, path, *, receipt_verified=False):
    item = item if isinstance(item, dict) else {}
    url, date, basis, excerpt = (item.get(a, item.get(b)) for a, b in (
        ("evidence_url", "url"), ("evidence_date", "date"),
        ("evidence_date_basis", "date_basis"), ("evidence_text", "text")))
    source = item.get("source") or {}
    try:
        event_date_bounds(date)
        date_valid = basis != "observed_current" or len(date) == 10
    except (ValueError, TypeError):
        date_valid = False
    missing = []
    if not (receipt_verified and url is None) and (not isinstance(url, str) or re.fullmatch(r"https?://[^\s]+", url) is None):
        missing.append("url (HTTP/HTTPS source)")
    if not date_valid:
        missing.append("date (YYYY, YYYY-MM or YYYY-MM-DD; observations require YYYY-MM-DD)")
    if not isinstance(basis, str) or basis not in {"published", "posted", "updated", "observed_current"}:
        missing.append("date_basis (published, posted, updated or observed_current)")
    limit = (RECORD_TEXT_LIMIT if isinstance(source, dict) and source.get("tool") in STRUCTURED_RECORD_TOOLS
             else EVIDENCE_TEXT_LIMIT)
    if isinstance(excerpt, str) and len(excerpt) > limit:
        missing.append(f"text (quote the supporting passage, at most {limit} characters, "
                       "exactly as captured; a whole page or record is not evidence)")
    if "event_date" in item:
        try:
            event_date_bounds(item["event_date"])
        except (ValueError, TypeError):
            missing.append("event_date (valid YYYY, YYYY-MM or YYYY-MM-DD)")
    if not _nonempty_text(excerpt):
        missing.append("text (supporting source excerpt)")
    if not isinstance(source, dict):
        missing.append("source (saved receipt reference)")
    else:
        missing += ["source." + k for k in ("provider", "operation", "route_id") if not _nonempty_text(source.get(k))]
    if missing:
        return f"{path} requires dated source evidence; missing or invalid: " + ", ".join(missing)
    return None


def candidate_fit_errors(document, row, path, run_file=None):
    """The acceptance checks without the signal ones, for a held row exported for signal research: required
    attributes, every required non-signal check passed with quote-checked evidence, no excluded or
    resembling-excluded company, and everything a lead row carries, checked as a lead is."""
    request = document["request"]
    required = [c for c in row.get("qualification_checks", []) if isinstance(c, dict)
                and c.get("importance") == "required" and not c.get("signal")]
    errors = required_attribute_errors(request, row, path)
    if excluded_company(request, row):
        errors.append(f"{path}: excluded company cannot pass the account gate")
    else:
        errors.extend(exclusion_identity_errors(request, row, path))
    if any(c.get("status") != "pass" or not c.get("evidence") for c in required):
        errors.append(f"{path}: every required non-signal check must pass with evidence")
    if errors:
        return errors  # No receipt reads for a row that already fails.
    # A held row saves only the company fields known so far, so a thin one cannot be read as a lead. The lead checks
    # require its identity, industry, description and account_fit, and quote-check that and every check's evidence.
    lead = {key: value for key, value in row.items() if key in ACCEPTED_FIELDS - {"signal_evidence"}}
    errors = accepted_errors(dict(document, accepted=[lead]), run_file=run_file, fill_missing=False)
    # A failed required signal is exported with its status and event dates, so its passage is quote-checked as a
    # rejection's is.
    for check in row.get("qualification_checks", []):
        if (run_file is not None and isinstance(check, dict) and check.get("signal")
                and check.get("importance") == "required" and check.get("status") == "fail"):
            for item in check.get("evidence") if isinstance(check.get("evidence"), list) else []:
                error = qualification_evidence_error(item, f"{path}.qualification_checks.{check.get('criterion_id')}",
                                                     document, row.get("company"), check, run_file, quote_failure=True)
                if error and error not in errors:
                    errors.append(error)
    return errors


def qualification_evidence_error(item, path, document, company, check, run_file, *, quote_failure=False):
    if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
        return source_evidence_error(item, path)
    if run_file is not None and (check.get("status") == "pass" or check.get("support") == "likely"
                                 or quote_failure and check.get("status") == "fail"):
        try:
            # Structured records are quoted too: the excerpt must occur in the record's field values.
            web_passage(run_file, document, item, require_excerpt=True)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return f"{path}.evidence ({item.get('source', {}).get('route_id')}): {exc}"
        if error := company_record_error(run_file, document, company, item):
            return f"{path}.evidence ({item.get('source', {}).get('route_id')}): {error}"
    if isinstance(item, dict) and item.get("url") is None and isinstance(item.get("source"), dict) and "result_index" in item["source"]:
        if not str(check.get("criterion_id")).startswith("attribute:"):
            return f"{path}: structured receipts are only supported for required company attributes; filters and signals require a source URL"
        try:
            funding_record(run_file, document, company, item)
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            return f"{path}: {exc}"
        return source_evidence_error(item, path, receipt_verified=True)
    return source_evidence_error(item, path)


def supporting_finding_errors(findings, path, *, document=None, run_file=None):
    """Optional client facts need real evidence, but never alter ICP eligibility."""
    if not isinstance(findings, list):
        return [f"{path} must be an array"]
    errors = []
    for index, finding in enumerate(findings):
        label = f"{path}[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{label} must be an object")
            continue
        if set(finding) - {"kind", "label", "claim", "evidence"}:
            errors.append(f"{label} accepts only kind, label, claim and evidence; use qualification_checks for eligibility")
        if finding.get("kind") not in ("signal", "context"):
            errors.append(f"{label}.kind must be signal or context")
        for field in ("label", "claim"):
            if not _nonempty_text(finding.get(field)):
                errors.append(f"{label}.{field} must be non-empty text")
        evidence = finding.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{label}.evidence requires at least one saved source; omit unsupported optional findings")
            continue
        for offset, item in enumerate(evidence):
            location = f"{label}.evidence[{offset}]"
            if error := source_evidence_error(item, location):
                errors.append(error)
            elif run_file is not None:
                try:
                    web_passage(run_file, document, item, require_excerpt=True)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append(f"{location}: {exc}; correct or omit this optional finding")
    return errors


def source_evidence_errors(document, *, run_file=None):
    """The evidence shape consumed by the workbook, the handoff and source review."""
    errors = []
    if document.get("accepted"):
        try:
            observed = str(document.get("retrieved_at", ""))[:10]
            if datetime.strptime(observed, "%Y-%m-%d").strftime("%Y-%m-%d") != observed:
                raise ValueError
        except ValueError:
            errors.append("retrieved_at requires an observation date")
    request = document.get("request", {})
    for index, row in enumerate(document.get("accepted", [])):
        if not isinstance(row, dict):
            continue
        company = row.get("company") if isinstance(row.get("company"), dict) else {}
        signal = row.get("signal_evidence")
        # Code derives signal_evidence from the passing required signal check. When that coverage is the gap,
        # qualification_errors reports it, so it is the one message.
        required_signal = signal or not (signals_optional(request)
                                         or signal_coverage_errors(request, row, f"accepted[{index}]"))
        if required_signal and (not isinstance(signal, dict) or not _nonempty_text(signal.get("signal"))):
            errors.append(f"accepted[{index}].signal_evidence.signal is required; it is derived from the passing "
                          "required signal check, so save that check with tyche_review")
        evidence = [("account_fit", row.get("account_fit"))]
        if required_signal:
            evidence.append(("signal_evidence", signal))
        if "employee_range_evidence" in company:
            evidence.append(("company.employee_range_evidence", company.get("employee_range_evidence")))
        for check in row.get("qualification_checks", []):
            if isinstance(check, dict):
                items = check.get("evidence", [])
                if not isinstance(items, list):
                    errors.append(f"accepted[{index}].qualification_checks.evidence must be an array")
                    continue
                for item in items:
                    if error := qualification_evidence_error(item, f"accepted[{index}].qualification_checks." + str(check.get("criterion_id")),
                                                             document, company, check, run_file):
                        errors.append(error)
        errors.extend(supporting_finding_errors(row.get("supporting_findings", []),
            f"accepted[{index}].supporting_findings", document=document, run_file=run_file))
        for path, item in evidence:
            if error := source_evidence_error(item, f"accepted[{index}].{path}"):
                errors.append(error)
            elif path == "account_fit" and run_file is not None:
                # A company is qualified on its fit passage: it must quote the captured page or the receipt-verified
                # record it cites, and a cited LinkedIn record must be this company's own.
                try:
                    web_passage(run_file, document, item, require_excerpt=True)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append(f"accepted[{index}].account_fit ({item.get('source', {}).get('route_id')}): {exc}")
                else:
                    if error := company_record_error(run_file, document, company, item):
                        errors.append(f"accepted[{index}].account_fit ({item.get('source', {}).get('route_id')}): {error}")
    return errors


def company_identity_errors(accepted: list) -> list[str]:
    """Every accepted row needs a canonical domain, a website on it, and no duplicate domain."""
    errors, domains = [], []
    for index, row in enumerate(accepted):
        if not isinstance(row, dict):
            continue
        company = row.get("company")
        domain = company.get("domain") if isinstance(company, dict) else None
        if not isinstance(domain, str) or not domain.strip():
            errors.append(f"accepted[{index}] requires a canonical company domain")
        else:
            domains.append(company_domain(domain))
            try:
                company_key(domain)  # Its own website domain: not a name, person profile or platform page.
            except ValueError as exc:
                errors.append(f"accepted[{index}].company.domain: {exc}")
        try:
            company_website(company if isinstance(company, dict) else {})
        except (ValueError, TypeError) as exc:
            errors.append(f"accepted[{index}].company.website: {exc}")
    duplicates = sorted(domain for domain in set(domains) if domains.count(domain) > 1)
    if duplicates:
        errors.append("accepted companies contain duplicate canonical domains: " + ", ".join(duplicates))
    return errors


ACCEPTED_FIELDS = {"company", "account_fit", "qualification_checks", "signal_evidence", "supporting_findings"}


def accepted_errors(document: dict, *, run_file=None, fill_missing=False) -> list[str]:
    """The accepted-company contract shared by review saves, confirmed companies and final delivery."""
    errors = linkedin_receipt_errors(document, run_file, fill_missing=fill_missing) if run_file is not None else []
    errors.extend(company_links.evidence_errors(document, run_file) if run_file is not None else [])
    errors.extend(linkedin_field_errors(document))
    errors.extend(source_evidence_errors(document, run_file=run_file))
    request, accepted = document.get("request", {}), document.get("accepted", [])
    required = {_identity(s.get("kind", "")) for s in request.get("buying_signals", [])
                if isinstance(s, dict) and s.get("importance") == "required"}
    for index, row in enumerate(accepted):
        if not isinstance(row, dict):
            errors.append(f"accepted[{index}] must be an object")
            continue
        if extra := sorted(set(row) - ACCEPTED_FIELDS):
            errors.append(f"accepted[{index}] carries {', '.join(extra)}; a company row has only "
                          f"{', '.join(sorted(ACCEPTED_FIELDS))}. {CONTACT_HANDOFF}")
        company = row.get("company")
        if not isinstance(company, dict):
            errors.append(f"accepted[{index}] requires a company object")
        elif not _nonempty_text(company.get("canonical_name")):
            errors.append(f"accepted[{index}].company.canonical_name is required")
        primary = row.get("signal_evidence")
        if required and isinstance(primary, dict) and primary.get("signal") and _identity(primary["signal"]) not in required:
            errors.append(f"accepted[{index}].signal_evidence.signal must be a required signal kind; a preferred signal "
                          "only ranks and never stands in as the initial signal")
    errors.extend(company_identity_errors(accepted))
    _validate_client_output(accepted, errors)
    return errors


DELIVERY_STOPS = {"target_met", "budget_exhausted", "time_limit_reached", arena_research_stop.DECISION}


def run_deadline(document: dict) -> Optional[datetime]:
    """Preserve the original clock plus explicit, audited operator extensions."""
    duration = document.get("request", {}).get("max_duration_seconds")
    if duration is None:
        return None
    if type(duration) is not int or duration <= 0:
        raise ValueError("max_duration_seconds must be a positive integer or null")
    from datetime import timedelta
    started = datetime.fromisoformat(document["stop_check"]["started_at"].replace("Z", "+00:00"))
    if started.utcoffset() is None:
        raise ValueError("started_at must be timezone-aware")
    try:
        deadline = started + timedelta(seconds=duration)
    except OverflowError as exc:
        raise ValueError("max_duration_seconds exceeds the supported timestamp range") from exc
    extensions = document.get("stop_check", {}).get("research_extensions", [])
    if not isinstance(extensions, list):
        raise ValueError("research_extensions must be an array")
    for extension in extensions:
        if (not isinstance(extension, dict) or not isinstance(extension.get("authorization"), str)
                or not extension["authorization"].strip()):
            raise ValueError("research extension requires its user authorization")
        try:
            previous, revised, recorded = [datetime.fromisoformat(extension[key].replace("Z", "+00:00"))
                for key in ("previous_deadline", "deadline", "recorded_at")]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError("research extension requires valid timestamps") from exc
        if any(value.utcoffset() is None for value in (previous, revised, recorded)):
            raise ValueError("research extension timestamps must be timezone-aware")
        if previous != deadline or revised <= max(previous, recorded):
            raise ValueError("research extension must extend the saved deadline")
        deadline = revised
    return deadline


def research_closes(document: dict) -> Optional[datetime]:
    """When research stops opening. The hard deadline itself never moves.

    A run may save stop_check.closing_seconds at start. Research then closes that
    long before run_deadline, so an open turn can end and report its own usage
    before the hard stop. Runs without the field close at their deadline.
    """
    deadline = run_deadline(document)
    seconds = document.get("stop_check", {}).get("closing_seconds", 0)
    if type(seconds) is not int or seconds < 0:
        raise ValueError("stop_check.closing_seconds must be a non-negative integer")
    from datetime import timedelta
    return None if deadline is None else deadline - timedelta(seconds=seconds)


def evaluate_stop(document: Any, *, now: Optional[datetime] = None, execution_budget=None) -> dict[str, Any]:
    """Check next actions independently of self-declared exhausted route labels."""
    errors: list[str] = []
    result: dict[str, Any] = {"decision": "repair_state", "eligible_actions": [], "errors": errors}
    if not isinstance(document, dict):
        errors.append("results must be an object")
        return result
    check = document.get("stop_check")
    request = document.get("request", {})
    if not isinstance(check, dict) or not isinstance(request, dict):
        errors.append("stop_check and request objects are required")
        return result
    target = request.get("target_count")
    accepted = document.get("accepted")
    if type(target) is not int or target < 1 or not isinstance(accepted, list):
        errors.append("positive target_count and accepted array are required")
        return result
    try:
        started = datetime.fromisoformat(check["started_at"].replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if started.utcoffset() is None or current.utcoffset() is None or started > current:
            raise ValueError("timestamp must be timezone-aware and not in the future")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        errors.append(f"stop_check.started_at: {exc}")
        return result
    duration = request.get("max_duration_seconds")
    if duration is not None and (type(duration) is not int or duration <= 0):
        errors.append("max_duration_seconds must be a positive integer or null")
        return result
    try:
        closes = research_closes(document)
    except ValueError as exc:
        errors.append(str(exc))
        return result
    result.update(checked_at=current.isoformat(), elapsed_seconds=(current - started).total_seconds())
    _validate_budget_accounting(document, errors)
    _validate_cost_accounting(document, errors)
    _validate_next_lead_budget(document, errors)
    if errors:
        return result
    actual_cost = document.get("budget", {}).get("policy") == "actual_cost"
    if actual_cost:
        if execution_budget is None:
            errors.append("actual-cost stopping requires the saved execution ledger")
            return result
        from budget_guard import spending_stop
        reason = spending_stop(execution_budget, accepted_count=len(accepted))
        if reason:
            result.update(decision="budget_exhausted" if reason == "budget_exhausted" else "input_or_configuration_stop",
                          reason=reason)
            return result
    try:
        arena_stop = arena_research_stop.read(document, execution_budget,
                                             now=current.timestamp())
    except (OSError, ValueError, KeyError, TypeError):
        errors.append("Arena research closure evidence is invalid; preserve it and reconcile the run")
        return result
    if sourcing_target_met(document):
        result["decision"] = "target_met"
        return result
    if arena_stop is not None:
        result.update(decision=arena_research_stop.DECISION, reason=arena_stop["cause"])
        return result
    if closes is not None and current >= closes:
        result["decision"] = "time_limit_reached"
        return result

    actions = check.get("next_actions")
    if not isinstance(actions, list):
        errors.append("stop_check.next_actions must be an array")
        return result
    for field in ("routes", "unresolved", "rejected"):
        if not isinstance(document.get(field, []), list):
            errors.append(f"{field} must be an array")
    request_budget = request.get("budget", {})
    if not isinstance(request_budget, dict):
        errors.append("request.budget must be an object")
    if errors:
        return result
    scopes = {"discovery"} if len(accepted) < target else set()
    for row in document.get("unresolved", []):
        if isinstance(row, dict) and row.get("stage") == "account":
            key = _company_key(row)
            if key:
                scopes.add(key)
    reviewed = _reviewed_company_scopes(document)
    covered: set[str] = set(reviewed)
    result["parked_scopes"] = sorted(reviewed)
    ids: set[str] = set()
    budget = document.get("budget", {})
    limits = budget.get("limits", {}) if isinstance(budget, dict) else {}
    if not isinstance(limits, dict):
        errors.append("budget.limits must be an object")
        return result
    costs = calculate_cost_summary(document)
    next_lead_limit = limits.get("max_deepline_credits_per_next_lead", request_budget.get("max_deepline_credits_per_next_lead"))
    blocked_kinds: list[str] = []
    needs_pricing = False
    stalled = set()
    strategy_changes = []
    for action in actions:
        if not isinstance(action, dict):
            errors.append("each next action must be an object")
            continue
        aid, scope = action.get("id"), action.get("scope")
        if not isinstance(aid, str) or not aid.strip() or aid in ids:
            errors.append("next action IDs must be nonempty and unique")
            continue
        ids.add(aid)
        if not isinstance(scope, str) or not scope.strip() or not _nonempty_text(action.get("description")):
            errors.append(f"{aid}: scope and concrete action description are required")
            continue
        covered.add(scope)
        blocker = action.get("blocker")
        if blocker is not None:
            if not isinstance(blocker, dict) or not isinstance(blocker.get("kind"), str) or blocker["kind"] not in {"approval_required", "access_unavailable", "required_input"}:
                errors.append(f"{aid}: invalid concrete blocker")
                continue
            problem = _blocker_error(action, document)
            if problem:
                errors.append(f"{aid}: {problem}")
            blocked_kinds.append(blocker["kind"])
            continue
        provider = action.get("provider")
        bound = action.get("cost_upper_bound_credits")
        calls = action.get("paid_calls")
        if "entity_type" in action and not _nonempty_text(action["entity_type"]):
            errors.append(f"{aid}: entity_type must be a nonempty string")
            continue
        if not isinstance(provider, str) or provider not in PAID_PROVIDERS | {"public_web"} or type(calls) is not int or calls < 0:
            errors.append(f"{aid}: valid provider and nonnegative paid_calls are required")
            continue
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound) or bound < 0):
            errors.append(f"{aid}: cost bound must be finite and nonnegative or null")
            continue
        if provider == "public_web" and (bound != 0 or calls != 0):
            errors.append(f"{aid}: public_web actions must have zero provider cost and paid calls")
            continue
        approach = action.get("approach")
        action_stalls = stalled_approaches(document, action)
        stalled.update(action_stalls)
        stalled_keys = {_approach_key(a) for a in action_stalls}
        repeated_recovery = scope in reviewed and any(
            _route_scope(r) == scope and r.get("entity_type") != "tool_catalog"
            and r.get("provider_status") in {"ok", "no_results"}
            and action.get("request_fingerprint") == r.get("request_fingerprint")
            and action.get("request_fingerprint")
            for r in document.get("routes", []) if isinstance(r, dict))
        if action.get("entity_type") != "tool_catalog" and (
                ((action_stalls or scope in reviewed) and not _nonempty_text(approach))
                or repeated_recovery
                or (isinstance(approach, str) and _approach_key(approach) in stalled_keys)):
            strategy_changes.append(aid)
            continue
        if bound == 0 and calls == 0:
            result["eligible_actions"].append(aid)
            continue
        if actual_cost:
            from budget_guard import BudgetError, check_allowance, refuses_unpriced
            # ScrapingDog is bounded by its documented tariff, which only the locked dispatch knows.
            if bound is None and refuses_unpriced(execution_budget) and provider != "scrapingdog":
                # Needs a price, as under the reserved policy. It is not a spent budget.
                needs_pricing = True
                result.setdefault("blocked_actions", {})[aid] = "A verified whole-call price bound is required."
                continue
            try:
                check_allowance(execution_budget, provider, bound, len(accepted),
                                documented_tariff=provider == "scrapingdog")
                result["eligible_actions"].append(aid)
            except BudgetError as exc:
                result.setdefault("blocked_actions", {})[aid] = str(exc)
            continue
        if bound is None:
            needs_pricing = True
            result.setdefault("blocked_actions", {})[aid] = "A verified whole-call price bound is required."
            continue
        maximum = costs.get(provider, {}).get("maximum_credits")
        cap = limits.get(f"{provider}_credits")
        if cap is None or _decimal(cap) is None or _decimal(cap) < 0:
            errors.append(f"{aid}: a finite nonnegative provider credit cap is required")
            continue
        if maximum is None:
            needs_pricing = True
            result.setdefault("blocked_actions", {})[aid] = "Existing provider cost has no safe upper bound; reconcile its saved receipts."
            continue
        if cap is not None and Decimal(str(maximum)) + Decimal(str(bound)) > Decimal(str(cap)):
            result.setdefault("blocked_actions", {})[aid] = f"{provider} credit cap {cap} would be exceeded by existing maximum {maximum} plus this call {bound}."
            continue
        if provider == "deepline" and next_lead_limit is not None:
            since_last = calculate_progress(document)["deepline_since_last_lead"]["maximum_credits"]
            if since_last is None:
                needs_pricing = True
                continue
            if Decimal(str(since_last)) + Decimal(str(bound)) > Decimal(str(next_lead_limit)):
                result.setdefault("blocked_actions", {})[aid] = "The per-next-lead credit cap would be exceeded."
                continue
        if execution_budget is not None:
            from budget_guard import BudgetError, check_allowance
            try:
                check_allowance(execution_budget, provider, bound, len(accepted))
            except BudgetError as exc:
                result.setdefault("blocked_actions", {})[aid] = str(exc)
                continue
            except (KeyError, TypeError, ArithmeticError) as exc:
                errors.append(f"execution budget: {exc}")
                continue
        result["eligible_actions"].append(aid)
    if errors:
        result["eligible_actions"] = []
        return result
    missing = sorted(scopes - covered)
    missing_routes = []
    for route in document.get("stop_audit", {}).get("route_frontier", []):
        if route.get("state") not in ACTIONABLE_FRONTIER_STATES:
            continue
        candidates = [a for a in actions if not a.get("blocker") and (
            a.get("id") == route.get("route_id") or a.get("id") in route.get("continuation_route_ids", [])
            or (_route_scope(route) == a.get("scope") and (
                not route.get("approach") or route["approach"] == a.get("approach"))))]
        if not candidates:
            missing_routes.append(route.get("route_id"))
    # A reviewed gap can have concrete new work; it is not parked while that
    # work is eligible. Exhaustion still requires the existing source review.
    result["parked_scopes"] = sorted(reviewed - {a["scope"] for a in actions if a["id"] in result["eligible_actions"]})
    result.update(strategy_change_required=bool(strategy_changes), stalled_approaches=sorted(stalled),
                  missing_routes=missing_routes)
    if not actions:
        # Exhausted queries describe past attempts, never the whole market.
        # Refill discovery instead of manufacturing an unaffordable action.
        result.update(decision="continue", missing_scopes=missing,
                      next="Choose a different source or research method within the saved budget and deadline.")
    elif result["eligible_actions"] or missing or missing_routes or needs_pricing or strategy_changes:
        result.update(decision="continue", missing_scopes=missing, pricing_required=needs_pricing)
    elif missing_review := _missing_catalog_review(document, scopes):
        result.update(decision="continue", catalog_review_required=missing_review)
    elif actions and len(blocked_kinds) == len(actions):
        result["decision"] = "input_or_configuration_stop" if any(k != "access_unavailable" for k in blocked_kinds) else "provider_stop"
    else:
        result["decision"] = "budget_exhausted"
    return result


def refuse_earlier_results(run_file) -> None:
    """Refuse a run saved by an earlier TYCHE before anything is written, so its files stay unchanged."""
    path = pathlib.Path(run_file)
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, UnicodeError, ValueError):
        return  # Unreadable state is reported by the caller's own checks.
    if document is not None and (error := schema_error(document)):
        raise ValueError(error)


def schema_error(document: Any) -> Optional[str]:
    """This code reads only its own schema; an older saved run stays on disk, unread."""
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version != RESULT_SCHEMA_VERSION:
        return (f"schema_version {version!r} is not {RESULT_SCHEMA_VERSION}: this run was written by an earlier TYCHE "
                "(1.x runs carry contacts; 2.0 runs lack criterion ids). Keep it as saved and read or resume it with the "
                "revision that wrote it (the last contact-flow and 2.0 revision is dc412f2); nothing was changed.")
    request = document.get("request")
    if isinstance(request, dict) and (contact := sorted(CONTACT_FIELDS & request.keys())):
        return f"request carries {', '.join(contact)}, which a company run never saves. {CONTACT_HANDOFF}"
    return None


def validate_run(document: Any, *, require_stop_check: bool = False, now: Optional[datetime] = None, execution_budget=None, run_file=None) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["results.json must contain one JSON object"]
    if error := schema_error(document):
        return [error]
    for collection in ("accepted", "rejected", "unresolved", "routes"):
        if not isinstance(document.get(collection, []), list):
            errors.append(f"{collection} must be an array")
    if errors:
        return errors

    request = document.get("request", {})
    summary = document.get("summary", {})
    accepted = document.get("accepted", [])
    if not isinstance(request, dict) or not isinstance(summary, dict):
        return ["request and summary must be objects"]
    errors.extend(qualification_errors(document, run_file=run_file))
    if document.get("criteria") != criteria(request):
        errors.append("criteria must equal the requested criteria derived from the saved request")

    target = request.get("target_count")
    if not isinstance(target, int) or isinstance(target, bool) or target < 1:
        return ["request.target_count must be a positive integer"]

    accepted_count = len(accepted)
    if summary.get("accepted_companies") != accepted_count:
        errors.append("summary.accepted_companies must equal len(accepted)")

    errors.extend(accepted_errors(document, run_file=run_file))

    _validate_budget_accounting(document, errors)
    _validate_cost_accounting(document, errors)
    _validate_next_lead_budget(document, errors)

    stop_reason = document.get("stop_reason")
    stop_check = None
    if require_stop_check or "stop_check" in document:
        stop_check = evaluate_stop(document, now=now, execution_budget=execution_budget)
        errors.extend(error for error in stop_check["errors"] if error not in errors)
        decision = stop_check["decision"]
        if decision == "continue":
            errors.append("stop check requires continuation: affordable actions, missing recovery/discovery actions, or unresolved pricing remain")
        elif decision != "repair_state" and decision != stop_reason:
            errors.append(f"stop_reason must match stop check decision: {decision}")
    elif stop_reason == "time_limit_reached":
        errors.append("time_limit_reached requires stop_check and an explicit max_duration_seconds")
    shortfall = max(0, target - accepted_count)
    try:
        complete = sourcing_target_met(document)
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    if complete:
        if stop_reason != "target_met":
            errors.append("a run that reaches its company target must stop with target_met")
    elif stop_reason == "target_met":
        errors.append("target_met is invalid while the company target is incomplete")

    audit = document.get("stop_audit")
    if shortfall and not isinstance(audit, dict):
        errors.append("a target shortfall requires stop_audit")
        return errors
    if not isinstance(audit, dict):
        return errors

    if audit.get("target_shortfall") != shortfall:
        errors.append("stop_audit.target_shortfall is inconsistent with the target")
    if audit.get("frontier_complete") is not True:
        errors.append("stop_audit.frontier_complete must be true")

    for field, expected in calculate_review_counts(document).items():
        if audit.get(field) != expected:
            errors.append(f"stop_audit.{field} must equal {expected}")

    frontier = audit.get("route_frontier")
    if not isinstance(frontier, list):
        errors.append("stop_audit.route_frontier must be an array")
        return errors

    frontier_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(frontier):
        if not isinstance(item, dict):
            errors.append(f"route_frontier[{index}] must be an object")
            continue
        route_id = item.get("route_id")
        if not isinstance(route_id, str) or not route_id.strip():
            errors.append(f"route_frontier[{index}].route_id must be non-empty")
            continue
        if route_id in frontier_by_id:
            errors.append(f"route_frontier contains duplicate route_id {route_id}")
        frontier_by_id[route_id] = item

    route_receipts = [
        route for route in document.get("routes", []) if isinstance(route, dict)
    ]
    receipts_by_id: dict[str, list[dict[str, Any]]] = {}
    for route in route_receipts:
        route_id = route.get("route_id")
        if isinstance(route_id, str):
            receipts_by_id.setdefault(route_id, []).append(route)
    duplicate_receipt_ids = sorted(
        route_id for route_id, rows in receipts_by_id.items() if len(rows) > 1
    )
    if duplicate_receipt_ids:
        errors.append(
            "routes contain duplicate route_id attempts: "
            + ", ".join(duplicate_receipt_ids)
        )
    attempted_ids = set(receipts_by_id)
    if require_stop_check or "stop_check" in document:
        # Unused future research is not unfinished dispatched work. Keep it in
        # the frontier after target completion instead of inventing exhaustion.
        if stop_reason not in {"time_limit_reached", "budget_exhausted", arena_research_stop.DECISION}:
            open_reviews = sorted(rid for rid in attempted_ids if
                                  frontier_by_id.get(rid, {}).get("state") in ACTIONABLE_FRONTIER_STATES)
            if open_reviews:
                errors.append("Review attempted routes before delivery: " + ", ".join(open_reviews))
    missing_receipts = sorted(attempted_ids - set(frontier_by_id))
    if missing_receipts:
        errors.append(
            "attempted routes missing from route_frontier: " + ", ".join(missing_receipts)
        )

    route_outcomes = [
        row
        for state in ("rejected", "unresolved")
        for row in document.get(state, [])
        if isinstance(row, dict)
        and row.get("stage") == "route"
        and isinstance(row.get("route_id"), str)
    ]
    route_outcomes_by_id: dict[str, list[dict[str, Any]]] = {}
    for outcome in route_outcomes:
        route_outcomes_by_id.setdefault(outcome["route_id"], []).append(outcome)
    duplicate_outcome_ids = sorted(
        route_id for route_id, rows in route_outcomes_by_id.items() if len(rows) > 1
    )
    if duplicate_outcome_ids:
        errors.append(
            "route outcomes contain duplicate route_id attempts: "
            + ", ".join(duplicate_outcome_ids)
        )
    route_outcome_ids = set(route_outcomes_by_id)
    missing_outcome_frontier = sorted(route_outcome_ids - set(frontier_by_id))
    if missing_outcome_frontier:
        errors.append(
            "route outcomes missing from route_frontier: "
            + ", ".join(missing_outcome_frontier)
        )
    shared_route_ids = sorted(set(receipts_by_id) & route_outcome_ids)
    reused_route_ids = [
        route_id
        for route_id in shared_route_ids
        if any(
            not any(
                receipt.get("provider_status") in ROUTE_OUTCOME_RECEIPT_STATUSES.get(
                    outcome.get("reason_code"), set()
                )
                for receipt in receipts_by_id[route_id]
            )
            for outcome in route_outcomes_by_id[route_id]
        )
    ]
    if reused_route_ids:
        errors.append(
            "route_id reused across a completed receipt and a separate route outcome: "
            + ", ".join(reused_route_ids)
        )
    unsupported_exhaustion = sorted(
        route_id
        for route_id, item in frontier_by_id.items()
        if item.get("state") == "exhausted" and route_id not in attempted_ids
    )
    if unsupported_exhaustion:
        errors.append(
            "exhausted routes missing attempt receipts: "
            + ", ".join(unsupported_exhaustion)
        )
    unsupported_blocks = sorted(
        route_id
        for route_id, item in frontier_by_id.items()
        if item.get("state") == "blocked"
        and route_id not in attempted_ids | route_outcome_ids
    )
    if unsupported_blocks:
        errors.append(
            "blocked routes missing attempt or route-outcome receipts: "
            + ", ".join(unsupported_blocks)
        )

    for route_id, item in frontier_by_id.items():
        statuses = {
            receipt.get("provider_status")
            for receipt in receipts_by_id.get(route_id, [])
            if isinstance(receipt.get("provider_status"), str)
        }
        if item.get("state") == "exhausted":
            blocking = sorted(statuses & BLOCKING_PROVIDER_STATUSES)
            if blocking:
                errors.append(
                    f"exhausted route {route_id} has blocking provider status: "
                    + ", ".join(blocking)
                )
            if not statuses & DETERMINATE_PROVIDER_STATUSES:
                errors.append(
                    f"exhausted route {route_id} requires a determinate attempt receipt"
                )
            basis = item.get("exhaustion_basis")
            if not isinstance(basis, str) or not basis.strip():
                errors.append(f"exhausted route {route_id} requires exhaustion_basis")
            if basis == "no_results" and any(
                receipt.get("provider_status") != "no_results" or receipt.get("rows_returned", 0) != 0
                for receipt in receipts_by_id.get(route_id, [])
            ):
                errors.append(f"exhausted route {route_id} claims no_results despite a nonempty or different outcome")
        elif (
            item.get("state") == "blocked"
            and statuses
            and route_id not in route_outcome_ids
        ):
            if not statuses & BLOCKING_PROVIDER_STATUSES:
                errors.append(
                    f"blocked route {route_id} requires a blocking provider status"
                )

    validate_continuations(frontier_by_id, errors)

    if shortfall:
        if not frontier:
            errors.append("a target shortfall requires at least one route-frontier item")
        actionable = sorted(
            route_id
            for route_id, item in frontier_by_id.items()
            if item.get("state") in ACTIONABLE_FRONTIER_STATES
        )
        invalid_states = sorted(
            route_id
            for route_id, item in frontier_by_id.items()
            if item.get("state") not in FINAL_FRONTIER_STATES | ACTIONABLE_FRONTIER_STATES
        )
        limit_reached = stop_check is not None and not stop_check["errors"] and stop_check["decision"] in {"time_limit_reached", "budget_exhausted", arena_research_stop.DECISION}
        if actionable and not limit_reached:
            errors.append(
                "run must continue while route_frontier is actionable: "
                + ", ".join(actionable)
            )
        if invalid_states:
            errors.append(
                "route_frontier has invalid or missing states: "
                + ", ".join(invalid_states)
            )
        for route_id, item in frontier_by_id.items():
            if item.get("state") in FINAL_FRONTIER_STATES:
                reason = item.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    errors.append(f"final route {route_id} requires a reason")

    capacity = audit.get("provider_call_capacity")
    if not isinstance(capacity, dict):
        errors.append("stop_audit.provider_call_capacity must be an object")
    else:
        budget = document.get("budget")
        spent = budget.get("spent") if isinstance(budget, dict) else None
        if isinstance(spent, dict):
            for provider in PAID_PROVIDERS:
                if (
                    spent.get(f"{provider}_credits") is None
                    and capacity.get(provider) != "unknown"
                ):
                    errors.append(
                        f"stop_audit.provider_call_capacity.{provider} must be unknown when actual spend is unknown"
                    )
    if stop_check is None and isinstance(capacity, dict) and shortfall and stop_reason == "budget_exhausted":
        available = sorted(
            provider
            for provider in PAID_PROVIDERS
            if capacity.get(provider) == "available"
        )
        unknown = sorted(
            provider
            for provider in PAID_PROVIDERS
            if capacity.get(provider) == "unknown"
        )
        if available:
            errors.append(
                "budget_exhausted is invalid while a paid provider is available: "
                + ", ".join(available)
            )
        if unknown:
            errors.append(
                "budget_exhausted requires known provider capacity; unknown: "
                + ", ".join(unknown)
            )

    if stop_check is None and shortfall and stop_reason == "provider_stop":
        blocked_routes = [
            item for item in frontier_by_id.values() if item.get("state") == "blocked"
        ]
        if not blocked_routes:
            errors.append("provider_stop requires at least one blocked route")
        if isinstance(capacity, dict):
            available = sorted(
                provider
                for provider in PAID_PROVIDERS
                if capacity.get(provider) == "available"
            )
            if available:
                errors.append(
                    "provider_stop is invalid while a paid provider is available: "
                    + ", ".join(available)
                )

    if shortfall and stop_reason == "no_productive_route":
        if not any(
            item.get("state") == "exhausted" for item in frontier_by_id.values()
        ):
            errors.append("no_productive_route requires at least one exhausted route")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a TYCHE company-sourcing run: rows, evidence, accounting and stopping."
    )
    parser.add_argument("results", type=pathlib.Path)
    parser.add_argument("--check-stop", action="store_true", help="evaluate continuation before the next action; draft results are allowed")
    parser.add_argument(
        "--show-cost-summary",
        action="store_true",
        help="include the route-derived cost summary in validator output",
    )
    parser.add_argument("--show-progress", action="store_true", help="include unresolved-company groups and nonblocking strategy warnings")
    parser.add_argument("--check-output", action="store_true", help="check accepted output only; use - for JSON stdin; never authorizes delivery")
    parser.add_argument("--confirmed-only", action="store_true", help="with --check-output, validate unchanged confirmed leads from a saved run")
    args = parser.parse_args()
    if args.confirmed_only and (not args.check_output or str(args.results) == "-"):
        parser.error("--confirmed-only requires --check-output and a saved results path")
    try:
        document = json.loads(sys.stdin.read() if args.check_output and str(args.results) == "-" else args.results.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "delivery_allowed": False, "errors": [str(exc)]}))
        return 2

    if error := schema_error(document):
        print(json.dumps({"valid": False, "delivery_allowed": False, "errors": [error]}))
        return 2

    if args.check_output:
        partial = {}
        try:
            if not isinstance(document.get("accepted"), list):
                raise ValueError("results must contain an accepted array")
            run_file = None if str(args.results) == "-" else args.results
            if args.confirmed_only:
                from confirmed_leads import export_view
                document, partial = export_view(run_file, document)
                partial["document"] = document
                errors = []
            else:
                errors = accepted_errors(document, run_file=run_file) + qualification_errors(document, run_file=run_file)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            errors = [str(exc)]
        print(json.dumps({**partial, "valid": not errors, "delivery_allowed": False, "errors": errors,
                          "websites": [company_website(row["company"]) for row in document["accepted"]] if not errors else []}))
        return 2 if errors else 0

    from budget_guard import audit_ledger, load_ledger
    try:
        execution_budget = load_ledger(args.results)
    except (ValueError, OSError) as exc:
        output = {"errors": [f"budget ledger: {exc}"], "valid": False, "delivery_allowed": False}
        if args.check_stop:
            output.update(decision="repair_state", eligible_actions=[])
        print(json.dumps(output, sort_keys=True))
        return 2
    ledger_errors = audit_ledger(args.results, document, state=execution_budget) if execution_budget is not None else []
    if ledger_errors:
        execution_budget = None
    if args.check_stop:
        output = evaluate_stop(document, execution_budget=execution_budget)
        output["delivery_allowed"] = False
        output["errors"].extend(ledger_errors)
        if output["errors"]:
            output.update(decision="repair_state", eligible_actions=[])
        print(json.dumps(output, sort_keys=True))
        return 2 if output["errors"] else 0
    checked_at = datetime.now(timezone.utc)
    errors = validate_run(document, require_stop_check=True, now=checked_at, execution_budget=execution_budget,
                          run_file=args.results)
    errors.extend(ledger_errors)
    output: dict[str, Any] = {"valid": not errors, "errors": errors, "stop_policy": "strict"}
    stop_check = evaluate_stop(document, now=checked_at, execution_budget=execution_budget)
    stop_check["errors"].extend(ledger_errors)
    if stop_check["errors"]:
        stop_check.update(decision="repair_state", eligible_actions=[])
    output["stop_decision"] = stop_check
    output["delivery_allowed"] = not errors and stop_check["decision"] in DELIVERY_STOPS
    if args.show_cost_summary and isinstance(document, dict):
        output["calculated_cost_summary"] = calculate_cost_summary(document)
    if args.show_progress and isinstance(document, dict):
        output["progress"] = calculate_progress(document)
    print(json.dumps(output, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
