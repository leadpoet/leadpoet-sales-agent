"""Arena entrypoint: ``run_icp(icp) -> list[dict]``.

Synchronous, one positional argument, returns a list of company dicts in the
public competition schema.  The host writes companies.json itself.

Pipeline (all inside the Arena's 300 s wall clock):
  1. read LAB_ARENA_COMPANY_LIMIT (1..5, default 5) -- a HARD cap: a list longer
     than MAX_COMPANIES=5 is rejected whole (qualification/competition_models.py:128,
     lab_arena/output.py:18) and the ICP scores 0
  2. run the PydanticAI agent with Deepline-only tools (agent.py), under a
     RESEARCH deadline that stops tool calls in time for a final answer
  3. hand the RUN deadline back (verification_deadline) and verify and repair
     every draft against the scorer's own rules (verify.py)
  4. re-check every emitted company against perplexity/sonar and refill any slot
     a MISMATCH emptied from the verified surplus (reverify.py,
     backfill_empty_slots) -- never above the five-company cap
  5. return the validated, ranked list; return [] for a genuinely empty search
     OR for a run the HOST broke (the exception chain or the recorded
     infrastructure channel says so), and RAISE only when the failure was ours
     (see _no_companies and diagnostics.py)

Nothing here spends or submits by itself; the Arena runner calls it.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import sys
import threading
import time
from typing import Any, Mapping, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

try:
    from . import diagnostics
    from . import scorer_mirror as sm
    from .arena_tools import ArenaTools
    from .verify import verify_companies
except ImportError:
    import importlib

    _pkg = os.path.basename(_HERE)
    diagnostics = importlib.import_module(f"{_pkg}.diagnostics")
    sm = importlib.import_module(f"{_pkg}.scorer_mirror")
    ArenaTools = importlib.import_module(f"{_pkg}.arena_tools").ArenaTools
    verify_companies = importlib.import_module(f"{_pkg}.verify").verify_companies

try:
    from .arena_tools import STRATEGY as _STRATEGY
    from .verify import announced_schema, strip_internal
    from . import intent_details as details_module
except ImportError:
    _STRATEGY = importlib.import_module(f"{_pkg}.arena_tools").STRATEGY
    _verify_mod = importlib.import_module(f"{_pkg}.verify")
    announced_schema, strip_internal = _verify_mod.announced_schema, _verify_mod.strip_internal
    details_module = importlib.import_module(f"{_pkg}.intent_details")

try:
    from . import contacts as contacts_module
except ImportError:
    try:
        import importlib as _importlib
        contacts_module = _importlib.import_module(f"{os.path.basename(_HERE)}.contacts")
    except Exception:
        contacts_module = None


def _strategy_number(key: str, default: float, low: float, high: float) -> float:
    value = _STRATEGY.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        default = float(value)
    return max(low, min(high, default))


WALL_CLOCK_SECONDS = 300.0
SAFETY_SECONDS = 22.0
AGENT_SHARE = 0.62
AGENT_SHARE_V5 = 0.55
SHORT_CLOCK_SECONDS = _strategy_number("short_clock_seconds", WALL_CLOCK_SECONDS, 120.0, WALL_CLOCK_SECONDS)
LONG_CLOCK_SECONDS = _strategy_number("long_clock_seconds", 0.0, 0.0, 5400.0)
AGENT_LONG_SECONDS = _strategy_number("agent_long_seconds", 900.0, 60.0, 3600.0)
LONG_PATH_MARGIN_SECONDS = 120.0
CONTACTS_ENABLED = _strategy_number("contacts", 1, 0, 1) >= 1
CONTACT_CANDIDATES = int(_strategy_number("contact_candidates", 3, 1, 6))
PARAGRAPH_ENABLED = _strategy_number("intent_paragraph", 1, 0, 1) >= 1
ICP_SPEND_CAP_USD = _strategy_number("icp_spend_cap_usd", 1.60, 0.0, 4.0)
COMPLETION_HOLDBACK_USD = _strategy_number("completion_holdback_usd", 0.08, 0.0, 1.0)
RESEARCH_CAP_USD = round(max(0.0, ICP_SPEND_CAP_USD - COMPLETION_HOLDBACK_USD), 4)
QUOTA_READS_MAX = 16
START_READ_RETRIES = 2
START_READ_BACKOFF_S = 1.5
SNAPSHOT_CACHE_MARGIN_S = 1.5
_QUOTA_READ_LOCK = threading.Lock()
PARAGRAPH_EST_USD = 0.04
PAIR_ALLOWANCE_USD = 0.80
COMPLETION_RESERVE_USD = 0.05
MIN_CONTACT_USD = 0.02
MAX_PAIRS = 5
REVERIFY_EST_USD = 0.016
PARAGRAPH_MAX_USD = 0.06
ATS_BONUS = _strategy_number("ats_bonus", 1, 0, 1) >= 1
ATS_PICK_MODEL = str(_STRATEGY.get("ats_pick_model") or "google/gemini-2.5-flash")
ATS_BONUS_SECONDS = _strategy_number("ats_bonus_seconds", 150.0, 10.0, 400.0)
RESERVE_DRAFTS = int(_strategy_number("reserve_drafts", 0, 0, 5))
REVERIFY_MAX_COMPANIES = 8
RESERVE_CHECKED = 3
LAST_REPORT: dict[str, Any] = {}


def _company_limit() -> int:
    raw = str(os.environ.get("LAB_ARENA_COMPANY_LIMIT") or "5").strip()
    try:
        return max(1, min(5, int(raw)))
    except ValueError:
        return 5


def announced_wall_clock() -> Optional[float]:
    """The wall clock the sandbox announces for this run, or None when it does not.

    LAB-LOG #309: `LAB_ARENA_WALL_CLOCK_SECONDS` is set for every sandbox by
    `runtime.sandbox_environment` (upstream 1cf9ce2d).  Anything unparseable,
    non-finite or under the contract minimum (30 s) is treated as absent, so a
    stray value can only ever shorten our plan back to the short clock.
    """

    raw = str(os.environ.get("LAB_ARENA_WALL_CLOCK_SECONDS") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 30.0:
        return None
    return value


def plan_clock() -> dict:
    """The two deadlines a run plans against, and where they came from.

    ``short``   -- a complete, contract-valid checkpoint must exist by here.
    ``horizon`` -- the run must have RETURNED by here (minus SAFETY_SECONDS).
    ``long``    -- whether the phases may spread over the horizon: the research
                   phase then gets up to AGENT_LONG_SECONDS and the later phases
                   plan against the horizon instead of the short clock.
    ``source``  -- "sandbox" when the announced clock decided, else "strategy".
    """

    announced = announced_wall_clock()
    if announced is None:
        short = SHORT_CLOCK_SECONDS
        horizon = LONG_CLOCK_SECONDS if LONG_CLOCK_SECONDS > short else short
        source = "strategy"
    else:
        short = min(SHORT_CLOCK_SECONDS, announced)
        horizon = announced
        if LONG_CLOCK_SECONDS > 0:
            horizon = min(horizon, max(LONG_CLOCK_SECONDS, short))
        source = "sandbox"
    horizon = max(short, horizon)
    return {"short": short, "horizon": horizon, "announced": announced, "source": source,
            "long": horizon >= short + LONG_PATH_MARGIN_SECONDS}


def _log(message: str) -> None:
    print(f"[arena-harness] {message}", file=sys.stderr, flush=True)


_INFRA_STATUSES = diagnostics.INFRA_STATUSES
_INFRA_CODES = diagnostics.INFRA_CODES
_HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def provider_infrastructure_error(exc: BaseException | None) -> str | None:
    """Name the host-side fault in an exception chain, else None (the fault is ours).

    The broker hands submitted code a 502 ``provider_unavailable`` for a provider
    outage, and since upstream `10b602f5` it also rewrites an OpenRouter error
    returned inside an HTTP 200 to that error's real status -- so a rate limit
    that used to arrive as an unparseable "success" now arrives as a 429.
    """
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
    """The host fault the run SAW, even though no exception carries it any more.

    LAB-LOG #234 gate 1 (the CRITICAL one).  ``agent.guard`` turns every tool
    exception into ``{"ok": False, ...}`` so the loop can keep going, and that
    includes the ``RuntimeError("provider_unavailable")`` a Deepline 429/5xx
    arrives as (`lab_arena/operations.py:1661-1667` collapses every
    infrastructure status into one generic 502 with that body).  The run then
    fails for some later, innocent-looking reason and
    ``provider_infrastructure_error`` -- which reads ONLY the final exception
    chain -- finds nothing.  The host, meanwhile, has the infrastructure call on
    record (`runner.py:1395-1405`), so our raise is rewritten to
    ``provider_error`` (`:1406-1408`), which is not model-caused, and the round
    dies for everyone.  So consult the record as well as the chain.
    """

    fault = getattr(tools, "infra_fault", None)
    if isinstance(fault, str) and fault:
        return fault
    return diagnostics.infra_fault()


def _no_companies(exc: BaseException | None, tools: Any = None) -> list[dict]:
    """What to hand back when the run produced nothing.

    ⚠️ This reverses the infrastructure branch of #224, which looked only at our
    own ICP score. The round-level rule dominates it
    (`scripts/185-lab-arena-miner-credentials.sql:610`):

        HAVING bool_and(runs.status <> 'accepted')
           AND NOT bool_or(terminal_cause IN (model_timeout, invalid_output,
                           budget_exhausted, credential_error, model_error))
        ...
        IF v_incomplete > 0 THEN cancel the WHOLE round

    That exclusion list is exactly MODEL_CAUSED_TERMINAL_CAUSES. So an assignment
    with no accepted run whose failures were all INFRASTRUCTURE counts as
    incomplete, and **one incomplete assignment cancels the round for everybody**.
    We field 4 submissions x 20 ICPs = 80 assignments, so raising on a host fault
    multiplies that exposure by 80 -- and a cancelled round voids every ICP we
    scored and any crown we earned. Three rounds in a row died this way
    (09-06 `capacity:scoring2:1`, 09-07 `scoring_window_closed`,
    09-08 `capacity:stage1:4`).

    So:
      * host fault (429/5xx/shared account, broker unavailable) -> return [].
        An accepted run with zero companies cannot make the assignment
        incomplete. We trade one ICP's zero -- which is all a failed ICP was ever
        worth -- for the round staying alive.
      * our own fault -> raise. `model_error` IS in the exclusion list, so it
        cannot cancel the round, and a failed run still earns one confirmation
        attempt (`scripts/179-lab-arena-v1.sql:1859`). Free upside, no round risk.
      * no exception at all -> return []: an empty search is an honest answer.

    A host fault counts whether it is still in the exception chain or only in
    the recorded infrastructure channel (`recorded_infra_fault`): what decides
    the round is what the HOST has in `state.calls`, not what our last exception
    happens to remember. A genuine agent failure with NO fault on either still
    raises -- that contract is the free confirmation attempt and is unchanged.
    """
    if exc is None:
        return []
    reason = provider_infrastructure_error(exc)
    if not reason:
        recorded = recorded_infra_fault(tools)
        if recorded:
            reason = "recorded:%s" % recorded
    if reason:
        LAST_REPORT["no_output"] = "host_fault_completed:%s" % reason
        _log("no companies after a HOST fault (%s): returning [] so this assignment "
             "completes -- an incomplete infrastructure assignment cancels the whole round" % reason)
        return []
    LAST_REPORT["no_output"] = "agent_failure"
    _log("no companies after %s (our fault): raising for the confirmation attempt "
         "-- model_error cannot cancel the round" % type(exc).__name__)
    raise exc


def verification_deadline(started: float) -> float:
    """The RUN deadline -- what ``tools.deadline`` must be reset to before verification.

    LAB-LOG #236 idea 3.  ``run_icp`` sets ``tools.deadline`` to the RESEARCH
    cut-off (``agent_timeout - 35 s``) so the model stops calling tools in time to
    still emit ``submit_companies`` itself.  That cut-off is by construction
    ALREADY IN THE PAST when the agent returns -- at the status-quo dials it
    lands ~137 s in, against an agent that may run to ~172 s -- and it was never
    reset.

    It really does bind afterwards, verified in this tree, not assumed:
    ``ArenaTools._deepline`` raises ``BudgetExhausted("time budget exhausted")``
    past the deadline (arena_tools.py:324), ``fetch_page`` turns that into
    ``Page(url, error="budget")`` (arena_tools.py:571-574, :609-610), and
    ``verify.verify_signal`` reads ``if not page.ok`` and drops the signal as
    "evidence page unavailable (budget)" (verify.py) -- then drops the company
    for "no verifiable intent signal".  So every evidence page the AGENT did not
    already cache was unfetchable during verification, and the company built on
    it was thrown away for a clock we set ourselves.  Pinned by
    ``test_verification_deadline.py``.

    The reset is to the run's own wall clock and nothing wider: the Arena kills
    the sandbox at WALL_CLOCK_SECONDS and SAFETY_SECONDS is the margin, so this
    is exactly the envelope LAB-LOG #235 §7 already prices (a worst-case tool
    fired at ~277 s settles by ~352 s, ~4.7x inside the post-35f81d81 lease
    budget).  Deepline calls are also not charged against the $5 OpenRouter
    execution cap, and ``ArenaTools.call_budget`` still caps their number.
    """

    return started + (WALL_CLOCK_SECONDS - SAFETY_SECONDS)


def identity_keys(company: Mapping[str, Any]) -> set[str]:
    """The two identities a refill must not repeat: the name key and the registrable host."""

    keys = {sm.company_name_key(company.get("company_name"))}
    host = sm.registrable_host(str(company.get("company_website") or ""))
    if host:
        keys.add(host)
    return {k for k in keys if k}


def backfill_empty_slots(companies: list[dict], report: Any, *, limit: int,
                         schema: str = sm.SCHEMA_V1, exclude: Optional[set[str]] = None) -> tuple[list[dict], list[str]]:
    """Fill slots the sonar re-verification EMPTIED, from the verified surplus.

    LAB-LOG #236 idea 2.  ``verify_companies`` ranks every locally verified
    company and truncates to ``limit``; the ones it cut are kept in
    ``report.surplus``.  ``reverify.rerank`` may then EVICT a company -- and only
    ever on an explicit MISMATCH verdict (reverify.py:616-619); "unprovable", a
    timeout, a refused call and a ``None`` verdict are all NO INFORMATION and
    keep the company (reverify.py:585-589).  This function refills what that
    eviction emptied.

    Three rules, all load-bearing:

    * ⛔ It only ever APPENDS into a genuinely empty slot.  A company that passed
      local verification is never replaced, re-ranked or competed against: if
      ``len(companies) >= limit`` this returns the list unchanged.
    * ⛔ The result can never exceed ``limit`` (<= 5).  A sixth company is not an
      ignored extra -- ``validate_companies`` rejects the whole list
      ("too many companies", qualification/competition_models.py:128,
      MAX_COMPANIES=5 at lab_arena/output.py:18), the run terminates
      ``invalid_output`` and the ICP scores 0.  The final list is re-validated
      through the scorer mirror before it is returned, and the original list is
      returned untouched if that fails.
    * ⛳️ POSITION.  The platform scores ``company_scores[:goal]`` in MODEL ORDER,
      not best-first (lab_arena/verify.py:242-243, lab_arena/scoring.py:260-261,
      qualification/scoring/competition.py:270 ``list(companies)[:_company_goal(icp)]``).
      A back-filled company is therefore appended LAST, deliberately: it ranked
      below every survivor on the local estimate, and it is the one company in
      the list the sonar gate has NOT seen (it was drawn after the reverify call
      went out).  If ``goal`` ever turns out smaller than what we emit -- the
      only case where position decides anything -- the unchecked candidate is the
      one that falls outside the slice, never a re-verified survivor.

    * ⛔ ``exclude`` (LAB-LOG #315): the identities of every company an explicit
      sonar MISMATCH evicted this run.  A reserve company that was promoted, then
      rejected, used to come straight back from the same surplus with its cached
      contact -- the pre-check's verdict undone by its own refill.

    Returns ``(companies, names_added)``; ``names_added`` is empty when nothing moved.
    """

    limit = max(1, int(limit))
    if len(companies) >= limit:
        return companies, []
    pool = [c for c in list(getattr(report, "surplus", None) or []) if isinstance(c, dict)]
    if not pool:
        return companies, []
    keys = {sm.company_name_key(c.get("company_name")) for c in companies}
    hosts = {sm.registrable_host(str(c.get("company_website") or "")) for c in companies}
    banned = set(exclude or ())
    out = list(companies)
    added: list[str] = []
    for candidate in pool:
        if len(out) >= limit:
            break
        key = sm.company_name_key(candidate.get("company_name"))
        host = sm.registrable_host(str(candidate.get("company_website") or ""))
        if key in keys or (host and host in hosts):
            continue
        if banned & identity_keys(candidate):
            continue
        keys.add(key)
        hosts.add(host)
        out.append(candidate)
        added.append(str(candidate.get("company_name") or ""))
    if not added:
        return companies, []
    try:
        out = sm.validate_output(out, max_companies=limit, schema_version=schema)
    except Exception as exc:
        _log("back-fill rejected by the output contract (%s): emitting the verified list unchanged"
             % type(exc).__name__)
        return companies, []
    return out, added


def recheck_ashby_postings(companies: list[dict], report: Any, tools: Any, *, deadline: float, limit: int,
                           schema: str, exclude: set[str]) -> list[dict]:
    """Loop s22 (teardown-0925 #6, upstream ab3a1e33): just before output, re-read the Ashby board API (free) and
    drop a row whose posting left the board -- the judge now scores it a company-local zero -- then refill that slot
    from the verified surplus.  Bounded (6 board reads, 20 s each) and never raises: on any doubt the rows stand."""

    saved = getattr(tools, "timeout", None)
    try:
        try:
            from . import hiring as hiring_module
        except ImportError:
            hiring_module = importlib.import_module(f"{os.path.basename(_HERE)}.hiring")
        if saved is not None:
            tools.timeout = min(float(saved), 20.0)
        surplus = [c for c in list(getattr(report, "surplus", None) or []) if isinstance(c, dict)]
        gone = hiring_module.recheck_ashby(companies + surplus, tools, deadline=deadline)
        if not gone:
            return companies
        keys = set().union(*(identity_keys(c) for c in gone))
        kept = [c for c in companies if not (identity_keys(c) & keys)]
        if len(kept) == len(companies):
            return companies
        out, added = backfill_empty_slots(kept, report, limit=min(limit, len(companies)), schema=schema,
                                          exclude=set(exclude) | keys)
        LAST_REPORT["ashby_unlisted"] = {"dropped": [str(c.get("company_name") or "") for c in gone], "backfilled": added}
        checkpoint(out, schema=schema, limit=limit)
        return out
    except Exception as exc:
        LAST_REPORT["ashby_unlisted"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        return companies
    finally:
        if saved is not None:
            try:
                tools.timeout = saved
            except Exception:
                pass


_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")
_DOMAIN_RE = re.compile(r"\"domain\":\s*\"([^\"]+)\"")
_DISCOVERY_TOOLS = ("search_companies", "search_web", "search_news", "get_company_events",
                    "get_company_profile", "fetch_page")
_NON_CANDIDATE_HOSTS = frozenset({
    "linkedin.com", "techcrunch.com", "prnewswire.com", "businesswire.com", "globenewswire.com",
    "crunchbase.com", "bloomberg.com", "reuters.com", "google.com", "wikipedia.org", "x.com",
    "twitter.com", "youtube.com", "medium.com", "forbes.com", "nytimes.com", "wsj.com", "ft.com",
    "sec.gov", "greenhouse.io", "lever.co", "workable.com", "indeed.com", "glassdoor.com",
})


def candidate_origins(tools: Any, companies: Optional[list] = None) -> dict[str, Any]:
    """Observable candidate identities per discovery path, read from the trace (#317 addendum).

    ``discovered`` counts only ``search_companies`` rows, which #281 §4 recorded as
    a counter artifact: on the 09-12 runs 15 of 27 returned companies came from
    attempts that reported zero discoveries because event-first research found
    them through ``search_web``.  This reads every discovery tool's trace entry
    for the registrable hosts it named (result rows and call arguments), keeps
    them per tool, and says for each returned company which paths had shown its
    host -- or ``unknown`` when none had, which is distinct from a zero count.
    An upper bound on identities, not a count of qualified candidates.  Telemetry
    only; never raises.
    """

    by_tool: dict[str, set[str]] = {}
    for entry in getattr(tools, "trace", None) or []:
        if not isinstance(entry, dict):
            continue
        tool = str(entry.get("tool") or "")
        if tool not in _DISCOVERY_TOOLS:
            continue
        try:
            text = str(entry.get("result") or "") + " " + json.dumps(entry.get("args") or {}, default=str)
        except Exception:
            text = str(entry.get("result") or "")
        hosts = by_tool.setdefault(tool, set())
        for url in _URL_RE.findall(text):
            host = sm.registrable_host(url)
            if host and host not in _NON_CANDIDATE_HOSTS:
                hosts.add(host)
        for domain in _DOMAIN_RE.findall(text):
            host = sm.registrable_host("https://%s/" % domain.strip().lower())
            if host and host not in _NON_CANDIDATE_HOSTS:
                hosts.add(host)
    seen: set[str] = set().union(*by_tool.values()) if by_tool else set()
    origins: dict[str, list[str]] = {}
    unknown = 0
    for company in companies or []:
        if not isinstance(company, dict):
            continue
        host = sm.registrable_host(str(company.get("company_website") or ""))
        paths = sorted(tool for tool, hosts in by_tool.items() if host and host in hosts)
        origins[host or str(company.get("company_name") or "?")] = paths or ["unknown"]
        if not paths:
            unknown += 1
    return {"candidates_seen": len(seen),
            "candidates_by_tool": {tool: len(hosts) for tool, hosts in sorted(by_tool.items())},
            "origins": origins, "origin_unknown": unknown}


def _write_empty_snapshot(icp: Mapping[str, Any], tools: Any, usage: Optional[Mapping[str, Any]] = None) -> None:
    """Local runs only: the exact research state at a voluntary empty decision (#317 addendum).

    The paired recovery experiment branches from this state (the same trace,
    pages and counters) so every arm shares one research expense.  Written only
    when ARENA_EMPTY_SNAPSHOT_DIR is set, which the sandbox never does; can
    never change what the ICP returns.
    """

    target = os.environ.get("ARENA_EMPTY_SNAPSHOT_DIR", "").strip()
    if not target:
        return
    try:
        snap = tools.snapshot() if hasattr(tools, "snapshot") else {"trace": list(getattr(tools, "trace", None) or [])}
        snap["icp"] = dict(icp)
        snap["agent_usage"] = dict(usage or {})
        os.makedirs(target, exist_ok=True)
        path = os.path.join(target, "%s-empty.json" % str(icp.get("icp_id") or "icp"))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(snap, handle, default=str)
        LAST_REPORT["empty_snapshot"] = path
    except Exception as exc:
        LAST_REPORT["empty_snapshot_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])


def _funnel(tools: Any, *, drafted: int, verify_out: int | None, reverify_out: int | None,
            returned: int, terminal: str, companies: Optional[list] = None) -> dict[str, Any]:
    """The per-ICP funnel, in stderr, on EVERY path (LAB-LOG #248).

    ⚠️ A rejection histogram alone cannot tell "the model produced nothing" from
    "verification discarded everything" -- both end at zero companies. Only the
    stage counts separate them, so they are emitted even (especially) when the
    ICP returns empty, which is exactly the path that used to log nothing:
    `if not drafts: return _no_companies(...)` returned before the report line.

    `verify_out`/`reverify_out` are None when that stage never ran, which is
    itself the signal -- do not coerce them to 0.
    """

    out: dict[str, Any] = {
        "discovered": int(getattr(tools, "discovered", 0) or 0),
        "discovered_bucket_ok": int(getattr(tools, "discovered_bucket_ok", 0) or 0),
        "discovered_with_linkedin": int(getattr(tools, "discovered_with_linkedin", 0) or 0),
        "drafted": int(drafted),
        "verify_in": int(drafted),
        "verify_out": verify_out,
        "reverify_out": reverify_out,
        "returned": int(returned),
        "terminal": terminal,
    }
    try:
        out.update(candidate_origins(tools, companies))
    except Exception:
        pass
    return out


def _emit_report() -> None:
    """Write the one stderr record for this ICP, exactly once.

    stderr is the only channel that survives the sandbox, and the run used to
    emit this only on the success path. It now runs from `finally`, so an empty
    ICP and a raising ICP are as legible as a scoring one. The `verify` block
    stays out: it carries company names, URLs and interpolated reasons, and the
    bounded histograms in `verify_counts` say the same thing in a form that
    aggregates across runs.
    """

    if LAST_REPORT.get("_emitted"):
        return
    LAST_REPORT["_emitted"] = True
    try:
        _log(json.dumps({k: v for k, v in LAST_REPORT.items()
                         if k not in ("verify", "_emitted")}))
    except Exception:
        pass


def checkpoint(companies: list[dict], *, schema: str, limit: int) -> bool:
    """Publish the current companies as the sandbox's output document, atomically.

    LAB-LOG #308.  agent_entrypoint writes our final return through the same
    ``lab_arena_checkpoint.write``; writing earlier makes every phase boundary a
    recoverable state under atomic_checkpoint_45m_v1 (runtime.observe_checkpoint
    reads the last VALID document at the deadline).  Validated through the mirror
    first so a checkpoint is never the malformed document that policy discards.
    Never raises; False means nothing was written.

    LAB-LOG #316 (independent review of #315): an EMPTY list is written when an
    earlier checkpoint of this run put companies on disk -- the sonar pre-check
    rejecting the last company must not leave that company as the document a
    timeout would recover.  It is NOT written when nothing was ever checkpointed:
    under the checkpoint policy a valid empty document turns a run that RAISES
    into an accepted zero (runtime: nonzero exit + valid checkpoint = accepted),
    which would forfeit the confirmation attempt a model_error still earns.
    """

    if not companies and not LAST_REPORT.get("checkpoints"):
        return False
    path = str(os.environ.get("LAB_ARENA_OUTPUT_PATH") or "").strip()
    if not path.startswith("/"):
        return False
    try:
        rows = sm.validate_output([strip_internal(c) for c in companies], max_companies=max(1, int(limit)),
                                  schema_version=schema)
    except Exception as exc:
        LAST_REPORT.setdefault("checkpoint_errors", []).append(f"validate: {str(exc)[:100]}")
        return False
    try:
        import lab_arena_checkpoint
    except ImportError:
        return False
    try:
        from pathlib import Path

        lab_arena_checkpoint.write(rows, output_path=Path(path))
    except Exception as exc:
        LAST_REPORT.setdefault("checkpoint_errors", []).append(f"write: {type(exc).__name__}")
        return False
    LAST_REPORT["checkpoints"] = int(LAST_REPORT.get("checkpoints") or 0) + 1
    return True


def read_quota(tools: Any) -> dict | None:
    """The SAFE v2 quota read; adopt the real Deepline ceiling and record our spend.

    ⚠️ Only ``include_sourcing_cost=True``.  A failed plain (v1) read marks
    ``trusted_quota_failure`` on the host, and a non-accepted run after that is an
    infrastructure failure that cancels the round for every miner (#299, upstream
    1aa25db8).  The v2 cost read is exempt: bc083c46 keeps its failures out of run
    health (#307).  Bounded to QUOTA_READS_MAX per run, far under the host's 256.
    """

    with _QUOTA_READ_LOCK:
        reads = int(LAST_REPORT.get("quota_reads") or 0)
        if reads >= QUOTA_READS_MAX:
            return None
        LAST_REPORT["quota_reads"] = reads + 1
    try:
        import lab_arena_checkpoint
    except ImportError:
        return None
    try:
        snapshot = lab_arena_checkpoint.quota_usage(include_sourcing_cost=True)
    except Exception as exc:
        LAST_REPORT["quota_error"] = type(exc).__name__
        return None
    try:
        LAST_REPORT["quota_adopted"] = tools.adopt_quota(snapshot)
        cost = snapshot.get("sourcing_cost") if isinstance(snapshot, dict) else None
        if isinstance(cost, dict):
            LAST_REPORT["sourcing_cost_usd"] = {
                k: round(int(cost.get(k) or 0) / 1e6, 4)
                for k in ("successful_microusd", "success_unresolved_microusd", "settled_microusd")
                if isinstance(cost.get(k), int)}
    except Exception:
        pass
    return snapshot if isinstance(snapshot, dict) else None


def confirmed_spend_usd(snapshot: dict | None, tools: Any) -> float:
    """What the ICP has spent so far: the host's number when readable, else our estimate."""

    cost = snapshot.get("sourcing_cost") if isinstance(snapshot, dict) else None
    if isinstance(cost, dict):
        try:
            return (int(cost.get("successful_microusd") or 0) + int(cost.get("success_unresolved_microusd") or 0)) / 1e6
        except (TypeError, ValueError):
            pass
    try:
        return float(tools.estimated_spend_usd())
    except Exception:
        return 0.0


def _paid_allowed(tools: Any, estimate_usd: float = 0.0) -> bool:
    """The LIVE spend decision (#314); a tools object without the guard allows everything."""

    check = getattr(tools, "paid_allowed", None)
    if not callable(check):
        return True
    try:
        return bool(check(estimate_usd))
    except Exception:
        return True


def _note_spend(tools: Any, usd: float) -> None:
    """Tell the ledger about a model-side charge it cannot see."""

    note = getattr(tools, "note_external_spend", None)
    if callable(note):
        try:
            note(usd)
        except Exception:
            pass


def refresh_spend(tools: Any) -> float:
    """Re-base the tools' ledger on the host's sourcing_cost when it is readable.

    Returns the spend the run now plans against.  ⚠️ Only the v2 read (read_quota)
    is used; a failed v1 read cancels the round (#299).  A missing or unreadable
    snapshot leaves the local estimate in charge.
    """

    read_started = time.monotonic()
    reads_before = int(LAST_REPORT.get("quota_reads") or 0)
    snapshot = read_quota(tools)
    attempted = int(LAST_REPORT.get("quota_reads") or 0) > reads_before
    as_of = read_started - SNAPSHOT_CACHE_MARGIN_S
    pending = getattr(tools, "_pending_read_cutoff", None)
    if isinstance(pending, (int, float)):
        as_of = min(as_of, float(pending))
    spend = confirmed_spend_usd(snapshot, tools)
    cost = snapshot.get("sourcing_cost") if isinstance(snapshot, dict) else None
    if not isinstance(cost, dict) and attempted:
        try:
            tools._pending_read_cutoff = as_of
        except Exception:
            pass
    if isinstance(cost, dict):
        settled = snapshot_settled(cost)
        LAST_REPORT["spend_basis"] = "host_settled" if settled else "host"
        try:
            tools._pending_read_cutoff = None
            if getattr(tools, "_unknown_start", False):
                tools._unknown_start = False
                tools.spend_cap_usd = getattr(tools, "_phase_cap_usd", RESEARCH_CAP_USD)
                LAST_REPORT["unknown_start_resolved"] = True
        except Exception:
            pass
        try:
            tools.set_confirmed_spend(spend, settled=settled, as_of=as_of)
        except TypeError:
            try:
                tools.set_confirmed_spend(spend)
            except Exception:
                pass
        except Exception:
            pass
    else:
        LAST_REPORT["spend_basis"] = "local"
    try:
        spend = float(tools.spend_usd())
    except Exception:
        pass
    LAST_REPORT["spend_usd"] = round(spend, 4)
    LAST_REPORT["allow_paid"] = _paid_allowed(tools)
    return spend


def snapshot_settled(cost: Mapping[str, Any]) -> bool:
    """s32 C1: the host number is the whole spend only with no inflight and no success-unresolved calls."""

    try:
        return int(cost.get("inflight_calls")) == 0 and int(cost.get("success_unresolved_calls")) == 0
    except (TypeError, ValueError):
        return False


def arm_spend_guard(tools: Any) -> float:
    """s32 C1: research cap + host re-read hook + one read NOW, so a retry sees what earlier attempts spent.

    Codex review-s32e: the sandbox cannot tell a retry from a first attempt, so an UNKNOWN starting balance fails
    closed -- the cap is $0 (no paid call) until a host read succeeds; every refused call re-reads (10 s cooldown)."""

    try:
        tools.spend_cap_usd = RESEARCH_CAP_USD
        tools._phase_cap_usd = RESEARCH_CAP_USD
        tools.spend_refresh = lambda: refresh_spend(tools)
    except Exception:
        pass
    try:
        from . import scout as _scout

        _scout.SPEND_GUARD = getattr(tools, "paid_allowed", None)
        _scout.SPEND_NOTE = getattr(tools, "note_external_spend", None)
        _scout.SPEND_REFRESH = getattr(tools, "maybe_refresh_spend", None)
    except Exception:
        pass
    spend = refresh_spend(tools)
    for _ in range(START_READ_RETRIES):
        if str(LAST_REPORT.get("spend_basis") or "").startswith("host"):
            break
        time.sleep(START_READ_BACKOFF_S)
        spend = refresh_spend(tools)
    if not str(LAST_REPORT.get("spend_basis") or "").startswith("host"):
        LAST_REPORT["spend_basis"] = "unknown_start"
        try:
            tools.spend_cap_usd = 0.0
            tools._unknown_start = True
        except Exception:
            pass
    try:
        tools._last_spend_refresh = time.monotonic()
    except Exception:
        pass
    LAST_REPORT["start_spend_usd"] = round(float(spend), 4)
    LAST_REPORT["caps_usd"] = {"research": RESEARCH_CAP_USD, "total": ICP_SPEND_CAP_USD}
    return spend


def release_holdback(tools: Any) -> None:
    """s32 C1: the paragraph and final re-checks may use the holdback, never more than the total."""

    try:
        tools._phase_cap_usd = ICP_SPEND_CAP_USD
        if not getattr(tools, "_unknown_start", False):
            tools.spend_cap_usd = ICP_SPEND_CAP_USD
    except Exception:
        pass


def draft_limit(limit: int) -> int:
    """How many drafts research may return: the public cap plus the reserve (#314 §5)."""

    return max(1, int(limit)) + max(0, int(RESERVE_DRAFTS))


def completion_plan(spend_usd: float, have: int, left: int) -> dict[str, Any]:
    """The eligibility zone the remaining paid work may aim for (campaign H2).

    ``have`` companies already hold a contact, ``left`` can still get one.  Zone 1: one pair suffices,
    so spend may rise to $0.80 minus the completion reserve.  Zone 2 (bounded rescue): settled spend
    already rules out one-pair eligibility, at least two companies can still complete, and two completions
    fit under $1.60.  Zone 0: no affordable route -- no more paid work; the valid output stays.
    Two required pairs with fewer than two attainable companies is never a plan.
    """

    spend = max(0.0, float(spend_usd or 0.0))
    have, left = max(0, int(have)), max(0, int(left))
    attainable = have + left
    need_new = 0 if have > 0 else 1
    base = max(1, have)
    zone = 0
    if attainable >= 1 and spend + need_new * MIN_CONTACT_USD + COMPLETION_RESERVE_USD <= base * PAIR_ALLOWANCE_USD + 1e-9:
        zone = base
    else:
        for pairs in range(max(2, have + 1), min(attainable, MAX_PAIRS) + 1):
            if spend + (pairs - have) * MIN_CONTACT_USD + COMPLETION_RESERVE_USD <= pairs * PAIR_ALLOWANCE_USD + 1e-9:
                zone = pairs
                break
    cap = round(zone * PAIR_ALLOWANCE_USD, 4)
    return {"zone": zone, "spend_usd": round(spend, 4), "have": int(have), "left": int(left),
            "cap_usd": cap, "contact_cap_usd": round(max(spend, cap - COMPLETION_RESERVE_USD), 4) if zone else round(spend, 4),
            "pairs_required": zone, "proven_infeasible": zone == 0 and attainable >= 1}


def _apply_plan(tools: Any, plan: Mapping[str, Any], *, phase: str) -> None:
    try:
        tools.spend_cap_usd = min(ICP_SPEND_CAP_USD, float(plan["contact_cap_usd"] if phase == "contacts" else plan["cap_usd"]))
    except Exception:
        pass


def attach_contacts(companies: list[dict], icp: dict, tools: Any, *, deadline: float,
                    allow_paid: bool = True, checkpoint_fn: Any = None, planner: Any = None) -> dict[str, Any]:
    """Phase B: one verified contact per company, in place; bounded by the deadline.

    ``allow_paid`` is the phase's ceiling; the LIVE decision is taken again before
    every company from the tools' spend guard (#314 §1), and every attached contact
    is checkpointed at once through ``checkpoint_fn`` (#314 §6) so a kill after the
    first contact keeps that contact.
    """

    notes: dict[str, Any] = {"attempted": 0, "attached": 0, "details": {}}
    for company in companies:
        if company.get("contact"):
            continue
        if time.monotonic() >= deadline - 5.0:
            notes["stopped"] = "deadline"
            break
        if callable(planner):
            planner_ok = planner(companies, company)
        else:
            planner_ok = True
        paid_now = bool(allow_paid) and planner_ok and _paid_allowed(tools)
        notes["attempted"] += 1
        try:
            claim, detail = contacts_module.find_contact(company, icp, tools, candidates=CONTACT_CANDIDATES,
                                                         deadline=deadline, allow_paid=paid_now)
        except Exception as exc:
            claim, detail = None, {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
        notes["details"][str(company.get("company_name") or "?")[:60]] = detail
        if claim:
            company["contact"] = claim
            notes["attached"] += 1
            if callable(checkpoint_fn):
                try:
                    checkpoint_fn()
                except Exception:
                    pass
        if detail.get("stopped") == "budget":
            notes["stopped"] = "budget"
            break
    return notes


def fill_contacts_from_reserve(companies: list[dict], report: Any, icp: dict, tools: Any, *,
                               deadline: float, limit: int, schema: str,
                               checkpoint_fn: Any = None, exclude: Optional[set[str]] = None) -> dict[str, Any]:
    """Replace a company that found no contact with a verified reserve company that has one.

    LAB-LOG #314 §5.  ``report.surplus`` holds every company that passed the FULL
    local verification and lost only to the ``limit`` truncation (each validated
    alone).  Under contacts_v1 a row without a contact scores 0, so a reserve
    company WITH a contact is strictly better in that slot.  Rules: the reserve
    company must not share a name or host with anything emitted; the emitted list
    never exceeds ``limit``; the swapped-in row goes LAST (model order is scoring
    order -- backfill_empty_slots explains why); a reserve company that finds no
    contact either is left out; the contactless company stays when nothing
    replaces it.  Bounded by the deadline and the live spend guard.
    """

    notes: dict[str, Any] = {"missing": 0, "tried": 0, "swapped": []}
    missing = [c for c in companies if not c.get("contact")]
    notes["missing"] = len(missing)
    pool = [c for c in list(getattr(report, "surplus", None) or []) if isinstance(c, dict)]
    if not missing or not pool:
        return notes
    keys = {sm.company_name_key(c.get("company_name")) for c in companies}
    hosts = {sm.registrable_host(str(c.get("company_website") or "")) for c in companies}
    banned = set(exclude or ())
    for candidate in pool:
        if not missing:
            break
        if time.monotonic() >= deadline - 10.0:
            notes["stopped"] = "deadline"
            break
        if not _paid_allowed(tools):
            notes["stopped"] = "budget"
            break
        key = sm.company_name_key(candidate.get("company_name"))
        host = sm.registrable_host(str(candidate.get("company_website") or ""))
        if key in keys or (host and host in hosts) or (banned & identity_keys(candidate)):
            continue
        notes["tried"] += 1
        original = candidate
        try:
            claim, detail = contacts_module.find_contact(candidate, icp, tools, candidates=CONTACT_CANDIDATES,
                                                         deadline=deadline, allow_paid=True)
        except Exception as exc:
            claim, detail = None, {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
        if not claim:
            if detail.get("stopped") == "budget":
                notes["stopped"] = "budget"
                break
            continue
        candidate = dict(candidate)
        candidate["contact"] = claim
        dropped = missing.pop(0)
        trial = [c for c in companies if c is not dropped] + [candidate]
        try:
            sm.validate_output([strip_internal(c) for c in trial], max_companies=max(1, int(limit)),
                               schema_version=schema)
        except Exception as exc:
            notes.setdefault("refused", []).append(
                f"{str(candidate.get('company_name') or '?')[:40]}: {str(exc)[:60]}")
            missing.insert(0, dropped)
            continue
        companies[:] = trial
        keys.add(key)
        hosts.add(host)
        try:
            report.surplus = [c for c in report.surplus if c is not original]
        except Exception:
            pass
        notes["swapped"].append({"out": str(dropped.get("company_name") or "")[:60],
                                 "in": str(candidate.get("company_name") or "")[:60]})
        if callable(checkpoint_fn):
            try:
                checkpoint_fn()
            except Exception:
                pass
    return notes


def run_icp(icp: dict) -> list[dict]:
    """Return up to LAB_ARENA_COMPANY_LIMIT verified companies, best first."""

    started = time.monotonic()
    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    limit = _company_limit()
    LAST_REPORT.clear()
    LAST_REPORT.update({"icp_id": icp.get("icp_id"), "limit": limit})

    socket_path = str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip()
    if not socket_path and not os.environ.get("ARENA_TOOLS_FACTORY"):
        _log("no LAB_ARENA_WORKER_SOCKET; returning an empty result")
        LAST_REPORT["error"] = "no worker socket"
        return []

    tools_factory = _tools_factory()
    tools = tools_factory()
    arm_spend_guard(tools)
    try:
        from . import role_judge as _role_judge

        tools.role_classifier = lambda title, targets, seniority, duties: _role_judge.classify(
            title, targets, seniority, duties, http_client_factory=_http_client_factory())
    except Exception:
        pass
    diagnostics.reset()
    agent_exc: BaseException | None = None
    companies: list[dict[str, Any]] = []
    schema = announced_schema(icp)
    v5 = sm.v5_family(schema)
    contacts_on = (v5 and CONTACTS_ENABLED and str(icp.get("contact_policy") or "") == "contacts_v1"
                   and contacts_module is not None)
    paragraph_on = v5 and PARAGRAPH_ENABLED
    clock = plan_clock()
    horizon = clock["horizon"]
    short_clock = clock["short"]
    short_deadline = started + short_clock - SAFETY_SECONDS
    phase_deadline = (started + horizon - SAFETY_SECONDS) if clock["long"] else short_deadline
    LAST_REPORT.update({"schema": schema, "contacts": contacts_on, "paragraph": paragraph_on,
                        "short_clock": short_clock, "horizon": horizon,
                        "clock_source": clock["source"], "announced_clock": clock["announced"],
                        "long_path": clock["long"]})
    try:
        share = AGENT_SHARE_V5 if (contacts_on or paragraph_on) else AGENT_SHARE
        budget = short_clock - SAFETY_SECONDS - (time.monotonic() - started)
        agent_timeout = max(30.0, budget * share)
        if clock["long"]:
            agent_timeout = max(agent_timeout, min(AGENT_LONG_SECONDS, (horizon - SAFETY_SECONDS) * share))
        LAST_REPORT["agent_timeout"] = round(agent_timeout, 1)
        drafts: list[dict[str, Any]] = []
        agent_module = _agent_module()
        tools.deadline = time.monotonic() + max(20.0, agent_timeout - 35.0)
        LAST_REPORT["draft_limit"] = draft_limit(limit)
        mode = str(_STRATEGY.get("research_mode") or "agent")
        LAST_REPORT["research_mode"] = mode
        if mode in ("scout", "hybrid"):
            try:
                from . import scout as scout_module
            except ImportError:
                import importlib

                scout_module = importlib.import_module(f"{os.path.basename(_HERE)}.scout")
            try:
                drafts = scout_module.run_scout(icp, tools, limit=draft_limit(limit), run_timeout=agent_timeout,
                                                http_client_factory=_http_client_factory())
            except Exception as exc:
                _log(f"scout failed: {type(exc).__name__}: {str(exc)[:200]}")
                LAST_REPORT["scout_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                agent_exc = exc
            LAST_REPORT["scout"] = dict(getattr(scout_module, "LAST", {}) or {})
        if mode == "agent" or (mode == "hybrid" and len(drafts) < 2 and time.monotonic() < tools.deadline - 60.0):
            try:
                found = agent_module.run_agent(icp, tools, limit=draft_limit(limit), run_timeout=agent_timeout,
                                               http_client_factory=_http_client_factory())
                seen_keys = {sm_keys for d in drafts for sm_keys in identity_keys(d)}
                drafts = drafts + [d for d in found if not (identity_keys(d) & seen_keys)]
            except Exception as exc:
                _log(f"agent failed: {type(exc).__name__}: {str(exc)[:200]}")
                LAST_REPORT["agent_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                agent_exc = exc
        LAST_REPORT["drafts"] = len(drafts)
        LAST_REPORT["agent_seconds"] = round(time.monotonic() - started, 1)
        try:
            meter = agent_module.LAST_USAGE.get("platform_meter_usd")
            LAST_REPORT["platform_meter_usd"] = meter
            if isinstance(meter, (int, float)) and not isinstance(meter, bool):
                _note_spend(tools, float(meter))
        except Exception:
            pass
        if not drafts:
            LAST_REPORT["funnel"] = _funnel(tools, drafted=0, verify_out=None, reverify_out=None,
                                            returned=0, terminal="no_drafts")
            _write_empty_snapshot(icp, tools, getattr(agent_module, "LAST_USAGE", None))
            return _no_companies(agent_exc, tools)
        tools.deadline = max(verification_deadline(started), phase_deadline)
        companies, report = verify_companies(drafts, icp=icp, tools=tools, limit=limit, schema=schema)
        LAST_REPORT["verify"] = report.as_dict()
        checkpoint(companies, schema=schema, limit=limit)
        allow_paid = _paid_allowed(tools)
        if companies:
            refresh_spend(tools)
            allow_paid = _paid_allowed(tools)

        def write_checkpoint() -> bool:
            return checkpoint(companies, schema=schema, limit=limit)

        rejected: set[str] = set()

        plans: list[dict[str, Any]] = []

        def planner(rows: list[dict], current: Any = None) -> bool:
            """campaign H2: re-plan from the host-confirmed spend before each paid contact attempt."""
            spend = refresh_spend(tools)
            have = sum(1 for c in rows if c.get("contact"))
            left = sum(1 for c in rows if not c.get("contact"))
            plan = completion_plan(spend, have, left)
            plan["before"] = str((current or {}).get("company_name") or "")[:60] if isinstance(current, dict) else ""
            plans.append(plan)
            _apply_plan(tools, plan, phase="contacts")
            LAST_REPORT["completion_plans"] = plans
            return plan["zone"] > 0

        if companies and contacts_on:
            saved_timeout = tools.timeout
            tools.timeout = min(saved_timeout, 35.0)
            tools.deadline = phase_deadline
            try:
                allow_paid = planner(companies) and allow_paid
                LAST_REPORT["contacts_phase"] = attach_contacts(companies, icp, tools, deadline=phase_deadline,
                                                                allow_paid=allow_paid, checkpoint_fn=write_checkpoint,
                                                                planner=planner)
                if any(not c.get("contact") for c in companies) and time.monotonic() < phase_deadline - 20.0:
                    LAST_REPORT["reserve_phase"] = fill_contacts_from_reserve(
                        companies, report, icp, tools, deadline=phase_deadline, limit=limit, schema=schema,
                        checkpoint_fn=write_checkpoint, exclude=rejected)
            finally:
                tools.timeout = saved_timeout
            checkpoint(companies, schema=schema, limit=limit)
            spend_now = refresh_spend(tools)
            final_plan = completion_plan(spend_now, sum(1 for c in companies if c.get("contact")), 0)
            final_plan["before"] = "paragraph"
            plans.append(final_plan)
            LAST_REPORT["completion_plans"] = plans
            _apply_plan(tools, final_plan, phase="completion")
            allow_paid = _paid_allowed(tools)
            LAST_REPORT["completion_state"] = [
                {"company": str(c.get("company_name") or "")[:60], "fit_intent": "verified_local",
                 "contact": "complete" if c.get("contact") else "missing",
                 "state": "awaiting_paragraph" if c.get("contact") else "incomplete_no_contact"}
                for c in companies]
        if companies and ATS_BONUS and time.monotonic() < phase_deadline - 60.0:
            try:
                try:
                    from . import ats_bonus as _ats
                except ImportError:
                    _ats = importlib.import_module(f"{os.path.basename(_HERE)}.ats_bonus")
                try:
                    from . import scout as _scout_llm
                except ImportError:
                    _scout_llm = importlib.import_module(f"{os.path.basename(_HERE)}.scout")
                _factory = _http_client_factory()

                def _pick_llm(prompt: str, deadline: float) -> Any:
                    return _scout_llm.llm_json(prompt, http_client_factory=_factory, max_tokens=200,
                                               model=ATS_PICK_MODEL, deadline=deadline)

                LAST_REPORT["ats_bonus"] = _ats.attach_hiring_signals(
                    companies, icp, report.evidence, sm.company_name_key, today=sm.evaluation_date(),
                    deadline=min(phase_deadline - 45.0, time.monotonic() + ATS_BONUS_SECONDS), llm_json=_pick_llm)
                for c in companies:
                    key = sm.company_name_key(str(c.get("company_name") or ""))
                    if paragraph_on and key in report.evidence:
                        c["intent_details"] = details_module.fallback_paragraph(
                            company_name=str(c.get("company_name") or ""), icp=icp, signals=report.evidence[key])
                checkpoint(companies, schema=schema, limit=limit)
            except Exception as exc:
                LAST_REPORT["ats_bonus"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        release_holdback(tools)
        if companies and paragraph_on:
            remaining_short = phase_deadline - time.monotonic()
            if not _paid_allowed(tools, PARAGRAPH_MAX_USD):
                LAST_REPORT["paragraph_phase"] = {"skipped": "spend cap; deterministic paragraphs stand"}
            elif remaining_short > 30.0:
                _agent_for_model = _agent_module()
                model_name = str(_STRATEGY.get("paragraph_model") or "").strip() or \
                    os.environ.get("ARENA_MODEL", str(_STRATEGY.get("model") or "")).strip() or _agent_for_model.DEFAULT_MODEL
                reserve = [c for c in list(getattr(report, "surplus", None) or [])[:RESERVE_CHECKED] if isinstance(c, dict)]
                LAST_REPORT["paragraph_phase"] = details_module.write_paragraphs(
                    companies + reserve, icp, evidence=report.evidence, model_name=model_name,
                    timeout=min(60.0, remaining_short - 15.0), http_client_factory=_http_client_factory())
                billed = (LAST_REPORT.get("paragraph_phase") or {}).get("cost_usd")
                _note_spend(tools, max(PARAGRAPH_MAX_USD, float(billed) if isinstance(billed, (int, float)) else 0.0))
                checkpoint(companies, schema=schema, limit=limit)
                refresh_spend(tools)
            else:
                LAST_REPORT["paragraph_phase"] = {"skipped": "no time; deterministic paragraphs stand"}
        try:
            counts = getattr(report, "counts", None)
            if callable(counts):
                LAST_REPORT["verify_counts"] = counts()
        except Exception:
            pass
        verify_out = len(companies)
        remaining = phase_deadline - time.monotonic()
        pool = list(companies)
        pool_keys = {k for c in pool for k in identity_keys(c)}
        for extra in list(getattr(report, "surplus", None) or [])[:RESERVE_CHECKED]:
            if isinstance(extra, dict) and not (identity_keys(extra) & pool_keys):
                pool.append(extra)
                pool_keys |= identity_keys(extra)
        reverify_est = REVERIFY_EST_USD * len(pool[:REVERIFY_MAX_COMPANIES])
        if companies and not _paid_allowed(tools, reverify_est):
            LAST_REPORT["reverify_skipped"] = {"reason": "spend cap", "estimate_usd": round(reverify_est, 4)}
        elif companies and remaining > 40.0 and str(os.environ.get("ARENA_REVERIFY", "1")).strip() != "0":
            try:
                try:
                    from . import reverify as reverify_module
                except ImportError:
                    reverify_module = importlib.import_module(f"{os.path.basename(_HERE)}.reverify")
                head = pool[:REVERIFY_MAX_COMPANIES]
                tail = companies[REVERIFY_MAX_COMPANIES:]
                checked, notes = reverify_module.rerank(head, icp, http_client_factory=_http_client_factory(),
                                                        timeout=min(reverify_module.TIMEOUT_S, remaining - 12.0))
                _note_spend(tools, REVERIFY_EST_USD * len(head))
                survivors = {id(c) for c in checked}
                for evicted in (c for c in head if id(c) not in survivors):
                    rejected |= identity_keys(evicted)
                if rejected:
                    notes["rejected_identities"] = sorted(rejected)
                companies = (checked + tail)[:limit]
                try:
                    companies = sm.validate_output(companies, max_companies=limit, schema_version=schema)
                except Exception as exc:
                    notes["pool_rejected"] = type(exc).__name__
                    companies = [c for c in checked if any(c is x for x in pool[:verify_out])][:limit] or pool[:verify_out]
                notes["checked"] = len(head)
                notes["unchecked_tail"] = len(tail)
                companies, added = backfill_empty_slots(companies, report, limit=limit, schema=schema,
                                                        exclude=rejected)
                if added:
                    notes["backfilled"] = added
                    if contacts_on and time.monotonic() < phase_deadline - 20.0:
                        attach_contacts([c for c in companies if not c.get("contact")], icp, tools,
                                        deadline=phase_deadline, allow_paid=_paid_allowed(tools),
                                        checkpoint_fn=write_checkpoint)
                LAST_REPORT["reverify"] = notes
                checkpoint(companies, schema=schema, limit=limit)
                refresh_spend(tools)
            except Exception as exc:
                LAST_REPORT["reverify"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        if clock["long"] and companies:
            long_deadline = phase_deadline
            tools.deadline = long_deadline
            missing = [c for c in companies if contacts_on and not c.get("contact")]
            if missing and _paid_allowed(tools) and time.monotonic() < long_deadline - 60.0:
                LAST_REPORT["contacts_phase_d"] = attach_contacts(missing, icp, tools, deadline=long_deadline,
                                                                  allow_paid=True, checkpoint_fn=write_checkpoint)
                checkpoint(companies, schema=schema, limit=limit)
        if companies and time.monotonic() < phase_deadline - 30.0:
            companies = recheck_ashby_postings(companies, report, tools, deadline=phase_deadline - 15.0, limit=limit,
                                               schema=schema, exclude=rejected)
        LAST_REPORT["calls"] = tools.calls
        try:
            LAST_REPORT["spend_usd"] = round(float(tools.spend_usd()), 4)
            LAST_REPORT["spend_refusals"] = int(getattr(tools, "spend_refusals", 0) or 0)
        except Exception:
            pass
        try:
            LAST_REPORT["calls_by_tool"] = dict(getattr(tools, "calls_by_tool", {}) or {})
        except Exception:
            pass
        LAST_REPORT["seconds"] = round(time.monotonic() - started, 1)
        LAST_REPORT["diagnostics"] = diagnostics.snapshot()
        LAST_REPORT["funnel"] = _funnel(
            tools, drafted=len(drafts), verify_out=verify_out,
            reverify_out=len(companies) if LAST_REPORT.get("reverify") else None,
            returned=len(companies),
            terminal="returned" if companies else "empty_after_verify", companies=companies)
        if v5 and contacts_on:
            LAST_REPORT["contacts_attached"] = sum(1 for c in companies if c.get("contact"))
        return [strip_internal(c) for c in companies]
    except Exception as exc:
        if exc is agent_exc:
            raise
        _log(f"harness failed: {type(exc).__name__}: {str(exc)[:200]}")
        LAST_REPORT["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if companies:
            return [strip_internal(c) for c in companies]
        return _no_companies(exc, tools)
    finally:
        try:
            LAST_REPORT["diagnostics"] = diagnostics.snapshot()
        except Exception:
            pass
        try:
            LAST_REPORT.setdefault("funnel", _funnel(
                tools, drafted=int(LAST_REPORT.get("drafts") or 0), verify_out=None,
                reverify_out=None, returned=len(companies),
                terminal=str(LAST_REPORT.get("no_output") or LAST_REPORT.get("error") or "unknown")[:60],
                companies=companies))
        except Exception:
            pass
        _emit_report()
        try:
            tools.close()
        except Exception:
            pass


def _tools_factory():
    """Tests inject a fake tool client through ARENA_TOOLS_FACTORY='module:callable'."""

    spec = str(os.environ.get("ARENA_TOOLS_FACTORY") or "").strip()
    if not spec:
        return lambda: ArenaTools(timeout=float(os.environ.get("ARENA_TOOL_TIMEOUT_SECONDS", "60") or 60))
    module_name, _, attr = spec.partition(":")
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def _http_client_factory():
    spec = str(os.environ.get("ARENA_HTTP_CLIENT_FACTORY") or "").strip()
    if not spec:
        return None
    module_name, _, attr = spec.partition(":")
    import importlib

    return getattr(importlib.import_module(module_name), attr)


class _NoAgent:
    """Loop s22: the scout bundle ships without agent.py/context.py (91 KB of full-source review headroom; research_mode
    'scout' never runs the agent loop).  What the harness reads from the agent module -- an empty usage record and the
    default model name -- comes from here; asking for the agent loop itself fails like an agent error."""

    DEFAULT_MODEL = "openai/gpt-5.5"
    LAST_USAGE: dict = {}

    @staticmethod
    def run_agent(*args: Any, **kwargs: Any) -> list:
        raise RuntimeError("agent mode is not bundled in this build")


def _agent_module() -> Any:
    try:
        from . import agent as module
        return module
    except ImportError:
        try:
            import importlib

            return importlib.import_module(f"{os.path.basename(_HERE)}.agent")
        except ImportError:
            return _NoAgent


def get_last_usage() -> dict:
    agent_module = _agent_module()
    usage = dict(agent_module.LAST_USAGE)
    usage["report"] = dict(LAST_REPORT)
    return usage


__all__ = ["run_icp", "get_last_usage", "backfill_empty_slots", "verification_deadline", "checkpoint",
           "read_quota", "attach_contacts", "confirmed_spend_usd", "refresh_spend", "draft_limit",
           "arm_spend_guard", "release_holdback", "snapshot_settled",
           "fill_contacts_from_reserve", "identity_keys"]
