"""The scorer's deterministic helpers, imported from the round image, each with a known/unknown status.

The agent runs as ``python3 -I``, which ignores PYTHONPATH, so ``/model`` (where the round image keeps the scorer
source) is appended to sys.path here -- appended, so bundle modules keep precedence.  Every symbol is imported on its
own; a symbol that does not import leaves its gate ``unknown``.  The wrappers below return None for an unknown gate,
and callers treat None as "not passed" for admission decisions.  Only pure modules are imported (the qualification
package, lab_arena.output and the gateway's company model); no transport, broker or network helper is touched.
"""

from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Optional

MODEL_DIR = "/model"
if MODEL_DIR not in sys.path:
    sys.path.append(MODEL_DIR)

_LS = "qualification.scoring.lead_scorer"
_CMP = "qualification.scoring.competition"
_IV3 = "qualification.scoring.intent_verification_three_stage"
_ISG = "qualification.scoring.intent_signal_gate"
SYMBOLS: dict[str, tuple[str, str]] = {
    "country": ("qualification.scoring.pre_checks", "check_country_match"),
    "data_quality": ("qualification.scoring.pre_checks", "_check_company_data_quality"),
    "exclusion": (_LS, "_matches_exclusion_list"),
    "stage_norm": (_LS, "_normalize_company_stage"),
    "stage_quote": (_LS, "_stage_quote_supports_observation"),
    "stage_evidence": (_LS, "_stage_evidence_supports_observation"),
    "acquired_quote": (_LS, "_acquired_stage_quote_supports_names"),
    "region_states": (_LS, "_requested_us_region_states"),
    "us_states": (_LS, "_requested_us_states"),
    "stage_conflict_hints": (_LS, "_submitted_intent_stage_conflict_hints"),
    "untrusted_source": (_LS, "_is_untrusted_evidence_source"),
    "claim_age": (_ISG, "_claim_max_age_days"),
    "antibot": (_ISG, "check_antibot_wall"),
    "evidence_source": (_CMP, "_evidence_source"),
    "icp_buckets": (_CMP, "employee_count_buckets_for_icp"),
    "normalized_icp": (_CMP, "_normalized_icp"),
    "intent_cap": (_CMP, "available_intent_score_cap"),
    "company_goal": (_CMP, "_company_goal"),
    "normalized_company": (_CMP, "_normalized_company"),
    "job_board_url": (_IV3, "_is_job_board_url"),
    "job_body_anchors": (_IV3, "JOB_BODY_ANCHORS"),
    "fetch_verdict": (_IV3, "_evaluate_sd_response"),
    "extracted_verdict": (_IV3, "_evaluate_extracted_sd_body"),
    "paragraph": ("qualification.intent_details", "validate_intent_details_text"),
    "relative_publication": ("qualification.scoring.intent_details", "_RELATIVE_PUBLICATION_TIMING"),
    "relative_event": ("qualification.scoring.intent_details", "_RELATIVE_EVENT_TIMING"),
    "employee_range": ("qualification.scoring.linkedin_company_size", "_canonical_employee_range"),
    "bucket_norm": ("qualification.employee_buckets", "normalize_employee_count_bucket"),
    "registrable": ("qualification.scoring.company_verification", "_registrable_domain"),
    "output_document": ("lab_arena.output", "output_document_from_bytes"),
    "company_model": ("gateway.qualification.models", "CompanyOutput"),
    "visible_text": ("qualification.scoring.verification_helpers", "visible_html_text"),
    "visible_links": ("qualification.scoring.verification_helpers", "visible_html_links"),
    "article_body": ("qualification.scoring.verification_helpers", "extract_article_body"),
    "published_date": (_IV3, "_published_date_from_html"),
}
_FORBIDDEN_PREFIXES = ("lab_arena.shim", "lab_arena.runner", "lab_arena.broker", "lab_arena.web_egress", "sitecustomize")
_LOADED: dict[str, Any] = {}
_ERRORS: dict[str, str] = {}
V6 = "leadpoet.lab_arena.output.v6"


