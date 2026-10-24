
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
    from . import yieldmap as _yieldmap
except ImportError:
    try:
        _yieldmap = importlib.import_module(f"{os.path.basename(_HERE)}.yieldmap")
    except Exception:
        _yieldmap = None
except Exception:
    _yieldmap = None

try:
    from . import judge as _judge
except ImportError:
    try:
        _judge = importlib.import_module(f"{os.path.basename(_HERE)}.judge")
    except Exception:
        _judge = None
except Exception:
    _judge = None

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
FLAT_POLICY_PAIR_CAP_MICROUSD = 4_000_000
_LAST_SNAPSHOT: Optional[dict] = None
FLAT_ICP_CAP_USD = _strategy_number("flat_icp_cap_usd", 3.5, 0.0, 3.9)
FLAT_HOLDBACK_USD = _strategy_number("flat_holdback_usd", 0.3, 0.0, 1.0)
FLAT_CALL_BUDGET = int(_strategy_number("flat_call_budget", 600, 50, 2000))
FLAT_FORCE = str(_STRATEGY.get("cost_policy_force") or "").strip().lower() == "flat"
FLAT_EVENT_COMPANIES = int(_strategy_number("flat_event_companies", 30, 0, 60))
FLAT_RESERVE_DRAFTS = int(_strategy_number("flat_reserve_drafts", 15, 0, 25))
FIT_FREE_FROM = str(_STRATEGY.get("fit_free_from") or "20261021").strip()
FIT_FREE_FORCE = _strategy_number("fit_free_force", 0, 0, 1) >= 1
FIT_FREE = False
FILL_TO_LIMIT = _strategy_number("fill_to_limit", 1, 0, 1) >= 1
DISCOVERY_PASSES = int(_strategy_number("discovery_passes", 3, 1, 6))
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
ATS_PICK_MODEL = str(_STRATEGY.get("ats_pick_model") or "google/gemini-3.5-flash-lite")
ATS_BONUS_SECONDS = _strategy_number("ats_bonus_seconds", 150.0, 10.0, 400.0)
SKIP_PRIVATE_EQUITY = _strategy_number("skip_private_equity", 0, 0, 1) >= 1
RESERVE_DRAFTS = int(_strategy_number("reserve_drafts", 0, 0, 10))
REVERIFY_MAX_COMPANIES = 8
POOL_CHECKED = 0
DEPTH_RANK = False
FLAT_POOL_CHECKED = int(_strategy_number("flat_pool_checked", 20, 3, 30))
FLAT_AGENT_SHARE = _strategy_number("flat_agent_share", 0.80, 0.3, 0.88)
REVERIFY_ASKS = 1
FLAT_CANDIDATES = int(_strategy_number("flat_candidates", 40, 12, 40))
FLAT_EXTRACT_ROWS = int(_strategy_number("flat_extract_rows", 400, 60, 800))
FLAT_REVERIFY_ASKS = int(_strategy_number("flat_reverify_asks", 3, 1, 5))
FLAT_AGENT_LONG_SECONDS = _strategy_number("flat_agent_long_seconds", 2900.0, 60.0, 3600.0)
JUDGE_MIRROR = _strategy_number("judge_mirror", 0, 0, 1) >= 1
RESERVE_CHECKED = 3
LAST_REPORT: dict[str, Any] = {}


def _company_limit() -> int:
    raw = str(os.environ.get("LAB_ARENA_COMPANY_LIMIT") or "5").strip()
    try:
        return max(1, min(5, int(raw)))
    except ValueError:
        return 5


def announced_wall_clock() -> Optional[float]:

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

    return started + (WALL_CLOCK_SECONDS - SAFETY_SECONDS)


def identity_keys(company: Mapping[str, Any]) -> set[str]:

    keys = {sm.company_name_key(company.get("company_name"))}
    host = sm.registrable_host(str(company.get("company_website") or ""))
    if host:
        keys.add(host)
    return {k for k in keys if k}


def backfill_empty_slots(companies: list[dict], report: Any, *, limit: int,
                         schema: str = sm.SCHEMA_V1, exclude: Optional[set[str]] = None) -> tuple[list[dict], list[str]]:

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


_STAGE_PROOF_STAGES = ("series a", "series b", "series c", "private equity")


