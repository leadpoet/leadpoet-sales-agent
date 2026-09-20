"""Credential-free provider transport for the Leadpoet agent Arena.

The Arena exposes one HTTP bridge over a Unix socket.  Requests keep the
provider host and path, but carry no provider credential.  The Arena host adds
credentials and enforces its own call, cost, token, and time limits.
"""

from __future__ import annotations

from datetime import date, timedelta
from html.parser import HTMLParser
from contextlib import ExitStack
import copy
import json
import os
import re
import threading
from typing import Any
from agent.safe_urls import urlsplit, urljoin

import httpx

from experiments.harness_bakeoff.models import _public_http_url, validate_companies


_ALLOWED_ARENA_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "connection",
        "content-length",
        "content-type",
        "date",
        "expect",
        "host",
        "http-referer",
        "keep-alive",
        "pragma",
        "te",
        "user-agent",
        "x-title",
    }
)
_ALLOWED_OPENROUTER_FIELDS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning",
        "reasoning_effort",
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "seed",
        "response_format",
        "include_reasoning",
    }
)
_ALLOWED_MESSAGE_FIELDS = frozenset(
    {"role", "content", "name", "tool_call_id", "tool_calls"}
)
_EVENT_TOOLS = {
    "HIRING": "predictleads_company_job_openings",
    "JOBS": "predictleads_company_job_openings",
    "FUNDING": "predictleads_company_financing_events",
    "FINANCING": "predictleads_company_financing_events",
    "PRODUCT_LAUNCH": "predictleads_company_news_events",
    "ACQUISITION": "predictleads_company_news_events",
    "PARTNERSHIP": "predictleads_company_news_events",
    "MARKET_EXPANSION": "predictleads_company_news_events",
    "LEADERSHIP_CHANGE": "predictleads_company_news_events",
    "FACILITY_OPENING": "predictleads_company_news_events",
    "NEWS": "predictleads_company_news_events",
}
_NEWS_CATEGORIES = {
    "PRODUCT_LAUNCH": ["launches"],
    "ACQUISITION": ["acquires", "merges_with", "sells_assets_to"],
    "PARTNERSHIP": ["partners_with"],
    "MARKET_EXPANSION": ["expands_offices_in", "expands_offices_to"],
    "LEADERSHIP_CHANGE": ["hires", "promotes"],
    "FACILITY_OPENING": [
        "expands_facilities",
        "expands_offices_in",
        "expands_offices_to",
        "opens_new_location",
    ],
}
_US_REGIONS = {
    "west coast": ("CA", "OR", "WA"),
    "northeast": ("CT", "ME", "MA", "NH", "NJ", "NY", "PA", "RI", "VT"),
    "midwest": ("IA", "IL", "IN", "KS", "MI", "MN", "MO", "ND", "NE", "OH", "SD", "WI"),
}


def arena_socket_path() -> str:
    """Return the absolute worker socket supplied by the Arena."""

    value = str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip()
    if not value.startswith("/"):
        raise RuntimeError("LAB_ARENA_WORKER_SOCKET is required")
    return value


async def strip_arena_request_headers(request: httpx.Request) -> None:
    """Remove SDK and credential headers before a request reaches the broker."""

    for name in list(request.headers):
        if name.lower() not in _ALLOWED_ARENA_HEADERS:
            del request.headers[name]


def _sanitize_openrouter_request(request: httpx.Request) -> httpx.Request:
    """Return the closed-schema request accepted by both Arena transports."""
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
        if name.lower() in _ALLOWED_ARENA_HEADERS
        and name.lower() not in {"content-length", "transfer-encoding"}
    }
    return httpx.Request(
        request.method,
        request.url,
        headers=headers,
        content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        extensions=dict(request.extensions),
    )


