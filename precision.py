"""Loop v5 "evidence precision": which stage / capability evidence goes out, and which rows ride along.

Pure text work on pages the run already holds (tools.pages): nothing here calls a provider or fetches a page.
scout.py and verify.py call each entry point behind a strategy.json knob (default below; 0 switches it off) and
inside a guard that keeps the previous behaviour on any error.

Why (published rounds 09-30 .. 10-03):
  * a row without company_stage_evidence passed stage 1 time in 137 on Series A / B / C+ ICPs, with one entry 79%;
  * the judge re-fetches the cited URL with a plain GET: the same release passed 93-97% from PR Newswire or
    GlobeNewswire, 89% from the company's own announcement page and about 35% from Business Wire;
  * a quote without the round label passed 7%, an adviser's deal page 1 time in 16;
  * a returned row whose required attribute is not confirmed costs a third of a qualified company, but only on an
    ICP where something qualifies -- elsewhere it is free.
"""

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
    """Is there a completion verb in the `window` characters before the round label at `at` that is the company's own?

    Not a compound adjective ('AI secured-lending platform', 'closed-end fund'), not 'secured loan / debt / credit',
    and not a verb standing before an advisory verb: in 'X announced that it advised Y on its Series A fundraise' the
    round is the adviser's mandate, and such a quote passed the judge 1 time in 16."""

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
    carries neither that name nor the label of the company's own domain.  Then a sentence matching the first word
    alone ('<Word> secures $46M Series B financing' on the site of another company whose name starts with the same
    word) binds nothing.  A one-word name, an unknown website and the company's own domain always bind."""

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
    """Smaller is better.  0: a past-tense body sentence in the judge's own first-pass form.  +1 for a headline (no
    closing period, or no past-tense completion verb), +1 for a page title with a site suffix ('... - Site Name':
    such quotes came back unavailable where a body sentence passed), +2 for a 58+ word run-on, +2 when the bundled
    copy of the judge's quote check does not accept the sentence (venture stages only: that copy mis-scores
    '(NYSE: X)' listing sentences)."""

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
    loop's own rules (public URL, a listing only from the company's page or a wire, no prompt controls).  A proof
    chosen with this function is never withheld later."""

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
    '<Company>[ Inc.][ (...)][, appositive,] [has / today announced it has ...] <completion verb>'.  In the run-on
    text of a scraped page '<Company> Industry: ... Related deals <Other> closed a Series A' names the company before
    the label too; there the verb belongs to someone else and this returns ''."""

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
    """(sentence, cut) for the page's sentences.  A scraped page has no line breaks, so menus, a headline and a
    dateline run into the lead sentence -- the very one that states the round or the listing -- and push it past the
    selectors' 60 words or put page chrome into it.  Such a sentence yields, for each stage wording in it, the
    verbatim stretch from the company's last mention before that wording to the end of the sentence (60 words at
    most; a venture round's stretch ends before the next mention of the company, where the next statement starts)."""

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
    venture stage, the company-bound exchange:ticker sentence for Public, the PE-control sentence for Private Equity.
    With `subject` (the cached-page scan, which reads every page naming the company) a venture sentence must also
    have the company as the subject of the round, as its named target or, on its own domain, as 'our Series B'."""

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
    """The accepted sentence without what the page glued in front of it.  A scraped page has no line breaks, so
    navigation words, a headline or a dateline run into the lead sentence ('Ok, Thanks Seed Round <Company> Raises
    ... <Company>, a ..., has raised $4.5 million in Seed funding').  The cut starts at a mention of the company or
    after a dateline dash -- the latest one that the emit gate still accepts -- and is verbatim page text; a lead of
    three words or fewer ('This article covers <Company> ...') is the sentence's own opening and stays."""

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
    """A quote naming a round the ICP stage accepts.  The older test looked for the normalised stage as a substring,
    'series c' for 'Series C+', so a Series D-H quote was never offered to the gate."""

    sc = _sibling("scout")
    want = sm.normalize_stage(stage)
    return any(sm.stage_matches(sc.round_label(m.group(1)), want) for m in sc._ROUND_RE.finditer(str(quote or "")))


# --------------------------------------------------------------------------------------------------- A3: source order

def direct_state(tools: Any, url: str, check: bool = False) -> Optional[bool]:
    """tools.direct_ok(url) when the tools object has it: True a direct plain GET returned the page, False it was
    tried and failed, None unknown (also for a missing method or any error).  With `check` an unknown URL is tried
    once through tools.direct_check (a free GET, no provider call)."""

    probe = getattr(tools, "direct_check" if check else "direct_ok", None)
    if not callable(probe):
        return None
    try:
        value = probe(url)
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def evidence_key(tools: Any, url: str, quote: str, stage: str, website: str = "", check: bool = False) -> tuple[int, int, int]:
    """Sort key of one accepted stage-evidence entry, smaller is better.  The HOST CLASS leads (first-entry stage
    pass over all published rounds: PR Newswire / GlobeNewswire 97%, the company's own page 86-89% whether or not a
    plain GET reads it, other hosts 80% when a plain GET reads them and 65% when not, deal listings and Business Wire
    last); the direct-read state only orders pages inside the 'other' class and demotes a wire page that failed.
    Then the quote (a judge-form body sentence before a headline or a page title)."""

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
    company's newsroom is two), best source first; ties keep the given order.  `check` (the final ordering only) lets up to DIRECT_CHECKS unknown URLs be tried by a free GET."""

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
    gate accepts for the ICP stage, best source first.  When one of those pages affirms a LATER round for the company
    nothing is offered ('later' names it): a Seed / A / B sentence beside a later round is a stale stage.  A page
    that itself leads with a later round (scout.later_round_on_page) is never quoted."""

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
    '<Company> raises ...' headline.  Only such a page's date is the round's date; an About paragraph on a fresh
    product release is not."""

    try:
        path = unquote(urlsplit(str(url)).path)
    except ValueError:
        path = ""
    return names_stage(re.sub(r"[-_/.]+", " ", path) + " " + str(text or "")[:300], stage) or bool(sc.raise_title(name, path))


def cache_proof(tools: Any, name: str, prof: Mapping[str, Any], icp: Mapping[str, Any],
                urls: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
    """A stage proof read from pages already in the cache, before any stage search is paid: {'url', 'quote', 'extras',
    'date'}; {'later': ...} when the cache affirms a later round or an ownership change; None when it says nothing.
    'date' is set only for a page about the round itself (_funding_page): the caller reads it as the round's date
    (fresh round -> no stage search; the current-stage search skips older results), and the first date on any other
    page is not that."""

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
    """Cookie, consent, tracking or privacy-banner wording: page chrome, never a statement of what the company sells.
    (Our rows quoted 'these cookies support core functionalities ...' as the required attribute; the judge confirmed
    the attribute for none of them.)  A phrase whose own words are in the ICP's offering text is not chrome: a
    consent-management or advertising ICP keeps its vocabulary."""

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
    text naming that round; 'or later' admits every later one.  A criterion naming no round accepts any funding event
    (10-03: '... secured 2.5m in funding from <investor>' went out for a Series A criterion and failed 16 times)."""

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
    """The row's own intent signal (its description or URL slug) names a round the ICP stage accepts.  The judge reads
    that page anyway: such rows passed stage without any stage evidence 52 times of 57 over the published rounds (rows
    whose signal is silent: 20 of 352), and two of them were qualifiers of ours."""

    try:
        return names_stage(" ".join(f"{s.get('description') or ''} " + re.sub(r"[-_/]+", " ", str(s.get("url") or ""))
                                    for s in row.get("intent_signals") or [] if isinstance(s, Mapping)), stage)
    except Exception:
        return True


def companion_drop(rows: list[Any], icp: Mapping[str, Any]) -> tuple[list[Any], list[tuple[Any, str]]]:
    """(rows kept, [(row dropped, reason)]) at final selection.

    Only when the ICP has a STRONG row -- capability proven on the company's own domain and, on a Series A / B / C+
    ICP, stage evidence attached -- a companion whose capability is unproven goes, and on those three stage classes
    one whose stage is unproven too, unless its own intent signal names the round (_signal_round).  A Seed, Public
    or Private Equity row is never dropped for missing stage evidence alone, a strong row is never dropped, and
    without a strong row every row stays: there the floor of the
    ICP score is zero and such rows cost nothing."""

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


EVENT_GATE = _knob("event_gate", 1)
_EV_ASK = re.compile(r"\bchanges? at the (?:very )?top\b|\b(?:c suite|c level|ceo|chief executive) (?:change|transition|appointment|succession)")
_EV_TOP = re.compile(r"\bchief \w|\bc[efotrmipsd]o\b|\bciso\b|(?<!vice )\bpresident\b|\bchair")
_EV_WEAK = re.compile(
    r"(?P<results>\bfinancial results\b|\b(?:quarter|quarterly|year|annual) (?:\w+ ){0,3}?results\b|\bearnings\b)|"
    r"(?P<board>\bboard appointments?\b|\b(?:appoint\w*|elect(?:s|ed)|nam(?:es|ed)|adds?|added|nominat\w+|welcom\w+) "
    r"(?:(?!report|present|accord|respon|answer)\w+ ){0,8}?to (?:its|the|their|our) (?:\w+ )?board\b|"
    r"\bjoins? (?:\w+ ){0,3}board\b|\b(?:new|as|as a) board members?\b|"
    r"\badvisory (?:board|council)\b|\bboard of advisors\b|\b(?:non executive|independent) directors?\b)|"
    r"(?P<regional>\b(?:general manager|managing director|vice president|[se]?vp|head|gm) (?:(?:of|for|and|the|sales|north|south|east|west) ){0,4}"
    r"(?:asia|apac|apj|emea|latam|latin america|middle east|africa|europe|americas|anz|india|japan|china|uk)\b|"
    r"\bregional (?:\w+ ){0,2}?(?:sales|head|director|manager|lead|leader|officer)\b|\bcountry (?:manager|head|lead|director)\b)")
_EV_OTHER = re.compile(r"((?:\w+ ){1,4}?)(?:adds|appoints|names|elects|hires|taps|welcomes|nominates) (.+)")


def _ev_norm(value):
    return re.sub("[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def event_gate(icp, row):
    """'' or why no primary signal of the row is a change at the top.  Runs only where a LEADERSHIP_CHANGE ICP's
    required_attribute asks for one (10-05: attribute confirmed for 10 of 63 flagged rows, 160 of 177 others).  Positive
    patterns only: a board seat, a regional role or a results release with no chief / president / chair title beside
    it, or another organisation's headline that merely mentions the company."""

    if str(icp.get("intent_category")).upper() != "LEADERSHIP_CHANGE" or not _EV_ASK.search(_ev_norm(icp.get("required_attribute"))):
        return ""
    ids = [i for i in (re.sub("^the ", "", _ev_norm(row.get("company_name"))).split(" ")[0],
                       _ev_norm(_sibling("identity")._reg(row.get("company_website")).split(".")[0]).replace(" ", "")) if len(i) > 1]
    why = ""
    for sig in row.get("intent_signals") or []:
        if sig.get("matched_icp_signal"):
            continue
        parts = [_ev_norm(sig.get("description"))] + [_ev_norm(p) for p in unquote(urlsplit(str(sig.get("url"))).path).split("/")]
        text = " / ".join(parts)
        weak = not _EV_TOP.search(text) and _EV_WEAK.search(text)
        why = "event: " + weak.lastgroup if weak else ""
        for m in filter(None, map(_EV_OTHER.match, () if why else parts)):     # "<other> adds <company> CFO ... to its board"
            lead, rest = m.group(1).replace(" ", ""), m.group(2).replace(" ", "")
            if "board" not in lead and not any(i in lead or lead in i for i in ids) and any(i in rest for i in ids):
                why = "event: another organisation's release"
        if not why:
            return ""
    return why


def event_select(rows, icp, tools, report):
    """After companion_drop.  Flagged: event_gate, or a company_name that is neither a name of the homepage this run read
    (a real page, not a wall) nor anywhere in its source; an unread homepage never flags.  Beside an unflagged strong row
    flagged rows go to report.companions; without one they stay, ranked last."""

    if not EVENT_GATE or len(rows) < 2:
        return rows
    try:
        homes, keep, gone = list((tools.__dict__.get("_s22_probe") or {}).values()), [], []
        for row in rows:
            why = event_gate(icp, row)
            key, site = sm.company_name_key(row.get("company_name")), sm.registrable_host(str(row.get("company_website")))
            html = "" if why else next((str(g.get("html"))[:2000000] for g in homes
                                        if (g.get("status") or 500) < 400 and _own(g.get("final_url"), site)), "")
            if key and html and key not in _ev_norm(re.sub(r"&#?\w+;", " ", html)).replace(" ", "") and \
                    key not in map(sm.company_name_key, _sibling("identity").parse_home(html)["names"] or [key]):
                wf = _sibling("webfetch")
                if not wf.wall("", *wf.visible_text(html, 4000)[:2]):
                    why = "company_name not on the homepage read"
            (gone if why else keep).append((row, why))
        if not gone:
            return rows
        gated, needs = sm.normalize_stage(icp.get("company_stage")) in _GATED_STAGES, bool(str(icp.get("required_attribute") or "").strip())
        if not ((needs or gated) and any((not needs or (r.get("_capability") and r.get("_cap_own"))) and
                                         (not gated or r.get("company_stage_evidence")) for r, _ in keep)):
            return [r for r, _ in keep + gone]
        report.companions.extend({k: v for k, v in r.items() if not k.startswith("_")} for r, _ in gone)
        report.dropped_companies.extend((str(r.get("company_name")), f"companion drop ({why} beside a strong row)") for r, why in gone)
        return [r for r, _ in keep]
    except Exception:
        return rows


def resolve_full(drafts: list[Any], limit: int) -> bool:
    """The resolve loop's 'enough drafts' stop.  Stage-unproven drafts no longer count toward it, so a producing ICP
    keeps resolving candidates for its open slots (the loop's call reserve still ends it).  A ceiling on all drafts
    bounds the verify phase: limit + 5, and limit + 12 once two drafts are proven."""

    if len(drafts) < limit:
        return False
    proven = sum(1 for draft in drafts if not draft.get("_stage_unproven"))
    return proven >= limit or len(drafts) >= limit + (12 if proven >= 2 else 5)


__all__ = ["round_verb", "page_binds", "deal_page_refused", "quote_rank", "emit_ok", "led_label",
           "stage_quotes", "tighten", "emit_quote", "names_stage", "direct_state", "evidence_key", "order_evidence", "same_site_quote", "rank_claims", "company_pages",
           "later_round", "cache_scan", "cache_proof", "consent_text", "capability_state",
           "funding_label_ok", "companion_drop", "resolve_full"]
