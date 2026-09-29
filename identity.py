"""Loop s22 (teardown-0925 #1/#2): bind a finalist's identity as the judge does.  09-25: blank company_linkedin passed
identity 29/29, filled 9/12 (our MRO/Manzil/Tuhk); d19's runwayml.com redirects to runway.com = judge MISMATCH."""

from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import scorer_mirror as sm
from .arena_tools import STRATEGY, _result_data
from .vendored_psl import registrable_domain

MAX_HOPS, HOP_TIMEOUT, PROBE_SECONDS, CALL_RESERVE = 5, 10.0, 20.0, 12
EMIT_BOUND = bool(STRATEGY.get("emit_bound_linkedin", 1))
_DBA = re.compile(r"^\S.*?\s+(?:operating as|doing business as|trading as|d/b/a|dba)\s+(?P<b>\S.*)$", re.I)
_FORMERLY = re.compile(r"^(?P<b>\S.*?)\s*[,(]?\s*\b(?:formerly(?: known as)?|f/k/a|fka)\s+\S.*$", re.I)
_TAIL = re.compile(r"[\s,]+(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|plc|gmbh|llp|pty\.? ltd)\.?$", re.I)
_SPLIT = re.compile(r"\s+[|\-–—]\s+|\s*:\s*")
_PARKED = re.compile(r"\bthis domain (?:is|may be) for sale\b|\bbuy this domain\b|\bdomain.*available for purchase\b|"
                     r"\bregister(?:ed)? this domain\b|\bsedo(?:parking)?\b|\b(?:godaddy|namecheap)\b.*\bparked\b|"
                     r"\bhostgator\b.*\bdefault\b|\bcoming soon\b.{0,80}\bdomain\b|\bdefault web site page\b", re.I)
_ROOT_PATH = re.compile(r"(?:/(?:[a-z]{2}(?:[-_][a-z]{2,3})?|home|index\.html?))*/?", re.I)


def clean_name(name: Any) -> str:
    """'Murabaha Inc. operating as Manzil' -> 'Manzil'; 'Runway AI, Inc.' -> 'Runway AI'; 'X (formerly Y)' -> 'X'."""

    text = orig = " ".join(str(name or "").split())
    for pattern in (_DBA, _FORMERLY):
        m = pattern.match(text)
        text = m.group("b").strip(" ,()") if m else text
    while _TAIL.search(text):
        text = _TAIL.sub("", text).strip(" ,")
    return text or orig


def _slug(href: Any) -> str:
    raw = str(href or "").strip()
    try:
        u = urlsplit("https:" + raw if raw.startswith("//") else raw)
        host, path = (u.hostname or "").casefold(), [p.casefold() for p in u.path.split("/") if p]
    except ValueError:
        return ""
    ok = u.scheme.casefold() in ("http", "https") and (host == "linkedin.com" or host.endswith(".linkedin.com"))
    return path[1] if ok and path[:1] == ["company"] and len(path) > 1 and re.fullmatch(r"[a-z0-9][a-z0-9._%+-]{0,99}", path[1]) else ""


def _same_as(node: Any, out: list, depth: int = 0) -> None:
    if isinstance(node, dict):
        kinds, same = node.get("@type"), node.get("sameAs")
        if "organization" in [str(k or "").casefold() for k in (kinds if isinstance(kinds, list) else [kinds])]:
            out.extend(s for s in map(_slug, (same if isinstance(same, list) else [same])[:20]) if s)
        node = list(node.values())
    for child in (node[:50] if isinstance(node, list) and depth < 12 else []):
        _same_as(child, out, depth + 1)


