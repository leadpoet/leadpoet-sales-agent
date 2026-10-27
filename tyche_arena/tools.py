"""The local TYCHE native tools, with provider calls sent through the Leadpoet broker.

This is tyche_tools.py's own server, with publication-field feedback on shared research reviews.
Codex already runs inside gVisor in Leadpoet, so there is no inner sandbox relay.
"""

import argparse
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import importlib
import json
import os
from pathlib import Path
import sys

from . import pacing  # noqa: F401  (sets sys.path for the skill scripts)
from .broker import Broker
from . import native_counts
from .input import STAGE_PREFIX, company_stage, intent_signal_ages, intent_signals, stage_line
from .output import ICP_FILE, companies, company_row, judge_unreachable_fields, listing_passage, publication_gaps
import budget_guard
import confirmed_leads
import run_attempt
from research_tools import ResearchTools, SOURCE_TEXT_PAGE_SIZE, tolerant_arguments
import tyche_tools
from validate_run import _company_key, company_domain, signal_request_errors, signal_window_start


def final_review_status(run_file, *, limit=5):
    """Verify the exact approved research snapshot and its Arena output fields."""
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


# On 2026-10-12, 12 of 36 main runs and 4 of 13 #132 runs that confirmed a company ended with none published:
# reviews held confirmed companies Leadpoet qualifies (Tare with no failing check; Navro, Origis, Trulioo for
# "not proven current"). A confirmed company is already published, so removing it needs a named reason.
# Offline over that day's runs, 21 confirmed companies were dropped: 1 with every check passing and 8 only for an
# unknown stage or company size (Origis, Toyo, Blackbird Health), which passed at confirmation with captured evidence.
CONFIRMED_HOLD_REFUSAL = (
    "{target} is confirmed and already published to Leadpoet. Hold or reject it only by saving, in the same review, a "
    "required check that fails with captured evidence, or one that is unknown for a requirement other than its stage "
    "or company size. Leadpoet keeps the newest captured round, listing, sale or ownership event and reads the current "
    "LinkedIn band, so an unknown stage or size does not remove a confirmed company: save fail with the captured source "
    "that shows a later round, sale, listing or exit, or a different band. To correct a confirmed company's fields, "
    "accept it with the corrected fields instead. No change was saved.")
SIZE_CRITERION = "icp:company_size"
# Leadpoet's decision summaries (docs/arena-decision-logging.md): short records of the model's own choices, sent with
# its lab_arena_checkpoint.log_decision helper. Each comes from a tool call the model already made and carries the text
# the model wrote there (a lookup purpose, a review reason, final review findings) with the URLs of the evidence it
# selected: no extra model call and no page text. An execution keeps 63 regular records and one finish, so each
# company reports each decision once, counted in one file across workers and tool restarts. A frame is at most 4 KiB
# after ASCII escaping, so evidence URLs and then rationale are trimmed to fit.
DECISION_TEXT_LIMIT = 500
DECISION_RECORDS = 60
DECISION_FILE = "decision-summaries.log"
DECISION_FRAME_BYTES = 3800  # Leaves room for the schema and control fields Leadpoet adds.
DECISIONS = {"accepted": "accept", "rejected": "reject", "unresolved": "defer"}
NEXT_ACTIONS = {"accept": "Confirm the accepted company's evidence packet, then research the next company.",
                "reject": "Research the next company.",
                "defer": "Research the next company; return to this one only with a source for the named gap."}


def decision_logger():
    try:
        return getattr(importlib.import_module("lab_arena_checkpoint"), "log_decision", None)
    except ImportError:
        return None  # Outside Leadpoet's sandbox, or an older runtime without decision summaries.


def evidence_urls(row):
    """The distinct URLs of a saved company's fit and check evidence, at most five."""
    entries = [row.get("account_fit")] + [e for c in row.get("qualification_checks") or [] if isinstance(c, dict)
                                          for e in c.get("evidence") or []]
    urls = [e.get("evidence_url") or e.get("url") for e in entries if isinstance(e, dict)]
    return list(dict.fromkeys(u for u in urls if isinstance(u, str) and u.startswith(("https://", "http://"))
                              and len(u) <= DECISION_TEXT_LIMIT))[:5]


