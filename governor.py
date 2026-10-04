"""Per-ICP spend governor and run clock.

Spend: a provider call may start only when the projected spend of this ICP -- committed spend + the maximum cost of
every call in flight + the maximum cost of the new call -- stays within the ICP cap ($0.74 by default across all
attempts, under the judge's $0.80-per-qualified-company rule; the 09-29 brief's per-ICP ceiling).  Committed spend is the host's last quota snapshot (successful plus
unresolved sourcing cost of every attempt, plus UNRESOLVED_CALL_PAD_USD per unresolved call because the host reserves
$0 for a dynamically priced Deepline call in flight) plus this attempt's own spend booked since that read; before the
first read it is the earlier attempts' spend plus this attempt's own spend.  Own spend books each call at its reported
charge, else at its maximum (an Exa search is booked at $0.016 and billed $0.010), so the snapshot is re-read every
SNAPSHOT_REFRESH_S while calls are being booked.  At most MAX_INFLIGHT provider calls run at once.

Clock: every deadline comes from LAB_ARENA_WALL_CLOCK_SECONDS.  No provider call starts after the paid cutoff
(wall - 7 min, or 80% of a short wall), and the final checkpoint is written by wall - 3 min (wall - 30 s on a short
wall).
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any, Optional

T0 = time.monotonic()
ICP_CAP_USD = 0.74
CLOSING_RESERVE_USD = 0.04
MAX_INFLIGHT = 6
INFLIGHT_WAIT_S = 90.0
LEGACY_WALL_S = 300.0

# Upper bounds per Deepline operation (USD).  Fixed-price tools use their list price.
DYNAMIC_CMAX_USD = {"exa_search": 0.016, "exa_contents": 0.006, "firecrawl_scrape": 0.008, "exa_answer": 0.007}
DEFAULT_DYNAMIC_CMAX_USD = 0.02
UNRESOLVED_CALL_PAD_USD = DEFAULT_DYNAMIC_CMAX_USD
SNAPSHOT_REFRESH_S = 15.0
SNAPSHOT_MIN_DRIFT_USD = 0.02
# OpenRouter per-token prices (USD) for the models this bundle calls; unknown models use FALLBACK_CALL_CMAX_USD.
MODEL_PRICES = {
    "google/gemini-2.5-flash": (0.30e-6, 2.50e-6, 0.0),
    "google/gemini-2.5-flash-lite": (0.10e-6, 0.40e-6, 0.0),
    "anthropic/claude-sonnet-4.5": (3.0e-6, 15.0e-6, 0.0),
    "perplexity/sonar": (1.0e-6, 1.0e-6, 0.005),
    "openai/gpt-6-luna": (0.10e-6, 0.50e-6, 0.0),
    "openai/gpt-4.1-mini": (0.40e-6, 1.60e-6, 0.0),
}
FALLBACK_CALL_CMAX_USD = 0.10


class Refused(RuntimeError):
    """A provider call the governor did not start (spend cap, paid cutoff or no free slot)."""


# The broker's per-request refusal (lab_arena/broker.py GENERIC_ERRORS: HTTP 403).  It answers an OpenRouter
# request-policy refusal and a Deepline generic_http_request whose target site returned 403; the account still works.
REQUEST_REFUSAL_CODES = frozenset({"provider_request_refused"})
# Deepline tools whose HTTP 403 reports the fetched site, not the account.
SITE_STATUS_TOOLS = frozenset({"generic_http_request"})


def error_code(body: Any) -> str:
    """The broker's error code in a failed reply ({"error": {"code": ...}}), else ''."""

    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else ""


def account_refusal(status: Any, body: Any = None, *, provider: str, tool: str = "") -> bool:
    """Does this failed reply stop every later call on the provider (so the governor may mark it dead)?

    HTTP 401/402 do (the broker's miner_credentials_unavailable, budget_refused and call_refused are 402).  A 403 does
    unless it is a per-request refusal: the provider_request_refused code, or any 403 on a Deepline tool in
    SITE_STATUS_TOOLS."""

    if isinstance(status, bool) or not isinstance(status, int):
        return False
    if error_code(body) in REQUEST_REFUSAL_CODES:
        return False
    if status in (401, 402):
        return True
    if status == 403:
        return not (provider == "deepline" and tool in SITE_STATUS_TOOLS)
    return False


