"""Rank returned companies by observed workforce evidence, without admission gates."""
import asyncio,re
from agent import v12_llm,v15_budget,v21_size,judge_mirror
from agent.deadline import BudgetExhausted
from agent.v92_common import domain
from agent.evidence import _parse_date
from agent.v20r8_strict import aliases,named

FLAGS=('V32_SIZE_CONFIRMED','V32_LINKEDIN_CONFIRM','V32_CONFLICT_RESOLVE')
TRUSTED=('fundup.ai','getclera.com','getclera.ai','zoominfo.com','caplight.com','pitchbook.com','tracxn.com')
PROFILE_HOSTS=set(TRUSTED)|{'tracxn.com','growjo.com'}
LABEL=re.compile(r'\b(?:company\s+size|number\s+of\s+employees|employee\s+count|employees)\s*[:\n]?\s*('+v21_size.NUMBER+r')\s*(?:employees\b)?',re.I)

def active():return any(v12_llm.enabled(f) for f in FLAGS)

def bound_slug(run,c,rows):
    slugs={s for row in rows if (s:=v21_size.homepage_anchor(row,c['company_website']))}
    if len(slugs)==1:return next(iter(slugs))
    if len(slugs)>1:return ''
    record=getattr(run,'v21_size_state',{}).get('records',{}).get(domain(c['company_website']),{})
    return record.get('linkedin_slug','') if record.get('linkedin_anchor') else ''

def priority(url,c,linkedin=False):
    if linkedin:return 0
    host=domain(url)
    if host in TRUSTED:return 1
    if host==domain(c['company_website']):return 20
    return 10

def page_date(row,evaluation_date):
    # Only an explicit publication/update date participates in a tie. A fetch
    # timestamp or an unrelated event date in the body is not a page date.
    values=[row.get(k) for k in ('dateModified','last_updated','updatedAt','datePublished','publishedDate','date')]
    values+=re.findall(r'\bLast updated\s*:\s*([^\n]+)',str(row.get('text') or ''),re.I)
    dates=[d for value in values if (d:=_parse_date(value)) and d<=evaluation_date]
    return max(dates).isoformat() if dates else ''

def observe(run,c,rows,allowed):
    slug=bound_slug(run,c,rows);found=[];seen=set()
    brand_domain=domain(c['company_website'])
    domain_pattern=re.compile(r'(?<![a-z0-9.-])'+re.escape(brand_domain)+r'(?![a-z0-9-]|\.[a-z0-9])',re.I)
    # When one profile explicitly binds the website, namesake profiles on the
    # same service cannot supply a contradictory workforce for this company.
    bound_profiles={domain(row.get('url')) for row in rows if domain(row.get('url')) in PROFILE_HOSTS
      and named(str(row.get('title','')),aliases(c)) and domain_pattern.search(str(row.get('text','')))}
    for row in rows:
        if row.get('error') or not domain(row.get('url')):continue
        text=str(row.get('text') or '');url=row['url'];proof=None;li=False
        if domain(url) in bound_profiles and not domain_pattern.search(text):continue
        if v21_size.is_linkedin(url):
            if not slug or v21_size.linkedin_slug(url)!=slug:continue
            # A workforce-member estimate is not LinkedIn's Company Size line.
            text=re.split(r'\b(?:Similar pages|People also viewed|Affiliated pages)\b',text,flags=re.I)[0]
            proof=v21_size.linkedin_size(c,[dict(row,text=text)],slug,allowed)
            li=bool(proof)
        else:
            proof=v21_size.evidence(c,row,allowed,run.eval_date)
            # Structured company-profile fields require a bound profile title;
            # arbitrary news/pricing text cannot gain attribution from a header.
            if not proof and domain(url) in PROFILE_HOSTS and named(str(row.get('title','')),aliases(c)):
                if not v21_size.NEGATIVE.search(text[:250]):
                    match=LABEL.search(text)
                    if match:
                        band=v21_size.bucket(match.group(1).replace('–','-').replace('—','-'))
                        if band:proof={'url':url,'band':band,'quote':match.group(0),'in_band':band in allowed}
        if not proof:continue
        key=(url.rstrip('/').replace('://www.','://'),proof['band'])
        if key in seen:continue
        seen.add(key);found.append({'url':url,'band':proof['band'],'quote':proof['quote'],
          'in_band':proof['band'] in allowed,'linkedin_confirmed':li,'priority':priority(url,c,li),
          'observation':'provider-returned page text','from_cache':not row.get('_v32_fetched',False),
          'names_domain':bool(domain_pattern.search(text)),'page_date':page_date(row,run.eval_date)})
    if v12_llm.enabled('V33_SIZE_PATH'):
        record=getattr(run,'v33_size_records',{}).get(brand_domain,{})
        found.extend(record.get('observations',[]))
        slug=v21_size.linkedin_slug(record.get('url')) or slug
    return found,slug

