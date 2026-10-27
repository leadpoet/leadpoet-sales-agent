"""Check saved LinkedIn company facts against this run's HarvestAPI company getter receipts."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import budget_guard
import deepline
from run_coordination import company_key


def _linkedin_url(value: Any, kind: str) -> bool:
    """Require a direct LinkedIn /company/ (or /in/) URL, not a post, search or lookalike host."""
    return isinstance(value, str) and re.fullmatch(
        rf"https?://(?:[a-z0-9-]+\.)*linkedin\.com/{kind}/[\w%~.'&-]+/?(?:[?#][^\s]*)?",  # As company_links.LINK reads them.
        value.strip(), re.IGNORECASE,
    ) is not None


def employee_range_bounds(value: Any) -> Optional[tuple[int, Optional[int]]]:
    """Read a published range without converting a member count into a band."""
    if not isinstance(value, str):
        return None
    value = re.sub(r"[\s,]", "", value).replace("–", "-").replace("—", "-")
    match = re.fullmatch(r"(\d+)(?:-(\d+)|(\+))", value)
    if not match:
        return None
    lower, upper = int(match[1]), int(match[2]) if match[2] else None
    return (lower, upper) if upper is None or upper >= lower else None


def _entity(value, kind):
    return unquote(urlsplit(value.strip()).path.split("/")[2]).casefold() if _linkedin_url(value, kind) else None


def _text(value):
    return " ".join(value.split()).casefold() if isinstance(value, str) else None


def identity_page(company):
    """The LinkedIn page that identifies a company for other records: its getter's own page only. A link saved from
    the company's own site shows what the site links, which may be a network's or a brand's page."""
    return company.get("linkedin_url") if isinstance(company, dict) and "linkedin_url_evidence" not in company else None


def _domain_key(value):
    try:
        return company_key(str(value or ""))
    except ValueError:
        return str(value or "").strip().lower().removeprefix("www.")


def searched_without_website(receipt, record):
    """A getter asked to search by name returns LinkedIn's best match. With no website, that record cannot show it
    is the searched company's, so it never identifies a company; a getter asked for a page keeps its own record."""
    payload = ((receipt.get("attempt") or {}).get("request") or {}).get("payload")
    return isinstance(payload, dict) and "search" in payload and not record.get("domain")


def company_record(run_file, source, url, routes):
    """The company record this run's HarvestAPI company getter returned for the LinkedIn page at url."""
    rid = source.get("route_id")
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", rid):
        raise ValueError("requires a saved HarvestAPI route ID")
    matching = [r for r in routes if isinstance(r, dict) and r.get("route_id") == rid]
    if len(matching) != 1:
        raise ValueError("requires one matching HarvestAPI route")
    route = matching[0]
    receipt = budget_guard.read_object(Path(run_file).resolve(strict=True).parent / "receipts" / (rid + ".json"))
    tool = source.get("tool")
    if (source.get("provider") != "deepline" or source.get("operation") != "execute"
            or not isinstance(tool, str) or "harvestapi" not in tool.casefold()
            or not re.sub(r"[^a-z0-9]", "", tool.casefold()).endswith("getcompany")
            or any(route.get(k) != source.get(k) for k in ("provider", "operation", "tool"))
            or receipt.get("provider") != "deepline" or receipt.get("operation") != "execute"
            or receipt.get("tool") != tool or receipt.get("receipt_status") != "complete"
            or receipt.get("status") not in {"ok", "partial"}
            or route.get("provider_status") != receipt.get("status")):
        raise ValueError("requires a completed successful HarvestAPI response")
    if receipt.get("run_fingerprint") != budget_guard.run_fingerprint(run_file):
        raise ValueError("saved HarvestAPI response belongs to another run or lacks run identity")
    if route.get("request_fingerprint") != receipt.get("request_fingerprint") or not route.get("request_fingerprint"):
        raise ValueError("saved HarvestAPI response does not match the route request")
    response = receipt.get("provider_response")
    if not isinstance(response, dict) or "body" not in response:
        raise ValueError("requires the captured HarvestAPI response body")
    # Reuse provider normalization on the captured body. Reviewer-authored
    # evidence text and edited normalized results cannot supply field values.
    normalized = deepline._execute_output(response["body"], tool, "company")
    if normalized.get("status") not in {"ok", "partial"}:
        raise ValueError("captured HarvestAPI response did not return usable company data")
    records = [p for p in normalized.get("results", []) if isinstance(p, dict)
               and _entity(url, "company") is not None and _entity(p.get("linkedinUrl"), "company") == _entity(url, "company")]
    if len(records) != 1:
        raise ValueError("captured HarvestAPI response must contain exactly one matching LinkedIn company")
    raw = {k: v for k, v in records[0].items() if k != "employee_range"}
    record = deepline.normalize_evidence(raw, tool=tool, entity_type="company")
    if searched_without_website(receipt, record):
        raise ValueError("a HarvestAPI name search returned a LinkedIn record with no website, so it cannot show "
                         "whose record it is")
    return record


def linkedin_receipt_errors(document, run_file, *, fill_missing=False):
    """A saved LinkedIn employee range must match this company's own getter receipt.

    Read-only by default; review saves may fill a missing range before validation.
    """
    if not isinstance(document, dict) or not isinstance(document.get("accepted", []), list):
        return ["results must be an object with an accepted array"]
    errors = []
    for index, row in enumerate(document.get("accepted", [])):
        company = row.get("company") if isinstance(row, dict) else None
        if not isinstance(company, dict) or not ("employee_range" in company or "employee_range_evidence" in company):
            continue  # LinkedIn company facts are optional; the structural contract reports a missing company.
        path = f"accepted[{index}].company"
        evidence = company.get("employee_range_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        source = evidence.get("source")
        try:
            record = company_record(run_file, source if isinstance(source, dict) else {},
                                   evidence.get("evidence_url"), document.get("routes", []))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{path}.employee_range_evidence: {exc}")
            continue
        if record.get("domain") and _domain_key(record["domain"]) != _domain_key(company.get("domain")):
            errors.append(f"{path}.employee_range_evidence cites another company's record ({record.get('company')!r}, "
                          f"{record['domain']!r}); select this company's own getter or omit its LinkedIn facts")
            continue
        actual, supplied = record.get("employee_range"), company.get("employee_range")
        if fill_missing and not supplied and actual:
            company["employee_range"] = supplied = actual
        if actual is None and supplied is None:
            continue  # The record states no range; it still identifies the company and its LinkedIn page.
        if employee_range_bounds(actual) is None or employee_range_bounds(supplied) != employee_range_bounds(actual):
            errors.append(f"{path}.employee_range must match the saved HarvestAPI LinkedIn value ({actual!r})")
    return errors


if __name__ == "__main__":
    try:
        errors = linkedin_receipt_errors(json.load(sys.stdin), Path(sys.argv[1]))
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        errors = [str(exc)]
    print(json.dumps({"valid": not errors, "errors": errors}))
    sys.exit(2 if errors else 0)
