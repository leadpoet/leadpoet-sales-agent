#!/usr/bin/env python3
"""Small JSON adapter for Deepline API execution and CLI-backed tool discovery.

API-key executions preserve raw errors for accounting. CLI-only authentication
continues to use the installed CLI; no uncertain execution is retried.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse, urlsplit

from provider_output import ResponseFile, load_json, response_body
from budget_guard import guarded_call, amount as billing_amount

# Tools whose rows are structured provider records, not captured pages: their text is a record field.
STRUCTURED_RECORD_TOOLS = frozenset({"harvestapi_get_company", "aviato_get_company_funding_rounds"})


STATUSES = {
    "ok",
    "no_results",
    "partial",
    "rate_limited",
    "auth_failed",
    "quota_exceeded",
    "timeout",
    "schema_error",
    "provider_error",
    "config_error",
}

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret|token|password|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_URL_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?key|token|secret|password|signature)=[^&#\s]+)"
)
_INLINE_SECRET = re.compile(
    r'''(?i)((?:["']?(?:api[_-]?key|access[_-]?key|private[_-]?key|credentials?|token|secret|password|signature|authorization|cookie)["']?)\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:Basic|Bearer)\s+[^,;\s]+|[^,;\s}]+)'''
)
_SECRET_HEADER = re.compile(r"(?im)^(\s*(?:authorization|(?:set-)?cookie)\s*:\s*)[^\r\n]*")
_STATUS_WORDS = {status: status for status in STATUSES}
_DEEPLINE_BIN = "DEEPLINE_BIN"
_EMPTY_CONTAINER_KEYS = {
    "toolResponse",
    "tool_response",
    "rawV2",
    "raw_v2",
    "raw",
    "getters",
    "extractedLists",
    "extracted_lists",
}
_PROVIDER_ERROR_STATUSES = {"rate_limited", "auth_failed", "quota_exceeded", "timeout", "provider_error"}
_FAILURE_STATUSES = _PROVIDER_ERROR_STATUSES | {"schema_error", "config_error"}
_SCALAR_RESULT_KEYS = ("count", "total")
_EXTRACTED_RESULT_KEYS = (
    "suggestions",
    "results",
    "items",
    "records",
    "data",
    "rows",
    "values",
    "preview",
    "elements",
    "matches",
    "evidence",
)
# Top-level fields that can carry a provider's output or a call's outcome, as opposed to metadata.
_PROVIDER_OUTPUT_FIELDS = (_EMPTY_CONTAINER_KEYS | set(_EXTRACTED_RESULT_KEYS) | set(_SCALAR_RESULT_KEYS)
                           | {"data", "result", "results", "response", "output"})
_OUTCOME_FIELDS = {"status", "success", "outcome", "state", "errors", "error_code", "tool_error"}


def _extracted_list_records(value: Any) -> List[Any]:
    """Extract rows from serialized Deepline list/getter output.

    ``deepline tools execute --json`` normally exposes provider rows through
    ``toolResponse.raw``. Some tools, including autocomplete tools, expose
    only a declared list such as ``suggestions`` (or a serialized dataset
    preview) in the command envelope. Keep this handling scoped to the
    extracted-list container so a generic ``{"value": ...}`` response is not
    accepted as a provider envelope by accident.
    """

    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    empty_rows: Optional[List[Any]] = None
    # The CLI may include several lists in this container. Only inspect known
    # result-bearing keys first, because metadata lists such as ``columns``
    # can appear before the actual provider rows.
    for key in _EXTRACTED_RESULT_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, list):
            if candidate:
                return candidate
            empty_rows = candidate
            continue
        if not isinstance(candidate, dict):
            continue
        if any(row_key in candidate for row_key in ("value", "label")):
            return [candidate]
        rows = _extracted_list_records(candidate)
        if rows:
            return rows
    # A serialized single row can be represented directly in the container.
    if any(key in value for key in ("value", "label")):
        return [value]
    return empty_rows or []


def _is_extracted_list_envelope(value: Any) -> bool:
    """Return whether a serialized extracted-list container has a known shape."""

    if isinstance(value, list):
        return True
    if not isinstance(value, dict):
        return False
    if not value:
        return True
    if any(key in value for key in ("value", "label")):
        return True
    for key in _EXTRACTED_RESULT_KEYS:
        if key not in value:
            continue
        candidate = value[key]
        if isinstance(candidate, list):
            return True
        if isinstance(candidate, dict) and _is_extracted_list_envelope(candidate):
            return True
    return False


def _scalar_result(value: Any) -> Optional[Dict[str, Any]]:
    """Return a valid count/total summary object as one result row."""

    if not isinstance(value, dict):
        return None
    if not any(
        key in value
        and isinstance(value[key], (int, float))
        and not isinstance(value[key], bool)
        for key in _SCALAR_RESULT_KEYS
    ):
        return None
    return value


def _scraped_document(value: Any) -> Optional[Dict[str, Any]]:
    """Recognize a successful captured page, not a company or buyer."""
    if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
        return None
    metadata = value["metadata"]
    status = metadata.get("statusCode")
    # ContextDev can return an explicit successful capture without HTTP
    # metadata. A supplied status still takes precedence over that flag.
    successful = (type(status) is int and 200 <= status < 300 if "statusCode" in metadata
                  else value.get("success") is True)
    url = metadata.get("sourceURL") or metadata.get("sourceUrl") or metadata.get("url") or metadata.get("finalUrl")
    if (not successful or value.get("error") or metadata.get("error")
            or value.get("success") is False or metadata.get("success") is False
            or not isinstance(url, str) or not url.startswith(("https://", "http://"))):
        return None
    try:
        if not urlparse(url).hostname:
            return None
    except ValueError:
        return None
    for content_format in ("markdown", "html", "text"):
        content = value.get(content_format)
        if isinstance(content, str) and content.strip():
            return dict(value, evidence_url=url, evidence_text=content,
                        content_format=content_format, signal="web_page")
    return None


def _native_page_output(parsed, request):
    """Map observed page-reader replies onto the existing captured-page shape."""
    tool = request["tool"]
    if (tool not in {"discolike_extract", "generic_http_request"}
            or not isinstance(parsed, dict) or parsed.get("status") != "completed"):
        return parsed
    envelope = parsed.get("toolResponse")
    raw = envelope.get("rawV2") if isinstance(envelope, dict) else None
    payload = request.get("payload", {})
    if not isinstance(raw, dict) or not isinstance(payload, dict):
        return parsed
    for part in (parsed, envelope, raw):
        status = _structured_status(part)
        if (part.get("ok") is False or part.get("success") is False
                or status not in (None, "ok") or "status" in part and status is None):
            return parsed
    url = payload.get("url")
    if tool == "discolike_extract" and url is None:
        domain = payload.get("domain")
        # The domain-only endpoint returns homepage text without a URL. Bind
        # only a literal hostname, never a path, redirect or inferred subpage.
        if (isinstance(domain, str) and len(domain) <= 253 and re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+", domain)):
            url = "https://" + domain.lower()
    try:
        address = urlparse(url) if isinstance(url, str) else None
        if address is None or address.scheme not in {"http", "https"} or not address.hostname:
            return parsed
    except ValueError:
        return parsed
    if tool == "discolike_extract":
        # This extractor omits the URL; bind its text to the executed request.
        if not isinstance(raw.get("language"), str):
            return parsed
        page = {"success": True, "metadata": {"sourceURL": url}, "text": raw.get("text")}
    else:
        if (raw.get("provider") != "generic_http" or raw.get("operation") != tool
                or raw.get("method") != "GET" or payload.get("method", "GET") != "GET"
                or raw.get("requested_url") != url
                or raw.get("ok") is not True
                or not isinstance(raw.get("headers"), dict)):
            return parsed
        content_type = next((v for k, v in raw["headers"].items() if k.lower() == "content-type"), "")
        media_type = content_type.split(";", 1)[0].strip().lower() if isinstance(content_type, str) else ""
        content_format = {"text/html": "html", "application/xhtml+xml": "html", "text/plain": "text"}.get(media_type)
        if content_format is None:
            return parsed
        page = {"metadata": {"sourceURL": raw.get("final_url"), "statusCode": raw.get("status_code")},
                content_format: raw.get("data")}
    if _scraped_document(page) is None:
        return parsed
    # Do not mutate the captured response or replace its billing/request IDs.
    return dict(parsed, toolResponse={**envelope, "rawV2": {"results": [page]}})


class InputError(ValueError):
    """An invalid local request."""


class ConfigError(RuntimeError):
    """A local invocation/configuration failure."""


class CallTimeout(RuntimeError):
    """The bounded Deepline process timeout elapsed."""

    def __init__(self, message, stdout="", stderr=""):
        super().__init__(message)
        self.stdout = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else (stdout or "")
        self.stderr = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")


def _redact_schema(value: Any, sensitive: bool = False) -> Any:
    """Preserve schema structure while removing credentials in literal values."""
    if not isinstance(value, dict):
        return value if isinstance(value, bool) else redact(value)
    result = {}
    for key, item in value.items():
        if key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"} and isinstance(item, dict):
            result[key] = {
                name: (_redact_schema(schema, sensitive or bool(_SECRET_KEY.search(name)))
                       if isinstance(schema, (dict, bool)) else "[REDACTED]")
                for name, schema in item.items()
            }
        elif key in {"items", "additionalItems", "additionalProperties", "contains", "propertyNames",
                     "not", "if", "then", "else", "unevaluatedItems", "unevaluatedProperties"}:
            result[key] = ([_redact_schema(child, sensitive) for child in item]
                           if isinstance(item, list) else _redact_schema(item, sensitive))
        elif key in {"allOf", "anyOf", "oneOf", "prefixItems"} and isinstance(item, list):
            result[key] = [_redact_schema(child, sensitive) for child in item]
        elif sensitive and key in {"default", "examples"}:
            continue  # Optional annotations can contain an actual credential.
        elif sensitive and key in {"const", "enum"}:
            # Do not publish a credential or silently remove its input constraint.
            return False
        elif _SECRET_KEY.search(key):
            result[key] = "[REDACTED]"
        else:
            result[key] = redact(item)
    return result


def redact(value: Any) -> Any:
    """Remove likely credentials from arbitrary provider data before output."""

    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                result[str(key)] = "[REDACTED]"
            elif key == "jsonSchema" and isinstance(item, (dict, bool)):
                result[str(key)] = _redact_schema(item)
            else:
                result[str(key)] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = _SECRET_HEADER.sub(r"\1[REDACTED]", value)
        value = _BEARER.sub(r"\1[REDACTED]", value)
        value = _URL_SECRET.sub(lambda match: match.group(1).split("=", 1)[0] + "=[REDACTED]", value)
        return _INLINE_SECRET.sub(r"\1[REDACTED]", value)
    return value


def _safe_error(message: str) -> Dict[str, str]:
    """Return one bounded, redacted failure line without echoing the command."""

    redacted = str(redact(message or ""))
    lines = [re.sub(r"\s+", " ", line).strip() for line in redacted.splitlines()]
    lines = [line for line in lines if line]
    diagnostic = next(
        (
            line
            for line in lines
            if any(
                marker in line.lower()
                for marker in ("error", "failed", "unknown", "invalid", "not found")
            )
        ),
        lines[0] if lines else "Deepline command failed",
    )
    if len(diagnostic) > 500:
        diagnostic = diagnostic[:497].rstrip() + "..."
    return {"message": diagnostic}


def _json_from_text(text: str, *, billing_feed=False) -> Any:
    """Decode JSON despite a CLI notice before the JSON payload."""

    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty provider response")
    # Preserve undecodable CLI bytes in the receipt, never as a parsed result.
    text.encode("utf-8")
    try:
        return load_json(text, billing_feed=billing_feed)
    except json.JSONDecodeError:
        if text.lstrip().startswith(("{", "[")):
            raise
    # Skip a leading CLI notice, never a malformed enclosing JSON document.
    start = re.search(r"(?m)^[ \t]*[\[{]", text)
    if start:
        return load_json(text[start.start():], billing_feed=billing_feed)
    raise ValueError("provider response was not JSON")


def _first(mapping: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = _first(value, "name", "value", "text", "title", "url")
    if value is None:
        return None
    return str(value).strip() or None


def _domain(value: Any) -> Optional[str]:
    text = _text(value)
    if not text:
        return None
    candidate = text if "://" in text else "https://" + text
    host = urlparse(candidate).hostname
    if host:
        host = host.lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        return host
    return text.lower().strip().strip("/")


def _is_linkedin_url(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    candidate = text if "://" in text else "https://" + text
    host = urlparse(candidate).hostname
    return bool(host) and (
        host.lower() == "linkedin.com" or host.lower().endswith(".linkedin.com")
    )


# LinkedIn company records sometimes give their website wrapped in one of these redirect pages.
LINKEDIN_REDIRECTS = frozenset({"/redir/redirect", "/redir/suspicious-page"})


def linkedin_redirect_target(value: Any) -> Any:
    """The one destination a LinkedIn redirect wrapper names in url=, read and never followed; None when it names
    none or several. Any other value is returned unchanged."""
    text = _text(value)
    if not _is_linkedin_url(text):
        return value
    parsed = urlsplit(text if "://" in text else "https://" + text)
    if parsed.path not in LINKEDIN_REDIRECTS:
        return value
    targets = parse_qs(parsed.query).get("url", [])
    if len(targets) != 1:
        return None
    try:  # A destination urllib cannot parse names no domain; it never stops normalization.
        urlsplit(targets[0] if "://" in targets[0] else "https://" + targets[0])
    except ValueError:
        return None
    return targets[0]


def _is_linkedin_company_url(value: Any) -> bool:
    """Return whether a URL is a LinkedIn company page, not a person profile."""

    text = _text(value)
    if not text:
        return False
    candidate = text if "://" in text else "https://" + text
    parsed = urlparse(candidate)
    host = parsed.hostname
    return bool(host) and (
        host.lower() == "linkedin.com" or host.lower().endswith(".linkedin.com")
    ) and parsed.path.lower().startswith("/company/")


def _artifact_refs(row: Dict[str, Any]) -> Any:
    for key in (
        "raw_artifact_refs",
        "raw_artifact_ref",
        "artifact_refs",
        "artifact_ref",
        "artifact",
        "rawV2",
        "raw_v2",
        "raw",
    ):
        if key in row and row[key] not in (None, "", [], {}):
            return redact(row[key])
    return None


def normalize_evidence(
    row: Any,
    provider: str = "deepline",
    tool: Optional[str] = None,
    entity_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize one provider row while retaining useful provider metadata."""

    # Result lists bypass the envelope parser's single-document path.
    captured = _scraped_document(row)
    source: Dict[str, Any] = captured or (row if isinstance(row, dict) else {"value": row})
    company_search = tool in {"peopledatalabs_company_search", "crustdata_v3_company_search", "fullenrich_company_search"}
    if company_search:
        # Company search fields describe firms, not people or buying signals.
        entity_type = "company"
        source = dict(source, company_name=source.get("name"))
    if tool == "crustdata_v3_company_search" and isinstance(source.get("basic_info"), dict):
        source = dict(source, company_linkedin_url=source["basic_info"].get("professional_network_url"))
    elif tool == "fullenrich_company_search":
        social = source.get("social_profiles")
        network = social.get("professional_network") if isinstance(social, dict) else None
        if isinstance(network, dict):
            source = dict(source, company_linkedin_url=network.get("url"))
    if tool == "crustdata_v3_job_search" and isinstance(source.get("company"), dict):
        company = source["company"]
        info = company.get("basic_info") if isinstance(company.get("basic_info"), dict) else {}
        job = source.get("job_details") if isinstance(source.get("job_details"), dict) else {}
        # Keep the firm's metrics and original job metadata available for review.
        # Indexing dates are not publication dates or evidence of an open vacancy.
        source = dict(source, company_details=company, company=info.get("name"),
                      domain=info.get("primary_domain") or info.get("website"), evidence_url=job.get("url"))
    result: Dict[str, Any] = redact(source)
    basic_info = source.get("basic_info")
    basic_info = basic_info if isinstance(basic_info, dict) else {}
    summary = source.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    link = source.get("link")
    link = link if isinstance(link, dict) else {}
    nested_company_identity = (
        _text(summary.get("name")) is not None
        and any(_text(link.get(key)) is not None for key in ("domain", "website"))
        and _is_linkedin_company_url(link.get("linkedin"))
    )
    result["company"] = _text(
        _first(source, "company", "company_name", "account", "organization")
    ) or _text(_first(basic_info, "name", "company", "company_name")) or (
        _text(summary.get("name")) if nested_company_identity else None)
    result["company_linkedin_url"] = _text(
        _first(source, "company_linkedin_url", "companyLinkedinUrl")
    ) or (_text(link.get("linkedin")) if nested_company_identity else None)
    linkedin = _first(source, "linkedin_url", "linkedinUrl")
    if not result["company_linkedin_url"] and _is_linkedin_company_url(linkedin):
        result["company_linkedin_url"] = _text(linkedin)
    domain_value = _first(source, "domain", "company_domain")
    if _is_linkedin_url(domain_value):
        domain_value = None
    if domain_value in (None, ""):
        for key in ("website", "company_url"):
            candidate = linkedin_redirect_target(source.get(key))
            if candidate not in (None, "") and not _is_linkedin_url(candidate):
                domain_value = candidate
                break
    if domain_value in (None, ""):
        domain_value = _first(
            basic_info, "primary_domain", "domain", "website", "company_url"
        )
    if domain_value in (None, "") and nested_company_identity:
        domain_value = _first(link, "domain", "website")
    # The destination's domain, never LinkedIn's own; the caller still matches it to the company.
    domain_value = linkedin_redirect_target(domain_value)
    result["domain"] = None if _is_linkedin_url(domain_value) else _domain(domain_value)
    result["signal"] = None if company_search else _text(_first(source, "signal", "signal_type", "intent", "type", "category"))
    result["evidence_url"] = _text(_first(source, "evidence_url", "source_url", "url", "link", "source"))
    result["evidence_date"] = _text(_first(source, "evidence_date", "date", "published_at", "published", "timestamp"))
    result["evidence_text"] = _text(_first(source, "evidence_text", "text", "snippet", "description", "evidence", "content"))
    result["provider"] = _text(_first(source, "provider")) or provider
    result["tool"] = _text(_first(source, "tool", "tool_name")) or tool

    if tool == "aviato_get_company_funding_rounds":
        # announcedOn is the financing announcement, not an ingestion timestamp.
        # Preserve the original fields and order; the researcher judges current
        # stage and reconciles conflicting records.
        result["evidence_date"] = None
        result["evidence_date_basis"] = "published"
        announced = _text(source.get("announcedOn"))
        if announced:
            try:
                result["evidence_date"] = datetime.fromisoformat(announced.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                pass
        result["evidence_text"] = result["evidence_text"] or _text(source.get("name"))

    # LinkedIn company page fields: the published employee band, not an associated-member count.
    # Preserve the raw HarvestAPI fields alongside the stable output.
    if "harvestapi" in (tool or "").lower():
        linkedin = _text(source.get("linkedinUrl"))
        if _is_linkedin_company_url(linkedin):
            result["company"] = _text(source.get("name")) or result["company"]
            result["company_linkedin_url"] = linkedin
            band = source.get("employeeCountRange")
            if isinstance(band, dict):
                start, end = band.get("start"), band.get("end")
                if type(start) is int and start >= 0 and (end is None and start == 10001 or type(end) is int and end >= start):
                    result["employee_range"] = f"{start}-{end}" if end is not None else f"{start}+"
            result["missing_fields"] = [field for field in ("company", "employee_range") if not result.get(field)]
    if entity_type:
        result["entity_type"] = entity_type
    elif result.get("company"):
        result["entity_type"] = "company"
    refs = _artifact_refs(source)
    if refs is not None:
        result["raw_artifact_refs"] = refs
    # Provenance comes from the adapter, never from an authored evidence label.
    result["content_kind"] = ("captured_page" if captured else "structured_record" if tool in STRUCTURED_RECORD_TOOLS
                              else "search_excerpt" if result.get("evidence_url") and result.get("evidence_text")
                              else "unverified")
    return result


def _jsonapi_resource(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("type"), str)
        and isinstance(value.get("attributes"), dict)
        and (
            "relationships" not in value
            or isinstance(value.get("relationships"), dict)
        )
    )


