"""P1: Interpret observed profile bins without inventing exact counts."""
from __future__ import annotations
import re
from agent import v92_common as v9
from agent.evidence import _bucket_for, _clean, _parse_date, _US_STATE_RE
CEILINGS = {10, 50, 200, 500, 1000, 5000, 10001}
def exact_headcount(cand, pages):
    """Only literal company-subject counts; not customer or partner headcounts."""
    name = re.escape(cand['company_name'])
    patterns = [rf'(?<!\w){name}\s+(?:currently\s+)?(?:has|employs)\s+([\d,]+)\s+(?:employees|staff)\b']
    observed = set()
    for page in pages:
        text = _clean(page.get('text'))
        own = v9.domain(page.get('url')) == v9.domain(cand['domain'])
        rules = patterns + ([r'\b(?:we employ|we have)\s+([\d,]+)\s+employees\b',
                             r'\bour team of\s+([\d,]+)\s+employees\b'] if own else [])
        for pattern in rules:
            for m in re.finditer(pattern, text, re.I):
                value = int(m.group(1).replace(',', ''))
                if value > 0:
                    observed.add(value)
    return next(iter(observed)) if len(observed) == 1 else None


def choose_bucket(value, allowed, *, exact=None, profile=True):
    original = _bucket_for(value)
    if not v9.enabled('V92_BUCKET_PAIR'):
        return original
    if exact is not None:
        return _bucket_for(exact)
    if not v9.enabled('V92_BUCKET_PAIR') or not profile:
        return original
    try:
        number = float(str(value).replace(',', ''))
    except (ValueError, TypeError):
        return original
    pair = [original]
    if number in CEILINGS and number != 10001:
        pair.append(_bucket_for(int(number) + 1))
    return next((b for b in pair if b and b in allowed), original)
