"""Budgeted extraction and evidence decisions over this run's own cached text."""
import asyncio,json,re
from datetime import timedelta
from agent.safe_urls import urlsplit
from agent import v15_budget as budgets, v16_budget, v17_budget, v18_budget, v18_identity, v18_fit, v17_domains, v15_stage_proof, v12_llm, judge_mirror, p6_identity, p7_observations, v13_slots
from agent.evidence import _clean,_norm,_host,_parse_date,_snippet_on_page,_bucket_for,_country_ok
from agent.v92_common import domain,signal_url_ok,observed_date
from agent.deadline import BudgetExhausted
from agent import v19_triage, v20_phases, v21_size, v20r2_budget, v20r4_headcount as hc, v20r5_primary as intent_guard

def cached_rows(run):
    rows={}
    for item in (*getattr(run.tools,'row_cache',{}).values(),*run.page_cache.values()):
        if isinstance(item,dict) and item.get('url') and not item.get('error'):
            key=item['url'].split('#')[0]
            if len(item.get('text',''))>=len(rows.get(key,{}).get('text','')):rows[key]=item
    return list(rows.values())

def batches(rows,size=20):
    return [rows[i:i+size] for i in range(0,len(rows),size)]

def list_field(data,name):
    value=data.get(name) if isinstance(data,dict) else None
    return value if isinstance(value,list) else []

async def cached_ask(run,system,build_prompt,*,max_tokens,excerpt_limit):
    """Keep every row; fit excerpts to the actual remaining reservation first."""
    meter=getattr(run,'v15_budget',None);limit=excerpt_limit;model=v12_llm.effective_model(run)
    if meter:
        available=meter.available_llm()-1000  # Small encoding/metadata margin.
        last_kwargs={}
        def fits(n):
            nonlocal last_kwargs
            kwargs={'model':model,'messages':[{'role':'system','content':system},
                    {'role':'user','content':build_prompt(n)}],'max_tokens':max_tokens,'temperature':.1,
                    'extra_body':{'usage':{'include':True}}}
            if model.startswith('openai/'):kwargs['reasoning_effort']='low'
            last_kwargs=kwargs
            if getattr(meter,'expected_mode',False) or getattr(meter,'predict',False):return meter.prediction_fits(model,kwargs)
            pricing=v16_budget.request_ceiling if getattr(meter,'settled_mode',False) else budgets.llm_ceiling
            if getattr(meter,'settled_mode',False):kwargs['max_tokens']=max(max_tokens,4096)
            return pricing(model,kwargs)<=available
        if not fits(limit):
            if not fits(120):
                if getattr(meter,'expected_mode',False):meter.note_prediction_refusal(model,last_kwargs)
                raise BudgetExhausted('cached prompt cannot fit remaining money with minimum excerpts')
            low,high=120,limit
            while low<high:
                middle=(low+high+1)//2
                if fits(middle):low=middle
                else:high=middle-1
            limit=low
            from agent import pipeline as p
            p._trace('llm.projection',{'original_excerpt_chars':excerpt_limit,'excerpt_chars':limit,
                                      'available_microusd':available,'all_rows_retained':True})
    return await run.ask(system,build_prompt(limit),max_tokens=max_tokens)

def quote_page(rows,fact):
    if not isinstance(fact,dict):return None,''
    idx=fact.get('row')
    if isinstance(idx,bool) or not isinstance(idx,int) or not 0<=idx<len(rows):return None,''
    row=rows[idx];quote=_snippet_on_page(str(fact.get('quote') or ''),row.get('text',''))
    return (row,quote) if quote else (None,'')

def related(run,candidate):
    urls=set(candidate.get('urls',[]));name=_norm(candidate['company_name']);host=domain(candidate['domain'])
    # Extraction has no name-token prefilter. This is only evidence grouping,
    # retaining explicitly selected URLs even when the title omits the brand.
    return [r for r in cached_rows(run) if r['url'] in urls or domain(r['url'])==host
            or (name and name in _norm(r.get('text','')+' '+r.get('title','')))]

def merge_candidates(old,new):
    identity_key=v17_domains.key if v12_llm.enabled('V17_DOMAINS') else lambda c:domain(c['domain']) or _norm(c['company_name'])
    merged={identity_key(c):dict(c) for c in old}
    for c in new:
        key=identity_key(c)
        if key in merged:
            merged[key]['urls']=list(dict.fromkeys(merged[key]['urls']+c['urls']))
            if not merged[key].get('domain') and c.get('domain'):
                merged[key].update(domain=c['domain'],_pending_identity=False)
        else:merged[key]=dict(c)
    return list(merged.values())

