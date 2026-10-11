"""Loop v5 "evidence precision": which stage / capability evidence goes out, and which rows ride along."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import unquote, urlsplit

try:
    from . import scorer_mirror as sm
    from .arena_tools import STRATEGY
except ImportError:
    import importlib as _importlib
    import os as _os
    _pkg = _os.path.basename(_os.path.dirname(_os.path.abspath(__file__)))
    sm = _importlib.import_module(f"{_pkg}.scorer_mirror")
    STRATEGY = _importlib.import_module(f"{_pkg}.arena_tools").STRATEGY


def _knob(key: str, default: int, high: int = 1) -> int:
    value = STRATEGY.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = default
    return max(0, min(high, int(value)))


STAGE_CACHE_SCAN = _knob("stage_cache_scan", 1)            # cached pages of the company are scanned for a stage sentence
STAGE_EXTRA_SOURCES = _knob("stage_extra_sources", 2, 2)   # corroborating entries kept beside the best one
STAGE_HOST_ORDER = _knob("stage_host_order", 1)            # plain-GET-friendly hosts first, Business Wire last
STAGE_GATE_AGREE = _knob("stage_gate_agree", 1)            # scout picks its proof quote with the emit gate
STAGE_GATE_STRICT = _knob("stage_gate_strict", 1)          # 'secured-lending', advisers' deal pages, namesakes
CAPABILITY_CHROME_GUARD = _knob("capability_chrome_guard", 1)
FUNDING_LABEL_GUARD = _knob("funding_label_guard", 1)
COMPANION_DROP = _knob("companion_drop", 1)
PROVEN_ONLY_STOP = _knob("proven_only_stop", 1)
STAGE_DIRECT_CHECK = _knob("stage_direct_check", 1)        # final ordering settles unknown URLs with a free direct GET
DIRECT_CHECKS = 4                                           # ... at most this many per company
INTENT_FIRST_PARTY = _knob("intent_first_party", 1)
# Loop s37: withholding a third-party-sourced row beside a strong row rested on one ICP of 10-05 (intent passed 2 of
# 26 such rows).  On 10-06 the two such rows that reached the intent check both qualified, one of them ours.  The
# swap to the company's own announcement stays; the withhold is off unless this is 1.
INTENT_WITHHOLD = _knob("intent_withhold", 0)
# Loop s38: runs with many stage entries went unjudged more often.  Entries sent per company, best first.
STAGE_EVIDENCE_KEEP = max(1, _knob("stage_evidence_keep", 3, 3))

SCAN_PAGES = 80
SCAN_CHARS = 30000
_VENTURE_EARLY = ("seed", "series a", "series b")
_GATED_STAGES = ("series a", "series b", "series c+")


def _sibling(name: str) -> Any:
    """A module of this bundle, whether it was imported as a package or loaded by file path in the sandbox."""

    import importlib
    import os

    return importlib.import_module(f"{__package__ or os.path.basename(os.path.dirname(os.path.abspath(__file__)))}.{name}")


def _own(url: Any, domain: str) -> bool:
    host = sm.registrable_host(str(url or ""))
    return bool(host and domain) and (host == domain or host.endswith("." + domain))


# ----------------------------------------------------------------------------------------------------- A4: false proofs

_COMPOUND_RE = re.compile(r"-\w")
_SECURED_NOUN_RE = re.compile(r"\s+(?:lending|loans?|debt|credit|notes?|bonds?|creditors?|borrowing|term\s+loan)\b", re.I)
_ADVISER_RE = re.compile(r"\b(?:advis(?:ed|es|ing)|advis[eo]rs?\s+(?:to|for|on)|acted\s+(?:as|for)|counsel\s+to|represented)\b", re.I)


def round_verb(sentence: str, at: int, window: int, pattern: "re.Pattern") -> bool:
    """Is there a completion verb in the `window` characters before the round label at `at` that is the company's own?"""

    start, reach = max(0, at - window), max(0, at - 240)
    floor = start
    for adviser in _ADVISER_RE.finditer(sentence[reach:at]):
        floor = max(floor, reach + adviser.end())
    for verb in pattern.finditer(sentence[start:at]):
        tail = sentence[start + verb.end():at]
        if _COMPOUND_RE.match(tail) or (verb.group(0).lower().startswith("secur") and _SECURED_NOUN_RE.match(tail)):
            continue
        if start + verb.start() >= floor:
            return True
    return False