def _is_linkedin_post_url(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    parsed = urlparse(text if "://" in text else "https://" + text)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return (host == "linkedin.com" or host.endswith(".linkedin.com")) and path.startswith(
        ("/posts/", "/feed/update/", "/pulse/")
    )


def _structured_execute_envelope(value: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Select a complete structured provider result for execute only."""

    if isinstance(value, str):
        try:
            value = _json_from_text(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    resources = data if isinstance(data, list) else [data]
    if (
        isinstance(data, (dict, list))
        and all(_jsonapi_resource(resource) for resource in resources)
        and ("included" not in value or isinstance(value.get("included"), list))
    ):
        return "jsonapi", value
    elements = value.get("elements")
    if isinstance(elements, list) and all(
        isinstance(post, dict)
        and isinstance(post.get("id"), str)
        and _is_linkedin_post_url(post.get("linkedinUrl"))
        and any(
            key in post
            for key in ("content", "author", "postedAt", "repost", "repostedBy")
        )
        for post in elements
    ):
        return "harvest", value
    for key in ("toolResponse", "tool_response", "rawV2", "raw_v2", "result", "response", "output", "raw"):
        selected = _structured_execute_envelope(value.get(key))
        if selected:
            return selected
    return None


def _included_index(included: Any) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], set]:
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    conflicts = set()
    for resource in included if isinstance(included, list) else []:
        if not _jsonapi_resource(resource):
            continue
        key = (resource["type"], resource["id"])
        if key in index and index[key] != resource:
            conflicts.add(key)
        else:
            index[key] = resource
    return index, conflicts


def _relationship_refs(resource: Dict[str, Any], relationship: str) -> List[Dict[str, Any]]:
    relationships = resource.get("relationships", {})
    relation = relationships.get(relationship) if isinstance(relationships, dict) else None
    data = relation.get("data") if isinstance(relation, dict) else None
    refs = data if isinstance(data, list) else [data]
    return [
        ref
        for ref in refs
        if isinstance(ref, dict)
        and isinstance(ref.get("type"), str)
        and isinstance(ref.get("id"), str)
    ]


def _normalize_jsonapi(
    envelope: Dict[str, Any], tool: str, entity_type: Optional[str]
) -> List[Dict[str, Any]]:
    data = envelope.get("data")
    resources = data if isinstance(data, list) else [data]
    included, conflicts = _included_index(envelope.get("included"))
    results = []
    for resource in resources:
        attributes = resource["attributes"]
        row = redact(resource)
        related_companies = []
        company_keys = set()
        unresolved_company = False
        for relationship, relation in resource.get("relationships", {}).items():
            relation_data = relation.get("data") if isinstance(relation, dict) else None
            relation_refs = relation_data if isinstance(relation_data, list) else [relation_data]
            if any(
                isinstance(ref, dict)
                and ref.get("type") == "company"
                and not isinstance(ref.get("id"), str)
                for ref in relation_refs
            ):
                unresolved_company = True
            for ref in _relationship_refs(resource, relationship):
                if ref["type"] != "company":
                    continue
                key = (ref["type"], ref["id"])
                linked = None if key in conflicts else included.get(key)
                linked_attributes = linked.get("attributes", {}) if linked else {}
                related_companies.append(
                    {
                        "relationship": relationship,
                        "id": ref["id"],
                        "company": _text(linked_attributes.get("company_name")),
                        "domain": _domain(linked_attributes.get("domain")),
                    }
                )
                company_keys.add(key)
                unresolved_company = unresolved_company or linked is None
        source = None
        source_refs = _relationship_refs(resource, "most_relevant_source")
        if len(source_refs) == 1 and source_refs[0]["type"] == "news_article":
            key = (source_refs[0]["type"], source_refs[0]["id"])
            if key not in conflicts:
                source = included.get(key)
        source_attributes = source.get("attributes", {}) if source else {}
        row.update(
            {
                "company": None,
                "domain": None,
                "signal": _text(attributes.get("category")) or resource["type"],
                "evidence_url": _text(source_attributes.get("url"))
                or _text(attributes.get("url")),
                "evidence_date": _text(source_attributes.get("published_at"))
                or _text(attributes.get("published_at")),
                "evidence_text": _text(
                    _first(
                        attributes,
                        "article_sentence",
                        "summary",
                        "description",
                        "content",
                        "title",
                    )
                ),
                "provider": "deepline",
                "tool": tool,
                "entity_type": entity_type or "signal",
            }
        )
        if "effective_date" in attributes:
            row["event_date"] = _text(attributes.get("effective_date"))
        if related_companies:
            row["related_companies"] = related_companies
        if len(company_keys) == 1 and not unresolved_company:
            row["company"] = related_companies[0]["company"]
            row["domain"] = related_companies[0]["domain"]
        if source is not None:
            row["related_source"] = redact(source)
        results.append(redact(row))
    return results


def _normalize_harvest(
    envelope: Dict[str, Any], tool: str, entity_type: Optional[str]
) -> List[Dict[str, Any]]:
    results = []
    for post in envelope["elements"]:
        author = post.get("author")
        author = author if isinstance(author, dict) else {}
        author_url = _text(author.get("linkedinUrl"))
        company_author = _is_linkedin_company_url(author_url)
        posted_at = post.get("postedAt")
        posted_at = posted_at if isinstance(posted_at, dict) else {}
        row = redact(post)
        row.update(
            {
                "company": _text(author.get("name")) if company_author else None,
                "domain": None,
                "company_linkedin_url": author_url if company_author else None,
                "signal": "linkedin_post",
                "evidence_url": _text(post.get("linkedinUrl")),
                "evidence_date": _text(posted_at.get("date")),
                "evidence_text": _text(post.get("content")),
                "provider": "deepline",
                "tool": tool,
                "entity_type": entity_type or "signal",
            }
        )
        results.append(redact(row))
    return results


def _structured_metadata(kind: str, envelope: Dict[str, Any]) -> Dict[str, Any]:
    metadata = {
        key: redact(envelope[key])
        for key in ("meta", "links", "pagination")
        if key in envelope
    }
    pagination = envelope.get("pagination")
    if kind == "harvest" and isinstance(pagination, dict):
        cursor = pagination.get("paginationToken")
        if isinstance(cursor, str) and cursor:
            safe_pagination = metadata.setdefault("pagination", {})
            if isinstance(safe_pagination, dict):
                safe_pagination["next_cursor"] = redact(cursor)
    return metadata


def _output_preview_metadata(value: Any) -> Dict[str, Any]:
    """Retain observed preview counts without inferring provider completeness."""

    if not isinstance(value, dict):
        return {}
    for key in ("output_preview", "outputPreview"):
        preview = value.get(key)
        if not isinstance(preview, dict):
            continue
        metadata = {
            field: redact(preview[field])
            for field in ("kind", "rowCount", "columns")
            if field in preview
        }
        rows = next(
            (
                preview.get(field)
                for field in ("rows", "preview", "items")
                if isinstance(preview.get(field), list)
            ),
            None,
        )
        if rows is not None:
            metadata["returnedRowCount"] = len(rows)
        return {"output_preview": metadata} if metadata else {}
    return {}


def _structured_status(envelope: Dict[str, Any]) -> Optional[str]:
    """Read route failure state only from a selected provider envelope."""

    status = envelope.get("status")
    if status is not None and (
        isinstance(status, bool) or not isinstance(status, (str, int, float))
    ):
        return "schema_error"
    error = envelope.get("error") or envelope.get("errors")
    diagnostic = json.dumps(redact(error), ensure_ascii=False) if error not in (None, "", [], {}) else ""
    if status == 429:
        return "rate_limited"
    if status == 401 or status == 403:
        return "auth_failed"
    if isinstance(status, (int, float)) and not isinstance(status, bool) and status >= 400:
        return "provider_error"
    if isinstance(status, str):
        normalized = status.strip().lower().replace("-", "_")
        if normalized in _FAILURE_STATUSES:
            return normalized
        if normalized in {"failed", "failure", "error"}:
            return _classify_error(diagnostic) if diagnostic else "provider_error"
        if normalized.isdigit() and int(normalized) >= 400:
            return _classify_error(normalized + " " + diagnostic)
    if diagnostic:
        return _classify_error(diagnostic)
    if envelope.get("partial") is True:
        return "partial"
    if isinstance(status, str) and normalized in _STATUS_WORDS:
        return normalized
    if isinstance(status, str) and normalized in {"success", "succeeded", "complete", "completed"}:
        return "ok"
    return None


def _records(value: Any) -> List[Any]:
    """Extract rows from common Deepline response envelopes."""

    if isinstance(value, str):
        try:
            return _records(_json_from_text(value))
        except ValueError:
            return []
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    # Firecrawl's declared web list can be larger than its CLI preview.
    # Do not follow arbitrary paths: getters also preview unrelated lists
    # such as profile interests and similar companies.
    preview = value.get("output_preview", {})
    source_path = preview.get("listSourcePath") if isinstance(preview, dict) else None
    if source_path == "toolResponse.rawV2.data.web" and "results" not in value:
        full = value
        for key in source_path.split("."):
            full = full.get(key) if isinstance(full, dict) else None
        if isinstance(full, list) and all(isinstance(row, dict) for row in full):
            return full
    document = _scraped_document(value)
    if document is not None:
        return [document]
    empty_direct: Optional[List[Any]] = None
    for key in (
        "evidence",
        "results",
        "items",
        "records",
        "rows",
        "matches",
        "tools",
        "getters",
        "elements",
        "suggestions",
    ):
        candidate = value.get(key)
        if isinstance(candidate, list):
            if candidate:
                return candidate
            empty_direct = candidate
    for key in ("extractedLists", "extracted_lists"):
        candidate = value.get(key)
        if isinstance(candidate, (dict, list)):
            records = _extracted_list_records(candidate)
            if records:
                return records
    for key in (
        "data",
        "output",
        "result",
        "response",
        "summary",
        "toolResponse",
        "tool_response",
        "rawV2",
        "raw_v2",
        "raw",
        "getters",
        "element",
    ):
        candidate = value.get(key)
        if isinstance(candidate, (dict, list, str)):
            found = _records(candidate)
            if found:
                return found
    # The CLI emits a bounded row preview when it materializes a declared list
    # but the raw provider envelope does not contain the list inline.
    for key in ("output_preview", "outputPreview"):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            for preview_key in ("rows", "preview", "items"):
                rows = candidate.get(preview_key)
                if isinstance(rows, list):
                    return rows
            summary = candidate.get("summary")
            scalar = _scalar_result(summary)
            if scalar is not None:
                return [scalar]
    if empty_direct is not None:
        return empty_direct
    # A single row is useful for a provider that returns one company/evidence object.
    if any(
        key in value
        for key in (
            "company",
            "company_name",
            "domain",
            "signal",
            "evidence_text",
            "url",
        )
    ) or _is_linkedin_company_url(_first(value, "linkedin_url", "linkedinUrl")):
        return [value]
    scalar = _scalar_result(value)
    if scalar is not None:
        return [scalar]
    return []


def _execution_metadata(parsed: Any) -> Dict[str, Any]:
    metadata = _output_preview_metadata(parsed)
    if isinstance(parsed, dict):
        for container in (parsed, parsed.get("tool_error"), parsed.get("error"), parsed.get("summary")):
            if not isinstance(container, dict):
                continue
            for key in ("job_id", "request_id"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    metadata.setdefault(key, value)
            if isinstance(container.get("requestId"), str) and container["requestId"].strip():
                metadata.setdefault("request_id", container["requestId"])
    billing = parsed.get("billing") if isinstance(parsed, dict) else None
    if isinstance(billing, dict):
        amounts = {}
        for key in ("credits_charged", "cost_usd"):
            if key not in billing:
                continue
            try:
                value = billing[key]
                if billing_amount(value, key) > billing_amount(sys.float_info.max, key):
                    raise ValueError("billing amount exceeds the report range")
                amounts[key] = value
            except ValueError:
                amounts = {}  # Contradictory/invalid billing must not partially settle.
                break
        if amounts:
            metadata["billing"] = amounts
            # Price finality and posting are different: a final price can be
            # queued for ledger posting. Keep both facts in the saved receipt.
            for key in ("pricing_status", "settlement_status"):
                if isinstance(billing.get(key), str):
                    metadata["billing"][key] = billing[key]
            metadata["billing_final"] = billing.get("pricing_status") == "final"
    return metadata


def _envelope_status(value: Any) -> Optional[str]:
    """Read status only from response envelopes, never from result rows."""

    def direct_status(mapping: Dict[str, Any]) -> Optional[str]:
        for key in ("status", "provider_status", "outcome", "state"):
            candidate = mapping.get(key)
            if isinstance(candidate, str):
                lowered = candidate.strip().lower().replace("-", "_")
                if lowered in _STATUS_WORDS:
                    return lowered
                if lowered in {"success", "succeeded", "complete", "completed"}:
                    return "ok"
                if lowered in {"empty", "none", "not_found", "notfound", "no_result"}:
                    return "no_results"
                if lowered in {"failed", "failure", "error"}:
                    return "provider_error"
        if mapping.get("success") is False or mapping.get("ok") is False:
            detail = mapping.get("error") or mapping.get("errors") or mapping.get("message") or ""
            return _classify_error(json.dumps(redact(detail), ensure_ascii=False)) if detail else "provider_error"
        if mapping.get("partial") is True:
            return "partial"
        if mapping.get("error") not in (None, "", [], {}):
            return _classify_error(json.dumps(redact(mapping.get("error")), ensure_ascii=False))
        if mapping.get("errors") not in (None, "", [], {}):
            return _classify_error(json.dumps(redact(mapping.get("errors")), ensure_ascii=False))
        return None

    if not isinstance(value, dict):
        return None
    status = direct_status(value)
    if status:
        return status
    # toolResponse is the only nested object treated as an envelope.  rawV2,
    # raw, getters, and result/data payloads may themselves contain a row whose
    # `status` field is provider data, not a route-level failure.
    for key in ("toolResponse", "tool_response"):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            status = direct_status(candidate)
            if status:
                return status
    return None


def _envelope_error(value: Any) -> Optional[Dict[str, str]]:
    """Extract bounded, redacted diagnostics from recognized response envelopes."""

    if not isinstance(value, dict):
        return None

    def error_value(mapping: Dict[str, Any]) -> Any:
        for key in ("error", "errors"):
            candidate = mapping.get(key)
            if candidate not in (None, "", [], {}):
                return candidate
        if mapping.get("success") is False or mapping.get("ok") is False:
            return _first(mapping, "message", "detail", "description")
        return None

    candidate = error_value(value)
    if candidate in (None, "", [], {}):
        for key in ("toolResponse", "tool_response"):
            nested = value.get(key)
            if isinstance(nested, dict):
                candidate = error_value(nested)
                if candidate not in (None, "", [], {}):
                    break
    if candidate in (None, "", [], {}):
        return None
    message = (_first(candidate, "message", "detail", "description", "code") or candidate
               if isinstance(candidate, dict) else candidate)
    error = _safe_error(message if isinstance(message, str) else json.dumps(message, ensure_ascii=False))
    if isinstance(candidate, dict):
        for key in ("code", "details"):
            detail = candidate.get(key)
            if detail not in (None, "", [], {}):
                error[key] = _safe_error(detail if isinstance(detail, str)
                                         else json.dumps(redact(detail), ensure_ascii=False))["message"]
    return error


def _known_envelope(value: Any) -> bool:
    """Return whether the CLI response has a recognized envelope shape."""

    if isinstance(value, list):
        return True
    if not isinstance(value, dict):
        if isinstance(value, str):
            try:
                return _known_envelope(_json_from_text(value))
            except ValueError:
                return False
        return False
    if _scalar_result(value) is not None or _scraped_document(value) is not None:
        return True
    # Observed single-object getter success: explicit null is a miss, not a
    # malformed response. Missing payloads or nested failures stay unrecognized.
    if (set(value) <= {"error", "status", "element"} and "element" in value
            and value["element"] is None and value.get("status") == 200
            and value.get("error") is None):
        return True
    if any(
        key in value
        for key in (
            "evidence",
            "results",
            "items",
            "records",
            "rows",
            "matches",
            "tools",
            "elements",
            "suggestions",
        )
    ):
        return True
    if any(
        key in value
        for key in (
            "company",
            "company_name",
            "domain",
            "signal",
            "evidence_text",
            "evidence_url",
        )
    ) or _is_linkedin_company_url(_first(value, "linkedin_url", "linkedinUrl")):
        return True
    for key in ("extractedLists", "extracted_lists"):
        if key in value:
            candidate = value[key]
            if candidate in (None, ""):
                return True
            if isinstance(candidate, (dict, list)):
                return _is_extracted_list_envelope(candidate)
    for key in ("output_preview", "outputPreview"):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            if any(isinstance(candidate.get(preview_key), list) for preview_key in ("rows", "preview", "items")):
                return True
    for key in (
        "toolResponse",
        "tool_response",
        "response",
        "summary",
        "data",
        "output",
        "result",
        "rawV2",
        "raw_v2",
        "raw",
        "getters",
        "element",
    ):
        if key in value:
            candidate = value[key]
            if key in _EMPTY_CONTAINER_KEYS and candidate in (None, "", [], {}):
                return True
            if isinstance(candidate, (dict, list, str)) and _known_envelope(candidate):
                return True
    return False


def _classify_error(text: str, timed_out: bool = False) -> str:
    if (
        timed_out
        or "timeout" in text.lower()
        or "timed out" in text.lower()
        or "timed_out" in text.lower()
    ):
        return "timeout"
    lowered = text.lower()
    if any(marker in lowered for marker in ("429", "rate limit", "rate_limit", "too many requests")):
        return "rate_limited"
    if any(
        marker in lowered
        for marker in (
            "quota",
            "credit balance",
            "credits exhausted",
            "insufficient credits",
            "insufficient_credits",
            "insufficient balance",
            "insufficient_balance",
        )
    ):
        return "quota_exceeded"
    if any(
        marker in lowered
        for marker in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "authentication",
            "invalid api key",
            "auth failed",
            "auth_failed",
            "not connected",
            "missing credentials",
        )
    ):
        return "auth_failed"
    if any(
        marker in lowered
        for marker in (
            "invalid_json",
            "validation_error",
            "schema error",
            "schema_error",
            "invalid input",
            "invalid_input",
        )
    ):
        return "schema_error"
    return "provider_error"


def _timeout_seconds(value: Any, default: float = 30.0, maximum: float = 120.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InputError("timeout_seconds must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise InputError("timeout_seconds must be greater than zero")
    return min(number, maximum)


def _result_limit(value: Any, default: int = 10, maximum: int = 10) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise InputError("limit must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InputError("limit must be an integer") from exc
    if number != value or number <= 0:
        raise InputError("limit must be a positive integer")
    return min(number, maximum)


def _validate_request(request: Any) -> Dict[str, Any]:
    if not isinstance(request, dict):
        raise InputError("input must be a JSON object")
    operation = request.get("operation", request.get("op"))
    if not isinstance(operation, str):
        raise InputError("operation is required")
    operation = operation.strip().lower().replace("-", "_")
    aliases = {
        "catalog_search": "search",
        "tools_search": "search",
        "catalog_describe": "describe",
        "tool_describe": "describe",
    }
    operation = aliases.get(operation, operation)
    if operation not in {"search", "describe", "execute"}:
        raise InputError("operation must be search, describe, or execute")
    request = dict(request)
    request["operation"] = operation
    if "entity_type" in request:
        entity_type = request["entity_type"]
        if not isinstance(entity_type, str) or not entity_type.strip():
            raise InputError("entity_type must be a non-empty string")
        # This is wrapper metadata only. It is deliberately not added to the
        # payload sent to the live Deepline tool, whose schema is discovered at
        # runtime and must not be guessed here.
        request["entity_type"] = entity_type.strip()
    if operation == "search":
        query = request.get("query", request.get("q"))
        if not isinstance(query, str) or not query.strip():
            raise InputError("search requires a non-empty query")
        request["query"] = query.strip()
    elif operation == "describe":
        tool = request.get("tool", request.get("name"))
        if not isinstance(tool, str) or not tool.strip():
            raise InputError("describe requires a tool name")
        request["tool"] = tool.strip()
    else:
        tool = request.get("tool", request.get("name"))
        payload = request.get("payload", request.get("input"))
        if not isinstance(tool, str) or not tool.strip():
            raise InputError("execute requires a tool name")
        if not isinstance(payload, dict):
            raise InputError("execute payload must be a JSON object")
        request["tool"] = tool.strip()
        request["payload"] = payload
        # This wrapper-only bound keeps every execute pilot within the skill's
        # maximum returned-row limit. It is not sent to the provider tool.
        request["limit"] = _result_limit(request.get("limit"))
    # Paid execute calls can take longer than catalog reads. A longer default
    # reduces the risk that a local timeout tempts a caller to repeat a paid
    # call whose remote outcome is unknown.
    if operation == "execute":
        request["timeout_seconds"] = _timeout_seconds(
            request.get("timeout_seconds"), default=240.0, maximum=780.0
        )
    else:
        request["timeout_seconds"] = _timeout_seconds(request.get("timeout_seconds"))
    return request


def _invoke(command: Sequence[str], timeout_seconds: float) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,  # Provider CLI must not own the MCP control pipe.
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CallTimeout("provider command timed out", exc.stdout, exc.stderr) from exc
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise ConfigError("deepline CLI could not be started") from exc
    return int(completed.returncode), completed.stdout or "", completed.stderr or ""


def _catalog_output(
    operation: str,
    parsed: Any,
    tool: Optional[str] = None,
    entity_type: Optional[str] = None,
) -> Dict[str, Any]:
    records = _records(parsed)
    envelope_status = _envelope_status(parsed)
    description_fields = {
        "toolId",
        "id",
        "name",
        "displayName",
        "description",
        "inputSchema",
        "pricing",
    }
    direct_description = (
        operation == "describe"
        and isinstance(parsed, dict)
        and bool(parsed)
        and bool(description_fields.intersection(parsed))
        and not records
        and envelope_status not in _FAILURE_STATUSES
    )
    if direct_description:
        records = [parsed]
    status = envelope_status
    if not status:
        status = "ok" if records else ("no_results" if _known_envelope(parsed) else "schema_error")
    if not _known_envelope(parsed) and not direct_description:
        status = status if status in _PROVIDER_ERROR_STATUSES else "schema_error"
    body: Dict[str, Any] = {
        "status": status,
        "provider": "deepline",
        "operation": operation,
        "results": redact(records),
    }
    if status in _FAILURE_STATUSES:
        error = _envelope_error(parsed)
        if error:
            body["error"] = error
    if tool:
        body["tool"] = tool
    if entity_type:
        body["entity_type"] = entity_type
    return body


def _native_result_envelope(parsed, tool):
    """Unwrap observed native outputs; retain IDs/billing and the raw receipt."""
    if (tool in {"aviato_get_company_funding_rounds", "deepline_native_get_funding_updates"}
            and isinstance(parsed, dict) and parsed.get("status") == "completed"):
        envelope = parsed.get("toolResponse")
        raw = envelope.get("rawV2") if isinstance(envelope, dict) else None
        if (isinstance(envelope, dict) and envelope.get("view") == "rawV2" and isinstance(raw, dict)
                and all(_structured_status(part) in (None, "ok") and part.get("success") is not False
                        and part.get("ok") is not False for part in (parsed, envelope, raw))):
            if tool == "aviato_get_company_funding_rounds":
                shape = set(raw) == {"fundingRounds", "pages", "totalResults"}
                shape = (shape and type(raw["pages"]) is int and raw["pages"] >= 0
                         and type(raw["totalResults"]) is int and raw["totalResults"] >= 0)
                rows = raw.get("fundingRounds")
            else:
                shape = (set(raw) == {"status", "code", "timestamp", "message", "updates", "has_more", "next_cursor"}
                         and raw["status"] == "success" and raw["code"] == 200
                         and type(raw["has_more"]) is bool)
                rows = raw.get("updates")
            if shape:
                def valid_row(row):
                    if not isinstance(row, dict):
                        return False
                    if tool == "aviato_get_company_funding_rounds":
                        # Incomplete older rounds remain visible; funding_record
                        # checks stage and announcement date before qualification.
                        return True
                    return (isinstance(row.get("data"), dict) and isinstance(row.get("content"), dict)
                            and all(isinstance(row.get(key), str) and row[key].strip()
                                    for key in ("record_id", "update_id", "update_type", "discovered_at")))

                if isinstance(rows, list) and all(valid_row(row) for row in rows):
                    return dict(parsed, results=rows)
                return dict(parsed, status="schema_error", results=[],
                            error=f"Expected {tool} array of objects")
    # Where each tool's result rows sit in its reply (the Glassdoor overview is one record, not a list).
    lists = {"crustdata_v3_job_search": ("job_listings",), "crustdata_v3_company_search": ("companies",),
             "fullenrich_company_search": ("companies",), "bloomberry_search_job_postings": ("jobs",),
             "openwebninja_glassdoor_company_reviews": ("data", "data", "reviews"),
             "openwebninja_glassdoor_company_overview": ("data", "data"), "sentrion_company_jobs_search": ("data", "jobs"),
             "forager_job_search": ("search_results",)}
    if (tool not in {"firecrawl_search", *lists}
            or not isinstance(parsed, dict)
            or parsed.get("status") not in {"completed", "no_result"} or _structured_status(parsed) not in {"ok", None}
            or tool == "firecrawl_search" and parsed.get("status") != "completed"):
        return parsed
    raw = parsed.get("toolResponse", {}).get("rawV2") if isinstance(parsed.get("toolResponse"), dict) else None
    if tool in lists:
        # Every envelope on the way to the rows can carry the provider's failure (Glassdoor's sits in rawV2.data).
        rows, parts = raw, [parsed, parsed.get("toolResponse"), raw.get("meta") if isinstance(raw, dict) else None]
        for key in lists[tool]:
            parts.append(rows)
            rows = rows.get(key) if isinstance(rows, dict) else None
        if isinstance(rows, dict):
            parts.append(rows)  # A single record (the Glassdoor overview) can carry the failure itself.
        for part in parts:
            # A miss explains an empty list; it never excuses an error beside rows.
            if not isinstance(part, dict) or not rows and _explained_miss(part):
                continue
            status = _structured_status(part)
            if status in _FAILURE_STATUSES or part.get("success") is False or part.get("ok") is False:
                return dict(parsed, status=status if status in _FAILURE_STATUSES else "provider_error",
                            error=_envelope_error(part), results=[])
        if tool == "openwebninja_glassdoor_company_overview" and isinstance(rows, dict):
            rows = [rows] if rows else []
        if rows is None and parsed.get("status") == "no_result":
            return parsed  # Deepline's own miss can arrive without the list.
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return dict(parsed, status="schema_error", results=[],
                        error=f"Expected {tool} {'.'.join(lists[tool])} array of objects")
        return dict(parsed, results=rows)
    # Native search returns web/news lists without a CLI list preview.
    # Keep every returned row and the original envelope, including billing.
    data = raw.get("data") if isinstance(raw, dict) else None
    meta = raw.get("meta") if isinstance(raw, dict) else None
    for part in (parsed, parsed.get("toolResponse"), raw, meta):
        if not isinstance(part, dict):
            continue
        status = _structured_status(part)
        if status in _FAILURE_STATUSES or part.get("success") is False or part.get("ok") is False:
            return dict(parsed, status=status if status in _FAILURE_STATUSES else "provider_error",
                        error=_envelope_error(part), results=[])
    if (not isinstance(data, dict) or not data or set(data) - {"web", "news"}
            or not isinstance(meta, dict) or meta.get("status") != 200
            or meta.get("success") is not True
            or any(_structured_status(part) not in (None, "ok")
                   for part in (parsed, parsed["toolResponse"], raw, meta))):
        return parsed
    rows = []
    for kind in ("web", "news"):
        found = data.get(kind, [])
        if not isinstance(found, list):
            return parsed
        for row in found:
            if (not isinstance(row, dict) or not isinstance(row.get("url"), str)
                    or not isinstance(row.get("title"), str) or not row["title"].strip()):
                return parsed
            try:
                address = urlparse(row["url"])
                if address.scheme not in {"http", "https"} or not address.hostname:
                    return parsed
            except ValueError:
                return parsed
            rows.append(row)
    return dict(parsed, results=rows)


def _explained_miss(envelope):
    """A reply that reports success with empty result lists and says nothing was found ("No jobs found for the given
    company information.") is a miss, not a failure. Any other message, or one naming a known failure, still fails."""
    return (isinstance(envelope, dict) and envelope.get("success") is True and isinstance(envelope.get("error"), str)
            and re.match(r"no\b.*\bfound\b", envelope["error"].strip(), re.IGNORECASE | re.DOTALL) is not None
            and _classify_error(envelope["error"]) == "provider_error" and isinstance(envelope.get("data"), dict)
            and not any(isinstance(value, list) and value for value in envelope["data"].values()))


def _empty_result_failure(parsed):
    """Read explicit envelope failures after extraction found no result rows.

    Follow only wrapper dictionaries, never lists or arbitrary row fields.
    """
    pending = [parsed] if isinstance(parsed, dict) else []
    while pending:
        envelope = pending.pop()
        # Some providers encode a canonical miss as a boolean error marker.
        # Accept only that exact shape; additional failure fields still apply.
        if (set(envelope) == {"error", "error_code", "status"}
                and envelope["error"] is True and envelope["error_code"] == "NO_MATCH"
                and envelope["status"] in ("no_result", "no_results")):
            continue
        status = None if _explained_miss(envelope) else _structured_status(envelope)
        if (status in _FAILURE_STATUSES or envelope.get("success") is False
                or envelope.get("ok") is False):
            error = _envelope_error(envelope)
            return (status if status in _FAILURE_STATUSES else "provider_error"), error
        pending.extend(envelope[key] for key in (
            "toolResponse", "tool_response", "rawV2", "raw", "result", "response", "data", "output"
        ) if isinstance(envelope.get(key), dict))
    return None, None


def _execute_output(
    parsed: Any,
    tool: str,
    entity_type: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    native = _native_result_envelope(parsed, tool)
    # The tool's own result list decides its rows; an empty list is not a cue to search other fields.
    unwrapped = native is not parsed and isinstance(native.get("results"), list)
    parsed = native
    structured = _structured_execute_envelope(parsed)
    metadata: Dict[str, Any] = _execution_metadata(parsed)
    if tool in {"fullenrich_company_search", "crustdata_v3_company_search"} and isinstance(parsed, dict):
        envelope = parsed.get("toolResponse")
        raw = envelope.get("rawV2") if isinstance(envelope, dict) else None
        page = raw.get("metadata") if isinstance(raw, dict) else None
        if tool == "crustdata_v3_company_search" and isinstance(raw, dict):
            page = {key: raw[key] for key in ("total_count", "next_cursor") if key in raw}
        if isinstance(page, dict):
            metadata["pagination"] = redact(page)
            if isinstance(page.get("search_after"), str) and page["search_after"]:
                metadata["pagination"]["next_cursor"] = page["search_after"]
    if structured:
        kind, envelope = structured
        records = (
            _normalize_jsonapi(envelope, tool, entity_type)
            if kind == "jsonapi"
            else _normalize_harvest(envelope, tool, entity_type)
        )
        metadata.update(_structured_metadata(kind, envelope))
    else:
        records = parsed["results"] if unwrapped else _records(parsed)
    outer_status = _envelope_status(parsed)
    selected_status = _structured_status(envelope) if structured else None
    # Harvest's single-company endpoint can wrap its failure as a one-item
    # result list. Recognize that exact shape without treating company fields
    # (or other providers' row-level statuses) as route failures.
    company_failure = None
    if (tool == "harvestapi_get_company" and len(records) == 1
            and isinstance(records[0], dict)
            and set(records[0]).issubset({"error", "status"})
            and _envelope_error(records[0]) is not None):
        company_failure = records[0]
        selected_status = _structured_status(company_failure)
    empty_status, empty_error = _empty_result_failure(parsed) if not records else (None, None)
    statuses = (empty_status, outer_status, selected_status)
    status = next(
        (candidate for candidate in statuses if candidate in _FAILURE_STATUSES),
        "partial" if "partial" in statuses else selected_status or outer_status,
    )
    if outer_status == "no_results":
        # A canonical empty outcome need not contain a row-shaped payload.
        nested = parsed.get("toolResponse", parsed.get("tool_response", {}))
        nested_status = empty_status or _envelope_status(nested)
        error = empty_error or _envelope_error(parsed)
        outcome = "schema_error" if records else "no_results"
        if nested_status in _FAILURE_STATUSES or error:
            outcome = nested_status if nested_status in _FAILURE_STATUSES else _classify_error(json.dumps(error))
        body = {"status": outcome, "provider": "deepline", "operation": "execute",
                "tool": tool, "results": [], "evidence": [], **metadata}
        if error:
            body["error"] = error
        if entity_type:
            body["entity_type"] = entity_type
        return body
    if not structured and not _known_envelope(parsed):
        body = {
            "status": status if status in _PROVIDER_ERROR_STATUSES else "schema_error",
            "provider": "deepline",
            "operation": "execute",
            "tool": tool,
            "results": [],
            "evidence": [],
        }
        error = empty_error or _envelope_error(parsed)
        if error:
            body["error"] = error
        if entity_type:
            body["entity_type"] = entity_type
        body.update(metadata)
        return body
    if status in _FAILURE_STATUSES:
        final_status = status
    elif status == "partial":
        final_status = "partial"
    elif records:
        final_status = "ok"
    else:
        final_status = "no_results"
    evidence = records if structured else [
        normalize_evidence(record, "deepline", tool, entity_type) for record in records
    ]
    if (structured or company_failure is not None) and final_status in _FAILURE_STATUSES:
        evidence = []
    body = {
        "status": final_status,
        "provider": "deepline",
        "operation": "execute",
        "tool": tool,
        "results": evidence,
        "evidence": evidence,
    }
    if final_status in _FAILURE_STATUSES:
        error = empty_error or _envelope_error(parsed) or (
            _envelope_error(envelope) if structured else None
        ) or _envelope_error(company_failure)
        if error:
            body["error"] = error
    if entity_type:
        body["entity_type"] = entity_type
    body.update(metadata)
    return body


def run(request: Dict[str, Any], capture=None) -> Tuple[Dict[str, Any], int]:
    """Run one validated request and return (JSON body, process exit code)."""

    request = _validate_request(request)
    if request["operation"] == "execute":
        from deepline_http import UNKEYED_TOOLS, api_key, new_execution_key
        try:
            key = api_key()
        except (OSError, ValueError):
            raise ConfigError("Deepline authentication could not be read; request was not sent") from None
        # Only the direct API sends an execution key; the ledger saves it before dispatch.
        execution_key = new_execution_key() if key and request["tool"] not in UNKEYED_TOOLS else None
        return guarded_call(request, "deepline",
                            lambda saved_key=None: _run_validated(request, capture, key, saved_key),
                            execution_key=execution_key)
    return _run_validated(request, capture)


def _run_validated(request: Dict[str, Any], capture=None, key=None, execution_key=None) -> Tuple[Dict[str, Any], int]:
    operation = request["operation"]
    timeout_seconds = request["timeout_seconds"]
    if operation == "execute" and key:
        from deepline_http import execute
        response = execute(request, key, execution_key)
        if capture is not None:
            capture(response)
        return normalize_response(request, response)
    deepline_bin = os.environ.get(_DEEPLINE_BIN, "").strip() or "deepline"
    if operation == "search":
        command = [deepline_bin, "tools", "search", request["query"], "--json"]
    elif operation == "describe":
        command = [deepline_bin, "tools", "describe", request["tool"], "--json"]
    else:
        payload_file = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
                json.dump(request["payload"], handle, ensure_ascii=False)
                payload_file = handle.name
            command = [
                deepline_bin,
                "tools",
                "execute",
                request["tool"],
                "--input",
                "@" + payload_file,
                "--json",
            ]
            try:
                return _run_command(request, command, timeout_seconds, capture)
            finally:
                if payload_file:
                    try:
                        os.unlink(payload_file)
                    except OSError:
                        pass
        except OSError as exc:
            raise ConfigError("could not create Deepline payload file") from exc
    return _run_command(request, command, timeout_seconds, capture)


def _run_command(request: Dict[str, Any], command: Sequence[str], timeout_seconds: float, capture=None) -> Tuple[Dict[str, Any], int]:
    timed_out = False
    try:
        returncode, stdout, stderr = _invoke(command, timeout_seconds)
    except CallTimeout as exc:
        timed_out = True
        stdout, stderr = exc.stdout, exc.stderr
    parsed: Any = None
    if stdout.strip():
        try:
            parsed = _json_from_text(stdout)
        except ValueError:
            pass
    response = {"body": response_body(parsed, stdout), "stderr": stderr}
    response.update({"timed_out": True} if timed_out else {"exit_code": returncode})
    if capture is not None:
        capture(response)
    return normalize_response(request, response)


def _completed_execute_output(parsed: Any, tool: str) -> Any:
    """Interpret observed raw API results without changing the captured receipt.

    The CLI and a hosted transport can return the same completed-job envelope.
    Keep generated answers separate from cited evidence and recognize only
    validated provider-specific result shapes. Other shapes use the normal parser.
    """
    if (not isinstance(parsed, dict)
            or parsed.get("status") != "completed"
            or not isinstance(parsed.get("job_id"), str) or not parsed["job_id"].strip()):
        return parsed
    result, response = parsed.get("result"), parsed.get("toolResponse")
    if (not set(parsed) - {"billing", "job_id", "request_id", "requestId", "result", "status"}
            and isinstance(result, dict) and set(result) == {"data"}):
        data = result["data"]
    elif (tool in {"harvestapi_get_company", "ai_ark_company_search", "serper_google_search", "limadata_search_web",
                   "builtwith_domain_lookup", "scrapecreators_instagram_profile"}
            and not set(parsed) - {"billing", "job_id", "request_id", "requestId", "toolResponse", "status"}
            and isinstance(response, dict) and not set(response) - {"rawV2", "view"}
            and response.get("view", "rawV2") in {"rawV2", "data"}):
        # The CLI wraps the same provider result differently from the API.
        data = response.get("rawV2")
    else:
        return parsed
    if not isinstance(data, dict):
        return parsed
    status, rows = None, []
    if (tool == "builtwith_domain_lookup" and set(data) == {"data", "meta"}
            and isinstance(data["meta"], dict) and data["meta"].get("status") == 200
            and isinstance(data["data"], dict) and set(data["data"]) == {"Results", "Errors"}
            and data["data"]["Errors"] == [] and isinstance(data["data"]["Results"], list)):
        for row in data["data"]["Results"]:
            if (not isinstance(row, dict) or not isinstance(row.get("Lookup"), str) or not row["Lookup"].strip()
                    or not isinstance(row.get("Result"), dict) or not isinstance(row["Result"].get("Paths"), list)):
                return parsed
            for path in row["Result"]["Paths"]:
                if (not isinstance(path, dict) or not isinstance(path.get("Technologies"), list)
                        or any(not isinstance(tech, dict) for tech in path["Technologies"])):
                    return parsed
            try:
                lookup = row["Lookup"].strip()
                address = urlparse(lookup if "://" in lookup else "https://" + lookup)
                if (address.scheme not in {"http", "https"} or not address.hostname
                        or address.username is not None or any(char.isspace() for char in lookup)
                        or address.port is not None):
                    return parsed
                domain = _domain(lookup)
                if (len(domain) > 253 or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain)
                        or any(len(label) > 63 or label.startswith("-") or label.endswith("-")
                               for label in domain.split("."))):
                    return parsed
            except ValueError:
                return parsed
            meta = row.get("Meta") if isinstance(row.get("Meta"), dict) else {}
            rows.append(dict(row, domain=domain, company_name=meta.get("CompanyName")))
        status = "ok" if rows else "no_results"
    elif (tool == "scrapecreators_instagram_profile" and set(data) == {"success", "data"}
            and data["success"] is True and isinstance(data["data"], dict) and set(data["data"]) == {"user"}):
        user = data["data"]["user"]
        if (not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"].strip()
                or not isinstance(user.get("username"), str) or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", user["username"])
                or (user.get("biography") is not None and not isinstance(user["biography"], str))):
            return parsed
        # A social account can represent an organization. Keep its full_name
        # inside the profile so it does not become a purported business contact.
        rows = [{"instagram_profile": user, "evidence_url": "https://www.instagram.com/" + user["username"] + "/",
                 "evidence_text": user.get("biography") or ""}]
        status = "ok"
    elif (tool == "ai_ark_company_search" and isinstance(data.get("content"), list)
            and type(data.get("numberOfElements")) is int
            and data["numberOfElements"] == len(data["content"])
            and all(isinstance(row, dict) and row.get("id") and isinstance(row.get("summary"), dict)
                    for row in data["content"])):
        rows = data["content"]
        status = "ok" if rows else "no_results"
    elif (tool == "serper_google_search" and isinstance(data.get("data"), dict)
            and isinstance(data["data"].get("organic"), list)
            and isinstance(data.get("meta"), dict)
            and data.get("meta", {}).get("status") == 200
            and data["meta"].get("success") is not False
            and all(isinstance(row, dict) and isinstance(row.get("link"), str)
                    and isinstance(row.get("title"), str) for row in data["data"]["organic"])):
        rows = [dict(row, evidence_url=row["link"], evidence_text=row.get("snippet", ""))
                for row in data["data"]["organic"]]
        status = "ok" if rows else "no_results"
    elif (tool == "limadata_search_web" and set(data) == {"organic"}
            and isinstance(data["organic"], list)):
        for row in data["organic"]:
            if (not isinstance(row, dict) or not isinstance(row.get("url"), str)
                    or not isinstance(row.get("title"), str) or not row["title"].strip()):
                return parsed
            url = row["url"].strip()
            try:
                address = urlparse(url)
                if address.scheme not in {"http", "https"} or not address.hostname:
                    return parsed
            except ValueError:
                return parsed
            rows.append(dict(row, evidence_url=url, evidence_text=row.get("snippet", "")))
        status = "ok" if rows else "no_results"
    elif tool == "exa_answer" and set(data) == {"answer", "citations", "requestId"}:
        citations = data["citations"]
        if (not isinstance(data["answer"], (str, dict)) or not data["answer"]
                or not isinstance(data["requestId"], str) or not data["requestId"].strip()
                or not isinstance(citations, list) or not 1 <= len(citations) <= 100):
            return parsed
        for citation in citations:
            if (not isinstance(citation, dict)
                    or set(citation) - {"author", "favicon", "id", "image", "publishedDate", "text", "title", "url"}
                    or not isinstance(citation.get("url"), str)):
                return parsed
            url = citation["url"].strip()
            try:
                address = urlparse(url)
            except ValueError:
                return parsed
            if (address.scheme not in {"http", "https"} or not address.netloc
                    or not any(isinstance(citation.get(key), str) and citation[key].strip()
                               for key in ("text", "title"))):
                return parsed
            row = dict(citation, evidence_url=url, source_kind="provider_citation")
            for source, target in (("text", "evidence_text"), ("publishedDate", "evidence_date")):
                if isinstance(citation.get(source), str) and citation[source].strip():
                    row[target] = citation[source].strip()
            rows.append(row)
        rows[0].update(provider_answer=data["answer"], provider_request_id=data["requestId"])
        status = "ok"
    elif (tool == "harvestapi_get_company"
            and set(data) in ({"element", "status"}, {"element", "error", "status"})
            and data.get("element") is None):
        if data["status"] == 200 and data.get("error") is None:
            status = "no_results"
        errors = data.get("error")
        if (data["status"] == 400 and isinstance(errors, list) and len(errors) == 1
                and isinstance(errors[0], dict) and set(errors[0]) == {"error", "status"}
                and errors[0]["status"] == 404
                and isinstance(errors[0]["error"], str) and errors[0]["error"].strip()):
            # Preserve the error for the existing company-failure classifier.
            status, rows = "ok", [{"status": 400, "error": errors}]
    if status is None:
        return parsed
    return {"status": status, "results": rows,
            **{key: parsed[key] for key in ("billing", "job_id", "request_id", "requestId") if key in parsed}}


def _firecrawl_batch_output(parsed, tool):
    """Keep the provider's async result state separate from Deepline's wrapper.

    Observed wrappers call an unfinished batch 'completed' and a free getter
    with a completed page 'no_result'. Neither label proves a charge or a miss.
    """
    if (tool not in {"firecrawl_batch_scrape", "firecrawl_get_batch_scrape_status"}
            or not isinstance(parsed, dict) or parsed.get("status") not in {"completed", "no_result"}
            or not parsed.get("job_id")):
        return parsed
    wrapper = parsed.get("toolResponse", {})
    raw = wrapper.get("rawV2") if isinstance(wrapper, dict) else None
    data = raw.get("data") if isinstance(raw, dict) and set(raw) == {"data"} else raw
    if (not isinstance(data, dict) or data.get("success") is not True
            or data.get("status") not in {"scraping", "processing", "completed"}
            or not isinstance(data.get("data"), list)):
        return parsed
    rows = [_scraped_document(row) for row in data["data"] if isinstance(row, dict)]
    if len(rows) != len(data["data"]) or any(row is None for row in rows):
        return parsed
    status = "partial" if data["status"] != "completed" else "ok" if rows else "no_results"
    return {"status": status, "results": rows,
            **{key: parsed[key] for key in ("billing", "job_id") if key in parsed}}


def normalize_response(request: Dict[str, Any], response: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Interpret a captured response using the live adapter rules, without I/O."""
    body, code = _normalize_response(request, response)
    if not body.get("request_id"):
        headers = response.get("headers", {})
        for key in ("x-deepline-request-id", "x-request-id", "x-vercel-id"):
            value = headers.get(key) if isinstance(headers, dict) else None
            if isinstance(value, str) and value.strip():
                body["request_id"] = value
                break
    return body, code


def _normalize_response(request: Dict[str, Any], response: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    if not isinstance(response, dict) or "body" not in response:
        raise ValueError("captured response requires its original body")
    parsed = response["body"]
    stdout = parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False)
    if isinstance(parsed, str):
        try:
            parsed = _json_from_text(parsed)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict) and "executionRecovery" in parsed:
        # A keyed call's execution state is Deepline metadata, not provider output; exact-shape
        # rules must read a keyed response as they read an unkeyed one. The receipt keeps it.
        parsed = {key: value for key, value in parsed.items() if key != "executionRecovery"}
    stderr = response.get("stderr", "")
    if not isinstance(stderr, str):
        raise ValueError("captured stderr must be text")
    if response.get("timed_out") is True:
        body = {
            "status": "timeout",
            "provider": "deepline",
            "operation": request["operation"],
            **_execution_metadata(parsed),
        }
        if request.get("tool"):
            body["tool"] = request["tool"]
        if request.get("entity_type"):
            body["entity_type"] = request["entity_type"]
        return body, 0
    returncode = response.get("exit_code")
    if type(returncode) is not int:
        raise ValueError("captured response requires an exit code or timeout")
    # Hunter's observed data-absence error is not an endpoint or transport 404. Deepline may add
    # metadata fields to its responses (2026-10-08: parsers should tolerate them), so only a
    # field that could carry provider output or a different outcome rules this reading out.
    if (request["operation"] == "execute" and request.get("tool") == "hunter_companies_find"
            and request.get("entity_type") == "company" and isinstance(parsed, dict)
            and not set(parsed) & (_PROVIDER_OUTPUT_FIELDS | _OUTCOME_FIELDS) and parsed.get("ok") is False
            and parsed.get("error") == {
                "message": "not_found: The domain does not exist in our database",
                "code": "UPSTREAM_NOT_FOUND", "details": {"statusCode": 404}}):
        return {
            "status": "no_results", "provider": "deepline", "operation": "execute",
            "tool": request["tool"], "entity_type": "company", "results": [], "evidence": [],
            "error": _envelope_error(parsed), **_execution_metadata(parsed),
        }, 0
    if returncode != 0:
        # A current CLI can print an update notice before a structured provider
        # error. Prefer that parsed error over notice/help text. Otherwise,
        # prefer stderr because stdout help examples can contain status words.
        parsed_error = _envelope_error(parsed)
        parsed_status = _envelope_status(parsed)
        diagnostic = stderr.strip() or stdout.strip()
        if parsed_error:
            status = (
                parsed_status
                if parsed_status in _FAILURE_STATUSES
                else _classify_error(parsed_error["message"])
            )
            error = parsed_error
        else:
            status = _classify_error(diagnostic)
            error = _safe_error(diagnostic)
        body = {
            "status": status,
            "provider": "deepline",
            "operation": request["operation"],
            "error": error,
            **_execution_metadata(parsed),
        }
        if status == "schema_error":
            body["error_stage"] = "provider"
        if request.get("tool"):
            body["tool"] = request["tool"]
        if request.get("entity_type"):
            body["entity_type"] = request["entity_type"]
        return body, 0
    if parsed is None:
        body = {
            "status": "schema_error",
            "error_stage": "response",
            "provider": "deepline",
            "operation": request["operation"],
        }
        if request.get("tool"):
            body["tool"] = request["tool"]
        if request.get("entity_type"):
            body["entity_type"] = request["entity_type"]
        return body, 0
    if request["operation"] == "execute":
        parsed = _native_page_output(parsed, request)
        parsed = _firecrawl_batch_output(parsed, request["tool"])
        parsed = _completed_execute_output(parsed, request["tool"])
        body = _execute_output(parsed, request["tool"], request.get("entity_type"), request["limit"])
    else:
        body = _catalog_output(request["operation"], parsed, request.get("tool"), request.get("entity_type"))
    if body.get("status") == "schema_error":
        body["error_stage"] = "provider" if _envelope_status(parsed) == "schema_error" else "response"
    return body, 0


def _read_cli_input(argv: Optional[Sequence[str]] = None) -> Any:
    parser = argparse.ArgumentParser(description="Run a bounded Deepline catalog or execute operation")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", help="JSON request object")
    group.add_argument("--input-file", help="Path to a JSON request object")
    parser.add_argument("--output-file", help="New file for the full redacted response; never overwritten")
    args = parser.parse_args(argv)
    try:
        raw = args.input
        if args.input_file:
            with open(args.input_file, "r", encoding="utf-8") as handle:
                raw = handle.read()
        return load_json(raw), args.output_file
    except (OSError, ValueError) as exc:
        raise InputError("input must contain valid JSON") from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    receipt = None
    try:
        request, output_file = _read_cli_input(argv)
        if os.environ.get("TYCHE_ISOLATED_RUN") == "1" and request.get("operation") not in {"search", "describe"}:
            raise InputError("The isolated TYCHE runtime researches only through its native tools; this command would bypass their safeguards. Report the native tool failure instead.")
        if output_file:
            try:
                receipt = ResponseFile(output_file, redact)
            except OSError:
                raise ConfigError("response output must be a new writable file; request was not sent")
        body, code = run(request, capture=receipt.capture) if receipt else run(request)
    except InputError as exc:
        body, code = {"status": "schema_error", "error_stage": "request", "error": _safe_error(str(exc))}, 2
    except ConfigError as exc:
        body, code = {"status": "config_error", "error": _safe_error(str(exc))}, 2
    if receipt is not None and not receipt.finish(body):
        body = dict(body, receipt_error="Response file could not be finalized. Preserve this output; do not repeat a possibly billed request.")
        code = 2
    # One compact JSON object is the only stdout output.  Operational detail is
    # intentionally omitted to keep credentials from appearing in logs.
    sys.stdout.write(json.dumps(redact(body), ensure_ascii=True, separators=(",", ":")) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
