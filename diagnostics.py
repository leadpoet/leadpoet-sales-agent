"""The infrastructure-fault channel: record a host fault where it is FIRST SEEN."""

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
    """Start one ICP (or one test) with a clean record."""

    with _LOCK:
        LAST_REPORT.clear()
        LAST_REPORT.update(_blank())


def _log(message: str) -> None:
    try:
        print("[arena-diagnostics] %s" % message, file=sys.stderr, flush=True)
    except Exception:
        return


def classify(status: Any = None, error_code: Any = None) -> str | None:
    """Name the host fault in a (status, error_code) pair, else None."""

    if isinstance(error_code, str) and error_code in INFRA_CODES:
        return error_code
    if isinstance(status, int) and not isinstance(status, bool) and status in INFRA_STATUSES:
        return "http_%d" % status
    return None


def record_response(status: Any, error_code: Any = None, *, channel: str = "openrouter") -> str | None:
    """Record one provider reply."""

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
    """A request that never produced a status."""

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
        _log("transport error on the %s channel (%s); not recorded as an infra fault -- the call may still "
             "have settled on the host's call ledger" % (channel, str(detail)[:80]))


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
    """The infra reason on the MOST RECENT reply on this channel, else None."""

    try:
        with _LOCK:
            health = dict(LAST_REPORT.get("channel_health") or {})
        return health.get(channel if channel in CHANNELS else "openrouter")
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
    "record_response", "record_transport_error", "reset", "snapshot",
]