_NAME_SUFFIXES = frozenset({"ai", "io", "hq", "app"})


def _name_words(name: Any) -> list[str]:
    """The words a page must show to carry the company's full name: a leading 'The' and trailing generic words
    ('Labs', 'Group', 'Technologies', 'AI', ...) are left out, since headlines and third-party pages drop them."""

    try:
        base = _sibling("identity").clean_name(name)
    except Exception:
        base = str(name or "")
    noise = _sibling("scout")._NAME_NOISE | _NAME_SUFFIXES
    words = re.findall(r"[^\W_]+", base)
    if len(words) > 1 and words[0].lower() == "the":
        words = words[1:]
    while len(words) > 1 and words[-1].lower() in noise:
        words = words[:-1]
    return words


def page_binds(name: str, text: str, url: str = "", website: str = "") -> bool:
    """False when a third-party page can only be a namesake's: the company's name has several words, and the page
    carries neither that name nor the label of the company's own domain."""

    words = _name_words(name)
    domain = sm.registrable_host(website) if website else ""
    if len(words) < 2 or not domain or _own(url, domain):
        return True
    blob = f"{url} {str(text or '')[:40000]}"
    if re.search(r"(?<![^\W_])" + r"[\W_]{0,3}".join(re.escape(w) for w in words) + r"(?![^\W_])", blob, re.I):
        return True
    label = max(domain.split(".")[:-1] or [""], key=len)
    return len(label) >= 4 and label in blob.casefold()


def deal_page_refused(quote: str, name: str, url: str, website: str = "") -> bool:
    """A venture-round quote on an adviser's tombstone, an investor's portfolio entry or a deal-database profile is
    kept only when the company is the subject of the completed round; 'advised ... on its Series A' is not."""

    domain = sm.registrable_host(website) if website else ""
    return bool(_sibling("sourcetype").deal_page(url, domain)) and not led_label(_sibling("scout"), name, quote)


# ------------------------------------------------------------------------------------------ A1 / A2: gate-chosen quotes

_TITLE_TAIL_RE = re.compile(r"\s[|–—-]\s+[^|]{2,40}$")
_PAST_VERB_RE = re.compile(r"\b(?:raised|closed|secured|completed|announced|received|led)\b", re.I)
_CUT_RE = re.compile(r"\s[–—|]\s+|\s--\s+")


def quote_rank(quote: str, stage: str) -> int:
    """Smaller is better. 0: a past-tense body sentence in the judge's own first-pass form. +1 for a headline (no
    closing period, or no past-tense completion verb), +1 for a page title with a site suffix ('... - Site Name':
    such quotes came back unavailable where a body sentence passed), +2 for a 58+ word run-on, +2 when the bundled
    copy of the judge's quote check does not accept the sentence (venture stages only: that copy mis-scores '(NYSE:
    X)' listing sentences)."""

    want = sm.normalize_stage(stage)
    text = str(quote or "").rstrip()
    closed = text.endswith((".", "!", "?"))
    titled = bool(_TITLE_TAIL_RE.search(text)) and not closed
    judged, body = True, closed
    if len(text.split()) >= 58:
        return 4 + (1 if titled else 0)
    if want in ("seed",) + _GATED_STAGES:
        body = closed and bool(_PAST_VERB_RE.search(text))
        try:
            judged = bool(_sibling("reverify")._stage_quote_supports_observation(want, text))
        except Exception:
            judged = True
    return (0 if judged else 2) + (0 if body else 1) + (1 if titled else 0)


def emit_ok(quote: str, text: str, *, name: str, stage: str, url: str, website: str = "") -> str:
    """'' when verify.stage_evidence would emit this (url, quote), else why not: scout.stage_quote_ok plus the emit
    loop's own rules (public URL, a listing only from the company's page or a wire, no prompt controls)."""

    try:
        url = sm.public_http_url(url)
    except ValueError:
        return "not a public url"
    if sm.normalize_stage(stage) == "public":
        kind = _sibling("sourcetype").source_kind(url, sm.registrable_host(website) if website else "", name)
        if kind not in ("first_party", "wire"):
            return f"listing on a {kind} page"
    why = _sibling("scout").stage_quote_ok(quote, text, name=name, stage=stage, url=url, website=website)
    if why:
        return why
    return "controls" if sm.injection_match(quote) or sm.strip_prompt_controls(quote) != quote else ""


