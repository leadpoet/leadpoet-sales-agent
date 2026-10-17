"""The judge's own deterministic verdicts, imported from the scorer source the round image carries.

The bundle runs as ``python3 -I``, so PYTHONPATH is ignored and ``/model`` -- where the image keeps the scorer
package -- has to be put on ``sys.path`` by hand.  It is *appended*, never prepended, so our own modules keep
precedence and nothing here can shadow the bundle.  Only pure scoring modules are touched (the ``qualification``
package, ``lab_arena.output`` and the gateway's company model): no transport, broker or network helper.

Every symbol is imported on its own and lazily.  A symbol that will not import leaves its gate *unknown*, and every
wrapper returns ``None`` for an unknown gate.  Callers must read ``None`` as "no verdict" and fall back to the
bundle's own heuristic -- never as a pass.

Why this exists: until now we scored our own rows with ``scorer_mirror.py``, a 985-line hand-written copy of rules
that change several times a week.  A copy is always a round behind.  These are the functions that will actually
judge the submission:

  * ``stage_verdict``     -- the whole ``company_stage_evidence`` decision, called exactly as the investigator calls
                             it (semantic/authoritative public listing included, venture-stage naming included).
  * ``headcount_verdict`` -- whether a required-attribute quote binds the submitted employee count.
  * ``headquarters_verdict`` -- the same for a geography attribute.
  * ``occurs``            -- whether a quote survives the judge's own page normalization; this is the gate that
                             silently killed markdown-flavoured quotes.
  * ``document_reason`` / ``row_reason`` -- the host's schema validators, so an output shape we have never seen
                             cannot lose a whole round.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Optional, Sequence

SOURCE_DIRS = tuple(path for path in (os.environ.get("SN71_JUDGE_SOURCE", ""), "/model") if path)
for _path in SOURCE_DIRS:
    if _path not in sys.path and os.path.isdir(_path):
        sys.path.append(_path)

_LS = "qualification.scoring.lead_scorer"
_CEI = "qualification.scoring.company_evidence_investigator"
_CMP = "qualification.scoring.competition"
_CV = "qualification.scoring.company_verification"
_IV3 = "qualification.scoring.intent_verification_three_stage"
_VH = "qualification.scoring.verification_helpers"
_PC = "qualification.scoring.pre_checks"

SYMBOLS: dict[str, tuple[str, str]] = {
    # the stage decision, in the order the investigator applies it
    "stage_norm": (_LS, "_normalize_company_stage"),
    "canonical_stages": (_LS, "_CANONICAL_COMPANY_STAGES"),
    "stage_lexical": (_LS, "_stage_quote_supports_observation"),
    "stage_evidence": (_LS, "_stage_evidence_supports_observation"),
    "acquired_names": (_LS, "_acquired_stage_quote_supports_names"),
    "venture_stage_named": (_CEI, "_quote_names_compatible_venture_stage"),
    "semantic_listing": (_CEI, "_quote_supports_semantic_public_listing"),
    "sec_listing": (_CEI, "_quote_supports_sec_equity_listing"),
    "exchange_profile": (_CV, "current_exchange_profile_names_issuer"),
    # the required-attribute decision
    "headcount_quote": (_CEI, "_quote_supports_headcount"),
    "headquarters_quote": (_CEI, "_quote_supports_headquarters"),
    "headquarters_named": (_CEI, "_quote_identifies_headquarters"),
    "names_company": (_CEI, "_quote_names_company"),
    # text the judge actually reads
    "quote_occurs": (_CEI, "_quote_occurs"),
    "quote_surface": (_CEI, "_visible_quote_surface"),
    "span": (_CEI, "_normalized_span"),
    "visible_text": (_VH, "visible_html_text"),
    "article_body": (_VH, "extract_article_body"),
    "fetch_verdict": (_IV3, "_evaluate_sd_response"),
    "antibot": ("qualification.scoring.intent_signal_gate", "check_antibot_wall"),
    # company- and ICP-level checks
    "country": (_PC, "check_country_match"),
    "data_quality": (_PC, "_check_company_data_quality"),
    "exclusion": (_LS, "_matches_exclusion_list"),
    "untrusted_source": (_LS, "_is_untrusted_evidence_source"),
    "stage_conflict": (_LS, "_submitted_intent_stage_conflict_hints"),
    "region_states": (_LS, "_requested_us_region_states"),
    "us_states": (_LS, "_requested_us_states"),
    "icp_buckets": (_CMP, "employee_count_buckets_for_icp"),
    "company_goal": (_CMP, "_company_goal"),
    "evidence_source": (_CMP, "_evidence_source"),
    "normalized_company": (_CMP, "_normalized_company"),
    "registrable": (_CV, "_registrable_domain"),
    "bucket_norm": ("qualification.employee_buckets", "normalize_employee_count_bucket"),
    "paragraph": ("qualification.intent_details", "validate_intent_details_text"),
    # the host's own output validators
    "output_document": ("lab_arena.output", "output_document_from_bytes"),
    "company_model": ("gateway.qualification.models", "CompanyOutput"),
}

# Nothing that moves bytes or money may be imported, whatever a symbol table says.
_FORBIDDEN = ("lab_arena.shim", "lab_arena.runner", "lab_arena.broker", "lab_arena.web_egress", "sitecustomize")
_LOADED: dict[str, Any] = {}
_FAILED: dict[str, str] = {}
V6 = "leadpoet.lab_arena.output.v6"


def _load(name: str) -> Any:
    if name in _LOADED:
        return _LOADED[name]
    if name in _FAILED:
        return None
    module_name, attr = SYMBOLS[name]
    if module_name.startswith(_FORBIDDEN):
        _FAILED[name] = "not allowed"
        return None
    try:
        value = getattr(importlib.import_module(module_name), attr)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - an unimportable symbol is simply unknown
        # SystemExit is caught on purpose: a scorer module that calls sys.exit() at import time would
        # otherwise end the whole ICP, and a gate we cannot import is only an unknown verdict.
        _FAILED[name] = f"{type(exc).__name__}: {str(exc)[:80]}"
        return None
    _LOADED[name] = value
    return value


def known(name: str) -> bool:
    return _load(name) is not None


def available() -> bool:
    """True when the scorer source is reachable at all (the stage gate is the bellwether)."""

    return known("stage_evidence")


def status() -> dict[str, str]:
    return {name: ("known" if known(name) else "unknown") for name in SYMBOLS}


def failures() -> dict[str, str]:
    for name in SYMBOLS:
        known(name)
    return dict(_FAILED)


# ---- identity, the way the investigator builds it -------------------------------------------------------------

def compact(value: Any) -> str:
    """The judge's identity key: casefolded, stripped of everything but letters and digits."""

    fn = _load("span")
    text = str(fn(str(value or ""))) if fn is not None else " ".join(str(value or "").casefold().split())
    return re.sub(r"[^a-z0-9]+", "", text)


