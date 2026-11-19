"""The local TYCHE native tools, with provider calls sent through the Leadpoet broker.

This is tyche_tools.py's own server, with publication-field feedback on shared research reviews.
Codex already runs inside gVisor in Leadpoet, so there is no inner sandbox relay.
"""

import argparse
import copy
import difflib
from datetime import datetime, timedelta, timezone
import fcntl
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from . import pacing
from .broker import Broker
from . import native_counts
from .input import (COMPANY_LIST_RESULTS, OPENING_TOOL, STAGE_PREFIX, company_stage, intent_signal_ages, intent_signals, stage_line)
from .output import (ICP_FILE, LATER_SIGNAL, _registrable_host, companies, company_row, judge_unreachable_fields, listing_passage,
                     public_claim, publication_gaps)
import budget_guard
import confirmed_leads
import run_attempt
from research_tools import ResearchTools, SOURCE_TEXT_PAGE_SIZE, tolerant_arguments
from source_receipts import quote_form
import tyche_tools
from validate_run import (BONUS_PASS_APPROACH, _company_key, company_domain, signal_request_errors,
                          signal_window_start)


_ASCII_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def repaired_keys(value):
    """Drop stray non-ASCII characters from object keys, as in DeepSeek's "industry\u5c14" for "industry".

    Every tool schema field name is plain ASCII, so a key whose ASCII part is a field name and that is not already
    present is restored to that name. Values are never changed.
    """
    if isinstance(value, list):
        return [repaired_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    repaired = {}
    for key, item in value.items():
        if isinstance(key, str) and not key.isascii():
            plain = re.sub(r"[^\x00-\x7f]+", "", key).strip()
            if _ASCII_FIELD.fullmatch(plain) and plain not in value and plain not in repaired:
                key = plain
        repaired[key] = repaired_keys(item)
    return repaired


_CLAIM_DATE = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|"
                         r"December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\b\d{1,2}\s+(?:January|February|March|April|"
                         r"May|June|July|August|September|October|November|December)\s+\d{4}|\b\d{4}-\d{2}-\d{2}\b", re.I)
_CLAIM_FIGURE = re.compile(r"(?<![A-Za-z0-9])[$\u20ac\u00a3]?\d[\d,.]*(?:\s*(?:%|percent|million|billion|thousand|[MBK]\b))?(?![A-Za-z0-9])")
_CLAIM_DAY_RANGE = re.compile(r"\b(\d{1,2})\s*[-\u2013]\s*\d{1,2}(?=,?\s+\d{4})")
_CLAIM_YEAR = re.compile(r"^(?:19|20)\d{2}$")


def claim_problems(claim):
    """Named reasons a signal claim is less precise than the paragraph judge prefers; [] when it is plain."""
    problems = []
    if re.search(r"\bis hiring\b", claim, re.I):
        return problems  # 'X is hiring a Y.': role titles may carry numbers (e.g. 'Engineer II')
    body = _CLAIM_DATE.sub(" ", _CLAIM_DAY_RANGE.sub(r"\1", claim))
    # Standard designations are names, not figures: SOC 2, ISO 27001, PCI DSS 4.0, Phase 3, Tier 1, Class A.
    body = re.sub(r"\b(?:SOC|ISO|IEC|PCI(?:\s+DSS)?|NIST|HIPAA|FedRAMP|Phase|Tier|Level|Gen|Version|Series|Class|"
                  r"Stage|Type|Model|Section)\s?\d[\w.:-]*", " ", body)
    figures = [f for f in _CLAIM_FIGURE.findall(body) if not _CLAIM_YEAR.match(f.strip(" ,.$"))]
    if len(figures) >= 2:
        problems.append(f"{len(figures)} figures besides the date: {', '.join(f.strip() for f in figures[:4])}")
    # Only "Vermeer's MV Solutions ..." (a possessive followed by another proper name), not "McDonald's opened".
    if re.match(r"^\s*(?:\S+\s+){0,3}\S+?['\u2019]s\s+[A-Z][A-Za-z0-9&.-]*\s+[A-Z]", claim):
        problems.append("it opens with another entity in the 'X's Y' form")
    return problems