def fill_to_limit(companies: list[dict], report: Any, icp: Mapping[str, Any], *, limit: int,
                  schema: str = sm.SCHEMA_V1, exclude: Optional[set[str]] = None,
                  verdicts: Optional[Mapping[str, str]] = None) -> tuple[list[dict], list[str]]:

    limit = max(1, int(limit))
    if len(companies) >= limit:
        return companies, []
    pool = [c for c in list(getattr(report, "surplus", None) or []) if isinstance(c, dict)]
    if not pool:
        return companies, []
    try:
        signal_round = _sibling_module("precision")._signal_round
    except Exception:
        signal_round = None
    stage = str(icp.get("company_stage") or "")
    needs_stage = stage.strip().lower().startswith(_STAGE_PROOF_STAGES)
    taken = {k for c in companies for k in identity_keys(c)}
    banned = set(exclude or ())
    verdicts = dict(verdicts or {})
    ranked: list[tuple[tuple, dict]] = []
    for order, row in enumerate(pool):
        keys = identity_keys(row)
        if not keys or keys & taken or keys & banned:
            continue
        primary = [sig for sig in row.get("intent_signals") or []
                   if isinstance(sig, Mapping) and sig.get("matched_icp_signal") in (0, None) and sig.get("url")]
        if not primary or not any(str(sig.get("date") or "").strip() for sig in primary):
            continue
        verdict = str(verdicts.get(sm.company_name_key(row.get("company_name")), ""))
        if verdict == "mismatch":
            continue
        proofs = len(row.get("company_stage_evidence") or [])
        if needs_stage and not proofs and not (signal_round and signal_round(row, stage)):
            continue
        attribute = row.get("required_attribute") if isinstance(row.get("required_attribute"), Mapping) else {}
        quote = len(str(attribute.get("evidence_quote") or ""))
        try:
            urls = int(evidence_depth(row)[2])
        except Exception:
            urls = 0
        tier = {"match": 0, "unavailable": 1}.get(verdict, 2)
        ranked.append(((-min(proofs, 3), tier, not 81 <= quote <= 160, -urls, order), row))
    ranked.sort(key=lambda item: item[0])
    out = list(companies)
    added: list[str] = []
    for _, row in ranked:
        if len(out) >= limit:
            break
        keys = identity_keys(row)
        if keys & taken:
            continue
        taken |= keys
        out.append(row)
        added.append(str(row.get("company_name") or ""))
    if not added:
        return companies, []
    try:
        out = sm.validate_output(out, max_companies=limit, schema_version=schema)
    except Exception as exc:
        _log("fill rejected by the output contract (%s): emitting the list unchanged" % type(exc).__name__)
        return companies, []
    return out, added


def recheck_ashby_postings(companies: list[dict], report: Any, tools: Any, *, deadline: float, limit: int,
                           schema: str, exclude: set[str]) -> list[dict]:

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

    if LAST_REPORT.get("_emitted"):
        return
    LAST_REPORT["_emitted"] = True
    try:
        _log(json.dumps({k: v for k, v in LAST_REPORT.items()
                         if k not in ("verify", "_emitted")}))
    except Exception:
        pass


EMIT_GATE = bool(_strategy_number("emit_gate", 0, 0, 1))
_PROOF_MATTERS = ("series c+", "series b", "series a", "public", "seed")


def drop_unprovable(companies: list, icp: Mapping[str, Any], report: Any = None) -> list:

    if not EMIT_GATE or not companies:
        return companies
    stage = str((icp or {}).get("company_stage") or "").strip().lower()

    def carries_proof(company: Mapping[str, Any]) -> bool:
        if company.get("_attribute_unproven"):
            return False
        if stage in _PROOF_MATTERS:
            return bool(company.get("company_stage_evidence"))
        return True

    proven = [c for c in companies if carries_proof(c)]
    if proven and len(proven) != len(companies):
        companies = _report_drop(companies, proven, report,
                                 "no proof the judge can read, beside a company that has it")
    return drop_weak_attribute(companies, icp, report)


_REVERIFY_MATCHED: set = set()
_ATTRIBUTE_RANK_FLOOR = 3
_NO_ATTRIBUTE_QUOTE = -1


