"""Fetch each cited intent URL the way the judge does, and classify it with the judge's own page verdicts.

On miner-funded scoring the judge reads a cited page through Deepline's firecrawl_scrape with a fixed rawHtml
request, then classifies the HTML (HTTP status, empty body, anti-bot marker, JS shell, non-text) and the extracted
visible text.  When that fails (and the Wayback tier fails) it reads the same URL through Exa contents and accepts
>= 300 characters (intent_verification_three_stage._scrape_exa, source 'exa_fallback').  So a content failure other
than HTTP 404/410 is checked against that fallback too: the URL is kept when the page is already cached from
exa_contents with >= 300 characters or one governed exa_contents call returns that much text without an anti-bot
wall.  Only a URL failing both routes is removed; a company left without a primary URL is dropped.  Infrastructure
failures (a provider error, a refused or oversized reply) and slow pages keep the URL, flagged.  ATS postings are
skipped: the judge reads those through the board's own API.
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import gates
from .arena_tools import BudgetExhausted, Page, _result_data

MAX_URLS = 6
SLOW_S = 12.0
_ATS_RE = re.compile(r"(?:greenhouse\.io|ashbyhq\.com|lever\.co|teamtailor\.com|workable\.com|myworkdayjobs\.com)", re.I)
CONTENT_FAILURES = frozenset({"http_404", "http_410", "html_empty_body", "anti_bot_marker", "js_shell", "body_too_short",
                              "non_textual"})
HARD_FAILURES = frozenset({"http_404", "http_410"})
EXA_MIN_CHARS = 300
EXA_MAX_CHARS = 12_000
MAX_EXA = 4
EXA_OK = "exa_fallback"


def exa_payload(url: str) -> dict[str, Any]:
    """The judge's Exa-contents fallback request (live read, no cache age), with a smaller text cap."""

    return {"ids": [url], "text": {"maxCharacters": EXA_MAX_CHARS}, "maxAgeHours": 0}


def _exa_text_ok(text: str) -> bool:
    return len(str(text or "").strip()) >= EXA_MIN_CHARS and not gates.antibot(text)


def exa_fallback(tools: Any, url: str) -> str:
    """'ok' when the judge's Exa fallback would read the page, 'thin' when it would not, 'unknown:<why>' when the
    check could not run.  A cached exa_contents page is used first; otherwise one exa_contents call is made."""

    cached = getattr(tools, "pages", {}).get(url)
    if cached is not None and getattr(cached, "ok", False) and getattr(cached, "source", "") == "exa_contents" and \
            _exa_text_ok(getattr(cached, "text", "")):
        return "ok"
    try:
        data = _result_data(tools._deepline("exa_contents", exa_payload(url)))
    except BudgetExhausted:
        return "unknown:refused"
    except Exception as exc:  # noqa: BLE001 - provider errors, oversized replies, timeouts
        return f"unknown:{type(exc).__name__}"
    results = data.get("results") if isinstance(data, dict) else None
    first = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    text = str(first.get("text") or "")
    if not _exa_text_ok(text):
        return "thin"
    if cached is None or not getattr(cached, "ok", False):
        tools.pages[url] = Page(url, title=str(first.get("title") or "")[:300], text=text[:EXA_MAX_CHARS],
                                source="exa_contents", ok=True)
    return "ok"


def judge_payload(url: str) -> dict[str, Any]:
    return {"url": url, "formats": ["rawHtml"], "onlyMainContent": False, "maxAge": 0, "timeout": 55_000,
            "storeInCache": False}


