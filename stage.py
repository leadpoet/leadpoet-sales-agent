"""Company-stage proof, as the scorer reads it, plus stage-first discovery.

Every published Arena ICP names a company_stage, and company fit passes only
when the scorer's web check observes that exact stage from a quote it accepts
(lead_scorer._decision_from_observed_stage, _stage_quote_supports_observation).
The block marked VENDORED below is copied verbatim from
qualification/scoring/lead_scorer.py (leadpoet/leadpoet @ c8b3aa5f, lines 437-842 and 4384-4403) so the harness asks the scorer's own question before it
spends a contact on a company; tests/test_stage.py pins it to the upstream copy.
"""

from __future__ import annotations

import re
from typing import Sequence

# ---- VENDORED from qualification/scoring/lead_scorer.py (do not edit) ----
# Only change: the three non-ASCII characters in its regexes are written as
# \uXXXX escapes (re reads them identically); the bundle must be ASCII.
_STAGE_PROOF_NEGATED_OR_UNCERTAIN_RE = re.compile(
    r"\b(?:not|never|no|without|unconfirmed|rumou?red|plans?|planned|"
    r"planning|proposed|future|seeks?|seeking|expects?|expected|targets?|"
    r"targeted|pending|might|could|would|will)\b(?:\W+\w+){0,6}\W*$",
    re.I,
)


_STAGE_PROOF_HISTORICAL_RE = re.compile(
    r"\b(?:formerly|previously|once)\b(?:\W+\w+){0,6}\W*$",
    re.I,
)


_STAGE_PROOF_FAILED_EVENT_RE = re.compile(
    r"^.{0,40}\b(?:not\s+(?:close|closed|complete|completed)|cancelled|"
    r"canceled|fell\s+through|superseded)\b",
    re.I,
)


_STAGE_PROOF_PROSPECTIVE_EVENT_RE = re.compile(
    r"^.{0,40}\b(?:planned|proposed|expected|discussions?|negotiations?|"
    r"talks?)\b",
    re.I,
)


_STAGE_PROOF_COMPLETED_EVENT_RE = re.compile(
    r"\b(?:raised|closed|secured|completed|received)\b",
    re.I,
)


_CALENDAR_MAY_LEFT_RE = re.compile(r"\b(?:in|on|since|during|of)\s*$", re.I)


_CALENDAR_MAY_RIGHT_RE = re.compile(
    r"^\W*(?:\d{1,2}(?:st|nd|rd|th)?(?:\W+\d{4})?|\d{4})\b",
    re.I,
)


def _has_stage_proof_uncertainty(value: str) -> bool:
    if _STAGE_PROOF_NEGATED_OR_UNCERTAIN_RE.search(value):
        return True
    for match in re.finditer(r"\bmay\b", value, re.I):
        tail = value[match.end():]
        if len(re.findall(r"\b\w+\b", tail)) > 6:
            continue
        if _CALENDAR_MAY_LEFT_RE.search(value[:match.start()]):
            continue
        if _CALENDAR_MAY_RIGHT_RE.search(tail):
            continue
        return True
    return False


def _series_stage_proof_patterns(label: str) -> tuple[re.Pattern, ...]:
    return (
        re.compile(
            rf"\b(?:raised|closed|secured|completed|announc(?:ed|ing)|received)\b"
            rf".{{0,60}}\b{label}\b",
            re.I,
        ),
        _present_tense_raise_proof_pattern(label),
        re.compile(
            rf"\bwe(?:\s+are|['\u2019]re)\s+(?:excited|thrilled)\s+to\s+"
            rf"announce\s+(?:our|an?|the)\b.{{0,60}}\b{label}\b",
            re.I,
        ),
        re.compile(
            rf"\b{label}\s+(?:(?:funding|financing)\s+)?round\s+"
            rf"(?:has\s+)?(?:just\s+)?(?:raised|closed|secured|completed)\b",
            re.I,
        ),
        re.compile(
            rf"\b{label}\s+(?:funding|financing)(?:\s+round)?\s+"
            rf"(?:that|which)\s+(?:has\s+)?(?:raised|closed|secured)\b",
            re.I,
        ),
    )


