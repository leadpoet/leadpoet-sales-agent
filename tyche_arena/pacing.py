"""Keep Leadpoet spend within the request's budget, capped by Leadpoet's reported limit.

The request's budget ($0.80 per requested company) drives spend; Leadpoet's quota
snapshot caps it (arena_research_stop.allowance): under its legacy per-qualified-company
rule that is one company's $0.80, under its flat per-ICP policy $4. Published companies
add no allowance. Research keeps 0.1 of the allowance for final review, which may use
the allowance less a small margin for the last in-flight call, never an additional
model margin. The read is Leadpoet's own passive quota snapshot; when
unavailable the host's hard limits still apply.

Final review gets no allowance for accepted rows that are not yet published. In
round arena-2026-10-15 that credit let one ICP's final review spend to $1.571 for two
accepted rows; one was published and qualified, and the ICP scored zero.
"""

import json
import math
import os
from decimal import Decimal
from pathlib import Path
import stat
import tempfile
import threading
import time

from . import SKILL  # noqa: F401  (sets sys.path for the skill scripts)
import arena_research_stop as research_stop

OPENROUTER_RESERVE = research_stop.MODEL_RESERVE
# Quota reads share the runtime's configured per-run limit.
# Remote provider calls read each time; model requests reuse a read this long.
MODEL_GUARD_READ_SECONDS = 20
PUBLISHED_FILE = "published-count.json"
HOST_COST_FILE = "host-cost-observation.json"
HOST_COST_FIELDS = ("settled_microusd", "success_unresolved_microusd",
                    "per_qualified_pair_cap_microusd", "admission_cap_microusd")
HOST_COST_NOTE = ("1,000,000 microusd = $1. Includes Luna and provider spend; overlaps local costs, "
                  "so do not add them. May lag in-flight charges. The per-qualified cap uses "
                  "Arena-qualified companies, not local approval. Observation only; not headroom or a reservation.")


def _snapshot():
    try:
        import lab_arena_checkpoint
        return lab_arena_checkpoint.quota_usage(include_sourcing_cost=True)
    except Exception:  # QuotaUnavailable, a host without cost snapshots, or no helper at all.
        return None


def _numeric_source_cost(snapshot):
    cost = snapshot.get("sourcing_cost") if isinstance(snapshot, dict) else None
    if not isinstance(cost, dict) or any(type(cost.get(key)) is not int or cost[key] < 0
                                         for key in HOST_COST_FIELDS):
        return None
    return {key: cost[key] for key in HOST_COST_FIELDS}


def save_cost_observation(run_file, snapshot):
    """Persist only an existing quota read; never fetch or use this file for admission."""
    cost = _numeric_source_cost(snapshot)
    observation = {"observed_at": time.time(), "status": "observed" if cost is not None else "unknown"}
    if cost is not None:
        observation["sourcing_cost"] = cost
    path = Path(run_file).parent / HOST_COST_FILE
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".host-cost-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(observation, handle)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cost_observation(run_file):
    """Bounded, passive display; absent or stale host cost is unknown, never zero."""
    unknown = lambda reason: {"status": "unknown", "reason": reason, "note": HOST_COST_NOTE}
    try:
        fd = os.open(Path(run_file).parent / HOST_COST_FILE,
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_size > 4096:
                return unknown("malformed")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            return unknown("malformed")
        observation = json.loads(raw)
        if not isinstance(observation, dict):
            return unknown("malformed")
        captured = observation.get("observed_at")
        if type(captured) not in (int, float) or not math.isfinite(captured):
            return unknown("malformed")
        age = time.time() - captured
        if age < 0:
            return unknown("future")
        if age > MODEL_GUARD_READ_SECONDS:
            return unknown("stale")
        if observation.get("status") == "unknown":
            return unknown("unavailable")
        cost = _numeric_source_cost(observation)
        if observation.get("status") != "observed" or cost is None:
            return unknown("malformed")
        return {"status": "observed", "observed_at": captured, "age_seconds": age,
                "sourcing_cost": cost, "note": HOST_COST_NOTE}
    except FileNotFoundError:
        return unknown("missing")
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        return unknown("malformed")


def save_published_count(run_dir, count):
    path = Path(run_dir) / PUBLISHED_FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"published": int(count)}))
    os.replace(temporary, path)