def identity_names(name: Any, *extra: Any) -> tuple[str, ...]:
    keys = [compact(name)] + [compact(value) for value in extra]
    return tuple(sorted({key for key in keys if key}))


def registrable_domain(url: Any) -> Optional[str]:
    fn = _load("registrable")
    if fn is None:
        return None
    try:
        return str(fn(str(url or "")) or "")
    except Exception:  # noqa: BLE001
        return None


def first_party_domains(*urls: Any) -> tuple[str, ...]:
    out = {registrable_domain(url) or "" for url in urls}
    return tuple(sorted(domain for domain in out if domain))


# ---- the gates that decide our rows ---------------------------------------------------------------------------

def normalize_stage(value: Any) -> Optional[str]:
    fn = _load("stage_norm")
    if fn is None:
        return None
    try:
        return str(fn(value))
    except Exception:  # noqa: BLE001
        return None


def canonical_stage(value: Any) -> Optional[bool]:
    """A stage outside the judge's own vocabulary is UNPROVEN however good the quote is."""

    stages, norm = _load("canonical_stages"), normalize_stage(value)
    if stages is None or norm is None:
        return None
    try:
        return norm in set(stages)
    except Exception:  # noqa: BLE001
        return None


def occurs(quote: Any, page_text: Any) -> Optional[bool]:
    """Does this quote survive the judge's page normalization?  The gate markdown quotes fail."""

    fn = _load("quote_occurs")
    if fn is None:
        return None
    try:
        return bool(fn(quote, str(page_text or "")))
    except Exception:  # noqa: BLE001
        return None


