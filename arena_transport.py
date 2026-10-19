"""The Arena worker socket: provider calls travel over it without credentials.

The host maps each HTTP request on the socket to one closed operation and refuses any header outside its allowlist
(`forbidden_header` for a credential header such as Authorization, `unknown_header` for anything else), so every
request sent here passes through ``ArenaSocketTransport``, which keeps only the allowlisted headers.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

import httpx

# lab_arena.operations.ALLOWED_REQUEST_HEADERS
ALLOWED_REQUEST_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "cache-control", "connection", "content-length", "content-type",
    "date", "expect", "host", "http-referer", "keep-alive", "pragma", "te", "user-agent", "x-title",
})


def arena_socket_path() -> str:
    """Return the absolute worker socket supplied by the Arena."""

    value = str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip()
    if not value.startswith("/"):
        raise RuntimeError("LAB_ARENA_WORKER_SOCKET is required")
    return value


def allowed_headers(headers: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> list[tuple[str, str]]:
    """The subset of ``headers`` the host accepts (credential and unknown headers removed)."""

    items = headers.items() if hasattr(headers, "items") else headers
    return [(str(name), str(value)) for name, value in items if str(name).strip().lower() in ALLOWED_REQUEST_HEADERS]


class ArenaSocketTransport(httpx.BaseTransport):
    """An httpx transport over the worker socket that sends only allowlisted headers."""

    def __init__(self, socket_path: str | None = None, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner if inner is not None else httpx.HTTPTransport(uds=socket_path or arena_socket_path())

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        kept = allowed_headers(request.headers.multi_items())
        if len(kept) != len(request.headers.multi_items()):
            request.headers = httpx.Headers(kept)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def socket_client(timeout: float, socket_path: str | None = None) -> httpx.Client:
    """An httpx client bound to the worker socket (no proxy environment, no redirects, allowlisted headers)."""

    return httpx.Client(transport=ArenaSocketTransport(socket_path), timeout=httpx.Timeout(timeout),
                        follow_redirects=False, trust_env=False)


__all__ = ["ALLOWED_REQUEST_HEADERS", "ArenaSocketTransport", "allowed_headers", "arena_socket_path", "socket_client"]