def final_review_status(run_file, *, limit=5):
    try:
        run_file = Path(run_file)
        document = json.loads(run_file.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Saved run is not an object")
        approval = document.get("final_review") or {}
        if not isinstance(approval, dict):
            raise ValueError("Final review is not an object")
        if not approval.get("review_ref"):
            return {"status": "missing_approval", "delivery_allowed": False}
        if approval["review_ref"] != run_attempt.review_fingerprint(document):
            return {"status": "stale_approval", "delivery_allowed": False}
        try:
            reviewed_at = datetime.fromisoformat(approval["reviewed_at"].replace("Z", "+00:00"))
            if reviewed_at.utcoffset() is None:
                raise ValueError("Final review time has no timezone")
            ResearchTools(run_file)._checked_review_findings(document, approval["findings"])
        except (KeyError, TypeError, AttributeError, ValueError):
            return {"status": "invalid_approval", "delivery_allowed": False}
        _, preflight = run_attempt.delivery_preflight(run_file, document)
        if preflight["errors"]:
            return {"status": "preflight_failed", "delivery_allowed": False,
                    "errors": preflight["errors"]}
        leads = json.loads((run_file.parent / "leads.json").read_text(encoding="utf-8"))
        if not isinstance(leads, dict):
            raise ValueError("Confirmed leads are not an object")
        if leads.get("leads") != document.get("accepted"):
            return {"status": "unconfirmed_snapshot", "delivery_allowed": False}
        icp = json.loads((run_file.parent / ICP_FILE).read_text(encoding="utf-8"))
        if not isinstance(icp, dict):
            raise ValueError("Arena ICP is not an object")
        issues = publication_gaps(leads.get("leads") or [], icp, document["request"])
        if issues:
            return {"status": "output_unpublishable", "delivery_allowed": False,
                    "unpublishable": issues}
        return {"status": "validated", "delivery_allowed": True,
                "rows": companies(leads, icp, document["request"], limit)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "invalid_state", "delivery_allowed": False,
                "error_kind": type(exc).__name__}


CONFIRMED_HOLD_REFUSAL = (
    "{target} is confirmed and already published to Leadpoet. Hold or reject it only by saving, in the same review, a "
    "required check that fails with captured evidence, or one that is unknown for a requirement other than its stage "
    "or company size. Leadpoet keeps the newest captured round, listing, sale or ownership event and reads the current "
    "LinkedIn band, so an unknown stage or size does not remove a confirmed company: save fail with the captured source "
    "that shows a later round, sale, listing or exit, or a different band. To correct a confirmed company's fields, "
    "accept it with the corrected fields instead. No change was saved.")
SIZE_CRITERION = "icp:company_size"
DECISION_TEXT_LIMIT = 500
DECISION_RECORDS = 60
DECISION_FILE = "decision-summaries.log"
DECISION_FRAME_BYTES = 3800
DECISIONS = {"accepted": "accept", "rejected": "reject", "unresolved": "defer"}
NEXT_ACTIONS = {"accept": "Confirm the accepted company's evidence packet, then research the next company.",
                "reject": "Research the next company.",
                "defer": "Research the next company; return to this one only with a source for the named gap."}


def decision_logger():
    try:
        return getattr(importlib.import_module("lab_arena_checkpoint"), "log_decision", None)
    except ImportError:
        return None


def evidence_urls(row):
    entries = [row.get("account_fit")] + [e for c in row.get("qualification_checks") or [] if isinstance(c, dict)
                                          for e in c.get("evidence") or []]
    urls = [e.get("evidence_url") or e.get("url") for e in entries if isinstance(e, dict)]
    return list(dict.fromkeys(u for u in urls if isinstance(u, str) and u.startswith(("https://", "http://"))
                              and len(u) <= DECISION_TEXT_LIMIT))[:5]


COMPANY_LOOKUP_LIMIT = 16
# Bonus pass (host.py ArenaHost.bonus_pass): TYCHE_BONUS_PASS is "enabled" for every session of a run that has the pass
# and "active" inside the pass session; unset when host.BONUS_PASS is False.
BONUS_PASS_TOOLS = {"tyche_inspect", "tyche_lookup", "tyche_review"}
BONUS_LOOKUP_CUTOFF_SECONDS = 330  # above one Deepline call's 305 s bound
# Sources a later signal may cite in the pass: the company's own site, the host that proves signal 0, or a press wire.
# judgedb Oct 28 - Nov 9, qualified two-signal companies: bonus verified on press wires 4/0, own site 22/7, other news
# 5/4; same host as signal 0 86% vs 62%.
BONUS_WIRE_HOSTS = ("prnewswire.com", "globenewswire.com", "accessnewswire.com")
BONUS_REVIEW_NOTE = ("A later intent signal is optional. If its cited passage does not state the event and its day for "
                     "this company, change only that check to unknown; the company stays confirmed on intent signal 0.")
BONUS_PASS_REFUSAL = ("Bonus pass: {reason} Nothing was saved. If no captured passage proves the later signal for this "
                      "company, save nothing for it and go on to the next company.")



def _bonus_page_key(url):
    """A URL compared as one page: lower-case host without www., path without a trailing slash, no query or fragment."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    return host + parts.path.rstrip("/") if host else ""


def _host(url):
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _on_host(host, domain):
    return bool(host) and bool(domain) and (host == domain or host.endswith("." + domain))


def company_digest(row):
    entities = row.get("entities") if isinstance(row, dict) else None
    entity = next((item for item in entities or [] if isinstance(item, dict) and item.get("type") == "company"), None)
    if entity is None or not isinstance(entity.get("properties"), dict):
        return None
    facts = entity["properties"]
    section = lambda value, key: value.get(key) if isinstance(value.get(key), dict) else {}
    head, funding = section(facts, "headquarters"), section(facts, "financials")
    latest = section(funding, "fundingLatestRound")
    return {"name": facts.get("name") or row.get("title"), "website": row.get("url"),
            "employees": section(facts, "workforce").get("total"),
            "headquarters": head.get("address") or ", ".join(str(v) for v in (head.get("city"), head.get("country")) if v),
            "founded": facts.get("foundedYear"),
            "latest_round": " ".join(str(v) for v in (latest.get("name"), latest.get("date")) if v) or None,
            "about": " ".join(str(facts.get("description") or "").split())[:240]}


_REPEATED_ROUTE = re.compile(r"request already attempted or pending; saved route: ([A-Za-z0-9._-]+)\.")
_QUOTE_REFUSAL = re.compile(r"\(([A-Za-z0-9][A-Za-z0-9._-]{0,95})\): required web evidence must quote captured source text")
NEAREST_PAGE_CHARACTERS = 60_000
NEAREST_SECONDS = 1.5
_ROUTE_ONLY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")


def _repaired_form(name, arguments):
    """Mechanical repairs of argument forms the tools refuse (local runs: about 50 refusals per 11 runs):
    tyche_inspect(field='review_ref') is a run-level derived field, so a target beside it is dropped; a literal term
    search needs one result row, so a bare ref to a one-row route gains ':0' (ArenaResearchTools._one_row_ref); a lookup check without a phase
    gets the only phase its target allows (account_discovery for target 'discovery', else account_verification)."""
    if name == "tyche_lookup" and isinstance(arguments, dict) and isinstance(arguments.get("checks"), list):
        checks = []
        for check in arguments["checks"]:
            if isinstance(check, dict) and not check.get("phase") and isinstance(check.get("target"), str) \
                    and check["target"].strip():
                check = dict(check, phase="account_discovery" if check["target"].strip().casefold() == "discovery"
                             else "account_verification")
            checks.append(check)
        return dict(arguments, checks=checks)
    if name != "tyche_inspect" or not isinstance(arguments, dict):
        return arguments
    arguments = dict(arguments)
    if arguments.get("field") == "review_ref" and "target" in arguments:
        arguments.pop("target")
    return arguments


def _evidence_items(companies):
    for item in companies if isinstance(companies, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        fits = [item.get("account_fit"), (item.get("company") or {}).get("account_fit")
                if isinstance(item.get("company"), dict) else None]
        for value in fits:
            if isinstance(value, dict):
                yield value
        for field in ("qualification_checks", "supporting_findings"):
            for check in item.get(field) or []:
                for evidence in (check.get("evidence") or []) if isinstance(check, dict) else []:
                    if isinstance(evidence, dict):
                        yield evidence


def _nearest_window(quote, page, width=1040):
    """The page window most similar to the attempted quote (difflib ratio), at most `width` characters, searched in
    the first NEAREST_PAGE_CHARACTERS of the page within NEAREST_SECONDS."""
    quote, page = " ".join(quote.split()), " ".join(page.split())[:NEAREST_PAGE_CHARACTERS]
    if len(quote) < 12 or len(page) < 12:
        return ""
    size = min(max(len(quote) + 40, 80), width)
    best, best_ratio = "", 0.0
    deadline = time.monotonic() + NEAREST_SECONDS
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(quote.lower())
    for start in range(0, max(1, len(page) - size + 1), max(20, size // 4)):
        if time.monotonic() > deadline:
            break
        window = page[start:start + size]
        matcher.set_seq1(window.lower())
        if matcher.real_quick_ratio() <= best_ratio or matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio = window, ratio
    return best if best_ratio >= 0.5 else ""


UMBRELLA_SUB_INDUSTRIES = {"enterprise software"}
_RESTATED_DATE_KEYS = ("date", "date_basis", "evidence_date", "evidence_date_basis")


def _without_restated_source_dates(companies):
    """Drop source-date copies from evidence that cites a saved result (ref).

    For such evidence research_tools._evidence_date always takes the date and its basis from the captured receipt
    and refuses a supplied copy that differs, even in format ('2026-04-13' vs 'Apr 13, 2026'): local runs show 2-7
    such refusals per run, each one a wasted review. Without the copy the saved receipt date is used, as it is when
    the copy matches. event_date (the activity date stated in the passage) is kept.
    """
    if not isinstance(companies, (list, tuple)):
        return companies

    def clean(evidence):
        if isinstance(evidence, dict) and evidence.get("ref"):
            return {key: value for key, value in evidence.items() if key not in _RESTATED_DATE_KEYS}
        return evidence

    out = []
    for item in companies:
        if isinstance(item, dict):
            item = dict(item)
            # An item-level account_fit goes through the same research_tools._evidence path (receipt date wins).
            if isinstance(item.get("account_fit"), dict):
                item["account_fit"] = clean(item["account_fit"])
            for field in ("qualification_checks", "supporting_findings"):
                if isinstance(item.get(field), list):
                    item[field] = [dict(check, evidence=[clean(e) for e in check["evidence"]])
                                   if isinstance(check, dict) and isinstance(check.get("evidence"), list) else check
                                   for check in item[field]]
        out.append(item)
    return type(companies)(out) if isinstance(companies, tuple) else out


class ArenaResearchTools(ResearchTools):

    def claim(self, target, company_url=None, name=None):
        result = super().claim(target, company_url, name)
        note = self._offering_note() if isinstance(result, dict) and result.get("claimed") else ""
        if note:
            result["next"] = (str(result.get("next") or "") + " " + note).strip()
        return result

    def _offering_note(self):
        """The ICP's offering line at the moment a candidate is chosen, before any signal lookup is spent on it.

        Leadpoet's industry judge requires every listed function and channel of the requested product or service on
        the company's own pages (qualification/scoring/industry_classification.py CRITERION_RULES: "Each listed
        function or capability of a required product or service remains required"; short of proof the verdict is
        unavailable). judgedb Nov 3-10: industry 'unavailable' was the largest single loss (30% of the points between
        our best entry and the best entry per ICP), mostly near neighbours that fail for every entry.
        """
        try:
            icp = json.loads((self.path.parent / ICP_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        offering = " ".join(str(icp.get("product_service") or "").split())
        if not offering or offering.casefold() in {"any", "all", "n/a", "not specified", "none"}:
            return ""
        return ("Offering check before any signal lookup: from this company's own homepage, product, pricing or "
                f"platform page, confirm that it supplies everything in \"{offering}\", each listed function and "
                "channel, in those words or a plain equivalent. Leadpoet's judge reads only the company's own pages for "
                "this and treats every listed function as required. If one is missing, hold the company with that gap "
                "and claim the next candidate.")

    def lookup(self, checks):
        self._company_lookup_limit(checks)
        return super().lookup(checks)

    def _company_lookup_limit(self, checks):
        try:
            routes = self._document().get("routes") or []
        except (OSError, ValueError):
            return
        for check in checks:
            if not isinstance(check, dict) or check.get("phase") != "account_verification":
                continue
            if self._bonus_pass == "active" and str(check.get("approach") or "").startswith(BONUS_PASS_APPROACH):
                continue  # validate_run.bonus_pass_refusal caps these at BONUS_PASS_LOOKUPS per company
            target = str(check.get("target", "")).strip().lower()
            if sum(str(route.get("scope", "")).lower() == target and route.get("phase") == "account_verification"
                   for route in routes) >= COMPANY_LOOKUP_LIMIT:
                raise ValueError(f"{target} has used its {COMPANY_LOOKUP_LIMIT} lookups. No paid call was made. Review it "
                                 "now from what is saved: accept it if its checks pass, otherwise hold it with the named "
                                 "gap, then move to the next candidate.")

    def _description_view(self, contract):
        view = super()._description_view(contract)
        schema = view.get("inputSchema")
        if isinstance(schema, dict) and isinstance(schema.get("fields"), list) and isinstance(schema.get("jsonSchema"), dict):
            required = schema["jsonSchema"].get("required")
            view["inputSchema"] = {**{key: value for key, value in schema.items() if key != "jsonSchema"},
                                   **({"required": required} if required else {}),
                                   "jsonSchema": "omitted: the fields above list the same inputs; "
                                                 "inspect(tool=..., field='inputSchema.jsonSchema') reads it"}
        return view

    def review(self, companies=(), web=(), sources=(), review_ref=None, review_findings=None):
        companies = _without_restated_source_dates(companies)
        self._refuse_unexplained_confirmed_holds(companies)
        self._refuse_imprecise_claims(companies)
        self._refuse_umbrella_sub_industry(companies)
        result = super().review(companies, web, sources, review_ref, review_findings)
        note = self._industry_source_note(companies) if self._bonus_pass != "active" else ""
        if note and isinstance(result, dict):
            result = {**result, "industry_source_note": note}
        return result

    def _industry_source_note(self, companies):
        """Name accepted companies whose saved passing industry check cites none of the company's own pages.

        Leadpoet's industry judge reads only first-party pages (company_evidence_investigator: a page counts only
        when its domain matches the company's), so an industry shown only by news or a directory is one it often
        cannot repeat. judgedb Nov 4-10: industry passed 85% (n=2,326) when one of the company's own pages was among
        the cited URLs, 72% (n=369) otherwise. This is a note only; nothing saved is changed.
        """
        targets = {str(item.get("target") or "") for item in companies
                   if isinstance(item, dict) and item.get("decision") == "accept"}
        if not targets:
            return ""
        try:
            accepted = self._document().get("accepted") or []
        except (OSError, ValueError, AttributeError):
            return ""
        missing = []
        for row in accepted:
            company = row.get("company") if isinstance(row, dict) else None
            if not isinstance(company, dict):
                continue
            site = _registrable_host(company.get("website") or company.get("domain") or "")
            key = str(company.get("domain") or "")
            if not site or not ({key, site} & targets or any(_registrable_host(t) == site for t in targets)):
                continue
            for check in row.get("qualification_checks") or []:
                if not (isinstance(check, dict) and check.get("criterion_id") == "icp:industries"
                        and check.get("status") == "pass"):
                    continue
                hosts = [_registrable_host(item.get("url") or "") for item in check.get("evidence") or []
                         if isinstance(item, dict)]
                if not any(host == site or host.endswith("." + site) for host in hosts):
                    missing.append(site)
        if not missing:
            return ""
        names = ", ".join(sorted(set(missing))) + ": the passing industry check cites none of the company's own pages. "
        try:
            target_met = len(accepted) >= int(self._document()["request"]["target_count"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            target_met = False
        if target_met:
            # The company target is met, so no further lookup is admitted: never advise holding a company here.
            return (names + "If a saved page of the company's own describes the requested offering, cite that passage "
                    "in the industry check and accept again; otherwise keep the company as it is.")
        return (names + "Leadpoet's industry judge reads only the company's own pages: before confirming, capture its "
                "homepage, About or product page and cite the passage there that describes the requested offering "
                "and each function it lists, then accept again. If its own pages do not describe it, hold the company "
                "with that gap and take the next candidate.")

    def _refuse_umbrella_sub_industry(self, companies):
        """Ask once for the company's specific taxonomy label instead of the umbrella 'Enterprise Software' label.

        The published paragraph closes with the company's sub_industry. judgedb Nov 2-10: closers naming
        'Enterprise Software' passed the paragraph 0 of 4 (icp_connection_missing); the same company, round and URL
        (UpGuard, Nov 10 ICP 9) failed 3 of 3 with it and passed with its specific label. The model chooses the
        replacement from the company's own pages; code never writes the label.
        """
        refused = self.__dict__.setdefault("_umbrella_refusals", set())
        for item in companies:
            if not isinstance(item, dict) or item.get("decision") != "accept":
                continue
            target = str(item.get("target") or "")
            supplied = item.get("company") if isinstance(item.get("company"), dict) else {}
            label = " ".join(str(supplied.get("sub_industry") or "").split())
            if label.casefold() in UMBRELLA_SUB_INDUSTRIES and target not in refused:
                refused.add(target)
                raise ValueError(
                    f"Accept not saved: company.sub_industry '{label}' for {target} is an umbrella label. Save the most "
                    "specific canonical industry and sub_industry pair for the product this company's own pages "
                    "describe (for example Software / Developer Tools or Software / CRM rather than Software / "
                    "Enterprise Software), then resubmit this accept once. If no narrower pair fits its own pages, "
                    "resubmit it unchanged.")

    def _refuse_imprecise_claims(self, companies):
        """Ask once for a plainer signal claim before accepting (the model rewrites it; code never edits text).

        Leadpoet's paragraph judge binds every figure and name in the published claim to the cited page. judgedb,
        Nov 2-10, intent passed, one-signal companies: claims with no figure besides the date passed the paragraph
        87%, one figure 75%, two or more 67% (+11.7 points within the same entry, every signal type); claims whose
        subject is a subsidiary or brand in the "X's Y" form passed 48% against 81% for the company itself.
        """
        refused = self.__dict__.setdefault("_claim_refusals", set())
        for item in companies:
            if not isinstance(item, dict) or item.get("decision") != "accept":
                continue
            target = str(item.get("target") or "")
            for check in item.get("qualification_checks") or []:
                if not (isinstance(check, dict) and str(check.get("criterion_id", "")).startswith("signal:")
                        and check.get("status") == "pass"):
                    continue
                claim = str(check.get("claim") or "")
                # Judge the claim as it will be published (labels and window clauses are stripped there).
                problems = claim_problems(claim if check.get("source_passage") else public_claim(claim))
                key = (target, check.get("criterion_id"))
                if problems and key not in refused:
                    refused.add(key)
                    raise ValueError(
                        f"Accept not saved: rewrite the {check.get('criterion_id')} claim for {target} before "
                        f"accepting ({'; '.join(problems)}). Write one plain sentence that opens with the company's own "
                        "name and states the event and its day exactly as the cited page prints them, keeping at most "
                        "one figure and only when the signal needs it (a funding amount, or the scale a 'major' "
                        "facility requires). Leadpoet's paragraph judge must find every figure and name on the page, "
                        "and each extra one makes the company fail more often. Then resubmit this accept once.")

    def _refuse_unexplained_confirmed_holds(self, companies):
        removing = [item for item in companies
                    if isinstance(item, dict) and item.get("decision") in ("hold_account", "reject")]
        if not removing:
            return
        try:
            leads = json.loads((self.path.parent / "leads.json").read_text(encoding="utf-8"))["leads"]
            confirmed = {_company_key(row) for row in leads}
            if self._bonus_pass == "active":
                # A bonus-pass re-accept leaves leads.json until it is confirmed again; it is still a confirmed company.
                confirmed |= {_company_key(row) for row in self._document().get("accepted") or []}
        except (OSError, ValueError, KeyError, TypeError):
            return
        for item in removing:
            try:
                key = company_domain(item["target"])
            except (KeyError, TypeError, ValueError):
                continue
            if key not in confirmed:
                continue
            currency = self._stage_and_size_criteria()
            named = [check for check in item.get("qualification_checks") or []
                     if isinstance(check, dict) and (check.get("status") == "fail" or (
                         check.get("status") == "unknown" and check.get("criterion_id") not in currency))
                     and not (self._bonus_pass and LATER_SIGNAL.match(str(check.get("criterion_id") or "")))]
            if not named:
                raise ValueError(CONFIRMED_HOLD_REFUSAL.format(target=item["target"]))

    def _stage_and_size_criteria(self):
        ids = {SIZE_CRITERION}
        try:
            icp = json.loads((self.path.parent / ICP_FILE).read_text(encoding="utf-8"))
            stage = company_stage(icp)
            criteria = self._document()["criteria"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return ids
        if stage:
            wanted = {stage.casefold(), stage_line(stage).removeprefix(STAGE_PREFIX).casefold()}
            for criterion in criteria if isinstance(criteria, list) else []:
                label = " ".join(str(criterion.get("label", "")).casefold().rstrip(".").split())
                if label.removeprefix(STAGE_PREFIX.strip().casefold()).strip() in wanted:
                    ids.add(criterion.get("id"))
        return ids

    def _overview_unlocked(self):
        overview = super()._overview_unlocked()
        overview["budget"]["host_cost_observation"] = pacing.cost_observation(self.path)
        return overview

    def _cost_summary(self):
        return {**super()._cost_summary(), "host_cost_observation": pacing.cost_observation(self.path)}

    def _stage_alias_request(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("icp"), dict):
            return request
        attributes = request["icp"].get("required_attributes")
        if not isinstance(attributes, list) or any(not isinstance(value, str) for value in attributes):
            return request
        try:
            icp = json.loads((self.path.parent / ICP_FILE).read_text(encoding="utf-8"))
            bound = json.loads((self.path.parent / "request-options.json").read_text(encoding="utf-8"))["required_attributes"]
            saved = json.loads(self.path.read_text(encoding="utf-8"))["request"] if self.path.exists() else None
            stage = company_stage(icp)
            saved_attributes = saved["icp"]["required_attributes"] if saved is not None else []
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return request
        if not stage:
            return request
        full, legacy = stage_line(stage), STAGE_PREFIX + stage
        if (not isinstance(bound, list) or any(not isinstance(value, str) for value in bound)
                or not isinstance(saved_attributes, list)):
            return request
        if full in bound:
            aliases = {stage, legacy} - {full}
        elif legacy in bound:
            aliases = {stage}
        else:
            return request
        removable = aliases - set(bound)
        if any(alias in saved_attributes for alias in removable) or not any(alias in attributes for alias in removable):
            return request
        normalized = copy.deepcopy(request)
        normalized["icp"]["required_attributes"] = [value for value in attributes if value not in removable]
        return normalized

    def _start(self, request, provider_credit_limits=None, **options):
        # The host's scrapingdog-allowance.json names the ScrapingDog allowance (tyche_arena.input.prepare); without it a
        # new run's ScrapingDog cap is zero and every ScrapingDog lookup is refused as "disabled by its zero credit cap".
        allowance_file = self.path.parent / "scrapingdog-allowance.json"
        bound = json.loads(allowance_file.read_text(encoding="utf-8")) if allowance_file.exists() else {}
        if isinstance(bound, dict) and isinstance(bound.get("scrapingdog_credits"), (int, float)) and not self.path.exists():
            limits = dict(provider_credit_limits or {})
            limits.setdefault("scrapingdog", bound["scrapingdog_credits"])
            provider_credit_limits = limits
            if bound.get("scrapingdog_usd_per_credit"):
                options.setdefault("scrapingdog_usd_per_credit", bound["scrapingdog_usd_per_credit"])
        return super()._start(self._stage_alias_request(self._arena_signal_request(request)),
                              provider_credit_limits=provider_credit_limits, **options)

    def _arena_signal_request(self, request):
        icp = json.loads((self.path.parent / ICP_FILE).read_text(encoding="utf-8"))
        expected = [" ".join(query.split()).casefold() for query in intent_signals(icp)]
        ages = intent_signal_ages(icp)
        saved = self._document()["request"] if self.path.exists() else None
        reference = self.environment.get("LAB_ARENA_EVALUATION_DATE")
        if reference:
            datetime.strptime(reference, "%Y-%m-%d")
        for value in ([saved] if saved is not None else []) + [request]:
            if errors := signal_request_errors(value):
                raise ValueError("; ".join(errors))
            signals = value.get("buying_signals")
            if not isinstance(signals, list) or not signals:
                nested = isinstance(value.get("icp"), dict) and "buying_signals" in value["icp"]
                raise ValueError("Arena requires Intent signal 0 in buying_signals, with importance required."
                                 + (" buying_signals goes beside icp, at the top level of the request, not inside icp."
                                    if nested else ""))
            window = value.get("time_window", {})
            as_of = value.get("as_of_date", (saved or {}).get("as_of_date", reference
                             or datetime.now(timezone.utc).date().isoformat()))
            if reference and (as_of != reference or window.get("as_of_date", reference) != reference):
                raise ValueError("Arena as_of_date and time_window.as_of_date must use the host evaluation date "
                                 + reference + ". Preserve the saved request; do not restart its budget.")
            as_of = datetime.strptime(window.get("as_of_date", as_of), "%Y-%m-%d")
            positions = []
            for index, signal in enumerate(signals):
                query = signal.get("query")
                folded = " ".join(query.split()).casefold() if isinstance(query, str) else ""
                if folded not in expected:
                    raise ValueError(f"Arena buying_signals[{index}].query must copy a complete Intent signal "
                                     "line, without its label; the informal Buyer request cannot replace it.")
                position = expected.index(folded)
                positions.append(position)
                importance = "required" if position == 0 else "preferred"
                if signal.get("importance", "required") != importance:
                    raise ValueError(f"Arena Intent signal {position} must have importance {importance}; "
                                     "only Intent signal 0 is required, so any/all cannot substitute a later signal.")
                age = ages[position]
                if age is not None:
                    cutoff = signal_window_start(as_of, signal, window)
                    if cutoff != as_of - timedelta(days=age) or signal.get("min_age_days", 0) != 0:
                        raise ValueError(f"Arena Intent signal {position} must use max_age_days {age} "
                                         "(per signal or shared time_window), or an equivalent calendar-month "
                                         "cutoff, and min_age_days 0. Do not omit or change the bound age limit.")
            if positions.count(0) != 1:
                raise ValueError("Arena requires exactly one complete Intent signal 0 query with importance required.")
        if saved is None and reference and "as_of_date" not in request:
            request = copy.deepcopy(request)
            request["as_of_date"] = reference
        return request

    def _company_review(self, row, sources, receipts=None):
        review = super()._company_review(row, sources, receipts)
        rest = {key: value for key, value in review.items() if key != "company"}
        try:
            icp = json.loads((self.path.parent / ICP_FILE).read_text(encoding="utf-8"))
            request = self._document()["request"]
            submitted = company_row(row, icp, request.get("buying_signals") or [])
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return {"company": review["company"], "arena_submission_preview": {
                "status": "unavailable", "reason": "arena_projection_input_unavailable",
                "error_kind": type(exc).__name__}, **rest}
        if submitted is None:
            return review
        preview = {
            "required_attribute": submitted.get("required_attribute"),
            "company_stage": submitted["company_stage"],
            "company_stage_evidence": submitted.get("company_stage_evidence", []),
            "intent_signals": submitted["intent_signals"],
            **({"later_signal_note": BONUS_REVIEW_NOTE} if self._bonus_pass and any(
                item.get("matched_icp_signal") for item in submitted["intent_signals"]) else {}),
            "review_note": "Arena submits these selected passages and URLs. Every required attribute part needs captured proof for the same company and, where the criterion is product-specific, the relevant product; several captured passages may jointly prove it. Follow a named published-one-passage check's distinctive-primary guidance and any stricter source constraint in the original request; every required part still needs captured support. The attribute publishes its first valid URL/quote, which must be a stable span without changing relative dates. Choose that primary source against the complete condition: when product fit and a recent event are both required, prefer an announcement supporting the company, business capability and qualifying event date. A homepage product description alone does not prove the event, even when intent fields contain it. Review those actual URLs below: other saved support and citations in explanation are not automatically submitted or fetched. The judge independently verifies loaded sources. Never join or fabricate quotes, repurpose stage evidence, or pass an unsupported part; hold required unknowns.",
        }
        if company_stage(icp):
            preview["stage_review_note"] = (
                "Review the requested-stage check against these emitted stage URLs and quotes before confirming. "
                "Identify the completed funding, listing or ownership event that supports the stage. Leadpoet's "
                "judge takes the newest completed round, listing, acquisition or sale it finds as the current stage, so "
                "the newest such event in captured sources stands unless a captured source shows a later one; do not "
                "hold a stage only because no newer page restates it, and do not spend lookups proving nothing later "
                "exists. For Private Equity, establish controlling sponsor ownership (an acquisition or majority "
                "stake); portfolio membership or a minority investment alone is insufficient. Prefer strongest "
                "captured company/investor passage first; credible secondary and multiple-source support remain "
                "allowed. Use contiguous captured text. Correct an evidenced mismatch to fail; hold the stage unknown "
                "only when no captured source names a qualifying event. Record reasoning in existing claim/review findings.")
            if submitted["company_stage"].casefold() == "public" and not any(
                    listing_passage({"url": item["url"], "text": item["quote"]})
                    for item in submitted.get("company_stage_evidence") or []):
                preview["stage_listing_note"] = (
                    "No emitted stage quote names the listing in a form Leadpoet's judge reliably confirms. Before "
                    "confirming, capture a current company or newswire passage that names the company with its "
                    "exchange and ticker, such as 'Acme Inc. (NASDAQ: ACME) today announced', and add it to the stage "
                    "check (a recent press release usually has one). Filing-table rows without the company name, "
                    "stock quote pages and sec.gov pages often stay unconfirmed. One fetch is enough; if none is "
                    "found, keep the stage as it is.")
        return {"company": review["company"], "arena_submission_preview": preview, **rest}

    def _lookup_view(self, attempt, offset=0, limit=10):
        body = attempt.get("result") if isinstance(attempt, dict) else None
        if isinstance(body, dict) and body.get("tool") == OPENING_TOOL:
            limit = max(limit, COMPANY_LIST_RESULTS)
        view = super()._lookup_view(attempt, offset=offset, limit=limit)
        route = view.get("route")
        if (not isinstance(body, dict) or not isinstance(route, str) or not view.get("recorded")
                or view.get("status") != "ok" or attempt.get("exit_code") != 0
                or attempt.get("provider_status") != "ok" or body.get("status") != "ok"
                or body.get("provider") != "deepline" or body.get("operation") != "execute"):
            return view
        remaining = SOURCE_TEXT_PAGE_SIZE
        for item in view.get("results", []):
            reference = item.get("ref") if isinstance(item, dict) else None
            if not isinstance(reference, str) or not reference.startswith(route + ":"):
                continue
            facts = item.get("facts")
            if isinstance(facts, dict) and facts.get("entities"):
                try:
                    digest = company_digest(self._resolve(reference)[0])
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    digest = None
                if digest:
                    item["facts"] = digest
                    continue
            if not isinstance(facts, dict) or not isinstance(facts.get("evidence_text"), str):
                continue
            try:
                row, source, saved = self._resolve(reference)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            if (saved.get("status") != "ok" or source.get("provider") != "deepline"
                    or source.get("operation") != "execute"
                    or any(body.get(key) != saved.get(key) for key in
                           ("provider", "operation", "tool", "run_fingerprint", "request_fingerprint"))
                    or row.get("content_kind") != "captured_page"
                    or not isinstance(row.get("evidence_text"), str)):
                continue
            text = row["evidence_text"]
            visible_chars = min(len(text), 1800 + remaining, SOURCE_TEXT_PAGE_SIZE)
            remaining -= max(0, visible_chars - 1800)
            facts["content_kind"] = "captured_page"
            for alias in ("markdown", "html", "text", "content"):
                if isinstance(facts.get(alias), str):
                    facts.pop(alias, None)
            facts["evidence_text"] = text[:visible_chars]
            for key in ("evidence_text_truncated", "evidence_text_total_characters",
                        "evidence_text_next_offset"):
                facts.pop(key, None)
            if len(text) > visible_chars:
                facts["evidence_text_truncated"] = True
                facts["evidence_text_total_characters"] = len(text)
                facts["evidence_text_next_offset"] = visible_chars
        return view

    def export_partial(self):
        try:
            document = self._document()
            saved = confirmed_leads.read(self.path, document)
            current = {_company_key(row): row for row in document["accepted"]}
            retained = [row for row in saved["leads"] if current.get(_company_key(row)) == row]
            icp = json.loads((self.path.parent / ICP_FILE).read_text(encoding="utf-8"))
            rows = companies({"leads": retained}, icp, document["request"])
            return {"exported": False, "partial": True, "delivery_allowed": False,
                    "confirmed_count": len(retained), "publishable_count": len(rows)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return {"exported": False, "partial": True, "delivery_allowed": False,
                    "status": "partial_unavailable", "error_kind": type(exc).__name__}

    def _export(self):
        result = final_review_status(self.path)
        if not result["delivery_allowed"]:
            return {**result, "next": "Correct saved findings through tyche_review, then request the current final review packet."}
        try:
            run_attempt.finalize_run(self.path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"status": "preflight_failed", "delivery_allowed": False,
                    "error_kind": type(exc).__name__,
                    "next": "Preserve the reviewed evidence and correct the saved validation error."}
        return {"status": "arena_output_validated", "delivery_allowed": True,
                "company_count": len(result["rows"])}

    def call(self, name, arguments):
        arguments = self._one_row_ref(name, _repaired_form(name, repaired_keys(arguments)))
        phase = "final_review" if self.environment.get("TYCHE_FINALIZATION_ONLY") == "1" else "research"
        label = name if isinstance(name, str) and name in native_counts.TOOLS else "unknown_tool"
        mode = native_counts.inspect_mode(label, arguments)
        native_counts.record(self.path, phase, label, mode, "started")
        try:
            try:
                result = self._call_with_publication_feedback(name, arguments)
            except ValueError as exc:
                result = self._saved_route_view(name, exc)
                if result is None:
                    enriched = self._with_nearest_passage(name, arguments, exc)
                    if enriched is exc:
                        enriched = self._with_identity_hint(name, exc)
                    if enriched is exc:
                        raise
                    raise enriched from exc
        except Exception as exc:
            try:
                native_counts.record(self.path, phase, label, mode, "raised", exception=exc)
            except Exception:
                pass
            raise
        native_counts.record(self.path, phase, label, mode, native_counts.outcome(result))
        try:
            self._report_decisions(name, arguments, result)
        except Exception:
            pass
        return result

    def _saved_route_view(self, name, exc):
        """For a lookup refused as already attempted, return that saved route's view instead of a bare refusal.

        No paid call is made either way (run_attempt refuses the repeat before dispatch); local runs show about
        2.5 such refusals per run, each followed by about 3.5 calls to find the saved route again.
        """
        match = _REPEATED_ROUTE.search(str(exc))
        if name != "tyche_lookup" or not match:
            return None
        try:
            view = self._call_with_publication_feedback("tyche_inspect", {"ref": match.group(1)})
        except Exception:
            return None
        return {"status": "already_attempted", "dispatched": False, "saved_route": match.group(1),
                "refusal": str(exc),
                "note": "No lookup was dispatched: this exact request already has the saved route below. Reuse its "
                        "result refs; send any other checks from that call again on their own.",
                "saved_route_view": view}

    def _with_identity_hint(self, name, exc):
        """Say how to supply the name and website when no LinkedIn record was selected (company.ref).

        Local runs: 2 fully checked companies were auto-held after 6 identical 'canonical_name is required' refusals,
        each time without the field. The values still come from the model's own captured sources.
        """
        message = str(exc)
        if name != "tyche_review" or "company.canonical_name is required" not in message:
            return exc
        return ValueError(message + " The saved company has no canonical_name (no selected LinkedIn record "
                          "supplied one), so add the name and website inside the company object, for example "
                          "\"company\": {\"canonical_name\": \"<the name its own homepage uses>\", \"website\": "
                          "\"https://<its domain>/\"}, from the captured homepage, then send the accept again.")

    def _with_nearest_passage(self, name, arguments, exc):
        """Add the closest captured window to a refused quote, so it can be copied exactly in one try.

        The validator names the cited route, '(route-id): required web evidence must quote captured source text';
        each evidence item citing that route whose quote is not found in its captured text gets the closest window.
        The exact-quote rule is unchanged (source_receipts still requires a contiguous captured passage).
        """
        message = str(exc)
        routes = list(dict.fromkeys(_QUOTE_REFUSAL.findall(message)))
        if name != "tyche_review" or not routes or not isinstance(arguments, dict):
            return exc
        hints = []
        for evidence in _evidence_items(arguments.get("companies")):
            ref = str(evidence.get("ref") or "")
            quote = str(evidence.get("text") or evidence.get("evidence_text") or "")
            if ref.split(":")[0] not in routes or not quote or len(hints) >= 3:
                continue
            try:
                row = self._resolve(ref)[0]
            except Exception:
                continue
            page = str(row.get("evidence_text") or row.get("text") or "")
            if quote_form(quote) in quote_form(page):
                continue  # this quote is found; another one was refused
            window = _nearest_window(quote, page)
            if window:
                hints.append(ref + ": " + json.dumps(window, ensure_ascii=False))
        if not hints:
            return exc
        return ValueError(message + " Closest captured text (copy only a part that states the claim, exactly as "
                          "shown; if no part does, keep that check unknown): " + "; ".join(hints))

    def _one_row_ref(self, name, arguments):
        """A literal term search needs one result row: a bare ref to a route that saved exactly one row gains ':0'."""
        if name != "tyche_inspect" or not isinstance(arguments, dict) or arguments.get("term") is None:
            return arguments
        ref = arguments.get("ref")
        if not isinstance(ref, str) or not _ROUTE_ONLY.fullmatch(ref):
            return arguments
        try:
            route = next((r for r in self._document().get("routes") or [] if r.get("route_id") == ref), None)
        except (OSError, ValueError, AttributeError):
            return arguments
        if route is None or route.get("rows_returned") != 1:
            return arguments
        return dict(arguments, ref=ref + ":0")

    def _report_decisions(self, name, arguments, result):
        log = decision_logger()
        if log is None or not isinstance(result, dict) or not isinstance(arguments, dict):
            return
        def send(key, decision, objective, rationale, evidence=(), candidate=None):
            if not isinstance(rationale, str) or not rationale.strip() or not self._reserve_decision(key, decision):
                return
            fields = {"objective": objective[:DECISION_TEXT_LIMIT], "evidence": list(evidence), "decision": decision,
                      "rationale": rationale.strip()[:DECISION_TEXT_LIMIT],
                      "next_action": NEXT_ACTIONS.get(decision, "Publish the reviewed companies."),
                      **({"candidate": candidate[:DECISION_TEXT_LIMIT]} if candidate else {})}
            while len(json.dumps(fields, ensure_ascii=True)) > DECISION_FRAME_BYTES and (
                    fields["evidence"] or len(fields["rationale"]) > 100):
                if fields["evidence"]:
                    fields["evidence"].pop()
                else:
                    fields["rationale"] = fields["rationale"][:-50]
            log(**fields)
        arguments = tolerant_arguments(name, arguments)
        if name == "tyche_lookup" and result.get("lookups"):
            discovery = [c for c in arguments.get("checks") or []
                         if isinstance(c, dict) and c.get("phase") == "account_discovery"]
            if discovery:
                send(("investigate",), "investigate", "Find companies that match the ICP.", discovery[0].get("purpose"))
        elif name == "tyche_review" and result.get("saved_companies") and "held_after_refusals" not in result:
            items = [c for c in arguments.get("companies") or [] if isinstance(c, dict)]
            if len(items) != len(result["saved_companies"]):
                return
            document = self._document()
            rows = {_company_key(row): (state, row) for state in DECISIONS for row in document.get(state) or []}
            for item, target in zip(items, result["saved_companies"]):
                state, row = rows.get(target, (None, None))
                if state is None:
                    continue
                decision = DECISIONS[state]
                send((target, decision), decision,
                     f"Decide whether {target} meets the ICP's required filters, size and buying signal.",
                     item.get("reason"), evidence_urls(row), target)
        elif name == "tyche_finish" and result.get("status") == "arena_output_validated":
            findings = [f for f in arguments.get("review_findings") or [] if isinstance(f, dict)]
            rationale = " ".join(f"{f.get('target')}: {f.get('finding')}" for f in findings
                                 if isinstance(f.get("finding"), str))
            send(("finish",), "finish", "Deliver the companies that passed the final review.",
                 rationale or f"{result.get('company_count', 0)} companies passed the final review.")

    def _reserve_decision(self, key, decision):
        key = json.dumps(list(key))
        with open(self.path.parent / DECISION_FILE, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            sent = handle.read().splitlines()
            if key in sent or decision != "finish" and sum(k != '["finish"]' for k in sent) >= DECISION_RECORDS:
                return False
            handle.write(key + "\n")
            return True

    @property
    def _bonus_pass(self):
        value = self.environment.get("TYCHE_BONUS_PASS")
        return value if value in ("enabled", "active") else None

    def _bonus_pass_guard(self, name, arguments):
        """Inside the bonus pass a call may only read, run a bonus-pass lookup for an accepted company, add one passing
        later-signal check to an accepted company, close lookups or approve a confirmation packet. Every other change
        is refused before anything is saved."""
        if name not in BONUS_PASS_TOOLS:
            raise ValueError(BONUS_PASS_REFUSAL.format(reason=f"{name} is not available in the bonus pass."))
        arguments = tolerant_arguments(name, copy.deepcopy(arguments)) if isinstance(arguments, dict) else {}
        document = self._document()
        accepted = {_company_key(row): row for row in document.get("accepted") or []}
        if name == "tyche_lookup":
            checks = arguments.get("checks") or []
            for check in checks if isinstance(checks, list) else [None]:
                if not isinstance(check, dict):
                    raise ValueError(BONUS_PASS_REFUSAL.format(reason="each lookup check must be an object."))
                try:
                    target = company_domain(str(check.get("target") or ""))
                except ValueError:
                    target = ""
                if (not str(check.get("approach") or "").startswith(BONUS_PASS_APPROACH)
                        or check.get("phase") != "account_verification" or target not in accepted):
                    raise ValueError(BONUS_PASS_REFUSAL.format(
                        reason=f"a lookup must be one of the listed checks: approach starting '{BONUS_PASS_APPROACH}', "
                               "phase account_verification, target an accepted company."))
            try:
                until = float(self.environment.get("TYCHE_BONUS_UNTIL") or 0)
            except ValueError:
                until = 0
            if checks and until - time.time() < BONUS_LOOKUP_CUTOFF_SECONDS:
                raise ValueError(BONUS_PASS_REFUSAL.format(
                    reason="no time is left for another paid lookup; finish the reviews already possible."))
        elif name == "tyche_review":
            if arguments.get("web"):
                raise ValueError(BONUS_PASS_REFUSAL.format(reason="web observations are not saved in the bonus pass."))
            for item in arguments.get("companies") or []:
                self._bonus_pass_item(item, accepted)

    def _bonus_pass_item(self, item, accepted):
        def refuse(reason):
            raise ValueError(BONUS_PASS_REFUSAL.format(reason=reason))
        if not isinstance(item, dict) or set(item) - {"target", "decision", "reason", "qualification_checks"}:
            refuse("a company item carries only target, decision 'accept', reason and the one added check.")
        try:
            target = company_domain(str(item.get("target") or ""))
        except ValueError:
            target = ""
        if item.get("decision") != "accept" or target not in accepted:
            refuse("only an accept of an already accepted company is allowed; nothing is held or rejected here.")
        checks = item.get("qualification_checks")
        check = checks[0] if isinstance(checks, list) and len(checks) == 1 and isinstance(checks[0], dict) else None
        if (check is None or not re.fullmatch(r"signal:[1-9]\d*", str(check.get("criterion_id") or ""))
                or check.get("status") not in ("pass", "unknown")
                or (check.get("status") == "pass" and not check.get("evidence"))):
            refuse("the accept carries exactly one later intent signal check (signal:N, N >= 1): status pass with "
                   "its evidence, or status unknown to withdraw a later signal its passage does not prove.")
        if check.get("status") == "unknown":
            return
        row = accepted[target]
        domain = str((row.get("company") or {}).get("domain") or target).lower()
        hosts = {_host(e.get("url") or e.get("evidence_url"))
                 for c in row.get("qualification_checks") or [] if c.get("criterion_id") == "signal:0"
                 for e in c.get("evidence") or [] if isinstance(e, dict)} - {""}
        # output.company_row drops a later signal cited from signal 0's own URL (the same event told twice), so a bonus
        # saved from that page was never published and the pass stopped looking for that company. Refuse it here.
        first_pages = {_bonus_page_key(e.get("url") or e.get("evidence_url"))
                       for c in row.get("qualification_checks") or [] if c.get("criterion_id") == "signal:0"
                       for e in c.get("evidence") or [] if isinstance(e, dict)} - {""}
        for evidence in check["evidence"] if isinstance(check["evidence"], list) else [None]:
            if not isinstance(evidence, dict):
                refuse("each evidence item is an object with ref, text and event_date.")
            url = evidence.get("url")
            if evidence.get("ref"):
                try:
                    url = self._resolve(str(evidence["ref"]))[0].get("url") or url
                except (ReferenceError, ValueError, OSError, KeyError, TypeError, AttributeError):
                    pass
            if url and _bonus_page_key(url) in first_pages:
                refuse("that page is signal 0's own page: a later signal from it is published as the same event told "
                       "twice and dropped. Cite a different page about a different event, or save nothing for this "
                       "company.")
            host = _host(url)
            if host and not (_on_host(host, domain) or host in hosts
                             or any(_on_host(host, wire) for wire in BONUS_WIRE_HOSTS)):
                refuse(f"{host} is not the company's own site, the site of its signal 0 page or a press wire.")

    def _call_with_publication_feedback(self, name, arguments):
        if self._bonus_pass == "active":
            self._bonus_pass_guard(name, arguments)
        result = super().call(name, arguments)
        if name != "tyche_review" or not isinstance(result, dict):
            return result
        status = result.get("status")
        if status not in {"review_required", "confirmed_leads_saved"}:
            return result
        try:
            icp = json.loads((self.path.parent / ICP_FILE).read_text(encoding="utf-8"))
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if status == "review_required":
                if result.get("review_scope") != "confirmed_leads":
                    return result
                targets = set(result.get("expected_targets") or [])
                leads = [row for row in document.get("accepted") or []
                         if _company_key(row) in targets]
            else:
                leads = json.loads((self.path.parent / "leads.json").read_text(encoding="utf-8"))["leads"]
            issues = publication_gaps(leads, icp, document["request"])
            unreachable = []
            for lead in leads:
                row = company_row(lead, icp, document["request"].get("buying_signals") or [])
                if row and (fields := judge_unreachable_fields(row)):
                    unreachable.append({"target": (lead.get("company") or {}).get("domain") or row["company_name"],
                                        "fields": fields})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(json.dumps({"tyche_arena_publication_feedback_error": type(exc).__name__}),
                  file=sys.stderr, flush=True)
            return result
        judge_note = {"judge_unreachable": unreachable,
                      "judge_next": "Leadpoet's judge cannot open LinkedIn pages. For each named field, capture a public "
                                    "page that states it (the company's own site, a press release or news) and cite it "
                                    "first with tyche_review; an unverifiable intent signal 0 costs a 10-point penalty."
                      } if unreachable else {}
        if not issues:
            return {**result, "arena_publication": {"status": "evidence_warning", **judge_note}} if unreachable else result
        action = ("Review this packet and repair arena_publication.unpublishable before approval. "
                  if status == "review_required" else
                  "Repair arena_publication.unpublishable before further sourcing. ")
        return {**result, "next": action + result.get("next", ""), "arena_publication": {
            "status": "repair_needed", "unpublishable": issues, **judge_note,
            "scope": "Output field checks only; Arena judges source truth and ICP fit separately.",
            "next": "Arena cannot publish these companies with the named fields missing, mismatched, duplicate or unstable. "
                    "For a relative_date gap, select a captured span without changing relative dates. "
                    "Correct them from captured source evidence with tyche_review, or hold_account when support "
                    "is unavailable. Other confirmed companies remain saved and publishable."}}


class PacedBroker(Broker):

    def __init__(self, *args, run_file, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_file = run_file

    def execute(self, request, capture, **options):
        operation = request.get("operation")
        provider = "deepline" if operation == "execute" else "scrapingdog"
        if operation not in {"search", "describe"}:
            reason = pacing.refusal(self.run_file)
            if reason:
                capture({"arena": {"dispatched": False, "error": reason}})
                return {"status": "config_error", "error_stage": "coordination", "provider": provider,
                        "operation": operation, "request_sent": False,
                        "error": {"code": reason, "message": "Leadpoet spend pacing: no remote call was made, and publishing "
                                  "another company does not reopen research. Confirm the current company from saved "
                                  "evidence or stop."}}, 2
        return super().execute(request, capture, **options)

    def _after_call(self, body):
        receipt = body.get("spend_receipt") if isinstance(body, dict) else None
        if isinstance(receipt, dict) and receipt.get("state") == "pending_billing" and receipt.get("ledger"):
            with budget_guard.transaction(Path(receipt["ledger"])) as state:
                call = state["calls"].get(receipt.get("route_id"))
                if (call and call.get("state") == "pending_billing" and call.get("provider") == "deepline"
                        and call.get("admission_bound_credits") is not None):
                    budget_guard.hold_unknown_charge(call)
                    receipt.update(state=call["state"], unknown_charge=call["unknown_charge"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-file", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True, help="Research deadline (time.monotonic)")
    parser.add_argument("--response-deadline", type=float, required=True, help="Hard deadline (time.monotonic)")
    args = parser.parse_args()
    broker = PacedBroker(os.environ["LAB_ARENA_WORKER_SOCKET"], args.deadline,
                         response_deadline=args.response_deadline, run_file=args.run_file)
    try:
        tyche_tools.serve(ArenaResearchTools(args.run_file, execute=broker.execute))
    finally:
        broker.stopped.set()


if __name__ == "__main__":
    main()
