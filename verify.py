"""Post-LLM verification and repair, applying the scorer's own rules first."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Optional

from . import criteria
from . import gates
from . import scorer_mirror as sm
from .arena_tools import ArenaTools, Page

try:
    from . import identity as idn, intent_details as idm
except ImportError:
    import importlib as _importlib
    import os as _os
    _pkg = _os.path.basename(_os.path.dirname(_os.path.abspath(__file__)))
    idm, idn = (_importlib.import_module(f"{_pkg}.{m}") for m in ("intent_details", "identity"))

WINDOW_WORDS = 34
MIN_SNIPPET_WORDS = 8
SIGNAL_OUTPUT_CAP = 3
PRIMARY_URL_CAP = 2
BONUS_URL_CAP = 1


class Report:
    def __init__(self) -> None:
        self.dropped_companies: list[tuple[str, str]] = []
        self.dropped_signals: list[tuple[str, str, str, str]] = []  # (company, url, reason, code)
        self.repaired: list[tuple[str, str]] = []
        self.kept: list[dict[str, Any]] = []
        self.surplus: list[dict[str, Any]] = []
        self.evidence: dict[str, list[dict[str, Any]]] = {}

    def as_dict(self) -> dict[str, Any]:
        return {"dropped_companies": self.dropped_companies, "dropped_signals": self.dropped_signals,
                "repaired": self.repaired, "kept": [c.get("company_name") for c in self.kept],
                "surplus": [c.get("company_name") for c in self.surplus]}

    def counts(self) -> dict[str, Any]:
        """Bounded, reconcilable counters for stderr."""

        return {"company_drops": _histogram(_company_drop_code, (r for _n, r in self.dropped_companies)),
                "signal_drops": _histogram(str, (c for _n, _u, _r, c in self.dropped_signals)),
                "repairs": _histogram(_repair_code, (r for _n, r in self.repaired))}

    def details(self, limit: int = 12) -> dict[str, Any]:
        """The first dropped companies and signals with their reasons, bounded for stderr."""

        return {"companies": [[n[:60], r[:160]] for n, r in self.dropped_companies[:limit]],
                "signals": [[n[:60], u[:120], c, r[:120]] for n, u, r, c in self.dropped_signals[:limit]]}


def _histogram(code_of, reasons) -> dict[str, int]:
    out: dict[str, int] = {}
    for reason in reasons:
        code = code_of(str(reason or ""))
        out[code] = out.get(code, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _first_code(reason: str, table: tuple[tuple[str, str], ...], fallback: str) -> str:
    lowered = reason.lower()
    for marker, code in table:
        if marker in lowered:
            return code
    return fallback


_COMPANY_DROP_CODES = (
    ("missing company_name", "missing_name"),
    ("company_name contains controls", "name_injection"),
    ("company_website is not", "bad_website"),
    ("duplicate company", "duplicate_company"),
    ("exclusion list", "excluded_by_icp"),
    ("missing country", "missing_country"),
    ("not allowed by icp", "country_mismatch"),
    ("employee_count is not a linkedin bucket", "bucket_unparseable"),
    ("employee bucket", "bucket_mismatch"),
    ("does not match icp", "stage_mismatch"),
    ("required_attribute not validated", "attr_missing"),
    ("required_attribute evidence_url", "attr_bad_url"),
    ("required_attribute evidence unavailable", "attr_page_unavailable"),
    ("required_attribute quote", "attr_quote_absent"),
    ("no index-0", "no_primary_signal"),
    ("no verifiable intent signal", "no_verifiable_signal"),
    ("(judge: identity mismatch)", "identity_redirect_or_parked"),
)

_REPAIR_CODES = (
    ("snippet re-cut", "snippet_recut"),
    ("description rebuilt", "description_rebuilt"),
    ("ungrounded signal words", "description_pruned"),
    ("no linkedin.com/company link", "no_homepage_linkedin"),
    ("required_attribute quote re-cut", "attr_quote_recut"),
    ("index-0 (primary) evidence verified first", "primary_reordered"),
    ("re-pointed", "website_repointed"), ("(homepage brand)", "name_brand"), ("probe unusable", "probe_unusable"),
)


def _company_drop_code(reason: str) -> str:
    return _first_code(reason, _COMPANY_DROP_CODES, "other")


def _repair_code(reason: str) -> str:
    return _first_code(reason, _REPAIR_CODES, "other")


def _words(text: str) -> list[str]:
    return str(text or "").split()


def best_window(page_text: str, *, focus_terms: list[str], prefer_text: str = "",
                window: int = WINDOW_WORDS, company_name: str = "") -> str:
    """The verbatim page window that best matches the focus terms."""

    words = _words(page_text)
    if len(words) < MIN_SNIPPET_WORDS:
        return " ".join(words)
    if len(words) <= window:
        return " ".join(words)
    focus = {w for w in sm.normalize_text(" ".join(focus_terms)).split() if len(w) >= 3}
    prefer = {w for w in sm.normalize_text(prefer_text).split() if len(w) >= 4}
    name_words = {w for w in sm.normalize_text(company_name).split() if len(w) >= 3}
    norm = [sm.normalize_text(w) for w in words]
    dirty = [1.0 if sm.contains_prompt_control(w) else 0.0 for w in words]
    best_score, best_start = -1.0, 0
    step = max(1, window // 3)
    for start in range(0, len(words) - window + 1, step):
        chunk = norm[start : start + window]
        chunk_set = set(chunk)
        score = 3.0 * len(chunk_set & focus) + 1.0 * len(chunk_set & prefer) + 2.0 * len(chunk_set & name_words)
        score += 1.5 * len(chunk_set & sm.SIGNAL_WORDS)
        if re.search(r"\b20\d\d\b", " ".join(chunk)):
            score += 1.0
        score -= 2.0 * sum(dirty[start : start + window])
        if score > best_score:
            best_score, best_start = score, start
    return " ".join(words[best_start : best_start + window])


def _fit_snippet(text: str, limit: int = sm.GATEWAY_SNIPPET_MAX) -> str:
    text = " ".join(sm.strip_gateway_controls(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[: cut.rfind(" ")] if " " in cut else cut


def _fit_description(text: str, limit: int = sm.GATEWAY_DESCRIPTION_MAX) -> str:
    return _fit_snippet(text, limit)


def _page_for(tools: ArenaTools, url: str) -> Page:
    page = tools.pages.get(url)
    if page is not None and page.ok:
        return page
    return tools.fetch_page(url)


_MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December"]


def date_visible(value: Any, text: str, limit: int = 6000) -> bool:
    """Does the page show this date where the judge's paragraph reviewer can see it?"""

    try:
        day = date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return False
    head = " ".join(str(text or "")[:limit].split())
    low = head.casefold()
    month, short = _MONTHS_LONG[day.month - 1], _MONTHS_LONG[day.month - 1][:3]
    forms = [day.isoformat(), f"{month} {day.day}, {day.year}", f"{short} {day.day}, {day.year}", f"{short}. {day.day}, {day.year}",
             f"{day.day} {month} {day.year}", f"{day.day} {short} {day.year}", f"{day.month:02d}/{day.day:02d}/{day.year}",
             f"{day.day:02d}/{day.month:02d}/{day.year}", f"{day.year}/{day.month:02d}/{day.day:02d}",
             f"{month} {day.day:02d}, {day.year}", f"{day.day:02d} {month} {day.year}", f"{short} {day.day:02d}, {day.year}"]
    return any(form.casefold() in low for form in forms)