_LEAD_TAIL = (r"(?:\s+(?:inc|corp|corporation|ltd|limited|plc|llc|group|holdings)\b\.?)?(?:\s*\([^)]{0,60}\))?"
              r"(?:(?:\s*,[^,]{0,90}){1,4},)?\s+(?:(?:today|has|have|had|recently|just|also|successfully|officially|"
              r"announced|that|it|which|who|said|says)\s+){0,6}(?:rais|clos|secur|complet|announc|receiv|land)\w*")


def led_label(sc: Any, name: str, sentence: str) -> str:
    """The round a sentence states with the company as its SUBJECT, or '': the first label within 80 characters after
    '<Company>[ Inc.][ (...)][, appositive,] [has / today announced it has ...] <completion verb>'."""

    source = sc._name_source(name)
    if not source:
        return ""
    for mention in re.finditer(source, sentence, re.I):
        lead = re.match(source + _LEAD_TAIL, sentence[mention.start():], re.I)
        if not lead:
            continue
        verb_end = mention.start() + lead.end()
        label = sc._ROUND_RE.search(sentence, verb_end)
        if label and label.start() - verb_end <= 80 and not sc._UNCERTAIN_RE.search(sentence[verb_end:label.start()]):
            return sc.round_label(label.group(1))
    return ""


def _pieces(sc: Any, body: str, name: str, probe: "re.Pattern", venture: bool) -> Iterable[tuple[str, bool]]:
    """(sentence, cut) for the page's sentences."""

    source = sc._name_source(name)
    for sentence in sc.split_sentences(body):
        if not source or (len(sentence.split()) <= 60 and not sc._CHROME_RE.search(sentence)
                          and not sc._URLISH_RE.search(sentence)):
            yield sentence, False
            continue
        mentions = [m.start() for m in re.finditer(source, sentence, re.I)]
        done: set[int] = set()
        for hit in list(probe.finditer(sentence))[:8]:
            before = [at for at in mentions if at < hit.start()]
            after = [at for at in mentions if at > hit.end()]
            # an investor's announcement names the company right AFTER the label ('... has led a Series B investment
            # in <Company>, to ...'): there the stretch runs past that mention and may start at a dateline mark.
            aimed = venture and bool(STAGE_GATE_AGREE) and bool(after) and after[0] - hit.end() <= 120
            stop = len(sentence)
            if venture and after:
                stop = (after[1] if len(after) > 1 else len(sentence)) if aimed else after[0]
            if before and before[-1] not in done:
                done.add(before[-1])
                yield " ".join(sentence[before[-1]:stop].split()[:60]), True
            if aimed:
                marks = [m.end() for m in _CUT_RE.finditer(sentence, 0, hit.start())]
                words = [m.start() for m in re.finditer(r"\S+", sentence[:hit.start()])]
                start = marks[-1] if marks and len(sentence[marks[-1]:hit.start()].split()) <= 40 else \
                    words[-25] if len(words) > 25 else 0
                if -start - 1 not in done and (not before or start > before[-1]):
                    done.add(-start - 1)
                    yield " ".join(sentence[start:stop].split()[:60]), True


def stage_quotes(text: str, name: str, stage: str, url: str, website: str = "", limit: int = 3,
                 subject: bool = False) -> list[str]:
    """The sentences of one page that the emit gate accepts for the ICP stage, best first: affirmed rounds for a
    venture stage, the company-bound exchange:ticker sentence for Public, the PE-control sentence for Private
    Equity."""

    sc = _sibling("scout")
    want = sm.normalize_stage(stage)
    body = str(text or "")[:SCAN_CHARS]
    if not want or not body:
        return []
    venture = want != "public" and "equity" not in want
    probe = sc._TICKER_RE if want == "public" else sc._ROUND_RE if venture else sc._PE_WORDS_RE
    first_party = _own(url, sm.registrable_host(website) if website else "")
    found: list[tuple[str, bool]] = []
    for piece, cut in _pieces(sc, body, name, probe, venture):
        if not probe.search(piece):
            continue
        if want == "public":
            found.append((sc.own_ticker_sentence(piece, name), False))
        elif not venture:
            found.append(("" if cut else sc.pe_sentence(piece, name), False))
        else:
            found.extend((sentence, cut or subject)
                         for label, sentence, _judge in sc.round_claims(piece, name, first_party=first_party)
                         if sm.stage_matches(label, want))
    ranked: dict[str, tuple[int, int]] = {}
    for index, (quote, bound) in enumerate(dict.fromkeys(item for item in found if item[0])):
        if len(ranked) >= 12:
            break
        if emit_ok(quote, body, name=name, stage=stage, url=url, website=website):
            continue
        quote = tighten(quote, body, name=name, stage=stage, url=url, website=website)
        if bound and not sm.stage_matches(led_label(sc, name, quote), want) and \
                not any(sc.round_target(quote, m, name) or first_party and sc.own_round(quote, m)
                        for m in sc._ROUND_RE.finditer(quote)):
            continue        # e.g. a customer story on the company's own site: '<Customer> closed its Series B'
        ranked.setdefault(quote, (quote_rank(quote, stage), index))
    return [quote for quote, _key in sorted(ranked.items(), key=lambda kv: kv[1])[:limit]]


