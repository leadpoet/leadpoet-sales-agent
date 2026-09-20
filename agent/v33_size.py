"""Domain-bound structured workforce observations and in-band output claims."""
import asyncio,json,re
from agent import v12_llm,v15_budget,v21_size,judge_mirror
from agent.v92_common import domain
from agent.deadline import BudgetExhausted

HOLDS={'free_simple_company_search':0,'harvestapi_get_company':3000,'exa_contents':10000}

def state(run):
    if not hasattr(run,'v33_size_records'):run.v33_size_records={}
    return run.v33_size_records

def elements(value):
    if isinstance(value,list):
        for item in value:yield from elements(item)
    elif isinstance(value,dict):
        yield value
        for key in ('data','result','results','elements','rows'):
            if isinstance(value.get(key),(dict,list)):yield from elements(value[key])

def linkedin(value):
    value=str(value or '').strip()
    if value.lower().startswith(('linkedin.com/','www.linkedin.com/')):value='https://'+value
    slug=v21_size.linkedin_slug(value)
    return 'https://www.linkedin.com/company/'+slug+'/' if slug else ''

def sql_count(value):
    # SQL counts are integers, not ranges, rounded floats or boolean sentinels.
    if type(value) is int:return value if value>0 else None
    if isinstance(value,str) and re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)',value.strip()):
        n=int(value.strip().replace(',',''));return n if n>0 else None
    return None

def structured(payload,key,url):
    slug=v21_size.linkedin_slug(url)
    for row in elements(payload):
        if domain(row.get('website'))!=key or not slug:continue
        if v21_size.linkedin_slug(linkedin(row.get('linkedinUrl') or row.get('linkedin_url')))!=slug:continue
        raw=row.get('employeeCountRange');literal=raw
        if isinstance(raw,dict):
            start,end=raw.get('start'),raw.get('end')
            if type(start) is not int or start<0 or (end is not None and (type(end) is not int or end<start)):literal=None
            else:literal=f'{start:,}-{end:,}' if end is not None else f'{start:,}+'
        band=(v21_size.bucket(literal) or None) if isinstance(literal,str) else None
        hq=next((x for x in (row.get('locations') or []) if isinstance(x,dict) and x.get('headquarter') is True),{})
        parsed=hq.get('parsed') if isinstance(hq.get('parsed'),dict) else {}
        return {'url':url,'raw_band':raw,'band':band,'hq':parsed,'headquarters':str(parsed.get('text') or '')[:300]}
    return None

def claim(run,operation):
    m=run.v15_budget;lane,key,primary,no_tools=v15_budget.WORK.get()
    with m.lock:
        if not v12_llm.enabled('V33_SIZE_PATH') or lane!='v33_size' or not primary or no_tools or operation not in HOLDS:
            raise BudgetExhausted('structured size operation not allowed')
        seen=getattr(m,'v33_claims',set());used=sum(m.lanes.values())
        # Do not consume remaining identities or the input-selected contact pairs.
        reserve=getattr(m,'r7_confirmation_reserve',0)
        if reserve:reserve+=max(0,3-getattr(m,'r7_proof_calls',0))
        ceiling=min(29-reserve,getattr(m,'v27_sourcing_ceiling',29))
        if used>=ceiling or (key,operation) in seen or m.overrun:raise BudgetExhausted('structured size allowance exhausted')
        if operation=='exa_contents':
            prior=getattr(run,'v21_size_state',{}).get('linkedin_calls',set())|getattr(run,'v32_fetched',set())
            if key in prior or len(prior)>=2:raise BudgetExhausted('two LinkedIn fallback confirmations maximum')
        ticket=m._reserve('deepline',HOLDS[operation]);seen.add((key,operation));m.v33_claims=seen;m.lanes['reserve']+=1
        if operation=='exa_contents':
            run.v32_fetched=getattr(run,'v32_fetched',set())|{key}
        m.trace('budget.claim',{'lane':lane,'company':key,'operation':operation,'hold_microusd':HOLDS[operation],'confirmation_reserved':29-ceiling})
        return ticket

async def call(run,key,name,args):
    with v15_budget.work('v33_size',key,primary=True):
        async with asyncio.timeout(min(8,max(0,run.remaining()-2))):return await run.tool(name,args)

