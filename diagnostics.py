"""The infrastructure-fault channel: record a host fault where it is FIRST SEEN.

Why this module exists (LAB-LOG #234 re-apply gate 1, the CRITICAL one):

`agent.guard` swallows every tool exception into ``{"ok": False, ...}`` so the
agent can keep working.  That is right for the loop and wrong for the failure
contract, because the ONE thing `harness._no_companies` looks at is the final
exception chain (`harness.provider_infrastructure_error`).  A Deepline 429/5xx
reaches us as an HTTP **502** whose body is exactly
``{"error":{"code":"provider_unavailable"}}`` -- `lab_arena/operations.py:203-204`
and `:1661-1667` turn every infrastructure provider status into that one generic
reply -- and `arena_tools._deepline` raises it as
``RuntimeError("provider_unavailable")``.  `guard` catches it, the run limps on,
and whatever exception finally escapes carries no trace of the host fault.
`_no_companies` then calls the run OUR fault and RAISES.

That is the expensive part.  The host has the same call on record
(`lab_arena/runner.py:1395-1405`: ``error_code in ("broker_unavailable",
"provider_unavailable")`` or an infrastructure ``provider_status``), so a run
that ends non-accepted has its terminal cause rewritten to ``provider_error``
(`runner.py:1406-1408`), which is NOT in ``MODEL_CAUSED_TERMINAL_CAUSES``
(`lab_arena/contracts.py:171-173`), so the assignment counts as incomplete and
**one incomplete assignment cancels the round for every miner**
(`scripts/185-lab-arena-miner-credentials.sql:609-620`).

So: record the fault at the point of first sight -- `arena_tools._deepline` for
the tool channel, `arena_transport` for the OpenRouter channel -- and let
`_no_companies` consult the record as well as the exception chain.

⛔ This module carries NO covert channel.  An earlier draft encoded a fault class
in the BYTE LENGTH of stdout (``emit_once`` / ``STDOUT_LENGTH_BY_CODE`` /
``decode_stdout_length``).  That is smuggling data through a host resource metric
and it is rejected; it must not be reintroduced in any form.  Everything here is
either an in-process counter or an ordinary human-readable stderr line.

Bounded on purpose: statuses and codes come from a closed vocabulary, so nothing
a provider puts in an error body can reach our logs or our report.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

INFRA_STATUSES = frozenset({401, 402, 403, 407, 429}) | frozenset(range(500, 600))
INFRA_CODES = ("provider_unavailable", "broker_unavailable", "miner_credentials_unavailable")

TERMINAL_REASONS = frozenset(
    {"http_401", "http_402", "http_403", "http_407", "miner_credentials_unavailable"}
)

ALLOWED_ERROR_CODES = frozenset(set(INFRA_CODES) | {
    "budget_refused", "call_refused", "call_uncertain", "invalid_request",
    "lease_stale", "miner_provider_not_configured", "model_not_allowed",
    "transport_error", "other",
})
ALLOWED_STATUSES = frozenset({200, 400, 401, 402, 403, 407, 408, 409, 422, 429, 500, 502, 503, 504})

CHANNELS = ("openrouter", "deepline")

_LOCK = threading.Lock()
LAST_REPORT: dict[str, Any] = {}


def _blank() -> dict[str, Any]:
    return {
        "responses": 0,
        "transport_errors": 0,
        "status_counts": {},
        "error_counts": {},
        "infra_faults": {},
        "channel_health": {},
        "logged": {},
    }


def reset() -> None:
    """Start one ICP (or one test) with a clean record.

    The Arena gives every run a fresh sandbox (`lab_arena/runner.py:1248-1266`),
    so this only matters for local multi-ICP runs in one process -- but there a
    leaked fault would make the NEXT ICP return [] instead of raising, and that
    silently forfeits its confirmation attempt.
    """

    with _LOCK:
        LAST_REPORT.clear()
        LAST_REPORT.update(_blank())


def _log(message: str) -> None:
    try:
        print("[arena-diagnostics] %s" % message, file=sys.stderr, flush=True)
    except Exception:
        return


def classify(status: Any = None, error_code: Any = None) -> str | None:
    """Name the host fault in a (status, error_code) pair, else None.

    Mirrors `harness.provider_infrastructure_error` for values we hold directly
    rather than dig out of an exception chain.
    """

    if isinstance(error_code, str) and error_code in INFRA_CODES:
        return error_code
    if isinstance(status, int) and not isinstance(status, bool) and status in INFRA_STATUSES:
        return "http_%d" % status
    return None


def record_response(status: Any, error_code: Any = None, *, channel: str = "openrouter") -> str | None:
    """Record one provider reply.  Returns the infra reason if it was one.

    Never raises: a diagnostic must not be able to fail a run.
    """

    try:
        channel = channel if channel in CHANNELS else "openrouter"
        parsed = int(status) if isinstance(status, int) and not isinstance(status, bool) else None
        status_key = str(parsed) if parsed in ALLOWED_STATUSES else "other"
        code_key = error_code if error_code in ALLOWED_ERROR_CODES else ("other" if error_code else None)
        if code_key is None and (parsed is None or not 200 <= parsed < 300):
            code_key = "other"
        reason = classify(parsed, error_code)
        with _LOCK:
            if not LAST_REPORT:
                LAST_REPORT.update(_blank())
            LAST_REPORT["responses"] = int(LAST_REPORT.get("responses", 0)) + 1
            statuses = LAST_REPORT.setdefault("status_counts", {})
            statuses[status_key] = int(statuses.get(status_key, 0)) + 1
            if code_key:
                errors = LAST_REPORT.setdefault("error_counts", {})
                errors[code_key] = int(errors.get(code_key, 0)) + 1
            first = False
            LAST_REPORT.setdefault("channel_health", {})[channel] = reason
            if reason:
                faults = LAST_REPORT.setdefault("infra_faults", {})
                if channel not in faults:
                    faults[channel] = reason
                    first = True
        if first:
            _log("HOST fault on the %s channel: %s -- recorded so an empty run completes "
                 "instead of raising (an incomplete infrastructure assignment cancels the "
                 "round for every miner)" % (channel, reason))
        return reason
    except Exception:
        return None


def record_transport_error(*, channel: str = "openrouter", detail: str = "") -> None:
    """A request that never produced a status.  Counted, never an infra fault.

    A transport error before dispatch leaves nothing on the host's ``state.calls``
    for `runner.py:1395` to read, so it cannot become ``provider_error``.  Raising
    on it books ``model_error``, which IS model-caused and round-safe, and earns a
    confirmation attempt.  Turning it into a silent [] would throw that away.
    """

    try:
        channel = channel if channel in CHANNELS else "openrouter"
        with _LOCK:
            if not LAST_REPORT:
                LAST_REPORT.update(_blank())
            LAST_REPORT["transport_errors"] = int(LAST_REPORT.get("transport_errors", 0)) + 1
            errors = LAST_REPORT.setdefault("error_counts", {})
            errors["transport_error"] = int(errors.get("transport_error", 0)) + 1
    except Exception:
        return
    if detail:
        _log("transport error on the %s channel (%s); not an infra fault -- nothing "
             "reached the host's call ledger" % (channel, str(detail)[:80]))


def infra_fault(channel: str | None = None) -> str | None:
    """The first recorded host fault, for one channel or for any of them."""

    try:
        with _LOCK:
            faults = dict(LAST_REPORT.get("infra_faults") or {})
        if channel is not None:
            return faults.get(channel)
        for name in CHANNELS:
            if faults.get(name):
                return faults[name]
        return None
    except Exception:
        return None


def channel_down(channel: str = "openrouter") -> str | None:
    """The infra reason on the MOST RECENT reply on this channel, else None.

    ⛔ Not a replacement for `infra_fault`: that one is the HISTORY, and the
    history is what `harness._no_companies` must keep reading, because what
    decides the round is what the HOST has on `state.calls`
    (`runner.py:1395-1408`) -- a fault that happened is on record even if the
    channel recovered afterwards.

    This is the other question: is the channel down RIGHT NOW, i.e. would the
    next request on it be refused?  Only the salvage decision asks it, because
    that decision is about whether one more request is worth buying.  A reply
    that never produced a status (`record_transport_error`) leaves the health
    unchanged: nothing reached the broker, so nothing was reserved and nothing
    says the next attempt is doomed.
    """

    try:
        with _LOCK:
            health = dict(LAST_REPORT.get("channel_health") or {})
        return health.get(channel if channel in CHANNELS else "openrouter")
    except Exception:
        return None


def terminal_fault(channel: str | None = None) -> str | None:
    """A recorded fault on this channel that CANNOT heal inside the run.

    401/402/403/407 (the money cap arrives as 402) and a missing credential.
    Once one of those is on record every later request on the channel is spend
    with no chance of an answer, so this reads the history, not the health.
    """

    try:
        with _LOCK:
            faults = dict(LAST_REPORT.get("infra_faults") or {})
            health = dict(LAST_REPORT.get("channel_health") or {})
        names = (channel,) if channel is not None else CHANNELS
        for name in names:
            for reason in (faults.get(name), health.get(name)):
                if reason in TERMINAL_REASONS:
                    return reason
        return None
    except Exception:
        return None


def snapshot() -> dict[str, Any]:
    """A small, allowlisted, human-readable copy for LAST_REPORT."""

    try:
        with _LOCK:
            raw_statuses = LAST_REPORT.get("status_counts") or {}
            raw_errors = LAST_REPORT.get("error_counts") or {}
            faults = dict(LAST_REPORT.get("infra_faults") or {})
            health = dict(LAST_REPORT.get("channel_health") or {})
            return {
                "responses": max(0, int(LAST_REPORT.get("responses", 0))),
                "transport_errors": max(0, int(LAST_REPORT.get("transport_errors", 0))),
                "status_counts": {k: int(v) for k, v in sorted(raw_statuses.items()) if int(v) > 0},
                "error_counts": {k: int(v) for k, v in sorted(raw_errors.items())
                                 if k in ALLOWED_ERROR_CODES and int(v) > 0},
                "infra_faults": {k: v for k, v in sorted(faults.items()) if k in CHANNELS},
                "channel_down_now": {k: v for k, v in sorted(health.items()) if k in CHANNELS and v},
            }
    except Exception:
        return {"responses": 0, "transport_errors": 0, "status_counts": {},
                "error_counts": {}, "infra_faults": {}, "channel_down_now": {}}


reset()

__all__ = [
    "ALLOWED_ERROR_CODES", "ALLOWED_STATUSES", "CHANNELS", "INFRA_CODES", "INFRA_STATUSES",
    "LAST_REPORT", "TERMINAL_REASONS", "channel_down", "classify", "infra_fault",
    "record_response", "record_transport_error", "reset", "snapshot", "terminal_fault",
]