def _present_tense_raise_proof_pattern(label: str) -> re.Pattern:
    """Match affirmative funding headlines without treating every raise as funding."""

    amount = r"(?:(?:US)?[$\u00a3\u20ac]\s*)?\d[\d,.]*\s*(?:[KMB]|million|billion)"
    return re.compile(
        rf"(?:^|[.!;:\n]\s*)"
        rf"(?!(?:[^.!?;:\n]|\.(?=\d))*"
        rf"\b(?:if|whether|conditional(?:ly)?|subject\s+to)\b)"
        rf"(?!(?:[^.!;:\n]|\.(?=\d))*\?)"
        rf"[^.!?;:\n]{{1,80}}\braises\s+"
        rf"(?:(?:an?|its|the)\s+)?(?:{amount}\s+(?:in\s+)?)?"
        rf"\b{label}\b(?:\s+(?:financing|funding|round))?",
        re.I,
    )


def _series_stage_statement_patterns(label: str) -> tuple[re.Pattern, ...]:
    """Match explicit completed-round statements without inferring from nouns."""

    return (
        re.compile(
            rf"\b(?:latest|most\s+recent)\s+(?:funding\s+)?round\s+"
            rf"(?:was|is)\b.{{0,30}}\b{label}\b",
            re.I,
        ),
        re.compile(
            rf"\bsuccessful\s+raise\b.{{0,60}}\b{label}\b",
            re.I,
        ),
        re.compile(
            rf"\bemerge[ds]\s+from\s+stealth(?:\s+mode)?\s+with\b"
            rf".{{0,60}}\b{label}\s+(?:financing|funding)\b",
            re.I,
        ),
        re.compile(
            rf"\bis\s+(?:currently\s+)?(?:an?\s+)?{label}\s+company\b",
            re.I,
        ),
    )


_VENTURE_STAGE_PROOF_PATTERNS = {
    "seed": (
        re.compile(
            r"\b(?:raised|closed|secured|completed|announced|received)\b.{0,40}"
            r"\b(?:pre[- ]seed|seed)\b",
            re.I,
        ),
        _present_tense_raise_proof_pattern(r"(?:pre[- ]seed|seed)"),
    ),
    "series a": _series_stage_proof_patterns(r"series\s+a"),
    "series b": _series_stage_proof_patterns(r"series\s+b"),
    "series c+": _series_stage_proof_patterns(r"series\s+[c-z]"),
}


_VENTURE_STAGE_STATEMENT_PATTERNS = {
    "series a": _series_stage_statement_patterns(r"series\s+a"),
    "series b": _series_stage_statement_patterns(r"series\s+b"),
    "series c+": _series_stage_statement_patterns(r"series\s+[c-z]"),
}


_PUBLIC_COMPANY_ALIAS_RE = re.compile(
    r'\("[^"()\r\n]{1,80}"\s+or\s+the\s+"Company"\)\s+'
    r'(?=\((?i:nasdaq|nyse)\s*:\s*[A-Z][A-Z0-9.-]{0,9}\))'
)