def page_surface(page_text: Any) -> Optional[str]:
    fn = _load("quote_surface")
    if fn is None:
        return None
    try:
        return str(fn(str(page_text or "")))
    except Exception:  # noqa: BLE001
        return None


def names_company(quote: Any, names: Iterable[str]) -> Optional[bool]:
    fn = _load("names_company")
    if fn is None:
        return None
    try:
        return bool(fn(str(quote or ""), {key for key in names if key}))
    except Exception:  # noqa: BLE001
        return None


def stage_verdict(stage: Any, quote: Any, *, url: Any = "", name: Any = "", website: Any = "",
                  names: Sequence[str] = (), domains: Sequence[str] = ()) -> Optional[bool]:
    """The investigator's whole stage decision for one {url, quote}: True, False, or None when unknown.

    Called exactly as ``company_evidence_investigator`` calls it -- the same normalized stage, the same compacted
    identity names, the same first-party domain set, and the two public-listing flags it computes first -- then the
    venture-stage naming check it applies afterwards.
    """

    gate, norm = _load("stage_evidence"), normalize_stage(stage)
    if gate is None or norm is None:
        return None
    keys = tuple(names) or identity_names(name)
    domain_set = tuple(domains) or first_party_domains(website)
    try:
        if norm == "acquired":
            fn = _load("acquired_names")
            return None if fn is None else bool(fn(keys, str(quote or "")))
        semantic = authoritative = False
        if norm == "public":
            sem_fn, profile_fn, sec_fn = _load("semantic_listing"), _load("exchange_profile"), _load("sec_listing")
            if sem_fn is None or profile_fn is None or sec_fn is None:
                return None
            semantic = bool(sem_fn(str(quote or ""))) or bool(profile_fn(str(quote or ""), set(keys)))
            authoritative = bool(sec_fn(str(url or ""), str(quote or ""), set(keys)))
        if not gate(norm, str(quote or ""), evidence_url=str(url or ""), first_party_domains=domain_set,
                    identity_names=keys, semantic_public_listing=semantic,
                    authoritative_public_listing=authoritative):
            return False
        venture = _load("venture_stage_named")
        if venture is None:
            return None
        return bool(venture(norm, str(quote or "")))
    except Exception:  # noqa: BLE001
        return None


def headcount_verdict(quote: Any, observed_value: Any) -> Optional[bool]:
    fn = _load("headcount_quote")
    if fn is None:
        return None
    try:
        return bool(fn(str(quote or ""), observed_value))
    except Exception:  # noqa: BLE001
        return None


def headquarters_verdict(quote: Any, *, country: Any = "", state: Any = "") -> Optional[bool]:
    fn = _load("headquarters_quote")
    if fn is None:
        return None
    try:
        return bool(fn(str(quote or ""), observed_country=str(country or ""), observed_state=str(state or "")))
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


def stage_conflicts(name: Any, urls: Iterable[str], stage: Any) -> Optional[list]:
    """The intent URLs whose own slug argues against the stage we submitted."""

    fn = _load("stage_conflict")
    if fn is None:
        return None
    try:
        company = SimpleNamespace(company_name=str(name or ""), intent_signals=[{"url": str(u)} for u in urls])
        return list(fn(company, str(stage or "")))
    except Exception:  # noqa: BLE001
        return None


def country_ok(observed: Any, wanted: Any) -> Optional[bool]:
    fn = _load("country")
    if fn is None:
        return None
    try:
        return bool(fn(str(observed or ""), str(wanted or "")).passed)
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


def region_states(geography: Any) -> Optional[frozenset]:
    fn = _load("region_states")
    if fn is None:
        return None
    try:
        return frozenset(fn(str(geography or "")))
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


def normalize_bucket(value: Any) -> Optional[str]:
    fn = _load("bucket_norm")
    if fn is None:
        return None
    try:
        return str(fn(value, default=None) or "")
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


