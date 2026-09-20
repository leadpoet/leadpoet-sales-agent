"""Category-first confirmation and bounded recovery from stage-only evidence."""
import asyncio
import copy
import re
from datetime import timedelta
from agent import v12_llm, v15_budget, v20r5_primary as intent
from agent.deadline import BudgetExhausted

EVENT_CAP = 5
IDENTITY_RESERVE = 5
WORDS = {'PRODUCT_LAUNCH': 'new product care service capability',
         'HIRING': 'hiring jobs recruiting',
         'REGULATORY_CLEARANCE': 'regulatory approval clearance FDA',
         'EXPANSION': 'expansion new market office'}


def plausibility(cand, rows):
    value = (cand.get('_verdict') or {}).get('venture_plausible')
    if value is False:
        return False, 'phase_a_venture_plausible_false'
    name = r'(?<!\w)' + re.escape(cand['company_name']) + r'(?!\w)'
    for row in rows:
        text = str(row.get('title', '')) + ' ' + str(row.get('text', ''))
        if not re.search(name, text, re.I):
            continue
        if re.search(r'\b(?:NYSE|Nasdaq|publicly traded|parent conglomerate)\b', text, re.I):
            return False, 'cached_public_or_parent_conglomerate'
        # Company-local type/retail wording, not a remembered company blacklist.
        for match in re.finditer(name, text, re.I):
            nearby = text[max(0, match.start()-80):match.end()+160]
            excluded=(r'\b(?:large retail chain|retail chain|public brand|chain of stores|big tech|government agency|university|stores nationwide)\b'
                      if v12_llm.enabled('V20R9_QUERIES') else r'\b(?:retailer|beauty retail|grocery retailer|womenswear brand|consumer brand|big tech|government agency|university|stores nationwide|in[- ]store pilot|body care collection)\b')
            if re.search(excluded,nearby,re.I):
                return False, 'cached_nonventure_company_type'
            if not v12_llm.enabled('V20R9_QUERIES') and re.search(name+r'\s+(?:stores|Shop)\b', nearby, re.I):
                return False, 'cached_retail_stores_or_marketplace'
    return (True, 'phase_a_venture_plausible_true') if value is True else ('unknown', 'unobserved')


async def event_lookup(run, cand, raw_icp, trace):
    from agent import v15_pipeline as p, v20_phases
    from agent.evidence import _parse_date
    key = p.v17_domains.key(cand)
    attempted = getattr(run, 'v20r6_events', None)
    if attempted is None:
        attempted = run.v20r6_events = set()
    def missing(reason):
        trace('candidate.loss', {'company': cand['company_name'], 'stage': 'category_event',
              'reason': 'category_primary_missing', 'lookup_status': reason})
        return None
    cap=4 if v12_llm.enabled('V20R7_ALLOCATION') else EVENT_CAP
    reserve=13 if v12_llm.enabled('V20R7_ALLOCATION') else IDENTITY_RESERVE
    if key in attempted or len(attempted) >= cap:
        return missing('event_lookup_cap')
    if run.used.get('deepline', 0) >= 29-reserve or run.remaining() < 12:
        return missing('confirmation_reserve')
    stage = cand.get('_phase_a', {}).get('stage_fact') or {}
    if not p.v19_triage.affirmed(cand, stage):
        return missing('stage_not_affirmed')
    attempted.add(key)
    category = intent.category(run.icp)
    words = WORDS.get(category, run.icp['required_intents'][0]['signal'])
    query = f'"{cand["company_name"].replace(chr(34), "")}" (launches OR unveils OR introduces OR announces) {words} {run.eval_date.year}'
    if v12_llm.enabled('V20R9_QUERIES'):
        from agent.v20r9_queries import event_query
        query=event_query(cand,run.icp,run.eval_date.year)
    days = min(365, int(run.icp['required_intents'][0].get('max_age_days') or 365))
    trace('category.event_lookup', {'company': cand['company_name'], 'query': query,
          'category': category, 'attempt': len(attempted), 'cap': cap, 'before_identity': True})
    try:
        with v15_budget.work('v20r6_event', key, primary=True):
            async with asyncio.timeout(min(20,run.remaining()-3)):
                result = await run.tool('exa_search', {'query': query, 'numResults': 5,
                    'startPublishedDate': (run.eval_date-timedelta(days=days)).isoformat(),
                    'endPublishedDate': run.eval_date.isoformat(), 'contents': {'text': {'maxCharacters': 2000}}})
        cache = {r['url']: r for r in p.cached_rows(run)}
        rows = [dict(cache.get(r['url'], r)) for r in result.get('results', []) if r.get('url')]
        if not rows:
            return missing('empty')
        with v15_budget.work('phase_a', no_tools=True):
            verdict = await p.assess(run, cand, rows, excerpt_limit=2000)
        if not isinstance(verdict, dict) or verdict.get('primary_status') != 'match':
            return missing('category_not_matched')
        primary = p.grounded_facts(rows, verdict).get('primary')
        if not primary:
            return missing('quote_not_grounded')
        when = _parse_date(primary.get('date'))
        if not when or not 0 <= (run.eval_date-when).days <= days:
            return missing('date_outside_window')
        signal = {'matched_icp_signal': 0, 'snippet': primary['quote'][:600],
                  'description': primary['quote'][:350], 'url': primary['page']['url'], 'date': when.isoformat()}
        if not intent.output({'company_name': cand['company_name'], 'company_website':cand.get('domain',''),
                              'intent_signals': [signal]}, run.icp, trace,primary=primary):
            return missing('r5_guard')
        recovered = copy.deepcopy(cand)
        old_rows = recovered.get('_verdict_rows') or []
        recovered['_verdict_rows'] = old_rows + rows
        recovered['_verdict'].update(primary_status='match', primary={**verdict['primary'], 'row': len(old_rows)+verdict['primary']['row']})
        decision = v20_phases.cache_decision(run, recovered, raw_icp)
        if not decision['primary_from_cache'] or decision['drop_reason']:
            return missing(decision['drop_reason'] or 'event_unobserved')
        recovered['_phase_a'] = decision
        trace('category.event_admitted', {'company': cand['company_name'], 'quote': primary['quote'],
              'url': primary['page']['url'], 'date': when.isoformat(), 'origin': 'category_event_lookup'})
        return recovered
    except (BudgetExhausted, v12_llm.LLMTruncated, TimeoutError):
        return missing('budget_or_incomplete_verdict')
