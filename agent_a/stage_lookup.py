
from __future__ import annotations

import re
import time
from typing import Any

try:
    from . import precision as px, scout as sc, scorer_mirror as sm
    from .arena_tools import DYNAMIC_PRICE_ESTIMATE_USD, STRATEGY, fetchable
except ImportError:
    import importlib as _importlib
    import os as _os

    _pkg = _os.path.basename(_os.path.dirname(_os.path.abspath(__file__)))
    px = _importlib.import_module(f"{_pkg}.precision")
    sc = _importlib.import_module(f"{_pkg}.scout")
    sm = _importlib.import_module(f"{_pkg}.scorer_mirror")
    _tools = _importlib.import_module(f"{_pkg}.arena_tools")
    STRATEGY, fetchable = _tools.STRATEGY, _tools.fetchable
    DYNAMIC_PRICE_ESTIMATE_USD = _tools.DYNAMIC_PRICE_ESTIMATE_USD


def _knob(key: str, default: int, high: int) -> int:
    value = STRATEGY.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = default
    return max(0, min(high, int(value)))


LOOKUPS_PER_ICP = _knob("stage_lookup", 0, 6)
LOOKUP_COST_USD = float(DYNAMIC_PRICE_ESTIMATE_USD)
FETCH_COST_USD = 2 * float(DYNAMIC_PRICE_ESTIMATE_USD)
DEADLINE_ROOM_S = 75.0
CALL_RESERVE = 15
MAX_FEED_FETCHES = 1
_FUNDING_WORDS = re.compile(r"\b(raised|raises|series|funding|seed|round)\b", re.I)


def allowed(tools: Any, cost: float = LOOKUP_COST_USD) -> bool:

    try:
        if getattr(tools, "_unknown_start", False):
            return False
        deadline = getattr(tools, "deadline", None)
        if deadline is not None and time.monotonic() + DEADLINE_ROOM_S >= float(deadline):
            return False
        remaining = getattr(tools, "remaining", None)
        if callable(remaining) and int(remaining()) < CALL_RESERVE:
            return False
        return bool(tools.paid_allowed(cost))
    except Exception:
        return False


def sentence(text: str, name: str, stage: str, url: str, website: str = "") -> str:

    want = sm.normalize_stage(stage)
    if want not in sc._VENTURE:
        return ""
    try:
        if not px.page_binds(name, text, url, website):
            return ""
        body = str(text or "")
        for span in px.stage_quotes(body, name, stage, url, website, limit=3, subject=True):
            span = str(span)
            if lead_makes_company_the_object(body, span):
                continue
            if strict_subject(span, name, want) and not other_subject(span, name, want):
                return span
    except Exception:
        return ""
    return ""


_OTHER_SUBJECT_RE = re.compile(
    r"((?:[A-Z][\w&.'-]*)(?:\s+(?:[A-Z][\w&.'-]*|of|&))+)\s+(?:has\s+|have\s+|today\s+|just\s+)?"
    r"(?:raised|raises|closed|closes|secured|secures|landed|lands|completed|completes|announced|announces)\b")


_CORP_RE = r"(?:,?\s*(?:inc|ltd|llc|corp|co|gmbh|ag|sa|plc|limited|incorporated)\.?)?"
_APPOSITIVE_RE = r"(?:,\s*(?:a|an|the)\s+[^,]{1,80},)?"
_VERB_PHRASE_RE = (
    r"\s*(?:(?:has|have|had|today|also|recently|just|officially|announced(?:\s+today)?\s+(?:that\s+)?it\s+has|"
    r"announced(?:\s+today)?\s+(?:that\s+)?it|said\s+(?:that\s+)?it\s+has|said\s+it|which\s+has)\s+)*"
    r"(?:raised|raises|closed|closes|secured|secures|landed|lands|completed|completes|announced\s+the\s+close\s+of|"
    r"announced\s+the\s+closing\s+of|bagged|nabbed|received)\s+"
    r"(?:(?:a|an|its|the|an?\s+new|an?\s+additional|an?\s+fresh|an?\s+oversubscribed)\s+)?"
    r"(?:(?:us\$|\$|€|£)?\s*[\d.,]+\s*(?:million|mn|m|billion|bn|k)?\s*)?"
    r"(?:(?:in|of|new|additional|fresh|oversubscribed|strategic|growth|venture|equity|seed|its|through|via|from|funding|"
    r"financing|round|capital|investment|the)\s+)*$")