def attribute_strength(company: Mapping[str, Any]) -> Optional[int]:

    attribute = company.get("required_attribute")
    if not isinstance(attribute, Mapping):
        return None
    quote = str(attribute.get("evidence_quote") or "").strip()
    if not quote:
        return _NO_ATTRIBUTE_QUOTE
    try:
        from . import scout as _scout
    except Exception:
        try:
            _scout = importlib.import_module(f"{os.path.basename(_HERE)}.scout")
        except Exception:
            return None
    try:
        key = sm.company_name_key(company.get("company_name"))
        return int(_scout.attribute_sentence_rank(attribute.get("evidence_url"), quote, key))
    except Exception:
        return None


GATE_REFILL = _strategy_number("gate_refill", 1, 0, 1) >= 1
GATE_REFILL_FLOOR = int(_strategy_number("gate_refill_floor", 5, 0, 9))


class _Pool:
    def __init__(self, rows: list) -> None:
        self.surplus = rows


def refill_after_gates(companies: list, icp: Mapping[str, Any], report: Any, *, emptied: int, limit: int,
                       schema: str, exclude: Optional[set] = None) -> list:

    if not GATE_REFILL or emptied <= 0 or report is None or len(companies) >= limit:
        return companies
    stage = str((icp or {}).get("company_stage") or "").strip().lower()
    asks_attribute = bool(str((icp or {}).get("required_attribute") or "").strip())
    pool, skipped = [], {}
    for row in [c for c in list(getattr(report, "surplus", None) or []) if isinstance(c, dict)]:
        name = str(row.get("company_name") or "")
        if stage in _PROOF_MATTERS and not row.get("company_stage_evidence"):
            skipped[name] = "no stage evidence"
            continue
        if asks_attribute and sm.company_name_key(name) not in _REVERIFY_MATCHED:
            rank = attribute_strength(row)
            if rank is None or rank < GATE_REFILL_FLOOR:
                skipped[name] = "attribute rank %s" % rank
                continue
        pool.append(row)
    try:
        out, added = backfill_empty_slots(companies, _Pool(pool), limit=min(int(limit), len(companies) + int(emptied)),
                                          schema=schema, exclude=exclude)
    except Exception as exc:
        LAST_REPORT["gate_refill"] = {"error": type(exc).__name__}
        return companies
    LAST_REPORT["gate_refill"] = {"emptied": int(emptied), "added": added, "skipped": dict(list(skipped.items())[:10])}
    return out


def drop_weak_attribute(companies: list, icp: Mapping[str, Any], report: Any = None) -> list:

    if not _strategy_number("emit_gate_strength", 1, 0, 1) >= 1:
        return companies
    if not companies or not str((icp or {}).get("required_attribute") or "").strip():
        return companies
    ranks = {id(c): attribute_strength(c) for c in companies}
    if any(rank is None for rank in ranks.values()):
        return companies
    confirmed = {c_id for c_id, c in ((id(c), c) for c in companies)
                 if sm.company_name_key(c.get("company_name")) in _REVERIFY_MATCHED}
    strong = [c for c in companies if ranks[id(c)] >= _ATTRIBUTE_RANK_FLOOR or id(c) in confirmed]
    weak = [c for c in companies if 0 <= ranks[id(c)] < _ATTRIBUTE_RANK_FLOOR and id(c) not in confirmed]
    if not strong or not weak:
        return companies
    LAST_REPORT["emit_gate_attribute_ranks"] = {str(c.get("company_name")): ranks[id(c)] for c in companies}
    keep = [c for c in companies if c not in weak]
    return _report_drop(companies, keep, report,
                        "the attribute quote reads like a menu, beside a row whose reads like a proof")


def _report_drop(companies: list, keep: list, report: Any, reason: str) -> list:
    kept = {id(c) for c in keep}
    dropped = [str(c.get("company_name")) for c in companies if id(c) not in kept]
    LAST_REPORT.setdefault("emit_gate_dropped", []).extend(dropped)
    _log("emission gate drops %s: %s" % (", ".join(dropped) or "nothing", reason))
    if report is not None:
        for name in dropped:
            try:
                report.dropped_companies.append((name, reason))
            except Exception:
                break
    return list(keep)


_JUDGE_ANNOUNCED = []


