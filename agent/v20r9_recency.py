"""One recent-round search, charged to the existing T2 lookup allowance."""
import re
from datetime import timedelta,date,datetime
from agent import v18_fit,v15_budget,v17_domains,v12_llm
from agent.deadline import BudgetExhausted

def latest(cand,rows,evaluation_date):
    from agent.pipeline import _date_in_text
    found=v18_fit.observations(cand,rows,evaluation_date)
    for f in found:
        explicit=_date_in_text(f['quote'])
        month=re.search(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d\d)\b',f['quote'],re.I)
        year=re.search(r'\bin\s+(20\d\d)\b',f['quote'],re.I)
        f['event_date']=explicit or (datetime.strptime(month[0],'%B %Y').date() if month else date(int(year[1]),1,1) if year else f['date'])
    return max((f for f in found if not f['event_date'] or f['event_date']<=evaluation_date),key=lambda f:(f['event_date'] or date.min,v18_fit.STAGES.index(f['value'])),default=None)

async def check(run,cand,trace):
    wanted=v18_fit.category(run.icp.get('company_stage'))
    if wanted not in ('Seed','Series A','Series B'):return True
    cache=getattr(run,'v20r9_recency',None)
    if cache is None:cache=run.v20r9_recency={}
    key=v17_domains.key(cand)
    if key in cache:return cache[key]
    soft_t1=v12_llm.enabled('V20R11_T1_RECENCY') and cand.get('_allocation_tier')=='T1'
    def finish(ok,reason,fact=None):
        cache[key]=ok
        trace('stage.recency',{'company':cand['company_name'],'decision':'match' if ok else 'drop','reason':reason,
          'wanted':wanted,'latest':(fact or {}).get('value'),'event_date':str((fact or {}).get('event_date') or ''),
          'quote':(fact or {}).get('quote',''),'url':(fact or {}).get('page',{}).get('url','')})
        if ok and fact:
            cand['_phase_a']['stage_fact']=fact
            # Replace the earlier cached screen with the latest observation.
            getattr(run,'v18_stage_cache',{}).pop(key,None)
        return ok
    from agent.v15_pipeline import cached_rows
    def proven_later(rows):
        facts=v18_fit.observations(cand,rows,run.eval_date)
        return next((f for f in sorted(facts,key=lambda f:v18_fit.STAGES.index(f['value']),reverse=True)
                     if v18_fit.STAGES.index(f['value'])>v18_fit.STAGES.index(wanted)),None)
    def uncertain(reason,fact=None):
        if soft_t1:
            proof=proven_later(cached_rows(run)+list(cand.get('_verdict_rows') or []))
            if proof:return finish(False,'latest_stage_conflict',proof)
            # Keep the already affirmed cached stage; uncertainty adds no fact.
            return finish(True,'t1_retained_'+reason)
        return finish(False,reason,fact)
    if run.remaining()<12:return uncertain('recency_deadline')
    query='"'+cand['company_name'].replace('"','')+'" (raises OR raised) Series'
    try:
        with v15_budget.work('v18_stage',key+':recency',primary=True):
            result=await run.tool('exa_search',{'query':query,'category':'news','numResults':6,
              'startPublishedDate':(run.eval_date-timedelta(days=730)).isoformat(),'endPublishedDate':run.eval_date.isoformat(),
              'contents':{'text':{'maxCharacters':2000}}})
    except (BudgetExhausted,TimeoutError):return uncertain('recency_budget_or_timeout')
    except Exception as exc:
        from agent.v20r2_errors import exception_provider_error
        if not soft_t1 or exception_provider_error(exc) is not None:raise
        return uncertain('recency_lookup_failure')
    if not isinstance(result,dict) or result.get('error') or result.get('ok') is False:return uncertain('recency_lookup_unavailable')
    from agent.v15_pipeline import cached_rows
    rows=cached_rows(run)+list(cand.get('_verdict_rows') or [])+list(result.get('results') or [])
    if soft_t1:
        proof=proven_later(rows)
        if proof:return finish(False,'latest_stage_conflict',proof)
    fact=latest(cand,rows,run.eval_date)
    if not fact:return uncertain('recency_unobserved')
    if v18_fit.category(fact['value'])!=wanted:return uncertain('older_round_only',fact) if soft_t1 else finish(False,'latest_stage_conflict',fact)
    from agent.v19_triage import later_hint
    if later_hint(cand,rows,wanted):
        # A later-stage headline is not enough to affirm a round, but it is
        # enough to withhold an older claim until the conflict is resolved.
        return uncertain('later_stage_hint_unresolved',fact)
    if not fact['event_date']:return uncertain('round_date_unobserved',fact)
    if wanted in ('Seed','Series A') and (run.eval_date-fact['event_date']).days>1095:
        return uncertain('latest_round_stale',fact)
    return finish(True,'latest_completed_round',fact)
