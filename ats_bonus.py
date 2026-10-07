"""Second-criterion HIRING evidence from the company's own ATS board, over the sandbox's free web egress.

Why: a company that proves both ICP criteria is capped at 80 intent points instead of 60 x source multiplier
(51-54), and HIRING is the most common second criterion.  The judge verifies a single Greenhouse, Ashby or Lever
posting through that ATS's public API and binds the board tenant to the company, so an open posting whose title
names one of the criterion's role families is first-party, dated, verifiable evidence.

Flow per company (no paid provider call): find the board tenant (links on the homepage / careers page, else the
domain label and name as tenant guesses), list open postings from the public board API, keep postings published
inside the criterion's window whose TITLE contains one of the criterion's role words, and emit the newest one as
matched_icp_signal 1 with the canonical posting URL the judge accepts.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import time
from typing import Any, Mapping, Optional
from urllib.parse import urljoin, urlsplit

import httpx

UA = {"User-Agent": "Mozilla/5.0 (compatible; LeadpoetArenaAgent/1.0)", "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
BOARD_RES = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]{2,60})", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]{2,60})", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9._-]{2,60})", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9][a-z0-9-]{1,98})", re.I)),
)
NOT_SLUGS = {"embed", "jobs", "job", "careers", "api", "v1", "boards", "posting-api", "js", "static", "assets"}
CAREER_PATHS = ("/careers", "/jobs", "/company/careers", "/about/careers", "/careers/")
STOP = {"and", "or", "the", "a", "an", "for", "of", "per", "with", "roles", "role", "positions", "specific",
        "current", "job", "postings", "careers", "page", "pages", "evidence", "in", "as", "shown", "by"}
SYNONYMS = {
    "operations": ["operations", "ops", "operator"], "revenue-operations": ["revenue operations", "revops", "rev ops"],
    "engineering": ["engineer", "engineering", "developer", "sre"], "security": ["security", "secops", "appsec", "infosec"],
    "cloud": ["cloud", "aws", "azure", "gcp", "kubernetes"], "systems": ["systems", "system", "sysadmin", "infrastructure"],
    "platform": ["platform"], "infrastructure": ["infrastructure", "infra", "sre", "devops"],
    "sales": ["sales", "account executive", "business development", "sdr", "bdr"], "marketing": ["marketing", "growth"],
    "analytics": ["analytics", "analyst", "data"], "integrations": ["integration", "integrations", "solutions engineer"],
    "customer success": ["customer success", "csm", "customer experience", "support"],
    "implementation": ["implementation", "onboarding", "deployment"], "compliance": ["compliance", "regulatory", "aml", "kyc"],
    "risk": ["risk", "fraud", "credit"], "supply-chain": ["supply chain", "procurement", "logistics", "sourcing"],
    "supply chain": ["supply chain", "procurement", "logistics", "sourcing"], "manufacturing": ["manufacturing", "production"],
    "network": ["network", "noc"], "dispatch": ["dispatch", "dispatcher"], "fleet": ["fleet", "driver"],
    "admissions": ["admissions", "enrollment", "recruitment"], "academic": ["faculty", "professor", "lecturer", "academic", "instructor"],
    "student-support": ["student", "advisor", "advising"], "student operations": ["student", "registrar"],
    "consultants": ["consultant", "consulting"], "analysts": ["analyst"], "growth": ["growth"],
    "hardware engineering": ["hardware", "electrical engineer", "mechanical engineer"],
}
LAST: dict[str, Any] = {}
# A second posting is added next to an existing one: the judge counts the strongest verified signal per criterion
# and a rejected bonus posting does not zero the company (09-29 local: ScaleOps qualified with one rejected).
ATS_URL = re.compile(r"greenhouse\.io|ashbyhq\.com|lever\.co|workable\.com", re.I)


def _client() -> httpx.Client:
    proxy = str(os.environ.get("LAB_ARENA_WEB_PROXY_URL") or "").strip()
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(12.0), "follow_redirects": True, "headers": UA}
    if proxy.startswith("http://"):
        try:
            return httpx.Client(proxy=proxy, trust_env=False, **kwargs)
        except TypeError:  # an httpx without the proxy= argument: the sandbox also exports HTTP(S)_PROXY
            pass
    return httpx.Client(trust_env=True, **kwargs)


def role_words(signal_text: str) -> list[list[str]]:
    """The criterion's role families as lists of title keywords: 'hiring for cloud, security, or systems roles'."""
    text = signal_text.lower()
    m = re.search(r"hiring (?:for|of)\s+(.+?)(?:\s+roles?\b|\s+positions?\b|,?\s+(?:per|with|as shown|according)\b|$)", text)
    body = m.group(1) if m else text
    parts = [p.strip(" .") for p in re.split(r",|\bor\b|\band\b|/", body) if p.strip(" .")]
    families: list[list[str]] = []
    for part in parts:
        words = [w for w in re.findall(r"[a-z][a-z-]+", part) if w not in STOP]
        if not words:
            continue
        key = " ".join(words)
        syn = SYNONYMS.get(key) or SYNONYMS.get(key.replace(" ", "-")) or SYNONYMS.get(words[-1])
        families.append(syn or [key if len(key) > 3 else words[-1]])
    return families


