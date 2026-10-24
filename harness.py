"""Merge harness: runs two complete Arena agents on the same ICP and returns the best rows of both.

Each sub-agent is an unchanged package beside this file (see merge.json). Per ICP the host calls run_icp once.
The agents run one after another, each in its own child process (_merge_child.py), so memory is freed between
them and an agent that overruns its window can be stopped. Each child gets:
  * its own time window, through LAB_ARENA_WALL_CLOCK_SECONDS (every agent plans from that and its start time);
  * its own spend view: the host's per-ICP sourcing spend minus the spend already used when it started;
  * a private checkpoint file. This process folds the private checkpoints into the real one, so the host's last
    checkpoint always holds the first agent's final rows plus the second agent's rows so far.
Every row is checked with the host's own output validator (lab_arena.output, under /model) before it is merged, so
one invalid row from one agent cannot void the other agent's rows. An agent's unfinished checkpoint rows only fill
slots left free by finished agents.
This function never raises: an ICP left unfinished can void the whole entry, so any failure returns the best rows
seen so far.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "merge.json"
CHILD = ROOT / "_merge_child.py"
POLL_SECONDS = 2.0
STOP_GRACE_SECONDS = 60.0
KILL_GRACE_SECONDS = 15.0
DEADLINE_SLACK_SECONDS = 20.0   # stop the last child this long before the host's wall clock, after both stop graces
DEFAULT_SCHEMA = "leadpoet.lab_arena.output.v6"
_STATE: dict[str, Any] = {"lists": [], "partials": [], "best": []}


def _log(message: str) -> None:
    try:
        print("merge: " + message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _checkpoint_module():
    try:
        import lab_arena_checkpoint  # noqa: PLC0415 - present only in the Arena sandbox
        return lab_arena_checkpoint
    except Exception:  # noqa: BLE001
        return None


def _snapshot(attempts: int = 3) -> dict | None:
    """The host's per-ICP quota and sourcing-cost snapshot, read with retries; None if it cannot be read."""
    ck = _checkpoint_module()
    if ck is None:
        return None
    for attempt in range(attempts):
        try:
            return ck.quota_usage(include_sourcing_cost=True)
        except Exception:  # noqa: BLE001
            if attempt + 1 < attempts:
                time.sleep(1.0)
    return None


def _spent_usd(snapshot: dict | None) -> float | None:
    """Conservative per-ICP sourcing spend (successful + unresolved + reserved), or None if unknown."""
    if not snapshot:
        return None
    cost = snapshot["sourcing_cost"]
    micro = cost["successful_microusd"] + cost["success_unresolved_microusd"] + cost["reserved_or_uncertain_microusd"]
    return micro / 1e6


def _cap_usd(snapshot: dict | None, default: float = 4.0) -> float:
    if not snapshot:
        return default
    return snapshot["sourcing_cost"]["admission_cap_microusd"] / 1e6


# ----------------------------------------------------------------------------------------------- merging rows

_VALIDATOR: dict[str, Any] = {}


def _validator():
    """The host's output validator (judge image /model), or None outside the sandbox."""
    if "fn" not in _VALIDATOR:
        try:
            if "/model" not in sys.path:
                sys.path.append("/model")
            from lab_arena import output  # noqa: PLC0415
            _VALIDATOR["fn"] = output.validate_companies
            _log("rows checked with the host's output validator")
        except Exception as exc:  # noqa: BLE001
            _VALIDATOR["fn"] = None
            _log("host output validator unavailable (%s); rows are merged unchecked" % type(exc).__name__)
    return _VALIDATOR["fn"]


def valid_rows(rows: list, icp: dict) -> list[dict]:
    """The rows the host's validator accepts one by one (all dict rows when the validator is unavailable)."""
    rows = [row for row in rows or [] if isinstance(row, dict)]
    validate = _validator()
    if validate is None:
        return rows
    schema = str((icp or {}).get("output_schema_version") or "").strip() or DEFAULT_SCHEMA
    kept = []
    for row in rows:
        try:
            validate([row], schema_version=schema)
        except Exception:  # noqa: BLE001 - OutputInvalid or an unsupported schema
            continue
        kept.append(row)
    return kept