def resolve(c,observations,allowed):
    bands={x['band'] for x in observations};conflict=len(bands)>1
    ordered=sorted(observations,key=lambda x:x['priority']);chosen=None;basis='fallback_smallest'
    highest=[x for x in ordered if x['priority']==ordered[0]['priority']] if ordered else []
    tied=len({x['band'] for x in highest})>1
    # Count supporting observations by distinct URL, so repeated cache/fetch
    # rows cannot win a tie by duplication. This is not an employee count.
    row_counts={band:len({x['url'].rstrip('/').replace('://www.','://') for x in highest if x['band']==band}) for band in bands}
    remaining=highest;tie_reason='stable_url'
    for name,value in (('company_domain',lambda x:bool(x.get('names_domain'))),
                       ('page_date',lambda x:x.get('page_date','')),
                       ('supporting_row_count',lambda x:row_counts[x['band']]),
                       ('in_band',lambda x:x['band'] in allowed)):
        if not remaining:break
        before={x['band'] for x in remaining};best=max(map(value,remaining))
        remaining=[x for x in remaining if value(x)==best]
        if len(before)>1 and len({x['band'] for x in remaining})==1:tie_reason=name
    if remaining:
        chosen=min(remaining,key=lambda x:(x['url'],x['band']))
        basis='confirmed_linkedin' if chosen['linkedin_confirmed'] else 'preferred_observed'
    if chosen:band=chosen['band']
    else:
        from agent.v20r4_headcount import bounds
        band=min(allowed,key=lambda b:bounds(b)[0]) if allowed else c['employee_count']
    contested=[x for x in observations if x['band']!=band]
    return {'band':band,'claim_basis':basis,'claim_source':domain(chosen['url']) if chosen else '',
      'found':bool(observations),'conflict':conflict,'resolved_conflict':bool(conflict and chosen),
      'unresolved_conflict':False,'tie_broken':tied,'tie_reason':tie_reason if tied else '',
      'supporting_row_counts':row_counts,'selected':chosen,'contradictions':contested,
      'effective_contradictions':[] if not chosen or chosen['band']==band else [chosen]}

def assess(run,c,raw,rows,trace):
    allowed=judge_mirror.employee_count_buckets_for_icp(raw);observations,slug=observe(run,c,rows,allowed)
    result=resolve(c,observations,allowed)
    in_band=result['band'] in allowed
    selected=result['selected']
    score=0 if not selected else 1 if not in_band else 4 if selected['linkedin_confirmed'] else 3 if selected['priority']==1 else 2
    rank_class={4:'linkedin_in_band',3:'cited_host_in_band',2:'news_or_own_in_band',1:'observed_outside',0:'nothing_found'}[score]
    confirmed=[x for x in observations if x['linkedin_confirmed']]
    top=score==4
    for x in confirmed:
        trace('size.confirmed_page',{'company':c['company_name'],'url':x['url'],'band':x['band'],'in_band':x['in_band'],'from_cache':x['from_cache'],'body_available':True,'observation':x['observation']})
    if not confirmed and slug:
        for row in rows:
            if v21_size.linkedin_slug(row.get('url'))==slug and row.get('text') and not row.get('error'):
                trace('size.confirmed_page',{'company':c['company_name'],'url':row['url'],'band':None,'in_band':False,
                  'body_available':True,'from_cache':not row.get('_v32_fetched',False),'reason':'company_size_not_observed'})
                break
    if result['tie_broken']:
        trace('size.tie_broken',{'company':c['company_name'],'reason':result['tie_reason'],'selected':selected,
          'supporting_row_counts':result['supporting_row_counts']})
    trace('size.conflicts',{'company':c['company_name'],'selected':selected,'conflicts':result['contradictions']})
    trace('size.rank',{'company':c['company_name'],'score':score,'rank_class':rank_class,'tie_broken':result['tie_broken'],'linkedin_in_band':top,'in_band':in_band,
        'found':result['found'],'conflict':result['conflict'],'resolved_conflict':result['resolved_conflict'],'observations':observations})
    out=c
    if v12_llm.enabled('V32_CONFLICT_RESOLVE'):
        out=dict(out,employee_count=result['band'])
        trace('size.claim_source',{'company':c['company_name'],'host':result['claim_source'],'url':selected['url'] if selected else '', 'band':result['band']})
        trace('size.claim_basis',{'company':c['company_name'],'basis':result['claim_basis'],'band':result['band'],'claim_source':result['claim_source'],
          'selected':result['selected'],'source_priority_resolved':result['resolved_conflict'],
          'unresolved_conflict':result['unresolved_conflict'],'contradictions':result['contradictions'],
          'effective_contradictions':result['effective_contradictions']})
    if confirmed and v12_llm.enabled('V32_LINKEDIN_CONFIRM'):
        best=next((x for x in confirmed if x['band']==result['band']),confirmed[0])
        out=dict(out,fit_evidence_urls=list(dict.fromkeys([best['url'],*out.get('fit_evidence_urls',[])]))[:3])
    state={'company':c['company_name'],'slug':slug,'confirmed':confirmed,'score':score,'rank_class':rank_class,'linkedin_in_band':top,'observations':observations,**result}
    return out,state

