"""s88: the judge's own stage investigation, run on our candidates before the re-check ranks them.

Stage is the gate our rows fail first (74 of our 166 rows on arena-2026-10-22). The judge decides it with its
company evidence investigator (qualification.scoring.company_evidence_investigator.investigate_company_evidence:
a bounded tool loop on google/gemini-3.5-flash-lite, two searches, four page reads). We call that same function
from the scorer source the round image carries (``/model``, put on sys.path by judge.py) and swap only its I/O:

  * the model call goes through the run's OpenRouter route (reverify._client_and_base), not openrouter.ai;
  * search goes through tools.search_web (Deepline exa_search, counted against the run's budget);
  * page reads go through the free direct web lane (webfetch.Direct), and the judge's own _plain_text still turns
    the HTML into the text its quote checks read;
  * Exa contents and the LinkedIn company-size lookup are switched off (no key, not needed for stage).

Measured offline on the rows our four hotkeys sent (stage only): 10-22, investigator VERIFIED on 31 companies, of
which the judge passed stage on 94% (4 qualified), not verified on 29, of which 3% (0 qualified); 10-21, 38 at 68%
(6 qualified) against 22 at 14% (0). The verdict is used for ranking only: harness.stage_mirror_verdicts ->
reverify.rerank puts verified companies first, unknown (an error, a timeout, no lane) in the middle, unproven /
contradicted last, inside each re-check tier (live single runs misread some companies, so it never overrides a
re-check tier). Never raises.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import threading
import time
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

try:
    from .arena_tools import STRATEGY as _STRATEGY
except Exception:  # noqa: BLE001
    _STRATEGY = {}


def _number(key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(_STRATEGY.get(key, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high) if value == value else default


STAGE_MIRROR = _number("stage_mirror", 1, 0, 1) >= 1
STAGE_MIRROR_SECONDS = _number("stage_mirror_seconds", 200.0, 30.0, 600.0)
STAGE_MIRROR_COMPANIES = int(_number("stage_mirror_companies", 20, 1, 40))
STAGE_MIRROR_CONCURRENCY = int(_number("stage_mirror_concurrency", 6, 1, 12))
STAGE_MIRROR_EST_USD = 0.03          # per company: two exa_search calls plus a few flash-lite turns
# Live 10-21 #8 / 10-22 #0 / 10-22 #5 (launch and partnership ICPs): the own-site index probes had used all 300 of the
# direct lane's hops before the re-check, every investigator read failed and every verdict came back unknown. The
# bridge allows 512 connections per ICP; the investigation gets its own room above whatever was spent, up to 460.
STAGE_MIRROR_HOPS = int(_number("stage_mirror_hops", 140, 0, 200))
HOP_CEILING = 460
LAST: dict[str, Any] = {}
_PATCH_LOCK = threading.Lock()

VERIFIED, UNKNOWN, UNPROVEN = "verified", "unknown", "unproven"
RANK = {VERIFIED: 0, UNKNOWN: 1, UNPROVEN: 2}


def _sibling(name: str) -> Any:
    try:
        return importlib.import_module(f".{name}", __package__)
    except Exception:  # noqa: BLE001
        return importlib.import_module(f"{os.path.basename(os.path.dirname(os.path.abspath(__file__)))}.{name}")


def _investigator() -> Any:
    """The judge's investigator module, or None when the scorer source cannot be imported here."""

    try:
        judge = _sibling("judge")
        judge._optional_dependency_stubs()
        return importlib.import_module(judge._CEI)
    except Exception as exc:  # noqa: BLE001
        LAST["import_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return None


def identity(company: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """The homepage identity the judge hands its investigator: name, registrable domain, LinkedIn company slug."""

    site = str(company.get("company_website") or "")
    host = (urlsplit(site if "//" in site else "https://" + site).hostname or "").lower().removeprefix("www.")
    linkedin = str(company.get("company_linkedin") or "")
    match = re.search(r"linkedin\.com/company/([^/?#]+)", linkedin, re.I)
    if not host or not match:
        return None
    return {"normalized_name": " ".join(str(company.get("company_name") or "").lower().split()),
            "registrable_dns_domain": host, "linkedin_company_slug": match.group(1).lower()}


def source_urls(company: Mapping[str, Any]) -> list[str]:
    """Our own evidence URLs, offered to the investigator the way the judge offers submitted sources."""

    urls = [s.get("url") for s in company.get("intent_signals") or [] if isinstance(s, Mapping)]
    attribute = company.get("required_attribute")
    urls.append(attribute.get("evidence_url") if isinstance(attribute, Mapping) else "")
    urls += [e.get("url") for e in company.get("company_stage_evidence") or [] if isinstance(e, Mapping)]
    urls.append(company.get("company_website"))
    return [u for u in dict.fromkeys(str(u) for u in urls if u and str(u).startswith("https://"))][:8]


def _verdict(result: Any) -> str:
    if not isinstance(result, Mapping) or result.get("failure_reason"):
        reason = str(result.get("failure_reason") if isinstance(result, Mapping) else type(result).__name__)[:60]
        failures = LAST.setdefault("failures", {})
        failures[reason] = failures.get(reason, 0) + 1
        return UNKNOWN
    status = str(((result.get("claims") or {}).get("stage") or {}).get("status") or "")
    return VERIFIED if status == "VERIFIED" else UNPROVEN if status in ("UNPROVEN", "CONTRADICTED") else UNKNOWN


def verdicts(companies: list, icp: Mapping[str, Any], tools: Any, *, http_client_factory: Any = None,
             deadline: Optional[float] = None) -> dict[str, str]:
    """{company name: verified | unknown | unproven} for up to STAGE_MIRROR_COMPANIES companies. Never raises."""

    out: dict[str, str] = {}
    for stale in ("evidence", "failures", "verdicts", "hops", "error", "skipped"):
        LAST.pop(stale, None)
    if not STAGE_MIRROR or not companies or not str(icp.get("company_stage") or "").strip():
        return out
    cei = _investigator()
    direct = getattr(tools, "_direct", None)
    if cei is None or direct is None or not callable(getattr(tools, "search_web", None)):
        return out
    try:
        if not direct.available():
            LAST["skipped"] = "direct lane off"
            return out
    except Exception:  # noqa: BLE001
        return out
    webfetch, reverify = _sibling("webfetch"), _sibling("reverify")
    stop = min(float(deadline) if deadline else time.monotonic() + STAGE_MIRROR_SECONDS,
               time.monotonic() + STAGE_MIRROR_SECONDS)
    picked = [c for c in companies if isinstance(c, Mapping)][:STAGE_MIRROR_COMPANIES]

    async def post_json(session, url, *, headers, payload):
        if "openrouter.ai" not in str(url):
            return 503, {}
        client, base, hdrs = reverify._client_and_base(http_client_factory, max(5.0, min(60.0, stop - time.monotonic())))
        try:
            response = await client.post(base + "/chat/completions", json=dict(payload),
                                         headers={**hdrs, "Content-Type": "application/json"})
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, {}
        finally:
            await client.aclose()

    async def search_web(session, query, *, key):
        data = await asyncio.to_thread(tools.search_web, str(query)[:500], limit=cei.MAX_SEARCH_RESULTS)
        rows = []
        for item in (data or {}).get("results") or []:
            url = cei._safe_https_url(item.get("url"))
            if url:
                rows.append({"url": url, "title": str(item.get("title") or "")[:300]})
        return {"results": rows[:cei.MAX_SEARCH_RESULTS], "notice": "discovery_only_not_evidence"}

    async def fetch_bounded_html(session, url, *, params=None):
        target = str((params or {}).get("url") or url) if "scrapingdog" in str(url) else str(url)
        if not webfetch._allowed(target) or not direct.available():
            return 0, target, ""
        got = await asyncio.to_thread(direct._get, target, time.monotonic(), webfetch.BROWSER_HEADERS)
        return int(got.get("status") or 0), str(got.get("final_url") or target), str(got.get("body") or "")

    async def no_exa(session, url):
        return {"ok": False, "error": "contents_provider_unavailable"}

    async def no_linkedin(*args, **kwargs):
        return None

    async def one(company):
        result = await cei.investigate_company_evidence(
            company_locator={"name": company.get("company_name"), "website": company.get("company_website"),
                             "linkedin": company.get("company_linkedin") or ""},
            targets=["stage"], requested_stage=str(icp.get("company_stage") or ""),
            requested_industry=str(icp.get("industry") or ""), requested_subindustry=str(icp.get("sub_industry") or ""),
            requested_product_service=str(icp.get("product_service") or ""),
            requested_attribute=str(icp.get("required_attribute") or ""), requested_geography="",
            prior_observations={"submitted_source_urls": source_urls(company),
                                "observed_company_name": company.get("company_name"),
                                "observed_company_website": company.get("company_website"),
                                "observed_company_linkedin": company.get("company_linkedin") or ""},
            verified_homepage_identity=identity(company))
        verdict = _verdict(result)
        if verdict == VERIFIED:   # s89: the page and sentence the investigator itself accepted
            claim = (result.get("claims") or {}).get("stage") or {}
            url, quote = str(claim.get("evidence_url") or ""), " ".join(str(claim.get("evidence_quote") or "").split())
            if url.startswith("https://") and 20 <= len(quote) <= 1500:
                LAST.setdefault("evidence", {})[str(company.get("company_name") or "")] = {"url": url, "quote": quote}
        return verdict

    async def run_all():
        gate = asyncio.Semaphore(STAGE_MIRROR_CONCURRENCY)

        async def guarded(company):
            async with gate:
                if time.monotonic() > stop - 20.0:
                    return UNKNOWN
                try:
                    return await asyncio.wait_for(one(company), timeout=max(5.0, stop - time.monotonic()))
                except Exception as exc:  # noqa: BLE001 - a failed investigation is no verdict
                    failures = LAST.setdefault("failures", {})
                    key = f"{type(exc).__name__}: {str(exc)[:60]}"
                    failures[key] = failures.get(key, 0) + 1
                    return UNKNOWN

        return await asyncio.gather(*(guarded(c) for c in picked))

    names = {"_post_json": post_json, "_search_web": search_web, "_fetch_bounded_html": fetch_bounded_html,
             "_fetch_exa_exact_page": no_exa, "fetch_current_linkedin_company_size": no_linkedin}
    started = time.monotonic()
    with _PATCH_LOCK:
        saved = {name: getattr(cei, name, None) for name in names}
        env = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "EXA_API_KEY", "SCRAPINGDOG_API_KEY")}
        saved_hops = webfetch.MAX_HOPS
        try:
            spent = int((getattr(direct, "stats", {}) or {}).get("hops") or 0)
            webfetch.MAX_HOPS = min(HOP_CEILING, max(saved_hops, spent + STAGE_MIRROR_HOPS))
            LAST["hops"] = {"before": spent, "ceiling": webfetch.MAX_HOPS}
            for name, fn in names.items():
                setattr(cei, name, fn)
            os.environ.setdefault("OPENROUTER_API_KEY", "arena-host")   # the investigator refuses to start without one
            os.environ.setdefault("EXA_API_KEY", "via-run-search")
            os.environ.pop("SCRAPINGDOG_API_KEY", None)                  # plain reads: the direct lane is the transport
            found = asyncio.run(run_all())
            out = {str(c.get("company_name") or ""): v for c, v in zip(picked, found)}
        except Exception as exc:  # noqa: BLE001
            LAST["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            LAST.setdefault("hops", {})["after"] = int((getattr(direct, "stats", {}) or {}).get("hops") or 0)
            webfetch.MAX_HOPS = saved_hops
            for name, fn in saved.items():
                if fn is not None:
                    setattr(cei, name, fn)
            for key, value in env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    LAST["verdicts"] = dict(out)
    LAST["seconds"] = round(time.monotonic() - started, 1)
    return out
