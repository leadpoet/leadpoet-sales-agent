"""The per-ICP sourcing spend guard keeps one qualified company cost-eligible."""

from __future__ import annotations

import sys
import types

from experiments.harness_bakeoff.adapters import pydantic_ai as adapter


class _Usage:
    def __init__(self, cost: float) -> None:
        self.cost = cost
        self.input_tokens = 0
        self.requests = 0
        self.tool_calls = 0


class _Client:
    deepline_calls = 0

    def call(self, name, arguments):
        return {"ok": True, "tool": name}


def _fresh(monkeypatch, cost: float) -> None:
    monkeypatch.setattr(adapter, "_ACTIVE_USAGE", _Usage(cost))
    monkeypatch.setattr(adapter, "_ACTIVE_BUDGET", None)
    adapter._SPEND_CACHE.update(at=-1e9, usd=0.0)


def test_local_estimate_finalizes_and_refuses_paid_research(monkeypatch) -> None:
    monkeypatch.delenv("LAB_ARENA_WORKER_SOCKET", raising=False)
    _fresh(monkeypatch, 0.40)
    assert abs(adapter._sourcing_spend_usd() - 0.40) < 1e-9
    assert not adapter._finalization_due(_Usage(0.40))
    budget = adapter._ToolBudget(_Client(), 30)
    assert budget.call("search_web", {"query": "x"}) == {"ok": True, "tool": "search_web"}

    _fresh(monkeypatch, 0.64)
    assert adapter._finalization_due(_Usage(0.64))
    assert budget.call("search_web", {"query": "x"})["error"].startswith("sourcing budget reached")
    assert budget.page_text("https://example.com/a") is not None  # still under the post-run cap

    _fresh(monkeypatch, 0.71)
    assert budget.page_text("https://example.com/b") is None  # evidence pass keeps it unverified


def test_arena_mode_reads_the_hosts_exact_counter(monkeypatch) -> None:
    fake = types.ModuleType("lab_arena_checkpoint")
    fake.quota_usage = lambda include_sourcing_cost=False: {
        "sourcing_cost": {"successful_microusd": 500_000, "success_unresolved_microusd": 70_000}
    }
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", fake)
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", "/run/lab_arena/worker.sock")
    _fresh(monkeypatch, 0.0)  # the local estimate would say $0.00
    assert abs(adapter._sourcing_spend_usd() - 0.57) < 1e-9
    assert adapter._finalization_due(_Usage(0.0))


def test_arena_mode_falls_back_to_the_estimate_when_the_host_is_silent(monkeypatch) -> None:
    fake = types.ModuleType("lab_arena_checkpoint")

    def unavailable(include_sourcing_cost=False):
        raise RuntimeError("quota unavailable")

    fake.quota_usage = unavailable
    monkeypatch.setitem(sys.modules, "lab_arena_checkpoint", fake)
    monkeypatch.setenv("LAB_ARENA_WORKER_SOCKET", "/run/lab_arena/worker.sock")
    _fresh(monkeypatch, 0.30)
    assert abs(adapter._sourcing_spend_usd() - 0.30) < 1e-9
