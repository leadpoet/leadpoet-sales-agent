"""Arena entrypoint: ``run_icp(icp) -> list[dict]``.

Pipeline for one ICP (all deadlines from LAB_ARENA_WALL_CLOCK_SECONDS, all spend under the per-ICP governor):
  1. discovery lanes (scout.py): event-first search, stage-first funding search, the exemplar-peer roster with its
     free per-company news, and the ATS hiring lane; each candidate is screened with the judge's deterministic checks,
     resolved (homepage, LinkedIn company record, headquarters), stage-checked and fit-proven from its own pages;
  2. verification (verify.py): identity binding, evidence pages, dates and windows, stage quotes;
  3. the bonus criterion (bonus.py) where a fetched page proves it;
  4. the judge-route page check (preflight.py) on cited intent URLs;
  5. admission (admission.py): up to 5 companies (list-type ICPs) or 3 by expected value, ranked, drops only on
     proven conflicts;
  6. lock and checkpoint (lock.py), the reviewed paragraph, the Sonar re-check, and a final lock.  A company the
     re-check drops leaves the output (admission then runs once more without it); if none is left the output is
     empty.
``run_icp`` returns the last locked list.  With nothing locked, a host fault returns [] (an infrastructure failure
would otherwise leave the assignment incomplete) and our own failure raises for the retry attempt.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
from typing import Any, Mapping

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

try:
    from . import admission, bonus, diagnostics, gates, governor, llm, lock, preflight
    from . import reverify as reverify_module
    from . import scout as scout_module
    from . import intent_details as details_module
    from . import scorer_mirror as sm
    from .arena_tools import STRATEGY as _STRATEGY, ArenaTools
    from .verify import announced_schema, strip_internal, verify_companies, verify_signal
except ImportError:
    _pkg = os.path.basename(_HERE)
    admission, bonus, diagnostics, gates, governor, llm, lock, preflight = (
        importlib.import_module(f"{_pkg}.{m}") for m in
        ("admission", "bonus", "diagnostics", "gates", "governor", "llm", "lock", "preflight"))
    details_module = importlib.import_module(f"{_pkg}.intent_details")
    reverify_module = importlib.import_module(f"{_pkg}.reverify")
    scout_module = importlib.import_module(f"{_pkg}.scout")
    sm = importlib.import_module(f"{_pkg}.scorer_mirror")
    _tools_mod = importlib.import_module(f"{_pkg}.arena_tools")
    _STRATEGY, ArenaTools = _tools_mod.STRATEGY, _tools_mod.ArenaTools
    _verify_mod = importlib.import_module(f"{_pkg}.verify")
    announced_schema, strip_internal = _verify_mod.announced_schema, _verify_mod.strip_internal
    verify_companies, verify_signal = _verify_mod.verify_companies, _verify_mod.verify_signal

DRAFT_LIMIT = int(_STRATEGY.get("draft_limit") or 8)
PARAGRAPH_MODEL = str(_STRATEGY.get("paragraph_model") or "anthropic/claude-sonnet-4.5")
FRUGAL_BELOW_USD = 0.15
# Spend kept for everything after discovery: bonus rows, paragraphs, the Sonar re-check and one replacement round
# (about $0.01-0.05 in local runs), plus the governor's closing reserve.
POST_SCOUT_RESERVE_USD = 0.10
# Deepline calls kept for verification, bonus rows and the judge-route preflight (run c-fix-r1: discovery used all
# 198 calls and the preflight of a cited URL was refused).
POST_SCOUT_CALLS = 30
SCOUT_BUDGET_USD = float(_STRATEGY.get("scout_budget_usd") or 0.30)
BONUS_ROWS = 5
REVERIFY_ENABLED = bool(_STRATEGY.get("reverify", 1))
LAST_REPORT: dict[str, Any] = {}
_GATES_PRINTED = {"done": False}
_INFRA_STATUSES = diagnostics.INFRA_STATUSES
_INFRA_CODES = diagnostics.INFRA_CODES
_HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def _log(message: str) -> None:
    print(f"[arena-harness] {message}", file=sys.stderr, flush=True)


def _company_limit() -> int:
    raw = str(os.environ.get("LAB_ARENA_COMPANY_LIMIT") or "5").strip()
    try:
        return max(1, min(5, int(raw)))
    except ValueError:
        return 5


def provider_infrastructure_error(exc: BaseException | None) -> str | None:
    """Name the host-side fault in an exception chain, else None (the fault is ours)."""

    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        for attribute in ("status_code", "status"):
            value = getattr(exc, attribute, None)
            if isinstance(value, int) and not isinstance(value, bool) and value in _INFRA_STATUSES:
                return "http_%d" % value
        response = getattr(exc, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and not isinstance(value, bool) and value in _INFRA_STATUSES:
            return "http_%d" % value
        text = str(exc)
        for code in _INFRA_CODES:
            if code in text:
                return code
        match = _HTTP_STATUS_RE.search(text)
        if match and int(match.group(1)) in _INFRA_STATUSES:
            return "http_%s" % match.group(1)
        exc = exc.__cause__ or exc.__context__
    return None


def recorded_infra_fault(tools: Any = None) -> str | None:
    fault = getattr(tools, "infra_fault", None)
    if isinstance(fault, str) and fault:
        return fault
    return diagnostics.infra_fault()


def _no_companies(exc: BaseException | None, tools: Any = None) -> list[dict]:
    """Nothing locked: [] after no error or a host fault; our own failure raises (the host grants a retry)."""

    if exc is None:
        return []
    reason = provider_infrastructure_error(exc) or (
        "recorded:%s" % recorded_infra_fault(tools) if recorded_infra_fault(tools) else None)
    if reason:
        LAST_REPORT["no_output"] = "host_fault_completed:%s" % reason
        _log("no companies after a host fault (%s): returning []" % reason)
        return []
    LAST_REPORT["no_output"] = "agent_failure"
    _log("no companies after %s (our fault): raising for the retry attempt" % type(exc).__name__)
    raise exc


def _emit_report() -> None:
    if LAST_REPORT.get("_emitted"):
        return
    LAST_REPORT["_emitted"] = True
    try:
        _log(json.dumps({k: v for k, v in LAST_REPORT.items() if k != "_emitted"}, default=str)[:60000])
    except Exception:  # noqa: BLE001
        pass


def _tools_factory():
    """Tests inject a fake tool client through ARENA_TOOLS_FACTORY='module:callable'."""

    spec = str(os.environ.get("ARENA_TOOLS_FACTORY") or "").strip()
    if not spec:
        return lambda: ArenaTools(timeout=float(os.environ.get("ARENA_TOOL_TIMEOUT_SECONDS", "60") or 60))
    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def _print_gates() -> dict[str, str]:
    if _GATES_PRINTED["done"]:
        return {}
    _GATES_PRINTED["done"] = True
    try:
        report = gates.print_self_test()
    except Exception as exc:  # noqa: BLE001
        _log(f"gate self-test failed: {type(exc).__name__}")
        return {}
    return {k: sum(1 for v in report.values() if v == k) for k in ("pass", "fail", "unknown")}


LEAN_CAP_USD = 0.40
# Rescue mode (earlier attempts spent the cap): one qualified company clears the $0.80 rule, so up to $0.78 in all.
RESCUE_CEILING_USD = 0.78
RESCUE_MARGIN_USD = 0.03
RESCUE_RESERVE_USD = 0.02
RESCUE_POST_SCOUT_USD = 0.03
BONUS_MIN_HEADROOM_USD = 0.08


def _start_governor(clock: Any, tools: Any) -> Any:
    """Earlier attempts' spend from the host snapshot.  Unknown or unsettled prior spend lowers the cap; settled
    prior spend within FRUGAL_BELOW_USD of the cap switches to rescue mode."""

    gov = governor.Governor(clock=clock)
    prior, settled = governor.read_prior()
    if prior is None:
        gov.set_mode_cap(LEAN_CAP_USD)
    else:
        gov.prior = prior + (0.0 if settled else 0.10)
        gov.note_snapshot(gov.prior)
        if not settled:
            gov.set_mode_cap(min(gov.cap, gov.prior + LEAN_CAP_USD))
        elif gov.cap - gov.prior < FRUGAL_BELOW_USD and gov.prior < RESCUE_CEILING_USD - RESCUE_MARGIN_USD:
            gov.cap = max(gov.cap, RESCUE_CEILING_USD)
            gov.reserve = min(gov.reserve, RESCUE_RESERVE_USD)
            gov.set_mode_cap(RESCUE_CEILING_USD)
            LAST_REPORT["rescue"] = True
    _refresh_snapshot(gov, tools)
    LAST_REPORT["prior_spend_usd"] = None if prior is None else round(prior, 4)
    LAST_REPORT["prior_settled"] = settled
    return gov


def scout_budget(gov: Any, rescue: bool = False) -> float:
    """Discovery's budget: the strategy budget within what the cap leaves (rescue: the rescue remainder)."""

    left = float(gov.mode_cap) - float(gov.committed()) - POST_SCOUT_RESERVE_USD
    if rescue:
        return max(0.03, float(gov.mode_cap) - float(gov.committed()) - RESCUE_POST_SCOUT_USD)
    if float(gov.mode_cap) - float(gov.prior) < FRUGAL_BELOW_USD:
        return 0.05
    return max(0.05, min(SCOUT_BUDGET_USD, left))