async def profile(run,c,trace,record=None):
    key=domain(c.get('company_website') or c.get('domain'));records=state(run)
    if key in records:return records[key]
    item={'company':c['company_name'],'path':'none','url':'','raw_band':None,'band':None,'sql_resolved':False,'observations':[]}
    records[key]=item
    try:
        response=record if record is not None else await call(run,key,'get_company_profile',{'domain':key})
        row=response.get('company') or {}
        if domain(row.get('normalized_domain') or row.get('domain'))==key:
            item.update(path='profile_sql',sql_resolved=True,raw_band=row.get('employee_count'),sql=row)
            li=linkedin(row.get('linkedin_url'))
            # Bind the URL through the exact-domain SQL row, never through a guessed slug.
            slug=v21_size.linkedin_slug(li)
            item['url']='https://www.linkedin.com/company/'+slug+'/' if slug else ''
            count=sql_count(row.get('employee_count'))
            band=v21_size.bucket(count) if count is not None else ''
            if band:
                item['band']=band;item['normalized_count']=count
                item['observations'].append({'url':item['url'],'band':band,'quote':f'companies.employee_count: {count}',
                    'in_band':band in judge_mirror.employee_count_buckets_for_icp(run.icp),
                    'linkedin_confirmed':False,'priority':0.5,'names_domain':True,
                    'observation':'exact-domain companies SQL row; not a fetched LinkedIn page',
                    'from_cache':False,'page_date':''})
            if getattr(run,'v27_input',None):run.v27_profiles[key]=dict(row)
    except (Exception,asyncio.CancelledError) as exc:item['reason']=type(exc).__name__
    trace('size.path',dict(item))
    return item

async def enrich(run,c,company,raw,trace):
    from agent.v15_pipeline import cached_rows
    key=domain(company['company_website']);rows=cached_rows(run);allowed=judge_mirror.employee_count_buckets_for_icp(raw)
    item=await profile(run,company,trace)
    if not item.get('enriched'):
        item['enriched']=True
        try:
            if item['url']:
                response=await call(run,key,'harvestapi_get_company',{'url':item['url']})
                if isinstance(response,dict) and response.get('error'):item['reason']=str(response['error'])[:200]
                evidence=structured(response,key,item['url'])
                if evidence:
                    sql_band=item['band']
                    item.update(evidence,path='harvestapi')
                    if not item['band']:item['band']=sql_band
                    if evidence['band']:
                        item['observations'].append({'url':item['url'],'band':item['band'],'quote':'employeeCountRange: '+json.dumps(item['raw_band'],ensure_ascii=False),
                            'in_band':item['band'] in allowed,'linkedin_confirmed':True,'priority':0,'names_domain':True,
                            'observation':'harvestapi_get_company.employeeCountRange; exact website and LinkedIn URL','from_cache':False,'page_date':''})
            else:
                slug=next((s for row in rows if (s:=v21_size.homepage_anchor(row,company['company_website']))),'')
                if slug:
                    url='https://www.linkedin.com/company/'+slug+'/'
                    response=await call(run,key,'exa_contents',{'urls':[url],'max_chars':4000})
                    proof=v21_size.linkedin_size(c,response.get('results',[])+rows,slug,allowed)
                    item['fallback_url']=url
                    # A SQL row with no LinkedIn URL stays a SQL resolution, with an explicit fallback subroute.
                    item['fallback']='exa_fallback'
                    if not item['sql_resolved']:item['path']='exa_fallback'
                    if proof:item['raw_band']=proof['count'];item['band']=proof['band']
        except (Exception,asyncio.CancelledError) as exc:item['reason']=type(exc).__name__
        trace('size.path',dict(item))
    # New observations cannot create an admission rejection. Keep the claim in-band even before publication.
    proof,_=v21_size.find_evidence(c,rows,allowed,run.eval_date)
    supported=item['band'] if item['band'] in allowed else proof['band'] if proof else None
    out=dict(company)
    if supported:out['employee_count']=supported
    if item['observations'] and item['url']:
        out['fit_evidence_urls']=list(dict.fromkeys([item['url'],*out.get('fit_evidence_urls',[])]))[:3]
    old=v21_size.state(run)['records'].get(key,{})
    v21_size.state(run)['records'][key]={**old,'company':company['company_name'],'website':company['company_website'],
      'provable':bool(supported),'supported_band':supported,'conflict':False,'provability_rank':4 if item['band'] in allowed else 3 if proof else 0,
      'a_score':c.get('_phase_a',{}).get('score',0),'linkedin_anchor':bool(item['url']),
      'linkedin_slug':v21_size.linkedin_slug(item['url']),'non_linkedin_proven':bool(proof)}
    return out

def guard(run,companies,raw,trace):
    if not v12_llm.enabled('V33_BAND_GUARD'):return companies
    allowed=judge_mirror.employee_count_buckets_for_icp(raw)
    if not allowed:return companies
    from agent.v20r4_headcount import bounds
    from agent.v32_size import observe,resolve
    from agent.v15_pipeline import cached_rows
    result=[]
    for c in companies:
        observations,_=observe(run,c,cached_rows(run),allowed)
        supported=[x for x in observations if x['band'] in allowed]
        selected=resolve(c,supported,allowed)['selected']
        band=selected['band'] if selected else min(allowed,key=lambda b:bounds(b)[0])
        basis='supported_in_band' if selected else 'fallback_smallest'
        result.append(dict(c,employee_count=band))
        trace('size.band_guard',{'company':c['company_name'],'previous':c.get('employee_count'),'submitted':band,'allowed':allowed,'source':selected['url'] if selected else '', 'claim_basis':basis})
        trace('size.claim_basis',{'company':c['company_name'],'basis':basis,'band':band,'selected':selected})
    return result