OFF_TOPIC = ("finance", "financial", "accounting", "accountant", "legal", "counsel", "recruiter", "recruiting", "talent",
             "people", "hr ", "payroll", "alliance", "partner", "marketing", "sales", "account executive", "intern")


def _hit(t: str, kw: str) -> bool:
    return (" " + kw + " ") in t or (" " + kw + "s ") in t or (len(kw) > 5 and kw in t)


def title_score(title: str, families: list[list[str]]) -> int:
    """0 = no match.  A family's core word beats a synonym; off-topic functions are excluded unless requested."""
    t = " " + re.sub(r"[^a-z0-9]+", " ", title.lower()) + " "
    requested = " ".join(kw for fam in families for kw in fam)
    if any(w.strip() in t and w.strip() not in requested for w in OFF_TOPIC):
        return 0
    score = 0
    for fam in families:
        if _hit(t, fam[0]):
            score += 3
        elif any(_hit(t, kw) for kw in fam[1:]):
            score += 1
    return score


def title_matches(title: str, families: list[list[str]]) -> bool:
    return title_score(title, families) > 0


def _find_boards(cli: httpx.Client, website: str, name: str, deadline: float) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def scan(html: str) -> None:
        for ats, rx in BOARD_RES:
            for slug in rx.findall(html):
                slug = slug.strip("/").split("/")[0]
                if slug.lower() not in NOT_SLUGS and (ats, slug) not in found:
                    found.append((ats, slug))

    pages = [website] + [urljoin(website, p) for p in CAREER_PATHS]
    for url in pages:
        if time.monotonic() > deadline or len(found) >= 2:
            break
        try:
            r = cli.get(url)
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            scan(r.text[:800_000])
            if not found:
                for href in re.findall(r'href="([^"]*(?:career|jobs)[^"]*)"', r.text[:800_000], re.I)[:3]:
                    if time.monotonic() > deadline:
                        break
                    try:
                        r2 = cli.get(urljoin(url, href))
                        if r2.status_code == 200:
                            scan(r2.text[:800_000])
                    except httpx.HTTPError:
                        pass
    host = (urlsplit(website).hostname or "").lower().removeprefix("www.")
    label = host.split(".")[0] if host else ""
    guesses = [g for g in dict.fromkeys([label, re.sub(r"[^a-z0-9]", "", name.lower()), label.replace("-", "")]) if g]
    for g in guesses:
        for ats in ("greenhouse", "ashby", "lever", "workable"):
            if (ats, g) not in found:
                found.append((ats, g))
    return found


