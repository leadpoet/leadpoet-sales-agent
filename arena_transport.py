"""Credential-free OpenRouter transport over the Arena worker socket.

Adapted from leadpoet/pydantic-harness (MIT, see LICENSE-pydantic-harness).
Only the transport is kept; the tool client lives in arena_tools.py because the
baseline's search_web/fetch_page call ScrapingDog, which miner-funded runs
cannot use.
"""

from __future__ import annotations

import json
import os

import httpx

from . import diagnostics

_ALLOWED_ARENA_HEADERS = frozenset(
    {
        "accept", "accept-encoding", "accept-language", "cache-control",
        "connection", "content-length", "content-type", "date", "expect", "host",
        "http-referer", "keep-alive", "pragma", "te", "user-agent", "x-title",
    }
)
_ALLOWED_OPENROUTER_FIELDS = frozenset(
    {
        "model", "messages", "tools", "tool_choice", "parallel_tool_calls",
        "reasoning", "reasoning_effort", "temperature", "max_tokens", "top_p",
        "stop", "seed", "response_format", "include_reasoning",
    }
)
_ALLOWED_MESSAGE_FIELDS = frozenset({"role", "content", "name", "tool_call_id", "tool_calls"})


def arena_socket_path() -> str:
    """Return the absolute worker socket supplied by the Arena."""

    value = str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip()
    if not value.startswith("/"):
        raise RuntimeError("LAB_ARENA_WORKER_SOCKET is required")
    return value


class ArenaOpenRouterTransport(httpx.AsyncBaseTransport):
    """Send OpenAI SDK requests over the Arena socket in its closed schema."""

    def __init__(self, socket_path: str | None = None, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(uds=socket_path or arena_socket_path())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("OpenRouter request body is invalid") from exc
        if not isinstance(body, dict):
            raise RuntimeError("OpenRouter request body must be an object")
        body = {name: value for name, value in body.items() if name in _ALLOWED_OPENROUTER_FIELDS}
        messages = body.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                for name in list(message):
                    if name not in _ALLOWED_MESSAGE_FIELDS:
                        del message[name]
                for name in ("content", "name", "tool_call_id", "tool_calls"):
                    if message.get(name) is None:
                        message.pop(name, None)
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in _ALLOWED_ARENA_HEADERS and name.lower() not in {"content-length", "transfer-encoding"}
        }
        forwarded = httpx.Request(
            request.method, request.url, headers=headers,
            content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        )
        try:
            response = await self._inner.handle_async_request(forwarded)
        except BaseException as exc:
            diagnostics.record_transport_error(channel="openrouter", detail=type(exc).__name__)
            raise
        diagnostics.record_response(response.status_code, channel="openrouter")
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def arena_openrouter_http_client(timeout: float) -> httpx.AsyncClient:
    """Build the HTTP client used by PydanticAI inside the Arena sandbox."""

    return httpx.AsyncClient(
        transport=ArenaOpenRouterTransport(), timeout=httpx.Timeout(timeout),
        follow_redirects=False, trust_env=False,
    )


__all__ = ["ArenaOpenRouterTransport", "arena_openrouter_http_client", "arena_socket_path"]