def _hold_calls(tools: Any) -> int:
    """Lower the call ceiling during discovery so POST_SCOUT_CALLS (at most a quarter of what is left) stay for the
    later phases; returns the number held."""

    try:
        held = min(POST_SCOUT_CALLS, max(0, int(tools.remaining()) // 4))
        tools.call_budget = int(tools.call_budget) - held
        LAST_REPORT["calls_held"] = held
        return held
    except Exception:  # noqa: BLE001 - a tool client without a call ceiling
        return 0


def _release_calls(tools: Any, held: int) -> None:
    if held:
        try:
            tools.call_budget = int(tools.call_budget) + held
        except Exception:  # noqa: BLE001
            pass


def _refresh_snapshot(gov: Any, tools: Any) -> None:
    """Re-base the governor on the host's spend and adopt the round's real Deepline call quota (never raises)."""

    marker = gov.marker()
    snap = governor.read_snapshot()
    usd = governor.snapshot_committed(snap)
    if usd is not None:
        gov.note_snapshot(usd, marker)
    if snap is not None:
        try:
            tools.adopt_quota(snap)
        except Exception:  # noqa: BLE001
            pass


def _evidence_for(row: Mapping[str, Any], report: Any) -> list[dict[str, Any]]:
    urls = {(s.get("matched_icp_signal"), s.get("url")) for s in row.get("intent_signals") or []}
    items = report.evidence.get(sm.company_name_key(row.get("company_name")), [])
    return [e for e in items if (e.get("index"), e.get("url")) in urls]


def _refresh_fallbacks(rows: list[dict[str, Any]], report: Any, icp: Mapping[str, Any]) -> None:
    for row in rows:
        signals = _evidence_for(row, report)
        if signals:
            row["intent_details"] = details_module.fallback_paragraph(
                company_name=str(row.get("company_name") or ""), icp=icp, signals=signals)


def _reverify(rows: list[dict[str, Any]], icp: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict]:
    """The Sonar re-check: drop only a name/website mismatch; other contradictions stay, marked ``rank_last``."""

    kept, notes = reverify_module.rerank([strip_internal(r) for r in rows], icp)
    contested = {str(site) for site in (notes or {}).get("contested") or []}
    by_site = {str(r.get("company_website") or ""): r for r in rows}
    out = []
    for match in kept:
        row = by_site.pop(str(match.get("company_website") or ""), None)
        if row is None:
            continue
        if str(row.get("company_website") or "") in contested:
            row.setdefault("_flags", {})["rank_last"] = True
        out.append(row)
    return out, notes


REPLACE_MIN_S = 90.0


def _site(row: Mapping[str, Any]) -> str:
    return str(row.get("company_website") or "")


def _signal_keys(row: Mapping[str, Any]) -> list[tuple[Any, Any]]:
    return [(s.get("matched_icp_signal"), s.get("url")) for s in row.get("intent_signals") or []]


def _rv_reasons(notes: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in (notes or {}).get("companies") or []:
        if isinstance(entry, Mapping):
            why = entry.get("reason") or entry.get("error") or entry.get("overall") or ""
            out[str(entry.get("company") or "?")[:60]] = f"{entry.get('overall') or ''}: {str(why)[:160]}".strip(": ")
    return out


def _reverify_step(chosen: list[dict[str, Any]], rows: list[dict[str, Any]], report: Any, icp: Mapping[str, Any],
                   clock: Any, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(the rows to submit, notes).  A company the re-check drops leaves the output even when nothing is left.
    With time to spare, admission runs again over the verified rows minus every dropped company, and the new picks
    get their paragraph and one re-check of their own."""

    checked, rv_notes = _reverify(chosen, icp)
    dropped = {_site(r) for r in chosen} - {_site(r) for r in checked}
    notes: dict[str, Any] = {"kept": [r.get("company_name") for r in checked],
                             "dropped": [r.get("company_name") for r in chosen if _site(r) in dropped],
                             "reasons": _rv_reasons(rv_notes)}
    if rv_notes.get("error"):
        notes["error"] = str(rv_notes["error"])[:160]
    if not dropped:
        return checked, notes
    final = checked
    if time.monotonic() < clock.paid_cutoff - REPLACE_MIN_S:
        pool = [r for r in rows if _site(r) not in dropped]
        again, adm = admission.select(pool, icp, limit=limit)
        done = {_site(r): r for r in checked}
        fresh = [r for r in again if _site(r) not in done]
        replacement: dict[str, Any] = {"admission": adm, "new": [r.get("company_name") for r in fresh]}
        if fresh and time.monotonic() < clock.paid_cutoff - 30.0:
            _refresh_fallbacks(fresh, report, icp)
            evidence = {sm.company_name_key(r.get("company_name")): _evidence_for(r, report) for r in fresh}
            replacement["paragraph"] = details_module.write_paragraphs(fresh, icp, evidence=evidence,
                                                                       model_name=PARAGRAPH_MODEL)
            checked_new, rv_new = _reverify(fresh, icp)
            dropped |= {_site(r) for r in fresh} - {_site(r) for r in checked_new}
            done.update({_site(r): r for r in checked_new})
            replacement.update(kept=[r.get("company_name") for r in checked_new],
                               dropped=[r.get("company_name") for r in fresh if _site(r) in dropped],
                               reasons=_rv_reasons(rv_new))
        picked: list[dict[str, Any]] = []
        for row in again:
            prior = done.get(_site(row))
            if prior is None or _site(row) in dropped:
                continue
            if _signal_keys(prior) != _signal_keys(row):
                _refresh_fallbacks([row], report, icp)
                prior = row
            picked.append(prior)
        notes["replacement"] = replacement
        final = picked or checked
    return final, notes


def run_icp(icp: dict) -> list[dict]:
    """Return up to LAB_ARENA_COMPANY_LIMIT companies for one ICP (the last locked list)."""

    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    clock = governor.Clock()
    limit = _company_limit()
    LAST_REPORT.clear()
    LAST_REPORT.update({"icp_id": icp.get("icp_id"), "limit": limit, "clock": clock.as_dict()})
    gate_counts = _print_gates()
    if gate_counts:
        LAST_REPORT["gates"] = gate_counts
    if not str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip() and not os.environ.get("ARENA_TOOLS_FACTORY"):
        _log("no LAB_ARENA_WORKER_SOCKET; returning an empty result")
        LAST_REPORT["error"] = "no worker socket"
        _emit_report()
        return []
    schema = announced_schema(icp)
    if not sm.v5_family(schema):
        schema = gates.V6
    tools = _tools_factory()()
    try:
        import random
        random.seed(int(str(os.environ.get("LAB_ARENA_RANDOM_SEED") or "0").strip() or 0))
    except ValueError:
        pass
    gov = _start_governor(clock, tools)
    rescue = bool(LAST_REPORT.get("rescue"))
    frugal = gov.mode_cap - gov.prior < FRUGAL_BELOW_USD and not rescue
    governor.install(gov, clock)
    llm.reset()
    lock.reset_decisions()
    diagnostics.reset()
    locker = lock.Locker(icp, schema=schema, limit=limit)
    rows: list[dict[str, Any]] = []
    lock.decide("investigate", "find companies that match this ICP and prove its intent criteria",
                evidence=[str(icp.get("icp_id") or ""), str(icp.get("intent_category") or "")])
    try:
        tools.deadline = clock.paid_cutoff
        scout_module.BUDGET_USD = scout_budget(gov, rescue=rescue)
        LAST_REPORT["scout_budget_usd"] = round(scout_module.BUDGET_USD, 4)
        LAST_REPORT["mode"] = "rescue" if rescue else ("frugal" if frugal else "full")
        discovery_s = max(60.0, clock.discovery_end - time.monotonic())
        agent_exc: BaseException | None = None
        drafts: list[dict[str, Any]] = []
        held = _hold_calls(tools)
        try:
            drafts = scout_module.run_scout(icp, tools, limit=DRAFT_LIMIT, run_timeout=discovery_s)
        except Exception as exc:  # noqa: BLE001
            agent_exc = exc
            LAST_REPORT["scout_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            _release_calls(tools, held)
        LAST_REPORT["scout"] = dict(getattr(scout_module, "LAST", {}) or {})
        LAST_REPORT["drafts"] = len(drafts)
        if not drafts:
            return _no_companies(agent_exc, tools)
        _refresh_snapshot(gov, tools)
        rows, report = verify_companies(drafts, icp=icp, tools=tools, schema=schema)
        LAST_REPORT["verify"] = report.counts()
        LAST_REPORT["verify_drops"] = report.details()
        LAST_REPORT["verified"] = [r.get("company_name") for r in rows]
        if rows and time.monotonic() < clock.paid_cutoff and (rescue or gov.headroom() < BONUS_MIN_HEADROOM_USD):
            LAST_REPORT["bonus"] = {"skipped": "rescue" if rescue else "headroom %.3f" % gov.headroom()}
        elif rows and time.monotonic() < clock.paid_cutoff:
            ranked = sorted(rows, key=lambda r: -admission.estimates(r, icp)["q"] * admission.value(r, icp))
            LAST_REPORT["bonus"] = bonus.attach(ranked[:BONUS_ROWS], report, icp, tools, verify_signal=verify_signal,
                                                deadline=clock.paid_cutoff - 60.0, clock=time.monotonic)
        if rows and time.monotonic() < clock.paid_cutoff:
            LAST_REPORT["preflight"] = preflight.check(rows, tools, deadline=clock.paid_cutoff - 30.0)
        _refresh_snapshot(gov, tools)
        chosen, notes = admission.select(rows, icp, limit=limit)
        LAST_REPORT["admission"] = notes
        try:
            LAST_REPORT["rows"] = [admission.explain(r, icp) for r in rows]
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            LAST_REPORT["rows"] = f"{type(exc).__name__}"
        _refresh_fallbacks(chosen, report, icp)
        for row in rows:
            accepted = any(row is c or row.get("company_name") == c.get("company_name") for c in chosen)
            lock.decide("accept" if accepted else "reject", "choose companies to submit",
                        candidate=str(row.get("company_name") or ""),
                        evidence=[f"tier {(row.get('_flags') or {}).get('fit_tier')}",
                                  f"stage proven {(row.get('_flags') or {}).get('stage_proven')}"])
        if not chosen:
            return locker.locked
        locker.lock(chosen)
        if time.monotonic() < clock.paid_cutoff - 30.0:
            evidence = {sm.company_name_key(r.get("company_name")): _evidence_for(r, report) for r in chosen}
            LAST_REPORT["paragraph"] = details_module.write_paragraphs(chosen, icp, evidence=evidence,
                                                                       model_name=PARAGRAPH_MODEL)
            locker.lock(chosen)
        if REVERIFY_ENABLED and locker.locked and time.monotonic() < clock.paid_cutoff - 30.0:
            final, LAST_REPORT["reverify"] = _reverify_step(chosen, rows, report, icp, clock, limit)
            locker.replace(final)
        return locker.locked
    except Exception as exc:  # noqa: BLE001
        LAST_REPORT["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if locker.locked:
            return locker.locked
        return _no_companies(exc, tools)
    finally:
        LAST_REPORT["locker"] = locker.as_dict()
        LAST_REPORT["governor"] = gov.as_dict()
        LAST_REPORT["llm"] = dict(llm.STATS)
        LAST_REPORT["tools"] = {"calls": getattr(tools, "calls", None),
                                "by_tool": dict(getattr(tools, "calls_by_tool", None) or {}),
                                "egress": dict(getattr(tools, "egress", None) or {})}
        LAST_REPORT["seconds"] = round(time.monotonic() - clock.t0, 1)
        LAST_REPORT["diagnostics"] = diagnostics.snapshot()
        lock.decide("finish", "submit the locked companies", evidence=[f"{len(locker.locked)} companies"])
        governor.install(None)
        _emit_report()
        try:
            tools.close()
        except Exception:  # noqa: BLE001
            pass


def get_last_usage() -> dict:
    return {"report": dict(LAST_REPORT)}


__all__ = ["run_icp", "get_last_usage", "provider_infrastructure_error"]