_PUBLIC_STAGE_PROOF_PATTERNS = (
    re.compile(r"\bpublicly\s+traded\b", re.I),
    re.compile(r"\bpublicly\s+listed\s+(?:shares?|stock)\b", re.I),
    re.compile(
        r"(?:^|[.!?;:\n]\s*)"
        r"(?:(?:[A-Z][A-Za-z0-9&,.'\u2019+-]*|[&+])\s+){1,8}"
        r"\((?i:nasdaq|nyse)\s*:\s*[A-Z][A-Z0-9.-]{0,9}\)",
    ),
    re.compile(
        r"\b(?:shares?|stock)\b.{0,35}\b(?:listed|trad(?:e|es|ed))\s+on\b",
        re.I,
    ),
    re.compile(
        r"\blisted(?:\s+company)?\s+on\s+(?:the\s+)?(?:nasdaq|nyse|new\s+york\s+"
        r"stock\s+exchange|london\s+stock\s+exchange|lse|euronext|tsx|asx|"
        r"hkex|hong\s+kong\s+stock\s+exchange|tokyo\s+stock\s+exchange|"
        r"dubai\s+financial\s+market)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:nasdaq|nyse|lse|euronext|tsx|asx|hkex)[- ]listed\b",
        re.I,
    ),
    re.compile(r"\b(?:went|became)\s+public\b", re.I),
    re.compile(
        r"\bcompleted\s+(?:its|an?|the)\s+(?:ipo|initial\s+public\s+offering)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:ipo|initial\s+public\s+offering)\s+(?:closed|completed)\b",
        re.I,
    ),
)


_PUBLIC_EXCHANGE_TRADING_STAGE_PROOF_PATTERNS = (
    re.compile(
        r"\b(?:traded|trades)\s+on\s+(?:the\s+)?(?:nasdaq|nyse|new\s+york\s+"
        r"stock\s+exchange|london\s+stock\s+exchange|lse|euronext|tsx|asx|"
        r"hkex|hong\s+kong\s+stock\s+exchange|tokyo\s+stock\s+exchange|"
        r"dubai\s+financial\s+market)\b",
        re.I,
    ),
)


_PUBLIC_NON_EQUITY_TRADING_CONTEXT_RE = re.compile(
    r"\b(?:bonds?(?:\s+(?:issues?|securit(?:y|ies)))?|"
    r"debt(?:\s+(?:instruments?|issues?|securit(?:y|ies)))?|notes?|funds?|etfs?)\b"
    r"\s+(?:(?:is|are|was|were)\s+)?(?:currently\s+)?"
    r"(?:traded|trades)\s+on\b",
    re.I,
)


_PUBLIC_CONDITIONAL_EXCHANGE_TRADING_CONTEXT_RE = re.compile(
    r"(?:\b(?:if|unless|conditionally)\b|\bsubject\s+to\b)"
    r"[^.!?;:\n]{0,100}\b(?:traded|trades)\s+on\b|"
    r"\b(?:traded|trades)\s+on\b[^.!?;:\n]{0,100}"
    r"(?:\b(?:if|unless|conditionally)\b|\bsubject\s+to\b)",
    re.I,
)


_PUBLIC_TICKER_STAGE_PROOF_PATTERNS = (
    re.compile(
        r"\b(?i:ticker)\s*:\s*[A-Z][A-Z0-9.-]{0,9}\s*"
        r"\((?i:nasdaq|nyse)\)(?!\s*/)",
    ),
    re.compile(
        r"\b(?i:ticker)\s*/\s*(?i:isin)\s*:\s*"
        r"[A-Z][A-Z0-9.-]{0,9}\s*\((?i:nasdaq|nyse)\)\s*/\s*"
        r"[A-Z]{2}[A-Z0-9]{9}[0-9]\b",
    ),
)


_PUBLIC_NON_EQUITY_TICKER_CONTEXT_RE = re.compile(
    r"\b(?:bonds?|debt)(?:[- ]only)?\b[\s\S]{0,60}"
    r"\bticker(?:\s*/\s*isin)?\s*:",
    re.I,
)


_PRIVATE_EQUITY_LABEL = (
    r"(?:private[- ]equity|private[- ]markets)(?:\s+(?:firm|fund|sponsor|"
    r"owner|group))?"
)


_PRIVATE_EQUITY_CONTROL = (
    r"(?:acquired\s+by|owned\s+by|controlled\s+by|taken\s+private\s+by|"
    r"majority[- ]owned\s+by|controlling\s+owner|majority\s+stake|"
    r"controlling\s+stake)"
)