class ArenaResearchTools(ResearchTools):
    """Return publisher field gaps with review responses; shared confirmation stays unchanged.

    Leadpoet charges model tokens to the ICP's allowance, so a tool description omits the JSON schema that repeats
    its field list.
    """

    def _description_view(self, contract):
        view = super()._description_view(contract)
        schema = view.get("inputSchema")
        if isinstance(schema, dict) and isinstance(schema.get("fields"), list) and isinstance(schema.get("jsonSchema"), dict):
            # In local runs the scrape tool's description was 16k characters, 7.4k of them this repeat of its fields.
            required = schema["jsonSchema"].get("required")
            view["inputSchema"] = {**{key: value for key, value in schema.items() if key != "jsonSchema"},
                                   **({"required": required} if required else {}),
                                   "jsonSchema": "omitted: the fields above list the same inputs; "
                                                 "inspect(tool=..., field='inputSchema.jsonSchema') reads it"}
        return view

    def review(self, companies=(), web=(), sources=(), review_ref=None, review_findings=None):
        self._refuse_unexplained_confirmed_holds(companies)
        return super().review(companies, web, sources, review_ref, review_findings)

    def _refuse_unexplained_confirmed_holds(self, companies):
        """A hold or reject of a confirmed company must name the required check that is not passing."""
        removing = [item for item in companies
                    if isinstance(item, dict) and item.get("decision") in ("hold_account", "reject")]
        if not removing:
            return
        try:
            leads = json.loads((self.path.parent / "leads.json").read_text(encoding="utf-8"))["leads"]
            confirmed = {_company_key(row) for row in leads}
        except (OSError, ValueError, KeyError, TypeError):
            return  # No confirmed leads yet: ordinary holds are unchanged.
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
                         check.get("status") == "unknown" and check.get("criterion_id") not in currency))]
            if not named:
                raise ValueError(CONFIRMED_HOLD_REFUSAL.format(target=item["target"]))

    def _stage_and_size_criteria(self):
        """Criterion ids of the request's stage line (with or without its prefix) and company size."""
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
        """Drop only a host-bound stage's redundant bare model transcription."""
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
            return request  # Shared startup owns malformed input and saved-state errors.
        if not stage:
            return request
        full, legacy = stage_line(stage), STAGE_PREFIX + stage
        if (not isinstance(bound, list) or any(not isinstance(value, str) for value in bound)
                or not isinstance(saved_attributes, list)):
            return request
        if full in bound:
            aliases = {stage, legacy} - {full}
        elif legacy in bound:
            aliases = {stage}  # Old short binding: the newer definition is not an alias.
        else:
            return request
        removable = aliases - set(bound)  # A separately bound attribute keeps its own criterion.
        if any(alias in saved_attributes for alias in removable) or not any(alias in attributes for alias in removable):
            return request
        normalized = copy.deepcopy(request)
        normalized["icp"]["required_attributes"] = [value for value in attributes if value not in removable]
        return normalized

    def _start(self, request, provider_credit_limits=None, **options):
        # ResearchTools.start holds the existing startup lock around this hook.
        return super()._start(self._stage_alias_request(self._arena_signal_request(request)),
                              provider_credit_limits=provider_credit_limits, **options)

    def _arena_signal_request(self, request):
        """Keep the saved research requirements aligned with Arena's own signals."""
        icp = json.loads((self.path.parent / ICP_FILE).read_text(encoding="utf-8"))
        expected = [" ".join(query.split()).casefold() for query in intent_signals(icp)]
        ages = intent_signal_ages(icp)
        saved = self._document()["request"] if self.path.exists() else None
        # Production supplies this date. Offline/legacy callers keep the shared saved/UTC date defaults.
        reference = self.environment.get("LAB_ARENA_EVALUATION_DATE")
        if reference:
            datetime.strptime(reference, "%Y-%m-%d")
        for value in ([saved] if saved is not None else []) + [request]:
            if errors := signal_request_errors(value):
                raise ValueError("; ".join(errors))
            signals = value.get("buying_signals")
            if not isinstance(signals, list) or not signals:
                # This check runs before the request's layout check, so say where the field goes: DeepSeek V4.1 Flash
                # nested it inside icp and renamed its kinds instead, 15 times in four ICPs on 2026-10-24.
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
            request["as_of_date"] = reference  # Default an omitted clock from the host, never overwrite a saved one.
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
            return review  # Existing publication feedback names fields that prevent output.
        preview = {
            "required_attribute": submitted.get("required_attribute"),
            "company_stage": submitted["company_stage"],
            "company_stage_evidence": submitted.get("company_stage_evidence", []),
            "intent_signals": submitted["intent_signals"],
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
                # The stage check still passes on a filing row or quote page; this asks for the form the judge reads.
                preview["stage_listing_note"] = (
                    "No emitted stage quote names the listing in a form Leadpoet's judge reliably confirms. Before "
                    "confirming, capture a current company or newswire passage that names the company with its "
                    "exchange and ticker, such as 'Acme Inc. (NASDAQ: ACME) today announced', and add it to the stage "
                    "check (a recent press release usually has one). Filing-table rows without the company name, "
                    "stock quote pages and sec.gov pages often stay unconfirmed. One fetch is enough; if none is "
                    "found, keep the stage as it is.")
        return {"company": review["company"], "arena_submission_preview": preview, **rest}

    def _lookup_view(self, attempt, offset=0, limit=10):
        """Expand captured source text within one bounded lookup response."""
        view = super()._lookup_view(attempt, offset=offset, limit=limit)
        body = attempt.get("result") if isinstance(attempt, dict) else None
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
        """Report unchanged confirmations on a blocked run without workbook export."""
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
        """Finish the shared review as company output, without a workbook."""
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
        phase = "final_review" if self.environment.get("TYCHE_FINALIZATION_ONLY") == "1" else "research"
        label = name if isinstance(name, str) and name in native_counts.TOOLS else "unknown_tool"
        mode = native_counts.inspect_mode(label, arguments)
        native_counts.record(self.path, phase, label, mode, "started")
        try:
            result = self._call_with_publication_feedback(name, arguments)
        except Exception as exc:
            try:
                native_counts.record(self.path, phase, label, mode, "raised", exception=exc)
            except Exception:
                pass  # Private diagnostics cannot replace the original tool exception.
            raise
        native_counts.record(self.path, phase, label, mode, native_counts.outcome(result))
        try:
            self._report_decisions(name, arguments, result)
        except Exception:
            pass  # A summary is best effort and never changes the tool result.
        return result

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
            # The initial approach: this worker's first discovery search, in the purpose the model gave it.
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
        """Claim one of the execution's summaries for key; False when it was already sent or the regular records ran
        out. Finish has its own reserved record."""
        key = json.dumps(list(key))
        with open(self.path.parent / DECISION_FILE, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            sent = handle.read().splitlines()
            if key in sent or decision != "finish" and sum(k != '["finish"]' for k in sent) >= DECISION_RECORDS:
                return False
            handle.write(key + "\n")
            return True

    def _call_with_publication_feedback(self, name, arguments):
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
            # Feedback must not turn a successful source-backed confirmation into an error.
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
    """The Leadpoet broker with this host's two accounting rules.

    1. A new remote provider call is refused before dispatch once spend reaches the
       research allowance inside the request's budget, capped by Leadpoet's limit (pacing.refusal). The
       refusal is an unsent coordination refusal; publishing another company does
       not reopen research.
    2. Leadpoet settles provider billing itself, and TYCHE cannot read Deepline's
       billing feed from the sandbox. A Deepline call that returned without a final
       charge is therefore counted at its documented price bound, as main already
       does for a call no bill can name, instead of staying pending and stopping
       every later paid call. This happens before that call releases its transport
       slot; already admitted siblings can remain in flight. Spend pacing checks
       observed host cost and remains approximate, not a shared headroom reservation.
    """

    def __init__(self, *args, run_file, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_file = run_file

    def execute(self, request, capture, **options):
        operation = request.get("operation")
        provider = "deepline" if operation == "execute" else "scrapingdog"
        # The bundled catalog can price a call at zero even when its actual
        # provider bill is positive. Only local catalog operations skip pacing.
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