class ArenaCredentialFreeAsyncClient(httpx.AsyncClient):
    """Strip SDK placeholders before either the shim or direct transport."""

    async def send(self, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        return await super().send(_sanitize_openrouter_request(request), *args, **kwargs)


class ArenaOpenRouterTransport(httpx.AsyncBaseTransport):
    """Send OpenAI SDK requests over the Arena socket in its closed schema."""

    def __init__(
        self,
        socket_path: str | None = None,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(
            uds=socket_path or arena_socket_path()
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._inner.handle_async_request(_sanitize_openrouter_request(request))

    async def aclose(self) -> None:
        await self._inner.aclose()


def arena_openrouter_http_client(timeout: float) -> httpx.AsyncClient:
    """Build the HTTP client used by PydanticAI inside the Arena sandbox."""

    return ArenaCredentialFreeAsyncClient(
        transport=ArenaOpenRouterTransport(),
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
        # The optional upstream shim intercepts AsyncClient.send before our
        # custom transport runs.  Strip SDK placeholder credentials at the
        # request-hook stage so both shimmed and direct UDS paths receive the
        # same credential-free OpenRouter request.
        event_hooks={"request": [strip_arena_request_headers]},
    )


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_depth = 0
        self._title_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        normalized = tag.casefold()
        if normalized in {"script", "style"}:
            self._ignored_depth += 1
        elif normalized == "title" and self._ignored_depth == 0:
            self._title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif normalized == "title" and self._title_depth:
            self._title_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = " ".join(str(data).split())
        if text and self._title_depth:
            self.title_parts.append(text)
        elif text:
            self.parts.append(text)


def _page_content(value: str, limit: int) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        without_hidden = re.sub(
            r"<(script|style)\b[^>]*>.*?</\1\s*>",
            " ",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        title_match = re.search(
            r"<title\b[^>]*>(.*?)</title\s*>",
            without_hidden,
            flags=re.IGNORECASE | re.DOTALL,
        )
        title = (
            " ".join(re.sub(r"<[^>]+>", " ", title_match.group(1)).split())[:500]
            if title_match
            else ""
        )
        without_title = re.sub(
            r"<title\b[^>]*>.*?</title\s*>",
            " ",
            without_hidden,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = " ".join(re.sub(r"<[^>]+>", " ", without_title).split())[:limit]
        return title, text
    return " ".join(parser.title_parts)[:500], " ".join(parser.parts)[:limit]


def _evidence_url(value: Any) -> str:
    try:
        return _public_http_url(str(value or ""))
    except ValueError as exc:
        from agent.safe_urls import invalid
        invalid(value,exc);return ""


def _domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlsplit(raw).hostname or "").rstrip(".").removeprefix("www.")
    if not host or "." not in host or len(host) > 253:
        return ""
    return host


def _sql_literal(value: str) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f]", " ", value)[:253]
    return "'" + clean.replace("'", "''") + "'"


def _result_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    tool_response = payload.get("toolResponse")
    if isinstance(tool_response, dict):
        for key in ("rawV2", "raw", "data"):
            value = tool_response.get(key)
            if isinstance(value, dict):
                return value
    result = payload.get("result")
    if isinstance(result, dict):
        data = result.get("data")
        return data if isinstance(data, dict) else result
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:20_000]
    if isinstance(value, list):
        return [_json_safe(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:200]: _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:150]
            if str(key).lower() not in {"api_key", "apikey", "authorization", "token"}
        }
    return str(value)[:2_000]


def _hunter_locations(value: str) -> list[dict[str, str]]:
    normalized = " ".join(str(value or "").lower().replace(",", " ").split())
    for label, states in _US_REGIONS.items():
        if label in normalized:
            return [{"country": "US", "state": state} for state in states]
    if "london" in normalized:
        return [{"country": "GB", "city": "London"}]
    if "united kingdom" in normalized or normalized in {"uk", "great britain"}:
        return [{"country": "GB"}]
    if "united states" in normalized or normalized in {"us", "usa"}:
        return [{"country": "US"}]
    return []


def _project_event_data(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    included: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in payload.get("included") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "")
        identifier = str(raw.get("id") or "")
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        allowed = (
            ("company_name", "domain", "ticker")
            if kind == "company"
            else ("title", "url", "published_at", "author")
        )
        included[(kind, identifier)] = {
            key: _json_safe(attrs.get(key))
            for key in allowed
            if attrs.get(key) not in (None, "", [])
        }

    attribute_names = {
        "amount",
        "amount_normalized",
        "article_sentence",
        "categories",
        "category",
        "confidence",
        "contract_types",
        "effective_date",
        "event",
        "financing_type",
        "financing_type_normalized",
        "first_seen_at",
        "found_at",
        "headcount",
        "job_title",
        "last_seen_at",
        "location",
        "normalized_title",
        "planning",
        "posted_at",
        "product",
        "recognition",
        "salary",
        "seniority",
        "status",
        "summary",
        "title",
        "url",
        "vulnerability",
    }
    items: list[dict[str, Any]] = []
    raw_items = payload.get("data")
    if not isinstance(raw_items, list):
        raw_items = []
    for raw in raw_items[:limit]:
        if not isinstance(raw, dict):
            continue
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        item: dict[str, Any] = {
            "type": str(raw.get("type") or "event"),
            "attributes": {
                key: _json_safe(value)
                for key, value in attrs.items()
                if key in attribute_names and value not in (None, "", [], {})
            },
        }
        relations = raw.get("relationships") if isinstance(raw.get("relationships"), dict) else {}
        related: dict[str, Any] = {}
        for name, relation in relations.items():
            data = relation.get("data") if isinstance(relation, dict) else None
            if not isinstance(data, dict):
                continue
            key = (str(data.get("type") or ""), str(data.get("id") or ""))
            if key in included:
                related[str(name)] = included[key]
        if related:
            item["related"] = related
        items.append(item)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {
        "items": items,
        "returned_count": len(items),
        "available_count": meta.get("count"),
    }


def _exa_rows(raw: Any) -> dict[str, Any]:
    """Normalize a Deepline-wrapped Exa response to {results:[{url,title,date,text,entity}]}."""
    data = raw if isinstance(raw, dict) else {}
    inner = data.get("toolResponse") or data.get("result") or data
    if isinstance(inner, dict) and isinstance(inner.get("rawV2"), dict):
        inner = inner["rawV2"]
    if isinstance(inner, dict) and isinstance(inner.get("data"), dict):
        inner = inner["data"]
    results = inner.get("results") if isinstance(inner, dict) else None
    rows: list[dict[str, Any]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        url = _evidence_url(r.get('url'))
        if not url:
            continue
        entity: dict[str, Any] = {}
        for e in r.get("entities") or []:
            if isinstance(e, dict) and str(e.get("type") or "") == "company":
                entity = dict(e.get("properties") or {})
                break
        rows.append({
            "url": url,
            "title": str(r.get("title") or "")[:200],
            "date": str(r.get("publishedDate") or "")[:10],
            "text": str(r.get("text") or "")[:8000],
            "snippet": str(r.get("highlights") or "")[:1400],
            "links": [{"url": target, "text": ""}
                      for u in (r.get('extras') or {}).get('links', [])[:40] if isinstance(u, str) and (target:=urljoin(url,u))],
            "datePublished": str(r.get("publishedDate") or "")[:10],
            "entity": entity,
        })
    return {"results": rows, "count": len(rows)}


class ArenaToolClient:
    """Implement the public semantic tools through approved Arena operations."""

    def __init__(self, timeout: float = 90.0, client: httpx.Client | None = None):
        self.timeout = max(1.0, min(float(timeout), 120.0))
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=httpx.HTTPTransport(uds=arena_socket_path()),
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=False,
            trust_env=False,
        )
        self.page_cache = {}
        self.row_cache = {}
        self._request_cache = {}
        self._locks = {}
        self._state_lock = threading.RLock()
        self.before_deepline = None
        from agent.v15_optional_provider import OptionalProvider
        self.optional_provider = OptionalProvider()

    def _lock(self, key):
        with self._state_lock:
            return self._locks.setdefault(key, threading.RLock())

    @staticmethod
    def _url(url):
        try:return _public_http_url(str(url))
        except ValueError as exc:
            from agent.safe_urls import invalid
            invalid(url,exc);raise

    def _store_page(self, page, requested=None):
        if not page.get('url'):
            return
        page = copy.deepcopy(page)
        try:
            page['url'] = self._url(page['url'])
        except ValueError:
            return
        page['text'] = page['text'][:8000]
        with self._state_lock:
            self.row_cache[page['url']] = page
        if len(str(page.get('text') or '').strip()) < 80:
            return
        # Preserve all retrieved text. Model-facing projections are separate.
        with self._state_lock:
            for url in (page['url'], requested):
                if url:
                    try:target=self._url(url)
                    except ValueError:continue
                    self.page_cache[target] = page

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._client.request(
            method,
            url,
            params=params,
            json=body,
            timeout=self.timeout,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Arena provider returned HTTP {response.status_code} with invalid JSON"
            ) from exc
        if not response.is_success:
            code = (
                (payload.get("error") or {}).get("code")
                if isinstance(payload.get("error"), dict)
                else ""
            )
            raise RuntimeError(str(code or f"Arena provider returned HTTP {response.status_code}"))
        if not isinstance(payload, dict):
            raise RuntimeError("Arena provider returned a non-object")
        return payload

    def _deepline(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = (tool, json.dumps(payload, sort_keys=True, separators=(',', ':')))
        with self._lock(key):
            if key in self._request_cache:
                return copy.deepcopy(self._request_cache[key])
            ticket = self.before_deepline(tool) if self.before_deepline is not None else None
            result = None
            try:
                result = self._json_request(
                    "POST", f"http://code.deepline.com/api/v2/integrations/{tool}/execute",
                    body={"payload": payload})
            finally:
                if ticket is not None and getattr(self, 'settle_deepline', None):
                    self.settle_deepline(ticket, result)
            if result.get('error') or result.get('success') is False:
                raise RuntimeError('Deepline returned a tool error envelope')
            self._request_cache[key] = result
            return copy.deepcopy(result)

    def search_companies(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        limit = max(1, min(int(arguments.get("limit") or 5), 6))
        industry = str(arguments.get("industry") or "").strip()
        geography = str(arguments.get("geography") or "").strip()
        bands = arguments.get("employee_count") or []
        if not isinstance(bands, list):
            bands = [bands]
        headcount = [
            str(value).replace(",", "").strip()
            for value in bands
            if str(value).strip()
        ]
        context = ". ".join(
            part
            for part in (
                query,
                f"Industry: {industry}" if industry else "",
                f"Headquarters: {geography}" if geography else "",
            )
            if part
        )
        request: dict[str, Any] = {"query": context[:1_000], "limit": limit}
        if headcount:
            request["headcount"] = headcount[:8]
        if industry:
            request["industry"] = {
                "include": [
                    part.strip()
                    for part in re.split(r"[/|]", industry)
                    if part.strip()
                ][:6]
            }
        if locations := _hunter_locations(geography):
            request["headquarters_location"] = {"include": locations}
        data = _result_data(self._deepline("hunter_discover", request))
        rows = data.get("data") or data.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        companies: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            domain = _domain(row.get("domain") or row.get("website"))
            identity = domain or str(
                row.get("organization")
                or row.get("company_name")
                or row.get("name")
                or ""
            ).strip().casefold()
            if not identity or identity in seen:
                continue
            seen.add(identity)
            company: dict[str, Any] = {
                "company_name": str(
                    row.get("organization")
                    or row.get("company_name")
                    or row.get("name")
                    or ""
                )[:300],
                "domain": domain,
            }
            for source, target in (
                ("linkedin_url", "company_linkedin"),
                ("industry", "industry"),
                ("location", "location"),
                ("employee_count", "employee_count"),
                ("headcount", "employee_count"),
            ):
                if row.get(source) not in (None, "", [], {}):
                    company[target] = _json_safe(row[source])
            if domain:
                company["company_website"] = f"https://{domain}/"
            companies.append(company)
            if len(companies) >= limit:
                break
        return {"companies": companies, "count": len(companies)}

    def get_company_profile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        domain = _domain(arguments.get("domain"))
        if not domain:
            raise ValueError("domain is required")
        columns = (
            "normalized_domain, domain, company_name, industry, location, "
            "linkedin_url, employee_count, year_founded, updated_at"
        )
        sql = (
            f"SELECT {columns} FROM companies WHERE normalized_domain = "
            f"{_sql_literal(domain)} LIMIT 3"
        )
        payload = self._deepline("free_simple_company_search", {"sql": sql})
        data = _result_data(payload)
        rows = data.get("rows") or (data.get("data") or {}).get("rows") or []
        if not isinstance(rows, list):
            rows = []
        exact = next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and _domain(row.get("primary_domain") or row.get("domain")) == domain
            ),
            rows[0] if rows else {},
        )
        from agent.v12_llm import enabled
        if enabled('V33_SIZE_PATH'):
            exact=next((r for r in rows if _domain(r.get('normalized_domain') or r.get('domain'))==domain),{})
        return {"domain": domain, "company": exact if isinstance(exact, dict) else {}}

    def get_company_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        domain = _domain(arguments.get("domain"))
        if not domain:
            raise ValueError("domain is required")
        categories = arguments.get("categories") or ["NEWS", "HIRING", "FUNDING"]
        if not isinstance(categories, list):
            categories = [categories]
        tools: list[str] = []
        unsupported: list[str] = []
        for category in categories:
            normalized = str(category).upper()
            tool = _EVENT_TOOLS.get(normalized)
            if tool is None:
                unsupported.append(normalized)
            elif tool not in tools:
                tools.append(tool)
        limit = max(1, min(int(arguments.get("limit") or 5), 5))
        events: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = [
            {"source": category, "error": "use targeted web search"}
            for category in unsupported
        ]
        for tool in tools[:3]:
            request: dict[str, Any] = {
                "company_id_or_domain": domain,
                "page": 1,
                "limit": limit,
            }
            if tool == "predictleads_company_job_openings":
                request.update({"active_only": True, "not_closed": True})
            elif tool == "predictleads_company_news_events":
                news_categories: list[str] = []
                for category in categories:
                    for value in _NEWS_CATEGORIES.get(str(category).upper(), []):
                        if value not in news_categories:
                            news_categories.append(value)
                if news_categories:
                    request["categories"] = news_categories
            try:
                data = _result_data(self._deepline(tool, request))
                events.append(
                    {"source": tool, "data": _project_event_data(data, limit)}
                )
            except Exception as exc:
                errors.append({"source": tool, "error": type(exc).__name__})
        return {"domain": domain, "events": events, "errors": errors}

    def search_web(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        mode = str(arguments.get("mode") or "search").strip().lower()
        if mode not in {"search", "news", "jobs"}:
            raise ValueError("mode must be search, news, or jobs")
        optional = self.optional_provider.search(self, arguments)
        if optional is not None:
            return optional
        recency = arguments.get("recency_days")
        evaluation = date.fromisoformat(
            os.environ.get("BAKEOFF_EVALUATION_DATE")
            or os.environ.get("LAB_ARENA_EVALUATION_DATE")
            or date.today().isoformat()
        )
        payload = {'query': query[:500], 'numResults': max(1, min(int(arguments.get('limit') or 5), 6)),
                   'contents': {'text': {'maxCharacters': 8000}, 'extras': {'links': 20}}}
        if recency not in (None, '') or mode == 'news':
            days = max(1, int(recency or 365))
            payload['startPublishedDate'] = (evaluation-timedelta(days=days)).isoformat()
            payload['endPublishedDate'] = evaluation.isoformat()
        if mode == 'news':
            payload['category'] = 'news'
        if mode == "jobs":
            payload['query'] = query[:470] + ' jobs careers hiring'
        rows = self.exa_search(payload)['results']
        rows = [{**r, 'snippet': r.get('text') or r.get('snippet', '')} for r in rows]
        return {"results": rows, "count": len(rows), "mode": mode}

    def fetch_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        url = self._url(arguments.get('url'))
        with self._lock(('page', url)):
            if url in self.page_cache:
                return copy.deepcopy(self.page_cache[url])
            optional = self.optional_provider.fetch(self, url)
            if optional is not None and len(optional.get('text','').strip()) >= 80:
                return copy.deepcopy(optional)
            try:
                result = self.exa_contents({'urls': [url], 'max_chars': 8000})
                page = next(iter(result.get('results', [])), {})
                if len(str(page.get('text') or '').strip()) >= 80:
                    self._store_page(page, url)
                    return copy.deepcopy(page)
            except Exception as exc:
                from agent.deadline import BudgetExhausted
                if isinstance(exc, BudgetExhausted):
                    raise
            try:
                page = self.firecrawl_scrape({'url': url})
                if len(str(page.get('text') or '').strip()) < 80 or page.get('error'):
                    raise RuntimeError('No usable page content')
                self._store_page(page, url)
                return copy.deepcopy(page)
            except Exception as exc:
                from agent.deadline import BudgetExhausted
                if isinstance(exc, BudgetExhausted):
                    raise
                page = {'url': url, 'text': '', 'error': type(exc).__name__ + ': page unavailable'}
                self.page_cache[url] = page
                return copy.deepcopy(page)

    def plain_homepage(self, arguments):
        """Plain UA through the approved broker operation; no direct networking."""
        url=self._url(arguments.get('url'))
        data=self._deepline('generic_http_request',{'url':url,'method':'GET',
            'headers':{'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36','Accept':'text/html'}})
        for _ in range(6):
            if not isinstance(data,dict):break
            if any(k in data for k in ('status_code','statusCode','http_status','body_text','body')):break
            inner=next((data[k] for k in ('toolResponse','rawV2','raw','result','data','response') if isinstance(data.get(k),dict)),None)
            if inner is None:break
            data=inner
        if not isinstance(data,dict):data={}
        status=data.get('status_code') or data.get('statusCode') or data.get('http_status') or data.get('status')
        try:status=int(status)
        except (TypeError,ValueError):status=None
        body=data.get('body_text') or data.get('body') or data.get('text') or data.get('content') or ''
        from agent.v12_llm import enabled
        if enabled('V23_IDENTITY') and not body and isinstance(data.get('data'),str):
            body=data['data']
        if not isinstance(body,str):body=''
        anchors=[]
        if enabled('V23_IDENTITY'):
            from agent.v21_size import Anchors
            parser=Anchors();parser.feed(body)
            anchors=[{'url':u} for u in dict.fromkeys(parser.urls)]
        title,text=_page_content(body,8000)
        final=self._url(data.get('final_url') or data.get('response_url') or data.get('url') or url)
        headers=data.get('headers') or {}
        headers={str(k).lower():str(v) for k,v in headers.items()} if isinstance(headers,dict) else {}
        return {'url':final,'final_url':final,'title':title,'text':text,'html':body[:32000],
                'plain_status':status,'status_code':status,'headers':headers,'source':'Deepline/generic_http_request',
                **({'links':anchors,'final_url_observed':bool(data.get('final_url') or data.get('response_url') or data.get('url'))} if enabled('V23_IDENTITY') else {}),
                **({'error':'plain_http_unavailable'} if status!=200 else {})}

    def firecrawl_scrape(self, arguments):
        url = self._url(arguments.get('url'))
        options = {'url': url, 'formats': ['markdown', 'links'],
                  'onlyMainContent': False, 'timeout': min(30000, max(1, int(self.timeout*1000))),
                  'proxy': 'basic', 'removeBase64Images': True}
        if arguments.get('fresh'):options['maxAge']=0
        payload = self._deepline('firecrawl_scrape', options)
        data = payload.get('toolResponse') or payload.get('result') or payload
        if isinstance(data, dict):
            data = data.get('rawV2') or data.get('raw') or data
        if isinstance(data, dict) and isinstance(data.get('data'), dict):
            data = data['data']
        if not isinstance(data, dict):
            raise RuntimeError('Invalid page response')
        meta = data.get('metadata') or {}
        status = int(meta.get('statusCode') or 200)
        if not 200 <= status < 300:
            raise RuntimeError(f'Page returned HTTP {status}')
        final = self._url(meta.get('sourceURL') or url)
        text = str(data.get('markdown') or '')[:8000]
        return {'url': final, 'text': text, 'title': str(meta.get('title') or '')[:200],
                'status_code': status, 'source': 'Deepline/firecrawl',
                'site_name': str(meta.get('ogSiteName') or ''),
                'datePublished': str(meta.get('publishedTime') or meta.get('article:published_time') or '')[:10],
                'canonical_url': str(meta.get('canonicalURL') or ''),
                'og_url': str(meta.get('ogUrl') or meta.get('og:url') or ''),
                'final_url': str(meta.get('url') or meta.get('sourceURL') or final),
                'links': [{'url': target, 'text': ''} for u in data.get('links', [])[:40] if isinstance(u, str) and (target:=urljoin(final,u))]}

    # --- Exa tools via Deepline (the broker's DEEPLINE_TOOLS allow-list) -------------
    def exa_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in dict(arguments).items() if v not in (None, "", [], {})}
        payload.setdefault("numResults", 5)
        contents = dict(payload.get('contents') or {})
        requested_text=contents.get('text')
        limit=getattr(self, 'search_text_characters', 8000)
        if getattr(self,'respect_search_text_limit',False) and isinstance(requested_text,dict):
            requested=requested_text.get('maxCharacters')
            if isinstance(requested,int) and not isinstance(requested,bool) and requested>0:limit=min(limit,requested)
        contents['text'] = {'maxCharacters': limit}
        contents.setdefault('extras', {'links': 20})
        payload['contents'] = contents
        result = _exa_rows(self._deepline("exa_search", payload))
        for row in result['results']:
            self._store_page(row)
        # Only projections return to planners; the verification cache is full.
        projected=limit if getattr(self,'respect_search_text_limit',False) and limit<=1500 else 1400
        return {**result, 'results': [{**r, 'text': r['text'][:projected]} for r in result['results']]}

    def exa_company_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in dict(arguments).items() if v not in (None, "", [], {})}
        payload.setdefault("numResults", 5)
        return _exa_rows(self._deepline("exa_company_search", payload))

    def exa_contents(self, arguments: dict[str, Any]) -> dict[str, Any]:
        urls = list(dict.fromkeys(self._url(u) for u in arguments.get('urls', [])))[:5]
        if not urls:
            return {'results': [], 'count': 0}
        limit=arguments.get('max_chars',8000)
        limit=max(1,min(8000,limit)) if isinstance(limit,int) and not isinstance(limit,bool) else 8000
        with ExitStack() as stack:
            for url in sorted(urls):
                stack.enter_context(self._lock(('page', url)))
            missing = [u for u in urls if u not in self.page_cache]
            if missing:
                result = _exa_rows(self._deepline('exa_contents', {'urls': missing,
                                   'text': {'maxCharacters': limit}, 'extras': {'links': 20}}))
                for row in result['results']:
                    requested = missing[0] if len(missing) == 1 and len(result['results']) == 1 else None
                    self._store_page(row, requested)
            rows = [copy.deepcopy(self.page_cache[u]) for u in urls
                    if u in self.page_cache and self.page_cache[u].get('text')]
            return {'results': rows, 'count': len(rows)}

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name in ("harvestapi_search_leads", "harvestapi_get_profile", "harvestapi_get_company"):
            return self._deepline(name, arguments)
        if name == "submit_companies":
            companies = validate_companies(arguments.get("companies"), max_companies=5)
            return {"companies": companies}
        if name not in {
            "search_companies",
            "get_company_profile",
            "get_company_events",
            "search_web",
            "fetch_page",
            "exa_search",
            "exa_company_search",
            "exa_contents",
            "firecrawl_scrape",
            "plain_homepage",
        }:
            raise ValueError(f"unknown tool: {name}")
        return getattr(self, name)(arguments)


__all__ = [
    "ArenaOpenRouterTransport",
    "ArenaToolClient",
    "arena_openrouter_http_client",
    "arena_socket_path",
    "strip_arena_request_headers",
]