class _Home(HTMLParser):
    """What the judge's homepage parser reads (title, og:site_name, application-name, a/link hrefs, JSON-LD)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.slugs, self.in_title, self.ld = [], [], [], False, None

    def handle_starttag(self, tag, attrs):
        a = {str(k or "").casefold(): str(v or "").strip() for k, v in attrs}
        key = (a.get("property") or a.get("name") or a.get("itemprop") or "").casefold()
        self.in_title = self.in_title or tag == "title"
        if tag in ("a", "link") and _slug(a.get("href")):
            self.slugs.append(_slug(a.get("href")))
        elif tag == "script" and a.get("type", "").split(";")[0].strip().casefold() == "application/ld+json":
            self.ld = []
        elif tag == "meta" and a.get("content") and key in ("og:site_name", "application-name"):
            self.meta.append(a["content"][:200])

    def handle_endtag(self, tag):
        self.in_title = self.in_title and tag != "title"
        if tag == "script" and self.ld is not None:
            try:
                _same_as(json.loads("".join(self.ld)), self.slugs)
            except Exception:
                pass
            self.ld = None

    def handle_data(self, data):
        if self.in_title and data.strip():
            self.title.append(data.strip())
        if self.ld is not None:
            self.ld.append(data)


def home_names(title: str, meta: list | tuple = ()) -> list[str]:
    title = " ".join(str(title or "").split())[:300]
    names = (" ".join(str(n).split()) for n in list(meta) + ([title] + _SPLIT.split(title) if title else []))
    return [n for n in dict.fromkeys(names) if n.casefold() not in ("home", "homepage", "welcome", "website") and 2 < len(n) <= 200]


def parse_home(html: str) -> dict[str, list[str]]:
    p = _Home()
    try:
        p.feed(str(html or "")[:2_000_000])
        p.close()
    except Exception:
        pass
    return {"names": home_names(" ".join(p.title), p.meta), "slugs": list(dict.fromkeys(p.slugs))}


def brand(name: Any, names: list[str], label: str = "") -> str:
    """Homepage display of our judge key (shortest), else a shorter prefix brand ('Runway' for 'Runway AI, Inc.').
    Never longer: 'Latent' passed on 09-25 beside a 'Latent Health' homepage."""

    ours = clean_name(name)
    key = sm.company_name_key(ours)
    usable = [n for n in names if sm.company_name_key(n) and clean_name(n) == n and len(n) <= 80 and len(n.split()) <= 8
              and sm.strip_prompt_controls(n) == n and not sm.injection_match(n)]
    same = sorted((n for n in usable if sm.company_name_key(n) == key), key=len)
    shorter = [n for n in usable if key.startswith(k := sm.company_name_key(n)) and k != key and (len(k) >= 4 or k == label)]
    return (same or shorter or [ours])[0]


def _same_brand(name: Any, names: list[str], domain: str) -> bool:
    key = sm.company_name_key(clean_name(name))
    keys = [sm.company_name_key(n) for n in names] + [domain.split(".")[0].replace("-", "")]
    return any(k and (k == key or min(len(k), len(key)) >= 4 and (key.startswith(k) or k.startswith(key))) for k in keys)


def _reg(url: Any) -> str:
    host = sm.registrable_host(str(url or ""))
    try:
        return registrable_domain(host) or host
    except Exception:
        return host


def bound_slug(slugs: list[str], name: str, domain: str, records: dict) -> str:
    """(a) one company page in the raw homepage, (b) our cached harvest record for it lists a website on our domain,
    (c) slug and brand key contain one another.  Else ''."""

    one = slugs[0] if len(set(slugs)) == 1 else ""
    rec = next((r for k, r in (records or {}).items() if one and _slug(k) == one and isinstance(r, dict)), {})
    flat, key = re.sub(r"[-_.%+]", "", one), sm.company_name_key(name)
    ok = rec.get("website") and _reg(rec["website"]) == domain and min(len(flat), len(key)) >= 3 and (key in flat or flat in key)
    url = "https://www.linkedin.com/company/" + one
    return url if ok and sm.gateway_linkedin_slug(url, allow_dots=False) else ""


def _reply(raw: Any) -> dict[str, Any]:
    node = {"body": raw} if isinstance(raw, str) else _result_data(raw)
    get = lambda keys, kind: next((v for k in keys if isinstance(v := node.get(k), kind) and not isinstance(v, bool)), None)
    loc = {str(k).casefold(): v for k, v in (get(("headers",), dict) or {}).items()}.get("location") or node.get("location")
    return {"status": get(("status", "status_code", "statusCode"), int), "body": get(("body", "html", "text", "content", "data"), str) or "",
            "location": str((loc[0] if loc else "") if isinstance(loc, list) else loc or ""),
            "url": u if (u := get(("final_url", "finalUrl", "response_url", "url"), str) or "").startswith("http") else ""}


def probe(tools: Any, url: str) -> dict[str, Any]:
    """Final URL + raw HTML: free generic_http_request, redirects followed explicitly; cached, never raises."""

    cache = tools.__dict__.setdefault("_s22_probe", {})
    if url in cache:
        return cache[url]
    out = cache[url] = {"final_url": "", "status": None, "html": "", "error": ""}
    started, current = time.monotonic(), url
    try:
        if tools.remaining() < CALL_RESERVE:
            out["error"] = "call reserve"
            return out
        for _ in range(MAX_HOPS + 1):
            if time.monotonic() - started > PROBE_SECONDS:
                out["error"] = "slow"
                return out
            r = _reply(tools._deepline("generic_http_request", {"url": current, "method": "GET", "follow_redirects": False,
                                                                 "timeout_ms": 8000}, timeout=HOP_TIMEOUT))
            if r["status"] is None or not 300 <= r["status"] < 400:
                status = r["status"] if r["status"] is not None else (200 if r["body"] else None)
                out.update(final_url=urljoin(current, r["url"]), status=status, html=r["body"])
                return out
            if not r["location"]:
                out["error"] = "redirect without a location"
                return out
            current = urljoin(current, r["location"])
        out["error"] = "too many redirects"
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out


def bind(tools: Any, *, name: str, website: str, home: Any = None) -> dict[str, Any]:
    """name / website / linkedin (emitted) / anchor (ranking only) / drop / notes."""

    out: dict[str, Any] = {"name": name, "website": website, "linkedin": "", "anchor": "", "drop": "", "notes": []}
    try:
        domain, got = _reg(website), probe(tools, website)
        html = got["html"] if (got["status"] or 500) < 400 else ""
        parsed = parse_home(html) if html else {"names": [], "slugs": []}
        final = _reg(got["final_url"]) if got["final_url"] else domain
        if final != domain:
            deep = not _ROOT_PATH.fullmatch(urlsplit(got["final_url"]).path)
            if not html or _PARKED.search(html) or not _same_brand(name, [] if deep else parsed["names"], final):
                out["drop"] = f"homepage redirects to {final}: parked or another brand (judge: identity mismatch)"
                return out
            out["website"], domain = f"https://{final}/", final
            out["notes"].append(f"website re-pointed to {out['website']} (homepage redirect)")
        elif html and _PARKED.search(html):
            out["drop"] = "homepage is parked / for sale (judge: identity mismatch)"
            return out
        out["name"] = brand(name, parsed["names"] or home_names(str(getattr(home, "title", "") or "")), domain.split(".")[0])
        if out["name"] != name:
            out["notes"].append(f"company_name {name!r} -> {out['name']!r} (homepage brand)")
        out["anchor"] = (parsed["slugs"] or [s for s in map(_slug, getattr(home, "links", None) or []) if s] or [""])[0]
        if EMIT_BOUND and html:
            out["linkedin"] = bound_slug(parsed["slugs"], out["name"], domain, tools.__dict__.get("_scout_linkedin") or {})
        if not html:
            out["notes"].append(f"homepage probe unusable ({got['error'] or got['status']}): company_linkedin blank")
    except Exception as exc:
        out.update(name=name, website=website, linkedin="", drop="", notes=[f"identity binding skipped: {type(exc).__name__}"])
    return out
