"""Evidence-first company sourcing for one ICP.

Why not a free-running tool agent: the Arena judge zeroes any company whose
submitted stage / employee bucket / country conflicts with the ICP, zeroes any
company whose primary intent cannot be verified on the cited page, and charges
a 10-point false-positive penalty on top.  Precision is the score.  So this
pipeline only submits companies that pass programmatic checks it can make
itself: bucket in ICP buckets, country match, snippet verbatim on the fetched
page, event date inside the ICP window, stage evidence when the ICP needs it.

Runs in both the Arena (LAB_ARENA_WORKER_SOCKET) and the local bakeoff runner
(BAKEOFF_TOOL_URL) through the same six semantic tools plus OpenRouter chat.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import signal
import threading
import time
from datetime import date, timedelta
from typing import Any
from agent.safe_urls import urlsplit

from agent import v92_common as v9, p1_bucket, p2_event, p3_signal, p4_fit, p5_normalize, p6_identity, p7_observations, v92_rank, judge_mirror, v12_llm, v12_secondary, v13_slots
from agent.deadline import BudgetExhausted, CALL_SECONDS, LLM_SECONDS, CALL_WINDOW, FINALIZE_SECONDS, IN_PROCESS, run_isolated

from agent.evidence import (
    BUCKETS,
    _bucket_for,
    _clean,
    _country_from_context,
    _country_ok,
    _date_in_text,
    _display_name, _host, _linkedin_identity_ok,
    _neighbor_buckets,
    _norm,
    _parse_date,
    _plain_query,
    _snippet_on_page,
)
from experiments.harness_bakeoff.models import normalize_icp, validate_companies

DEFAULT_MODEL = os.environ.get("AGENT_OPENROUTER_MODEL") or os.environ.get("BAKEOFF_OPENROUTER_MODEL") or "openai/gpt-5.6-sol"
LAST_USAGE: dict[str, Any] = {}

_CATEGORY_HINTS = {
    "PRODUCT_LAUNCH": "launches OR unveils OR introduces new product platform feature",
    "HIRING": "hiring OR job openings OR open roles OR expanding team",
    "FUNDING": "raises OR closes funding round OR secures investment",
    "FACILITY_OPENING": "opens new office OR facility OR headquarters OR plant OR location",
    "MARKET_EXPANSION": "expands into OR launches in new market OR region OR country",
    "PARTNERSHIP": "announces partnership OR collaboration OR agreement with",
    "REGULATORY_CLEARANCE": "receives FDA clearance OR approval OR regulatory authorization OR license",
    "LEADERSHIP_CHANGE": "appoints OR names new CEO OR CFO OR chief officer OR hires executive",
    "ACQUISITION": "acquires OR acquisition of OR to acquire OR merger",
}


def _now() -> float:
    return time.monotonic()


def _trace(stage: str, payload: Any) -> None:
    path = os.environ.get("AGENT_TRACE_PATH")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            row = {"t": round(time.time(), 1), "stage": stage, "data": payload}
            serialized = json.dumps(row, ensure_ascii=False, default=str)
            if len(serialized) > 20000:
                row['data'] = {'payload_truncated': True, 'preview': serialized[:1400]}
                serialized = json.dumps(row, ensure_ascii=False, default=str)
            fh.write(serialized + "\n")
    except OSError:
        pass


def _json_from(text: str) -> Any:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


async def _gather_owned(*coroutines):
    """A failed request must not leave sibling requests running unowned."""
    tasks = [asyncio.create_task(c) for c in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _funnel_mark(run, stage, candidate):
    if not hasattr(run, 'funnel'):
        run.funnel = {}
    key = v9.domain(candidate.get('domain') or candidate.get('company_website')) or str(candidate.get('company_name'))
    run.funnel.setdefault(stage, set()).add(key)
    _trace('funnel.' + stage, {'company': candidate.get('company_name'), 'domain': key})


# --------------------------------------------------------------------------- run context
class _Run:
    def __init__(self, icp: dict[str, Any], *, started: float | None = None):
        self.icp = icp
        self.arena = bool(str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip())
        self.started = _now() if started is None else started
        self.deadline = self.started + max(0.0, min(190.0, float(os.environ.get("AGENT_RUN_BUDGET_SECONDS") or 190.0)))
        # One usable tool-provider quota. Internal fallbacks count physically.
        self.budget = {'deepline': max(0, min(30, int(os.environ.get('AGENT_DEEPLINE_BUDGET') or 30)))}
        self.used = {'deepline': 0}
        self.llm_budget = max(0, min(60, int(os.environ.get('AGENT_OPENROUTER_BUDGET') or 60)))
        self._quota_lock = threading.RLock()
        self.funnel = {}
        self.max_tool_calls = sum(self.budget.values())
        self.tool_calls = 0
        self.llm_requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self.tool_lock = asyncio.Lock()
        self.page_cache, self.identity_pages, self.ranking = {}, {}, {}
        self.completed_context = {}
        self.model = DEFAULT_MODEL
        self.tools = None
        self._http = None
        self.llm = None
        self._setup_error = None
        try:
            if self.arena:
                if not v9.enabled('V14_DEEPLINE_ONLY'):
                    raise ValueError('The miner tool route cannot be disabled')
                from arena_transport import ArenaToolClient, arena_openrouter_http_client
                from openai import AsyncOpenAI
                self.tools = p6_identity.IdentityToolClient(timeout=CALL_SECONDS)
                self.tools.page_cache = self.page_cache
                self.tools.before_deepline = self._claim_deepline
                self._http = arena_openrouter_http_client(timeout=LLM_SECONDS)
                self.llm = AsyncOpenAI(api_key="arena-host", base_url="http://openrouter.ai/api/v1", http_client=self._http, max_retries=0)
            else:
                from experiments.harness_bakeoff.tool_client import ToolClient
                from openai import AsyncOpenAI
                self.tools = ToolClient(timeout=CALL_SECONDS)
                self.llm = AsyncOpenAI(api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1", max_retries=0)
        except Exception as exc:
            # Defer the error to the async owner so both partially constructed
            # clients are closed there, without creating an unowned coroutine.
            self._setup_error = exc
        self.eval_date = _parse_date(os.environ.get("LAB_ARENA_EVALUATION_DATE") or os.environ.get("BAKEOFF_EVALUATION_DATE")) or date.today()

    def remaining(self) -> float:
        return self.deadline - _now()

    def _claim_deepline(self, operation):
        with self._quota_lock:
            if self.remaining() <= 0:
                raise BudgetExhausted('planning deadline exhausted')
            from agent.v30_spend import before_call
            before_call(self,operation,_trace)
            if hasattr(self, 'v15_budget'):
                from agent import v31_signals, v32_size, v33_size, v15_budget
                lane=v15_budget.WORK.get()[0]
                ticket = v33_size.claim(self,operation) if lane=='v33_size' else v31_signals.claim(self,operation) if lane=='v31_signal' else v32_size.claim(self,operation) if lane=='v32_linkedin' else self.v15_budget.claim_dl(operation)
                self.used['deepline'] += 1
                self.tool_calls += 1
                _trace('provider.attempt', {'provider':'deepline','operation':operation,'used':self.used['deepline']})
                return ticket
            available = self.budget['deepline'] - self.used['deepline']
            lease = v12_secondary.ACTIVE.get()
            if available <= 0:
                raise BudgetExhausted('deepline budget exhausted')
            if lease:
                lease.before_call('deepline', available)
            elif available <= 2:
                raise BudgetExhausted('deepline completion reserve')
            self.used['deepline'] += 1
            self.tool_calls += 1
            _trace('provider.attempt', {'provider': 'deepline', 'operation': operation,
                                        'used': self.used['deepline']})

    async def close(self) -> None:
        close = getattr(self.tools, "close", None)
        try:
            if callable(close):
                await asyncio.to_thread(close)
        finally:
            if self.llm is not None:
                await self.llm.close()
            elif self._http is not None:
                await self._http.aclose()

    async def tool(self, name: str, args: dict[str, Any]) -> Any:
        if self.remaining() <= 0:
            raise BudgetExhausted('planning deadline exhausted')
        if name != 'submit_companies' and not self.arena:
            self._claim_deepline(name)
        try:
            call_limit = CALL_SECONDS
            if name in ('harvestapi_search_leads', 'harvestapi_get_profile'):
                from agent.v35_contacts import CONTACT_CALL_TIMEOUT
                contact_limit = CONTACT_CALL_TIMEOUT.get()
                if contact_limit is not None:call_limit = contact_limit
            timeout = min(call_limit, self.remaining())
            expires = _now() + timeout
            def call():
                remaining = lambda: min(self.remaining(), expires - _now())
                if remaining() <= 0:
                    raise BudgetExhausted('queued request deadline exhausted')
                token = CALL_WINDOW.set(remaining)
                try:
                    # Never mutate a timeout shared by concurrent calls.
                    client = copy.copy(self.tools)
                    client.timeout = min(call_limit, remaining())
                    return client.call(name, args)
                finally:
                    CALL_WINDOW.reset(token)
            result = await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout)
            _trace('tool.result', {'tool': name, 'arguments': args, 'result': result})
            return result
        except Exception as exc:  # tool errors are data, not fatal
            if name == "submit_companies":
                raise
            _trace("tool.error", {"tool": name, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    async def ask(self, system: str, user: str, *, max_tokens: int = 2500, planning: bool = False) -> Any:
        return await v12_llm.ask(self, system, user, max_tokens=max_tokens, planning=planning,
                                 trace=_trace, parse_legacy=_json_from,
                                 llm_seconds=LLM_SECONDS, budget_error=BudgetExhausted)


def _icp_brief(icp: dict[str, Any]) -> str:
    keys = ("industry", "sub_industry", "geography", "country", "employee_count", "company_stage", "product_service", "required_attribute", "required_intents", "bonus_intents", "excluded_companies", "prompt")
    return json.dumps({k: icp.get(k) for k in keys if icp.get(k) not in (None, "", [])}, ensure_ascii=False)


async def _plan_queries(run: _Run) -> list[dict[str, Any]]:
    icp = run.icp
    intent = icp["required_intents"][0]
    cat = str(intent.get("category") or "").upper()
    stage = _clean(icp.get("company_stage"))
    plan = await run.ask(
        "You write web search queries that surface SPECIFIC companies which recently did an event. Output JSON only.",
        "ICP:\n" + _icp_brief(icp) + "\n\n"
        f"Required event: {intent['signal']} (category {cat}, within the last {intent.get('max_age_days', 365)} days).\n"
        "Write 5 diverse PLAIN-LANGUAGE search queries of 5-10 words, the way a journalist would type them into Google News. "
        "Each combines the industry/product niche, one or two event words (e.g. " + _CATEGORY_HINTS.get(cat, "announces").split(" OR ")[0] + "), the country or region, and, when the ICP names a company stage, that stage phrase. "
        "STRICT RULES: no search operators of any kind (no site:, inurl:, after:, minus signs, OR, parentheses, quotation marks). No company names. "
        "Return {\"queries\":[{\"q\":\"...\",\"mode\":\"news|search|jobs\"}]}.",
        max_tokens=600, planning=True,
    )
    queries = []
    if isinstance(plan, dict):
        for row in plan.get("queries") or []:
            q = _plain_query((row or {}).get("q"))
            mode = str((row or {}).get("mode") or "news").lower()
            if q and mode in ("news", "search", "jobs"):
                queries.append({"q": q, "mode": mode})
    niche = _clean(icp.get("sub_industry") or icp.get("industry"))
    event_word = _CATEGORY_HINTS.get(cat, "announces").split(" OR ")[0]
    stage_q = f" {stage}" if stage and stage.lower() != "any" else ""
    country = _clean(icp.get("country") or icp.get("geography"))
    if cat == "HIRING":
        fallback = [
            {"q": _plain_query(f"{stage} vertical SaaS data platform {country} customer data engineer"), "mode": "jobs"},
            {"q": _plain_query(f"subscription data analytics platform {country} analytics engineer jobs"), "mode": "jobs"},
            {"q": _plain_query(f"{stage} business intelligence {country} revenue operations jobs"), "mode": "jobs"},
        ]
    else:
        fallback = [
            {"q": _plain_query(f"{niche} startup {event_word}{stage_q} {country}"), "mode": "news"},
            {"q": _plain_query(f"{niche} company {event_word} {country}"), "mode": "news"},
        ]
    seen = set()
    out = []
    planned = fallback + queries[:3] if cat == "HIRING" else queries[:4] + fallback
    for q in planned:
        key = q["q"].lower()
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:6]


def _exa_start(run: _Run, days: int) -> str:
    return (run.eval_date - timedelta(days=max(1, int(days)))).isoformat()


async def _exa_news(run: _Run, query: str, days: int, n: int = 6, category: str = "news") -> list[dict[str, Any]]:
    args = {"query": query, "numResults": n, "startPublishedDate": _exa_start(run, days), "contents": {"text": {"maxCharacters": 600}}}
    if category:
        args["category"] = category
    res = await run.tool("exa_search", args)
    rows = []
    for r in (res or {}).get("results") or []:
        if r.get("url"):
            rows.append({"url": r["url"], "title": _clean(r.get("title"))[:160], "date": _clean(r.get("date"))[:10], "snippet": _clean(r.get("text"))[:500], "mode": "exa"})
    return rows


async def _discover(run: _Run, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days = int(run.icp["required_intents"][0].get("max_age_days") or 365)
    hiring = str(run.icp["required_intents"][0].get("category") or "").upper() == "HIRING"
    jobs = [q for q in queries if q["mode"] == "jobs"]
    other = [q for q in queries if q["mode"] != "jobs"]
    exa_q = (jobs + other)[:3] if hiring else other[:3]
    google_q = jobs[:2] if hiring else jobs[:1] + other[3:4]
    async def exa(q):
        return await _exa_news(run, q["q"], days, 6, "news" if q["mode"] == "news" else "")
    async def google(q):
        res = await run.tool("search_web", {"query": q["q"], "mode": q["mode"], "limit": 5, "recency_days": min(days, 3650)})
        return [{"url": str(r.get("url") or ""), "title": _clean(r.get("title"))[:160], "date": _clean(r.get("date"))[:32], "snippet": _clean(r.get("snippet"))[:500], "mode": q["mode"]} for r in (res or {}).get("results") or []]
    groups = await asyncio.gather(*([exa(q) for q in exa_q] + [google(q) for q in google_q]), return_exceptions=True)
    rows, seen = [], set()
    for group in groups:
        if isinstance(group, BaseException):
            _trace('discover.error', {'error': str(group)[:160]})
            continue
        for r in group:
            url = r.get("url")
            if url and url not in seen:
                seen.add(url)
                rows.append(r)
    return rows


async def _extract_candidates(run: _Run, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    icp = run.icp
    intent = icp["required_intents"][0]
    excluded = [str(x).lower() for x in icp.get("excluded_companies") or []]
    hiring_rule = (
        "For HIRING, an ATS or job-board URL is valid evidence: extract the actual employer named in the posting and return the employer's primary website domain, never the ATS/job-board domain. "
        "If a relevant result title names both the employer and requested role but its body collapsed to an ATS or login landing page, still return the employer; a later stage will locate the exact posting. "
        if str(intent.get("category") or "").upper() == "HIRING" else ""
    )
    data = await run.ask(
        "You extract candidate companies from search results for B2B prospecting. Be strict about fit. Output JSON only.",
        "ICP:\n" + _icp_brief(icp) + f"\n\nEvaluation date: {run.eval_date.isoformat()}\n"
        f"Required event: {intent['signal']}\n\nSearch results:\n" + json.dumps(rows, ensure_ascii=False) + "\n\n"
        "List up to 8 DISTINCT companies that (1) are the SUBJECT of the required event in one of these results (the company that did it; not the acquired target, not a vendor mentioned in passing, not a list article unless it names one company), "
        "(2) plausibly match the ICP industry and headquarters country, and (3) are not in excluded_companies. Do NOT drop a company because its funding stage or size is unknown. "
        + hiring_rule +
        "Skip universities, governments and non-companies. When company_stage is 'Public', prefer publicly listed companies; when it is an early funding round, prefer startups. "
        "Return {\"candidates\":[{\"company_name\":\"\",\"domain\":\"primary website domain, best guess\",\"evidence_url\":\"the result url that reports the event\",\"event_date\":\"YYYY-MM-DD or empty\",\"event\":\"one sentence\",\"stage_hint\":\"funding stage if the result states it, else empty\",\"confidence\":0.0}]} ordered by confidence.",
        max_tokens=1800,
    )
    out, seen = [], set()
    row_dates = {r["url"]: r.get("date") for r in rows}
    for c in (data or {}).get("candidates") or [] if isinstance(data, dict) else []:
        name = _clean(c.get("company_name"))
        dom = _host(str(c.get("domain") or ""))
        url = str(c.get("evidence_url") or "")
        if not name or not url or not url.startswith("http"):
            continue
        key = dom or name.lower()
        if key in seen or any(x and (x in key or key in x or x in name.lower()) for x in excluded):
            continue
        seen.add(key)
        out.append({"company_name": name, "domain": dom, "evidence_url": url, "event_date": _clean(c.get("event_date")), "result_date": _clean(row_dates.get(url)), "event": _clean(c.get("event")), "stage_hint": _clean(c.get("stage_hint")), "confidence": float(c.get("confidence") or 0)})
    return out[:8]


_STAGE_WORDS = {"seed": "seed round", "series a": "Series A", "series b": "Series B", "series c": "Series C", "series d": "Series D", "public": "publicly traded"}
_FUNDING_STAGE_RE = re.compile(r"\b(debt financing|debt facility|credit facility|private equity|pre[- ]seed|seed|series\s+[a-h])\b", re.I)
_PUBLIC_RE = re.compile(r"\b(NYSE|NASDAQ|Nasdaq|TSX|LSE|ASX|publicly traded|publicly listed|public company|ticker)\b")
def _funding_stage(text: str) -> str:
    cleaned = _clean(text)
    enumeration = v12_secondary.observed_enumeration(cleaned)
    if enumeration:
        return enumeration
    match = _FUNDING_STAGE_RE.search(cleaned)
    if not match:
        return ""
    value = match.group(1).lower().replace("-", " ")
    if value in {"debt financing", "debt facility", "credit facility"}:
        return "Debt"
    if value == "private equity":
        context = cleaned.lower()
        if re.search(r"\b(no|not|isn't|is not)\b.{0,25}\bprivate equity\b", context) or not re.search(
            r"private equity[- ]backed|private equity (firm|fund|investment)|"
            r"(acquired|bought|buyout|investment|invested).{0,80}private equity|"
            r"private equity.{0,80}(acquired|bought|buyout|investment|invested)",
            context,
        ):
            return ""
    if value.startswith("series "):
        return "Series " + value[-1].upper()
    return "Private Equity" if value == "private equity" else ("Pre-Seed" if value == "pre seed" else "Seed")
def _stage_matches(observed: str, wanted: str) -> bool:
    if v12_secondary.enumeration_matches(observed, wanted):
        return True
    observed_norm, wanted_norm = _norm(observed), _norm(wanted)
    if wanted_norm == "series c" and "+" in str(wanted):
        return observed_norm in {f"series {letter}" for letter in "cdefgh"}
    return observed_norm == wanted_norm


async def _stage_first_candidates(run: _Run) -> list[dict[str, Any]]:
    """Find companies whose funding stage is stated in the news, then look for the event per company."""
    _trace("stage_first.start", {"seconds": round(_now() - run.started, 3)})
    icp = run.icp
    stage = _clean(icp.get("company_stage"))
    niche = _clean(icp.get("sub_industry") or icp.get("industry"))
    country = _clean(icp.get("country") or icp.get("geography"))
    if _norm(stage) == "public":
        qs = [
            {"q": _plain_query(f"{niche} publicly traded company {country} {_CATEGORY_HINTS.get(str(icp['required_intents'][0].get('category') or '').upper(), 'announces').split(' OR ')[0]}"), "mode": "news"},
            {"q": _plain_query(f"NYSE Nasdaq listed {niche} company {country} announces"), "mode": "news"},
        ]
        days = 400
    else:
        qs = [
            {"q": _plain_query(f"{niche} startup raises {stage} funding {country}"), "mode": "news"},
            {"q": _plain_query(f"{niche} company closes {stage} round {country}"), "mode": "news"},
            {"q": _plain_query(f"{stage} {icp.get('industry')} {country} funding announcement"), "mode": "search"},
        ]
        days = 1100
    async def one(q):
        return await _exa_news(run, q["q"], days, 6, "news")
    results = await _gather_owned(*(one(q) for q in qs[:2]))
    rows, seen = [], set()
    for res in results:
        for r in res:
            url = r["url"]
            if url and url not in seen:
                seen.add(url)
                rows.append({"url": url, "title": r["title"], "date": r["date"], "snippet": r["snippet"]})
    _trace("stage_discover", {"icp": icp.get("icp_id"), "queries": qs[:2], "n": len(rows), "rows": rows[:20]})
    if not rows:
        return []
    data = await run.ask(
        "You extract companies and their funding stage from search results. Output JSON only.",
        "ICP:\n" + _icp_brief(icp) + "\n\nSearch results:\n" + json.dumps(rows, ensure_ascii=False) + "\n\n"
        f"List up to 8 DISTINCT companies whose stage is EXPLICITLY stated in a result as '{stage}' (for 'Public': the result shows the company is publicly listed, e.g. a NYSE/NASDAQ ticker), "
        "that plausibly match the ICP industry and headquarters country, and are not in excluded_companies. Skip list articles that do not name a specific company. "
        "Return {\"candidates\":[{\"company_name\":\"\",\"domain\":\"primary website domain, best guess\",\"stage_url\":\"result url stating the stage\",\"stage_quote\":\"short phrase from the result stating the stage\",\"confidence\":0.0}]}",
        max_tokens=1500,
    )
    out, seen_keys = [], set()
    excluded = [str(x).lower() for x in icp.get("excluded_companies") or []]
    for c in (data or {}).get("candidates") or [] if isinstance(data, dict) else []:
        name = _clean(c.get("company_name")); dom = _host(str(c.get("domain") or ""))
        if not name or not dom:
            continue
        key = dom
        if key in seen_keys or any(x and (x in key or x in name.lower()) for x in excluded):
            continue
        seen_keys.add(key)
        out.append({"company_name": name, "domain": dom, "evidence_url": "", "event_date": "", "result_date": "", "event": "", "stage_hint": stage, "stage_url": str(c.get("stage_url") or ""), "confidence": float(c.get("confidence") or 0), "source": "stage_first"})
    _trace("stage_candidates", {"icp": icp.get("icp_id"), "candidates": out})
    return out[:8]


async def _company_search_candidates(run: _Run) -> list[dict[str, Any]]:
    """Exa company search: niche + stage + country; entity gives workforce/HQ so we can pre-filter fit."""
    icp = run.icp
    stage = _clean(icp.get("company_stage"))
    niche = _clean(icp.get("sub_industry") or icp.get("industry"))
    country = _clean(icp.get("country") or icp.get("geography"))
    buckets = [b for b in (icp.get("employee_count") or []) if b in BUCKETS]
    stage_q = f" {stage}" if stage and stage.lower() != "any" else ""
    q = f"{niche} company{stage_q} headquartered in {country} {_clean(icp.get('product_service'))[:80]}"
    res = await run.tool("exa_company_search", {"query": q, "numResults": 8})
    out = []
    for r in (res or {}).get("results") or []:
        ent = r.get("entity") or {}
        dom = _host(r.get("url") or "")
        name = _clean(ent.get("name") or r.get("title"))
        if not dom or not name:
            continue
        wf = ent.get("workforce") if isinstance(ent.get("workforce"), dict) else {}
        b = _bucket_for(wf.get("total"))
        if b and buckets and b not in buckets:
            continue
        hq = ent.get("headquarters") if isinstance(ent.get("headquarters"), dict) else {}
        if hq.get("country") and _country_ok(str(hq.get("country")), country) is False:
            continue
        out.append({"company_name": name, "domain": dom, "evidence_url": "", "event_date": "", "result_date": "", "event": "", "stage_hint": "", "confidence": 0.5, "source": "company_search", "entity": ent})
    _trace("company_search", {"icp": icp.get("icp_id"), "q": q, "candidates": [(c["company_name"], c["domain"], (c["entity"].get("workforce") or {}).get("total")) for c in out]})
    return out[:6]


async def _hunter_candidates(run: _Run) -> list[dict[str, Any]]:
    """Last-resort Hunter discovery with provider-side size/HQ filters."""
    icp = run.icp
    country = _clean(icp.get("country") or icp.get("geography"))
    buckets = [b for b in (icp.get("employee_count") or []) if b in BUCKETS]
    query = _plain_query(
        f"{icp.get('sub_industry') or icp.get('industry')} "
        f"{icp.get('product_service') or icp.get('required_attribute')}"
    )
    res = await run.tool("search_companies", {
        "query": query,
        "industry": _clean(icp.get("industry")),
        "geography": country,
        "employee_count": buckets,
        "limit": 6,
    })
    out = []
    for company in (res or {}).get("companies") or []:
        name = _clean(company.get("company_name"))
        domain = _host(str(company.get("domain") or company.get("company_website") or ""))
        bucket = _bucket_for(company.get("employee_count"))
        if not name or not domain or (bucket and buckets and bucket not in buckets):
            continue
        if _country_ok(str(company.get("location") or ""), country) is False:
            continue
        out.append({
            "company_name": name,
            "domain": domain,
            "evidence_url": "",
            "event_date": "",
            "result_date": "",
            "event": "",
            "stage_hint": "",
            "confidence": 0.35,
            "source": "hunter",
        })
    _trace("hunter_candidates", {
        "icp": icp.get("icp_id"),
        "query": query,
        "filters": {"employee_count": buckets, "geography": country},
        "candidates": [(c["company_name"], c["domain"]) for c in out],
    })
    return out[:6]


def _predictleads_rows(payload: Any) -> list[dict[str, Any]]:
    """Project PredictLeads event/job responses into evidence candidates."""
    rows: list[dict[str, Any]] = []
    for group in (payload or {}).get("events") or []:
        data = group.get("data") if isinstance(group, dict) else {}
        for item in (data or {}).get("items") or []:
            attrs = item.get("attributes") if isinstance(item, dict) else {}
            related = item.get("related") if isinstance(item, dict) else {}
            source = (related or {}).get("most_relevant_source") or {}
            url = str(source.get("url") or (attrs or {}).get("url") or "")
            if not url.startswith("http"):
                continue
            published = next((
                (attrs or {}).get(key)
                for key in ("effective_date", "posted_at", "found_at", "first_seen_at")
                if (attrs or {}).get(key)
            ), source.get("published_at"))
            text = " — ".join(filter(None, (
                _clean(source.get("title")),
                _clean((attrs or {}).get("title") or (attrs or {}).get("job_title")),
                _clean((attrs or {}).get("summary")),
                _clean((attrs or {}).get("article_sentence")),
                _clean((attrs or {}).get("location")),
            )))
            rows.append({"url": url, "date": _clean(published)[:32], "text": text[:700], "via": "predictleads"})
    return rows


async def _find_event_evidence(run: _Run, cand: dict[str, Any]) -> list[dict[str, Any]]:
    """Return candidate evidence rows {url,date,text} for the ICP's required event for one company."""
    if cand.get("source") == "stage_first" and v9.enabled("V92_OWN_EVENT_FIRST"):
        rows = await p2_event.event_evidence(run, cand)
        _trace("event_evidence", {"company": cand["company_name"], "route": "v92_own_first", "rows": rows})
        return rows
    icp = run.icp
    intent = icp["required_intents"][0]
    cat = str(intent.get("category") or "NEWS").upper()
    max_age = int(intent.get("max_age_days") or 365)
    word = _CATEGORY_HINTS.get(cat, "announces").split(" OR ")[0]
    signal = str(intent.get("signal") or "")
    exa_rows = await _exa_news(run, f"{cand['company_name']} {word} {signal[:60]}", max_age, 6, "news" if cat != "HIRING" else "")
    rows: list[dict[str, Any]] = [{"url": r["url"], "date": r["date"], "text": (r["title"] + " — " + r["snippet"])[:300], "via": "exa"} for r in exa_rows]
    first = cand["company_name"].split()[0].lower()
    rows = [r for r in rows if first in (r["text"] + " " + r["url"]).lower()]
    if not rows and cand.get("domain") and run.remaining() > 60:
        events = await run.tool("get_company_events", {"domain": cand["domain"], "categories": [cat], "limit": 5})
        rows.extend(_predictleads_rows(events))
    if not rows and run.remaining() > 60:
        web = await run.tool("search_web", {"query": _plain_query(f"{cand['company_name']} {word}"), "mode": "news" if cat != "HIRING" else "jobs", "limit": 4, "recency_days": max_age})
        for r in (web or {}).get("results") or []:
            url = str(r.get("url") or "")
            if url:
                rows.append({"url": url, "date": _clean(r.get("date"))[:32], "text": (_clean(r.get("title")) + " — " + _clean(r.get("snippet")))[:300], "via": "web"})
    rows = [r for r in rows if first in (r["text"] + " " + r["url"]).lower()]
    # keep in-window rows first, then unknown dates
    def in_window(row):
        d = _parse_date(row["date"])
        return 0 if d and 0 <= (run.eval_date - d).days <= max_age else (1 if d is None else 2)
    rows.sort(key=in_window)
    rows = [r for r in rows if in_window(r) < 2]
    _trace("event_evidence", {"company": cand["company_name"], "rows": rows[:8]})
    return rows[:6]


