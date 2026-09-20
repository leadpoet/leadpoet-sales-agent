"""Read-only upstream stage-proof predicates, d2f02967 (public scorer)."""
import re
from typing import Sequence
from agent.judge_mirror import _SERIES_C_PLUS_MATCHING_STAGES

_STAGE_PROOF_NEGATED_OR_UNCERTAIN_RE = re.compile(
    r"\b(?:not|never|no|without|unconfirmed|rumou?red|plans?|planned|"
    r"planning|proposed|future|seeks?|seeking|expects?|expected|targets?|"
    r"targeted|might|could|would|will)\b(?:\W+\w+){0,6}\W*$",
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
            rf"\b(?:raised|closed|secured|completed|announced|received)\b"
            rf".{{0,60}}\b{label}\b",
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
    ),
    "series a": _series_stage_proof_patterns(r"series\s+a"),
    "series b": _series_stage_proof_patterns(r"series\s+b"),
    "series c+": _series_stage_proof_patterns(r"series\s+[c-z]"),
}
_PUBLIC_STAGE_PROOF_PATTERNS = (
    re.compile(r"\bpublicly\s+traded\b", re.I),
    re.compile(r"\bpublicly\s+listed\s+(?:shares?|stock)\b", re.I),
    re.compile(
        r"(?:^|[.!?;:\n]\s*)"
        r"(?:[A-Z][A-Za-z0-9&.'’+-]*\s+){1,8}"
        r"\((?i:nasdaq|nyse)\s*:\s*[A-Z][A-Z0-9.-]{0,9}\)",
    ),
    re.compile(
        r"\b(?:shares?|stock)\b.{0,35}\b(?:listed|trad(?:e|es|ed))\s+on\b",
        re.I,
    ),
    re.compile(
        r"\b(?:listed|traded)\s+on\s+(?:the\s+)?(?:nasdaq|nyse|new\s+york\s+"
        r"stock\s+exchange|london\s+stock\s+exchange|lse|euronext|tsx|asx|"
        r"hkex|hong\s+kong\s+stock\s+exchange|tokyo\s+stock\s+exchange)\b",
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
)
_PUBLIC_STAGE_SUPERSESSION_PATTERNS = (
    re.compile(r"\bdelisted(?:\s+from\b)?", re.I),
    re.compile(r"\b(?:taken|went|became)\s+private\b", re.I),
    re.compile(r"\b(?:ceased|stopped)\s+trading\b", re.I),
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
            if reject_historical and (
                _STAGE_PROOF_HISTORICAL_RE.search(prefix)
                or _STAGE_PROOF_HISTORICAL_RE.search(match.group(0))
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
        text,
        _PUBLIC_STAGE_PROOF_PATTERNS,
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
    if public and private_equity:
        return False
    if public or private_equity:
        expected = "public" if public else "private equity"
        return observed == expected

    proven_venture_stages = [
        stage
        for stage, patterns in _VENTURE_STAGE_PROOF_PATTERNS.items()
        if _has_affirmed_stage_proof(text, patterns)
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


def submit_stage_quote(observed, quote):
    """The order is stricter than upstream for historical venture wording."""
    if re.search(r'\b(?:previously|formerly|once)\b',str(quote or ''),re.I):return False
    return _stage_quote_supports_observation(str(observed or '').lower(),quote)

def announcement_source(url,company_domain):
    from agent.v92_common import domain
    return domain(url)==domain(company_domain) or domain(url) in {
        'prnewswire.com','businesswire.com','globenewswire.com','accessnewswire.com','newswire.com'}
