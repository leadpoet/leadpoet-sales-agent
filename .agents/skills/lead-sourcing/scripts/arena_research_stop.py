"""One host-authored Arena research closure; never approval or cost settlement."""

from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

FILE = "arena-research-stop.json"
SCHEMA = "tyche.arena.research_stop.v1"
POLICY = "published_allowance_final_review_reserve_v2"
DECISION = "arena_research_limit_reached"
# Final review costs $0.04-0.16 (2026-10-12) and may use the allowance down to FINAL_REVIEW_MARGIN. With 0.05,
# round arena-2026-10-14 ended ICPs at $0.80-0.82, over one company's $0.80; 0.1 can miss a second company
# found after $0.72, while an ICP over its allowance scores nothing.
RESERVE_COMPANIES = Decimal("0.1")
# The last admitted model call can settle after the final-review check (up to $0.02 on 2026-10-14).
FINAL_REVIEW_MARGIN = Decimal("0.04")
MODEL_RESERVE = 4
MAX_SNAPSHOT_AGE_SECONDS = 20
COST_FIELDS = {"settled_microusd", "success_unresolved_microusd",
               "per_qualified_pair_cap_microusd", "admission_cap_microusd"}
CAUSES = {"budget_paced", "quota_exceeded_openrouter_reserve"}


def research_ceiling(cost, published):
    full = min(Decimal(cost["admission_cap_microusd"]),
               Decimal(cost["per_qualified_pair_cap_microusd"]) * max(published, 1))
    return full - Decimal(cost["per_qualified_pair_cap_microusd"]) * RESERVE_COMPANIES


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _binding(document, ledger):
    import budget_guard
    path = Path(ledger["run_file"])
    budget_guard.check_run_identity(path, ledger)
    if ledger["version"] != 2 or document["budget"].get("policy") != "actual_cost":
        raise ValueError("Arena closure requires the unchanged actual-cost ledger")
    request = _hash(document["request"])
    started = document["stop_check"]["started_at"]
    if request != ledger.get("initial_request_fingerprint") or started != ledger.get("initial_started_at"):
        raise ValueError("Arena closure original request or clock mismatch")
    return {"run_fingerprint": ledger["run_fingerprint"], "request_fingerprint": request,
            "started_at": started,
            "icp_sha256": hashlib.sha256((path.parent / "arena-icp.json").read_bytes()).hexdigest()}


def _check(proof, document, ledger, now):
    fields = {"schema", "policy", "cause", "binding", "observed_at", "denied_at", "closed_at",
              "published_count", "cost", "remaining_model_calls", "provider_call_ids"}
    if not isinstance(proof, dict) or set(proof) != fields:
        raise ValueError("Arena closure shape is invalid")
    if proof["schema"] != SCHEMA or proof["policy"] != POLICY or proof["cause"] not in CAUSES:
        raise ValueError("Arena closure schema, policy or cause is unsupported")
    if proof["binding"] != _binding(document, ledger):
        raise ValueError("Arena closure belongs to another run or input")
    moments = [proof[key] for key in ("observed_at", "denied_at", "closed_at")]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in moments):
        raise ValueError("Arena closure timestamps are invalid")
    observed, denied, closed = moments
    started = datetime.fromisoformat(proof["binding"]["started_at"].replace("Z", "+00:00"))
    if started.utcoffset() is None or not started.timestamp() <= observed <= denied <= closed <= now:
        raise ValueError("Arena closure chronology is invalid")
    if denied - observed > MAX_SNAPSHOT_AGE_SECONDS:
        raise ValueError("Arena closure used a stale quota snapshot")
    count = proof["published_count"]
    cost = proof["cost"]
    remaining = proof["remaining_model_calls"]
    if type(count) is not int or not 0 <= count <= 5 or type(remaining) is not int or remaining < 0:
        raise ValueError("Arena closure count or quota is invalid")
    if (not isinstance(cost, dict) or set(cost) != COST_FIELDS
            or any(type(value) is not int or value < 0 for value in cost.values())
            or min(cost["admission_cap_microusd"], cost["per_qualified_pair_cap_microusd"]) <= 0):
        raise ValueError("Arena closure cost snapshot is invalid")
    spent = cost["settled_microusd"] + cost["success_unresolved_microusd"]
    ceiling = research_ceiling(cost, count)
    if spent >= cost["admission_cap_microusd"]:
        raise ValueError("Arena closure cannot claim a final-review reserve at the hard cap")
    if proof["cause"] == "budget_paced":
        if spent < ceiling:
            raise ValueError("Arena closure budget threshold was not reached")
    elif spent >= ceiling or remaining > MODEL_RESERVE:
        raise ValueError("Arena closure model reserve threshold was not reached")
    ids = proof["provider_call_ids"]
    # Ledger reservations identify admitted/uncertain calls, not proof of a
    # remote dispatch or charge. Settlement may change their state and amounts.
    # Unsent plans have no reservation and do not invalidate review.
    if (not isinstance(ids, list) or any(not isinstance(value, str) for value in ids)
            or ids != sorted(ledger["calls"])):
        raise ValueError("Arena closure provider reservations changed; review dispatch accounting")
    return proof


def read(document, ledger, *, now=None):
    """None means no Arena policy; malformed explicit evidence fails closed."""
    if ledger is None:
        return None
    path = Path(ledger["run_file"]).parent / FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_size > 65536:
            raise ValueError("Arena closure must be a bounded regular file")
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    try:
        proof = json.loads(raw)
    except RecursionError as exc:
        raise ValueError("Arena closure nesting is invalid") from exc
    return _check(proof, document, ledger, time.time() if now is None else now)


def write(run_file, event):
    """Persist the actual guard event after its research worker has exited."""
    import budget_guard
    run_file = Path(run_file)
    document = budget_guard.read_object(run_file)
    ledger = budget_guard.load_ledger(run_file)
    proof = {"schema": SCHEMA, "policy": POLICY, "binding": _binding(document, ledger),
             **event, "closed_at": time.time(), "provider_call_ids": sorted(ledger["calls"])}
    _check(proof, document, ledger, proof["closed_at"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=run_file.parent,
                                         prefix=".arena-stop-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(proof, handle, allow_nan=False)
        os.replace(temporary, run_file.parent / FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
