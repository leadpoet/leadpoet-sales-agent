"""P5: Preserve explicit primary category and the v8 M1 query vocabulary."""
from copy import deepcopy
from agent.v92_common import WORDS, enabled
from agent.evidence import _clean
def discovery_queries(icp, deficit=False):
    category = str(icp['required_intents'][0].get('category') or '').upper()
    words = WORDS.get(category, ('announces', 'company announcement', 'business news'))
    if category == 'REGULATORY_CLEARANCE':
        regulator = {'United States': 'FDA 510k clearance', 'Australia': 'TGA approval', 'United Kingdom': 'MHRA approval', 'Canada': 'Health Canada approval'}.get(icp.get('country'), 'CE mark')
        words = (regulator, *words[:2])
    subject = _clean(icp.get('sub_industry') or icp.get('industry'))
    country = _clean(icp.get('country') or icp.get('geography'))
    chosen = words[2:] if deficit else words[:2]
    return [{'q': f'{subject} {country} {word}', 'mode': 'jobs' if category == 'HIRING' else 'news'} for word in chosen]

def discovery_input(raw):
    """M1 needs the explicit primary category even with a flat bonus list.

    Keep every intent required by the v6 schema; do not reinterpret bonuses.
    Only bind supplied primary metadata to its exact matching signal.
    """
    result = deepcopy(raw)
    if not enabled('V92_M1_NORMALIZATION'):
        return result
    if result.get('required_intents') or not isinstance(result.get('intent_signals'), list):
        return result
    primary = result.get('intent_signal_text') or result.get('intent_signal')
    categories = result.get('intent_signal_evidence_types')
    ages = result.get('intent_signal_max_age_days')
    rows = []
    for index, text in enumerate(result['intent_signals']):
        if not isinstance(text, str):
            return result  # let v6 handle any other documented shape
        category = categories[index] if isinstance(categories, list) and index < len(categories) else ''
        age = ages[index] if isinstance(ages, list) and index < len(ages) else None
        if text == primary:
            category = category or result.get('intent_category', '')
            age = age if age is not None else result.get('intent_max_age_days')
        rows.append({'signal': text, 'category': category, 'max_age_days': age})
    if rows:
        result['required_intents'] = rows
    return result