def _load(name: str) -> Any:
    if name in _LOADED:
        return _LOADED[name]
    if name in _ERRORS:
        return None
    module_name, attr = SYMBOLS[name]
    if module_name.startswith(_FORBIDDEN_PREFIXES):
        _ERRORS[name] = "not allowed"
        return None
    try:
        value = getattr(importlib.import_module(module_name), attr)
    except Exception as exc:  # noqa: BLE001 - the gate stays unknown
        _ERRORS[name] = f"{type(exc).__name__}: {str(exc)[:80]}"
        return None
    _LOADED[name] = value
    return value


def known(name: str) -> bool:
    return _load(name) is not None


def all_known(names: Iterable[str]) -> bool:
    return all(known(n) for n in names)


def status() -> dict[str, str]:
    return {name: ("known" if known(name) else "unknown") for name in SYMBOLS}


# ---- wrappers: None means the gate is unknown ----------------------------------------------------------------

def country_ok(observed_country: Any, icp_country: Any) -> Optional[bool]:
    fn = _load("country")
    if fn is None:
        return None
    try:
        return bool(fn(str(observed_country or ""), str(icp_country or "")).passed)
    except Exception:  # noqa: BLE001
        return None


def data_quality_ok(name: Any, website: Any) -> Optional[bool]:
    fn = _load("data_quality")
    if fn is None:
        return None
    try:
        ok, _reason = fn(SimpleNamespace(company_name=str(name or ""), company_website=str(website or "")))
        return bool(ok)
    except Exception:  # noqa: BLE001
        return None


def excluded(name: Any, website: Any, linkedin: Any, entries: Any) -> Optional[bool]:
    fn = _load("exclusion")
    if fn is None:
        return None
    try:
        company = SimpleNamespace(company_name=str(name or ""), company_website=str(website or ""),
                                  company_linkedin=str(linkedin or ""))
        return bool(fn(company, list(entries or [])))
    except Exception:  # noqa: BLE001
        return None


def normalize_stage(value: Any) -> Optional[str]:
    fn = _load("stage_norm")
    if fn is None:
        return None
    try:
        return str(fn(value))
    except Exception:  # noqa: BLE001
        return None


def stage_quote_ok(stage: Any, quote: Any) -> Optional[bool]:
    fn, norm = _load("stage_quote"), normalize_stage(stage)
    if fn is None or norm is None:
        return None
    try:
        return bool(fn(norm, str(quote or "")))
    except Exception:  # noqa: BLE001
        return None


def stage_evidence_ok(stage: Any, quote: Any, *, url: Any, first_party_domains: Iterable[str] = (),
                      identity_names: Iterable[str] = ()) -> Optional[bool]:
    fn = _load("stage_evidence")
    if fn is None:
        return None
    try:
        return bool(fn(str(stage or ""), str(quote or ""), evidence_url=str(url or ""),
                       first_party_domains=tuple(first_party_domains), identity_names=tuple(identity_names)))
    except Exception:  # noqa: BLE001
        return None


def acquired_quote_ok(names: Iterable[str], quote: Any) -> Optional[bool]:
    fn = _load("acquired_quote")
    if fn is None:
        return None
    try:
        return bool(fn(tuple(names), str(quote or "")))
    except Exception:  # noqa: BLE001
        return None


def region_states(geography: Any) -> Optional[frozenset]:
    fn = _load("region_states")
    if fn is None:
        return None
    try:
        return frozenset(fn(str(geography or "")))
    except Exception:  # noqa: BLE001
        return None


def us_states(geography: Any) -> Optional[frozenset]:
    fn = _load("us_states")
    if fn is None:
        return None
    try:
        return frozenset(fn(str(geography or "")))
    except Exception:  # noqa: BLE001
        return None


def stage_conflict_urls(name: str, urls: Iterable[str], stage: Any) -> Optional[list]:
    fn = _load("stage_conflict_hints")
    if fn is None:
        return None
    try:
        company = SimpleNamespace(company_name=str(name or ""), intent_signals=[{"url": u} for u in urls])
        return list(fn(company, str(stage or "")))
    except Exception:  # noqa: BLE001
        return None


