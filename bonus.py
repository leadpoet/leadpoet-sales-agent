"""The second (bonus) intent criterion: one row, added only when a fetched page proves it.

A bonus row that the judge rejects can cost the company its paragraph, so a row is added only with strong proof: a
dated page inside the criterion's window that names the company in a sentence reporting the event (for hiring, a
native ATS posting on the company's own tenant whose title names one of the requested role groups), confirmed by one
inexpensive yes/no model call, then passed through the same signal verification as the primary.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from . import criteria
from . import llm
from . import scorer_mirror as sm
from .arena_tools import BudgetExhausted

MODEL = "google/gemini-2.5-flash"
MAX_NEWS_ROWS = 4
WINDOW_CHARS = 4000
_ROLE_SPLIT_RE = re.compile(r"\s*(?:,|\bor\b|\band\b|/)\s*", re.I)


def role_groups(signal_text: str) -> list[str]:
    """'hiring for implementation, customer success, or platform roles' -> ['implementation', 'customer success',
    'platform']; [] when the criterion names no role group."""

    match = re.search(r"\bhiring\s+(?:for\s+)?(.+?)\s+(?:roles?|positions?|talent|staff)\b", str(signal_text or ""), re.I)
    if not match:
        return []
    return [p.strip().casefold() for p in _ROLE_SPLIT_RE.split(match.group(1)) if len(p.strip()) >= 3]


def title_matches(title: str, groups: list[str]) -> bool:
    if not groups:
        return True
    low = str(title or "").casefold().replace("-", " ")
    return any(all(word[:5] in low for word in group.replace("-", " ").split()) for group in groups)


def confirm(name: str, signal_text: str, sentence: str) -> bool:
    prompt = ("Answer yes only if the sentence itself reports that %s did what the criterion describes (its own event, "
              "completed, not a plan, not another company's event).\nCRITERION: %s\nSENTENCE: %s\n"
              "Return {\"answer\": \"yes\"|\"no\"}." % (name, signal_text[:400], sentence[:600]))
    parsed = llm.chat_json(prompt, model=MODEL, max_tokens=20, purpose="bonus")
    return isinstance(parsed, dict) and str(parsed.get("answer") or "").strip().lower() == "yes"


def _hiring_candidate(tools: Any, icp: Mapping[str, Any], index: int, name: str, domain: str) -> Optional[dict]:
    from . import hiring

    ats, _slug, postings = hiring.board_postings(tools, {"company_name": name, "domain": domain})
    groups = role_groups(criteria.signals(icp)[index]["text"])
    for posting in hiring.by_relevance(postings, icp):
        if not posting.get("date") or not criteria.in_window(icp, index, posting["date"]):
            continue
        if hiring.posting_in_geo(posting.get("location"), icp) is False:
            continue
        if not title_matches(posting.get("title"), groups):
            continue
        if hiring.posting_tier(posting["url"], domain, name) > 1:
            continue
        where = f" ({posting['location']})" if posting.get("location") else ""
        return {"matched_icp_signal": index, "url": posting["url"], "date": posting["date"],
                "description": f"{name} is hiring a {posting['title']}{where}"[:350], "snippet": posting["title"],
                "_title": posting["title"], "_ats": ats}
    return None


def _news_candidate(tools: Any, icp: Mapping[str, Any], index: int, name: str, domain: str,
                    reuse: list[str]) -> Optional[dict]:
    from .roster import CATEGORY_WORDS, company_news
    from .scout import _CHROME_RE, _fetch_text, event_sentence, find_date
    from .sourcetype import body_dateline

    category = criteria.category(icp, index)
    pattern = re.compile(CATEGORY_WORDS.get(category, r"announc"), re.I)
    signal_text = criteria.signals(icp)[index]["text"]
    rows: list[tuple[str, Optional[str]]] = [(u, None) for u in reuse]
    for story in company_news(tools, domain)[:12]:
        blob = f"{story.get('title') or ''} {story.get('excerpt') or ''}"
        if story.get("url") and pattern.search(blob) and criteria.in_window(icp, index, story.get("published_date")):
            rows.append((str(story["url"]), str(story.get("published_date") or "")[:10] or None))
    tried = 0
    for url, listed in rows:
        if tried >= MAX_NEWS_ROWS or criteria.url_admissible(icp, index, url, f"https://{domain}/"):
            continue
        tried += 1
        text = _fetch_text(tools, url)
        if not text:
            continue
        sentence = ""
        for candidate in re.split(r"(?<=[.!?])\s+", text[:WINDOW_CHARS]):
            if pattern.search(candidate) and candidate.strip():
                cut = event_sentence(candidate, name)
                if cut and not _CHROME_RE.search(cut):
                    sentence = cut
                    break
        when = body_dateline(text) or listed or find_date(text, url)
        if not sentence or not when or not criteria.in_window(icp, index, when):
            continue
        if not confirm(name, signal_text, sentence):
            continue
        return {"matched_icp_signal": index, "url": url, "date": str(when)[:10], "description": sentence[:350],
                "snippet": sentence[:600]}
    return None


def _date_shown(tools: Any, verified: Mapping[str, Any]) -> bool:
    from .verify import date_admitted
    return date_admitted(verified["date"], tools.pages.get(verified["url"]), 4000)


def primary_in_window(tools: Any, row: Mapping[str, Any]) -> bool:
    """With a bonus row the reviewer admits about 4,000 characters per page: the primary's description must be there."""

    for signal in row.get("intent_signals") or []:
        if signal.get("matched_icp_signal") != 0:
            continue
        page = tools.pages.get(signal.get("url"))
        text = " ".join(str(getattr(page, "text", "") or "")[:WINDOW_CHARS + 500].split()).casefold()
        words = [w for w in re.findall(r"[a-z0-9]{4,}", str(signal.get("description") or "").casefold())][:12]
        if text and words and sum(1 for w in words if w in text) >= max(1, int(len(words) * 0.7)):
            return True
    return False