_STRICT_TAIL_RE = re.compile("^" + _CORP_RE + _APPOSITIVE_RE + _VERB_PHRASE_RE, re.I)


def lead_makes_company_the_object(body: str, span: str, words: int = 8) -> bool:

    at = body.find(span)
    if at < 0:
        return False
    before = body[:at]
    cut = max(before.rfind(ch) for ch in (".", "!", "?", "\n"))
    lead = before[cut + 1:].strip() if cut >= 0 else before.strip()
    tail = " ".join(lead.split()[-words:])
    if not tail:
        return False
    return bool(_LEAD_OBJECT_RE.search(tail)) or tail.endswith("'s") or tail.endswith("\u2019s")


def strict_subject(span: str, name: str, want: str) -> bool:

    labels = [(m, sc.round_label(m.group(1))) for m in sc._ROUND_RE.finditer(span)]
    mine = [m for m, label in labels if sm.stage_matches(label, want)]
    if not mine or any(not sm.stage_matches(label, want) for _, label in labels):
        return False
    label_at = mine[0].start()
    prefix = span[:label_at]
    low = prefix.lower()
    variants = []
    for cand in (str(name or ""), re.sub(r"[,.]?\s+(?:inc|ltd|llc|corp|co|gmbh|ag|plc|limited)\.?$", "", str(name or ""), flags=re.I)):
        cand = " ".join(cand.split()).lower()
        if len(cand) >= 3 and cand not in variants:
            variants.append(cand)
    end = -1
    for cand in variants:
        at = low.rfind(cand)
        if at >= 0:
            end = max(end, at + len(cand))
    if end < 0:
        return False
    segment = prefix[end:]
    if not _STRICT_TAIL_RE.match(segment):
        return False
    body = re.sub("^" + _CORP_RE, "", segment, count=1, flags=re.I)
    body = re.sub("^" + _APPOSITIVE_RE, "", body, count=1, flags=re.I)
    if re.search(r"\b[A-Z][a-z]", body):
        return False
    lead = prefix[:end - max(len(v) for v in variants if low.rfind(v) >= 0 and low.rfind(v) + len(v) == end)]
    lead = _DATELINE_RE.sub("", lead.strip(), count=1).strip()
    if _LEAD_OBJECT_RE.search(lead) or not _LEAD_IN_RE.fullmatch(lead):
        return False
    tail = span[mine[0].end():]
    if _AFTER_LABEL_RE.match(tail):
        return False
    if want != "series c+":
        try:
            oldest = sm.evaluation_date().year - max(1, int(sc.FRESH_ROUND_DAYS) // 365 + 1)
            if any(int(y) <= oldest for y in re.findall(r"\b(19\d\d|20\d\d)\b", span)):
                return False
        except Exception:
            return False
    return True


_DATELINE_RE = re.compile(
    r"^(?:[A-Z][A-Za-z.&' -]{0,40},?\s*(?=(?:[A-Z][a-z]{2,8}\.?|\d{1,2})\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}|--|\u2013|\u2014|[/(]\s*(?:PR|GLOBE|Globe|Business|BUSINESS|ACCESS|News)))?(?:(?:[A-Z][a-z]{2,8}\.?|\d{1,2})\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})?\s*"
    r"(?:--|-|\u2013|\u2014)?\s*[/(]?\s*(?:PRNewswire|PR Newswire|GLOBE NEWSWIRE|GlobeNewswire|Business Wire|BUSINESS WIRE|"
    r"ACCESSWIRE|Newswire)?\s*[/)]?\s*(?:--|-|\u2013|\u2014)?\s*")
_LEAD_IN_RE = re.compile(
    r"(?:exclusive:\s*)?(?:on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s*)?"
    r"(?:(?:today|yesterday|this week|this morning|earlier today),?\s*)?"
    r"(?:(?-i:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})-based\s*)?(?:(?-i:[A-Za-z][a-z]+(?:\s+[a-z]+)?)\s+(?:startup|start-up|company|maker|firm)\s*)?", re.I)
_LEAD_OBJECT_RE = re.compile(r"\b(?:of|by|for|from|who|which|that|behind|backed|left|at|with|in|to)\b", re.I)
_AFTER_LABEL_RE = re.compile(
    r"\s*(?:funding\s+|financing\s+|round\s+|investment\s+|extension\s+)?"
    r"(?:fund\b|investment\s+in\b|in\s+(?:a|an|the|its)\b|of\s+(?:its|a|an|the)\b|for\s+(?:its|their|it|a|an|the)\b|"
    r"(?:of|for|in|into)\s+(?!(?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|Q[1-4])\b)(?-i:[A-Z])|on\s+behalf\s+of\b|"
    r"for\s+(?:subsidiary|portfolio|affiliate)\b|"
    r"round\s+(?:of|for)\s+(?:its|a|an|the)\b|led\s+by\s+[^.]*\bfor\s+(?:its|a)\b)", re.I)


def other_subject(span: str, name: str, want: str) -> bool:

    label_at = None
    for m in sc._ROUND_RE.finditer(span):
        if sm.stage_matches(sc.round_label(m.group(1)), want):
            label_at = m.start()
            break
    if label_at is None:
        return False
    prefix = span[:label_at]
    last = None
    for m in _OTHER_SUBJECT_RE.finditer(prefix):
        last = m
    return bool(last) and not sc.names_company(name, last.group(1))


def lookup(tools: Any, name: str, stage: str, website: str) -> tuple[str, str]:

    try:
        if LOOKUPS_PER_ICP <= 0 or sm.normalize_stage(stage) not in sc._VENTURE:
            return "", ""
        if getattr(tools, "_stage_lookups", 0) >= LOOKUPS_PER_ICP or not allowed(tools, 0.0):
            return "", ""
        tools._stage_lookups = getattr(tools, "_stage_lookups", 0) + 1
        from .roster import company_news
        from .sourcetype import NEWS_HOSTS, WIRE_HOSTS
        domain = sm.registrable_host(website)
        want = sm.normalize_stage(stage)

        def usable(rows: Any) -> list[dict[str, Any]]:
            out = []
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                u = str(r.get("url") or "")
                blob = f"{r.get('title') or ''} {r.get('excerpt') or ''} {u}"
                if sc._stage_host_tier(u, domain) >= 3 or not fetchable(u) or not _FUNDING_WORDS.search(blob):
                    continue
                if str(r.get("match") or "primary") not in ("primary", "None", ""):
                    continue
                age = sc.event_age_days(str(r.get("published_date") or "")[:10] or None)
                if want != "series c+" and age is not None and age > sc.FRESH_ROUND_DAYS:
                    continue
                out.append(r)
            out.sort(key=lambda r: sc._stage_host_tier(str(r.get("url") or ""), domain))
            return out

        rows = usable(company_news(tools, domain)) if domain else []
        searched = False
        if not rows and allowed(tools, 2 * LOOKUP_COST_USD):
            searched = True
            hosts = [h for h in WIRE_HOSTS + NEWS_HOSTS if sc._stage_host_tier("https://" + h, "") < 3]
            data = tools.search_web(f"{name} raises OR Series OR funding", limit=4, include_domains=hosts[:20],
                                    category="news")
            rows = usable((data or {}).get("results"))
        if not rows:
            return "", ""
        url = sm.public_http_url(str(rows[0].get("url") or ""))
        page = tools.pages.get(url)
        if page is None or not page.ok:
            if not allowed(tools, FETCH_COST_USD):
                return "", ""
            page = tools.fetch_page(url, allow_firecrawl=False)
            if page is not None and page.ok:
                tools.pages[url] = page
        if page is None or not page.ok:
            return "", ""
        if searched and domain:
            label = domain.split(".")[0]
            text_low = str(page.text or "").lower()
            name_key = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
            label_binds = label not in name_key and bool(re.search(r"\b" + re.escape(label) + r"\b", text_low))
            if domain not in text_low and not label_binds:
                return "", ""
        span = sentence(page.text, name, stage, url, website)
        return (url, span) if span else ("", "")
    except Exception as exc:
        if type(exc).__name__ == "BudgetExhausted":
            raise
        return "", ""


__all__ = ["LOOKUPS_PER_ICP", "LOOKUP_COST_USD", "FETCH_COST_USD", "allowed", "sentence", "strict_subject", "other_subject", "lookup"]