async def extract(run,rows,trace,*,salvage=False):
    candidates=[]
    chunks=batches(rows,20) if not salvage else batches(rows,max(1,(len(rows)+1)//2))
    for index,chunk in enumerate(chunks):
        # Shorter than the 1,400-char ceiling to leave room for max-cost reservation.
        def build_prompt(limit):
            projection=[{'row':i,'url':r['url'],'title':r.get('title','')[:120],
                         'date':r.get('date',''),'text':r.get('text','')[:limit]}
                        for i,r in enumerate(chunk)]
            if v12_llm.enabled('V17_DOMAINS'):
                for projected,row in zip(projection,chunk):projected['domain_hints']=v17_domains.model_hints(row)
            domain_instruction=('The domain is REQUIRED: give the company primary website as your best guess, e.g. example.com. '
                'A guess is expected and will be verified later. The publisher domain is wrong only when the publisher is not the company. '
                'Do not invent companies; all company names and events must come from the supplied rows. '
                if v12_llm.enabled('V17_DOMAINS') else 'A company domain is an unverified hint, not the publisher domain. No remembered companies. ')
            return ('Extract ALL distinct commercial companies that are subjects of an event in these rows. '
                'Do not filter industry, stage, country, size, or the first word of a name; fit comes later. '
                +domain_instruction+
                'Return compact JSON {"candidates":[["name","domain",[row_indices]]]} only. '
                'Include multiple company-subject events on a list page. Rows:\n'+json.dumps(projection,ensure_ascii=False))
        trace('extract.batch',{'phase':'salvage' if salvage else 'main','batch':index,'rows':len(chunk),
                               'urls':[r['url'] for r in chunk]})
        try:
            with v16_budget.phase('extract',first=index==0):
                data=await cached_ask(run,'Extract only from supplied rows. JSON only.',build_prompt,
                                  max_tokens=2000 if v12_llm.enabled('V16_SPEND_SETTLED') else 1800,
                                  excerpt_limit=400 if salvage else 600)
        except (BudgetExhausted,v12_llm.LLMTruncated) as exc:
            trace('candidate.loss',{'stage':'candidates_extracted','reason':type(exc).__name__,
                                     'rows_unprocessed':sum(len(x) for x in chunks[index:])});break
        out=[]
        if v12_llm.enabled('V17_DOMAINS'):
            out=v17_domains.parse(list_field(data,'candidates'),chunk,trace)
            out=[c for c in out if not c['domain'] or not any(domain(str(x))==domain(c['domain']) for x in run.icp.get('excluded_companies',[]))]
            candidates=merge_candidates(candidates,out)
            continue
        for c in list_field(data,'candidates'):
            if not isinstance(c,list) or len(c)!=3 or not isinstance(c[2],list):continue
            name=_clean(c[0]);host=_host(str(c[1] or ''))
            urls=[chunk[i]['url'] for i in c[2] if isinstance(i,int) and not isinstance(i,bool) and 0<=i<len(chunk)]
            if not name or not host or not urls:
                trace('candidate.loss',{'company':name,'stage':'candidates_extracted','reason':'missing_name_domain_or_row_reference'})
                continue
            if any(domain(str(x))==domain(host) for x in run.icp.get('excluded_companies',[])):continue
            out.append({'company_name':name,'domain':host,'urls':list(dict.fromkeys(urls))})
        candidates=merge_candidates(candidates,out)
    return candidates

def identity_from_cache(run,cand):
    for home in cached_rows(run):
        host=domain(home['url'])
        if host!=domain(cand['domain']) or urlsplit(home['url']).path.strip('/'):
            continue
        text=_clean(home.get('text',''))
        if len(text)<80 or judge_mirror.check_antibot_wall(text) or re.search(r'domain (?:is )?for sale|buy this domain|for sale \|',text[:500]+' '+home.get('title',''),re.I):continue
        canonical=domain(home.get('canonical_url'))
        if canonical and canonical!=host:continue
        brand=p6_identity.brand(home,cand['company_name'],host)
        if not brand and re.search(r'\b(?:every|our|the)\s+'+re.escape(cand['company_name'])+r'\s+(?:product|platform|team|solution)s?\b',text,re.I):
            brand=cand['company_name']  # Literal self-product wording on its own page.
        if brand:return home,{'name':brand,'website':home['url'],'source':'company_homepage'}
    return None,None

def compact_pages(rows,limit=1400):
    return [{'row':i,'url':r['url'],'title':r.get('title','')[:100],'date':r.get('date',''),
             'text':r.get('text','')[:limit]} for i,r in enumerate(rows)]

async def assess(run,cand,rows,*,excerpt_limit=1400):
    intent=run.icp['required_intents'][0]
    prefix=(intent_guard.instruction(run.icp)+f"Company: {cand['company_name']} ({cand['domain']}). Evaluation {run.eval_date}. "
            f"Required PRIMARY event: {intent['signal']}. Industry: {run.icp.get('industry')}. "
            f"Required product: {run.icp.get('required_attribute') or run.icp.get('product_service')}. "
            'Decide in order: primary event sentence/date, industry/product, headquarters country, stage, employees. '
            'Use only supplied text; UNKNOWN is null. Quote each claim verbatim, with its row index. '
            'Country must be this company HQ, not customers, expansion, factory or regional HQ. '
            'Employee count belongs to this company, not customers. Stage belongs to this company, not investors. '
            'Return {"primary_status":"match|mismatch|unknown","primary":{"row":0,"quote":"event sentence","date":"YYYY-MM-DD"},'
            '"industry":"match|mismatch|unknown","attribute":{"row":0,"quote":"product wording"},'
            '"country":{"row":0,"quote":"HQ wording","value":"country"},'
            '"stage":{"row":0,"quote":"funding wording","value":"round name"},'
            '"employees":{"row":0,"quote":"employee wording","value":"exact integer or stated size band"}}. '
            'No primary when the event belongs to another entity or misses the requested event. '
            'Use null for absent facts. Pages:\n')
    return await cached_ask(run,'Evidence-only ordered decisions. JSON only.',
               lambda limit:prefix+json.dumps(compact_pages(rows,limit),ensure_ascii=False),max_tokens=1100,excerpt_limit=excerpt_limit)

async def assess_batch(run,candidates):
    """Share prompt overhead; each candidate still needs a complete verdict."""
    pages=[];seen={};items=[]
    for i,cand in enumerate(candidates):
        indices=[]
        for row in related(run,cand):
            if row['url'] not in seen:
                seen[row['url']]=len(pages);pages.append(row)
            indices.append(seen[row['url']])
        items.append({'id':i,'name':cand['company_name'],'domain':cand['domain'],'rows':indices})
    prefix=(intent_guard.instruction(run.icp)+f"Evaluation {run.eval_date}. Primary event: {run.icp['required_intents'][0]['signal']}. "
            f"Industry {run.icp.get('industry')}; required product {run.icp.get('required_attribute') or run.icp.get('product_service')}. "
            'For EACH supplied candidate, decide in order: its primary event and date; industry/product; GLOBAL HQ country; '
            'its own LATEST completed funding stage (not an old round); its own employees, not customers or investors. No memory. Missing facts are null. '
            'Each fact needs a VERBATIM quote (max 140 characters) and page row index. '
            +((('Also return venture_plausible: true, false, or "unknown". False for public companies, large retail chains/public brands, big tech, government, universities or cached NYSE/Nasdaq/publicly traded/parent-conglomerate evidence. An independent early-stage DTC retailer/brand is not automatically false. Decide industry from the described product and ICP required attribute, not directory taxonomy labels alone. ' if v12_llm.enabled('V20R9_QUERIES') else 'Also return venture_plausible: true, false, or "unknown" per candidate. False for public companies, consumer brands, retailers, big tech, government, universities, or cached NYSE/Nasdaq/publicly traded/parent-conglomerate evidence. ')+'This only controls stage lookups, not company rejection. ') if v12_llm.enabled('V20R6_PLAUSIBLE') else '')+
            'Return {"verdicts":[{"id":0,'+
            ('"venture_plausible":true,' if v12_llm.enabled('V20R6_PLAUSIBLE') else '')+
            '"primary_status":"match|mismatch|unknown","primary":{"row":0,"quote":"event","date":"YYYY-MM-DD"},'
            '"industry":"match|mismatch|unknown","attribute":{"row":0,"quote":"product"},'
            '"country":{"row":0,"quote":"headquarters","value":"country"},'
            '"stage":{"row":0,"quote":"round","value":"stage"},'
            '"employees":{"row":0,"quote":"employees","value":"stated count or band"}}]}. '
            +('Prefer the original company newsroom page for the PRIMARY event when it is among the supplied pages; '
              'a funding-stage announcement is not proof of a different requested event. '
              if v12_llm.enabled('V20_TWO_PHASE') and v12_llm.enabled('V92_OWN_EVENT_FIRST') else '')+
            'Candidates: '+json.dumps(items,ensure_ascii=False)+'\nPages: ')
    data=await cached_ask(run,'Cached evidence only, per-company decisions. JSON only.',
             lambda limit:prefix+json.dumps(compact_pages(pages,limit),ensure_ascii=False),max_tokens=(150*len(candidates)+200 if budgets.WORK.get()[0]=='phase_a' and v12_llm.enabled('V20R2_CHEAP_PHASE_A') else (2500 if len(candidates)>3 else 1800)),excerpt_limit=1000 if len(candidates)>3 else 700)
    by_id={d['id']:d for d in list_field(data,'verdicts') if isinstance(d,dict) and isinstance(d.get('id'),int)
           and not isinstance(d['id'],bool) and 0<=d['id']<len(candidates)}
    return [{**c,'_verdict':by_id.get(i,{}),'_verdict_rows':pages} for i,c in enumerate(candidates)]

def grounded_facts(rows,verdict):
    result={}
    if not isinstance(verdict,dict):return result
    for key in ('primary','attribute','country','stage','employees'):
        fact=verdict.get(key);page,quote=quote_page(rows,fact)
        if not page:continue
        value=fact.get('value')
        if key=='country' and (not isinstance(value,str) or _country_ok(quote,value) is not True):continue
        if key=='employees':
            literal=str(value or '').replace(',','').replace(' ','').lower()
            quoted=quote.replace(',','').replace(' ','').lower()
            if not literal or not re.search(r'(?<!\d)'+re.escape(literal)+r'(?!\d)',quoted):continue
            if isinstance(value,bool):continue
        result[key]={'page':page,'quote':quote,'value':value,'date':fact.get('date')}
    return result

async def candidate(run,cand,raw_icp,trace,*,no_tools=False,salvage_identity=False,defer_profile=False):
    from agent import pipeline as p
    key=v17_domains.key(cand) if v12_llm.enabled('V17_DOMAINS') else domain(cand['domain']);stage='primary_verified'
    soft=v12_llm.enabled('V17_FIT_UNOBSERVED')
    def loss(reason,**kw):
        if hasattr(run,'v16_rejected') and reason in {'stage_conflict','stage_conflict_lookup','stage_required_unobserved','size_prior_smaller_hint','cached_country_conflict','cached_headcount_conflict',
            'profile_country_conflict','country_conflict','headcount_conflict'}:
            run.v16_rejected.add(key)
            if v12_llm.enabled('V17_DOMAINS'):run.v16_rejected.add(v17_domains.key(cand))
        trace('candidate.loss',{'company':cand['company_name'],'domain':key,'stage':stage,'reason':reason,**kw})
        return None
    rows=cand.get('_verdict_rows') or related(run,cand)
    verdict=cand.get('_verdict') if '_verdict' in cand else await assess(run,cand,rows)
    if not isinstance(verdict,dict):return loss('no_complete_verdict')
    frozen=cand.get('_phase_a_primary')
    if cand.get('_phase_a',{}).get('primary_from_cache') and not frozen:
        return loss('phase_a_primary_missing')
    if not frozen and verdict.get('primary_status')=='mismatch':return loss('event_conflict')
    if not frozen and verdict.get('primary_status')!='match':return loss('category_primary_unobserved')
    facts=grounded_facts(rows,verdict)
    if frozen:facts['primary']=frozen
    if 'primary' not in facts and not no_tools:
        with budgets.work('target',key):
            result=await run.tool('exa_search',{'query':f"{cand['company_name']} {run.icp['required_intents'][0]['signal']}"[:500],
                                               'numResults':3,'contents':{'text':{'maxCharacters':3000}}})
        cand['urls']+= [r['url'] for r in result.get('results',[])]
        rows=related(run,cand);verdict=await assess(run,cand,rows)
        if not isinstance(verdict,dict) or verdict.get('primary_status')!='match':return loss('category_primary_unobserved')
        facts=grounded_facts(rows,verdict or {})
    primary=facts.get('primary')
    if not primary:return loss('event_quote_unobserved')
    if intent_guard.funding_mismatch(run.icp,primary['quote']):return loss('funding_primary_for_nonfunding')
    page=primary['page'];when=_parse_date(primary['date']);observed=observed_date(page)
    if not when or (observed and _parse_date(observed)!=when):return loss('event_date_not_supported')
    if not observed and p._date_in_text(page.get('text',''))!=when:return loss('event_date_unobserved')
    age=(run.eval_date-when).days
    if age<0 or age>int(run.icp['required_intents'][0].get('max_age_days') or 365):return loss('event_date_outside_window')
    if not signal_url_ok(page['url']):return loss('invalid_event_source')
    run.v15_primary.add(key)
    trace('candidate.primary',{'company':cand['company_name'],'domain':key,'url':page['url'],
          'quote':primary['quote'],'date':when.isoformat(),'origin':'phase_a.primary_from_cache' if frozen else 'assessed_primary'})
    stage='fit_gate_passed'
    if verdict.get('industry')=='mismatch':return loss('industry_conflict')
    stage_fact=facts.get('stage',{});want=run.icp.get('company_stage') or ''
    actual=p._funding_stage(stage_fact.get('quote',''))
    if actual and (not v15_stage_proof.submit_stage_quote(actual,stage_fact.get('quote',''))
                   or not v15_stage_proof.announcement_source(stage_fact['page']['url'],cand['domain'])):
        trace('stage.hint_omitted',{'company':cand['company_name'],'reason':'not_affirmed_announcement'})
        actual=''
    if actual and want and _norm(want)!='any' and not p._stage_matches(actual,want):return loss('stage_conflict',observed=actual)
    if facts.get('country') and _country_ok(facts['country']['value'],run.icp.get('country','')) is False:
        return loss('cached_country_conflict')
    known_count=facts.get('employees',{}).get('value')
    known_band=judge_mirror.normalize_employee_count_bucket(known_count,default=None) or judge_mirror.normalize_observed_employee_count_bucket(known_count,default=None)
    allowed=judge_mirror.employee_count_buckets_for_icp(raw_icp)
    r4=v12_llm.enabled('V20R4_HEADCOUNT_HINT')
    if known_band and known_band not in allowed:
        if not r4 or hc.inspect(run,cand,known_count,allowed,trace)['conflict']:return loss('cached_headcount_conflict')
    if hasattr(run,'v16_primary_candidates') and not no_tools:
        run.v16_primary_candidates[key]={**cand,'_verdict':verdict,'_verdict_rows':rows}
    stage_required=v12_llm.enabled('V19_STAGE_REQUIRED') and v18_fit.venture(want)
    cheap_first=bool(cand.get('_phase_a')) and v12_llm.enabled('V20_CHEAP_FIRST')
    async def screen_stage():
        nonlocal actual,stage_fact
        if v12_llm.enabled('V18_STAGE_LOOKUP') or stage_required:
            screening=await v18_fit.stage_lookup(run,cand,cached_rows(run),trace,actual=actual,stage_fact=stage_fact,
                                                require_announcement=stage_required)
            if screening['decision']=='conflict':
                loss('stage_conflict_lookup',observed=screening['actual'],origin=screening['origin'])
                return False
            actual=screening['actual'];stage_fact=screening['fact']
            if stage_required and (v18_fit.category(actual)!=v18_fit.category(want) or not v19_triage.affirmed(cand,stage_fact)):
                loss('stage_required_unobserved',lookup_status=screening['lookup_status'])
                return False
        return True
    if cheap_first and not await screen_stage():return None
    original_key=key
    home,identity=identity_from_cache(run,cand)
    bind=salvage_identity and v12_llm.enabled('V16_SALVAGE_IDENTITY')
    if v12_llm.enabled('V17_DOMAINS'):
        if not no_tools or bind:
            from agent import v20r7_identity
            resolver=v20r7_identity.resolve if v12_llm.enabled('V20R7_ALLOCATION') else v18_identity.resolve if v12_llm.enabled('V18_REDIRECT') else v17_domains.resolve
            resolved=await resolver(run,cand,cached_rows(run),trace)
            if resolved:
                home,identity,final=resolved
                cand['domain']=final;cand['_pending_identity']=False
            else:home,identity=None,None
        if home is None:return loss('identity_unresolved')
        key=domain(cand['domain'])
        if any(domain(str(x))==key for x in run.icp.get('excluded_companies',[])):return loss('identity_excluded_company')
        if hasattr(run,'v17_with_domain'):run.v17_with_domain.add(original_key)
    elif home is None and (not no_tools or bind):
        with budgets.work('salvage_identity' if bind else 'identity',key,primary=True):
            fetched=await run.tool('fetch_page',{'url':'https://'+cand['domain']+'/'})
        final=domain(fetched.get('url'))
        if final and final!=key and not fetched.get('error'):
            cand={**cand,'domain':final};key=final
        home,identity=identity_from_cache(run,cand)
    if home is None:return loss('identity_homepage_unproven')
    cand['_bound_name']=identity['name']
    from agent.v30_spend import identity_completed
    identity_completed(run,cand,home)
    if not cheap_first and not await screen_stage():return None
    # Home text is new evidence, not a reason to redo already-settled dimensions.
    employees=facts.get('employees',{});value=employees.get('value')
    band=judge_mirror.normalize_employee_count_bucket(value,default=None) or judge_mirror.normalize_observed_employee_count_bucket(value,default=None)
    country=facts.get('country',{}).get('value') or ''
    attr=facts.get('attribute');profile={};profile_used=False
    if soft:
        exact=p.p1_bucket.exact_headcount(cand,related(run,cand))
        if exact is not None:
            band=judge_mirror.normalize_observed_employee_count_bucket(exact,default=None)
        for match in re.finditer(r'\b(?:headquartered|global headquarters|headquarters are|based)\s+in\s+([^.;\n]{3,100})',home.get('text',''),re.I):
            decision=_country_ok(match.group(1),run.icp.get('country',''))
            if decision is False:return loss('country_conflict')
            if decision is True:country=run.icp.get('country','')
    if not band and not no_tools:
        try:
            with budgets.work('profile',key,primary=True):
                record=await run.tool('get_company_profile',{'domain':cand['domain']})
        except BudgetExhausted:
            if not v12_llm.enabled('V20R6_EVENT_LOOKUP'):raise
            record={}
            trace('profile.skipped',{'company':cand['company_name'],'reason':'confirmation_reserve'})
        profile=record.get('company') or {};profile_used=True
        profile_host=domain(profile.get('normalized_domain') or profile.get('domain'))
        if profile_host and profile_host!=key:profile={}
        if getattr(run,'v27_input',None):run.v27_profiles[key]=dict(profile)
        if r4:hc.register(run,cand,profile.get('employee_count'))
        band=(v18_fit.bucket(profile.get('employee_count')) if v12_llm.enabled('V18_HEADCOUNT_PRIOR') else
              p.p1_bucket.choose_bucket(profile.get('employee_count'),run.icp.get('employee_count') or [],profile=True))
    if v12_llm.enabled('V33_SIZE_PATH') and not no_tools:
        from agent import v33_size
        await v33_size.profile(run,cand,trace,record={'company':profile} if profile_used and profile else None)
    async def finish():
        nonlocal band,country,attr,employees,stage,allowed,value
        hint_applied=False;proof_supported=False
        if r4:
            raw=profile.get('employee_count') if profile_used else (exact if soft and exact is not None else value)
            hint=hc.inspect(run,cand,raw,allowed,trace,profile=profile_used)
            if hint['conflict']:return loss('headcount_conflict')
            if profile_used or not band or band not in allowed:
                band=hc.claim(want,allowed,hint);hint_applied=True
            if v12_llm.enabled('V21_SIZE_EVIDENCE'):
                proof,_=v21_size.find_evidence(cand,cached_rows(run),allowed,run.eval_date)
                if proof:band=proof['band'];hint_applied=False;proof_supported=True
        if profile.get('location') and _country_ok(profile['location'],run.icp.get('country','')) is False:return loss('profile_country_conflict')
        if not country and _country_ok(profile.get('location',''),run.icp.get('country','')) is True:country=run.icp.get('country','')
        if not country:
            home_country,_=p7_observations.homepage_country(home,run.icp.get('country',''))
            if home_country is True:country=run.icp.get('country','')
        if (not attr or not country or not band) and not soft:
            if not no_tools:
                missing='product platform' if not attr else ('headquarters country' if not country else 'employees headcount')
                with budgets.work('target',key,primary=True):
                    lookup=await run.tool('exa_search',{'query':f"{cand['company_name']} {cand['domain']} {missing}",
                                                       'numResults':3,'contents':{'text':{'maxCharacters':3000}}})
                cand['urls'] += [r['url'] for r in lookup.get('results',[])]
            # Reconsider only actual newly-cached text; no unconditional profile call.
            new_rows=related(run,cand)
            if new_rows!=rows and not no_tools:
                extra=await assess(run,cand,new_rows)
                extra_facts=grounded_facts(new_rows,extra or {})
                if not attr:attr=extra_facts.get('attribute')
                if not country:country=extra_facts.get('country',{}).get('value') or ''
                if not band:
                    employees=extra_facts.get('employees',{})
                    value=employees.get('value')
                    band=judge_mirror.normalize_employee_count_bucket(value,default=None) or judge_mirror.normalize_observed_employee_count_bucket(value,default=None)
        if not attr and not soft:return loss('required_product_unobserved')
        if not country and not soft:return loss('hq_country_unobserved')
        if _country_ok(country,run.icp.get('country','')) is False:return loss('country_conflict')
        observed_band=bool(band) and (proof_supported or not (r4 and (profile_used or hint_applied)));observed_country=bool(country)
        if not band and not soft:return loss('headcount_unobserved')
        if not band:
            allowed=judge_mirror.employee_count_buckets_for_icp(raw_icp)
            band=(v18_fit.prior(want,allowed,profile.get('employee_count') or v18_fit.cached_headcount_hint(cand,related(run,cand)))
                  if v12_llm.enabled('V18_HEADCOUNT_PRIOR') else allowed[len(allowed)//2])
            if v12_llm.enabled('V19_HEADCOUNT_HINT') and v12_llm.enabled('V18_HEADCOUNT_PRIOR'):
                smaller=v19_triage.smaller_hint(cand,cached_rows(run),raw_icp,band)
                if smaller and not r4:return loss('size_prior_smaller_hint',basis=smaller,unobserved=True)
        if not country:country=run.icp.get('country','')
        if band not in allowed:return loss('headcount_conflict')
        size_basis=('observed' if observed_band else 'profile hint; unobserved' if r4 and profile_used and hint['count'] is not None else 'stage prior guess; unobserved' if v12_llm.enabled('V18_HEADCOUNT_PRIOR') else 'ICP midpoint guess; unobserved')
        company={'company_name':identity['name'],'company_website':home['url'],'company_linkedin':'',
                 'company_stage':actual,'industry':run.icp.get('industry',''),'country':country,'state':'','employee_count':band,
                 'fit_summary':f"Employee bucket {band} ({size_basis}); headquarters {country} ({'observed' if observed_country else 'ICP guess; unobserved'}); stage {actual or 'unproven'}."+
                               (' Stage announcement: '+stage_fact['quote'] if actual else ''),
                 'fit_evidence_urls':list(dict.fromkeys(([stage_fact['page']['url']] if actual else [])+
                                      [home['url']]+([attr['page']['url']] if attr else [])+([employees['page']['url']] if employees.get('page') else [])))[:4],
                 'required_attribute':{'text':str(run.icp.get('required_attribute') or run.icp.get('product_service') or ''),
                                       'passed':bool(attr),'evidence_url':attr['page']['url'] if attr else '',
                                       'evidence_quote':attr['quote'] if attr else '',
                                       'explanation':'Observed product wording.' if attr else 'Unobserved; no supporting product claim supplied.'},
                 'intent_signals':[{'matched_icp_signal':0,'url':page['url'],'date':when.isoformat(),'snippet':primary['quote'][:600],
                                    'description':primary['quote'][:350],'why_now':'A dated primary event within the requested window.'}]}
        if not attr:
            # The public schema permits null, but forbids an empty evidence URL/quote.
            company['required_attribute']=None
            company['fit_summary']+=' Required product unobserved; no supporting claim supplied.'
        if not judge_mirror.submitted_conflicts(company,raw_icp):run.v15_fit.add(key)
        stage='slots_filled'
        accepted=judge_mirror.filter_company(company,raw_icp,pages=run.page_cache,identity=identity,
                                             evaluation_date=run.eval_date,log=trace,admission=True)
        if not accepted:return loss('mirror_rejected')
        if hasattr(run,'v17_fit_rank'):
            run.v17_fit_rank[key]=(int(observed_band)+int(observed_country)+int(bool(attr))+int(bool(actual)),
                                  v13_slots.weight(page['url'],home['url']))
            if v12_llm.enabled('V18_STAGE_LOOKUP') and v18_fit.venture(want):
                run.v17_fit_rank[key]=(int(bool(actual)),*run.v17_fit_rank[key])
        trace('fit.observations',{'company':company['company_name'],'employee_count_observed':observed_band,
                                'country_observed':observed_country,'product_observed':bool(attr),'stage_observed':bool(actual)})
        try:
            if any(v12_llm.enabled(f) for f in ('V30_MULTI','V30_SECOND_SIGNAL','V30_SPEND_GUARD')):
                from agent.v30_signals import cap_summary
                accepted=cap_summary(accepted)
            result=p.validate_companies([accepted],1)[0]
            if cand.get('_triage'):trace('triage.admit',{'company':company['company_name'],'candidate':cand['company_name'],
                'domain':key,**cand['_triage']})
            return intent_guard.output(result,run.icp,trace,primary=frozen)
        except Exception as exc:return loss('output_schema',detail=str(exc)[:160])
    if defer_profile:return finish
    return await finish()

async def salvage(run,raw_icp,trace,remember,filled,goal):
    """At most two model calls; extraction and text-first decisions are combined."""
    rows=cached_rows(run)
    chunks=batches(rows,min(20,max(1,(len(rows)+1)//2)) if v12_llm.enabled('V16_SPEND_SETTLED') else max(1,(len(rows)+1)//2))
    recovered=[];before=run.used['deepline'];calls=run.llm_requests
    bind=v12_llm.enabled('V16_SALVAGE_IDENTITY')
    handled=set()
    def rank(cand):
        facts=grounded_facts(cand.get('_verdict_rows',[]),cand.get('_verdict',{}))
        page=facts.get('primary',{}).get('page',{})
        return (-(len(facts)+int(bool(identity_from_cache(run,cand)[0]))),
                -v13_slots.weight(page.get('url',''),'https://'+cand['domain']+'/'))
    async def finish(items):
        if v12_llm.enabled('V19_TRIAGE'):
            items=v19_triage.rank(items,cached_rows(run),run.icp,run.eval_date,trace,
                                 pool=getattr(run,'v19_pool',[]),phase='salvage')
        else:items=sorted(items,key=rank)
        for cand in items:
            if filled()>=goal:break
            if cand.get('_triage',{}).get('list_only') and getattr(run,'v19_unresolved',set()):
                trace('triage.defer',{'company':cand['company_name'],'reason':'single_subject_unresolved'})
                continue
            key=v17_domains.key(cand) if v12_llm.enabled('V17_DOMAINS') else domain(cand['domain'])
            if key in handled:continue
            handled.add(key);recovered.append(cand)
            if key in getattr(run,'v16_rejected',set()):
                trace('candidate.loss',{'company':cand['company_name'],'stage':'fit_gate_passed','reason':'cached_proven_conflict'})
                continue
            try:remember(await candidate(run,cand,raw_icp,trace,no_tools=True,salvage_identity=bind))
            finally:getattr(run,'v19_unresolved',set()).discard(key)
            if filled()>=goal:break
    trace('salvage.start',{'filled':filled(),'rows':len(rows)})
    if len(chunks)>2:trace('salvage.coverage_limit',{'rows_for_model':sum(len(c) for c in chunks[:2]),
                 'rows_not_repeated':sum(len(c) for c in chunks[2:]),'reason':'two_calls_with_twenty_rows_max; main extraction already covers cache'})
    try:
        with budgets.work('target',no_tools=True):
            # Reuse complete cached verdicts before buying another model call.
            if bind:await finish(list(getattr(run,'v16_primary_candidates',{}).values()))
            for chunk in chunks[:2]:
                if filled()>=goal or run.remaining()<8:break
                prefix=(intent_guard.instruction(run.icp)+f"Company sourcing ICP: {json.dumps(run.icp,ensure_ascii=False)}. Evaluation {run.eval_date}. "
                        'Salvage companies with a dated primary proved by these cached pages. No new discovery or memory. '
                        'An official homepage identity lookup may follow; do not discard a company for that missing page. '
                        +('Domain is required as your best website guess, and will be verified; never invent companies or evidence. '
                          if v12_llm.enabled('V17_DOMAINS') else '')+
                        'For each company return company_name, domain and verdict. Decide primary event/date first, '
                        'then industry/product, HQ country, stage, employees. Each fact must quote verbatim text with row index. '
                        'Missing facts are null; country is GLOBAL HQ, not operations. '
                        'Return {"companies":[{"company_name":"","domain":"","verdict":{'
                        '"primary_status":"match|mismatch|unknown","primary":{"row":0,"quote":"event","date":"YYYY-MM-DD"},'
                        '"industry":"match|mismatch|unknown","attribute":{"row":0,"quote":"product"},'
                        '"country":{"row":0,"quote":"HQ","value":"country"},'
                        '"stage":{"row":0,"quote":"round","value":"stage"},'
                        '"employees":{"row":0,"quote":"employees","value":"stated size"}}}]}. '
                        'Rows:\n')
                trace('salvage.batch',{'rows':len(chunk),'urls':[r['url'] for r in chunk]})
                data=await cached_ask(run,'One evidence-only salvage decision. JSON only.',
                     lambda limit:prefix+json.dumps(compact_pages(chunk,limit),ensure_ascii=False),max_tokens=1800,excerpt_limit=400)
                items=[]
                for item in list_field(data,'companies'):
                    if not isinstance(item,dict) or not isinstance(item.get('verdict'),dict):continue
                    name=_clean(item.get('company_name'));host=_host(str(item.get('domain') or ''))
                    if not name or (not host and not v12_llm.enabled('V17_DOMAINS')):
                        trace('candidate.loss',{'company':name,'stage':'candidates_extracted','reason':'salvage_missing_name_or_domain'})
                        continue
                    if v12_llm.enabled('V17_DOMAINS'):host,_=v17_domains.hint(name,host,chunk)
                    if any(domain(str(x))==domain(host) for x in run.icp.get('excluded_companies',[])):continue
                    cand={'company_name':name,'domain':host,'urls':[r['url'] for r in chunk],
                          '_verdict':item['verdict'],'_verdict_rows':chunk}
                    if v12_llm.enabled('V19_TRIAGE'):
                        cand['urls']=list(dict.fromkeys(f['page']['url'] for f in grounded_facts(chunk,item['verdict']).values()))
                    items.append(cand)
                await finish(items)
    except (BudgetExhausted,v12_llm.LLMTruncated) as exc:
        trace('salvage.stop',{'reason':type(exc).__name__})
    finally:
        used=run.used['deepline']-before
        assert used<=29 if v12_llm.enabled('V17_DOMAINS') else (used<=6 if bind else used==0)
        trace('salvage.done',{'filled':filled(),'deepline_calls':used,'discovery_calls':0,
                             'identity_binding_enabled':bind,'llm_calls':run.llm_requests-calls})
    return recovered

async def run(raw_icp,*,started=None,publish=None):
    if publish and v12_llm.enabled('V33_BAND_GUARD'):
        original_publish=publish
        def publish(companies,usage):
            from agent import v33_size
            original_publish(v33_size.guard(context,companies,raw_icp,p._trace),usage)
    from agent import pipeline as p
    icp=p.normalize_icp(p.p5_normalize.discovery_input(raw_icp))
    if not icp.get('required_intents'):
        icp['required_intents']=[{'signal':str(icp.get('intent_signal') or icp.get('prompt') or 'recent activity'),
                                  'category':str(icp.get('intent_category') or ''),'max_age_days':int(icp.get('intent_max_age_days') or 365)}]
    context=p._Run(icp,started=started);context.deadline=context.started+138
    context.v15_budget=v20r2_budget.Budget(p._trace,expected=v12_llm.enabled('V20R2_EXPECTED_COST'),predict=v12_llm.enabled('V18_COST_PREDICT'),
        redirect=v12_llm.enabled('V18_REDIRECT'),shape=v12_llm.enabled('V15_BUDGET_SHAPE'),
        settled_mode=v12_llm.enabled('V16_SPEND_SETTLED'),identity_mode=v12_llm.enabled('V16_SALVAGE_IDENTITY'),
        or_only=v12_llm.enabled('V17_OR_ONLY'),domains=v12_llm.enabled('V17_DOMAINS'))
    context.budget={'deepline':29};context.v16_primary_candidates={};context.v16_rejected=set()
    context.v15_primary=set();context.v15_fit=set()
    context.v20_state={'assessed':set(),'started':set(),'admitted':set(),'eligible':set(),'decisions':{}}
    context.v17_with_domain=set();context.v17_fit_rank={};context.v17_identity_attempted=set()
    if context.tools:
        context.tools.search_text_characters=3000
        context.tools.respect_search_text_limit=v12_llm.enabled('V18_STAGE_LOOKUP')
        context.tools.settle_deepline=context.v15_budget.settle_dl
        def claim_optional(operation,probe):
            if context.remaining()<=0:raise BudgetExhausted('planning deadline exhausted')
            context.v15_budget.optional_ready=context.tools.optional_provider.state=='ready'
            ticket=context.v15_budget.claim_optional(operation,probe)
            context.used['scrapingdog']=context.used.get('scrapingdog',0)+1
            context.tool_calls+=1
            p._trace('provider.attempt',{'provider':'scrapingdog','operation':operation,'probe':probe})
            return ticket
        context.tools.before_optional_provider=claim_optional
        context.tools.settle_optional_provider=context.v15_budget.settle
    goal=v13_slots.goal(raw_icp);companies=[];candidates=[];attempted=set()
    context.v30_input=raw_icp
    context.v30_attempted=set()
    from agent import v27_policies
    if v27_policies.active(raw_icp) and not v27_policies.default_contacts(raw_icp):v27_policies.install(context,raw_icp,goal,p._trace)
    candidate_key=v17_domains.key if v12_llm.enabled('V17_DOMAINS') else lambda c:domain(c['domain'])
    def enough():
        if len(companies)<goal:return False
        if v12_llm.enabled('V20_TWO_PHASE'):return True
        stage_rank=v12_llm.enabled('V18_STAGE_LOOKUP') and v18_fit.venture(icp.get('company_stage'))
        return not v12_llm.enabled('V17_FIT_UNOBSERVED') or all(
            (context.v17_fit_rank.get(domain(c['company_website']),(0,0,0))[0]==1 and
             context.v17_fit_rank.get(domain(c['company_website']),(0,0,0))[1]>=3) if stage_rank else
            context.v17_fit_rank.get(domain(c['company_website']),(0,0))[0]>=3 for c in companies)
    def remember(c):
        c=intent_guard.output(c,icp,p._trace,context_rows=cached_rows(context))
        if v12_llm.enabled('V20R8_STRICT'):
            from agent.v20r8_strict import final_size
            c=final_size(c,icp,p._trace)
        if c and v12_llm.enabled('V20R10_EVIDENCE'):
            from agent.v20r10_evidence import broaden
            c=broaden(c,icp,cached_rows(context),p._trace)
        from agent.v30_signals import add_second
        c=add_second(c,icp,cached_rows(context),p._trace)
        if c and domain(c['company_website']) not in {domain(x['company_website']) for x in companies}:
            companies.append(c)
            if v12_llm.enabled('V17_FIT_UNOBSERVED') or v12_llm.enabled('V21_PROVABLE_FIRST'):
                companies.sort(key=lambda c:((__import__('agent.v20r9_identity',fromlist=['rank']).rank(context,c) if v12_llm.enabled('V20R9_IDENTITY') else (0,0)),v21_size.rank(context,c) if v12_llm.enabled('V21_PROVABLE_FIRST') else context.v17_fit_rank.get(domain(c['company_website']),(0,0))),reverse=True)
                del companies[goal:]
            if publish:publish(companies,p._usage(context))
    async def collect():
        nonlocal candidates
        if context._setup_error:raise context._setup_error
        niche=icp.get('sub_industry') or icp.get('industry') or ''
        country=icp.get('country') or '';stage=icp.get('company_stage') or ''
        queries=p.p5_normalize.discovery_queries(icp)+p.p5_normalize.discovery_queries(icp,deficit=True)
        queries+= [{'q':f'{niche} {country} {stage} funding announcement','mode':'news'},
                   {'q':f'{niche} {country} {stage} {icp["required_intents"][0]["signal"]}','mode':'search'},
                   {'q':f'{niche} {country} {icp["required_intents"][0]["category"]} company press release','mode':'news'}]
        query_text=list(dict.fromkeys(q['q'] for q in queries))[:6]
        stage_queries=v20_phases.stage_queries(icp,context.eval_date) if v12_llm.enabled('V20_STAGE_SUPPLY') else []
        if stage_queries:
            # Four event queries plus three stage queries: one additional call.
            query_text=query_text[:4]+stage_queries
            p._trace('stage_supply.plan',{'queries':stage_queries,'numResults':8,'text_characters':3000})
        if v12_llm.enabled('V20R9_QUERIES'):
            from agent.v20r9_queries import plan
            category_plan=plan(icp,context.eval_date);query_text=category_plan['queries'];stage_queries=category_plan['stage_queries']
            p._trace('discovery.query_plan',category_plan)
        if getattr(getattr(context.tools,'optional_provider',None),'requested',False):
            with budgets.work('discovery'):
                await context.tool('search_web',{'query':query_text[0],'limit':1})
            if context.tools.optional_provider.state!='ready':query_text=query_text[1:]
        async def search(q):
            with budgets.work('discovery'):
                await context.tool('exa_search',{'query':q[:500],'numResults':8,
                    'startPublishedDate':(context.eval_date-timedelta(days=int(icp['required_intents'][0].get('max_age_days') or 365))).isoformat(),
                    'endPublishedDate':context.eval_date.isoformat(),'contents':{'text':{'maxCharacters':3000}},
                    **({'category':'news'} if q in stage_queries else {})})
        if v12_llm.enabled('V20R10_COST'):
            from agent.v20r10_cost import discover_extract
            candidates=await discover_extract(query_text,search,lambda:cached_rows(context),
                lambda rows:extract(context,rows,p._trace),merge_candidates,p._trace)
        else:
            for q in query_text:await search(q)
            candidates=await extract(context,cached_rows(context),p._trace)
        if v12_llm.enabled('V19_TRIAGE'):
            candidates=v19_triage.rank(candidates,cached_rows(context),icp,context.eval_date,p._trace)
            context.v19_pool=candidates
            context.v19_unresolved={candidate_key(c) for c in candidates if not c['_triage']['list_only']}
        context.v17_with_domain.update(candidate_key(c) for c in candidates if c['domain'])
        p._trace('candidates.final',{'icp':icp.get('icp_id'),'n':len(candidates),'names':[c['company_name'] for c in candidates]})
        if v12_llm.enabled('V20_TWO_PHASE'):
            await v20_phases.process(context,candidates,raw_icp,p._trace,remember,lambda:len(companies),goal,
                                    slots_provable=lambda:v21_size.slots_provable(context,companies))
            from agent.v30_multi import fill
            await fill(context,raw_icp,p._trace,remember,lambda:companies,goal)
            return
        chunks=v19_triage.chunks(candidates) if v12_llm.enabled('V19_TRIAGE') else batches(candidates,3)
        for chunk in chunks:
            if enough() or context.remaining()<35 or len(context.v17_identity_attempted)>=8:break
            if chunk[0].get('_triage',{}).get('list_only') and (len(companies)>=goal or context.v19_unresolved):break
            try:
                if v12_llm.enabled('V15_TEXT_FIRST') or v12_llm.enabled('V19_STAGE_REQUIRED'):
                    assessed=await assess_batch(context,chunk)
                    if v12_llm.enabled('V17_FIT_UNOBSERVED') and not v12_llm.enabled('V19_TRIAGE'):
                        assessed.sort(key=lambda c:len(grounded_facts(c.get('_verdict_rows',[]),c.get('_verdict',{}))),reverse=True)
                    for c in assessed:
                        if enough() or (c.get('_triage',{}).get('list_only') and len(companies)>=goal):break
                        attempted.add(candidate_key(c))
                        try:remember(await candidate(context,c,raw_icp,p._trace))
                        finally:getattr(context,'v19_unresolved',set()).discard(candidate_key(c))
                else:
                    for c in chunk:
                        attempted.add(candidate_key(c))
                        legacy={**c,'evidence_url':c['urls'][0],'event_date':'','result_date':'','event':'','stage_hint':''}
                        with budgets.work('target',domain(c['domain'])):remember(await p._verify(context,legacy))
            except (BudgetExhausted,v12_llm.LLMTruncated) as exc:
                p._trace('candidate.loss',{'companies':[c['company_name'] for c in chunk],'stage':'primary_verified','reason':type(exc).__name__})
                if isinstance(exc,BudgetExhausted):break
        if len(companies)<goal:
            recovered=await salvage(context,raw_icp,p._trace,remember,lambda:len(companies),goal)
            candidates=merge_candidates(candidates,recovered)
            attempted.update(candidate_key(c) for c in recovered)
        for c in candidates:
            if candidate_key(c) not in attempted:
                p._trace('candidate.loss',{'company':c['company_name'],'stage':'primary_verified',
                                           'reason':'goal_already_filled' if len(companies)>=goal else 'not_started_before_budget_stop'})
    try:
        async with asyncio.timeout(max(0,context.remaining())):await collect()
    except (TimeoutError,BudgetExhausted,asyncio.CancelledError,v12_llm.LLMTruncated) as exc:
        p._trace('run.budget_stop',{'reason':type(exc).__name__,'n':len(companies)})
    finally:
        if any(v12_llm.enabled(f) for f in ('V30_MULTI','V30_SECOND_SIGNAL','V30_SPEND_GUARD')):
            from agent.v30_signals import finish
            companies=finish(companies,icp,cached_rows(context),p._trace)
            if publish:publish(companies,p._usage(context))
        if v27_policies.active(raw_icp) and not v27_policies.default_contacts(raw_icp):
            try:
                companies=await v27_policies.finish(context,companies,raw_icp,p._trace)
                if publish:publish(companies,p._usage(context))
            except (TimeoutError,BudgetExhausted,asyncio.CancelledError):pass
        from agent import v31_signals
        companies=await v31_signals.finish(context,companies,raw_icp,cached_rows(context),p._trace)
        if publish and any(v12_llm.enabled(f) for f in ('V31_SECOND_CRITERION','V31_SOURCE_UPGRADE')):publish(companies,p._usage(context))
        from agent import v32_size
        companies=await v32_size.finish(context,companies,raw_icp,cached_rows(context),p._trace)
        from agent import v33_size
        companies=v33_size.guard(context,companies,raw_icp,p._trace)
        if publish and (v32_size.active() or v12_llm.enabled('V33_BAND_GUARD')):publish(companies,p._usage(context))
        if v27_policies.default_contacts(raw_icp):
            try:companies=await v27_policies.finish(context,companies,raw_icp,p._trace)
            except (Exception,asyncio.CancelledError) as exc:
                for company in companies:
                    if not company.get('contact'):p._trace('contact.failed',{'company':company['company_name'],'reason':'contact_finalization_'+type(exc).__name__,'calls_used':context.v15_budget.v27_calls.get(('v27_contact',p.v9.domain(company['company_website'])),0) if hasattr(context.v15_budget,'v27_calls') else 0})
            if publish:publish(companies,p._usage(context))
        telemetry={'rows_cached':len(cached_rows(context)),'candidates_extracted':len(candidates),
                   'fit_gate_passed':len(context.v15_fit),'primary_verified':len(context.v15_primary),'slots_filled':len(companies)}
        if v12_llm.enabled('V20_TWO_PHASE'):
            state=context.v20_state
            keys={candidate_key(c) for c in candidates}
            unassessed=keys-state['assessed']
            pending=state['eligible']-state['started']
            # An assessed-but-unconfirmed primary is still unfinished work.
            unfinished=unassessed|pending if len(companies)<goal else unassessed
            for c in candidates:
                if candidate_key(c) in unfinished:
                    p._trace('candidate.loss',{'company':c['company_name'],'stage':'phase_b' if candidate_key(c) in pending else 'phase_a',
                                              'reason':'not_started_before_budget_stop'})
            telemetry.update({'triage_ranked':len(candidates),'phase_a.assessed':len(state['assessed']),
                'phase_b.started':len(state['started']),'phase_b.admitted':len(state['admitted']),
                'phase_b.eligible':len(state['eligible']),'phase_a.unassessed':len(unassessed),
                'phase_b.not_started':len(pending),'not_started_before_budget_stop':len(unfinished)})
        if v12_llm.enabled('V17_DOMAINS'):telemetry['candidates_with_domain']=len(context.v17_with_domain)
        if hasattr(context,'v21_size_state'):
            records=context.v21_size_state['records']
            returned=[records.get(domain(c['company_website']),{}) for c in companies]
            for c,r in zip(companies,returned):
                p._trace('provability.returned',{'company':c['company_name'],**r,'employee_count':c['employee_count'],
                         'fit_evidence_urls':c['fit_evidence_urls']})
            telemetry.update({'size_evidence.non_linkedin_returned':sum(bool(r.get('non_linkedin_proven')) for r in returned),
                              'size_evidence.provable_returned':sum(bool(r.get('provable')) for r in returned)})
        if v12_llm.enabled('V20R4_HEADCOUNT_HINT'):telemetry['headcount_conflict_margin_saved']=hc.saved(context)
        if v12_llm.enabled('V20R7_ALLOCATION'):
            telemetry.update({'confirmed_from_'+k:v for k,v in getattr(context,'v20r7_confirmed',{'T0':0,'T1':0,'T2':0}).items()})
        p._trace('funnel',telemetry)
        p._trace('money.funnel',context.v15_budget.snapshot())
        if v12_llm.enabled('V20R10_COST'):
            from agent.v20r10_cost import summary,eligibility
            cost=summary(context,len(companies));p._trace('cost.summary',cost)
            p._trace('cost.eligibility',eligibility(cost['sourcing_usd_upper_bound'],len(companies),billing_resolved=cost['billing_resolved']))
        p._trace('submit',{'icp':icp.get('icp_id'),'n':len(companies),'names':[c['company_name'] for c in companies],
                           'used':context.used,'llm':context.llm_requests,'sec':round(p._now()-context.started,1)})
        p._trace('llm.funnel',{k:v for k,v in p._usage(context).items() if k.startswith('llm_')})
        try:await asyncio.wait_for(context.close(),timeout=5)
        except Exception:pass
        p.LAST_USAGE.clear();p.LAST_USAGE.update(p._usage(context))
    return companies[:goal]
