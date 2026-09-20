"""Stable public entrypoint for the Leadpoet Arena challenger."""

from typing import Any

from agent.pipeline import get_last_usage as _get_last_usage, run_icp as _run_icp


def run_icp(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Return up to three best-fit companies, ranked best first."""

    return _run_icp(icp)


def get_last_usage() -> dict[str, Any]:
    """Return an isolated copy of usage data from the last run."""

    return _get_last_usage()


__all__ = ["get_last_usage", "run_icp"]