def _host(url: Any) -> str:
    text = str(url or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    return text[4:] if text.startswith("www.") else text


def _linkedin_slug(url: Any) -> str:
    match = re.search(r"linkedin\.com/company/([^/?#]+)", str(url or "").lower())
    return match.group(1).strip() if match else ""


def _name_key(name: Any) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    words = [w for w in text.split() if w not in {"inc", "llc", "ltd", "limited", "corp", "corporation", "co",
                                                   "company", "gmbh", "plc", "sa", "ag", "the"}]
    return " ".join(words)


def _keys(row: dict) -> set:
    keys = {("n", _name_key(row.get("company_name"))), ("h", _host(row.get("company_website"))),
            ("l", _linkedin_slug(row.get("company_linkedin")))}
    return {k for k in keys if k[1]}


def _staged(icp: dict) -> bool:
    stage = str((icp or {}).get("company_stage") or "").strip().lower()
    return bool(stage) and stage not in {"any", "all", "none", "n/a"}


def merge_rows(lists: list[list[dict]], limit: int, icp: dict, policy: str = "interleave",
               partials: list[list[dict]] | None = None) -> list[dict]:
    """Union of the agents' rows, best first, without duplicates, at most `limit` rows.

    Order: each agent's own ranking is kept; agents alternate rank by rank ("interleave") or the first agent's rows
    come first ("first"). On an ICP that names a stage, rows with company_stage_evidence go before rows without it
    (10-20..10-22: 15.7% vs 0.3% qualified). Rows from `partials` (agents that have not returned: unvetted drafts)
    only fill the slots left after every finished agent's rows. A row is a duplicate if its name, website host or
    LinkedIn slug matches an earlier row (duplicates are penalized). Rows the host's validator rejects are dropped."""
    candidates = []
    for agent_index, rows in enumerate(lists):
        for rank, row in enumerate(valid_rows(rows, icp)):
            candidates.append((rank, agent_index, row))
    if policy == "first":
        candidates.sort(key=lambda item: (item[1], item[0]))
    else:
        candidates.sort(key=lambda item: (item[0], item[1]))
    if _staged(icp):
        candidates.sort(key=lambda item: not item[2].get("company_stage_evidence"))
    out, seen = [], set()
    for _rank, _agent, row in candidates:
        keys = _keys(row)
        if not keys or keys & seen:
            continue
        seen |= keys
        out.append(row)
        if len(out) >= limit:
            return out
    for rows in partials or []:
        for row in valid_rows(rows, icp):
            keys = _keys(row)
            if not keys or keys & seen:
                continue
            seen |= keys
            out.append(row)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------------------------- child processes

def _read_rows(path: Path) -> list[dict] | None:
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("companies")
    except Exception:  # noqa: BLE001
        return None
    if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
        return rows
    return None


def _write_checkpoint(rows: list[dict]) -> None:
    ck = _checkpoint_module()
    if ck is None:
        _STATE["best"] = rows
        return
    _STATE["best"] = rows
    try:
        ck.write(rows)
    except Exception as exc:  # noqa: BLE001
        _log("checkpoint write failed: %s" % type(exc).__name__)


def _stop(proc: subprocess.Popen) -> None:
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=KILL_GRACE_SECONDS)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except Exception:  # noqa: BLE001
            pass


