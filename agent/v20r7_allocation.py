"""Reserve confirmation before buying missing event/stage evidence."""
import re
from agent import v12_llm,v15_budget,v18_fit,v19_triage,v20r5_primary as primary,v20r6_supply,v21_size
from agent.deadline import BudgetExhausted

LOOKUPS=9
EVENTS=4
STAGES=5
IDENTITY=10
P1=3


def claim(m,operation):
    lane,company,verified,no_tools=v15_budget.WORK.get()
    with m.lock:
        used=sum(m.lanes.values());proofs=getattr(m,'r7_proof_calls',0)
        reserve=getattr(m,'r7_confirmation_reserve',0)
        identity=lane in ('v18_identity','v17_identity','identity','salvage_identity')
        events=getattr(m,'r7_events',set());stages=getattr(m,'r7_stages',set())
        if lane in ('v18_stage','v20r6_event'):
            chosen,cap=(events,EVENTS) if lane=='v20r6_event' else (stages,STAGES)
            if no_tools or not verified or operation!='exa_search':raise BudgetExhausted('qualified lookup required')
            if company in chosen or len(chosen)>=cap or len(events)+len(stages)>=LOOKUPS:
                raise BudgetExhausted('R7 lookup cap')
            if used+1>29-IDENTITY-P1:raise BudgetExhausted('R7 confirmation and P1 reserved')
            ticket=m._reserve('deepline',v15_budget.DL_CEILINGS[operation])
            chosen.add(company);m.r7_events=events;m.r7_stages=stages
            if lane=='v18_stage':m.stage_lookups.add(company)
            m.lanes['reserve']+=1
            m.trace('budget.claim',{'lane':lane,'company':company,'operation':operation,'confirmation_reserved':IDENTITY,'p1_reserved':P1})
            return ticket
        if identity:
            identities=getattr(m,'r7_identities',{})
            if sum(identities.values())>=IDENTITY or identities.get(company,0)>=2:
                raise BudgetExhausted('R7 two identity calls per company; ten total')
        if identity or lane=='profile':
            if used+1>29-reserve-max(0,P1-proofs):raise BudgetExhausted('R7 later confirmation reserved')
        if lane in ('v21_size','v21_linkedin'):
            if proofs>=P1 or used+1>29-reserve:raise BudgetExhausted('R7 three proof calls maximum')
        ticket=m._claim_dl(operation)
        if identity:identities[company]=identities.get(company,0)+1;m.r7_identities=identities
        if lane in ('v21_size','v21_linkedin'):m.r7_proof_calls=proofs+1
        return ticket


def event_hint(c):
    # Operating capability/expansion wording is a lookup priority, not proof.
    text=str(c['_phase_a']['stage_fact'].get('page',{}).get('text',''))
    return bool(re.search(r'\b(?:is expanding|now includes|now a platform|expanding its)\b|\bas\s+'+
        re.escape(c['company_name'])+r'\s+expands\b',text,re.I))


def tiers(candidates):
    result={n:[] for n in ('T0','T1','T2','T3')}
    for c in candidates:
        d=c['_phase_a'];event=d['primary_from_cache'] and not d['drop_reason'] and d['complete']
        stage=bool(d['components'].get('affirmed_stage_from_cache'))
        tier='T0' if event and stage else 'T1' if d.get('event_recoverable') else 'T2' if event else 'T3'
        c['_allocation_tier']=tier;result[tier].append(c)
    for values in result.values():
        # Older affirmed rounds have had more time for a subsequent rollout;
        # no recorded event-lookup outcomes or company whitelist enter ranking.
        values.sort(key=lambda c:(-sum(c['_phase_a']['components'].values()),
            -int(event_hint(c)) if c['_allocation_tier']=='T1' else 0,
            str(c['_phase_a']['stage_fact'].get('date') or '9999'),c['company_name'].casefold()))
    return result


