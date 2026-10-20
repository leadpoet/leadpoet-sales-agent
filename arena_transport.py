"""The Arena worker socket: provider calls travel over it without credentials.

The host maps each HTTP request on the socket to one closed operation and refuses any header outside its allowlist
(`forbidden_header` for a credential header such as Authorization, `unknown_header` for anything else), so every
request sent here passes through ``ArenaSocketTransport``, which keeps only the allowlisted headers.
"""

from __future__ import annotations

import os
import hashlib
from typing import Any, Iterable, Mapping

import httpx

from . import arena_training

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
        response = self._inner.handle_request(request)
        if not 200 <= response.status_code < 300:
            return response
        identity = response.headers.get("x-leadpoet-call-identity", "")
        if response.is_stream_consumed:
            # A test or custom transport may return an already-read Response.
            try:
                body = response.content
            except httpx.ResponseNotRead:
                arena_training.provider_incomplete()
            else:
                arena_training.provider_response(
                    response.status_code, identity, "sha256:" + hashlib.sha256(body).hexdigest(), len(body))
        else:
            response.stream = _ReceiptStream(response.stream, response.status_code, identity)
        return response

    def close(self) -> None:
        self._inner.close()


class _ReceiptStream(httpx.SyncByteStream):
    """Hash raw response chunks and acknowledge only after their iterator ends normally."""

    def __init__(self, inner: httpx.SyncByteStream, status: int, identity: str) -> None:
        self._inner, self._status, self._identity = inner, status, identity
        self._digest = hashlib.sha256()
        self._size = 0
        self._complete = False
        self._closed = False

    def __iter__(self):
        try:
            for chunk in self._inner:
                self._digest.update(chunk)
                self._size += len(chunk)
                yield chunk
            self._complete = True
            arena_training.provider_response(
                self._status, self._identity, "sha256:" + self._digest.hexdigest(), self._size)
        finally:
            if not self._complete and not self._closed:
                self._closed = True
                arena_training.provider_incomplete()

    def close(self) -> None:
        if not self._complete and not self._closed:
            self._closed = True
            arena_training.provider_incomplete()
        self._inner.close()


def socket_client(timeout: float, socket_path: str | None = None) -> httpx.Client:
    """An httpx client bound to the worker socket (no proxy environment, no redirects, allowlisted headers)."""

    return httpx.Client(transport=ArenaSocketTransport(socket_path), timeout=httpx.Timeout(timeout),
                        follow_redirects=False, trust_env=False)


__all__ = ["ALLOWED_REQUEST_HEADERS", "ArenaSocketTransport", "allowed_headers", "arena_socket_path", "socket_client"]