def fetch_verdict(tools: Any, url: str) -> dict[str, Any]:
    """{'verdict': ok | <content failure> | infra:<reason> | unknown, 'seconds': float}."""

    started = time.monotonic()
    try:
        data = _result_data(tools._deepline("firecrawl_scrape", judge_payload(url)))
    except BudgetExhausted as exc:
        return {"verdict": f"infra:refused {str(exc)[:40]}", "seconds": 0.0}
    except Exception as exc:  # noqa: BLE001 - provider errors, oversized replies, timeouts
        return {"verdict": f"infra:{type(exc).__name__}: {str(exc)[:60]}", "seconds": round(time.monotonic() - started, 1)}
    seconds = round(time.monotonic() - started, 1)
    raw = data.get("rawHtml") if isinstance(data, dict) else None
    meta = data.get("metadata") if isinstance(data, dict) and isinstance(data.get("metadata"), dict) else {}
    status = meta.get("statusCode")
    if not isinstance(raw, str) or not isinstance(status, int) or isinstance(status, bool):
        return {"verdict": "infra:no rawHtml", "seconds": seconds}
    if str(meta.get("sourceURL") or url) != url or not str(meta.get("url") or url).startswith("https://"):
        return {"verdict": "infra:source_url_mismatch", "seconds": seconds}
    verdict = gates.fetch_verdict(status, raw)
    if verdict is None:
        return {"verdict": "unknown", "seconds": seconds}
    if verdict == "ok":
        text = gates.visible_text(raw) or ""
        extracted = gates.extracted_verdict(raw, text)
        verdict = extracted if extracted else verdict
    return {"verdict": verdict, "seconds": seconds}


def check(rows: list[dict[str, Any]], tools: Any, *, deadline: float, clock: Any = time.monotonic) -> dict[str, Any]:
    """Apply the verdicts to the rows in place; returns notes.  Rows left without a primary URL are removed."""

    notes: dict[str, Any] = {"checked": {}, "dropped_urls": [], "dropped_rows": []}
    budget = MAX_URLS
    verdicts: dict[str, dict[str, Any]] = {}
    for row in rows:
        for signal in row.get("intent_signals") or []:
            url = str(signal.get("url") or "")
            if url in verdicts or _ATS_RE.search(url) or budget <= 0 or clock() >= deadline:
                continue
            budget -= 1
            verdicts[url] = fetch_verdict(tools, url)
    notes["checked"] = {u: v["verdict"] for u, v in verdicts.items()}
    exa_budget = MAX_EXA
    failing = [u for u, v in verdicts.items() if v["verdict"] in CONTENT_FAILURES - HARD_FAILURES]
    primary_urls = {str(s.get("url") or "") for row in rows for s in row.get("intent_signals") or []
                    if s.get("matched_icp_signal") == 0}
    failing.sort(key=lambda u: u not in primary_urls)
    notes["exa"] = {}
    for url in failing:
        if exa_budget <= 0 or clock() >= deadline:
            result = "unknown:not checked"
        else:
            exa_budget -= 1
            result = exa_fallback(tools, url)
        notes["exa"][url] = result
        if result == "ok":
            verdicts[url] = dict(verdicts[url], verdict=EXA_OK, firecrawl=verdicts[url]["verdict"])
        elif result.startswith("unknown"):
            verdicts[url] = dict(verdicts[url], verdict=f"exa_unknown:{verdicts[url]['verdict']}")
    kept_rows = []
    for row in rows:
        signals = list(row.get("intent_signals") or [])
        keep = []
        for signal in signals:
            v = verdicts.get(str(signal.get("url") or ""))
            if v and v["verdict"] in CONTENT_FAILURES:
                notes["dropped_urls"].append(f"{signal.get('url')}: {v['verdict']}")
                continue
            keep.append(signal)
        primaries = [s for s in keep if s.get("matched_icp_signal") == 0]
        slow = [s for s in primaries if verdicts.get(str(s.get("url")), {}).get("seconds", 0) > SLOW_S]
        if slow and len(primaries) > len(slow):
            keep = [s for s in keep if s not in slow]
            notes["dropped_urls"].extend(f"{s.get('url')}: slow" for s in slow)
        if not any(s.get("matched_icp_signal") == 0 for s in keep):
            notes["dropped_rows"].append(str(row.get("company_name")))
            continue
        row["intent_signals"] = keep
        flags = row.setdefault("_flags", {})
        flags["preflight"] = [verdicts.get(str(s.get("url")), {}).get("verdict", "skipped") for s in keep]
        flags["preflight_primary"] = [verdicts.get(str(s.get("url")), {}).get("verdict", "skipped") for s in keep
                                      if s.get("matched_icp_signal") == 0]
        kept_rows.append(row)
    rows[:] = kept_rows
    return notes


__all__ = ["check", "fetch_verdict", "judge_payload", "exa_fallback", "exa_payload", "CONTENT_FAILURES",
           "HARD_FAILURES", "EXA_OK"]