def published_count(run_file):
    try:
        return int(json.loads((Path(run_file).parent / PUBLISHED_FILE).read_text())["published"])
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def closure(run_file):
    """Read an explicit closure only; ordinary runs keep their existing policy."""
    if not os.path.lexists(Path(run_file).parent / research_stop.FILE):
        return None
    import budget_guard
    ledger = budget_guard.load_ledger(run_file)
    if ledger is None:
        raise ValueError("Arena closure has no bound ledger")
    return research_stop.read(budget_guard.read_object(Path(run_file)), ledger)


def refusal(run_file, *, snapshot=None, reserve_model_calls=0, final_review=False):
    """A refusal code, or None when another request fits the allowance."""
    if not final_review:
        try:
            if stopped := closure(run_file):
                return stopped["cause"]  # Confirmation cannot reopen provider research.
        except (OSError, ValueError, KeyError, TypeError):
            return "invalid_arena_research_stop"
    snapshot = _snapshot() if snapshot is None else snapshot
    if not isinstance(snapshot, dict):
        return None
    try:
        cost = snapshot["sourcing_cost"]
        spent = cost["settled_microusd"] + cost["success_unresolved_microusd"]
        cap = cost["admission_cap_microusd"]
        if "per_qualified_pair_cap_microusd" not in cost:
            return None
        remaining_model_calls = snapshot["providers"]["openrouter"]["remaining"]
    except (KeyError, TypeError):
        return None
    if spent >= cap:
        return "budget_exhausted"
    # Keep the micro-USD boundary exact.
    budget = research_stop.request_budget(run_file)
    if final_review:
        ceiling = research_stop.allowance(cost, budget) * (1 - research_stop.FINAL_REVIEW_MARGIN)
    else:
        ceiling = research_stop.research_ceiling(cost, budget)
    if spent >= ceiling:
        return "budget_paced"
    if remaining_model_calls <= reserve_model_calls:
        return "quota_exceeded_openrouter_reserve"
    return None


class ModelRequestGuard:
    """The Codex bridge's request_guard: True admits one more model request."""

    def __init__(self, run_file, clock=time.monotonic):
        self.run_file, self.clock = run_file, clock
        self._final_review = False
        self.denial = None
        self.snapshot, self.read_at = None, None
        self.observed_at, self.research_stop_event = None, None
        self._lock = threading.RLock()

    @property
    def final_review(self):
        return self._final_review

    @final_review.setter
    def final_review(self, value):
        if value != self._final_review:
            self._final_review = value
            self.read_at = None  # A new phase must observe its current spend/quota.

    def __call__(self):
        # Parallel researchers each have a Leadpoet session whose bridge calls this guard from its own thread.
        with self._lock:
            return self._admit()

    def _admit(self):
        if self.read_at is None or self.clock() - self.read_at >= MODEL_GUARD_READ_SECONDS:
            self.snapshot, self.read_at = _snapshot(), self.clock()
            self.observed_at = time.time()
            try:
                save_cost_observation(self.run_file, self.snapshot)
            except Exception:
                # Do not leave an earlier successful observation looking current after a failed write.
                try:
                    (Path(self.run_file).parent / HOST_COST_FILE).unlink(missing_ok=True)
                except OSError:
                    pass  # Optional display must never change the existing admission decision.
        # An unavailable read ({}) is not retried before the interval either; the host's own limits still apply.
        count = published_count(self.run_file)
        self.denial = refusal(self.run_file, snapshot=self.snapshot if self.snapshot is not None else {},
                              reserve_model_calls=0 if self.final_review else OPENROUTER_RESERVE,
                              final_review=self.final_review)
        if not self.final_review and self.denial in research_stop.CAUSES and self.research_stop_event is None:
            cost = _numeric_source_cost(self.snapshot)
            if cost is not None:
                self.research_stop_event = {"cause": self.denial, "observed_at": self.observed_at,
                    "denied_at": time.time(), "published_count": count, "cost": cost,
                    "remaining_model_calls": self.snapshot["providers"]["openrouter"]["remaining"]}
        return self.denial is None

    def close_research(self):
        with self._lock:
            if self.research_stop_event is not None and closure(self.run_file) is None:
                research_stop.write(self.run_file, self.research_stop_event)