def tighten(quote: str, text: str, *, name: str, stage: str, url: str, website: str = "") -> str:
    """The accepted sentence without what the page glued in front of it."""

    source = _sibling("scout")._name_source(name)
    cuts = {m.end() for m in _CUT_RE.finditer(quote)}
    if source:
        cuts |= {m.start() for m in re.finditer(source, quote, re.I)}
    for at in sorted(cuts, reverse=True):
        cut = quote[at:].strip()
        if len(quote[:at].split()) > 3 and len(cut.split()) >= 6 and \
                not emit_ok(cut, text, name=name, stage=stage, url=url, website=website):
            return cut
    return quote


def emit_quote(text: str, name: str, stage: str, url: str, website: str = "") -> str:
    """The best sentence of this page the emit gate accepts, or ''."""

    return (stage_quotes(text, name, stage, url, website, limit=1) or [""])[0]


def names_stage(quote: Any, stage: str) -> bool:
    """A quote naming a round the ICP stage accepts."""

    sc = _sibling("scout")
    want = sm.normalize_stage(stage)
    return any(sm.stage_matches(sc.round_label(m.group(1)), want) for m in sc._ROUND_RE.finditer(str(quote or "")))


# --------------------------------------------------------------------------------------------------- A3: source order

def direct_state(tools: Any, url: str, check: bool = False) -> Optional[bool]:
    """tools.direct_ok(url) when the tools object has it: True a direct plain GET returned the page, False it was
    tried and failed, None unknown (also for a missing method or any error)."""

    probe = getattr(tools, "direct_check" if check else "direct_ok", None)
    if not callable(probe):
        return None
    try:
        value = probe(url)
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def evidence_key(tools: Any, url: str, quote: str, stage: str, website: str = "", check: bool = False) -> tuple[int, int, int]:
    """Sort key of one accepted stage-evidence entry, smaller is better."""

    domain = sm.registrable_host(website) if website else ""
    rank = int(_sibling("sourcetype").stage_host_rank(url, domain))
    state = direct_state(tools, url) if rank in (1, 2) else None
    if state is None and check and rank in (1, 2):
        state = direct_state(tools, url, True)
    if rank == 1:
        tier = 0 if state is not False else 2
    elif rank == 0:
        tier = 1
    elif rank == 2:
        tier = 2 if state is True else 4 if state is False else 3
    else:
        tier = rank + 2
    return tier, rank, quote_rank(quote, stage)


def order_evidence(items: list[tuple[str, str]], *, tools: Any, stage: str, website: str = "",
                   check: bool = False) -> list[tuple[str, str]]:
    """Accepted (url, quote) entries, one per URL (its best quote) and one URL per site for the same quote text (a
    banner sentence on three pages of one site is one piece of evidence, not three; a release on a wire and on the
    company's newsroom is two), best source first; ties keep the given order."""

    best: dict[str, tuple[tuple[int, int, int], int, str]] = {}
    checks = DIRECT_CHECKS if check and STAGE_DIRECT_CHECK else 0
    for index, (url, quote) in enumerate(items):
        held = best.get(url)
        probe = bool(checks) and held is None and direct_state(tools, url) is None
        key = evidence_key(tools, url, quote, stage, website, probe)
        if probe and key[1] in (1, 2):
            checks -= 1
        if held is None:
            best[url] = (key, index, quote)
        elif key[2] < held[0][2]:
            best[url] = ((held[0][0], held[0][1], key[2]), held[1], quote)
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for url, (_key, _index, quote) in sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1])):
        mark = same_site_quote(url, quote)
        if mark not in seen:
            seen.add(mark)
            out.append((url, quote))
    return out


