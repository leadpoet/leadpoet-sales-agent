"""Fill remaining slots after the original admission cohort, never displacing it."""
from agent import v12_llm,v17_domains,v21_size,v30_provability as proof
from agent.deadline import BudgetExhausted

def candidates(run,companies):
    admitted={proof.domain(c['company_website']) for c in companies};names={c['company_name'].casefold() for c in companies};tried=getattr(run,'v30_attempted',set());found=[]
    for cand in run.v20_state['decisions'].values():
        key=v17_domains.key(cand);d=cand.get('_phase_a') or {}
        if key in tried or cand['company_name'].casefold() in names or proof.domain(cand.get('domain')) in admitted:continue
        if not d.get('complete') or not d.get('primary_from_cache') or d.get('drop_reason'):continue
        score=proof.inspect(cand,proof.rows(run,cand),run.v30_input,run.eval_date)['score']
        if score>=2:found.append((cand,d.get('score') or 0,score))
    return sorted(found,key=lambda x:(-x[1],-x[2],x[0]['company_name'].casefold()))

async def fill(run,raw_icp,trace,remember,current,goal):
    if not v12_llm.enabled('V30_MULTI'):return
    from agent import v15_pipeline as p
    limit=min(5,goal);run.v30_input=raw_icp
    if not hasattr(run,'v30_attempted'):run.v30_attempted=set()
    baseline={proof.domain(c['company_website']) for c in current()}
    pool=candidates(run,current())
    trace('multi.plan',{'baseline':len(baseline),'eligible_extra':len(pool),'goal':limit,'order':'A-score then provability'})
    for cand,score,provability in pool:
        if len(current())>=limit:break
        if 29-run.used['deepline']<2 or run.remaining()<12:
            trace('multi.stop',{'reason':'confirmation_calls_or_time_reserved','deepline_remaining':29-run.used['deepline']});break
        key=v17_domains.key(cand);run.v30_attempted.add(key)
        run.v20_state['started'].add(key)
        # Preserve the original cohort; only unfilled slots may use this pass.
        trace('multi.started',{'company':cand['company_name'],'a_score':score,'provability':provability})
        try:
            cached_home,_=p.identity_from_cache(run,cand)
            result=await p.candidate(run,cand,raw_icp,trace,no_tools=bool(cached_home))
            if result and any(v12_llm.enabled(f) for f in ('V21_SIZE_EVIDENCE','V21_LINKEDIN_CHECK','V21_PROVABLE_FIRST')):
                result=await v21_size.enrich(run,cand,result,raw_icp,trace)
            if result:
                from agent.v20r5_primary import output
                result=output(result,run.icp,trace,primary=cand.get('_phase_a_primary'))
            if result:
                remember(result)
                if proof.domain(result['company_website']) in {proof.domain(c['company_website']) for c in current()}:
                    run.v20_state['admitted'].add(key)
                    trace('multi.admitted',{'company':result['company_name'],'provability':provability})
        except (BudgetExhausted,v12_llm.LLMTruncated,TimeoutError) as exc:
            trace('multi.stop',{'company':cand['company_name'],'reason':type(exc).__name__})
    assert baseline<={proof.domain(c['company_website']) for c in current()},'additional confirmation displaced original admissions'