def date_admitted(value: Any, page: Any, limit: int = 6000) -> bool:
    """May the paragraph state this date for the page?  When the page shows it in its first ``limit`` characters,
    or when it is the publication date the judge itself reads from the page's metadata
    (intent_verification_three_stage._published_date_from_html)."""

    if not value or page is None or not getattr(page, "ok", False):
        return False
    return date_visible(value, getattr(page, "text", ""), limit) or \
        bool(getattr(page, "published", "")) and str(getattr(page, "published", ""))[:10] == str(value)[:10]


def complete_partial_tail(description: str, text: str) -> str:
    """When the description's."""

    words = str(description or "").split()
    if len(words) < 3:
        return description
    tail = " ".join(words[-4:])
    flat = " ".join(str(text or "").split())
    match = re.search(re.escape(tail) + r"([A-Za-z]{1,20})\b", flat, re.I)
    if match is None:
        return description
    return description + match.group(1)


def _ordinal_day(value: Any) -> Optional[date]:
    return sm.parse_signal_date(value)


_CURRENT_STATE_RE = re.compile(
    r"\b(hiring|hires?|recruit\w*|open(?:\s+\w+){0,2}\s+(?:roles?|positions?|jobs?|vacanc\w*)|"
    r"job opening\w*|vacanc\w*|is looking for|careers?)\b", re.I)


def integrity_policy(icp: Mapping[str, Any]) -> bool:
    """Is this round judged under arena_integrity_v1?"""

    marker = str(icp.get("integrity_policy") or "").strip()
    if marker:
        return marker == "arena_integrity_v1"
    return sm.v5_family(announced_schema(icp))


def _evidence_domain(signal: Mapping[str, Any], website: str) -> str:
    """The scorer's dedup key for this candidate's URL, or "" when it has none."""

    try:
        return sm.extract_domain(sm.public_http_url(str(signal.get("url") or "")))
    except (ValueError, TypeError):
        return ""


def _is_primary(signal: Mapping[str, Any]) -> bool:
    """Index 0 is the ICP's PRIMARY intent; every other index is a bonus."""

    idx = signal.get("matched_icp_signal")
    return isinstance(idx, int) and not isinstance(idx, bool) and idx == 0


