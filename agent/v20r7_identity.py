"""One company search, at most one homepage fetch; keep existing binding rules."""
import re
from agent import v17_domains as old,v15_budget
from agent.evidence import _linkedin_identity_ok
from agent.v92_common import domain


def search_bound(row,cand):
    bound=old.bind(row,cand)
    if not bound:return None
    text=str(row.get('text',''))+' '+str(row.get('links',[]))+' '+str(row.get('linkedin_url',''))
    links=re.findall(r'https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9_-]+',text)
    return bound if any(_linkedin_identity_ok(cand['company_name'],bound[2],url) for url in links) else None


async def resolve(run,cand,rows,trace):
    from agent.v12_llm import enabled
    if enabled('V20R9_IDENTITY'):
        from agent.v20r9_identity import resolve as observed
        return await observed(run,cand,rows,trace)
    key=old.key(cand);cache=getattr(run,'v20r7_identities',None)
    if cache is None:cache=run.v20r7_identities={}
    if key in cache:return cache[key]
    for row in rows:
        if domain(row.get('url'))==domain(cand.get('domain')):
            bound=old.bind(row,cand)
            if bound:cache[key]=bound;return bound
    with v15_budget.work('v18_identity',key,primary=True):
        found=await run.tool('exa_company_search',{'query':cand['company_name'],'numResults':3})
    proposed=''
    for row in found.get('results',[]):
        bound=search_bound(row,cand)
        if bound:
            run.page_cache[row['url']]=row;cache[key]=bound
            trace('identity.bound',{'company':cand['company_name'],'domain':bound[2],'source':'company_search_bound_homepage','home_calls':0,'physical_calls':1})
            return bound
        if not proposed and old.eligible(domain(row.get('url'))):proposed=domain(row['url'])
    host=proposed or old.hint(cand['company_name'],cand.get('domain'),rows)[0]
    bound=None
    if old.eligible(host):
        with v15_budget.work('v18_identity',key,primary=True):
            page=await run.tool('firecrawl_scrape',{'url':'https://'+host+'/','fresh':True})
        if isinstance(page,dict) and page.get('url'):
            run.page_cache[page['url']]=page;bound=old.bind(page,cand)
    cache[key]=bound
    if bound:trace('identity.bound',{'company':cand['company_name'],'domain':bound[2],'source':'company_search_then_homepage','home_calls':1,'physical_calls':2})
    return bound
