"""P3: One independently published, page-grounded account of the same event."""
import json
import re
from agent.v92_common import domain, signal_url_ok, observed_date, enabled, WIRES
from agent.evidence import _norm, _clean, _snippet_on_page
async def second_signal(run, cand, primary):
    """At most two provider calls, then one same-event verification LLM call."""
    if not enabled('V92_SECOND_SIGNAL') or run.remaining() < 25:
        return None
    own, first = domain(cand['domain']), domain(primary['url'])
    intent = run.icp['required_intents'][0]
    query = f"{cand['company_name']} {primary['description'][:150]}"
    rows = []
    if first != own:
        links = [x for x in cand.get('_home', {}).get('links', []) if domain(x['url']) == own
                 and re.search(r'news|press|newsroom|blog|announcements', x['url'] + ' ' + x['text'], re.I)]
        if links:
            hub = await run.tool('fetch_page', {'url': links[0]['url'], 'max_chars': 4000})
            rows = [{'url': x['url'], 'text': x['text']} for x in hub.get('links', [])
                    if domain(x['url']) == own and signal_url_ok(x['url'])]
            if observed_date(hub):
                rows.insert(0, hub)
        else:
            found = await run.tool('exa_search', {'query': query, 'includeDomains': [own], 'numResults': 5,
                                                'contents': {'text': {'maxCharacters': 1000}}})
            rows = found.get('results', [])
    else:
        found = await run.tool('search_web', {'query': query + ' (' + ' OR '.join('site:' + d for d in WIRES) + ')',
                                           'mode': 'news', 'limit': 5, 'recency_days': int(intent.get('max_age_days') or 365)})
        rows = found.get('results', [])
    rows = [r for r in rows if signal_url_ok(r.get('url', '')) and domain(r['url']) != first
            and (domain(r['url']) == own if first != own else domain(r['url']) in WIRES)]
    if not rows:
        return None
    # Pick likely same-event pages deterministically before the second call.
    words = {w for w in _norm(primary['description'] + ' ' + primary['snippet']).split() if len(w) > 3}
    rows.sort(key=lambda r: len(words.intersection(_norm(str(r)).split())), reverse=True)
    fetched = await run.tool('exa_contents', {'urls': list(dict.fromkeys(r['url'] for r in rows))[:3], 'max_chars': 4500})
    for page in fetched.get('results', []):
        if page.get('url') and page.get('text'):
            run.page_cache.setdefault(page['url'], page)
    pages = [r for r in fetched.get('results', []) if signal_url_ok(r.get('url', '')) and domain(r['url']) != first
             and (domain(r['url']) == own if first != own else domain(r['url']) in WIRES)
             and observed_date(r) and 0 <= (run.eval_date - observed_date(r)).days <= int(intent.get('max_age_days') or 365)]
    if not pages:
        return None
    verdict = await run.ask('Verify the SAME specific event, not merely the same category. Quote supplied page text verbatim. JSON only.',
        f"Company: {cand['company_name']} ({own})\nPrimary: {json.dumps(primary)}\nFetched pages: {json.dumps([{k:(v[:1400] if k == 'text' else v) for k,v in p.items() if k in ('url','title','text','date','datePublished')} for p in pages])}\n"
        'Require the same product/round/appointment/transaction/facility and subject company. A different event in the same category is NOT a match. '
        'Return {"same_event":true/false,"url":"exact supplied URL","snippet":"verbatim sentence proving this event","description":"short factual description","why_now":"sales timing"}.', max_tokens=650)
    if not isinstance(verdict, dict) or verdict.get('same_event') is not True:
        return None
    page = next((r for r in pages if r['url'] == verdict.get('url')), None)
    quote = _snippet_on_page(str(verdict.get('snippet') or ''), page.get('text', '') if page else '')
    if not page or not quote:
        return None
    return {'matched_icp_signal': 0, 'url': page['url'], 'date': observed_date(page).isoformat(),
            'snippet': quote[:600], 'description': _clean(verdict.get('description') or primary['description'])[:350],
            'why_now': _clean(verdict.get('why_now') or primary['why_now'])[:600]}