def _announce_judge_gates() -> None:

    if _JUDGE_ANNOUNCED:
        return
    _JUDGE_ANNOUNCED.append(True)
    gates = judge_gates()
    if gates is None:
        _log("the scorer source is not reachable in this image: every judge verdict stays unknown")
        return
    try:
        verdicts = gates.self_test()
        counts = {key: sum(1 for value in verdicts.values() if value == key)
                  for key in ("pass", "fail", "unknown")}
        _log("scorer helpers reachable: %s %s" % (json.dumps(counts), json.dumps(verdicts, sort_keys=True)))
        LAST_REPORT["judge_self_test"] = counts
    except (Exception, SystemExit) as exc:
        _log("scorer helper self-test failed: %s" % type(exc).__name__)


def judge_gates() -> Any:

    if _judge is None or _strategy_number("judge_gates", 1, 0, 1) < 1:
        return None
    try:
        return _judge if _judge.available() else None
    except Exception:
        return None


def judge_stage_verdicts(company: Mapping[str, Any]) -> list[Optional[bool]]:

    gates = judge_gates()
    if gates is None:
        return []
    name = str(company.get("company_name") or "")
    website = str(company.get("company_website") or "")
    stage = str(company.get("company_stage") or "")
    out: list[Optional[bool]] = []
    for item in company.get("company_stage_evidence") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            out.append(gates.stage_verdict(stage, item.get("quote"), url=item.get("url"), name=name,
                                           website=website))
        except Exception:
            out.append(None)
    return out


def judge_intent_urls(companies: list, tools: Any) -> dict:

    gates = judge_gates()
    pages = getattr(tools, "pages", None)
    if gates is None or not isinstance(pages, Mapping) or not companies:
        return {}
    notes: dict[str, str] = {}
    for company in companies:
        signals = [s for s in (company.get("intent_signals") or []) if isinstance(s, Mapping)]
        if len(signals) < 2:
            continue
        paragraph = sm.normalize_text(company.get("intent_details") or "")
        keep = []
        for signal in signals:
            url = str(signal.get("url") or "")
            page = pages.get(url)
            text = " ".join(str(getattr(page, "text", "") or "").split()) if page is not None else ""
            if not text:
                keep.append(signal)
                continue
            verdict = "" if len(text) >= 300 else "body_too_short"
            verdict = verdict or (gates.antibot(text) and "anti_bot_marker") or ""
            primary = signal.get("matched_icp_signal") == 0
            described = bool(paragraph) and sm.normalize_text(signal.get("description") or "")[:60] in paragraph
            if verdict and not primary and not described and len(keep) + 1 < len(signals):
                notes["%s|%s" % (company.get("company_name"), url[:60])] = verdict
                continue
            keep.append(signal)
        if keep and len(keep) != len(signals):
            company["intent_signals"] = keep
    if notes:
        LAST_REPORT["judge_intent_urls"] = notes
        _log("intent URLs the scorer's fetch classifier would reject: %s" % json.dumps(notes)[:300])
    return notes


def judge_preflight(companies: list, *, schema: str, report: Any = None, tools: Any = None) -> list:

    gates = judge_gates()
    if gates is None or not companies:
        return companies
    if tools is not None:
        try:
            judge_intent_urls(companies, tools)
        except Exception:
            pass
    try:
        LAST_REPORT["judge_gates"] = gates.status()
        LAST_REPORT["judge_stage_items"] = {str(c.get("company_name")): judge_stage_verdicts(c)
                                            for c in companies}
    except Exception:
        pass
    rows = [dict(c) for c in companies]
    reasons = {}
    for index, row in enumerate(rows):
        try:
            reason = gates.row_reason(row)
        except Exception:
            reason = None
        if reason:
            reasons[str(row.get("company_name") or index)] = reason
    if reasons:
        LAST_REPORT["judge_row_reasons"] = reasons
        _log("the scorer's own company model complains about %d row(s): %s"
             % (len(reasons), json.dumps(reasons)[:400]))
    try:
        document = gates.document_reason(rows, schema=schema)
    except Exception:
        document = None
    if not document:
        LAST_REPORT["judge_document"] = "accepted" if document == "" else "unknown"
        return companies
    LAST_REPORT["judge_document"] = document
    _log("the host's own output validator REJECTS this document (%s): finding the rows it cannot read" % document[:160])
    kept, removed = [], []
    for company, row in zip(companies, rows):
        if gates.document_reason([row], schema=schema):
            removed.append(str(row.get("company_name") or ""))
            continue
        kept.append(company)
    if removed and kept and not gates.document_reason([dict(c) for c in kept], schema=schema):
        LAST_REPORT["judge_document_dropped"] = removed
        _log("dropped %s; the rest of the document validates" % ", ".join(removed))
        if report is not None:
            for name in removed:
                try:
                    report.dropped_companies.append((name, "the host's output validator cannot read this row"))
                except Exception:
                    break
        return kept
    return companies