def _signature(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _merged(limit: int, icp: dict, policy: str, extra: list[dict] | None = None, extra_final: bool = False) -> list[dict]:
    lists, partials = list(_STATE["lists"]), list(_STATE["partials"])
    if extra is not None:
        (lists if extra_final else partials).append(extra)
    return merge_rows(lists, limit, icp, policy, partials)


def _run_child(agent: dict, icp_path: Path, window: float, budget: float, work: Path, icp: dict,
               limit: int, policy: str, base_snapshot: dict | None, deadline: float) -> tuple[list[dict], bool]:
    """Run one agent; returns (rows, finished): finished rows came back from run_icp, unfinished ones from the
    agent's last checkpoint (it crashed, overran or was stopped at the deadline)."""
    partial, final = work / (agent["pkg"] + ".partial.json"), work / (agent["pkg"] + ".final.json")
    settings = {"pkg": agent["pkg"], "icp_path": str(icp_path), "partial_path": str(partial),
                "final_path": str(final), "budget_usd": round(budget, 4), "allowance": agent.get("allowance", "real"),
                "patches": agent.get("patches") or [], "sys_path": list(sys.path), "base_snapshot": base_snapshot,
                "window_seconds": int(window), "spawned_at": time.time()}
    env = dict(os.environ)
    env["LAB_ARENA_WALL_CLOCK_SECONDS"] = str(int(window))
    env["MERGE_CHILD_SETTINGS"] = json.dumps(settings)
    started = time.monotonic()
    # the stop (up to two kill graces) must finish before the host's wall clock ends, but never cuts the window itself
    hard_end = max(started + window, min(started + window + STOP_GRACE_SECONDS, deadline - 2 * KILL_GRACE_SECONDS))
    _log("%s (%s): window %ds, budget $%.2f" % (agent["pkg"], agent.get("lib"), window, budget))
    try:
        proc = subprocess.Popen([sys.executable or "python3", "-I", str(CHILD)], env=env, cwd=str(ROOT))
    except Exception as exc:  # noqa: BLE001
        _log("%s did not start: %s" % (agent["pkg"], type(exc).__name__))
        return [], False
    seen = None
    latest: list[dict] = []
    final_since = None
    while True:
        code = proc.poll()
        sig = _signature(partial)
        if sig and sig != seen:
            seen = sig
            rows = _read_rows(partial)
            if rows is not None:
                latest = rows
                _write_checkpoint(_merged(limit, icp, policy, latest))
        if code is not None:
            break
        if final.exists():   # the agent has returned; do not let a slow exit hold its rows back
            final_since = final_since or time.monotonic()
            done = _read_rows(final)
            if done is not None:
                _write_checkpoint(_merged(limit, icp, policy, done, extra_final=True))
            if time.monotonic() - final_since > 10.0:
                _log("%s returned but did not exit; stopping it" % agent["pkg"])
                _stop(proc)
                break
        if time.monotonic() > hard_end:
            _log("%s overran its window; stopping it" % agent["pkg"])
            _stop(proc)
            break
        time.sleep(POLL_SECONDS)
    rows = _read_rows(final)
    finished = rows is not None
    if not finished:
        rows = _read_rows(partial) or latest
    _log("%s %s in %ds with %d rows (exit %s)" % (agent["pkg"], "finished" if finished else "stopped unfinished",
                                                  time.monotonic() - started, len(rows), proc.returncode))
    return rows, finished


def _run(icp: dict, started: float) -> list[dict]:
    cfg = _config()
    agents = cfg["agents"]
    policy = cfg.get("merge_policy", "interleave")
    wall = _env_float("LAB_ARENA_WALL_CLOCK_SECONDS", 3600.0)
    end = started + wall - float(cfg.get("reserve_seconds", 45))
    deadline = started + wall - DEADLINE_SLACK_SECONDS
    limit = int(_env_float("LAB_ARENA_COMPANY_LIMIT", 5))
    margin = float(cfg.get("spend_margin_usd", 0.3))
    work = Path(tempfile.mkdtemp(prefix="merge-"))
    icp_path = work / "icp.json"
    icp_path.write_text(json.dumps(icp), encoding="utf-8")
    start_snapshot = _snapshot()
    start_spent = _spent_usd(start_snapshot) or 0.0
    assumed = 0.0   # spend assumed for earlier agents when the host snapshot cannot be read
    for index, agent in enumerate(agents):
        later = agents[index + 1:]
        snapshot = _snapshot()   # read first: its retries must not eat into the window computed below
        left = end - time.monotonic() - sum(float(a.get("min_window_seconds", 600)) for a in later)
        window = left if agent.get("window_seconds") is None else min(float(agent["window_seconds"]), left)
        if window < float(agent.get("min_window_seconds", 600)):
            _log("%s skipped: %ds left" % (agent["pkg"], window))
            continue
        spent = _spent_usd(snapshot)
        if spent is None:   # unknown: assume every earlier agent spent its whole cap
            spent = start_spent + assumed
            _log("spend unknown; assuming $%.2f" % spent)
        cap = _cap_usd(snapshot or start_snapshot)
        room = cap - margin - spent
        room -= sum(float(a.get("min_budget_usd", 0.3)) for a in later)
        budget = room if agent.get("budget_usd") is None else min(float(agent["budget_usd"]), room)
        if budget < float(agent.get("min_budget_usd", 0.3)):
            _log("%s skipped: $%.2f left" % (agent["pkg"], budget))
            continue
        assumed += min(budget, float(agent.get("own_cap_usd") or budget))
        rows, finished = _run_child(agent, icp_path, window, budget, work, icp, limit, policy,
                                    snapshot or start_snapshot, deadline)
        _STATE["lists" if finished else "partials"].append(rows)
        _write_checkpoint(_merged(limit, icp, policy))
    return _merged(limit, icp, policy)


def run_icp(icp):
    started = time.monotonic()
    try:
        return _run(icp if isinstance(icp, dict) else {}, started)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - never leave the ICP unfinished
        _log("merge failed: %s: %s" % (type(exc).__name__, str(exc)[:200]))
        return list(_STATE["best"])   # the last merged checkpoint (never fewer rows than it holds)