_PRIVATE_EQUITY_STAGE_PROOF_PATTERNS = (
    re.compile(
        rf"\b{_PRIVATE_EQUITY_CONTROL}\b.{{0,100}}\b{_PRIVATE_EQUITY_LABEL}\b",
        re.I,
    ),
    re.compile(
        rf"\b{_PRIVATE_EQUITY_LABEL}\b.{{0,100}}?\b(?:acquired|owns?|"
        r"majority[- ]owned|controls?|controlling\s+owner|took\s+.{0,30}\s+private|"
        r"majority\s+stake|controlling\s+stake)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:an?\s+)?affiliate\s+of\s+[^,;.!?\n]{1,100},\s+"
        r"(?:(?:a|an|the|leading|global|middle[- ]market)\s+){0,5}"
        rf"{_PRIVATE_EQUITY_LABEL}\s*,?\s+announced\s+(?:today\s+)?that\s+it\s+has\s+completed"
        r"\s+(?:(?:the|its|an?)\s+)?(?:previously\s+announced\s+)?acquisition\b",
        re.I,
    ),
    re.compile(
        r"(?:^|,\s+)(?:an?\s+|the\s+)?"
        rf"{_PRIVATE_EQUITY_LABEL}\b"
        r"(?:\s+focused\s+on\s+investing\s+in\s+[^,;.!?\n]{1,100})?"
        r"\s*,?\s+announced\s+(?:today\s+)?(?:an?\s+)?"
        r"majority(?:\s+growth)?\s+recapitalization\b"
        r"(?![^.!?\n]{0,100}\b(?:expected|planned|proposed|subject)\b"
        r"[^.!?\n]{0,30}\b(?:close|complete|completion|closing)\b)",
        re.I,
    ),
)


_PUBLIC_STAGE_SUPERSESSION_PATTERNS = (
    re.compile(r"\bdelisted(?:\s+from\b)?", re.I),
    re.compile(r"\b(?:taken|went|became)\s+private\b", re.I),
    re.compile(r"\b(?:ceased|stopped)\s+trading\b", re.I),
)


_ACQUIRED_STAGE_PROOF_PATTERNS = (
    re.compile(
        r"\b(?:was|has\s+been)\s+(?:fully\s+|wholly\s+)?acquired\s+by\b",
        re.I,
    ),
    re.compile(
        r"\bis\s+(?:now\s+)?(?:an?\s+)?[^,.;!?\n]{1,80}\s+company\s*,\s*"
        r"acquired\s+by\b",
        re.I,
    ),
    re.compile(
        r"\bis\s+(?:now\s+)?(?:an?\s+)?(?:wholly[- ]owned\s+|"
        r"majority[- ]owned\s+)?subsidiary\s+of\b",
        re.I,
    ),
)


_PRIVATE_EQUITY_STAGE_SUPERSESSION_PATTERNS = (
    re.compile(
        r"\b(?:was|were|has\s+been)\s+"
        r"(?:later\s+|subsequently\s+)?sold\s+to\b",
        re.I,
    ),
    re.compile(
        r"\b(?:sold|divested)\s+(?:its|the)\s+(?:majority|controlling)\s+"
        r"(?:stake|interest|ownership)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:exited|sold|divested)\s+(?:its|the)\s+"
        r"(?:investment|ownership)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:relinquished|transferred)\s+(?:its|the)?\s*control\b",
        re.I,
    ),
)


_STAGE_SUPERSESSION_FUTURE_RE = re.compile(
    r"\bwill\b(?:\W+\w+){0,6}\W*$",
    re.I,
)


