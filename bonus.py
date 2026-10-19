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

MODEL = "google/gemini-3.5-flash-lite"
MAX_NEWS_ROWS = 4
MAX_PAID_SEARCHES = 5           # v108: paid bonus searches per ICP (one per row), $0.010 each
PAID_RESULT_ROWS = 3            # pages read from one paid search
WINDOW_CHARS = 4000
_ROLE_SPLIT_RE = re.compile(r"\s*(?:,|\bor\b|\band\b|/)\s*", re.I)


# v98: every 10-07 ICP carried a bonus criterion and six of the ten were hiring ones worded without the verb "hiring"
# ("Posted current openings for underwriting, risk, compliance, or operations roles on its careers page.", "... openings
# tied to growth, merchandising, ecommerce operations, or marketplace expansion on its careers page.").  The old form
# found no role group there, so any posting title was accepted.
_ROLE_FORMS = (
    re.compile(r"\bhiring\s+(?:for\s+)?(.+?)\s+(?:roles?|positions?|talent|staff)\b", re.I),
    re.compile(r"\bopenings?\s+(?:for|tied\s+to|related\s+to|in|across)\s+(.+?)"
               r"(?:\s+(?:roles?|positions?|teams?|functions?|talent|staff)\b|\s+on\s+(?:its|the(?:ir)?)\b|[.;]|$)", re.I),
    re.compile(r"\b(?:roles?|positions?|jobs?)\s+(?:in|for|across|tied\s+to|related\s+to)\s+(.+?)"
               r"(?:\s+on\s+(?:its|the(?:ir)?)\b|[.;]|$)", re.I),
)
# Short forms a posting title uses for a requested role group.
_ROLE_ALIASES = {"machine learning": ("ml", "mlops"), "applied ai": ("ai", "genai", "llm"),
                 "artificial intelligence": ("ai",), "customer success": ("csm",), "ecommerce": ("e commerce",),
                 "go to market": ("gtm",), "research": ("r&d", "scientist"), "site reliability": ("sre",)}


def role_groups(signal_text: str) -> list[str]:
    """'hiring for implementation, customer success, or platform roles' -> ['implementation', 'customer success',
    'platform']; 'Posted current openings for data engineering or integrations roles on its careers page' ->
    ['data engineering', 'integrations']; [] when the criterion names no role group."""

    text = str(signal_text or "")
    for form in _ROLE_FORMS:
        match = form.search(text)
        if match:
            groups = [p.strip().casefold() for p in _ROLE_SPLIT_RE.split(match.group(1)) if len(p.strip()) >= 3]
            if groups:
                return groups
    return []


def _title_has(word: str, low: str, words: list[str]) -> bool:
    """A short word must be a word of the title ('data' is not in 'Database Engineering', 'risk' not in 'asterisk');
    a longer one may differ in its ending ('engineering' ~ 'Engineer') or be written apart ('E-Commerce')."""

    if len(word) <= 4:
        return word in words or word + "s" in words
    stem = word[:5]
    return any(w.startswith(stem) for w in words) or stem in low.replace(" ", "")


def title_matches(title: str, groups: list[str]) -> bool:
    if not groups:
        return True
    low = str(title or "").casefold().replace("-", " ")
    words = re.findall(r"[a-z0-9&+#]+", low)
    for group in groups:
        if all(_title_has(word, low, words) for word in group.replace("-", " ").split()):
            return True
        if any(alias in words or (" " in alias and alias in low) for alias in _ROLE_ALIASES.get(group, ())):
            return True
    return False


def confirm(name: str, signal_text: str, sentence: str) -> bool:
    prompt = ("Answer yes only if the sentence itself reports that %s did what the criterion describes (its own event, "
              "completed, not a plan, not another company's event).\nCRITERION: %s\nSENTENCE: %s\n"
              "Return {\"answer\": \"yes\"|\"no\"}." % (name, signal_text[:400], sentence[:600]))
    parsed = llm.chat_json(prompt, model=MODEL, max_tokens=20, purpose="bonus")
    return isinstance(parsed, dict) and str(parsed.get("answer") or "").strip().lower() == "yes"


def _board_bound(tools: Any, hiring: Any, ats: str, slug: str, name: str, domain: str) -> bool:
    """Is this job board the company's own?  Yes when a page of the company's site links it, or when the tenant is the
    company's whole name.  A tenant that is only the domain's label can be another company (clear.co is Clearco; the
    Greenhouse tenant 'clear' is CLEAR, the airport identity company), and a posting of the wrong company fails."""

    tenant = re.sub(r"[^a-z0-9]+", "", str(slug or "").casefold())
    names = {re.sub(r"[^a-z0-9]+", "", str(v or "").casefold()) for v in (sm.company_name_key(name), name)}
    if tenant and tenant in names:
        return True
    for url, page in (getattr(tools, "__dict__", {}).get("_hiring_pages") or {}).items():
        if page is None or sm.registrable_host(str(url)) != sm.registrable_host(f"https://{domain}/"):
            continue
        linked = hiring.boards_from_links(getattr(page, "links", None) or [])
        if any(a == ats and str(b).casefold() == str(slug).casefold() for a, b in linked):
            return True
    return False