def untrusted_source(url: Any, website: Any = "") -> Optional[str]:
    fn = _load("untrusted_source")
    if fn is None:
        return None
    try:
        return str(fn(str(url or ""), str(website or "")) or "")
    except Exception:  # noqa: BLE001
        return None


def claim_max_age(text: Any) -> Optional[int]:
    fn = _load("claim_age")
    if fn is None:
        return None
    try:
        value = fn(str(text or ""))
        return int(value) if value else None
    except Exception:  # noqa: BLE001
        return None


def antibot(content: Any) -> Optional[str]:
    fn = _load("antibot")
    if fn is None:
        return None
    try:
        return str(fn(str(content or "")) or "")
    except Exception:  # noqa: BLE001
        return None


def evidence_source(url: Any, website: Any) -> Optional[str]:
    fn = _load("evidence_source")
    if fn is None:
        return None
    try:
        return str(fn(str(url or ""), company_website=str(website or "")))
    except Exception:  # noqa: BLE001
        return None


def icp_buckets(icp: Mapping[str, Any]) -> Optional[list]:
    fn = _load("icp_buckets")
    if fn is None:
        return None
    try:
        return list(fn(icp))
    except Exception:  # noqa: BLE001
        return None


def intent_cap(icp: Mapping[str, Any]) -> Optional[float]:
    fn = _load("intent_cap")
    if fn is None:
        return None
    try:
        return float(fn(icp))
    except Exception:  # noqa: BLE001
        return None


def company_goal(icp: Mapping[str, Any]) -> Optional[int]:
    fn = _load("company_goal")
    if fn is None:
        return None
    try:
        return int(fn(icp))
    except Exception:  # noqa: BLE001
        return None


def is_job_board_url(url: Any) -> Optional[bool]:
    fn = _load("job_board_url")
    if fn is None:
        return None
    try:
        return bool(fn(str(url or "")))
    except Exception:  # noqa: BLE001
        return None


def has_job_body(text: Any) -> Optional[bool]:
    anchors = _load("job_body_anchors")
    if anchors is None:
        return None
    low = str(text or "").lower()
    return any(a in low for a in anchors)


def fetch_verdict(status_code: int, body: Any) -> Optional[str]:
    fn = _load("fetch_verdict")
    if fn is None:
        return None
    try:
        return str(fn(int(status_code), str(body or "")))
    except Exception:  # noqa: BLE001
        return None


def extracted_verdict(raw_body: Any, body: Any) -> Optional[str]:
    fn = _load("extracted_verdict")
    if fn is None:
        return None
    try:
        return str(fn(str(raw_body or ""), str(body or "")))
    except Exception:  # noqa: BLE001
        return None


def paragraph_ok(text: Any) -> Optional[bool]:
    fn = _load("paragraph")
    if fn is None:
        return None
    try:
        fn(text)
        return True
    except Exception:  # noqa: BLE001 - the judge's validator raised: the paragraph is invalid
        return False


def relative_time_hits(text: Any) -> Optional[list]:
    pub, event = _load("relative_publication"), _load("relative_event")
    if pub is None or event is None:
        return None
    body = str(text or "")
    return [m.group(0) for pattern in (pub, event) for m in pattern.finditer(body)]


def employee_range(record: Any) -> Optional[str]:
    fn = _load("employee_range")
    if fn is None:
        return None
    try:
        return str(fn(record) or "")
    except Exception:  # noqa: BLE001
        return None


def normalize_bucket(value: Any) -> Optional[str]:
    fn = _load("bucket_norm")
    if fn is None:
        return None
    try:
        return str(fn(value, default=None) or "")
    except Exception:  # noqa: BLE001
        return None


def registrable_domain(url: Any) -> Optional[str]:
    fn = _load("registrable")
    if fn is None:
        return None
    try:
        return str(fn(str(url or "")) or "")
    except Exception:  # noqa: BLE001
        return None


def visible_text(html: Any) -> Optional[str]:
    fn = _load("visible_text")
    if fn is None:
        return None
    try:
        return str(fn(str(html or "")))
    except Exception:  # noqa: BLE001
        return None