def _has_affirmed_stage_proof(
    text: str,
    patterns: Sequence[re.Pattern],
    *,
    reject_historical: bool = False,
    reject_minority: bool = False,
    supersession_patterns: Sequence[re.Pattern] = (),
    reject_future_will: bool = False,
) -> bool:
    """Reject negated, historical, prospective, and failed stage mentions."""

    for pattern in patterns:
        for match in pattern.finditer(text):
            prefix = re.split(
                r"[.!?;:\n]|\bbut\b|\bhowever\b",
                text[max(0, match.start() - 100):match.start()],
                flags=re.I,
            )[-1]
            suffix = text[match.end():match.end() + 60]
            suffix_clause = re.split(r"[.!?;:\n]", suffix, maxsplit=1)[0]
            proof_suffix_clause = (
                re.split(
                    r",|\b(?:and|but|while|although|however)\b",
                    suffix_clause,
                    maxsplit=1,
                    flags=re.I,
                )[0]
                if supersession_patterns
                else suffix_clause
            )
            context = text[max(0, match.start() - 40):match.end() + 60]
            if (
                _has_stage_proof_uncertainty(prefix)
                or _has_stage_proof_uncertainty(match.group(0))
                or (
                    reject_future_will
                    and _STAGE_SUPERSESSION_FUTURE_RE.search(prefix)
                )
            ):
                continue
            # A completed, previously announced acquisition is current proof;
            # "previously" describes its announcement, not former ownership.
            historical_proof = re.sub(
                r"\b(completed\s+(?:(?:the|its|an?)\s+)?)previously\s+announced\s+(acquisition)\b",
                r"\1\2",
                match.group(0),
                flags=re.I,
            )
            if reject_historical and (
                _STAGE_PROOF_HISTORICAL_RE.search(prefix)
                or _STAGE_PROOF_HISTORICAL_RE.search(historical_proof)
            ):
                continue
            if _STAGE_PROOF_FAILED_EVENT_RE.search(suffix):
                continue
            match_names_completed_event = bool(
                _STAGE_PROOF_COMPLETED_EVENT_RE.match(match.group(0))
            )
            if (
                not match_names_completed_event
                and (
                    _STAGE_PROOF_PROSPECTIVE_EVENT_RE.search(
                        proof_suffix_clause
                    )
                    or _has_stage_proof_uncertainty(proof_suffix_clause)
                )
            ):
                continue
            if reject_minority and "minority" in context.casefold():
                continue
            if supersession_patterns and _has_affirmed_stage_proof(
                text[match.end():],
                supersession_patterns,
                reject_future_will=True,
            ):
                continue
            return True
    return False


def _stage_quote_supports_observation(observed: str, quote: str) -> bool:
    """Sanity-check that a quote names evidence specific to the reported stage.

    This guard rejects bare category keywords and obvious uncertainty. It does
    not replace the web verifier's independent company-attribution check.
    """

    text = str(quote or "").strip()
    if not text:
        return False
    public = _has_affirmed_stage_proof(
        _PUBLIC_COMPANY_ALIAS_RE.sub("", text),
        _PUBLIC_STAGE_PROOF_PATTERNS,
        reject_historical=True,
        supersession_patterns=_PUBLIC_STAGE_SUPERSESSION_PATTERNS,
    )
    if (
        not public
        and not _PUBLIC_NON_EQUITY_TRADING_CONTEXT_RE.search(text)
        and not _PUBLIC_CONDITIONAL_EXCHANGE_TRADING_CONTEXT_RE.search(text)
    ):
        public = _has_affirmed_stage_proof(
            text,
            _PUBLIC_EXCHANGE_TRADING_STAGE_PROOF_PATTERNS,
            reject_historical=True,
            supersession_patterns=_PUBLIC_STAGE_SUPERSESSION_PATTERNS,
        )
    if not public and not _PUBLIC_NON_EQUITY_TICKER_CONTEXT_RE.search(text):
        public = _has_affirmed_stage_proof(
            text,
            _PUBLIC_TICKER_STAGE_PROOF_PATTERNS,
            reject_historical=True,
            supersession_patterns=_PUBLIC_STAGE_SUPERSESSION_PATTERNS,
        )
    private_equity = _has_affirmed_stage_proof(
        text,
        _PRIVATE_EQUITY_STAGE_PROOF_PATTERNS,
        reject_historical=True,
        reject_minority=True,
        supersession_patterns=_PRIVATE_EQUITY_STAGE_SUPERSESSION_PATTERNS,
    )
    acquired = not private_equity and _has_affirmed_stage_proof(
        text,
        _ACQUIRED_STAGE_PROOF_PATTERNS,
        reject_historical=True,
    )
    proven_ownership_states = [
        state
        for state, proven in (
            ("public", public),
            ("private equity", private_equity),
            ("acquired", acquired),
        )
        if proven
    ]
    if len(proven_ownership_states) > 1:
        return False
    if proven_ownership_states:
        return observed == proven_ownership_states[0]

    proven_venture_stages = [
        stage
        for stage, patterns in _VENTURE_STAGE_PROOF_PATTERNS.items()
        if _has_affirmed_stage_proof(text, patterns)
        or _has_affirmed_stage_proof(
            text,
            _VENTURE_STAGE_STATEMENT_PATTERNS.get(stage, ()),
            reject_historical=True,
        )
    ]
    if not proven_venture_stages:
        return False
    latest = max(
        proven_venture_stages,
        key=("seed", "series a", "series b", "series c+").index,
    )
    observed_category = (
        "series c+" if observed in _SERIES_C_PLUS_MATCHING_STAGES else observed
    )
    if re.search(
        rf"\bformerly\s+(?:an?\s+)?{re.escape(observed_category)}\b",
        text,
        re.I,
    ):
        return False
    return observed_category == latest