def order_candidates(candidates: list[Mapping[str, Any]], *, website: str) -> tuple[list[Mapping[str, Any]], str]:
    """Source-weight order, with ONE narrow exception for the primary signal."""

    ordered = list(candidates)
    primary_at = next((i for i, sg in enumerate(ordered) if _is_primary(sg)), None)
    if primary_at is None or primary_at == 0:
        return ordered, ""
    primary = ordered[primary_at]
    domain = _evidence_domain(primary, website)
    ahead = [_evidence_domain(sg, website) for sg in ordered[:primary_at]]
    if domain and domain in ahead:
        reason = "same-domain collision with higher-weight evidence (%s)" % domain
    elif len({d for d in ahead if d}) >= SIGNAL_OUTPUT_CAP:
        reason = "the %d-signal output cap would cut it" % SIGNAL_OUTPUT_CAP
    else:
        return ordered, ""
    return [primary] + [sg for i, sg in enumerate(ordered) if i != primary_at], reason


def company_named(name: str, website: str, text: str, aliases: Any = ()) -> bool:
    """Is this company identifiable on the page -- by name, its domain, its domain label or a verified alias?  A page
    that says 'ABC' names Alpha Beta Capital (abc.com): the judge's pre-check looks for the domain label, and its
    entity judge accepts the brand."""

    return idn.named_on_page(text, name, website, aliases)


def signal_out(record: Mapping[str, Any], *, v5: bool) -> dict[str, Any]:
    """The per-schema signal payload from an internal verified record."""

    if v5:
        return {"matched_icp_signal": record["matched_icp_signal"], "description": record["description"],
                "date": record["date"], "url": record["url"]}
    return {k: v for k, v in record.items() if not k.startswith("_")}


def verify_signal(signal: Mapping[str, Any], *, company: Mapping[str, Any], icp: Mapping[str, Any],
                  tools: ArenaTools, today: date, seen_domains: set[str], report: Report,
                  signals_spec: list[dict[str, Any]], buyer_cap: int) -> Optional[dict[str, Any]]:
    name = str(company.get("company_name") or "")
    website = str(company.get("company_website") or "")
    url = str(signal.get("url") or "").strip()

    def drop(reason: str, code: str) -> None:
        report.dropped_signals.append((name, url, reason, code))

    try:
        url = sm.public_http_url(url)
    except ValueError:
        drop("url is not a public http url", "signal_bad_url")
        return None
    idx = signal.get("matched_icp_signal")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0 or idx >= len(signals_spec):
        drop(f"matched_icp_signal {idx!r} out of range 0..{len(signals_spec) - 1}", "signal_index_out_of_range")
        return None
    reason = sm.url_structural_reason(url)
    if reason:
        drop(reason, "signal_url_path_not_evidence")
        return None
    reason = sm.untrusted_source_reason(url, website)
    if reason:
        drop(reason, "signal_untrusted_source")
        return None
    integrity = integrity_policy(icp)
    spec = signals_spec[idx]
    domain = sm.extract_domain(url)
    dedup_key = f"{idx}|{url}" if integrity else domain
    if dedup_key in seen_domains:
        drop("duplicate evidence url for this criterion" if integrity else "duplicate evidence domain on this company",
             "signal_duplicate_url" if integrity else "signal_duplicate_domain")
        return None
    reason = sm.future_date_reason(signal.get("date"), today)
    if reason:
        drop(reason, "signal_future_date")
        return None
    undated_ok = False
    if sm.parse_signal_date(signal.get("date")) is None:
        drop("signal has no parseable event date", "signal_undated")
        return None
    if not criteria.in_window(icp, idx, signal.get("date")):
        drop(f"signal date {signal.get('date')} outside the criterion window", "signal_outside_window")
        return None
    reason = criteria.url_admissible(icp, idx, url, website)
    if reason:
        drop(reason, "signal_url_not_admissible")
        return None
    page = _page_for(tools, url)
    if not page.ok:
        drop(f"evidence page unavailable ({page.error})", "signal_page_unavailable")
        return None
    text = page.text
    reason = sm.antibot_reason(text)
    if reason:
        drop(reason, "signal_antibot_page")
        return None
    source = sm.evidence_source(url, website)
    if source not in {"job_board", "review_site", "wikipedia"} and \
            not company_named(name, website, text[:12000], company.get("aliases") or ()):
        drop("company name not in evidence page", "signal_name_not_on_page")
        return None

    description = " ".join(sm.strip_prompt_controls(signal.get("description") or "").split())
    snippet = " ".join(sm.strip_prompt_controls(signal.get("snippet") or "").split())
    why_now = " ".join(sm.strip_prompt_controls(signal.get("why_now") or "").split())
    signal_text = spec["text"]
    for label, value in (("description", description), ("snippet", snippet)):
        hit = sm.injection_match(value)
        if hit:
            report.repaired.append((name, f"{label} contained an injection phrase ({hit!r}); rebuilt"))
            if label == "description":
                description = ""
            else:
                snippet = ""

    if len(snippet.split()) < MIN_SNIPPET_WORDS or sm.snippet_overlap(snippet, text) < 0.6:
        snippet = best_window(text, focus_terms=[signal_text, description], prefer_text=snippet, company_name=name)
        report.repaired.append((name, "snippet re-cut from page"))
        _focus = {w for w in sm.normalize_text(signal_text).split() if len(w) >= 4}
        if _focus and not (_focus & set(sm.normalize_text(snippet).split())):
            drop("re-cut snippet carries no ICP-signal term", "signal_recut_off_topic")
            return None
    snippet = _fit_snippet(snippet)
    if sm.snippet_overlap(snippet, text) < 0.5 or len(snippet.split()) < 4:
        drop("could not obtain a verbatim snippet", "signal_no_verbatim_snippet")
        return None
    grounded, total, _ = sm.signal_word_grounding(snippet, text)
    if total > 0 and grounded == 0:
        drop("snippet signal words not on page", "signal_words_absent")
        return None

    grounding = sm.description_grounding(description, text)
    grounded, total, ungrounded = sm.signal_word_grounding(description, text)
    if not description or grounding < 0.4 or (total > 0 and grounded == 0):
        description = _fit_description(f"{name}: {snippet}")
        report.repaired.append((name, "description rebuilt from snippet"))
    elif ungrounded:
        cleaned = " ".join(w for w in description.split() if sm.normalize_text(w) not in set(ungrounded))
        description = _fit_description(cleaned or f"{name}: {snippet}")
        report.repaired.append((name, "ungrounded signal words removed from description"))
    description = _fit_description(complete_partial_tail(description, text))
    reason = sm.negation_reason(description, snippet)
    if reason:
        drop(reason, "signal_negated")
        return None
    posting: dict[str, str] = {}
    if idm.is_posting_url(url):
        try:
            posting = idm.posting_fields([signal.get("description"), description], text)
            if posting.get("title") and idm.is_field_dump(description, name):
                description = _fit_description(idm.posting_description(name, posting))
                report.repaired.append((name, "posting description rebuilt from its title"))
        except Exception:
            posting = {}
    event_day = _ordinal_day(signal.get("date"))
    if event_day is None and not undated_ok:
        drop("signal has no parseable event date", "signal_undated")
        return None
    if not why_now or sm.injection_match(why_now):
        report.repaired.append((name, "why_now rebuilt from the verified event"))
        dated = f" (event dated {event_day.isoformat()})" if event_day else ""
        why_now = f"{description}{dated}, so outreach now is timely."
    why_now = _fit_snippet(why_now, sm.GATEWAY_WHY_NOW_MAX)
    if sm.injection_match(description) or sm.injection_match(snippet):
        drop("injection phrase survived repair", "signal_injection")
        return None

    seen_domains.add(dedup_key)
    return {
        "matched_icp_signal": idx,
        "description": description,
        "date": event_day.isoformat() if event_day else None,
        "why_now": why_now,
        "url": url,
        "snippet": snippet,
        "_quote": snippet,
        "_posting": posting,
        "_signal_text": signal_text,
        "_source": source,
        "_estimate": 60.0 * sm.SOURCE_MULTIPLIERS.get(source, 0.5),
    }