def evidence_source(url: Any, website: Any) -> Optional[str]:
    fn = _load("evidence_source")
    if fn is None:
        return None
    try:
        return str(fn(str(url or ""), company_website=str(website or "")))
    except Exception:  # noqa: BLE001
        return None


def excluded(name: Any, website: Any, linkedin: Any, entries: Iterable[str]) -> Optional[bool]:
    fn = _load("exclusion")
    if fn is None:
        return None
    try:
        company = SimpleNamespace(company_name=str(name or ""), company_website=str(website or ""),
                                  company_linkedin=str(linkedin or ""))
        return bool(fn(company, list(entries or [])))
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


def paragraph_ok(text: Any) -> Optional[bool]:
    fn = _load("paragraph")
    if fn is None:
        return None
    try:
        fn(text)
        return True
    except Exception:  # noqa: BLE001 - the judge's own validator rejected the paragraph
        return False


def antibot(content: Any) -> Optional[str]:
    """The scorer's own bot-wall check, the one gate that reads extracted text rather than raw HTML.

    `_evaluate_sd_response` judges a *raw* response (under 3000 characters of HTML is a JS shell to it), so it
    must never be pointed at text we have already extracted. This one may be.
    """

    fn = _load("antibot")
    if fn is None:
        return None
    try:
        return str(fn(str(content or "")) or "")
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


def article_body(html_or_text: Any) -> Optional[str]:
    fn = _load("article_body")
    if fn is None:
        return None
    try:
        return str(fn(str(html_or_text or "")))
    except Exception:  # noqa: BLE001
        return None


def fetch_verdict(status_code: Any, body: Any) -> Optional[str]:
    fn = _load("fetch_verdict")
    if fn is None:
        return None
    try:
        return str(fn(int(status_code), str(body or "")))
    except Exception:  # noqa: BLE001
        return None


# ---- the output shape, checked by the host's own validators ----------------------------------------------------

def document_reason(companies: Sequence[Mapping[str, Any]], *, schema: str = V6) -> Optional[str]:
    """'' when the host validator accepts the document, its complaint when it does not, None when unknown."""

    fn = _load("output_document")
    if fn is None:
        return None
    try:
        fn(json.dumps({"companies": list(companies)}).encode("utf-8"), expected_schema_version=schema,
           require_intent_dates=False)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:200]}"


def row_reason(row: Mapping[str, Any]) -> Optional[str]:
    """'' when the scorer's internal company model accepts the row, its complaint when it does not."""

    normalize, model = _load("normalized_company"), _load("company_model")
    if normalize is None or model is None:
        return None
    try:
        model(**normalize(dict(row), integrity_policy=True))
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:200]}"


# ---- self-test: one input that must pass and one that must fail, per gate ---------------------------------------

_PUBLIC_QUOTE = ("Acme Analytics, Inc. (NASDAQ: ACME) today reported results for the quarter.")
_SERIES_A_QUOTE = "Acme Analytics today announced it has raised $12 million in Series A funding led by Example Ventures."