def _normalize_company_stage(value) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"any", "all", "unknown", "n/a", "na", "not specified"}:
        return ""
    if re.fullmatch(r"series\s*c\s*\+", text):
        return "series c+"
    text = re.sub(r"[^a-z0-9]+", " ", text)
    normalized = " ".join(text.split())
    if normalized in _SERIES_C_PLUS_MATCHING_STAGES:
        return "series c+"
    if normalized in {
        "private equity",
        "private equity backed",
        "pe backed",
    }:
        return "private equity"
    return normalized


_SERIES_C_PLUS_MATCHING_STAGES = frozenset(
    {"series c+", "series c", "series d", "series e", "series f", "series g", "series h"}
)
# ---- end VENDORED ----


# ---- our own code below ----------------------------------------------------

# What to search for, per canonical ICP stage: a query phrase and one literal
# the page must contain (exa.search includeText). Series C+ also accepts D..H,
# but includeText takes literals, so it asks for the commonest, Series C.
_SEARCH_TERMS = {
    # Seed was missing entirely until 2026-09-21, so search_terms("seed")
    # returned ("", ""), stage_discover returned [] and every Seed ICP was a
    # structural zero while STAGE_GATE still demanded proof. The scorer has
    # accepted seed all along (lead_scorer's seed proof patterns, which also
    # take pre-seed), and the 2026-09-21 bank carries two Seed ICPs.
    "seed": ("announces seed funding round", "seed"),
    "seed alt": ("raises pre-seed round", "pre-seed"),
    "series a": ("announces Series A funding round", "Series A"),
    "series b": ("announces Series B funding round", "Series B"),
    "series c+": ("announces Series C funding round", "Series C"),
    "public": ("publicly traded company listed on Nasdaq or NYSE", ""),
    # Two shapes, because the scorer accepts two: a completed acquisition by a
    # PE firm, and -- since 2026-09-20 -- an announced "majority growth
    # recapitalization" (lead_scorer._PRIVATE_EQUITY_STAGE_PROOF_PATTERNS).
    # Measured 2026-09-20: searching only the first returned 49 hits for the
    # published managed-IT ICP and proved nothing.
    "private equity": ("acquired by private equity firm majority owner",
                       "private equity"),
    "private equity alt": ("announces majority growth recapitalization "
                           "private equity firm", "recapitalization"),
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_FUNDING_VERB = re.compile(
    r"^(?P<name>.{2,80}?)\s+(?:announces|raises|secures|closes|lands|completes|"
    r"has\s+raised|raised|receives|nabs|bags)\b", re.I)


def icp_stage(value) -> str:
    """The ICP's stage in the scorer's canonical form, or "" for none/any."""
    return _normalize_company_stage(value)


# One stage, more than one way to prove it. search_terms returns the first;
# alternate_terms returns the next, for the second search.
_ALTERNATES = {"private equity": "private equity alt", "seed": "seed alt"}


def alternate_terms(stage: str) -> tuple:
    """A second query shape for a stage the first shape rarely proves."""
    key = _ALTERNATES.get(_normalize_company_stage(stage) or "")
    return _SEARCH_TERMS.get(key or "", ())


def search_terms(stage: str) -> tuple:
    """(query phrase, required literal) for a canonical stage, or ("", "")."""
    return _SEARCH_TERMS.get(stage, ("", ""))


def stage_quote(text: str, stage: str, window: int = 8000) -> str:
    """The first sentence the scorer would accept as proof of `stage`, or "".

    A sentence alone is not enough: the scorer takes the LATEST completed round
    it can prove, so a page that also proves a later round (a Series C after
    the Series B) proves the later one. The whole page window must therefore
    support the stage too.
    """
    body = str(text or "")[:window]
    if not stage or not body.strip() or not _stage_quote_supports_observation(stage, body):
        return ""
    for sentence in _SENTENCE_SPLIT.split(body):
        sentence = sentence.strip()
        if 20 <= len(sentence) <= 600 and _stage_quote_supports_observation(stage, sentence):
            return sentence
    return ""


# A Public-stage hit is not a funding headline: measured 2026-09-20 on the
# published management-consulting ICP, the proof came back as stock-quote
# pages titled "Icf International Stock Price Today (NASDAQ: ICFI) Quote,
# Market Cap, Chart" and "Korn Ferry (NYSE:KFY)". Read blindly, the whole
# title became the company name and the quote site became its domain.
_EXCHANGES = r"NASDAQ|NYSE(?:\s+American)?|AMEX|NYSEAMERICAN|OTCQB|OTCQX|OTC|LSE|TSX|TSXV|ASX|CBOE"
_TICKER_HEAD = re.compile(
    r"^(?P<name>.{2,80}?)\s*[\(\[:|-]*\s*(?:%s)\s*[:.]\s*[A-Z.]{1,6}\b" % _EXCHANGES,
    re.I)
_QUOTE_PAGE_HEAD = re.compile(
    r"^(?P<name>.{2,80}?)\s+(?:stock|share)\s+(?:price|quote)\b", re.I)


_QUOTE_PAGE_TAIL = re.compile(
    r"\s*(?:\([^)]{1,20}\))?\s*(?:stock|share)?\s*"
    r"(?:price|quote)?\s*(?:today|now)?\s*$", re.I)


def name_from_listing(title: str) -> str:
    """The company a stock-quote page is about ("Acme (NASDAQ: ACME)" -> "Acme").

    The page furniture comes in either order -- before the ticker ("Icf
    International Stock Price Today (NASDAQ: ICFI)") or after it ("Huron
    Consulting Group Inc. (HURN) Stock Price Today") -- so whichever pattern
    matches, the tail is trimmed afterwards.
    """
    text = " ".join(str(title or "").split())
    for pattern in (_TICKER_HEAD, _QUOTE_PAGE_HEAD):
        match = pattern.match(text)
        if not match:
            continue
        name = _QUOTE_PAGE_TAIL.sub("", match.group("name")).strip(" -|:,([")
        if len(name) >= 2:
            return name
    return ""


def name_from_announcement(title: str) -> str:
    """The company a funding headline is about ("Acme Raises $30M ..." -> "Acme")."""
    match = _FUNDING_VERB.match(" ".join(str(title or "").split()))
    return match.group("name").strip(" -|:") if match else ""