def attach(rows: list[dict[str, Any]], report: Any, icp: Mapping[str, Any], tools: Any, *, verify_signal: Any,
           deadline: float, clock: Any) -> dict[str, Any]:
    """Add at most one verified bonus row per bonus criterion to each row, in place; notes for the report."""

    notes: dict[str, Any] = {"added": [], "tried": 0}
    n = criteria.count(icp)
    if n < 2:
        return notes
    today = sm.evaluation_date()
    for row in rows:
        if clock() >= deadline:
            notes["stopped"] = "deadline"
            break
        name, website = str(row.get("company_name") or ""), str(row.get("company_website") or "")
        domain = sm.registrable_host(website)
        primary_urls = [s["url"] for s in row.get("intent_signals") or [] if s.get("matched_icp_signal") == 0]
        for index in range(1, n):
            if any(s.get("matched_icp_signal") == index for s in row.get("intent_signals") or []):
                continue
            notes["tried"] += 1
            try:
                if criteria.is_hiring(icp, index):
                    found = _hiring_candidate(tools, icp, index, name, domain)
                else:
                    found = _news_candidate(tools, icp, index, name, domain, primary_urls)
            except BudgetExhausted:
                notes["stopped"] = "budget"
                return notes
            except Exception as exc:  # noqa: BLE001 - no bonus row for this company
                notes.setdefault("errors", []).append(f"{name[:30]}: {type(exc).__name__}")
                continue
            if not found:
                continue
            seen = {f"{s.get('matched_icp_signal')}|{s.get('url')}" for s in row.get("intent_signals") or []}
            verified = verify_signal(found, company={"company_name": name, "company_website": website}, icp=icp,
                                     tools=tools, today=today, seen_domains=seen, report=report,
                                     signals_spec=criteria.signals(icp), buyer_cap=sm.icp_max_age_days(icp))
            if not verified:
                continue
            if not primary_in_window(tools, row):
                notes.setdefault("skipped", []).append(f"{name[:30]}: primary fact beyond the paragraph window")
                continue
            row["intent_signals"].append({"matched_icp_signal": index, "description": verified["description"],
                                          "date": verified["date"], "url": verified["url"]})
            key = sm.company_name_key(name)
            report.evidence.setdefault(key, []).append({
                "index": index, "description": verified["description"], "date": verified["date"],
                "url": verified["url"], "quote": verified.get("_quote") or verified["description"],
                "title": found.get("_title") or "", "signal_text": criteria.signals(icp)[index]["text"],
                "date_visible": not criteria.is_hiring(icp, index) and _date_shown(tools, verified)})
            row.setdefault("_flags", {})["bonus"] = index
            notes["added"].append(f"{name[:40]}: {criteria.category(icp, index)}")
    return notes


__all__ = ["attach", "role_groups", "title_matches", "confirm"]
