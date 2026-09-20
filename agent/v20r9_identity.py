"""Bound plain-HTTP identity observations within two paid calls per company."""
from agent.safe_urls import urljoin
from agent import v17_domains as old,v15_budget,v21_size,v12_llm
from agent.v92_common import domain

def record(run,cand,page,requested,trace):
    records=getattr(run,'v20r9_identity',None)
    if records is None:records=run.v20r9_identity={}
    status=page.get('plain_status');final=domain(page.get('final_url') or page.get('url'))
    anchor=bool(v21_size.homepage_anchor(page,page.get('url','')))
    value={'company':cand['company_name'],'requested_domain':domain(requested),'final_registrable_domain':final,
           'plain_fetch_status':status,'linkedin_anchor':anchor,'identity_risk':status in (403,429) or isinstance(status,int) and status>=500,
           'redirected':bool(final and final!=domain(requested)), 'plain_status_source':'generic_http_request' if status else 'unobserved'}
    if v12_llm.enabled('V23_IDENTITY'):
        value['final_url_observed'] = page.get('final_url_observed', True)
        value['identity_risk'] = status != 200 or not final or final != domain(requested) or not value['final_url_observed']
        value['demotion_reason'] = ('plain_status_not_200' if status != 200 else
                                     'final_url_unobserved' if not value['final_url_observed'] else
                                     'registrable_domain_changed' if final != domain(requested) else '')
    records[old.key(cand)]=value
    if final:records[final]=value
    trace('identity.http_observation',value)
    return value

def rank(run,company):
    r=getattr(run,'v20r9_identity',{}).get(domain(company.get('company_website')), {})
    status=r.get('plain_fetch_status')
    if v12_llm.enabled('V23_IDENTITY'):
        clean = (status == 200 and r.get('final_url_observed', True) and bool(r.get('requested_domain')) and
                 r.get('final_registrable_domain') == r.get('requested_domain'))
        return (1 if clean else -1, int(clean and bool(r.get('linkedin_anchor'))))
    # Every observed clean candidate precedes a 403/5xx candidate, regardless
    # of other provability scores. Unknown is never mislabeled as HTTP 200.
    return (-1 if r.get('identity_risk') else 1 if status==200 else 0,
            2 if status==200 and r.get('linkedin_anchor') else -2 if r.get('identity_risk') else 0)

async def resolve(run,cand,rows,trace):
    key=old.key(cand);host,_=old.hint(cand['company_name'],cand.get('domain'),rows)
    calls=0;search_rows=[]
    async def call(name,args):
        nonlocal calls
        if calls>=2:return {'error':'identity two-call cap'}
        calls+=1
        with v15_budget.work('v18_identity',key,primary=True):return await run.tool(name,args)
    if not host:
        found=await call('exa_company_search',{'query':cand['company_name'],'numResults':3})
        search_rows=found.get('results') or []
        host=next((domain(r.get('url')) for r in search_rows if old.eligible(domain(r.get('url')))), '')
    if not old.eligible(host):return None
    requested='https://'+host+'/'
    page=await call('plain_homepage',{'url':requested})
    observation=record(run,cand,page,requested,trace)
    location=(page.get('headers') or {}).get('location')
    if isinstance(page.get('plain_status'),int) and 300<=page['plain_status']<400 and location:
        target=urljoin(requested,location)
        if old.eligible(domain(target)) and target.startswith('https://'):
            page=await call('plain_homepage',{'url':target})
            observation=record(run,cand,page,requested,trace)
    if page.get('plain_status')==200:
        bound=old.bind(page,cand)
        if bound:run.page_cache[page['url']]=page;return bound
    # A plain denial remains risk telemetry even if the existing Firecrawl
    # path supplies usable identity content. This is not a plain-fetch 200.
    if calls<2:
        final=observation['final_registrable_domain'] or host
        fallback=await call('firecrawl_scrape',{'url':'https://'+final+'/','fresh':True})
        actual=domain(fallback.get('final_url') or fallback.get('url'))
        if actual and actual!=domain(fallback.get('url')):
            # Metadata alone cannot prove the newly named homepage body.
            return None
        bound=old.bind(fallback,cand)
        if bound:
            run.page_cache[fallback['url']]=fallback
            getattr(run,'v20r9_identity',{})[bound[2]]=observation
            return bound
    for row in search_rows:
        if domain(row.get('url'))==observation['final_registrable_domain']:
            bound=old.bind(row,cand)
            if bound:return bound
    return None
