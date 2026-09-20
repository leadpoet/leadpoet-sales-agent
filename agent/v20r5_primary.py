"""Keep the requested event separate from funding-stage evidence."""
import copy
import re


ROUND = re.compile(
    r'\b(?:raises?|raised|closes?|closed|secures?|secured)\b'
    r'.{0,240}?\b(?:series\s+[a-z](?:\+)?|(?:pre[-\s]?)?seed|funding)\b',
    re.I | re.S)


def category(icp):
    intents = icp.get('required_intents') or []
    first = intents[0] if intents and isinstance(intents[0], dict) else {}
    return str(first.get('category') or icp.get('intent_category') or '').upper().strip()


def funding_mismatch(icp, text):
    if category(icp) == 'FUNDING':
        return False
    text = text or ''
    if ROUND.search(text):
        return True
    from agent.v12_llm import enabled
    if not enabled('V23_PRIMARY'):
        return False
    # Capital-raising announcements need not explicitly name a round.
    if re.search(r"\bannounc(?:ing|ed|es?)\b.*?\b(?:rais(?:e|ed|ing)|secur(?:e|ed|ing))\s+\$", text, re.I | re.S):
        return True
    # Renaming a company is not a product event. A product noun must occur
    # in the same sentence, rather than elsewhere on the article page.
    product = re.compile(r'\b(?:products?|services?|platforms?|capabilit(?:y|ies)|programs?|tools?|features?|apps?|applications?|solutions?|agents?|software)\b', re.I)
    for sentence in re.split(r'(?<=[.!?])\s+', text):
        if product.search(sentence):
            continue
        if re.search(r'\bformerly\b|\bintroducing\s+(?:[A-Z][\w-]*)(?:\s+[A-Z][\w-]*){0,3}\b', sentence, re.I):
            return True
    return False


def instruction(icp):
    from agent.v12_llm import enabled
    from agent.v20r9_intent import instruction as literal
    from agent.v20r10_evidence import instruction as hiring
    return ((hiring(icp) if enabled("V20R10_HIRING") else "") + (literal(icp) if enabled("V20R9_INTENT") else "") + (f"Required intent category: {category(icp)}. "
            'primary_status=match requires a sentence establishing that requested event, not merely this company or its fit. '
            'For PRODUCT_LAUNCH quote the actual launched product, new care service, or major capability. '
            'A completed funding round is stage evidence only unless the requested category is FUNDING. '
            'If that event is absent, set primary_status=unknown and primary=null. '))


def freeze(cand, primary):
    # Detach both the fact and page from verdict, row-cache and stage mutations.
    cand['_phase_a_primary'] = copy.deepcopy(primary)


def output(company, icp, trace, *, primary=None, context_rows=()):
    """Fail closed; never promote a fit/stage signal into the primary slot."""
    if not company:
        return None
    signals = company.get('intent_signals') or []
    reason = ''
    if not signals:
        reason = 'category_primary_missing'
    else:
        first = signals[0]
        if first.get('matched_icp_signal') != 0:
            reason = 'category_primary_missing'
        elif funding_mismatch(icp, first.get('snippet', '') + ' ' + first.get('description', '')):
            reason = 'funding_primary_for_nonfunding'
        elif primary and (first.get('url') != primary['page']['url'] or
                          first.get('date') != primary['date'] or
                          first.get('snippet') != primary['quote'][:600] or
                          first.get('description') != primary['quote'][:350]):
            reason = 'phase_a_primary_changed'
    if reason:
        trace('candidate.loss', {'company': company.get('company_name'),
              'stage': 'primary_output_guard', 'reason': reason, 'category': category(icp)})
        return None
    from agent import v12_llm
    if v12_llm.enabled('V20R10_HIRING'):
        from agent.v20r10_evidence import hiring_ok
        page=primary.get('page',{}) if primary else next((r for r in context_rows if r.get('url')==signals[0].get('url')), {})
        page={**page,'url':signals[0].get('url')}
        if not hiring_ok(icp,signals[0].get('snippet',''),page):
            trace('candidate.loss',{'company':company.get('company_name'),'stage':'primary_output_guard','reason':'hiring_source_not_open_or_linkedin'})
            return None
    if v12_llm.enabled('V20R9_INTENT'):
        from agent.v20r9_intent import matches
        page=primary.get('page',{}) if primary else next((r for r in context_rows if r.get('url')==signals[0].get('url')), {})
        if not matches(icp, signals[0].get('snippet',''), page):
            trace('candidate.loss', {'company':company.get('company_name'),'stage':'primary_output_guard','reason':'literal_category_not_established'})
            return None
    if v12_llm.enabled('V20R8_STRICT'):
        from agent.v20r8_strict import subject
        page=primary.get('page',{}) if primary else next((r for r in context_rows if r.get('url')==signals[0].get('url')), {})
        if not subject(company, signals[0].get('snippet',''), page):
            trace('candidate.loss', {'company':company.get('company_name'),'stage':'primary_output_guard',
                                    'reason':'primary_subject_unverified','category':category(icp)})
            return None
    if v12_llm.enabled('V20R10_HIRING'):
        clean=[signals[0]]
        for signal in signals[1:]:
            index=signal.get('matched_icp_signal',0);intents=icp.get('required_intents') or []
            criterion={'required_intents':[intents[index]]} if isinstance(index,int) and 0<=index<len(intents) else icp
            page=next((r for r in context_rows if r.get('url')==signal.get('url')),{})
            if hiring_ok(criterion,signal.get('snippet',''),{**page,'url':signal.get('url')}):clean.append(signal)
        company=dict(company,intent_signals=clean);signals=clean
    # A later funding-stage headline cannot become a non-funding intent either.
    if category(icp) != 'FUNDING':
        company = dict(company, intent_signals=[signals[0]] + [s for s in signals[1:]
            if not funding_mismatch(icp, s.get('snippet', '') + ' ' + s.get('description', ''))])
    return company