def _hiring_candidate(tools: Any, icp: Mapping[str, Any], index: int, name: str, domain: str) -> Optional[dict]:
    from . import hiring

    ats, slug, postings = hiring.board_postings(tools, {"company_name": name, "domain": domain})
    if postings and not _board_bound(tools, hiring, ats, slug, name, domain):
        return None
    wanted = criteria.signals(icp)[index]["text"]
    groups = role_groups(wanted)
    # v98: rank the postings against the hiring criterion itself (the ICP's own intent_signals hold only the primary)
    for posting in hiring.by_relevance(postings, {"intent_signals": [wanted]}):
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


_QUERY_DROP_RE = re.compile(r"\b(?:(?:in|within|over) the (?:last|past) \d+ (?:days?|weeks?|months?|years?)|"
                            r"per (?:a|an|the|its) [^.]*)", re.I)
_QUERY_STOP = frozenset("a an the or and of in on to its their for with by per has have had was were is are new own "
                        "any some that which this these those from into within during".split())


_DATELINE_RE = re.compile(r"^.{0,90}?(?:/\s?PR\s?Newswire\s?/|\((?:BUSINESS WIRE|GLOBE NEWSWIRE|ACCESSWIRE|ACCESS Newswire|"
                          r"Newsfile Corp\.?[^)]{0,40}|EZ Newswire|PRWEB)\))\s*[-–—]{1,2}\s*", re.I)


def after_dateline(sentence: str) -> str:
    """A press-wire lede without its dateline: 'COLUMBUS, Ohio, Jan. 14, 2026 /PRNewswire/ -- Huntington ... today
    announced ...' -> 'Huntington ... today announced ...'.  A sentence split at 'Jan.' keeps the fragment
    '14, 2026 /PRNewswire/ -- ' in front, which is no part of the event."""

    flat = " ".join(str(sentence or "").split())
    rest = _DATELINE_RE.sub("", flat, count=1)
    return rest if rest != flat and len(rest.split()) >= 5 else flat


def paid_query(name: str, signal_text: str) -> str:
    """The company's name in quotes and the criterion's own content words, without its time and source clauses:
    'Received a regulatory clearance, approval, or certification in the last 12 months, per a filing or regulator
    update.' -> '"Zions Bancorporation" Received regulatory clearance approval certification'."""

    text = _QUERY_DROP_RE.sub(" ", " ".join(str(signal_text or "").split()))
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9+\-]{2,}", text) if w.casefold() not in _QUERY_STOP]
    return (f'"{" ".join(str(name or "").split())}" ' + " ".join(words[:10])).strip()[:200]


def _news_candidate(tools: Any, icp: Mapping[str, Any], index: int, name: str, domain: str,
                    reuse: list[str], paid: Any = None) -> Optional[dict]:
    from .roster import CATEGORY_WORDS, company_news
    from .scout import _CHROME_RE, _fetch_text, event_sentence, find_date
    from .sourcetype import body_dateline

    category = criteria.category(icp, index)
    pattern = re.compile(CATEGORY_WORDS.get(category, r"announc"), re.I)
    signal_text = criteria.signals(icp)[index]["text"]
    rows: list[tuple[str, Optional[str]]] = [(u, None) for u in reuse]
    for story in company_news(tools, domain, name)[:12]:
        blob = f"{story.get('title') or ''} {story.get('excerpt') or ''}"
        if story.get("url") and pattern.search(blob) and criteria.in_window(icp, index, story.get("published_date")):
            rows.append((str(story["url"]), str(story.get("published_date") or "")[:10] or None))
    def scan(pages: list[tuple[str, Optional[str]]], most: int) -> Optional[dict]:
        tried = 0
        for url, listed in pages:
            if tried >= most or criteria.url_admissible(icp, index, url, f"https://{domain}/"):
                continue
            tried += 1
            text = _fetch_text(tools, url)
            if not text:
                continue
            sentence = ""
            for candidate in re.split(r"(?<=[.!?])\s+", text[:WINDOW_CHARS]):
                if pattern.search(candidate) and candidate.strip():
                    cut = after_dateline(event_sentence(candidate, name))
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

    found = scan(rows, MAX_NEWS_ROWS)
    if found is not None or paid is None or not paid():
        return found
    # v108: the free feed lists about twelve stories; the bonus event is searched for by name, once, in its window
    data = tools.search_web(paid_query(name, signal_text), recency_days=criteria.window(icp, index), limit=10,
                            company_website=f"https://{domain}/")
    seen = {url for url, _ in rows}
    key = sm.company_name_key(name)
    extra: list[tuple[str, Optional[str]]] = []
    for story in (data or {}).get("results") or []:
        url = str(story.get("url") or "")
        blob = f"{story.get('title') or ''} {story.get('excerpt') or story.get('text') or ''}"
        when = str(story.get("published_date") or "")[:10]
        if not url.startswith("http") or url in seen or not pattern.search(blob):
            continue
        if when and not criteria.in_window(icp, index, when):
            continue
        if key and key not in sm.company_name_key(blob):      # the story must name the company
            continue
        seen.add(url)
        extra.append((url, when or None))
    return scan(extra, PAID_RESULT_ROWS)


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
    paid_left = [MAX_PAID_SEARCHES]

    def paid() -> bool:
        if paid_left[0] <= 0 or clock() >= deadline:
            return False
        paid_left[0] -= 1
        notes["paid_searches"] = notes.get("paid_searches", 0) + 1
        return True

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
                    found = _news_candidate(tools, icp, index, name, domain, primary_urls, paid)
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


__all__ = ["attach", "role_groups", "title_matches", "confirm", "paid_query", "after_dateline"]