def _scout_module():
    try:
        from . import scout
    except ImportError:
        import importlib
        import os as _os

        scout = importlib.import_module(f"{_os.path.basename(_os.path.dirname(_os.path.abspath(__file__)))}.scout")
    return scout


def name_aliases(raw: Mapping[str, Any], *, name: str, website: str, tools: Any) -> list[str]:
    """Other names pages may use for the company: its cached homepage's brand and the exchange ticker its own Public
    stage quote binds to its name ('NYSE: ABC')."""

    out: list[str] = []
    page = getattr(tools, "pages", {}).get(website)
    if page is not None and getattr(page, "ok", False):
        home = idn.brand(name, idn.home_names(str(getattr(page, "title", "") or "")), idn.domain_label(website))
        out += [home] if home != idn.clean_name(name) else []
    try:
        out.append(_scout_module().own_ticker_sentence(str(raw.get("stage_evidence_quote") or ""), name, ticker=True))
    except Exception:  # noqa: BLE001
        pass
    return [a for a in out if a]


def _paragraph_quote(record: Mapping[str, Any]) -> str:
    """The paragraph writer's quote -- a posting's admitted fields, else the verbatim snippet."""

    try:
        posting = record.get("_posting") or {}
        if posting.get("title"):
            return idm.posting_quote(posting, record.get("description"))
    except Exception:
        pass
    return str(record.get("_quote") or "")