def claim(run,operation):
    b=run.v15_budget;lane,key,primary,no_tools=v15_budget.WORK.get()
    with b.lock:
        fetched=getattr(run,'v32_fetched',set());prior=getattr(run,'v21_size_state',{}).get('linkedin_calls',set())
        if not v12_llm.enabled('V32_LINKEDIN_CONFIRM') or lane!='v32_linkedin' or not primary or no_tools or operation!='exa_contents' or key not in getattr(run,'v32_returned',set()):raise BudgetExhausted('size confirmation requires an already returned company')
        if key in fetched or key in prior or len(fetched|prior)>=2 or sum(b.lanes.values())>=29 or b.overrun:raise BudgetExhausted('size confirmation allowance exhausted')
        ticket=b._reserve('deepline',v15_budget.DL_CEILINGS[operation]);fetched.add(key);run.v32_fetched=fetched;b.lanes['reserve']+=1
        b.trace('budget.claim',{'lane':lane,'company':key,'charged_lane':'reserve','primary':True,'operation':operation})
        return ticket

async def finish(run,companies,raw,rows,trace):
    if not active():return companies
    rows=list(rows);items=[];run.v32_returned={domain(c['company_website']) for c in companies}
    for index,c in enumerate(companies):
        try:
            out,state=assess(run,c,raw,rows,trace);items.append([index,out,state])
        except Exception as exc:
            trace('size.unavailable',{'company':c['company_name'],'reason':type(exc).__name__});items.append([index,c,{'score':0,'linkedin_in_band':False,'slug':'','confirmed':[]}])
    ranking=lambda item:(item[2]['score'],not item[2].get('tie_broken',False))
    if v12_llm.enabled('V32_LINKEDIN_CONFIRM'):
        for item in sorted(items,key=ranking,reverse=True):
            index,c,state=item;key=domain(c['company_website']);prior=getattr(run,'v21_size_state',{}).get('linkedin_calls',set());fetched=getattr(run,'v32_fetched',set())
            if state['confirmed']:continue
            reason=('structured_size_path_already_handled' if v12_llm.enabled('V33_SIZE_PATH') and key in getattr(run,'v33_size_records',{}) else
                    'no_homepage_bound_url' if not state['slug'] else
                    'already_attempted' if key in prior|fetched else
                    'call_budget_exhausted' if sum(run.v15_budget.lanes.values())>=29 else
                    'confirmation_cap' if len(prior|fetched)>=2 else
                    'deadline' if run.remaining()<3 else '')
            if reason:
                trace('size.confirm_skipped',{'company':c['company_name'],'reason':reason,
                  'url':'https://www.linkedin.com/company/'+state['slug'] if state['slug'] else '',
                  'calls_used':sum(run.v15_budget.lanes.values()),'company_size_observed':False})
                continue
            try:
                with v15_budget.work('v32_linkedin',key,primary=True):
                    async with asyncio.timeout(min(8,max(0,run.remaining()-1))):
                        response=await run.tool('exa_contents',{'urls':['https://www.linkedin.com/company/'+state['slug']], 'max_chars':4000})
                returned=response.get('results',[]) if isinstance(response,dict) else []
                rows.extend(dict(row,_v32_fetched=True) for row in returned if isinstance(row,dict))
                item[1],item[2]=assess(run,companies[index],raw,rows,trace)
                if not item[2]['confirmed']:trace('size.confirmed_page',{'company':c['company_name'],'url':'https://www.linkedin.com/company/'+state['slug'],'band':None,'in_band':False,'from_cache':False})
            except (Exception,asyncio.CancelledError) as exc:trace('size.unavailable',{'company':c['company_name'],'reason':type(exc).__name__})
    if v12_llm.enabled('V32_SIZE_CONFIRMED'):items.sort(key=ranking,reverse=True)
    run.v32_size_records={domain(c['company_website']):state for _,c,state in items}
    return [c for _,c,_ in items]
