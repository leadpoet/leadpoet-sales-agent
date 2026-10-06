"""Deepline-only sourcing tools over the Arena worker socket.

Miner-funded runs hold two credentials: OpenRouter and Deepline
(lab_arena/credentials.py RUNTIME_PROVIDERS).  The public baseline routes
search_web and fetch_page through api.scrapingdog.com, which a miner run
cannot use -- so here every tool is a Deepline integration:

  search_companies     hunter_discover
  get_company_profile  free_simple_company_search
  get_company_events   predictleads_company_{job_openings,financing_events,news_events}
  search_web           exa_search  (with text, so results already carry page text)
  fetch_page           exa_contents, then contextdev_get_web_scrape_markdown
                       (free, fixed price), then firecrawl_scrape as last resort

Every page fetched is cached by URL so the verification pass never spends a
second call on the same evidence, and a hard call budget keeps the run under
the Arena's 30-call Deepline quota per ICP.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from . import diagnostics
from . import scorer_mirror as sm
from .arena_transport import arena_socket_path

DEEPLINE_QUOTA_PER_ICP = 200
DEFAULT_CALL_BUDGET = 27

CONTEXTDEV_TOOL = "contextdev_get_web_scrape_markdown"
CONTEXTDEV_WEB_SEARCH = "contextdev_post_web_search"
CONTEXTDEV_NEWS_SEARCH = "contextdev_post_news_search"
HARVEST_PROFILE_TOOL = "harvestapi_get_profile"
_LINKEDIN_PERSON_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9%._~-]{1,120})/?", re.I)
DEEPLINE_USD_PER_CREDIT = 0.10
_DEEPLINE_FIXED_CREDITS = {
    CONTEXTDEV_TOOL: 0, CONTEXTDEV_WEB_SEARCH: 0, CONTEXTDEV_NEWS_SEARCH: 0,
    "free_simple_company_search": 0, "generic_http_request": 0, "hunter_discover": 0,
    "bounceban_get_single_status": 0, "exa_answer": 0.07, "harvestapi_get_company": 0.03,
    "harvestapi_search_leads": 0.7, "zerobounce_validate": 0.28, "bounceban_verify_single": 0.06,
    "harvestapi_get_job": 0.01, "harvestapi_get_post": 0.03,
    "predictleads_company_financing_events": 0.56, "predictleads_company_job_openings": 0.56,
    "predictleads_company_news_events": 0.56,
}
FIXED_PRICES_USD = {tool: round(float(credits) * DEEPLINE_USD_PER_CREDIT, 6)
                    for tool, credits in _DEEPLINE_FIXED_CREDITS.items()}
DYNAMIC_PRICE_ESTIMATE_USD = 0.02
SETTLED_MARGIN_USD = 0.02
SPEND_REFRESH_SECONDS = 10.0
PERIODIC_REFRESH_SECONDS = 45.0
SNAPSHOT_STALE_SECONDS = 5.0

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_strategy() -> dict[str, Any]:
    """The per-submission strategy.json that arena_pack.py writes into the bundle."""

    try:
        with open(os.path.join(_HERE, "strategy.json"), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


STRATEGY = load_strategy()


def paced(key: str, default: int, low: int, high: int, env: str = "") -> int:
    """strategy.json[key], overridable by ``env`` for local runs, clamped to [low, high]."""

    value = STRATEGY.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        default = int(value)
    if env:
        try:
            default = int(os.environ.get(env, str(default)))
        except (TypeError, ValueError):
            pass
    return max(low, min(high, default))


PACING = {
    "call_budget": paced("call_budget", DEFAULT_CALL_BUDGET, 2, DEEPLINE_QUOTA_PER_ICP, "ARENA_CALL_BUDGET"),
    "discover_limit": paced("discover_limit", 6, 1, 10, "ARENA_DISCOVER_LIMIT"),
    "events_limit": paced("events_limit", 3, 1, 5, "ARENA_EVENTS_LIMIT"),
    "search_web_limit": paced("search_web_limit", 4, 1, 6, "ARENA_SEARCH_WEB_LIMIT"),
    "search_web_text_chars": paced("search_web_text_chars", 1400, 300, 3000, "ARENA_SEARCH_WEB_TEXT_CHARS"),
    "fetch_page_max_chars": paced("fetch_page_max_chars", 3500, 1000, 6000, "ARENA_FETCH_PAGE_MAX_CHARS"),
    "free_fetch_first": paced("free_fetch_first", 1, 0, 1, "ARENA_FREE_FETCH_FIRST"),
    "free_search_first": paced("free_search_first", 0, 0, 1, "ARENA_FREE_SEARCH_FIRST"),
}

_EVENT_TOOLS = {
    "HIRING": "predictleads_company_job_openings", "JOBS": "predictleads_company_job_openings",
    "FUNDING": "predictleads_company_financing_events", "FINANCING": "predictleads_company_financing_events",
    "PRODUCT_LAUNCH": "predictleads_company_news_events", "ACQUISITION": "predictleads_company_news_events",
    "PARTNERSHIP": "predictleads_company_news_events", "MARKET_EXPANSION": "predictleads_company_news_events",
    "LEADERSHIP_CHANGE": "predictleads_company_news_events", "FACILITY_OPENING": "predictleads_company_news_events",
    "NEWS": "predictleads_company_news_events",
}
_NEWS_CATEGORIES = {
    "PRODUCT_LAUNCH": ["launches"], "ACQUISITION": ["acquires", "merges_with", "sells_assets_to"],
    "PARTNERSHIP": ["partners_with"], "MARKET_EXPANSION": ["expands_offices_in", "expands_offices_to"],
    "LEADERSHIP_CHANGE": ["hires", "promotes"],
    "FACILITY_OPENING": ["expands_facilities", "expands_offices_in", "expands_offices_to", "opens_new_location"],
}
_US_REGIONS = {
    "west coast": ("CA", "OR", "WA"),
    "northeast": ("CT", "ME", "MA", "NH", "NJ", "NY", "PA", "RI", "VT"),
    "midwest": ("IA", "IL", "IN", "KS", "MI", "MN", "MO", "ND", "NE", "OH", "SD", "WI"),
    "southwest": ("AZ", "NM", "OK", "TX"),
    "south": ("AL", "AR", "DE", "DC", "FL", "GA", "KY", "LA", "MD", "MS", "NC", "OK",
              "SC", "TN", "TX", "VA", "WV"),
}
_COUNTRY_ALIASES = {
    "US": ("united states of america", "united states", "u s a", "usa", "u s", "us"),
    "GB": ("united kingdom", "great britain", "u k", "uk"),
    "CA": ("canada",),
    "DE": ("germany",),
    "AU": ("australia",),
    "IE": ("ireland",),
    "SG": ("singapore",),
    "NZ": ("new zealand",),
}
_COUNTRY_CITIES = {
    ("CA", "toronto"): "Toronto",
    ("CA", "london"): "London",
    ("GB", "london"): "London",
    ("AU", "sydney"): "Sydney",
}
_HUNTER_HEADCOUNT = {
    "2-10": "1-10", "11-50": "11-50", "51-200": "51-200",
    "201-500": "201-500", "501-1,000": "501-1000",
    "1,001-5,000": "1001-5000", "5,001-10,000": "5001-10000",
    "10,001+": "10001+",
}


class BudgetExhausted(RuntimeError):
    """A refused call.  ``calls_left`` is what the model should be told remains.

    The TIME deadline and the CALL ceiling are two separate budgets, and only
    the call ceiling is what ``ArenaTools.remaining()`` counts.  When the
    deadline trips, remaining() still reports whatever the call budget had
    left, so the model was handed {"error": "time budget exhausted",
    "calls_left": 19} and read it, correctly, as a contradiction -- ICP 016,
    in its own words: "the tool's message is a bit strange since it says there
    are still 19 calls left but is marked as exhausted" (LAB-LOG #283).
    No later call can succeed once the deadline has passed, so the deadline
    raise carries an explicit 0 and the call-ceiling raise leaves this None to
    fall back to the live counter.
    """

    def __init__(self, message, *, calls_left=None):
        super().__init__(message)
        self.calls_left = calls_left


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self.title_parts: list[str] = []
        self._ignored = 0
        self._title = 0

    def handle_starttag(self, tag, attrs):
        t = tag.casefold()
        if t in {"script", "style", "noscript"}:
            self._ignored += 1
        elif t == "title" and not self._ignored:
            self._title += 1
        elif t == "a" and not self._ignored and len(self.links) < 400:
            href = dict(attrs).get("href")
            if isinstance(href, str):
                self.links.append(href)

    def handle_endtag(self, tag):
        t = tag.casefold()
        if t in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1
        elif t == "title" and self._title:
            self._title -= 1

    def handle_data(self, data):
        if self._ignored:
            return
        text = " ".join(str(data).split())
        if text and self._title:
            self.title_parts.append(text)
        elif text:
            self.parts.append(text)


def html_to_text(html: str, limit: int) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        stripped = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", html, flags=re.I | re.S)
        return "", " ".join(re.sub(r"<[^>]+>", " ", stripped).split())[:limit]
    return " ".join(parser.title_parts)[:500], " ".join(parser.parts)[:limit]


def _domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlsplit(raw).hostname or "").rstrip(".").removeprefix("www.")
    except ValueError:
        return ""
    return host if host and "." in host and len(host) <= 253 else ""


UNFETCHABLE_HOSTS = ("reuters.com", "bloomberg.com", "wsj.com", "ft.com", "nytimes.com", "forbes.com",
                     "businessinsider.com", "barrons.com", "economist.com", "seekingalpha.com", "washingtonpost.com",
                     "morningstar.com", "marketscreener.com", "streetinsider.com")


def fetchable(url: Any) -> bool:
    """False for evidence URLs on hosts the judge cannot fetch as plain text."""

    host = _domain(url)
    return bool(host) and not any(host == h or host.endswith("." + h) for h in UNFETCHABLE_HOSTS)


def _sql_literal(value: str) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f]", " ", value)[:253]
    return "'" + clean.replace("'", "''") + "'"


def _result_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    tool_response = payload.get("toolResponse")
    if isinstance(tool_response, dict):
        for key in ("rawV2", "raw", "data"):
            if isinstance(tool_response.get(key), dict):
                return tool_response[key]
    result = payload.get("result")
    if isinstance(result, dict):
        data = result.get("data")
        return data if isinstance(data, dict) else result
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _result_list(payload: Any) -> list[Any]:
    """The list a Deepline tool returned, whatever envelope it came in.

    contextdev_post_news_search answers with a BARE list under result.data, which
    ``_result_data`` (dict-only) turns into {} -- so a list has to be unwrapped on
    its own (LAB-LOG #306).
    """

    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("result", "toolResponse"):
        inner = payload.get(key)
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            for sub in ("data", "raw", "rawV2", "results", "items"):
                if isinstance(inner.get(sub), list):
                    return inner[sub]
                if isinstance(inner.get(sub), dict) and isinstance(inner[sub].get("results"), list):
                    return inner[sub]["results"]
    for key in ("data", "results", "items"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def _safe(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:20_000]
    if isinstance(value, list):
        return [_safe(v, depth + 1) for v in value[:100]]
    if isinstance(value, dict):
        return {str(k)[:200]: _safe(v, depth + 1) for k, v in list(value.items())[:150]
                if str(k).lower() not in {"api_key", "apikey", "authorization", "token"}}
    return str(value)[:2000]


def _hunter_locations(value: str) -> list[dict[str, str]]:
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())
    padded = f" {normalized} "

    def mentions(label: str) -> bool:
        return f" {label} " in padded

    countries = [code for code, aliases in _COUNTRY_ALIASES.items()
                 if any(mentions(alias) for alias in aliases)]
    if len(countries) != 1:
        return []
    country = countries[0]

    matched_aliases = [alias for alias in _COUNTRY_ALIASES[country] if mentions(alias)]
    country_alias = max(matched_aliases, key=len)
    before, _, after = padded.partition(f" {country_alias} ")
    remainder = " ".join(f"{before} {after}".split())
    words = set(remainder.split())
    ambiguous = bool(words & {"and", "or", "outside", "except", "excluding", "not"})

    if remainder.startswith(("or ", "and ")) or remainder.endswith((" or", " and")):
        return []

    if country == "US" and not ambiguous:
        region_aliases = {
            "west coast": "west coast", "westcoast": "west coast",
            "northeast": "northeast", "north east": "northeast",
            "midwest": "midwest", "mid west": "midwest",
            "southwest": "southwest", "south west": "southwest",
            "south": "south",
        }
        region = region_aliases.get(remainder)
        if region is not None:
            states = _US_REGIONS[region]
            return [{"country": "US", "state": state} for state in states]

    if not ambiguous:
        city_remainders = {
            ("CA", "toronto"): "Toronto",
            ("CA", "london"): "London",
            ("CA", "london ontario"): "London",
            ("CA", "ontario london"): "London",
            ("GB", "london"): "London",
            ("AU", "sydney"): "Sydney",
        }
        city = city_remainders.get((country, remainder))
        if city is not None:
            return [{"country": country, "city": city}]
    return [{"country": country}]


def _evidence_url(value: Any, *, base_url: str = "") -> str:
    try:
        raw = str(value or "")
        return sm.public_http_url(urljoin(base_url, raw) if base_url else raw)
    except ValueError:
        return ""


class Page:
    __slots__ = ("url", "final_url", "title", "text", "source", "ok", "error", "links")

    def __init__(self, url: str, *, final_url: str = "", title: str = "", text: str = "",
                 source: str = "", ok: bool = False, error: str = "") -> None:
        self.url, self.final_url, self.title, self.text = url, final_url or url, title, text
        self.source, self.ok, self.error = source, ok, error
        self.links: list[str] = []

    def has_linkedin_company_link(self) -> bool:
        return any("linkedin.com/company/" in link.lower() for link in self.links)

    def as_dict(self, max_chars: int = PACING["fetch_page_max_chars"]) -> dict[str, Any]:
        return {"url": self.url, "final_url": self.final_url, "title": self.title,
                "text": self.text[:max_chars], "source": self.source, "ok": self.ok, "error": self.error}


class ArenaTools:
    """Deepline tools with a page cache and a hard call budget."""

    def __init__(self, *, timeout: float = 60.0, call_budget: int = PACING["call_budget"],
                 client: httpx.Client | None = None, post: Optional[Callable[[str, dict], dict]] = None) -> None:
        self.timeout = max(1.0, min(float(timeout), 120.0))
        self.call_budget = int(call_budget)
        self.calls = 0
        self.deadline: float | None = None
        self.calls_by_tool: dict[str, int] = {}
        self.discovered = 0
        self.discovered_bucket_ok = 0
        self.discovered_with_linkedin = 0
        self.pages: dict[str, Page] = {}
        self._link_enrichment_attempted: set[str] = set()
        self.infra_fault: str | None = None
        self._lock = threading.Lock()
        self._post = post
        self.icp_buckets: list[str] = []
        self.trace: list[dict[str, Any]] = []
        self.spend_cap_usd: float | None = None
        self.external_spend_usd = 0.0
        self._confirmed_spend: tuple[float, float] | None = None
        self.spend_refresh: Optional[Callable[[], Any]] = None
        self._last_spend_refresh = 0.0
        self._ledger_log: list[tuple[float, float]] = []
        self._last_host: Optional[tuple[float, float]] = None
        self.spend_refusals = 0
        self._owns_client = client is None and post is None
        self._client = client if client is not None else (
            None if post is not None else httpx.Client(
                transport=httpx.HTTPTransport(uds=arena_socket_path()),
                timeout=httpx.Timeout(self.timeout), follow_redirects=False, trust_env=False,
            )
        )

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def set_icp(self, icp: Any) -> None:
        """Remember the ICP's exact buckets so discovery can flag off-bucket domains."""

        try:
            self.icp_buckets = sm.icp_buckets(icp) if isinstance(icp, dict) else []
        except Exception:
            self.icp_buckets = []

    def _weight(self, url: str, company_website: str = "") -> float:
        return sm.SOURCE_MULTIPLIERS.get(sm.evidence_source(url, company_website), 0.5)

    def remaining(self) -> int:
        return max(0, self.call_budget - self.calls)


    def snapshot(self) -> dict[str, Any]:
        """The research state at one instant, JSON-safe, for a paired replay (#317 addendum).

        Everything a second chance could read: the trace, the page cache with its
        full text, the call counters and the spend ledger.  The harness writes it
        only when ARENA_EMPTY_SNAPSHOT_DIR is set (local runs); it is never an
        input to any gate and never emitted in the sandbox.
        """

        deadline = self.deadline
        return {
            "calls": int(self.calls),
            "call_budget": getattr(self, "call_budget", None),
            "calls_by_tool": dict(self.calls_by_tool),
            "calls_left": int(self.remaining()),
            "seconds_left": (round(float(deadline) - time.monotonic(), 1)
                             if isinstance(deadline, (int, float)) and not isinstance(deadline, bool) else None),
            "spend_usd": round(float(self.spend_usd()), 4),
            "spend_cap_usd": self.spend_cap_usd,
            "trace": [dict(entry) for entry in self.trace],
            "pages": {url: {"final_url": page.final_url, "title": page.title, "text": page.text,
                            "source": page.source, "ok": page.ok, "error": page.error}
                      for url, page in self.pages.items()},
        }

    def _deepline(self, tool: str, payload: dict[str, Any], timeout: Optional[float] = None) -> dict[str, Any]:
        timeout = self.timeout if timeout is None else max(1.0, min(float(timeout), self.timeout))
        price = self.tool_price_usd(tool)
        if price > 0.0 and not self.paid_allowed(price):
            self.maybe_refresh_spend()
        elif price > 0.0:
            self.maybe_refresh_spend(PERIODIC_REFRESH_SECONDS)
        with self._lock:
            if self.deadline is not None and time.monotonic() + timeout > self.deadline:
                raise BudgetExhausted("time budget exhausted", calls_left=0)
            if self.calls >= self.call_budget:
                raise BudgetExhausted(f"provider-call budget of {self.call_budget} exhausted")
            price = self.tool_price_usd(tool)
            if price > 0.0 and not self.paid_allowed(price):
                self.spend_refusals += 1
                raise BudgetExhausted("spend budget exhausted: %.2f of %.2f USD on this ICP; free tools still work"
                                      % (self.spend_usd(), self.spend_cap_usd or 0.0))
            self.calls += 1
            self.calls_by_tool[tool] = self.calls_by_tool.get(tool, 0) + 1
            self._log_ledger()
        if self._post is not None:
            return self._post(tool, payload)
        assert self._client is not None
        try:
            response = self._client.post(
                f"http://code.deepline.com/api/v2/integrations/{tool}/execute",
                json={"payload": payload}, timeout=timeout,
            )
        except BaseException as exc:
            self._note_transport(exc)
            raise
        try:
            body = response.json()
        except ValueError as exc:
            self._note_infra(response.status_code)
            raise RuntimeError(f"Deepline {tool} returned HTTP {response.status_code} with invalid JSON") from exc
        if not response.is_success:
            code = (body.get("error") or {}).get("code") if isinstance(body, dict) and isinstance(body.get("error"), dict) else ""
            if code != "provider_request_refused":
                self._note_infra(response.status_code, code)
            raise RuntimeError(str(code or f"Deepline {tool} returned HTTP {response.status_code}"))
        return body if isinstance(body, dict) else {}

    def _note_transport(self, exc: BaseException) -> None:
        """A call that never produced a status; never let the note fail the call.

        NOT an infra fault: no status came back, so nothing proves the host has an
        infrastructure row, and `_no_companies` must keep its round-safe raise
        (model_error, which cannot cancel the round and earns the confirmation
        attempt).  What this buys is a readable record instead of silence.
        """

        try:
            diagnostics.record_transport_error(channel="deepline", detail=type(exc).__name__)
        except Exception:
            pass

    def _note_infra(self, status: Any = None, code: Any = None) -> None:
        """Remember a host fault on the tool channel; never let it fail the call."""

        try:
            reason = diagnostics.record_response(status, code, channel="deepline")
            if reason and self.infra_fault is None:
                self.infra_fault = reason
        except Exception:
            pass

    def search_companies(self, query: str, *, industry: str = "", geography: str = "",
                         employee_count: list[str] | None = None,
                         limit: int = PACING["discover_limit"]) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("query is required")
        limit = max(1, min(int(limit or PACING["discover_limit"]), 10))
        requested = [str(value).strip() for value in (employee_count or [])
                     if str(value).strip() in _HUNTER_HEADCOUNT]
        if self.icp_buckets:
            narrowed = [value for value in requested if value in self.icp_buckets]
            requested_buckets = narrowed or list(self.icp_buckets)
        else:
            requested_buckets = requested
        headcount: list[str] = []
        for value in requested_buckets:
            band = _HUNTER_HEADCOUNT.get(value)
            if band and band not in headcount:
                headcount.append(band)
        constraints = [
            f"Industry: {str(industry).strip()[:250]}" if industry else "",
            f"Headquarters: {str(geography).strip()[:250]}" if geography else "",
            f"Company size: {', '.join(headcount)}" if headcount else "",
        ]
        suffix = ". ".join(part for part in constraints if part)
        free_limit = max(0, 1000 - len(suffix) - (2 if suffix else 0))
        context = query[:free_limit]
        if suffix:
            context = f"{context}. {suffix}" if context else suffix
        request: dict[str, Any] = {"query": context, "limit": limit}
        if headcount:
            request["headcount"] = headcount[:8]
        if industry:
            request["industry"] = {"include": [p.strip() for p in re.split(r"[/|]", industry) if p.strip()][:6]}
        locations = _hunter_locations(geography)
        if locations:
            request["headquarters_location"] = {"include": locations}
        data = _result_data(self._deepline("hunter_discover", request))
        rows = data.get("data") or data.get("rows") or []
        companies: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            domain = _domain(row.get("domain") or row.get("website"))
            name = str(row.get("organization") or row.get("company_name") or row.get("name") or "").strip()
            identity = domain or name.casefold()
            if not identity or identity in seen:
                continue
            seen.add(identity)
            company: dict[str, Any] = {"company_name": name[:200], "domain": domain}
            if domain:
                company["company_website"] = f"https://{domain}/"
            for src, dst in (("linkedin_url", "company_linkedin"), ("industry", "industry"),
                             ("location", "location"), ("employee_count", "employee_count"),
                             ("headcount", "headcount"), ("description", "description"),
                             ("founded", "founded"), ("year_founded", "founded")):
                if row.get(src) not in (None, "", [], {}):
                    company[dst] = _safe(row[src])
            bucket = sm.any_bucket(company.get("employee_count")) or sm.any_bucket(company.get("headcount"))
            if bucket:
                company["employee_bucket"] = bucket
                if self.icp_buckets:
                    company["bucket_ok"] = bucket in self.icp_buckets
            companies.append(company)
            if len(companies) >= limit:
                break
        self.discovered += len(companies)
        self.discovered_bucket_ok += sum(1 for c in companies if c.get("bucket_ok") is True)
        self.discovered_with_linkedin += sum(
            1 for c in companies if str(c.get("company_linkedin") or "").strip()
        )
        result: dict[str, Any] = {"companies": companies, "count": len(companies)}
        meta = data.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("filters"), dict):
            result["catalog_filters"] = _safe(meta["filters"])
        return result

    def get_company_profile(self, domain: str) -> dict[str, Any]:
        domain = _domain(domain)
        if not domain:
            raise ValueError("domain is required")
        columns = ("normalized_domain, domain, company_name, industry, location, linkedin_url, "
                   "employee_count, year_founded, updated_at")
        sql = f"SELECT {columns} FROM companies WHERE normalized_domain = {_sql_literal(domain)} LIMIT 3"
        data = _result_data(self._deepline("free_simple_company_search", {"sql": sql}))
        rows = data.get("rows") or (data.get("data") or {}).get("rows") or []
        rows = rows if isinstance(rows, list) else []
        exact = next((r for r in rows if isinstance(r, dict) and _domain(r.get("primary_domain") or r.get("domain")) == domain),
                     rows[0] if rows and isinstance(rows[0], dict) else {})
        profile = _safe(exact) if isinstance(exact, dict) else {}
        bucket = sm.any_bucket(profile.get("employee_count")) if isinstance(profile, dict) else ""
        return {"domain": domain, "company": profile, "employee_bucket": bucket}

    def get_company_events(self, domain: str, *, categories: list[str] | None = None,
                           limit: int = PACING["events_limit"]) -> dict[str, Any]:
        domain = _domain(domain)
        if not domain:
            raise ValueError("domain is required")
        categories = [str(c).upper() for c in (categories or ["NEWS", "HIRING", "FUNDING"])]
        tools: list[str] = []
        unsupported: list[str] = []
        for category in categories:
            tool = _EVENT_TOOLS.get(category)
            if tool is None:
                unsupported.append(category)
            elif tool not in tools:
                tools.append(tool)
        limit = max(1, min(int(limit or PACING["events_limit"]), 5))
        events: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = [{"source": c, "error": "use search_web"} for c in unsupported]
        for tool in tools[:3]:
            request: dict[str, Any] = {"company_id_or_domain": domain, "page": 1, "limit": limit}
            if tool == "predictleads_company_job_openings":
                request.update({"active_only": True, "not_closed": True})
            elif tool == "predictleads_company_news_events":
                news: list[str] = []
                for category in categories:
                    for value in _NEWS_CATEGORIES.get(category, []):
                        if value not in news:
                            news.append(value)
                if news:
                    request["categories"] = news
            try:
                data = _result_data(self._deepline(tool, request))
                projected = _project_events(data, limit)
                site = f"https://{domain}/"
                for item in projected.get("items") or []:
                    url = (item.get("attributes") or {}).get("url") or ((item.get("related") or {}).get("article") or {}).get("url")
                    if isinstance(url, str) and url:
                        item["evidence_weight"] = self._weight(url, site)
                events.append({"source": tool, "data": projected})
            except BudgetExhausted:
                raise
            except Exception as exc:
                errors.append({"source": tool, "error": type(exc).__name__})
        return {"domain": domain, "events": events, "errors": errors}

    def estimated_spend_usd(self) -> float:
        """A LOCAL estimate from the price mirror; the platform's ledger is the truth."""

        total = 0.0
        for tool, count in self.calls_by_tool.items():
            total += float(count) * FIXED_PRICES_USD.get(tool, DYNAMIC_PRICE_ESTIMATE_USD)
        return round(total, 4)

    def tool_price_usd(self, tool: str) -> float:
        """The local per-call estimate: the fixed price, else the dynamic guess."""

        return float(FIXED_PRICES_USD.get(tool, DYNAMIC_PRICE_ESTIMATE_USD))

    @staticmethod
    def _usd(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number or number in (float("inf"), float("-inf")) or number < 0.0:
            return None
        return number

    def note_external_spend(self, usd: Any) -> None:
        """Model-side charges the Deepline ledger cannot see (agent meter, paragraph, sonar)."""

        number = self._usd(usd)
        if number:
            self.external_spend_usd += number
            self._log_ledger()

    def _log_ledger(self) -> None:
        self._ledger_log.append((time.monotonic(), self._local_ledger_usd()))
        if len(self._ledger_log) > 4000:
            del self._ledger_log[:2000]

    def _local_ledger_usd(self) -> float:
        return self.estimated_spend_usd() + self.external_spend_usd

    def set_confirmed_spend(self, usd: Any, *, settled: bool = False, as_of: Optional[float] = None) -> bool:
        """Re-base the planning ledger on the host's sourcing_cost -- never downward, unless SETTLED.

        s32 C1: ``settled`` = the snapshot showed zero inflight and zero success-unresolved calls, so
        its number is the whole ICP spend (every attempt, SQL 319) up to the read.  Then the ledger
        may move DOWN to it plus SETTLED_MARGIN_USD: across 147 saved runs the local price-mirror
        estimate never ran below the host (median $0.56 vs $0.34), and keeping that over-estimate
        would stop research at ~2/3 of the real ceiling.

        LAB-LOG #315 (independent review of #314): a snapshot may be cached for a
        second and may carry successful-but-unresolved calls at zero dollars, so
        "the host says $0.75" does not prove that the $0.03 we estimated a moment
        ago is inside that $0.75.  The watermark therefore moves to
        max(host number, what we were already planning against): a local estimate
        stays until the host's number has visibly grown past it.  This is
        CONSERVATIVE PACING, not exact settlement -- an over-estimate is kept until
        settlement overtakes it, never silently dropped.
        """

        number = self._usd(usd)
        if number is None:
            return False
        planned = self.spend_usd()
        cutoff_in = time.monotonic() - SNAPSHOT_STALE_SECONDS if as_of is None else min(float(as_of), time.monotonic() - 1.0)
        if self._last_host is not None and abs(self._last_host[0] - number) < 1e-9:
            cutoff_in = min(cutoff_in, self._last_host[1])
        self._last_host = (number, cutoff_in)
        if settled:
            cutoff = cutoff_in
            older = [value for at, value in self._ledger_log if at <= cutoff]
            self._confirmed_spend = (number + SETTLED_MARGIN_USD, older[-1] if older else 0.0)
        else:
            self._confirmed_spend = (max(number, planned), self._local_ledger_usd())
        return True

    def maybe_refresh_spend(self, min_interval: float = SPEND_REFRESH_SECONDS) -> bool:
        """Run the harness's host re-read if one is installed and the last one is old enough."""

        hook = self.spend_refresh
        now = time.monotonic()
        if not callable(hook) or now - self._last_spend_refresh < float(min_interval):
            return False
        self._last_spend_refresh = now
        try:
            hook()
        except Exception:
            return False
        return True

    def spend_usd(self) -> float:
        """What this ICP has spent: the host's last number plus our estimate of what followed."""

        ledger = self._local_ledger_usd()
        if self._confirmed_spend is None:
            return round(ledger, 4)
        confirmed, at_read = self._confirmed_spend
        return round(confirmed + max(0.0, ledger - at_read), 4)

    def paid_allowed(self, estimate_usd: float = 0.0) -> bool:
        """May a call that will charge about ``estimate_usd`` start now?  No cap = yes.

        With an estimate the call must FIT: spend + estimate <= cap.  Without one
        the question is "is there room for any priced call at all", which at
        exactly the cap is no -- every priced call costs more than nothing.
        """

        if self.spend_cap_usd is None:
            return True
        cap = float(self.spend_cap_usd)
        estimate = max(0.0, float(estimate_usd or 0.0))
        if estimate <= 0.0:
            return self.spend_usd() < cap - 1e-9
        return self.spend_usd() + estimate <= cap + 1e-9

    def adopt_quota(self, snapshot: Any, *, reserve: int = 2) -> dict[str, Any]:
        """Replace the hard-coded call ceiling with the round's real Deepline quota.

        ``snapshot`` is a lab_arena_checkpoint.quota_usage() document.  The round
        may allow 30 or 200 Deepline calls per ICP (three quota profiles coexist,
        contracts.EXECUTION_CALL_QUOTA_PROFILES) and a constant is wrong on one of
        them; this reads the real number.  Never raises: an unreadable snapshot
        leaves the ceiling where it was.
        """

        try:
            providers = snapshot.get("providers") if isinstance(snapshot, dict) else None
            deepline = providers.get("deepline") if isinstance(providers, dict) else None
            limit = int(deepline["limit"])
            used = int(deepline["used"])
            remaining = int(deepline["remaining"])
        except Exception:
            return {"adopted": False}
        with self._lock:
            new_budget = max(self.calls, self.calls + remaining - max(0, int(reserve)))
            before = self.call_budget
            self.call_budget = new_budget
        return {"adopted": True, "limit": limit, "used": used, "remaining": remaining,
                "call_budget_before": before, "call_budget": new_budget}

    def search_news(self, query: str, *, limit: int = 5, company_website: str = "") -> dict[str, Any]:
        """Dated news results at zero cost (contextdev_post_news_search, Decimal("0")).

        Replaces the $0.56 predictleads_* calls for FUNDING / HIRING / NEWS discovery
        wherever a headline and a URL are enough; the page still has to be fetched
        (free branch first) before anything on it can be cited.
        """

        query = str(query or "").strip()
        if not query:
            raise ValueError("query is required")
        limit = max(1, min(int(limit or 5), 10))
        site = _domain(company_website) if company_website else ""
        if site:
            entity: dict[str, str] = {"type": "domain", "domain": site}
        elif "." in query and " " not in query and _domain(query):
            entity = {"type": "domain", "domain": _domain(query)}
        else:
            entity = {"type": "name", "name": query[:200]}
        raw = self._deepline(CONTEXTDEV_NEWS_SEARCH, {"searchBy": {"type": "entity", "entity": entity}})
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in _result_list(raw):
            if isinstance(item, str):
                item = {"url": item}
            if not isinstance(item, dict):
                continue
            url = _evidence_url(item.get("url") or item.get("link") or item.get("href"))
            if not url or url in seen:
                continue
            seen.add(url)
            published = str(item.get("published_at") or item.get("published_date") or item.get("publishedDate")
                            or item.get("date") or item.get("published") or "")[:10] or None
            match = item.get("match") if isinstance(item.get("match"), dict) else {}
            rows.append({
                "url": url, "title": str(item.get("title") or "")[:200], "published_date": published,
                "evidence_weight": self._weight(url, company_website),
                "structural_reason": sm.url_structural_reason(url),
                "excerpt": " ".join(str(item.get("snippet") or item.get("description") or
                                       item.get("summary") or "").split())[:400],
                "match": str(match.get("level") or "")[:20] or None,
            })
        rows.sort(key=lambda r: 0 if r.get("match") in (None, "primary") else 1)
        rows = rows[:limit]
        return {"results": rows, "count": len(rows), "entity": entity}

    def validate_email(self, email: str) -> str:
        """'valid' | 'catch_all' | 'invalid' | 'unknown' from one zerobounce_validate call ($0.028).

        The judge validates the submitted email with the same provider and zeroes the
        pair on an invalid verdict (2 of 5 accepted contacts in #317/e5).  The reading
        mirrors contact_verification._email_status: unsafe flags and invalid statuses
        first, then catch-all, then valid; anything else is unknown.
        """

        data = _result_data(self._deepline("zerobounce_validate", {"email": str(email or "").strip()}))
        records = [r for r in (data, data.get("result") if isinstance(data, dict) else None) if isinstance(r, dict)]

        def norm(value: Any) -> str:
            return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")

        seen = {norm(r.get(k)) for r in records for k in ("status", "sub_status", "result", "verdict", "classification")
                if not isinstance(r.get(k), (dict, list))}
        flags = ("is_disposable", "disposable", "is_abuse", "abuse", "do_not_mail", "is_do_not_mail", "is_spamtrap",
                 "spamtrap", "is_toxic", "toxic")
        if seen & {"invalid", "abuse", "do_not_mail", "disposable", "spamtrap", "toxic"} or any(
                r.get(f) is True or norm(r.get(f)) in {"true", "1", "yes"} for r in records for f in flags):
            return "invalid"
        if seen & {"catch_all", "accept_all", "valid_accept_all", "ok_for_all"}:
            return "catch_all"
        return "valid" if "valid" in seen else "unknown"


    def search_company_people(self, company_linkedin: str, roles: list[str] | None = None, *,
                              title_filter: bool = True) -> list[dict[str, str]]:
        """CURRENT employees of one LinkedIn company ($0.07, one page of up to 25; from autopilot c8, e12).

        Web people-search returns name matches -- in loop d1 most rejections were "no current position at this
        company".  LinkedIn's own search filtered by the company page returns current employees with their
        current title, so a paid profile lookup goes only to someone who is there.
        """

        url = str(company_linkedin or "").strip()
        if "linkedin.com/company/" not in url.lower():
            return []
        payload: dict[str, Any] = {"currentCompanies": url, "page": 1}
        roles = [str(r).strip() for r in (roles or []) if str(r).strip()][:3]
        if title_filter and roles:
            payload["currentJobTitles"] = ",".join(roles)
        data = _result_data(self._deepline("harvestapi_search_leads", payload))
        rows = data.get("elements") if isinstance(data, dict) else None
        out: list[dict[str, str]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            match = _LINKEDIN_PERSON_RE.search(str(row.get("linkedinUrl") or ""))
            positions = row.get("currentPositions") if isinstance(row.get("currentPositions"), list) else []
            position = next((p for p in positions if isinstance(p, dict)), {})
            title = str(position.get("position") or position.get("title") or "").strip()
            if not match or not title:
                continue
            out.append({"linkedin_url": f"https://www.linkedin.com/in/{match.group(1).rstrip('/')}/", "title": title[:200],
                        "source": "linkedin_company_search"})
        return out

    def find_people(self, company_name: str, domain: str, roles: list[str] | None = None, *,
                    limit: int = 6, allow_paid: bool = True, company_linkedin: str = "") -> list[dict[str, str]]:
        """LinkedIn person URLs at the company: free web search first, then the company-filtered LinkedIn search
        when the company's page is known (loop s3), then paid web search as the last fallback.

        Returns [{"linkedin_url", "title"}].  The result titles are search-engine
        titles ("Name - Role - Company | LinkedIn"), useful for ranking and never
        for attribution; only harvestapi_get_profile proves a person.
        """

        company_name = " ".join(str(company_name or "").split())
        domain = _domain(domain)
        roles = [str(r).strip() for r in (roles or []) if str(r).strip()][:3]
        if not company_name and not domain:
            raise ValueError("company_name or domain is required")
        queries: list[str] = []
        anchor = f'"{company_name}"' if company_name else domain
        for role in roles[:2] or [""]:
            queries.append(" ".join(part for part in ("site:linkedin.com/in", anchor, role) if part))
        found: list[dict[str, str]] = []
        seen: set[str] = set()

        def collect(items: Any) -> None:
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or item.get("link") or "")
                match = _LINKEDIN_PERSON_RE.search(url)
                if not match:
                    continue
                slug = match.group(1).rstrip("/")
                canonical = f"https://www.linkedin.com/in/{slug}/"
                if canonical.casefold() in seen:
                    continue
                seen.add(canonical.casefold())
                found.append({"linkedin_url": canonical, "title": str(item.get("title") or "")[:200]})

        completed = 0
        errors: list[str] = []
        for query in queries:
            if len(found) >= limit:
                break
            try:
                data = _result_data(self._deepline(CONTEXTDEV_WEB_SEARCH, {"query": query}))
                collect(data.get("results") or _result_list(data))
                completed += 1
            except BudgetExhausted:
                raise
            except Exception as exc:
                errors.append(type(exc).__name__)
                continue
        if not found and allow_paid and company_linkedin:
            try:
                rows = self.search_company_people(company_linkedin, roles) or \
                    self.search_company_people(company_linkedin, roles, title_filter=False)
            except BudgetExhausted:
                raise
            except Exception:
                rows = []
            if rows:
                return rows[:max(limit, 1) * 2]
        if not found and allow_paid:
            try:
                data = _result_data(self._deepline("exa_search", {
                    "query": f"{company_name} {' '.join(roles[:2])} linkedin".strip(),
                    "numResults": limit, "includeDomains": ["linkedin.com"],
                }))
                collect(data.get("results") or [])
                completed += 1
            except BudgetExhausted:
                raise
            except Exception as exc:
                errors.append(type(exc).__name__)
        if not found and not completed and errors:
            raise RuntimeError("people search unavailable: " + errors[-1])
        return found[:limit]

    def get_person_profile(self, linkedin_url: str) -> dict[str, Any]:
        """One harvestapi_get_profile call with findEmail, returned RAW.

        Raw on purpose: contacts.py mirrors the verifier's own candidate walk
        (qualification/scoring/contact_verification._profile_candidates) over the
        same envelope, so what we accept is what the judge will re-fetch and read.
        """

        match = _LINKEDIN_PERSON_RE.search(str(linkedin_url or ""))
        if not match:
            raise ValueError("a LinkedIn /in/ profile URL is required")
        url = f"https://www.linkedin.com/in/{match.group(1).rstrip('/')}/"
        return self._deepline(HARVEST_PROFILE_TOOL, {"url": url, "findEmail": "true"})

    def _free_search(self, query: str) -> list[dict[str, Any]]:
        """The free contextdev web search as a list of result rows (title + url)."""

        free = _result_data(self._deepline(CONTEXTDEV_WEB_SEARCH, {"query": query}))
        rows = free.get("results") or _result_list(free)
        return [row for row in rows if isinstance(row, dict)]

    def _paid_search(self, payload: dict[str, Any], limit: int) -> dict[str, Any]:
        """Exa with the full payload, then minimal, then the free search as the last resort."""

        try:
            return _result_data(self._deepline("exa_search", payload))
        except BudgetExhausted:
            raise
        except Exception as first_error:
            minimal = {"query": payload["query"], "numResults": limit}
            try:
                return _result_data(self._deepline("exa_search", minimal))
            except BudgetExhausted:
                raise
            except Exception:
                try:
                    return {"results": self._free_search(payload["query"])}
                except BudgetExhausted:
                    raise
                except Exception:
                    raise first_error

    def search_web(self, query: str, *, recency_days: Optional[int] = None,
                   limit: int = PACING["search_web_limit"],
                   include_domains: list[str] | None = None, category: str = "",
                   text_chars: int = PACING["search_web_text_chars"], company_website: str = "") -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("query is required")
        limit = max(1, min(int(limit or PACING["search_web_limit"]), 6))
        payload: dict[str, Any] = {
            "query": query[:2000], "type": "auto", "numResults": limit,
            "contents": {"text": {"maxCharacters": 6000}},
        }
        if recency_days:
            start = sm.evaluation_date() - timedelta(days=max(1, int(recency_days)))
            payload["startPublishedDate"] = start.isoformat() + "T00:00:00.000Z"
        if include_domains:
            payload["includeDomains"] = [d for d in (_domain(x) for x in include_domains) if d][:20]
        if category in {"company", "news", "research paper", "tweet", "personal site", "financial report", "pdf", "github"}:
            payload["category"] = category
        data: Optional[dict[str, Any]] = None
        if PACING.get("free_search_first"):
            try:
                free = self._free_search(payload["query"])
                if free:
                    data = {"results": free}
            except BudgetExhausted:
                raise
            except Exception:
                data = None
        if data is None:
            data = self._paid_search(payload, limit)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in data.get("results") or []:
            if not isinstance(raw, dict):
                continue
            url = _evidence_url(raw.get("url") or raw.get("id"))
            if not url or url in seen:
                continue
            seen.add(url)
            text = str(raw.get("text") or "")
            excerpt_chars = max(300, min(int(text_chars or PACING["search_web_text_chars"]), 3000))
            row = {"url": url, "title": str(raw.get("title") or "")[:200],
                   "published_date": str(raw.get("publishedDate") or "")[:10] or None,
                   "evidence_weight": self._weight(url, company_website),
                   "structural_reason": sm.url_structural_reason(url),
                   "excerpt": " ".join(text.split())[:excerpt_chars],
                   "note": "full page cached; cite only after fetch_page or from this excerpt verbatim"}
            rows.append(row)
            if text:
                self.pages[url] = Page(url, title=row["title"], text=text, source="exa_search", ok=True)
            if len(rows) >= limit:
                break
        return {"results": rows, "count": len(rows)}

    def _contextdev_page(self, url: str, cache_chars: int) -> Page:
        """The free fixed-price scrape as a Page; never raises except on the call ceiling."""

        try:
            data = _result_data(self._deepline(CONTEXTDEV_TOOL, {"url": url}))
            markdown = data.get("markdown")
            if isinstance(markdown, str) and len(markdown.strip()) >= 200:
                text = " ".join(re.sub(r"[#*_>`\[\]()]", " ", markdown).split())
                page = Page(url, title=str(data.get("title") or "")[:300],
                            text=text[:cache_chars], source="contextdev", ok=True)
                page.links = list(dict.fromkeys(
                    link for raw in re.findall(r"https?://[^\s)\]>\"']+", markdown)[:400]
                    if (link := _evidence_url(raw, base_url=page.final_url))
                ))[:400]
                return page
            return Page(url, error="contextdev: empty")
        except BudgetExhausted:
            return Page(url, error="budget")
        except Exception as exc:
            return Page(url, error=f"contextdev: {type(exc).__name__}")

    def fetch_page(self, url: str, *, max_chars: int = PACING["fetch_page_max_chars"],
                   allow_firecrawl: bool = True, prefer_firecrawl: bool = False,
                   allow_contextdev: bool = True) -> Page:
        url = str(url or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return Page(url, error="an absolute HTTP(S) URL is required")
        cached = self.pages.get(url)
        enrich_links = bool(
            cached is not None and cached.ok and not cached.links
            and cached.source != "firecrawl" and prefer_firecrawl and allow_firecrawl
            and url not in self._link_enrichment_attempted
        )
        if cached is not None and (cached.ok or cached.error == "budget") and not enrich_links:
            return cached
        if enrich_links:
            self._link_enrichment_attempted.add(url)
        max_chars = max(1000, min(int(max_chars), 12000))
        cache_chars = 12000
        page = Page(url)
        payloads = (
            {"urls": [url], "text": {"maxCharacters": cache_chars}, "livecrawl": "fallback"},
            {"urls": [url], "text": {"maxCharacters": cache_chars}},
        )
        if prefer_firecrawl and allow_firecrawl:
            payloads = ()
        elif allow_contextdev and PACING.get("free_fetch_first"):
            page = self._contextdev_page(url, cache_chars)
            if page.ok:
                self.pages[url] = page
                return page
            if page.error == "budget":
                self.pages[url] = page
                return page
            page = Page(url)
        for attempt, payload in enumerate(payloads):
            try:
                data = _result_data(self._deepline("exa_contents", payload))
                results = data.get("results") or []
                first = results[0] if results and isinstance(results[0], dict) else {}
                text = str(first.get("text") or "")
                if len(text.strip()) >= 200:
                    page = Page(url, title=str(first.get("title") or "")[:300], text=text[:cache_chars],
                                source="exa_contents", ok=True)
                break
            except BudgetExhausted:
                page = Page(url, error="budget")
                self.pages[url] = page
                return page
            except Exception as exc:
                page = Page(url, error=f"exa_contents: {type(exc).__name__}")
                if attempt == len(payloads) - 1 or self.remaining() < 3:
                    break
        if not page.ok and allow_contextdev and not prefer_firecrawl and not PACING.get("free_fetch_first"):
            page = self._contextdev_page(url, cache_chars)
            if page.error == "budget":
                self.pages[url] = page
                return page
        if not page.ok and allow_firecrawl:
            try:
                data = _result_data(self._deepline("firecrawl_scrape", {
                    "url": url, "formats": ["markdown", "rawHtml"], "onlyMainContent": True, "timeout": 60_000,
                }))
                markdown = data.get("markdown")
                raw_html = data.get("rawHtml") or data.get("html")
                metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
                title = str(metadata.get("title") or "")[:300]
                if isinstance(markdown, str) and len(markdown.strip()) >= 200:
                    text = " ".join(re.sub(r"[#*_>`\[\]()]", " ", markdown).split())
                    page = Page(url, final_url=str(metadata.get("url") or url), title=title, text=text[:cache_chars],
                                source="firecrawl", ok=True)
                    page.links = re.findall(r"https?://[^\s)\]>\"']+", markdown)[:400]
                elif isinstance(raw_html, str) and raw_html.strip():
                    html_title, text = html_to_text(raw_html, cache_chars)
                    if len(text.strip()) >= 200:
                        page = Page(url, final_url=str(metadata.get("url") or url), title=title or html_title,
                                    text=text, source="firecrawl", ok=True)
                    else:
                        page = Page(url, error="firecrawl: too little text")
                else:
                    page = Page(url, error="firecrawl: empty")
                if page.ok and isinstance(raw_html, str):
                    parser = _TextExtractor()
                    parser.feed(raw_html)
                    parser.close()
                    page.links.extend(parser.links)
                if page.ok:
                    page.links = list(dict.fromkeys(
                        link for raw in page.links
                        if (link := _evidence_url(raw, base_url=page.final_url))
                    ))[:400]
            except BudgetExhausted:
                page = Page(url, error="budget")
            except Exception as exc:
                page = Page(url, error=f"firecrawl: {type(exc).__name__}")
        if enrich_links:
            if page.ok and sm.canonical_domain(page.final_url) == sm.canonical_domain(url):
                cached.links = page.links
            return cached
        self.pages[url] = page
        return page

    def page_text(self, url: str) -> str:
        page = self.pages.get(str(url or "").strip())
        return page.text if page and page.ok else ""


def _project_events(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    included: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in payload.get("included") or []:
        if not isinstance(raw, dict):
            continue
        kind, identifier = str(raw.get("type") or ""), str(raw.get("id") or "")
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        allowed = ("company_name", "domain", "ticker") if kind == "company" else ("title", "url", "published_at", "author")
        included[(kind, identifier)] = {k: _safe(attrs.get(k)) for k in allowed if attrs.get(k) not in (None, "", [])}
    names = {"amount", "amount_normalized", "article_sentence", "categories", "category", "confidence",
             "contract_types", "effective_date", "event", "financing_type", "financing_type_normalized",
             "first_seen_at", "found_at", "headcount", "job_title", "last_seen_at", "location",
             "normalized_title", "planning", "posted_at", "product", "recognition", "salary", "seniority",
             "status", "summary", "title", "url", "vulnerability"}
    items: list[dict[str, Any]] = []
    for raw in (payload.get("data") or [])[:limit] if isinstance(payload.get("data"), list) else []:
        if not isinstance(raw, dict):
            continue
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        item: dict[str, Any] = {"type": str(raw.get("type") or "event"),
                                "attributes": {k: _safe(v) for k, v in attrs.items() if k in names and v not in (None, "", [], {})}}
        relations = raw.get("relationships") if isinstance(raw.get("relationships"), dict) else {}
        related: dict[str, Any] = {}
        for name, relation in relations.items():
            data = relation.get("data") if isinstance(relation, dict) else None
            if isinstance(data, dict):
                key = (str(data.get("type") or ""), str(data.get("id") or ""))
                if key in included:
                    related[str(name)] = included[key]
        if related:
            item["related"] = related
        items.append(item)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return {"items": items, "returned_count": len(items), "available_count": meta.get("count")}


__all__ = ["ArenaTools", "BudgetExhausted", "Page", "html_to_text", "DEEPLINE_QUOTA_PER_ICP"]