def same_site_quote(url: Any, quote: Any) -> tuple[str, str]:
    """(site, quote text) -- two entries with the same mark are one piece of evidence."""

    return sm.registrable_host(str(url or "")), " ".join(str(quote or "").split()).casefold()


def rank_claims(tools: Any, claims: list[tuple[str, str]], name: str, stage: str, website: str = "") -> list[tuple[str, str, bool]]:
    """The stage search's (url, sentence) claims for the proven round as (url, quote, emit gate accepts), the ones the
    gate accepts first and in source order; each page is asked for its best accepted sentence."""

    pages = getattr(tools, "pages", None) or {}
    good: list[tuple[str, str]] = []
    rest: list[tuple[str, str, bool]] = []
    for url, sentence in claims:
        page = pages.get(url)
        text = str(getattr(page, "text", "") or "") if getattr(page, "ok", False) else ""
        quote = emit_quote(text, name, stage, url, website) if text and STAGE_GATE_AGREE else ""
        if not quote and text and not emit_ok(sentence, text, name=name, stage=stage, url=url, website=website):
            quote = sentence
        if quote:
            good.append((url, quote))
        else:
            rest.append((url, sentence, False))
    if STAGE_HOST_ORDER:
        good = order_evidence(good, tools=tools, stage=stage, website=website)
    return [(url, quote, True) for url, quote in good] + rest


# ---------------------------------------------------------------------------------------------- A2: cached-page scan

def company_pages(tools: Any, name: str, website: str = "", urls: Iterable[Any] = ()) -> list[tuple[str, str]]:
    """(url, text) of the cached, readable pages that can speak about this company, likeliest first: the listed URLs
    (intent pages, stage hint, required-attribute page), its own domain, then every other cached page naming it
    (news rows, the result pages of the stage and current-stage searches)."""

    sc, st = _sibling("scout"), _sibling("sourcetype")
    domain = sm.registrable_host(website) if website else ""
    listed = {str(u) for u in urls if u}
    words = [w for w in re.findall(r"[a-z0-9]+", str(name or "").casefold()) if w not in sc._NAME_NOISE]
    token = words[0] if words and len(words[0]) >= 3 else ""
    ranked: list[tuple[int, int, str, str]] = []
    for index, (url, page) in enumerate(list((getattr(tools, "pages", None) or {}).items())):
        text = str(getattr(page, "text", "") or "") if getattr(page, "ok", False) else ""
        if len(text) < 60 or getattr(page, "source", "") == "direct_json" or st._is(st.host_of(url), st.ATS_HOSTS):
            continue        # a job posting's 'recently raised ...' line passed the judge as stage evidence 1 time in 3
        if url in listed:
            rank = 0
        elif _own(url, domain):
            rank = 1
        elif token and token in text[:SCAN_CHARS].casefold():
            rank = 2
        else:
            continue
        ranked.append((rank, index, str(url), text))
    return [(url, text) for _rank, _index, url, text in sorted(ranked)[:SCAN_PAGES]]


def later_round(text: str, name: str, want: str, url: str, website: str = "") -> str:
    """A round later than the ICP's Seed / A / B stage that this page states with the company as the subject."""

    sc = _sibling("scout")
    if want not in sc._ORDER or not page_binds(name, text, url, website):
        return ""
    for sentence in sc.split_sentences(str(text or "")[:SCAN_CHARS]):
        if not sc._ROUND_RE.search(sentence):
            continue
        label = led_label(sc, name, sentence)
        if label in sc._ORDER and sc._ORDER.index(label) > sc._ORDER.index(want):
            return label
        for match in sc._ROUND_RE.finditer(sentence):      # '<Investor> has led a Series B investment in <Company>'
            label = sc.round_label(match.group(1))
            if label in sc._ORDER and sc._ORDER.index(label) > sc._ORDER.index(want) and \
                    sc.round_target(sentence, match, name):
                return label
    return ""


