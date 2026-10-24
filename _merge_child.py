"""Child process for the merge harness: runs one sub-agent's run_icp with its own window, spend view and checkpoint.

Started by harness.py as `python3 -I _merge_child.py` with MERGE_CHILD_SETTINGS (JSON) in the environment and
LAB_ARENA_WALL_CLOCK_SECONDS set to this agent's window. It
  * restores the parent's import path (the Arena helpers, the bundle root and the dependencies);
  * redirects lab_arena_checkpoint.write to a private file the parent folds into the real checkpoint;
  * shifts the per-ICP sourcing spend in lab_arena_checkpoint.quota_usage by the spend at this child's start, so an
    agent sees only its own spend (call counters stay real: the host enforces them anyway; point-in-time gauges such
    as in-flight calls and unresolved charges are shown unshifted, which can only overstate the spend);
  * shortens LAB_ARENA_WALL_CLOCK_SECONDS by the time this child spent starting up, so the agent's clock ends where
    the parent's window ends;
  * applies the agent's cap settings from merge.json (module attributes read at call time); a setting that cannot be
    applied is logged and skipped;
  * writes the agent's returned rows to the final file. Any failure leaves the private checkpoint in place.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import sys
from pathlib import Path

# cumulative counters, shifted by their value at this child's start; the gauges (success_unresolved_*,
# reserved_or_uncertain_microusd, inflight_calls) describe calls open right now and are left as they are
COUNTERS = ("successful_microusd", "settled_microusd")
MARGIN_MICROUSD = 60_000   # the C governors keep $0.06 below the allowance they are shown


def _log(message: str) -> None:
    try:
        print("merge-child: " + message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def shifted(snapshot: dict, base: dict | None, allowance: str, budget_usd: float) -> dict:
    """The snapshot an agent sees: money spent since `base`, caps per `allowance` ('real', 'remainder', 'budget')."""
    if not base or "sourcing_cost" not in snapshot or "sourcing_cost" not in base:
        return snapshot
    out = copy.deepcopy(snapshot)
    cost, start = out["sourcing_cost"], base["sourcing_cost"]
    for key in COUNTERS:
        cost[key] = max(0, int(cost[key]) - int(start.get(key, 0)))
    cost["successful_microusd"] = min(cost["successful_microusd"], cost["settled_microusd"])
    used_at_start = sum(int(start.get(key, 0)) for key in ("successful_microusd", "success_unresolved_microusd",
                                                            "reserved_or_uncertain_microusd"))
    if allowance == "remainder":
        cost["admission_cap_microusd"] = max(1, int(cost["admission_cap_microusd"]) - used_at_start)
    elif allowance == "budget":
        cost["admission_cap_microusd"] = max(1, int(round(budget_usd * 1e6)) + MARGIN_MICROUSD)
    return out


def _start_snapshot(real_quota, fallback: dict | None, attempts: int = 3, settle_seconds: float = 20.0) -> dict | None:
    """The spend at this child's start: read with retries, waiting (bounded) for calls still in flight from the
    previous agent; if every read fails, the parent's last snapshot (taken just before this child started)."""
    import time  # noqa: PLC0415
    deadline = time.monotonic() + settle_seconds
    snapshot = None
    for attempt in range(attempts):
        try:
            snapshot = real_quota(include_sourcing_cost=True)
        except Exception:  # noqa: BLE001
            snapshot = None
            if attempt + 1 < attempts:
                time.sleep(1.5)
            continue
        while snapshot["sourcing_cost"]["inflight_calls"] and time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                snapshot = real_quota(include_sourcing_cost=True)
            except Exception:  # noqa: BLE001
                break
        return snapshot
    return fallback if isinstance(fallback, dict) and "sourcing_cost" in fallback else None


def _set_attr(pkg: str, path: str, value) -> None:
    """'module.ATTR' or 'module.DICT[*].key' on the agent package; the value replaces (or caps, for [*]) the old."""
    module_name, _, attr = path.partition(".")
    module = importlib.import_module(pkg + "." + module_name)
    if "[*]." in attr:
        name, _, key = attr.partition("[*].")
        table = getattr(module, name)
        for entry in table.values():
            if isinstance(entry.get(key), (int, float)):
                entry[key] = min(entry[key], value)
        return
    setattr(module, attr, value)


def main() -> int:
    settings = json.loads(os.environ["MERGE_CHILD_SETTINGS"])
    sys.path[:] = [p for p in settings["sys_path"] if isinstance(p, str)]
    pkg, budget = settings["pkg"], float(settings["budget_usd"])
    partial, final = Path(settings["partial_path"]), Path(settings["final_path"])
    icp = json.loads(Path(settings["icp_path"]).read_text(encoding="utf-8"))
    try:
        import lab_arena_checkpoint as ck  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - outside the Arena sandbox (tests): run without the host hooks
        ck = None
    if ck is not None:
        real_write, real_quota = ck.write, ck.quota_usage
        base = _start_snapshot(real_quota, settings.get("base_snapshot"))
        if base is None:
            _log("no spend snapshot at start: %s sees the host's whole per-ICP spend" % pkg)

        def write(companies, *args, **kwargs):  # the agent's checkpoints go to the private file
            kwargs.pop("output_path", None)
            return real_write(companies, output_path=partial)

        def quota_usage(*args, **kwargs):
            snapshot = real_quota(*args, **kwargs)
            if kwargs.get("include_sourcing_cost"):
                return shifted(snapshot, base, settings.get("allowance", "real"), budget)
            return snapshot

        ck.write, ck.quota_usage = write, quota_usage
    window, spawned_at = settings.get("window_seconds"), settings.get("spawned_at")
    if isinstance(window, (int, float)) and isinstance(spawned_at, (int, float)):
        import time  # noqa: PLC0415
        left = int(window - max(0.0, time.time() - spawned_at))
        os.environ["LAB_ARENA_WALL_CLOCK_SECONDS"] = str(max(1, left))
    module = importlib.import_module(pkg + ".harness")
    for path, value in settings.get("patches") or []:
        try:
            _set_attr(pkg, path, budget if value == "budget" else value)
        except Exception as exc:  # noqa: BLE001 - run the agent with its own setting rather than not at all
            _log("%s: setting %s not applied: %s" % (pkg, path, type(exc).__name__))
    rows = module.run_icp(icp)
    if not (isinstance(rows, list) and all(isinstance(row, dict) for row in rows)):
        _log("%s returned %s" % (pkg, type(rows).__name__))
        return 2
    tmp = final.with_suffix(".tmp")
    tmp.write_text(json.dumps({"companies": rows}), encoding="utf-8")
    os.replace(tmp, final)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the parent falls back to the private checkpoint
        _log("%s: %s" % (type(exc).__name__, str(exc)[:300]))
        sys.exit(3)
