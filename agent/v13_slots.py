"""Fill verified company slots before optional independent corroboration."""
import json
import os
from agent.safe_urls import urlsplit
from agent import v92_common as v9, p2_event
from agent.evidence import _clean, _snippet_on_page

SOURCE_WEIGHTS = {'linkedin':1.0,'job_board':1.0,'github':1.0,'news':.9,'company_website':.85}


def goal(icp):
    requested = icp.get('max_companies') or os.environ.get('LAB_ARENA_COMPANY_LIMIT') or os.environ.get('BAKEOFF_MAX_COMPANIES') or 5
    count=max(1,min(5,int(requested)))
    host=os.environ.get('LAB_ARENA_COMPANY_LIMIT')
    return min(count,max(1,int(host))) if host else count


def evidence_source(url, *, company_website):
    """Exact public competition adapter classification (MIT platform source)."""
    hostname = (urlsplit(str(url)).hostname or '').lower().removeprefix('www.')
    company_hostname = (urlsplit(str(company_website)).hostname or '').lower().removeprefix('www.')
    path = (urlsplit(str(url)).path or '').lower()
    if hostname == 'linkedin.com' or hostname.endswith('.linkedin.com'):
        return 'linkedin'
    if hostname == 'github.com' or hostname.endswith('.github.com'):
        return 'github'
    if any(marker in path for marker in ('/jobs', '/job/', '/careers')):
        return 'job_board'
    if company_hostname and (hostname == company_hostname or hostname.endswith('.'+company_hostname)):
        return 'company_website'
    return 'news'


def weight(url, company_website):
    return SOURCE_WEIGHTS[evidence_source(url,company_website=company_website)]


def ordered_pages(rows, company_website):
    return sorted(rows,key=lambda r:-weight(r['url'],company_website))


def scorer_domain(url):
    # The current competition scorer still uses last-two-label dedup, unlike
    # its identity helper's PSL-aware domain. Respect both to avoid zero-score
    # duplicates; never call two subdomains independent.
    return '.'.join((urlsplit(url).hostname or '').lower().removeprefix('www.').split('.')[-2:])


def independent(left,right):
    a,b=v9.domain(left),v9.domain(right)
    return bool(a and b and a!=b and scorer_domain(left)!=scorer_domain(right))


async def prefer_primary(run,cand,result,trace):
    """Only already-fetched, higher-weight pages can replace a verified primary.

    No provider calls. One bounded same-event verdict; a promising URL alone
    never changes the submitted signal. The original remains on a valid no.
    """
    if not v9.enabled('V13_SOURCE_WEIGHT') or run.remaining()<15:
        return result
    primary=result['intent_signals'][0]
    website=result['company_website']
    current=weight(primary['url'],website)
    pages=[]
    for page in run.page_cache.values():
        url=page.get('url','')
        if not url or weight(url,website)<=current or not v9.signal_url_ok(url):continue
        date=v9.observed_date(page)
        if not date or not 0 <= (run.eval_date-date).days <= int(run.icp['required_intents'][0].get('max_age_days') or 365):continue
        if len(page.get('text',''))<200 or not p2_event.brand_bound(cand,page):continue
        pages.append({'url':url,'date':date.isoformat(),'text':page['text'][:1400],
                      'weight':weight(url,website)})
    pages=ordered_pages(pages,website)[:3]
    if not pages:return result
    verdict=await run.ask('Verify the SAME specific event on supplied fetched pages. Quote verbatim. JSON only.',
        f"Company: {cand['company_name']} ({cand['domain']})\nRequired: {run.icp['required_intents'][0]['signal']}\n"
        f"Verified primary: {json.dumps(primary)}\nFetched alternatives: {json.dumps(pages)}\n"
        'Only pages independently proving the SAME event qualify. A job mentioning an existing product/site does not prove its launch/opening. '
        'Among qualifying pages select the highest supplied weight, never trade proof for weight. '
        'Return {"same_event":true/false,"url":"exact supplied URL","snippet":"verbatim proof","description":"short factual event","why_now":"sales timing"}.',max_tokens=1500)
    if not isinstance(verdict,dict) or verdict.get('same_event') is not True:return result
    page=next((p for p in pages if p['url']==verdict.get('url')),None)
    quote=_snippet_on_page(str(verdict.get('snippet') or ''),page['text'] if page else '')
    if not page or not quote:return result
    replacement={**primary,'url':page['url'],'date':page['date'],'snippet':quote[:600],
                 'description':_clean(verdict.get('description') or primary['description'])[:350],
                 'why_now':_clean(verdict.get('why_now') or primary['why_now'])[:600]}
    trace('source_weight.upgrade',{'company':cand['company_name'],'from':current,'to':page['weight'],'url':page['url']})
    return {**result,'intent_signals':[replacement]+result['intent_signals'][1:]}