def published_date(html: Any, url: Any) -> Optional[str]:
    """The publication date the judge reads from a page's own metadata (ISO date, '' when none; None unknown)."""

    fn = _load("published_date")
    if fn is None:
        return None
    try:
        return str(fn(str(html or ""), str(url or "")) or "")
    except Exception:  # noqa: BLE001
        return None


def visible_links(html: Any) -> Optional[list]:
    fn = _load("visible_links")
    if fn is None:
        return None
    try:
        return [str(link) for link in fn(str(html or ""))]
    except Exception:  # noqa: BLE001
        return None


def article_body(html_or_text: Any) -> Optional[str]:
    fn = _load("article_body")
    if fn is None:
        return None
    try:
        return str(fn(str(html_or_text or "")))
    except Exception:  # noqa: BLE001
        return None


def validate_document(companies: list, *, schema: str = V6) -> Optional[str]:
    """'' when the host validator accepts the document, the reason when it rejects it, None when unknown."""

    fn = _load("output_document")
    if fn is None:
        return None
    try:
        fn(json.dumps({"companies": companies}).encode("utf-8"), expected_schema_version=schema,
           require_intent_dates=False)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:160]}"


def validate_row(row: Mapping[str, Any]) -> Optional[str]:
    """'' when the judge's internal company model accepts the row, else the reason; None when unknown."""

    normalize, model = _load("normalized_company"), _load("company_model")
    if normalize is None or model is None:
        return None
    try:
        model(**normalize(dict(row), integrity_policy=True))
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:160]}"


# ---- self-test: one positive and one negative input per gate ------------------------------------------------