def cache_scan(tools: Any, *, name: str, stage: str, website: str = "", urls: Iterable[Any] = ()) -> dict[str, Any]:
    """{'entries': [(url, quote)], 'later': label}: for each cached page of this company the best sentence the emit
    gate accepts for the ICP stage, best source first."""

    sc = _sibling("scout")
    want = sm.normalize_stage(stage)
    if not want or not name:
        return {"entries": [], "later": ""}
    probe = sc._TICKER_RE if want == "public" else sc._PE_WORDS_RE if "equity" in want else sc._ROUND_RE
    found: list[tuple[str, str]] = []
    pages = company_pages(tools, name, website, urls)
    venture = want != "public" and "equity" not in want
    if venture:
        # the stage search drops a company on any page saying it was acquired or taken private (scout._OWNED_RE);
        # a cached page saying so must not supply the round sentence instead.
        for url, text in pages:
            for sentence in re.split(r"(?<=[.!?])\s+", text[:20000]):
                if sc._OWNED_RE.search(sentence) and sc.name_hit(name, sentence):
                    return {"entries": [], "later": f"ownership change ({url[:90]})"}
    domain = sm.registrable_host(website) if website else ""
    listed = {str(u) for u in urls if u}
    loose = bool(domain) and len(_name_words(name)) < 2      # a one-word name: any namesake's page matches it
    for url, text in pages:
        if not probe.search(text[:SCAN_CHARS]):
            continue
        if loose and url not in listed and not _own(url, domain) and not _names_this_company(tools, url, text, domain, name):
            continue
        if want in _VENTURE_EARLY:
            later = later_round(text, name, want, url, website)
            if later:
                return {"entries": [], "later": f"{later} ({url[:90]})"}
            if sc.later_round_on_page(tools, url, want):
                continue
        quote = (stage_quotes(text, name, stage, url, website, limit=1, subject=True) or [""])[0]
        if quote:
            found.append((url, quote))
    if STAGE_HOST_ORDER:
        found = order_evidence(found, tools=tools, stage=stage, website=website)
    return {"entries": found, "later": ""}


def _names_this_company(tools: Any, url: str, text: str, domain: str, name: str) -> bool:
    """What tells a third-party page about a one-word-named company from a namesake's (a namesake's round cited as
    stage evidence failed 3 times of 3): the company's registrable domain in the page's text or among its links, or
    its full name when that has a second word ('<Word> AI', '<Word> Technology')."""

    low = str(text or "")[:40000].casefold()
    if domain in low:
        return True
    full = re.findall(r"[^\W_]+", str(name or "").casefold())
    if len(full) >= 2 and re.search(r"(?<![^\W_])" + r"[\W_]{0,3}".join(re.escape(w) for w in full) + r"(?![^\W_])", low):
        return True
    page = (getattr(tools, "pages", None) or {}).get(url)
    return any(domain in str(link).casefold() for link in (getattr(page, "links", None) or [])[:400])


def _funding_page(sc: Any, url: str, text: str, name: str, stage: str) -> bool:
    """A page ABOUT the round: its URL slug or its opening names a round the ICP stage accepts, or its slug is a
    '<Company> raises ...' headline."""

    try:
        path = unquote(urlsplit(str(url)).path)
    except ValueError:
        path = ""
    return names_stage(re.sub(r"[-_/.]+", " ", path) + " " + str(text or "")[:300], stage) or bool(sc.raise_title(name, path))


