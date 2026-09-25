"""The paid financing lookup inside get_company_profile is opt-in."""

from __future__ import annotations

import httpx

import arena_transport


class _Recording(arena_transport.ArenaToolClient):
    def __init__(self) -> None:
        super().__init__(client=httpx.Client())
        self.tools: list[str] = []

    def _deepline(self, tool, payload):
        self.tools.append(tool)
        raise RuntimeError("offline")


def test_profile_skips_financing_by_default() -> None:
    client = _Recording()
    profile = client.get_company_profile({"domain": "example.com"})
    assert "predictleads_company_financing_events" not in client.tools
    assert profile["latest_financing_events"] == []
    assert "financing_lookup" in profile


def test_profile_runs_financing_when_requested() -> None:
    client = _Recording()
    client.get_company_profile({"domain": "example.com", "include_financing": True})
    assert "predictleads_company_financing_events" in client.tools
