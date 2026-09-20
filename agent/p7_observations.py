"""P7: Observe an own-site US address; conflicting LinkedIn slugs omit hints only."""
from __future__ import annotations
import re
from agent import v92_common as v9
from agent.evidence import _bucket_for, _clean, _parse_date, _US_STATE_RE
def homepage_country(page, country):
    """Recognize own-site address/about text, never customer geography alone."""
    if country.lower() != 'united states':
        return None, ''
    text = _clean(page.get('text'))
    for match in _US_STATE_RE.finditer(text):
        before = text[max(0, match.start()-110):match.start()]
        after = text[match.end():match.end()+25]
        hq = re.search(r'\b(?:headquartered|headquarters|based|located|our office|address|contact us)\b[^.!?]{0,90}$', before, re.I)
        address = re.search(r'\b\d{1,6}\s+[^.!?]{1,85}(?:street|st\.?|avenue|ave\.?|road|rd\.?|suite|floor|way|drive|dr\.?)\b[^.!?]*$', before, re.I)
        postcode = re.match(r'\s+\d{5}(?:-\d{4})?\b', after)
        if hq or (address and postcode):
            return True, match.group(1) or match.group(2)
    return None, ''



from agent.evidence import _linkedin_identity_ok

def linkedin_conflict(cand, linkedin):
    conflict = not _linkedin_identity_ok(cand['company_name'], cand['domain'], linkedin)
    if conflict and v9.enabled('V92_SMALL'):
        cand['_omit_linkedin_hint'] = True
        return False
    return conflict