def cache_proof(tools: Any, name: str, prof: Mapping[str, Any], icp: Mapping[str, Any],
                urls: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
    """A stage proof read from pages already in the cache, before any stage search is paid: {'url', 'quote', 'extras',
    'date'}; {'later': ...} when the cache affirms a later round or an ownership change; None when it says nothing."""

    stage = str(icp.get("company_stage") or "")
    website = str(prof.get("website") or prof.get("domain") or "")
    got = cache_scan(tools, name=name, stage=stage, website=website, urls=list(urls) + [website])
    if got["later"]:
        return {"later": got["later"]}
    if not got["entries"]:
        return None
    url, quote = got["entries"][0]
    page = (getattr(tools, "pages", None) or {}).get(url)
    sc, text = _sibling("scout"), str(getattr(page, "text", "") or "")
    return {"url": url, "quote": quote[:2000], "date": sc.find_date(text, url) if _funding_page(sc, url, text, name, stage) else None,
            "extras": [{"url": u, "quote": q[:2000]} for u, q in got["entries"][1:1 + STAGE_EXTRA_SOURCES]]}


# ------------------------------------------------------------------------------------------ A5: penalty suppression

_CONSENT_RE = re.compile(
    r"\bcookies?\b|\bconsent\s+(?:management|preferences|banner|settings)\b|\bprivacy\s+(?:policy|notice|preferences|settings|choices)\b|"
    r"\bopt[- ]?out\b|\bweb\s+beacons?\b|\btracking\s+(?:technolog\w+|pixels?|scripts?)\b|\bhashed\s+email\b|\bvalue\s+your\s+privacy\b|"
    r"\byour\s+(?:ip\s+address|browsing\s+(?:experience|behaviou?r|activity|history)|personal\s+(?:data|information))\b|"
    r"\b(?:targeted|personali[sz]ed|customi[sz]ed|relevant|interest[- ]based)\s+(?:advertis\w+|ads)\b|\bdo\s+not\s+sell\b", re.I)


_CONSENT_DOMAIN_RE = re.compile(r"advertis|ad[- ]?tech|privacy|consent|cookie|tracking|personali[sz]|data protection", re.I)
_BANNER_RE = re.compile(r"\b(?:we|this (?:web)?site|our (?:web)?site)\s+(?:use|uses|may use|collects?)\b|\bthese cookies\b|"
                        r"\byour (?:browsing|ip address|consent|preferences|choices)\b|"
                        r"\b(?:cookie|privacy)\s+(?:policy|notice)\b|\baccept (?:all|cookies)\b", re.I)


def consent_text(sentence: Any, icp_text: Any = "") -> bool:
    """Cookie, consent, tracking or privacy-banner wording: page chrome, never a statement of what the company sells."""

    focus = str(icp_text or "").casefold()
    if _CONSENT_DOMAIN_RE.search(focus) and not _BANNER_RE.search(str(sentence or "")):
        return False        # an advertising / privacy ICP: this vocabulary is the product, only banner phrasing is chrome
    for hit in _CONSENT_RE.finditer(str(sentence or "")):
        if not any(word in focus for word in re.findall(r"[a-z]{5,}", hit.group(0).casefold())):
            return True
    return False


def capability_state(claim: Mapping[str, Any], website: str, icp: Mapping[str, Any], capable: bool) -> tuple[bool, bool, str]:
    """(capability proven, proven on the company's own domain, note) for the verified required_attribute claim."""

    host, domain = sm.registrable_host(str(claim.get("evidence_url") or "")), sm.registrable_host(website)
    own = bool(host and domain) and (host == domain or host.endswith("." + domain) or domain.endswith("." + host))
    focus = " ".join(str(icp.get(k) or "") for k in ("required_attribute", "product_service", "sub_industry", "industry"))
    if capable and CAPABILITY_CHROME_GUARD and consent_text(claim.get("evidence_quote"), focus):
        return False, own, "required_attribute quote is cookie / consent text: capability unproven"
    return capable, own, ""


_LATER_RE = re.compile(r"\b(?:or|and)\s+(?:later|above|beyond|higher|subsequent)\b|\+", re.I)


def funding_label_ok(signal: Any, event_text: Any) -> bool:
    """A FUNDING criterion that names a round ('Announced a Series A funding round ...') is shown only by an event
    text naming that round; 'or later' admits every later one."""

    sc = _sibling("scout")

    def labels(text: Any) -> set[str]:
        return {label for label in (sc.round_label(m.group(1)) for m in sc._ROUND_RE.finditer(str(text or "")))
                if label in sc._ORDER}

    named, seen = labels(signal), labels(event_text)
    if not named or named & seen:
        return True
    floor = min(sc._ORDER.index(label) for label in named)
    return bool(_LATER_RE.search(str(signal or ""))) and any(sc._ORDER.index(label) >= floor for label in seen)


def _signal_round(row: Mapping[str, Any], stage: str) -> bool:
    """The row's own intent signal (its description or URL slug) names a round the ICP stage accepts."""

    try:
        return names_stage(" ".join(f"{s.get('description') or ''} " + re.sub(r"[-_/]+", " ", str(s.get("url") or ""))
                                    for s in row.get("intent_signals") or [] if isinstance(s, Mapping)), stage)
    except Exception:
        return True


def companion_drop(rows: list[Any], icp: Mapping[str, Any]) -> tuple[list[Any], list[tuple[Any, str]]]:
    """(rows kept, [(row dropped, reason)]) at final selection."""

    want = sm.normalize_stage(icp.get("company_stage"))
    gated = want in _GATED_STAGES
    needs_capability = bool(str(icp.get("required_attribute") or "").strip())

    def strong(row: Mapping[str, Any]) -> bool:
        return (not needs_capability or bool(row.get("_capability") and row.get("_cap_own"))) and \
            (not gated or bool(row.get("company_stage_evidence"))) and not row.get("_weak")

    if len(rows) < 2 or not (needs_capability or gated) or not any(strong(row) for row in rows):
        return rows, []
    kept: list[Any] = []
    dropped: list[tuple[Any, str]] = []
    for row in rows:
        why = ""
        if not strong(row):
            if needs_capability and not row.get("_capability"):
                why = "capability unproven"
            elif row.get("_weak") and INTENT_WITHHOLD:   # v6 intent_first_party, s37 intent_withhold
                why = "third-party signal page on an ICP naming its proof sources"
            elif gated and row.get("_stage_unproven") and not row.get("company_stage_evidence") and \
                    not _signal_round(row, str(icp.get("company_stage") or "")):
                why = f"stage unproven on a {icp.get('company_stage')} ICP"
        if why:
            dropped.append((row, f"companion drop ({why} beside a strong row)"))
        else:
            kept.append(row)
    return (kept, dropped) if kept else (rows, [])


_SCREENED_STAGES = _GATED_STAGES + ("private equity",)


def _primary(row: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    signals = [x for x in row.get("intent_signals") or [] if isinstance(x, Mapping)]
    return min(signals, key=lambda x: x.get("matched_icp_signal") or 0) if signals else None


_LI_POST_RE = re.compile(r"/(?:posts|feed|pulse)/|/activity", re.I)


def li_post(row: Mapping[str, Any]) -> bool:
    """The primary signal page is a LinkedIn post: 0 of 31 such rows qualified on 10-07/10-09."""

    try:
        parts = urlsplit(str((_primary(row) or {}).get("url") or "").strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return (host == "linkedin.com" or host.endswith(".linkedin.com")) and bool(_LI_POST_RE.search(parts.path))


def stage_screen(rows: list[Any], icp: Mapping[str, Any]) -> tuple[list[Any], list[Any]]:
    """(rows kept, rows set aside).  Any ICP: a LinkedIn-post row goes unless it is the only row left.  Series A/B/C+
    and PE ICPs: an unevidenced row goes beside an evidenced one."""

    posts = [li_post(row) for row in rows]
    gone: list[Any] = []
    if len(rows) > 1 and any(posts):
        stay = [r for r, p in zip(rows, posts) if not p] or rows[:1]
        gone = [r for r in rows if not any(r is s for s in stay)]
        rows = stay
    stage = str(icp.get("company_stage") or "")
    if sm.normalize_stage(stage) not in _SCREENED_STAGES:
        return rows, gone

    def proven_row(row: Mapping[str, Any]) -> bool:
        primary = _primary(row)
        return bool(row.get("company_stage_evidence")) or bool(primary and _signal_round({"intent_signals": [primary]}, stage))

    proven = [proven_row(row) for row in rows]
    if not any(proven) or all(proven):
        return rows, gone
    return [r for r, ok in zip(rows, proven) if ok], gone + [r for r, ok in zip(rows, proven) if not ok]


def resolve_full(drafts: list[Any], limit: int) -> bool:
    """The resolve loop's 'enough drafts' stop."""

    if len(drafts) < limit:
        return False
    proven = sum(1 for draft in drafts if not draft.get("_stage_unproven"))
    return proven >= limit or len(drafts) >= limit + (12 if proven >= 2 else 5)


__all__ = ["round_verb", "page_binds", "deal_page_refused", "quote_rank", "emit_ok", "led_label",
           "stage_quotes", "tighten", "emit_quote", "names_stage", "direct_state", "evidence_key", "order_evidence", "same_site_quote", "rank_claims", "company_pages",
           "later_round", "cache_scan", "cache_proof", "consent_text", "capability_state",
           "funding_label_ok", "companion_drop", "li_post", "stage_screen", "resolve_full"]