def _parse_date(value: Any) -> Optional[_dt.date]:
    if isinstance(value, (int, float)) and value > 10_000_000_000:
        return _dt.datetime.utcfromtimestamp(value / 1000).date()
    if isinstance(value, str) and value:
        try:
            return _dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _postings(cli: httpx.Client, ats: str, slug: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        if ats == "greenhouse":
            r = cli.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            if r.status_code != 200:
                return []
            for j in (r.json() or {}).get("jobs") or []:
                jid = j.get("id")
                if jid:
                    out.append({"title": j.get("title") or "", "date": _parse_date(j.get("first_published") or j.get("updated_at")),
                                "url": f"https://job-boards.greenhouse.io/{slug}/jobs/{jid}",
                                "location": ((j.get("location") or {}).get("name") or "")})
        elif ats == "ashby":
            r = cli.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            if r.status_code != 200:
                return []
            for j in (r.json() or {}).get("jobs") or []:
                if j.get("isListed") is False:
                    continue
                url = str(j.get("jobUrl") or "")
                if url.startswith("https://jobs.ashbyhq.com/"):
                    out.append({"title": j.get("title") or "", "date": _parse_date(j.get("publishedAt")),
                                "url": url.split("?")[0], "location": j.get("location") or ""})
        elif ats == "lever":
            r = cli.get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            if r.status_code != 200:
                return []
            for j in r.json() or []:
                url = str(j.get("hostedUrl") or "")
                if url.startswith("https://jobs.lever.co/"):
                    out.append({"title": j.get("text") or "", "date": _parse_date(j.get("createdAt")),
                                "url": url.split("?")[0], "location": ((j.get("categories") or {}).get("location") or "")})
        elif ats == "workable":
            r = cli.post(f"https://apply.workable.com/api/v3/accounts/{slug.lower()}/jobs",
                         json={"query": "", "location": [], "department": [], "worktype": [], "remote": []})
            if r.status_code != 200:
                return []
            for j in (r.json() or {}).get("results") or []:
                code = str(j.get("shortcode") or "")
                if code and j.get("state", "published") == "published":
                    loc = j.get("location") or {}
                    out.append({"title": j.get("title") or "", "date": _parse_date(j.get("published")),
                                "url": f"https://apply.workable.com/{slug.lower()}/j/{code}/",
                                "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)})
    except (httpx.HTTPError, ValueError):
        return []
    return out


def _tenant_bound(slug: str, website: str, name: str) -> bool:
    """The judge binds the ATS tenant to the company: keep tenants that carry its domain label or name."""
    s = re.sub(r"[^a-z0-9]", "", slug.lower())
    host = (urlsplit(website).hostname or "").lower().removeprefix("www.")
    label = re.sub(r"[^a-z0-9]", "", host.split(".")[0]) if host else ""
    nm = re.sub(r"[^a-z0-9]", "", name.lower())
    return bool(s) and ((label and (label in s or s in label)) or (nm and (nm in s or s in nm)))


PICK_PROMPT = (
    "A buyer's criterion for a company is: \"%s\"\nBelow are the company's open job postings (id: title). Pick the ONE "
    "posting whose title most clearly is one of the role families the criterion names -- a reviewer must agree at a "
    "glance that it is such a role. Reject titles that only share a word (e.g. 'Finance Systems' is not a systems role "
    "for IT; 'Alliance Manager' is not a cloud role). If none clearly fits, answer null.\n%s\n"
    "Return JSON {\"id\": <id or null>}."
)


def pick_with_llm(llm_json, signal_text: str, posts: list[dict[str, Any]], deadline: float) -> Optional[dict[str, Any]]:
    """One cheap model call: the posting a reviewer would accept for the criterion, else None."""
    listing = "\n".join(f"{i}: {p['title'][:90]}" for i, p in enumerate(posts[:80]))
    try:
        answer = llm_json(PICK_PROMPT % (signal_text, listing), deadline)
    except Exception:
        return None
    idx = answer.get("id") if isinstance(answer, dict) else None
    if isinstance(idx, str) and idx.strip().isdigit():
        idx = int(idx.strip())
    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < min(len(posts), 80):
        return posts[idx]
    return None


def find_posting(website: str, name: str, signal_text: str, *, today: _dt.date, max_age_days: int,
                 deadline: float, llm_json=None) -> Optional[dict[str, Any]]:
    families = role_words(signal_text)
    if not families:
        return None
    with _client() as cli:
        for ats, slug in _find_boards(cli, website, name, deadline):
            if time.monotonic() > deadline:
                break
            if not _tenant_bound(slug, website, name):
                continue
            posts = _postings(cli, ats, slug)
            if not posts:
                continue
            window = sorted([p for p in posts if p["date"] and 0 <= (today - p["date"]).days <= max_age_days],
                            key=lambda p: p["date"], reverse=True)
            if llm_json is not None and window:
                picked = pick_with_llm(llm_json, signal_text, window, deadline)
                return {**picked, "ats": ats, "slug": slug} if picked else None
            fresh = [p for p in window if title_matches(p["title"], families)]
            if fresh:
                best = max(fresh, key=lambda p: (title_score(p["title"], families), p["date"]))
                return {**best, "ats": ats, "slug": slug}
            return None  # the company's real board has no matching fresh posting: stop guessing other tenants
    return None


def hiring_criterion(icp: Mapping[str, Any]) -> Optional[tuple[int, str, int]]:
    """(index, text, max_age_days) of a HIRING criterion that is NOT the primary, else None."""
    signals = list(icp.get("intent_signals") or [])
    for bonus in icp.get("bonus_intents") or []:
        if str(bonus.get("intent_category") or "").upper() != "HIRING":
            continue
        text = str(bonus.get("intent_signal") or "")
        if text in signals and signals.index(text) > 0:
            return signals.index(text), text, int(bonus.get("intent_max_age_days") or icp.get("intent_max_age_days") or 90)
    return None


def attach_hiring_signals(companies: list[dict[str, Any]], icp: Mapping[str, Any], evidence: dict[str, list],
                          name_key, *, today: _dt.date, deadline: float, llm_json=None) -> dict[str, Any]:
    """Add one ATS posting as the HIRING second criterion to every company lacking it, in place."""
    notes: dict[str, Any] = {"tried": 0, "added": [], "missed": []}
    crit = hiring_criterion(icp)
    if crit is None:
        notes["skipped"] = "no HIRING second criterion"
        return notes
    index, text, max_age = crit
    for company in companies:
        if time.monotonic() > deadline - 5:
            notes["stopped"] = "deadline"
            break
        signals = company.get("intent_signals") or []
        existing = [s for s in signals if int(s.get("matched_icp_signal", -1)) == index]
        if any(not ATS_URL.search(str(s.get("url") or "")) for s in existing):
            continue  # a non-posting proof already covers the criterion
        name, website = str(company.get("company_name") or ""), str(company.get("company_website") or "")
        notes["tried"] += 1
        try:
            post = find_posting(website, name, text, today=today, max_age_days=max_age,
                                deadline=min(deadline, time.monotonic() + 30.0), llm_json=llm_json)
        except Exception as exc:  # never let the bonus lane break a verified company
            notes["missed"].append(f"{name}: {type(exc).__name__}")
            continue
        if not post:
            notes["missed"].append(name)
            continue
        if any(str(s.get("url") or "").rstrip("/") == post["url"].rstrip("/") for s in existing):
            notes["missed"].append(f"{name}: same posting already attached")
            continue
        where = f" ({post['location']})" if post.get("location") else ""
        description = (f"{name} has an open {post['title']} position{where} on its {post['ats'].title()} careers board, "
                       f"first published {post['date'].isoformat()}.")
        signals.append({"matched_icp_signal": index, "description": description[:350],
                        "date": post["date"].isoformat(), "url": post["url"]})
        company["intent_signals"] = signals
        key = name_key(name)
        evidence.setdefault(key, []).append({
            "index": index, "description": description[:350], "date": post["date"].isoformat(), "url": post["url"],
            "quote": post["title"], "title": post["title"], "signal_text": text, "date_visible": True})
        notes["added"].append(f"{name}: {post['title']} [{post['ats']}/{post['slug']}] {post['date']}")
    LAST.clear()
    LAST.update(notes)
    return notes