def _checks() -> dict[str, bool]:
    icp = {"icp_id": "t", "industry": "Software", "intent_signals": ["Launched a product in the last 90 days"],
           "employee_count": ["11-50", "51-200"], "max_companies": 5, "bonus_intents": []}
    names = identity_names("Acme Analytics")
    page = "Acme Analytics today announced it has raised $12 million in Series A funding led by Example Ventures."
    return {
        "stage_norm": normalize_stage("Series C+") == "series c+" and normalize_stage("series_a") == "series a",
        "canonical_stages": canonical_stage("Series C+") is True and canonical_stage("Nonsense") is False,
        "stage_lexical": known("stage_lexical"),
        "stage_evidence": stage_verdict("Series A", _SERIES_A_QUOTE, url="https://news.example.com/acme",
                                        name="Acme Analytics", website="https://acme-analytics.io/") is True
        and stage_verdict("Series A", "Acme Analytics builds reporting software.",
                          url="https://news.example.com/acme", name="Acme Analytics",
                          website="https://acme-analytics.io/") is False,
        "acquired_names": stage_verdict("Acquired", "Acme Analytics was acquired by Beta Corp in 2025.",
                                        name="Acme Analytics") is True
        and stage_verdict("Acquired", "Acme Analytics builds software.", name="Acme Analytics") is False,
        "venture_stage_named": stage_verdict("Series B", _SERIES_A_QUOTE, url="https://news.example.com/acme",
                                             name="Acme Analytics") is False,
        "semantic_listing": known("semantic_listing"),
        "sec_listing": known("sec_listing"),
        "exchange_profile": known("exchange_profile"),
        "headcount_quote": headcount_verdict("Company size: 120 employees", "120") is True
        and headcount_verdict("The company has about 120 employees", "120") is False,
        "headquarters_quote": headquarters_verdict("Acme Analytics is headquartered in Austin, Texas.",
                                                   country="United States", state="Texas") is True
        and headquarters_verdict("Acme Analytics builds software.", country="United States", state="Texas") is False,
        "headquarters_named": known("headquarters_named"),
        "names_company": names_company("Acme Analytics raised a round", names) is True
        and names_company("Beta Corp raised a round", names) is False,
        "quote_occurs": occurs("raised $12 million in Series A funding", page) is True
        and occurs("raised $40 million in Series B funding", page) is False,
        "quote_surface": isinstance(page_surface("<p>Hello</p>"), str),
        "span": compact("Acme Analytics, Inc.") == "acmeanalyticsinc",
        "visible_text": "Hello" in (visible_text("<html><body><p>Hello</p><script>x</script></body></html>") or "")
        and visible_text("<html><body><script>x</script></body></html>") == "",
        "article_body": known("article_body"),
        "fetch_verdict": fetch_verdict(404, "") == "http_404",
        "antibot": bool(antibot("Just a moment... Checking your browser"))
        and antibot("A long and ordinary article about a company. " * 40) == "",
        "country": country_ok("United States", "United States") is True and country_ok("", "United States") is False,
        "data_quality": data_quality_ok("Acme Analytics", "https://acme-analytics.io/") is True
        and data_quality_ok("", "https://acme-analytics.io/") is False,
        "exclusion": excluded("Acme", "https://acme.io/", "", ["acme.io"]) is True
        and excluded("Beta", "https://beta.io/", "", ["acme.io"]) is False,
        "untrusted_source": untrusted_source("https://acme.io/x", "https://acme.io/") == "",
        "stage_conflict": isinstance(stage_conflicts("Acme", ["https://news.io/acme-raises-40m-series-b"],
                                                     "Series A"), list),
        "region_states": "California" in (region_states("United States, West Coast") or frozenset())
        and not region_states("Canada"),
        "us_states": us_states("United States, Texas") == frozenset({"Texas"}),
        "icp_buckets": icp_buckets(icp) == ["11-50", "51-200"],
        "company_goal": company_goal(icp) == 5,
        "evidence_source": evidence_source("https://acme-analytics.io/news/a", "https://acme-analytics.io/")
        == "company_website",
        "normalized_company": known("normalized_company"),
        "registrable": registrable_domain("https://www.news.acme.co.uk/x") == "acme.co.uk",
        "bucket_norm": normalize_bucket("51-200") == "51-200" and normalize_bucket("plenty") == "",
        "paragraph": paragraph_ok("One plain paragraph about the company.") is True
        and paragraph_ok("- a bullet") is False,
        "output_document": known("output_document"),
        "company_model": known("company_model"),
    }


def self_test() -> dict[str, str]:
    """'pass', 'fail' (imported but disagreeing with what we believe it does) or 'unknown' (not importable)."""

    try:
        results = _checks()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {name: ("unknown" if not known(name) else ("pass" if results.get(name) else "fail")) for name in SYMBOLS}


def report(stream: Any = None) -> dict[str, str]:
    verdicts = self_test()
    counts = {key: sum(1 for value in verdicts.values() if value == key) for key in ("pass", "fail", "unknown")}
    print("[judge] source=%s %s %s" % (SOURCE_DIRS, json.dumps(counts), json.dumps(verdicts, sort_keys=True)),
          file=stream or sys.stderr, flush=True)
    return verdicts


__all__ = [name for name in dir() if not name.startswith("_")]