async def _pick_evidence(run: _Run, cand: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    icp = run.icp
    intent = icp["required_intents"][0]
    if v9.enabled('V13_SOURCE_WEIGHT'):
        rows = v13_slots.ordered_pages(rows, 'https://' + cand['domain'])
    _trace("evidence.pick", {"company": cand["company_name"], "rows": len(rows)})
    pick = await run.ask(
        "Pick the single best evidence page. Output JSON only.",
        f"Company: {cand['company_name']} ({cand['domain']})\nRequired event: {intent['signal']}\nCandidate pages:\n" + json.dumps(rows, ensure_ascii=False)
        + ("\nAmong equally strong same-event evidence, prefer jobs/careers or GitHub, then external news, then the company site. A page category never proves the event. " if v9.enabled('V13_SOURCE_WEIGHT') else '')
        + "\n\nReturn {\"url\":\"the page most likely to state, on the page itself, that this company did the required event; empty if none plausibly does\",\"event_date\":\"YYYY-MM-DD if a listed date applies, else empty\"}",
        max_tokens=200,
    )
    if not isinstance(pick, dict) or not str(pick.get("url") or "").startswith("http"):
        _trace("verify.reject", {"company": cand["company_name"], "why": "no_evidence_picked"})
        return None
    url = str(pick["url"])
    row = next((r for r in rows if r["url"] == url), None)
    if row is None:
        return None
    return {**cand, "evidence_url": url, "event_date": _clean(pick.get("event_date")), "result_date": _clean((row or {}).get("date"))}


async def _page_text(run: _Run, url: str) -> dict[str, Any]:
    if url in run.page_cache:
        return run.page_cache[url]
    page = await run.tool("fetch_page", {"url": url, "max_chars": 8000})
    result = {**(page or {}), "text": _clean((page or {}).get("text")), "source": "fetch"}
    run.page_cache[url] = result
    return result


async def _second_verified_signal(run: _Run, cand: dict[str, Any], primary_url: str) -> dict[str, Any] | None:
    """Find independent same-intent evidence with one Exa call and one LLM call."""
    if run.remaining() < 60:
        _trace("second_signal.skip", {"company": cand["company_name"], "why": "time"})
        return None
    intent = run.icp["required_intents"][0]
    category = str(intent.get("category") or "NEWS").upper()
    max_age = int(intent.get("max_age_days") or 365)
    event_word = _CATEGORY_HINTS.get(category, "announces").split(" OR ")[0]
    try:
        rows = await _exa_news(run, f"{cand['company_name']} {event_word} {str(intent.get('signal') or '')[:80]}", max_age, 8, "" if category == "HIRING" else "news")
    except Exception as exc:
        _trace("second_signal.skip", {"company": cand["company_name"], "why": type(exc).__name__})
        return None
    primary_host = _host(primary_url)
    identity_words = {word for word in _norm(cand["company_name"]).split() if len(word) > 2 and word not in {"the", "company", "global", "group", "holdings"}}
    candidates = []
    for row in rows:
        observed = _parse_date(row.get("date"))
        text = _clean(f"{row.get('title')} — {row.get('snippet')}")
        if _host(row.get("url")) in ("", primary_host) or not identity_words.intersection(_norm(text).split()) or not observed or not 0 <= (run.eval_date - observed).days <= max_age:
            continue
        candidates.append({"url": row["url"], "date": observed.isoformat(), "text": text[:700]})
    if not candidates:
        _trace("second_signal.skip", {"company": cand["company_name"], "why": "no_independent_page"})
        return None
    verdict = await run.ask(
        "Verify independent event evidence. Quote the supplied page text VERBATIM. Output JSON only.",
        f"Company: {cand['company_name']} ({cand['domain']})\nRequired event: {intent['signal']}\nPages: {json.dumps(candidates, ensure_ascii=False)}\n"
        "Choose one page that independently says THIS company did the required event. Return {\"verified\":true/false,\"url\":\"exact supplied URL\",\"date\":\"YYYY-MM-DD\",\"snippet\":\"verbatim supplied text\",\"description\":\"<=300 chars\",\"why_now\":\"<=400 chars\"}.",
        max_tokens=650,
    )
    row = next((item for item in candidates if item["url"] == str((verdict or {}).get("url") or "")), None)
    snippet = _snippet_on_page(str((verdict or {}).get("snippet") or ""), row["text"] if row else "")
    observed = _parse_date((row or {}).get("date"))
    if not isinstance(verdict, dict) or not verdict.get("verified") or not row or not snippet or not observed or not 0 <= (run.eval_date - observed).days <= max_age:
        _trace("second_signal.skip", {"company": cand["company_name"], "why": "unverified"})
        return None
    signal = {"matched_icp_signal": 0, "description": (_clean(verdict.get("description")) or str(intent["signal"]))[:350], "date": observed.isoformat(), "why_now": (_clean(verdict.get("why_now")) or "Independent recent evidence strengthens the timing signal.")[:600], "url": row["url"], "snippet": snippet[:600]}
    _trace("second_signal.accept", {"company": cand["company_name"], "url": row["url"], "date": signal["date"]})
    return signal


async def _identity(run, cand):
    if cand.get('_v92_identity_resolved'):
        return cand
    if v9.enabled('V92_IDENTITY'):
        result = await p6_identity.resolve_identity(run, cand)
        return {**result, '_v92_identity_resolved': True} if result else None
    # Other independently enabled rules may read the homepage, but P6 OFF
    # must not turn a failed homepage into a new rejection gate.
    url = f"https://{cand['domain']}/"
    if url not in run.identity_pages:
        run.identity_pages[url] = await run.tool('fetch_page', {'url': url, 'max_chars': 4000})
    return {**cand, '_home': run.identity_pages[url], '_v92_identity_resolved': True}


async def _verify(run: _Run, cand: dict[str, Any]) -> dict[str, Any] | None:
    if v9.enabled('V92_SECOND_SIGNAL') and not v9.signal_url_ok(cand['evidence_url']):
        _trace('verify.reject', {'company': cand['company_name'], 'why': 'signal_source'})
        return None
    cand = await _identity(run, cand)
    if not cand:
        return None
    icp = run.icp
    intent = icp["required_intents"][0]
    max_age = int(intent.get("max_age_days") or 365)
    buckets = [b for b in (icp.get("employee_count") or []) if b in BUCKETS]
    want_stage = _clean(icp.get("company_stage"))
    stage_required = bool(want_stage) and want_stage.lower() != "any"
    country = _clean(icp.get("country") or icp.get("geography"))

    # 1. firmographics + primary evidence and optional undated ATS fit page
    profile_task = run.tool("get_company_profile", {"domain": cand["domain"]}) if cand["domain"] else asyncio.sleep(0, result={})
    page_task = _page_text(run, cand["evidence_url"])
    fit_url = str(cand.get("fit_url") or "")
    fit_task = _page_text(run, fit_url) if fit_url and fit_url != cand["evidence_url"] else asyncio.sleep(0, result={})
    profile, page, fit_page = await _gather_owned(profile_task, page_task, fit_task)
    company = (profile or {}).get("company") or {}
    profile_count = bool(company.get('employee_count'))
    ent = cand.get("entity") or {}
    if not company.get("employee_count") and isinstance(ent.get("workforce"), dict) and ent["workforce"].get("total"):
        company["employee_count"] = ent["workforce"]["total"]
    if not company.get("location") and isinstance(ent.get("headquarters"), dict):
        hq = ent["headquarters"]
        company["location"] = ", ".join(str(hq.get(k) or "") for k in ("city", "country") if hq.get(k))
    page_text = _clean((page or {}).get("text"))
    final_url = page.get('url') or cand['evidence_url']
    if v9.enabled('V92_SECOND_SIGNAL') and not v9.signal_url_ok(final_url):
        return None
    cand['evidence_url'] = final_url
    fit_text = _clean((fit_page or {}).get("text"))
    _trace("verify.fetch", {"company": cand["company_name"], "profile": company, "page_len": len(page_text), "fit_page_len": len(fit_text), "page_err": (page or {}).get("error")})
    if len(page_text) < 200:
        return None
    exact_count = p1_bucket.exact_headcount(cand, [page, fit_page, cand.get('_home', {})])
    observed_bucket = p1_bucket.choose_bucket(company.get('employee_count'), buckets, exact=exact_count, profile=profile_count)
    _trace('bucket_observation', {'company': cand['company_name'], 'profile_value': company.get('employee_count'),
                                 'page_exact': exact_count, 'selected': observed_bucket})
    if observed_bucket and buckets and observed_bucket not in buckets:
        _trace("verify.reject", {"company": cand["company_name"], "why": "bucket", "observed": observed_bucket})
        return None
    loc_ok = _country_ok(str(company.get("location") or ""), country)
    if loc_ok is False:
        _trace("verify.reject", {"company": cand["company_name"], "why": "country_profile", "location": company.get("location")})
        return None
    website = f"https://{cand['domain']}/" if cand["domain"] else ""
    linkedin = str(company.get("linkedin_url") or "")
    if linkedin and not linkedin.startswith("http"):
        linkedin = "https://" + linkedin
    if p7_observations.linkedin_conflict(cand, linkedin):
        _trace('verify.reject', {'company': cand['company_name'], 'why': 'identity_linkedin_conflict'})
        return None
    if cand.get('_omit_linkedin_hint'):
        _trace('fit_hint.skip', {'company': cand['company_name'], 'why': 'identity_linkedin_conflict'})

    # 2. LLM reads the page: verbatim snippet, date, stage/attribute evidence, fit fields
    verdict = await run.ask(
        "You are a strict evidence checker. Quote text VERBATIM from the page. Output JSON only.",
        "ICP:\n" + _icp_brief(icp) + f"\n\nEvaluation date: {run.eval_date.isoformat()}\nCompany: {cand['company_name']} ({cand['domain'] or 'domain unknown'})\n"
        f"Required event: {intent['signal']}\nPage URL: {cand['evidence_url']}\nPage text:\n\"\"\"\n{page_text[:1400]}\n\"\"\"\n\n"
        + (f"Supporting fit/stage URL: {fit_url}\nSupporting text:\n\"\"\"\n{fit_text[:1400]}\n\"\"\"\n\n" if fit_text else "")
        + "Answer: does the primary Page text itself state that THIS company (the subject, not a partner/acquirer/vendor mentioned in passing) did the required event? The event snippet/date must come from the primary Page text; stage and attribute evidence may come from either supplied page. Judge the CONTENT only; ignore whether a date is printed. Return {"
        "\"event_verified\":true/false, \"snippet\":\"verbatim sentence(s) from the page proving it, 1-2 sentences, max 400 chars\", "
        "\"event_date\":\"YYYY-MM-DD if the page or its dateline states when the event happened/was announced, else empty\", \"description\":\"<=300 chars factual description of the event\", "
        "\"why_now\":\"<=400 chars sales angle\", "
        f"\"stage_stated\":\"funding stage the page states for this company (e.g. Series A) or empty\", "
        "\"attribute_quote\":\"verbatim page text showing the company sells what required_attribute/product_service describes, or empty\", "
        "\"country\":\"HQ country if the page states it, else empty\", \"state\":\"US state/region if stated else empty\", "
        "\"industry_ok\":true/false, \"fit_summary\":\"<=300 chars why it fits the ICP\"}",
        max_tokens=900,
    )
    _trace("verify.verdict", {"company": cand["company_name"], "verdict": verdict})
    if not isinstance(verdict, dict) or not verdict.get("event_verified"):
        _trace("verify.reject", {"company": cand["company_name"], "why": "event_not_verified"})
        return None
    snippet = _snippet_on_page(str(verdict.get("snippet") or ""), page_text)
    if not snippet:
        _trace("verify.reject", {"company": cand["company_name"], "why": "snippet_not_on_page"})
        return None
    ev_date = (
        _parse_date(verdict.get("event_date"))
        or _parse_date(cand.get("event_date"))
        or _parse_date(cand.get("result_date"))
        or _date_in_text(page_text)
    )
    if not ev_date or ev_date > run.eval_date + timedelta(days=3) or (run.eval_date - ev_date).days > max_age:
        _trace("verify.reject", {"company": cand["company_name"], "why": "date", "date": str(ev_date)})
        return None
    if v9.enabled('V92_SECOND_SIGNAL') and v9.domain(cand['evidence_url']) != v9.domain(cand['domain']) and not v9.observed_date(page):
        _trace('verify.reject', {'company': cand['company_name'], 'why': 'press_without_dateline'})
        return None
    raw_attr = str(verdict.get("attribute_quote") or "")
    _funnel_mark(run, 'primary_verified', cand)
    attr_quote = _snippet_on_page(raw_attr, fit_text) if fit_text else ""
    attr_url = fit_url if attr_quote else cand["evidence_url"]
    if not attr_quote:
        attr_quote = _snippet_on_page(raw_attr, page_text)
    if verdict.get("industry_ok") is False and not (icp.get("required_attribute") and attr_quote):
        _trace("verify.reject", {"company": cand["company_name"], "why": "industry"})
        return None
    if _country_ok(str(verdict.get("country") or ""), country) is False:
        _trace("verify.reject", {"company": cand["company_name"], "why": "country_page", "page_country": verdict.get("country")})
        return None
    v12_secondary.primary_passed()
    inferred_state = ""
    if loc_ok is None and _country_ok(str(verdict.get("country") or ""), country) is not True:
        ctx_ok, inferred_state = _country_from_context(page_text, country)
        if ctx_ok is not True and v9.enabled('V92_SMALL'):
            home_ok, home_state = p7_observations.homepage_country(cand.get('_home', {}), country)
            if home_ok is True:
                ctx_ok, inferred_state = home_ok, home_state
                _trace('country_observation', {'company': cand['company_name'], 'source': 'homepage', 'state': home_state})
        if ctx_ok is not True and run.remaining() > 45:
            hq = await run.tool("search_web", {"query": _plain_query(f"{cand['company_name']} headquarters {cand['domain']}"), "mode": "search", "limit": 3, "recency_days": 3650})
            blob = " ".join(_clean(r.get("title")) + " " + _clean(r.get("snippet")) for r in (hq or {}).get("results") or [])
            ctx_ok = _country_ok(blob, country) or _country_from_context(blob, country)[0]
        if ctx_ok is not True:
            _trace("verify.reject", {"company": cand["company_name"], "why": "country_unobserved"})
            return None  # nobody observed the country
    stage_stated = _clean(verdict.get("stage_stated") or cand.get("stage_hint"))
    if stage_required:
        if _norm(want_stage) == "public":
            listed = bool(_PUBLIC_RE.search(page_text)) or _norm(stage_stated) in ("public", "publicly traded", "publicly listed") or bool(company.get("ticker"))
            if not listed and stage_stated and not _stage_matches(stage_stated, want_stage):
                _trace('verify.reject', {'company': cand['company_name'], 'why': 'stage_conflict', 'stated': stage_stated})
                return None
            if not listed:
                extra = await run.tool("search_web", {"query": _plain_query(f"{cand['company_name']} NYSE Nasdaq stock ticker"), "mode": "search", "limit": 3, "recency_days": 3650})
                listed = any(_PUBLIC_RE.search(_clean(r.get("title")) + " " + _clean(r.get("snippet")) + " " + str(r.get("url"))) and cand["company_name"].split()[0].lower() in (_clean(r.get("title")) + " " + _clean(r.get("snippet"))).lower() for r in (extra or {}).get("results") or [])
            if not listed:
                if not v12_secondary.unconfirmed_allowed():
                    _trace("verify.reject", {"company": cand["company_name"], "why": "not_public"})
                    return None
                _trace('stage.unproven', {'company': cand['company_name'], 'decision': 'keep', 'query': 'public listing'})
                stage_stated = ''
            else:
                stage_stated = want_stage
        elif stage_stated and not _stage_matches(stage_stated, want_stage) and cand.get("source") != "stage_first":
            _trace("verify.reject", {"company": cand["company_name"], "why": "stage_conflict", "stated": stage_stated})
            return None
        else:
            financials = cand.get("entity", {}).get("financials", {}) if isinstance(cand.get("entity"), dict) else {}
            entity_stage = _funding_stage(json.dumps((financials or {}).get("fundingLatestRound"), default=str))
            if entity_stage:
                _trace("stage_probe", {"company": cand["company_name"], "source": "entity", "observed": entity_stage})
                if not _stage_matches(entity_stage, want_stage):
                    _trace("verify.reject", {"company": cand["company_name"], "why": "stage_conflict_latest", "want": want_stage, "observed": entity_stage})
                    return None
                stage_stated = want_stage
            else:
                stage_query = _plain_query(f"{cand['company_name']} {cand['domain']} latest funding round {run.eval_date.year}")
                extra = await _exa_news(run, stage_query, 1500, 6, "news")
                first = cand["company_name"].split()[0].lower()
                rounds = []
                for index, row in enumerate(extra):
                    text = _clean(row.get("title")) + " " + _clean(row.get("snippet"))
                    observed, published = _funding_stage(text), _parse_date(row.get("date"))
                    if observed and published and published <= run.eval_date + timedelta(days=3) and first in (text + " " + str(row.get("url"))).lower():
                        rank = v12_secondary.stage_rank(observed)
                        rounds.append((rank, published, -index, observed, row))
                if not rounds:
                    if not v12_secondary.unconfirmed_allowed():
                        _trace("verify.reject", {"company": cand["company_name"], "why": "stage_unconfirmed", "want": want_stage, "query": stage_query})
                        return None
                    _trace('stage.unproven', {'company': cand['company_name'], 'decision': 'keep', 'query': stage_query})
                    # Do not turn an absent observation into the requested fit.
                    stage_stated = ''
                else:
                    _, _, _, latest_stage, latest_row = max(rounds)
                    _trace("stage_probe", {"company": cand["company_name"], "source": "exa", "observed": latest_stage, "url": latest_row["url"]})
                    if not _stage_matches(latest_stage, want_stage):
                        _trace("verify.reject", {"company": cand["company_name"], "why": "stage_conflict_latest", "want": want_stage, "observed": latest_stage, "url": latest_row["url"]})
                        return None
                    stage_stated, cand["stage_url"] = want_stage, latest_row["url"]

    bucket = observed_bucket
    if not bucket and run.remaining() > 45:
        # try to OBSERVE headcount (never assume the ICP bucket): Exa company entity, else one profile-style search
        ent_res = await run.tool("exa_company_search", {"query": f"{cand['company_name']} {cand['domain']}", "numResults": 3})
        for r in (ent_res or {}).get("results") or []:
            if _host(r.get("url") or "") == cand["domain"]:
                wf = (r.get("entity") or {}).get("workforce") or {}
                bucket = _bucket_for(wf.get("total") if isinstance(wf, dict) else None)
                break
        if bucket and buckets and bucket not in buckets:
            _trace("verify.reject", {"company": cand["company_name"], "why": "bucket", "observed": bucket})
            return None
    if not bucket:
        _trace("verify.reject", {"company": cand["company_name"], "why": "no_bucket_observed"})
        return None  # headcount unobserved → do not submit (judge would zero + penalize a wrong bucket)
    if not attr_quote and website and run.remaining() > 40:
        home = cand.get('_home') or await _page_text(run, website)
        home_text = _clean((home or {}).get("text"))
        if len(home_text) > 100:
            v2 = await run.ask(
                "Quote VERBATIM. Output JSON only.",
                f"Requirement: {icp.get('required_attribute') or icp.get('product_service')}\nCompany: {cand['company_name']}\nHomepage text:\n\"\"\"\n{home_text[:1400]}\n\"\"\"\n"
                "Return {\"quote\":\"verbatim homepage text (max 300 chars) showing the company meets the requirement, or empty\",\"explanation\":\"<=200 chars\"}",
                max_tokens=300,
            )
            q = _snippet_on_page(str((v2 or {}).get("quote") or ""), home_text) if isinstance(v2, dict) else ""
            if q:
                attr_quote, attr_url = q, website
    required_attribute = None
    if icp.get("required_attribute"):
        if not attr_quote:
            _trace("verify.reject", {"company": cand["company_name"], "why": "no_attribute_quote"})
            return None
        required_attribute = {"text": str(icp["required_attribute"])[:2000], "passed": True, "evidence_url": attr_url, "evidence_quote": attr_quote[:2000], "explanation": _clean(verdict.get("fit_summary"))[:2000] or "The quoted text shows the company sells the required product."}

    result = {
        "company_name": cand["company_name"] if v9.enabled("V92_IDENTITY") else _display_name(cand["company_name"], str(company.get("company_name") or "")),
        "company_website": website or cand["evidence_url"],
        "company_linkedin": "",
        "industry": _clean(icp.get("industry")),
        "employee_count": bucket,
        "company_stage": want_stage if stage_required else (stage_stated or ""),
        "country": country,
        "state": (_clean(verdict.get("state")) or inferred_state)[:80],
        "fit_summary": (_clean(verdict.get("fit_summary")) or f"{cand['company_name']} matches the ICP.")[:500],
        "fit_evidence_urls": [u for u in (website, cand.get("stage_url"), cand["evidence_url"]) if u][:2],
        "intent_signals": [{
            "matched_icp_signal": 0,
            "description": (_clean(verdict.get("description")) or cand.get("event") or intent["signal"])[:350],
            "date": ev_date.isoformat(),
            "why_now": (_clean(verdict.get("why_now")) or "A fresh, verified event signals budget and change; reach out now.")[:600],
            "url": cand["evidence_url"],
            "snippet": snippet[:600],
        }],
        "required_attribute": required_attribute,
    }
    stage_page = {}
    if v9.enabled('V92_FIT_HINTS'):
        stage_url = cand.get('stage_url') or (cand['evidence_url'] if _funding_stage(page_text) else '')
        if stage_url and v9.domain(stage_url) == v9.domain(cand['domain']) and run.remaining() > 15 and run.used['deepline'] < run.budget['deepline']:
            stage_page = await _page_text(run, stage_url)
            if not _stage_matches(_funding_stage(stage_page.get('text', '')), stage_stated):
                stage_page = {}
        if not stage_page and stage_required and run.remaining() > 35 and run.used['deepline'] < run.budget['deepline']:
            found = await run.tool('exa_search', {'query': f"{cand['company_name']} {stage_stated} funding", 'includeDomains': [cand['domain']], 'numResults': 3, 'contents': {'text': {'maxCharacters': 2000}}})
            cand['_stage_links'] = [{'url': r.get('url', '')} for r in found.get('results', [])]
            for r in found.get('results', []):
                if v9.domain(r.get('url')) == v9.domain(cand['domain']) and _stage_matches(_funding_stage(r.get('text', '')), stage_stated):
                    stage_page = r
                    break
    if v12_secondary.unconfirmed_allowed() and not stage_stated:
        result['company_stage'] = ''
    # The observed-field summary must see the same blank stage as the output.
    # Clearing only after fit_fields would leave the requested ICP stage in prose.
    p4_fit.fit_fields(result, cand, stage_page, company, page_text)
    if v9.enabled('V13_SOURCE_WEIGHT'):
        result = await v13_slots.prefer_primary(run, cand, result, _trace)
    try:
        second = None
        if not v9.enabled('V13_FILL_FIRST'):
            second = await p3_signal.second_signal(run, cand, result['intent_signals'][0]) if v9.enabled('V92_SECOND_SIGNAL') else await _second_verified_signal(run, cand, cand['evidence_url'])
    except v12_llm.LLMTruncated:
        raise  # An incomplete model response is not negative event evidence.
    except Exception as exc:
        _trace('second_signal.skip', {'company': cand['company_name'], 'why': str(exc)[:200]})
        second = None  # optional corroboration must not discard a valid primary
    if second:
        result["intent_signals"].append(second)
    if not website:
        return None
    try:
        validate_companies([result], 1)
    except Exception:
        return None
    run.ranking[v9.domain(website)] = v92_rank.size_anchor(cand, company if profile_count else {})
    if v9.enabled('V13_FILL_FIRST'):
        run.completed_context[v9.domain(website)] = cand
    return result


def _usage(run):
    return {
        "requests": run.llm_requests, "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
        "cost": run.cost, "tool_calls": run.tool_calls, "provider_calls": run.tool_calls,
        "seconds": round(_now() - run.started, 1), "model": run.model,
        "llm_truncated": getattr(run, 'llm_truncated', 0),
        "llm_incomplete_responses": getattr(run, 'llm_incomplete_responses', 0),
        "llm_truncation_recovered": getattr(run, 'llm_truncation_recovered', 0),
    }


async def _run(icp_raw: dict[str, Any], *, started=None, publish=None) -> list[dict[str, Any]]:
    if v12_llm.enabled('V15_FULL_EXTRACT'):
        from agent.v15_pipeline import run as text_first_run
        return await text_first_run(icp_raw, started=started, publish=publish)
    icp = normalize_icp(p5_normalize.discovery_input(icp_raw))
    if not icp.get("required_intents"):
        icp["required_intents"] = [{"signal": str(icp.get("intent_signal") or icp.get("prompt") or "recent activity"), "category": str(icp.get("intent_category") or ""), "max_age_days": int(icp.get("intent_max_age_days") or 365)}]
    max_companies = max(1, min(int(os.environ.get("LAB_ARENA_COMPANY_LIMIT") or os.environ.get("BAKEOFF_MAX_COMPANIES") or icp.get("max_companies") or 3), 3))
    if v9.enabled('V13_FILL_GOAL'):
        max_companies = v13_slots.goal(icp_raw)
    run = _Run(icp) if started is None else _Run(icp, started=started)
    companies: list[dict[str, Any]] = []
    finished = {}

    def remember(index, result):
        nonlocal companies
        if not result:
            return
        site = result.get('company_website', '')
        home = run.identity_pages.get(site, {})
        observed_name = p6_identity.brand(home, result.get('company_name', ''), v9.domain(site)) if home else ''
        observed_identity = {'name': observed_name, 'website': home.get('url'), 'source': 'company_homepage'} if observed_name else None
        if not judge_mirror.submitted_conflicts(result, icp_raw):
            _funnel_mark(run, 'fit_gate_passed', result)
        result = judge_mirror.filter_company(result, icp_raw, pages=run.page_cache,
                                            identity=observed_identity, evaluation_date=run.eval_date, log=_trace,
                                            admission=v9.enabled('V13_MIRROR_GATE'))
        if not result:
            return
        # Validate before mutating the admitted pool. A bad optional update
        # must not poison the already-valid companies or later checkpoints.
        result = validate_companies([result], 1)[0]
        finished[index] = result
        verified, domains = [], set()
        # Completion order must not change duplicate selection or ranking ties.
        for _, row in sorted(finished.items()):
            domain = v9.domain(row['company_website'])
            if domain not in domains:
                verified.append(row)
                domains.add(domain)
        companies = validate_companies(v92_rank.order(verified, run.ranking)[:max_companies], max_companies)
        if publish is not None:
            publish(companies, _usage(run))

    async def collect():
        if getattr(run, '_setup_error', None) is not None:
            raise run._setup_error
        want_stage = _clean(icp.get("company_stage"))
        stage_cands = []
        if want_stage and want_stage.lower() != "any":
            try:
                stage_cands = await _stage_first_candidates(run)
            except v12_llm.LLMTruncated:
                _trace('discovery.inconclusive', {'why': 'llm_truncated', 'phase': 'stage_first'})
        try:
            queries = p5_normalize.discovery_queries(icp) if v9.enabled('V92_M1_NORMALIZATION') else await _plan_queries(run)
        except v12_llm.LLMTruncated:
            _trace('discovery.inconclusive', {'why': 'llm_truncated', 'phase': 'planning'})
            queries = p5_normalize.discovery_queries(icp)
        _trace("queries", {"icp": icp.get("icp_id"), "queries": queries})
        rows = await _discover(run, queries)
        _trace("discover", {"icp": icp.get("icp_id"), "n": len(rows), "rows": rows[:25]})
        try:
            candidates = await _extract_candidates(run, rows) if rows else []
        except v12_llm.LLMTruncated:
            _trace('discovery.inconclusive', {'why': 'llm_truncated', 'phase': 'extraction'})
            candidates = []
        _trace("candidates", {"icp": icp.get("icp_id"), "candidates": candidates})
        if stage_cands:
            known = {c["domain"] for c in candidates if c["domain"]}
            candidates = candidates[:5] + [c for c in stage_cands if c["domain"] not in known]
        if len(candidates) < 4 and run.remaining() > 90:
            extra = await _company_search_candidates(run)
            known = {c["domain"] for c in candidates if c["domain"]}
            candidates += [c for c in extra if c["domain"] not in known]
        if len(candidates) < 3 and run.remaining() > 75:
            extra = await _hunter_candidates(run)
            known = {c["domain"] for c in candidates if c["domain"]}
            candidates += [c for c in extra if c["domain"] not in known]
        _trace("candidates.final", {"icp": icp.get("icp_id"), "n": len(candidates), "names": [c["company_name"] for c in candidates]})
        for candidate in candidates:
            _funnel_mark(run, 'discovered', candidate)
        sem = asyncio.Semaphore(1 if v12_llm.enabled('V12_COMPLETION_BUDGET') else 4)
        hiring = str(icp["required_intents"][0].get("category") or "").upper() == "HIRING"
        async def guarded(index, c):
            async with sem:
                if run.remaining() < 30:
                    return None
                if v9.enabled('V13_FILL_FIRST') and len(companies) >= max_companies:
                    return None  # Do not start a fresh candidate after slots fill.
                name = c.get('company_name')
                lease = None
                try:
                    if v12_llm.enabled('V12_COMPLETION_BUDGET'):
                        lease = v12_secondary.CompletionLease(run, name, _trace)
                    c = await _identity(run, c)
                    if not c:
                        return None
                    path = urlsplit(str(c.get("evidence_url") or "")).path.strip("/")
                    dated = _parse_date(c.get("event_date")) or _parse_date(c.get("result_date"))
                    if hiring and path and not dated:
                        c = {**c, "fit_url": c["evidence_url"]}
                    if not c.get("evidence_url") or (hiring and (not path or not dated)):
                        rows_ = await _find_event_evidence(run, c)
                        if not rows_:
                            _trace("verify.reject", {"company": c["company_name"], "why": "no_event_evidence"})
                            return None
                        c = await _pick_evidence(run, c, rows_)
                        if not c:
                            return None
                    result = await asyncio.wait_for(_verify(run, c), timeout=max(10.0, run.remaining() - 10))
                    remember(index, result)
                    return result
                except v12_llm.LLMTruncated as exc:
                    _trace('verify.reject', {'company': name, 'why': 'llm_truncated', 'detail': str(exc)})
                    return None
                except Exception as exc:
                    _trace("verify.error", {"company": name, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                    return None
                finally:
                    if lease:
                        lease.close()
        await asyncio.gather(*(guarded(index, c) for index, c in enumerate(candidates)))
        if v9.enabled('V13_FILL_FIRST'):
            _trace('fill_first.primary_done', {'filled': len(companies), 'goal': max_companies})
            # Primary admission finishes for the whole candidate pool first.
            # No secondary request is made while even one requested slot is empty.
            for row in list(companies):
                if len(companies) < max_companies or run.remaining() < 25:
                    break
                key = v9.domain(row['company_website'])
                cand = run.completed_context.get(key)
                if not cand:
                    continue
                index = next(i for i, r in finished.items() if v9.domain(r['company_website']) == key)
                try:
                    _trace('fill_first.secondary_start', {'company': row['company_name'], 'filled': len(companies), 'goal': max_companies})
                    second = await p3_signal.second_signal(run, cand, row['intent_signals'][0])
                    if second and all(v13_slots.independent(s['url'], second['url']) for s in row['intent_signals']):
                        remember(index, {**row, 'intent_signals': row['intent_signals'] + [second]})
                except v12_llm.LLMTruncated as exc:
                    _trace('verify.reject', {'company': row['company_name'], 'why': 'llm_truncated', 'detail': str(exc)})
                    finished.pop(index, None)
                    # Keep the v12 inconclusive policy and preserve the other
                    # companies. The next iteration stops if a slot is empty.
                    companies[:] = [r for r in companies if v9.domain(r['company_website']) != key]
                    if publish is not None:
                        publish(companies, _usage(run))
                except Exception as exc:
                    _trace('second_signal.skip', {'company': row['company_name'], 'why': str(exc)[:200]})

    loop = asyncio.get_running_loop()
    root_task = asyncio.current_task()
    signal_guard = publish is not None and not IN_PROCESS.get()
    if signal_guard:
        loop.add_signal_handler(signal.SIGTERM, root_task.cancel)
    try:
        async with asyncio.timeout(max(0.0, run.started + FINALIZE_SECONDS - _now())):
            await collect()
    except (TimeoutError, BudgetExhausted, asyncio.CancelledError) as exc:
        _trace('run.budget_stop', {'reason': type(exc).__name__, 'n': len(companies)})
    finally:
        # No tool/LLM call is needed to preserve or deliver the last good list.
        _trace("submit", {"icp": icp.get("icp_id"), "n": len(companies), "names": [c["company_name"] for c in companies], "tool_calls": run.tool_calls, "used": run.used, "llm": run.llm_requests, "sec": round(_now() - run.started, 1)})
        _trace('llm.funnel', {k: v for k, v in _usage(run).items() if k.startswith('llm_')})
        funnel = getattr(run, 'funnel', {})
        fit = funnel.get('fit_gate_passed', set())
        primary = funnel.get('primary_verified', set())
        _trace('sourcing.funnel', {'discovered': len(funnel.get('discovered', set())),
               'fit_gate_passed': len(fit), 'index0_verified': len(fit & primary),
               'slot_filled': len(companies),
               'second_domain_added': sum(any(v13_slots.independent(c['intent_signals'][0]['url'], s['url'])
                                            for s in c['intent_signals'][1:]) for c in companies),
               'raw_primary_verified_before_fit': len(primary)})
        try:
            await asyncio.wait_for(run.close(), timeout=10.0)
        except (Exception, asyncio.CancelledError) as exc:
            _trace('run.close_error', {'reason': type(exc).__name__})
        if signal_guard:
            loop.remove_signal_handler(signal.SIGTERM)
        LAST_USAGE.clear()
        LAST_USAGE.update(_usage(run))
    return companies


def _run_worker(icp, started, publish):
    from agent.v36_output import output_companies
    output_companies([], icp, _trace)
    def wire_publish(companies, usage):
        publish(output_companies(companies, icp, _trace), usage)
    companies = asyncio.run(_run(icp, started=started, publish=wire_publish))
    return output_companies(companies, icp, _trace), get_last_usage()


def run_icp(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the bounded requested number of verified companies, best first."""
    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        companies, usage = run_isolated(_run_worker, icp)
        LAST_USAGE.clear()
        LAST_USAGE.update(usage)
        return companies
    raise RuntimeError("run_icp must be called outside an active asyncio event loop")


def get_last_usage() -> dict[str, Any]:
    return dict(LAST_USAGE)


__all__ = ["run_icp", "get_last_usage", "LAST_USAGE"]
