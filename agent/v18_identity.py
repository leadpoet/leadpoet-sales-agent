"""Follow cross-domain metadata once, then bind freshly fetched homepage text."""
from agent import v17_domains as old,v15_budget
from agent.v92_common import domain

def replacement(page,requested):
    for field in ('canonical_url','og_url','final_url','url'):
        value=domain(page.get(field))
        if value and value!=requested and old.eligible(value):return value
    return ''

async def resolve(run,cand,rows,trace):
    key=old.key(cand)
    cache=getattr(run,'v18_identities',None)
    if cache is None:run.v18_identities={};cache=run.v18_identities
    if key in cache:return cache[key]
    attempted=getattr(run,'v17_identity_attempted',None)
    if attempted is None:run.v17_identity_attempted=set();attempted=run.v17_identity_attempted
    if key not in attempted and len(attempted)>=8:return None
    attempted.add(key)
    host,_=old.hint(cand['company_name'],cand.get('domain'),rows)
    homes=0;seen=set()
    async def fetch(d):
        nonlocal homes
        if not old.eligible(d) or d in seen or homes>=2:return {}
        homes+=1;seen.add(d)
        with v15_budget.work('v18_identity',key,primary=True):
            page=await run.tool('firecrawl_scrape',{'url':'https://'+d+'/','fresh':True})
        if isinstance(page,dict) and page.get('url'):
            run.page_cache[page['url']]=page
        return page if isinstance(page,dict) else {}
    async def follow(d):
        page=await fetch(d)
        if page.get('error') or page.get('ok') is False:return None
        next_host=replacement(page,d)
        if next_host:
            trace('identity.redirect',{'company':cand['company_name'],'from':d,'to':next_host})
            page=await fetch(next_host)
            if not page or replacement(page,next_host):return None
        bound=old.bind(page,cand)
        if bound:
            trace('identity.bound',{'company':cand['company_name'],'domain':bound[2],'fresh_homepage':True,'home_calls':homes})
        return bound
    result=await follow(host) if host else None
    if result is None and homes<2:
        with v15_budget.work('v18_identity',key,primary=True):
            found=await run.tool('exa_company_search',{'query':cand['company_name'],'numResults':3})
        for row in found.get('results',[]):
            proposed=domain(row.get('url'))
            if proposed and proposed not in seen and old.eligible(proposed):
                result=await follow(proposed)
                break
    cache[key]=result
    return result