def wall_seconds() -> float:
    raw = str(os.environ.get("LAB_ARENA_WALL_CLOCK_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return LEGACY_WALL_S
    return value if math.isfinite(value) and value >= 30.0 else LEGACY_WALL_S


class Clock:
    """Deadlines as monotonic times measured from process start."""

    def __init__(self, wall: Optional[float] = None, t0: Optional[float] = None) -> None:
        self.t0 = T0 if t0 is None else t0
        self.wall = float(wall if wall is not None else wall_seconds())
        self.compressed = self.wall < 900.0
        self.end = self.t0 + self.wall
        if self.compressed:
            self.paid_cutoff = self.t0 + self.wall * 0.80
            self.final_write = self.t0 + self.wall - 30.0
            self.discovery_end = self.t0 + self.wall * 0.40
        else:
            self.paid_cutoff = self.end - 420.0
            self.final_write = self.end - 180.0
            self.discovery_end = self.t0 + min(self.wall * 0.40, 1500.0)

    def left(self, until: float) -> float:
        return until - time.monotonic()

    def as_dict(self) -> dict[str, Any]:
        now = time.monotonic()
        return {"wall": self.wall, "compressed": self.compressed, "elapsed": round(now - self.t0, 1),
                "paid_cutoff_in": round(self.paid_cutoff - now, 1), "final_write_in": round(self.final_write - now, 1)}


def llm_cmax(model: str, prompt_chars: int, max_tokens: int) -> float:
    prices = MODEL_PRICES.get(str(model or ""))
    if prices is None:
        return FALLBACK_CALL_CMAX_USD
    inp, out, fee = prices
    return round((int(prompt_chars) / 3.0 + 64) * inp + int(max_tokens) * out + fee, 6)


def llm_cost(model: str, usage: Any) -> Optional[float]:
    """The charge a reply reports (usage.cost), else the token count priced from MODEL_PRICES, else None."""

    if not isinstance(usage, dict):
        return None
    raw = usage.get("cost")
    try:
        if raw is not None and float(raw) >= 0.0 and math.isfinite(float(raw)):
            return float(raw)
    except (TypeError, ValueError):
        pass
    prices = MODEL_PRICES.get(str(model or ""))
    try:
        prompt, completion = int(usage.get("prompt_tokens")), int(usage.get("completion_tokens"))
    except (TypeError, ValueError):
        return None
    if prices is None:
        return None
    return round(prompt * prices[0] + completion * prices[1] + prices[2], 6)


class Governor:
    def __init__(self, *, cap_usd: float = ICP_CAP_USD, reserve_usd: float = CLOSING_RESERVE_USD,
                 max_inflight: int = MAX_INFLIGHT, clock: Optional[Clock] = None) -> None:
        self.cap = float(cap_usd)
        self.mode_cap = float(cap_usd)
        self.reserve = float(reserve_usd)
        self.max_inflight = int(max_inflight)
        self.clock = clock
        self.prior = 0.0
        self.snapshot_usd: Optional[float] = None
        self.own_at_snapshot = 0.0
        self.snapshot_reads = 0
        self.last_read = -math.inf
        self.reading = False
        self.own_done = 0.0
        self.inflight: dict[int, float] = {}
        self.calls = 0
        self.refusals: dict[str, int] = {}
        self.dead_providers: set[str] = set()
        self._next = 0
        self._cond = threading.Condition()

    # ---- accounting ----
    def committed(self) -> float:
        with self._cond:
            return self._committed()

    def _committed(self) -> float:
        if self.snapshot_usd is None:
            return self.prior + self.own_done
        return self.snapshot_usd + max(0.0, self.own_done - self.own_at_snapshot)

    def projected(self, extra: float = 0.0) -> float:
        with self._cond:
            return self._committed() + sum(self.inflight.values()) + max(0.0, extra)

    def headroom(self, *, closing: bool = False) -> float:
        with self._cond:
            limit = self.mode_cap - (0.0 if closing else self.reserve)
            return limit - self._committed() - sum(self.inflight.values())

    def set_mode_cap(self, usd: float) -> None:
        with self._cond:
            self.mode_cap = max(0.0, min(self.cap, float(usd)))

    def marker(self) -> float:
        """Own spend booked so far; take it before a snapshot read and pass it to note_snapshot."""

        with self._cond:
            return self.own_done

    def note_snapshot(self, usd: Optional[float], marker: Optional[float] = None) -> None:
        """Re-base committed spend on a host reading taken when own spend was ``marker`` (default: now).  Calls booked
        after the marker are added on top, so a call that settled on the host during the read counts twice, never
        zero times."""

        if usd is None:
            return
        with self._cond:
            self.snapshot_usd = max(0.0, float(usd))
            self.own_at_snapshot = self.own_done if marker is None else min(self.own_done, float(marker))
            self.snapshot_reads += 1
            self._cond.notify_all()

    def mark_dead(self, provider: str) -> None:
        with self._cond:
            self.dead_providers.add(provider)

    def _refuse(self, why: str) -> None:
        self.refusals[why] = self.refusals.get(why, 0) + 1
        raise Refused(why)

    def acquire(self, c_max: float, *, provider: str = "", closing: bool = False,
                wait_s: float = INFLIGHT_WAIT_S) -> int:
        c_max = max(0.0, float(c_max))
        deadline = time.monotonic() + max(0.0, wait_s)
        with self._cond:
            while True:
                if provider and provider in self.dead_providers:
                    self._refuse(f"{provider} unavailable")
                if self.clock is not None and time.monotonic() >= self.clock.paid_cutoff:
                    self._refuse("paid cutoff")
                limit = self.mode_cap - (0.0 if closing else self.reserve)
                if c_max > 0.0 and self._committed() + sum(self.inflight.values()) + c_max > limit + 1e-9:
                    self._refuse("spend cap")
                if len(self.inflight) < self.max_inflight:
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    self._refuse("no free slot")
                self._cond.wait(timeout=min(left, 5.0))
            self._next += 1
            self.inflight[self._next] = c_max
            self.calls += 1
            return self._next

    def release(self, token: int, cost: Optional[float] = None) -> None:
        with self._cond:
            c_max = self.inflight.pop(token, 0.0)
            spent = c_max if cost is None else max(0.0, float(cost))
            self.own_done += spent
            self._cond.notify_all()

    def charge(self, usd: float) -> None:
        with self._cond:
            self.own_done += max(0.0, float(usd))

    def as_dict(self) -> dict[str, Any]:
        with self._cond:
            return {"cap": self.cap, "mode_cap": self.mode_cap, "prior": round(self.prior, 4),
                    "snapshot": None if self.snapshot_usd is None else round(self.snapshot_usd, 4),
                    "snapshot_reads": self.snapshot_reads, "committed": round(self._committed(), 4),
                    "own_done": round(self.own_done, 4), "inflight": len(self.inflight), "calls": self.calls,
                    "refusals": dict(self.refusals), "dead": sorted(self.dead_providers)}


_CURRENT: Optional[Governor] = None
_CURRENT_CLOCK: Optional[Clock] = None


def install(gov: Optional[Governor], clock: Optional[Clock] = None) -> None:
    global _CURRENT, _CURRENT_CLOCK
    _CURRENT, _CURRENT_CLOCK = gov, clock


def current() -> Optional[Governor]:
    return _CURRENT


def read_snapshot() -> Optional[dict]:
    """One host quota snapshot with sourcing cost, or None (never raises)."""

    try:
        import lab_arena_checkpoint  # provided by the sandbox beside the entrypoint
    except ImportError:
        return None
    try:
        snap = lab_arena_checkpoint.quota_usage(include_sourcing_cost=True)
    except Exception:  # noqa: BLE001 - QuotaUnavailable and socket errors
        return None
    return snap if isinstance(snap, dict) else None


def snapshot_spend(snap: Optional[dict]) -> tuple[Optional[float], Optional[int]]:
    """(successful + success_unresolved USD, inflight calls) from a v2 snapshot."""

    cost = snap.get("sourcing_cost") if isinstance(snap, dict) else None
    if not isinstance(cost, dict):
        return None, None
    try:
        usd = (int(cost.get("successful_microusd") or 0) + int(cost.get("success_unresolved_microusd") or 0)) / 1e6
        return usd, int(cost.get("inflight_calls") or 0)
    except (TypeError, ValueError):
        return None, None


def snapshot_committed(snap: Optional[dict]) -> Optional[float]:
    """Successful + unresolved USD from a v2 snapshot plus UNRESOLVED_CALL_PAD_USD per unresolved call, or None."""

    cost = snap.get("sourcing_cost") if isinstance(snap, dict) else None
    usd, _inflight = snapshot_spend(snap)
    if usd is None or not isinstance(cost, dict):
        return None
    try:
        unresolved = max(0, int(cost.get("success_unresolved_calls") or 0))
    except (TypeError, ValueError):
        return None
    return usd + unresolved * UNRESOLVED_CALL_PAD_USD


def refresh(force: bool = False, now: Optional[float] = None) -> bool:
    """Re-base the installed governor on a fresh host snapshot when SNAPSHOT_REFRESH_S have passed and at least
    SNAPSHOT_MIN_DRIFT_USD was booked since the last reading (single-flight; never raises).  True when re-based."""

    gov = _CURRENT
    if gov is None:
        return False
    now = time.monotonic() if now is None else now
    with gov._cond:
        drift = gov.own_done - gov.own_at_snapshot if gov.snapshot_usd is not None else gov.own_done
        if gov.reading or (not force and (now - gov.last_read < SNAPSHOT_REFRESH_S or drift < SNAPSHOT_MIN_DRIFT_USD)):
            return False
        gov.reading, gov.last_read, marker = True, now, gov.own_done
    try:
        usd = snapshot_committed(read_snapshot())
        if usd is None:
            return False
        gov.note_snapshot(usd, marker)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        with gov._cond:
            gov.reading = False


def read_prior(max_wait_s: float = 20.0, sleep=time.sleep) -> tuple[Optional[float], bool]:
    """Earlier attempts' spend: re-read the snapshot until no call is in flight (bounded).  (usd, settled)."""

    deadline = time.monotonic() + max_wait_s
    last: Optional[float] = None
    while True:
        usd, inflight = snapshot_spend(read_snapshot())
        if usd is None:
            return last, False
        last = usd
        if not inflight:
            return usd, True
        if time.monotonic() >= deadline:
            return usd, False
        sleep(2.0)


__all__ = ["Governor", "Clock", "Refused", "account_refusal", "error_code", "install", "current",
           "llm_cmax", "llm_cost", "read_snapshot", "snapshot_spend", "snapshot_committed", "refresh",
           "read_prior", "wall_seconds", "DYNAMIC_CMAX_USD", "MODEL_PRICES",
           "ICP_CAP_USD", "CLOSING_RESERVE_USD", "MAX_INFLIGHT"]