def checkpoint(companies: list[dict], *, schema: str, limit: int) -> bool:

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
    global _LAST_SNAPSHOT
    _LAST_SNAPSHOT = snapshot if isinstance(snapshot, dict) else _LAST_SNAPSHOT
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

    check = getattr(tools, "paid_allowed", None)
    if not callable(check):
        return True
    try:
        return bool(check(estimate_usd))
    except Exception:
        return True


def _note_spend(tools: Any, usd: float) -> None:

    note = getattr(tools, "note_external_spend", None)
    if callable(note):
        try:
            note(usd)
        except Exception:
            pass


def refresh_spend(tools: Any) -> float:

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

    try:
        return int(cost.get("inflight_calls")) == 0 and int(cost.get("success_unresolved_calls")) == 0
    except (TypeError, ValueError):
        return False


def arm_spend_guard(tools: Any) -> float:

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
    apply_cost_policy(tools, _LAST_SNAPSHOT)
    LAST_REPORT["caps_usd"] = {"research": RESEARCH_CAP_USD, "total": ICP_SPEND_CAP_USD}
    return spend


def flat_policy(snapshot: Any) -> bool:

    if FLAT_FORCE:
        return True
    cost = snapshot.get("sourcing_cost") if isinstance(snapshot, dict) else None
    try:
        return int((cost or {}).get("per_qualified_pair_cap_microusd")) >= FLAT_POLICY_PAIR_CAP_MICROUSD
    except (TypeError, ValueError):
        return False