def stage_evidence(raw: Mapping[str, Any], *, name: str, stage: str, tools: ArenaTools,
                   signals: list[dict[str, Any]], report: Report, website: str = "",
                   stats: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Up to three {url, quote} passages the investigator may use for stage (v5).

    Every item passes scout.stage_quote_ok (verbatim, fetchable host, names the company, proves the stage).  For
    venture and PE stages the judge's deterministic _stage_evidence_supports_observation must also accept the quote.
    For Public it is only recorded in ``stats['judge_pass']``: that function knows tickers for NASDAQ/NYSE only, and
    the judge's stage decision passes on its investigator's own observation as well, so an issuer-bound ASX / TSX /
    LSE line is still sent (ranked lower by admission).  A Public line on a page other than the company's own or a
    wire copy is not sent; when it passes the same check on its cached page it is counted in
    ``stats['withheld_proof']``.
    """

    try:
        _scout = _scout_module()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    candidates: list[tuple[str, str]] = []
    url = str(raw.get("stage_evidence_url") or "").strip()
    quote = " ".join(str(raw.get("stage_evidence_quote") or "").split())
    if url:
        candidates.append((url, quote))
    for extra in raw.get("stage_evidence_more") or []:
        if isinstance(extra, Mapping) and str(extra.get("url") or "").strip():
            candidates.append((str(extra["url"]).strip(), " ".join(str(extra.get("quote") or "").split())))
    stage_key = sm.normalize_text(stage) if stage else ""
    for signal in signals:
        text = sm.normalize_text(signal.get("_quote") or "")
        if stage_key and stage_key in text:
            candidates.append((signal["url"], signal.get("_quote") or ""))
    for cand_url, cand_quote in candidates:
        if len(out) >= sm.STAGE_EVIDENCE_MAX:
            break
        try:
            cand_url = sm.public_http_url(cand_url)
        except ValueError:
            continue
        if any(item["url"] == cand_url for item in out):
            continue
        if stage_key == "public":
            try:
                from .sourcetype import source_kind
                kind = source_kind(cand_url, sm.registrable_host(website) if website else "", name)
            except Exception as exc:
                kind = f"check failed: {type(exc).__name__}"
            if kind not in ("first_party", "wire"):
                report.repaired.append((name, f"stage evidence withheld (listing on a {kind} page): {cand_url[:80]}"))
                cached = tools.pages.get(cand_url)
                if stats is not None and not kind.startswith("check failed") and cached is not None and cached.ok:
                    try:
                        proven = not _scout.stage_quote_ok(cand_quote, cached.text, name=name, stage=stage,
                                                           url=cand_url, website=website)
                    except Exception:  # noqa: BLE001
                        proven = False
                    stats["withheld_proof"] = int(stats.get("withheld_proof") or 0) + (1 if proven else 0)
                continue
        page = _page_for(tools, cand_url)
        if not page.ok:
            continue
        try:
            why = _scout.stage_quote_ok(cand_quote, page.text, name=name, stage=stage, url=cand_url, website=website)
            judged = gates.stage_evidence_ok(stage, cand_quote, url=cand_url,
                                             first_party_domains=[d for d in (gates.registrable_domain(website),) if d],
                                             identity_names=[name])
            if not why and judged is False and stage_key != "public":
                why = "the judge's stage check rejects the quote"
        except Exception as exc:
            why = f"check failed: {type(exc).__name__}"
        if why or sm.injection_match(cand_quote) or sm.strip_prompt_controls(cand_quote) != cand_quote:
            report.repaired.append((name, f"stage evidence withheld ({why or 'controls'}): {cand_url[:80]}"))
            continue
        out.append({"url": cand_url, "quote": cand_quote})
        if stats is not None:
            stats["judge_pass"] = int(stats.get("judge_pass") or 0) + (1 if judged is True else 0)
    if out:
        report.repaired.append((name, "company_stage_evidence attached (%d)" % len(out)))
        if stage_key == "public" and stats is not None and not stats.get("judge_pass"):
            report.repaired.append((name, "Public listing line not matched by the judge's deterministic check "
                                          "(non-NASDAQ/NYSE form): kept, ranked lower"))
    return out


_VENTURE_STAGES = ("seed", "series a", "series b", "series c+")


def attribute_out(claim: Mapping[str, Any], *, name: str, tools: ArenaTools, report: Report) -> Optional[dict[str, Any]]:
    """The required_attribute object as proven on the company's own page: the quote must be verbatim there (no
    re-cut); ``passed`` stays true only when that holds.  None when the object cannot be emitted."""

    try:
        evidence_url = sm.public_http_url(str(claim.get("evidence_url") or ""))
    except ValueError:
        return None
    quote = " ".join(str(claim.get("evidence_quote") or "").split())
    if not quote or "linkedin.com" in evidence_url.lower():
        return None
    passed = bool(claim.get("passed"))
    page_text = ""
    try:
        from .identity import probe
        got = probe(tools, evidence_url)
        page_text = (gates.visible_text(got.get("html") or "") or "") if (got.get("status") or 500) < 400 else ""
    except Exception:  # noqa: BLE001
        page_text = ""
    if not page_text:
        page = _page_for(tools, evidence_url)
        page_text = page.text if page.ok else ""
    if page_text and " ".join(quote.split()).casefold() not in " ".join(page_text.split()).casefold():
        passed = False
        report.repaired.append((name, "required_attribute quote not verbatim on its page: passed=false"))
    explanation = " ".join(sm.strip_prompt_controls(claim.get("explanation") or "").split()) or \
        f"The quoted sentence describes {name}'s own offering."
    if sm.injection_match(explanation) or sm.injection_match(quote) or sm.strip_prompt_controls(quote) != quote:
        return None
    return {"text": str(claim.get("text") or "")[: sm.GATEWAY_CLAIM_TEXT_MAX], "passed": passed,
            "evidence_url": evidence_url, "evidence_quote": quote[: sm.GATEWAY_CLAIM_TEXT_MAX],
            "explanation": explanation[: sm.GATEWAY_CLAIM_TEXT_MAX]}


def verify_company(raw: Mapping[str, Any], *, icp: Mapping[str, Any], tools: ArenaTools, today: date,
                   report: Report, seen_names: set[str], allowed_buckets: list[str],
                   signals_spec: list[dict[str, Any]], buyer_cap: int,
                   schema: str = sm.SCHEMA_V1) -> Optional[dict[str, Any]]:
    name = " ".join(sm.strip_gateway_controls(raw.get("company_name")).split())[: sm.GATEWAY_NAME_MAX]

    def drop(reason: str) -> None:
        report.dropped_companies.append((name or "?", reason))

    if not name:
        drop("missing company_name")
        return None
    key = sm.company_name_key(name)
    if key in seen_names:
        drop("duplicate company")
        return None
    try:
        website = sm.public_http_url(str(raw.get("company_website") or ""))
    except ValueError:
        drop("company_website is not a public http url")
        return None
    excluded_entries = [str(v).strip() for v in (icp.get("excluded_companies") or []) if str(v).strip()]

    def is_excluded(n: str, site: str, linkedin: str = "") -> bool:
        hit = gates.excluded(n, site, linkedin, excluded_entries)
        if hit is not None:
            return hit
        low = [e.lower() for e in excluded_entries]
        return bool(low) and (n.lower() in low or sm.registrable_host(site) in low
                              or sm.company_name_key(n) in {sm.company_name_key(e) for e in low})

    if is_excluded(name, website, str(raw.get("company_linkedin") or "")):
        drop("matches ICP exclusion list")
        return None
    if gates.data_quality_ok(name, website) is False:
        drop("company data quality")
        return None
    industry = " ".join(sm.strip_prompt_controls(str(raw.get("industry") or "")).split())
    if not industry:
        drop("missing industry")
        return None
    bucket = gates.normalize_bucket(raw.get("employee_count"))
    if bucket is None:
        bucket = sm.any_bucket(raw.get("employee_count"))
    if not bucket:
        drop("employee_count is not a LinkedIn bucket")
        return None
    if allowed_buckets and bucket not in allowed_buckets:
        drop(f"employee bucket {bucket} not in ICP {allowed_buckets}")
        return None
    icp_country = str(icp.get("country") or icp.get("geography") or "").strip()
    country = str(raw.get("country") or "").strip()
    verdict = gates.country_ok(country, icp_country)
    if verdict is None:
        allowed, echo = sm.allowed_countries(icp_country)
        verdict = bool(country) and (not allowed or sm.normalize_country(country) in allowed)
    if not verdict:
        drop(f"country {country!r} not allowed by ICP {icp_country!r}")
        return None
    icp_stage = sm.normalize_stage(icp.get("company_stage"))
    stage = str(raw.get("company_stage") or "").strip()
    if icp_stage:
        observed = sm.normalize_stage(stage)
        if observed and not sm.stage_matches(observed, icp_stage):
            drop(f"stage {stage!r} does not match ICP {icp.get('company_stage')!r}")
            return None
        if not observed and icp_stage not in _VENTURE_STAGES:
            drop(f"stage unproven for a {icp.get('company_stage')} ICP")
            return None
        stage = str(icp.get("company_stage")).strip() if observed else ""
    claim_out: Optional[dict[str, Any]] = None
    claim = raw.get("required_attribute") if isinstance(raw.get("required_attribute"), Mapping) else None
    if claim and str(icp.get("required_attribute") or "").strip():
        claim_out = attribute_out(claim, name=name, tools=tools, report=report)
    if claim_out and icp_stage:
        try:
            try:
                from . import reverify as _rv
            except ImportError:
                import importlib
                import os as _os

                _rv = importlib.import_module(
                    f"{_os.path.basename(_os.path.dirname(_os.path.abspath(__file__)))}.reverify")
            conflict = _rv.first_party_ownership_conflicts_with_stage(
                website, name, "", icp_stage, claim_out["evidence_url"], claim_out["evidence_quote"])
        except Exception:
            conflict = False
        if conflict:
            drop("the required-attribute page on the company's own domain proves a current owner; "
                 "the judge leaves the pinned stage unresolved")
            return None
    seen_domains: set[str] = set()
    signals: list[dict[str, Any]] = []
    candidates = [sg for sg in (raw.get("intent_signals") or []) if isinstance(sg, Mapping)]
    candidates.sort(key=lambda sg: -sm.SOURCE_MULTIPLIERS.get(sm.evidence_source(str(sg.get("url") or ""), website), 0.5))
    candidates, hoisted = order_candidates(candidates, website=website)
    if hoisted:
        report.repaired.append((name, "index-0 (primary) evidence verified first: " + hoisted))
    per_index: dict[int, int] = {}
    aliases = name_aliases(raw, name=name, website=website, tools=tools)
    first_drop = len(report.dropped_signals)
    for signal in candidates:
        index = signal.get("matched_icp_signal")
        if isinstance(index, int) and per_index.get(index, 0) >= (PRIMARY_URL_CAP if index == 0 else BONUS_URL_CAP):
            continue
        verified = verify_signal(signal, company={"company_name": name, "company_website": website, "aliases": aliases},
                                 icp=icp, tools=tools, today=today, seen_domains=seen_domains, report=report,
                                 signals_spec=signals_spec, buyer_cap=buyer_cap)
        if verified:
            signals.append(verified)
            per_index[verified["matched_icp_signal"]] = per_index.get(verified["matched_icp_signal"], 0) + 1
    if not signals:
        codes = sorted({d[3] for d in report.dropped_signals[first_drop:]})
        drop("no verifiable intent signal (%s)" % (", ".join(codes) or "no candidate signal"))
        return None
    has_primary = any(s["matched_icp_signal"] == 0 for s in signals)
    if not has_primary:
        drop("no index-0 (primary) intent signal: an unverified primary is a -10 false positive")
        return None
    home, got = None, idn.probe(tools, website)
    if not got["html"] or (got["status"] or 500) >= 400:
        try:
            home = tools.pages.get(website)
            if home is None or not home.ok or not home.links:
                home = tools.fetch_page(website, prefer_firecrawl=True)
        except Exception:
            home = None
    bound = idn.bind(tools, name=name, website=website, home=home)
    if bound["drop"]:
        drop(bound["drop"])
        return None
    report.repaired.extend((name, note) for note in bound["notes"])
    if bound["website"] != website:
        if claim_out and claim_out["evidence_url"] == website:
            claim_out["evidence_url"] = bound["website"]
        website = bound["website"]
    name, key = bound["name"], sm.company_name_key(bound["name"])
    if key in seen_names or is_excluded(name, website):
        drop("duplicate company or ICP exclusion after identity binding")
        return None
    linkedin, identity_linkable = bound["linkedin"], bool(bound["anchor"])
    fallback = str(raw.get("company_linkedin") or "")
    if not linkedin and (raw.get("_flags") or {}).get("linkedin_source") == "homepage" and \
            fallback.startswith("https://www.linkedin.com/company/") and bound["website"] == str(raw.get("company_website")) \
            and not fallback.rstrip("/").rsplit("/", 1)[-1].isdigit():
        linkedin = fallback
        report.repaired.append((name, "company_linkedin from the homepage link whose LinkedIn record lists this domain"))
    seen_names.add(key)
    fit_summary = " ".join(sm.strip_prompt_controls(raw.get("fit_summary") or "").split())
    if not fit_summary or sm.injection_match(fit_summary):
        primary = next(s for s in signals if s["matched_icp_signal"] == 0)
        facts = [industry, f"{bucket} employees", country] + ([stage] if stage else [])
        report.repaired.append((name, "fit_summary rebuilt from verified fields"))
        fit_summary = f"{name} ({website}): {'; '.join(facts)}. Verified signal: {primary['description']}"
    if sm.injection_match(name) or sm.strip_prompt_controls(name) != name:
        drop("company_name contains controls or an injection phrase")
        return None
    fit_urls = []
    for value in [website] + list(raw.get("fit_evidence_urls") or []):
        try:
            u = sm.public_http_url(str(value))
        except ValueError:
            continue
        if u not in fit_urls:
            fit_urls.append(u)
    fit_guess = 2.0 if identity_linkable else 0.0
    if not identity_linkable:
        report.repaired.append((name, "no linkedin.com/company link on the homepage: identity rests on the exact name; ranked lower"))
    estimate = sm.company_score_estimate(fit_guess, [s["_estimate"] for s in signals])
    v5 = sm.v5_family(schema)
    state = " ".join(str(raw.get("state") or "").split())
    report.evidence[sm.company_name_key(name)] = [
        {"index": s["matched_icp_signal"], "description": s["description"],
         "date": s["date"], "url": s["url"],
         "quote": _paragraph_quote(s),
         "title": (s.get("_posting") or {}).get("title") or "",
         "signal_text": s.get("_signal_text") or "",
         "date_visible": bool(s["date"]) and date_admitted(s["date"], _page_for(tools, s["url"]))} for s in signals]
    if v5:
        stage_stats: dict[str, Any] = {}
        evidence_items = stage_evidence(raw, name=name, stage=stage, tools=tools, signals=signals, report=report,
                                        website=website, stats=stage_stats) if stage else []
        if stage and not evidence_items and (icp_stage == "public" or gates.known("stage_evidence")):
            if icp_stage in _VENTURE_STAGES:
                report.repaired.append((name, "no stage quote passed the judge's stage check: company_stage literal "
                                              "kept without evidence"))
            elif icp_stage == "public" and stage_stats.get("withheld_proof"):
                report.repaired.append((name, "Public listing line proven on a news page only: company_stage kept "
                                              "without stage evidence (the judge researches the listing), ranked lower"))
            elif icp_stage == "public":
                drop("no issuer-bound exchange:ticker or listing line")
                return None
            else:
                drop(f"no quoted line passes the judge's {icp.get('company_stage')} check")
                return None
        flags = dict(raw.get("_flags") or {})
        if str(icp.get("required_attribute") or "").strip() and flags.get("fit_tier") in ("A", "B") and \
                not (claim_out or {}).get("passed"):
            flags.update(fit_tier="C", ra_unverified=True)
            report.repaired.append((name, "required_attribute not verified on its page: fit tier C"))
        proven_stage = bool(stage) and (bool(evidence_items) or icp_stage not in _VENTURE_STAGES)
        flags.update(anchored=identity_linkable, linkedin_bound=bool(linkedin), stage_proven=proven_stage,
                     stage_evidence=len(evidence_items),
                     stage_judge_pass=bool(stage_stats.get("judge_pass")) if evidence_items else False)
        out: dict[str, Any] = {
            "company_name": name,
            "company_website": website,
            "company_linkedin": linkedin if isinstance(linkedin, str) else "",
            "industry": industry,
            "employee_count": bucket,
            "company_stage": stage,
            "country": country,
            "state": state,
            "intent_details": idm.fallback_paragraph(company_name=name, icp=icp, signals=signals),
            "intent_signals": [signal_out(s, v5=True) for s in signals],
            "company_stage_evidence": evidence_items,
            "required_attribute": claim_out,
            "_estimate": estimate,
            "_flags": flags,
            "_fit_proof": dict(raw.get("_fit_proof") or {}),
        }
        return out
    out = {
        "company_name": name,
        "company_website": website,
        "company_linkedin": linkedin,
        "industry": industry,
        "employee_count": bucket,
        "company_stage": stage,
        "country": country,
        "state": state,
        "fit_summary": _fit_snippet(fit_summary, 500),
        "fit_evidence_urls": fit_urls[:5],
        "intent_signals": [signal_out(s, v5=False) for s in signals],
        "required_attribute": claim_out,
        "_estimate": estimate,
    }
    return out


def announced_schema(icp: Mapping[str, Any]) -> str:
    """The output schema the round announces in the ICP, else v1."""

    value = str((icp or {}).get("output_schema_version") or "").strip()
    return value or sm.SCHEMA_V1


def verify_companies(drafts: list[Mapping[str, Any]], *, icp: Mapping[str, Any], tools: ArenaTools,
                     limit: int = 0, today: Optional[date] = None,
                     schema: Optional[str] = None) -> tuple[list[dict[str, Any]], Report]:
    """Every draft that passes verification, ranked, with its working fields (admission decides what is sent)."""

    today = today or sm.evaluation_date()
    schema = schema or announced_schema(icp)
    report = Report()
    signals_spec = sm.icp_signals(icp)
    buyer_cap = sm.icp_max_age_days(icp)
    allowed_buckets = gates.icp_buckets(icp) or sm.icp_buckets(icp)
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for draft in drafts:
        if not isinstance(draft, Mapping):
            continue
        try:
            company = verify_company(draft, icp=icp, tools=tools, today=today, report=report, seen_names=seen,
                                     allowed_buckets=allowed_buckets, signals_spec=signals_spec, buyer_cap=buyer_cap,
                                     schema=schema)
        except Exception as exc:
            report.dropped_companies.append((str(draft.get("company_name") or "?"), f"verifier error {type(exc).__name__}"))
            company = None
        if company:
            kept.append(company)
    kept.sort(key=lambda c: (not c.get("company_stage_evidence"), -c["_estimate"]))
    final: list[dict[str, Any]] = []
    for company in kept:
        cleaned = {k: v for k, v in company.items() if not k.startswith("_")}
        try:
            sm.validate_output([cleaned], max_companies=1, schema_version=schema)
        except Exception as exc:  # noqa: BLE001 - one company, never the payload
            report.dropped_companies.append(
                (str(cleaned.get("company_name") or "?"), f"output contract: {str(exc)[:100]}"))
            continue
        final.append(company)
    report.kept = [strip_internal(c) for c in final]
    report.surplus = []
    return final, report


def strip_internal(company: Mapping[str, Any]) -> dict[str, Any]:
    """The emitted row: every "_"-prefixed working field removed."""

    return {k: v for k, v in company.items() if not str(k).startswith("_")}


__all__ = ["verify_companies", "verify_company", "verify_signal", "best_window", "order_candidates",
           "SIGNAL_OUTPUT_CAP", "Report", "announced_schema", "strip_internal", "signal_out",
           "company_named", "stage_evidence", "integrity_policy"]