async def process(run,raw_icp,trace,remember,filled,goal):
    from agent import v15_pipeline as p
    state=run.v20_state;meter=run.v15_budget;t=tiers(list(state['decisions'].values()))
    if v12_llm.enabled('V20R10_COST'):
        for c in t['T3']:trace('allocation.lookup_skipped',{'company':c['company_name'],'tier':'T3','reason':'neither_category_nor_affirmed_stage'})
    target=min(5,goal);ready=t['T0'][:target]
    skip_stage=v18_fit.category(run.icp.get('company_stage'))=='Series C+' and len(t['T0'])+len(t['T1'])>=5
    trace('allocation.plan',{'tiers':{k:len(v) for k,v in t.items()},'identity_reserved':10,'p1_reserved':3,
        'discovery':7,'lookup_cap':9,'event_cap':4,'stage_cap':5,'skip_stage_supply_sufficient':skip_stage,
        't1_tie_break':'A-score, cached operating-change hint, oldest completed round, company name'})
    def stop(reason):trace('allocation.stop',{'reason':reason,'confirmable':len(ready),'used':run.used['deepline']})
    def room():
        if len(ready)>=target:stop('five_confirmable');return False
        if run.remaining()<25:stop('confirmation_time_reserved');return False
        if run.used['deepline']+1>16:stop('confirmation_calls_reserved');return False
        return True
    for c in t['T1']:
        if not room():break
        if len(getattr(run,'v20r6_events',set()))>=EVENTS:stop('event_cap');break
        recovered=await v20r6_supply.event_lookup(run,c,raw_icp,trace)
        if recovered:
            recovered['_allocation_tier']='T1';ready.append(recovered)
    if skip_stage:stop('series_c_cache_supply_sufficient')
    else:
        for c in t['T2']:
            if not room():break
            if len(meter.stage_lookups)>=STAGES:stop('stage_cap');break
            if v12_llm.enabled('V20R9_RECENCY') and v18_fit.category(run.icp.get('company_stage')) in ('Seed','Series A','Series B') and len(meter.stage_lookups)+len(ready)+2>STAGES:
                stop('t2_recency_reserved');break
            screen=await v18_fit.stage_lookup(run,c,p.cached_rows(run)+c.get('_verdict_rows',[]),trace,require_announcement=True)
            if screen['decision']=='match' and v19_triage.affirmed(c,screen['fact']):
                c['_phase_a']['stage_fact']=screen['fact'];ready.append(c)
    if v12_llm.enabled('V20R9_RECENCY'):
        from agent.v20r9_recency import check
        checked=[]
        for c in ready:
            if await check(run,c,trace):checked.append(c)
        ready=checked
    prepared=[]
    # All identities are attempted before optional P1. Profile calls must leave
    # two calls for every later confirmation and three for proof enrichment.
    for index,c in enumerate(ready):
        if run.remaining()<8:stop('confirmation_deadline');break
        meter.r7_confirmation_reserve=2*(len(ready)-index-1)
        key=p.v17_domains.key(c);state['decisions'][key]=c;state['eligible'].add(key);state['started'].add(key)
        trace('phase_b.started',{'company':c['company_name'],'domain':key,'tier':c['_allocation_tier']})
        try:
            result=await p.candidate(run,c,raw_icp,trace,defer_profile=True)
            prepared.append((c,key,result))
        except (BudgetExhausted,v12_llm.LLMTruncated):stop('confirmation_budget');continue
    meter.r7_confirmation_reserve=0
    counts=getattr(run,'v20r7_confirmed',None)
    if counts is None:counts=run.v20r7_confirmed={k:0 for k in ('T0','T1','T2')}
    for c,key,result in prepared:
        if filled()>=goal:break
        try:
            if callable(result):result=await result()
            if result and any(v12_llm.enabled(f) for f in ('V21_SIZE_EVIDENCE','V21_LINKEDIN_CHECK','V21_PROVABLE_FIRST')):
                result=await v21_size.enrich(run,c,result,raw_icp,trace)
            result=primary.output(result,run.icp,trace,primary=c.get('_phase_a_primary'))
            if v12_llm.enabled('V20R8_STRICT'):
                from agent.v20r8_strict import final_size
                result=final_size(result,run.icp,trace,cand=c,run=run)
            if result:
                remember(result);state['admitted'].add(key);counts[c['_allocation_tier']]+=1
                trace('phase_b.admitted',{'company':c['company_name'],'domain':key,'tier':c['_allocation_tier'],
                    'primary_quote':result['intent_signals'][0]['snippet']})
        except (BudgetExhausted,v12_llm.LLMTruncated):stop('proof_budget')
    stop('confirmation_complete')