def apply_cost_policy(tools: Any, snapshot: Any) -> None:

    global ICP_SPEND_CAP_USD, RESEARCH_CAP_USD, RESERVE_DRAFTS, REVERIFY_MAX_COMPANIES, POOL_CHECKED, DEPTH_RANK
    global AGENT_SHARE, AGENT_SHARE_V5, AGENT_LONG_SECONDS, REVERIFY_ASKS
    if FLAT_ICP_CAP_USD <= 0 or not flat_policy(snapshot):
        LAST_REPORT["cost_policy"] = "per_qualified"
        return
    ICP_SPEND_CAP_USD = max(ICP_SPEND_CAP_USD, FLAT_ICP_CAP_USD)
    RESEARCH_CAP_USD = round(max(0.0, ICP_SPEND_CAP_USD - FLAT_HOLDBACK_USD), 4)
    try:
        tools._phase_cap_usd = RESEARCH_CAP_USD
        if not getattr(tools, "_unknown_start", False):
            tools.spend_cap_usd = RESEARCH_CAP_USD
        deepline = ((snapshot or {}).get("providers") or {}).get("deepline") or {}
        if FLAT_FORCE or (deepline.get("limit") == 0 and deepline.get("remaining") is None):
            tools.call_budget = max(int(getattr(tools, "call_budget", 0) or 0), FLAT_CALL_BUDGET)
    except Exception:
        pass
    try:
        _scout = _sibling_module("scout")
        _scout.BUDGET_USD = max(_scout.BUDGET_USD, ICP_SPEND_CAP_USD)
        _scout.LINKEDIN_SEARCH_MAX = max(_scout.LINKEDIN_SEARCH_MAX, 20)
        if _strategy_number("flat_second_pass", 1, 0, 1) >= 1:
            _scout.SECOND_PASS = True
        if _strategy_number("flat_sonar_seed", 0, 0, 1) >= 1:
            _scout.SEED_LANE = True
        if _strategy_number("flat_hq_unknown_keep", 1, 0, 1) >= 1:
            _scout.HQ_UNKNOWN_KEEP = True
        if _strategy_number("flat_event_retry", 1, 0, 1) >= 1:
            _scout.EVENT_RETRY = True
        _sf = _sibling_module("stagefirst")
        if _strategy_number("flat_wide_all", 0, 0, 1) >= 1:
            _sf.WIDE_CATEGORIES = frozenset()
        _sf.MAX_COMPANIES = max(_sf.MAX_COMPANIES, FLAT_EVENT_COMPANIES)
        _sf.LATE_COMPANIES = max(_sf.LATE_COMPANIES, FLAT_EVENT_COMPANIES)
        _sf.EVENT_RESULTS = max(_sf.EVENT_RESULTS, 10)
        _scout.MAX_CANDIDATES = max(_scout.MAX_CANDIDATES, FLAT_CANDIDATES)
        _scout.EXTRACT_MAX_ROWS = max(_scout.EXTRACT_MAX_ROWS, FLAT_EXTRACT_ROWS)
        _sf.STAGE_CANDIDATES = max(_sf.STAGE_CANDIDATES, FLAT_CANDIDATES)
    except Exception as exc:
        LAST_REPORT["cost_policy_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    RESERVE_DRAFTS = max(RESERVE_DRAFTS, FLAT_RESERVE_DRAFTS)
    if _strategy_number("flat_pool", 1, 0, 1) >= 1:
        POOL_CHECKED = max(POOL_CHECKED, FLAT_POOL_CHECKED)
        REVERIFY_MAX_COMPANIES = max(REVERIFY_MAX_COMPANIES, FLAT_POOL_CHECKED)
    if _strategy_number("flat_depth_rank", 0, 0, 1) >= 1:
        DEPTH_RANK = True
    AGENT_SHARE = max(AGENT_SHARE, FLAT_AGENT_SHARE)
    AGENT_SHARE_V5 = max(AGENT_SHARE_V5, FLAT_AGENT_SHARE)
    AGENT_LONG_SECONDS = max(AGENT_LONG_SECONDS, FLAT_AGENT_LONG_SECONDS)
    REVERIFY_ASKS = max(REVERIFY_ASKS, FLAT_REVERIFY_ASKS)
    LAST_REPORT["cost_policy"] = "flat"


def fit_free_policy(icp: Any) -> bool:

    if FIT_FREE_FORCE:
        return True
    match = re.match(r"icp_(\d{8})_", str((icp or {}).get("icp_id") or "") if isinstance(icp, Mapping) else "")
    return bool(match) and FIT_FREE_FROM.isdigit() and len(FIT_FREE_FROM) == 8 and match.group(1) >= FIT_FREE_FROM


_EMIT_GATE_SHIPPED: Optional[bool] = None


def apply_fit_policy(icp: Any) -> None:

    global EMIT_GATE, FIT_FREE, _EMIT_GATE_SHIPPED
    if _EMIT_GATE_SHIPPED is None:
        _EMIT_GATE_SHIPPED = EMIT_GATE
    FIT_FREE = fit_free_policy(icp) and _strategy_number("fit_free_gates_off", 1, 0, 1) >= 1
    EMIT_GATE = bool(_EMIT_GATE_SHIPPED) and not FIT_FREE
    try:
        _px = _sibling_module("precision")
        _px.FIT_FREE = FIT_FREE
    except Exception as exc:
        LAST_REPORT["fit_policy_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    try:
        _sc = _sibling_module("scout")
        _sc.FIT_FREE = FIT_FREE
        _sc.DISCOVERY_PASSES = DISCOVERY_PASSES if FIT_FREE else 1
    except Exception as exc:
        LAST_REPORT["fit_policy_scout_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    LAST_REPORT["fit_policy"] = "free" if FIT_FREE else "penalized"


def evidence_depth(row: Mapping[str, Any]) -> tuple[int, int, int]:

    def bare(url: Any) -> str:
        return str(url or "").split("#")[0].rstrip("/")

    stage = [bare(i.get("url")) for i in row.get("company_stage_evidence") or [] if isinstance(i, Mapping) and i.get("url")]
    urls = set(stage)
    urls |= {bare(i.get("url")) for i in row.get("intent_signals") or [] if isinstance(i, Mapping) and i.get("url")}
    attribute = row.get("required_attribute")
    if isinstance(attribute, Mapping) and attribute.get("evidence_url"):
        urls.add(bare(attribute["evidence_url"]))
    urls.discard("")
    return (int(len(stage) >= 2) + int(len(urls) >= 5), len(stage), len(urls))


def _sibling_module(name: str) -> Any:

    try:
        return importlib.import_module("." + name, __package__) if __package__ else importlib.import_module(
            f"{os.path.basename(_HERE)}.{name}")
    except ImportError:
        return importlib.import_module(f"{os.path.basename(_HERE)}.{name}")


def release_holdback(tools: Any) -> None:

    try:
        tools._phase_cap_usd = ICP_SPEND_CAP_USD
        if not getattr(tools, "_unknown_start", False):
            tools.spend_cap_usd = ICP_SPEND_CAP_USD
    except Exception:
        pass


def draft_limit(limit: int) -> int:

    return max(1, int(limit)) + max(0, int(RESERVE_DRAFTS))


def completion_plan(spend_usd: float, have: int, left: int) -> dict[str, Any]:

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

    started = time.monotonic()
    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    limit = _company_limit()
    LAST_REPORT.clear()
    LAST_REPORT.update({"icp_id": icp.get("icp_id"), "limit": limit})
    apply_fit_policy(icp)
    if _yieldmap is not None:
        try:
            LAST_REPORT["icp_prior"] = {"prior": round(_yieldmap.prior(icp), 4), "pays": _yieldmap.pays(icp),
                                        "industry": icp.get("industry"), "stage": icp.get("company_stage"),
                                        "category": icp.get("intent_category")}
        except Exception as exc:
            LAST_REPORT["icp_prior"] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
    else:
        LAST_REPORT["icp_prior"] = {"error": "yieldmap unavailable"}
    _announce_judge_gates()

    try:
        skip = SKIP_PRIVATE_EQUITY and sm.normalize_stage(icp.get("company_stage")) == "private equity"
    except Exception:
        skip = False
    if skip:
        _log("Private Equity ICP: research not started (skip_private_equity=1)")
        LAST_REPORT["skipped"] = "private_equity"
        _emit_report()
        return []

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
        for extra in list(getattr(report, "surplus", None) or [])[:POOL_CHECKED or RESERVE_CHECKED]:
            if isinstance(extra, dict) and not (identity_keys(extra) & pool_keys):
                pool.append(extra)
                pool_keys |= identity_keys(extra)
        if DEPTH_RANK:
            pool.sort(key=evidence_depth, reverse=True)
            LAST_REPORT["depth_ranked"] = [str(c.get("company_name") or "?") for c in pool[:12]]
        reverify_n = min(len(pool), REVERIFY_MAX_COMPANIES)
        while reverify_n > 1 and not _paid_allowed(tools, REVERIFY_EST_USD * reverify_n * REVERIFY_ASKS):
            reverify_n -= 1
        reverify_est = REVERIFY_EST_USD * reverify_n * REVERIFY_ASKS
        if companies and not _paid_allowed(tools, reverify_est):
            LAST_REPORT["reverify_skipped"] = {"reason": "spend cap", "estimate_usd": round(reverify_est, 4)}
        elif companies and remaining > 40.0 and str(os.environ.get("ARENA_REVERIFY", "1")).strip() != "0":
            try:
                try:
                    from . import reverify as reverify_module
                except ImportError:
                    reverify_module = importlib.import_module(f"{os.path.basename(_HERE)}.reverify")
                head = pool[:reverify_n]
                tail = companies[reverify_n:]
                mirror_key = None
                if JUDGE_MIRROR and phase_deadline - time.monotonic() > 120.0:
                    try:
                        try:
                            from . import judgemirror as _jm
                            from . import scout as _scout_jm
                        except ImportError:
                            _jm = importlib.import_module(f"{os.path.basename(_HERE)}.judgemirror")
                            _scout_jm = importlib.import_module(f"{os.path.basename(_HERE)}.scout")
                        _jm_factory = _http_client_factory()

                        def _jm_llm(prompt: str, **kw: Any) -> Any:
                            return _scout_jm.llm_json(prompt, http_client_factory=_jm_factory, **kw)

                        LAST_REPORT["judge_mirror"] = _jm.grade(
                            head, icp, llm_json=_jm_llm,
                            deadline=min(phase_deadline - 60.0, time.monotonic() + 90.0))
                        _note_spend(tools, 0.01)
                        mirror_key = _jm.order_key
                    except Exception as exc:
                        LAST_REPORT["judge_mirror"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
                checked, notes = reverify_module.rerank(
                    head, icp, http_client_factory=_http_client_factory(),
                    timeout=min(reverify_module.TIMEOUT_S, phase_deadline - time.monotonic() - 12.0),
                    asks=REVERIFY_ASKS, order_key=mirror_key)
                _note_spend(tools, REVERIFY_EST_USD * len(head) * REVERIFY_ASKS)
                survivors = {id(c) for c in checked}
                entries = notes.get("companies") or []
                if len(entries) == len(head):
                    evicted_rows = [row for row, entry in zip(head, entries)
                                    if isinstance(entry, Mapping) and entry.get("dropped")]
                else:
                    kept_keys = {k for c in checked for k in identity_keys(c)}
                    evicted_rows = [c for c in head if id(c) not in survivors and not (identity_keys(c) & kept_keys)]
                for evicted in evicted_rows:
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
                try:
                    try:
                        from . import precision as _px
                    except ImportError:
                        _px = importlib.import_module(f"{os.path.basename(_HERE)}.precision")
                    verdicts = {str(row.get("company")): str(row.get("overall"))
                                for row in (notes.get("companies") or []) if isinstance(row, Mapping)}
                    kept, withheld = _px.confident_only(companies, verdicts)
                    if withheld and kept:
                        companies = kept
                        notes["confident_only"] = [str(c.get("company_name") or "?") for c, _ in withheld]
                except Exception as exc:
                    notes["confident_only_error"] = type(exc).__name__
                if added:
                    notes["backfilled"] = added
                    if contacts_on and time.monotonic() < phase_deadline - 20.0:
                        attach_contacts([c for c in companies if not c.get("contact")], icp, tools,
                                        deadline=phase_deadline, allow_paid=_paid_allowed(tools),
                                        checkpoint_fn=write_checkpoint)
                if not companies and getattr(report, "companions", None):
                    try:
                        back = [c for c in report.companions if isinstance(c, dict) and not (identity_keys(c) & rejected)]
                        companies = sm.validate_output(back[:limit], max_companies=limit, schema_version=schema)
                        notes["companions_restored"] = [str(c.get("company_name") or "") for c in companies]
                    except Exception:
                        companies = []
                LAST_REPORT["reverify"] = notes
                _REVERIFY_MATCHED.clear()
                _REVERIFY_MATCHED.update(
                    sm.company_name_key(entry.get("company")) for entry in (notes.get("companies") or [])
                    if isinstance(entry, Mapping) and entry.get("overall") == "match")
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
        before_gates = len(companies)
        companies = drop_unprovable(companies, icp, report)
        companies = refill_after_gates(companies, icp, report, emptied=before_gates - len(companies), limit=limit,
                                       schema=schema, exclude=rejected)
        if FIT_FREE and FILL_TO_LIMIT and len(companies) < limit:
            verdicts = {sm.company_name_key(entry.get("company")): str(entry.get("overall") or "")
                        for entry in ((LAST_REPORT.get("reverify") or {}).get("companies") or [])
                        if isinstance(entry, Mapping)}
            companies, filled = fill_to_limit(companies, report, icp, limit=limit, schema=schema,
                                              exclude=rejected, verdicts=verdicts)
            if filled:
                fill_notes: dict[str, Any] = {"added": filled}
                LAST_REPORT["fill_to_limit"] = fill_notes
                if paragraph_on and time.monotonic() < phase_deadline - 75.0 and _paid_allowed(tools, PARAGRAPH_MAX_USD):
                    try:
                        _agent_for_model = _agent_module()
                        model_name = str(_STRATEGY.get("paragraph_model") or "").strip() or \
                            os.environ.get("ARENA_MODEL", str(_STRATEGY.get("model") or "")).strip() or \
                            _agent_for_model.DEFAULT_MODEL
                        picks = [c for c in companies if str(c.get("company_name") or "") in set(filled)]
                        fill_notes["paragraphs"] = details_module.write_paragraphs(
                            picks, icp, evidence=report.evidence, model_name=model_name,
                            timeout=min(60.0, phase_deadline - time.monotonic() - 20.0),
                            http_client_factory=_http_client_factory())
                        _note_spend(tools, PARAGRAPH_MAX_USD)
                    except Exception as exc:
                        fill_notes["paragraph_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                checkpoint(companies, schema=schema, limit=limit)
        companies = judge_preflight(companies, schema=schema, report=report, tools=tools)
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
        try:
            LAST_REPORT.setdefault("direct_fetch", tools.direct_stats())
        except Exception:
            pass
        _emit_report()
        try:
            tools.close()
        except Exception:
            pass


def _tools_factory():

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