def _checks() -> dict[str, bool]:
    icp = {"icp_id": "t", "industry": "Software", "intent_signals": ["Launched a product in the last 90 days"],
           "employee_count": ["11-50", "51-200"], "max_companies": 5, "bonus_intents": []}
    row = {"company_name": "Acme Analytics", "company_website": "https://acme-analytics.io/", "company_linkedin": "",
           "industry": "Software", "employee_count": "11-50", "company_stage": "", "country": "United States",
           "state": "", "intent_details": "On September 3, 2026, Acme Analytics launched a reporting tool. This launch "
           "may extend its analytics platform.", "intent_signals": [{"matched_icp_signal": 0, "description": "Acme "
           "Analytics launched a reporting tool", "date": "2026-09-03", "url": "https://acme-analytics.io/news/launch"}],
           "company_stage_evidence": [], "required_attribute": None}
    bad_row = dict(row, company_linkedin=None)
    return {
        "country": country_ok("United States", "United States") is True and country_ok("", "United States") is False,
        "data_quality": data_quality_ok("Acme Analytics", "https://acme.io/") is True
        and data_quality_ok("", "https://acme.io/") is False,
        "exclusion": excluded("Acme", "https://acme.io/", "", ["acme.io"]) is True
        and excluded("Beta", "https://beta.io/", "", ["acme.io"]) is False,
        "stage_norm": normalize_stage("Series C+") == "series c+" and normalize_stage("Unknown") == "",
        "stage_quote": stage_quote_ok("Series A", "Acme today announced it has raised $12 million in Series A funding")
        is True and stage_quote_ok("Series A", "Acme builds software") is False,
        "stage_evidence": stage_evidence_ok("Series A", "Acme today announced it has raised $12 million in Series A "
                                            "funding", url="https://acme.io/news") is True
        and stage_evidence_ok("Series A", "Acme builds software", url="https://acme.io/") is False,
        "acquired_quote": acquired_quote_ok(["Beta"], "Beta was acquired by Acme in 2025.") is True
        and acquired_quote_ok(["Beta"], "Beta builds software.") is False,
        "region_states": "California" in (region_states("United States, West Coast") or ())
        and not region_states("Canada"),
        "us_states": us_states("United States, Texas") == frozenset({"Texas"}) and not us_states("United States"),
        "stage_conflict_hints": bool(stage_conflict_urls("Acme", ["https://news.io/acme-raises-40m-series-b"], "Series A"))
        and not stage_conflict_urls("Acme", ["https://news.io/acme-raises-10m-series-a"], "Series A"),
        "untrusted_source": untrusted_source("https://acme.io/x", "https://acme.io/") == ""
        and bool(untrusted_source("https://news.blog/x", "https://acme.io/")) in (True, False),
        "claim_age": claim_max_age("in the last 90 days") is not None and claim_max_age("a product") is None,
        "antibot": bool(antibot("Just a moment... Checking your browser")) and antibot("A long article " * 50) == "",
        "evidence_source": evidence_source("https://boards.greenhouse.io/acme/jobs/1234567", "https://acme.io/")
        == "job_board" and evidence_source("https://acme.io/news/a", "https://acme.io/") == "company_website",
        "icp_buckets": icp_buckets(icp) == ["11-50", "51-200"] and icp_buckets({"employee_count": ["x"]}) is None,
        "normalized_icp": known("normalized_icp"),
        "intent_cap": intent_cap(icp) == 60.0 and intent_cap(dict(icp, intent_signals=["a", "b"])) == 80.0,
        "company_goal": company_goal(icp) == 5 and company_goal({"max_companies": 9}) == 5,
        "normalized_company": validate_row(row) == "" and bool(validate_row(bad_row)),
        "company_model": known("company_model"),
        "job_board_url": is_job_board_url("https://jobs.lever.co/acme/1") is True
        and is_job_board_url("https://acme.io/news") is False,
        "job_body_anchors": has_job_body("Responsibilities: build things") is True and has_job_body("News") is False,
        "fetch_verdict": fetch_verdict(200, "<html><body><p>" + "Acme launched a reporting tool. " * 120 + "</p></body></html>")
        == "ok"
        and fetch_verdict(404, "") == "http_404",
        "extracted_verdict": extracted_verdict("<html><body><p>x</p></body></html>", "Real article text " * 40) == "ok"
        and extracted_verdict("<html></html>", "") != "ok",
        "paragraph": paragraph_ok("One plain paragraph.") is True and paragraph_ok("- a bullet") is False,
        "relative_publication": bool(relative_time_hits("recent coverage says")) and not relative_time_hits("On May 1"),
        "relative_event": bool(relative_time_hits("the newly launched tool")) and not relative_time_hits("is hiring"),
        "employee_range": employee_range({"start": 51, "end": 200}) == "51-200"
        and employee_range({"start": 50, "end": 200}) == "",
        "bucket_norm": normalize_bucket("51-200") == "51-200" and normalize_bucket("lots") == "",
        "registrable": registrable_domain("https://www.news.acme.co.uk/x") == "acme.co.uk"
        and registrable_domain("not a url") in ("", None),
        "output_document": validate_document([row]) == "" and bool(validate_document([dict(row, fit_summary="x")])),
        "visible_text": "Hello" in (visible_text("<html><body><p>Hello</p><script>x</script></body></html>") or "")
        and visible_text("<html><body><script>x</script></body></html>") == "",
        "visible_links": "/about" in (visible_links('<html><body><a href="/about">About</a></body></html>') or [])
        and not visible_links("<html><body><p>none</p></body></html>"),
        "article_body": known("article_body"),
        "published_date": published_date('<html><head><meta property="article:published_time" '
                                         'content="2026-09-03T10:00:00Z"></head><body>x</body></html>',
                                         "https://acme.io/news/a") == "2026-09-03"
        and published_date("<html><head></head><body>x</body></html>", "https://acme.io/news/a") == "",
    }


def self_test() -> dict[str, str]:
    """Run every gate on known inputs: 'pass', 'fail' (imported but disagreeing) or 'unknown' (not importable)."""

    try:
        results = _checks()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return {name: ("unknown" if not known(name) else ("pass" if results.get(name) else "fail")) for name in SYMBOLS}


def print_self_test(stream: Any = None) -> dict[str, str]:
    report = self_test()
    counts = {k: sum(1 for v in report.values() if v == k) for k in ("pass", "fail", "unknown")}
    print("[gates] model_dir=%s %s %s" % (MODEL_DIR, json.dumps(counts), json.dumps(report, sort_keys=True)),
          file=stream or sys.stderr, flush=True)
    return report


__all__ = [name for name in dir() if not name.startswith("_")]
