"""Reserve fresh sourcing calls for candidates with usable cached evidence."""
from agent import v12_llm,v15_budget,v18_fit,v19_triage,v30_provability as proof
from agent.v92_common import domain
from agent.deadline import BudgetExhausted

LANES={'identity','v17_identity','v18_identity','salvage_identity','profile','v21_size','v21_linkedin','v18_stage'}
class UnprovableSpend(BudgetExhausted):pass

def evidence(run,cand):
    pages=proof.rows(run,cand)
    score=proof.inspect(cand,pages,run.v30_input,run.eval_date)['score']
    stage=bool((cand.get('_phase_a') or {}).get('components',{}).get('affirmed_stage_from_cache'))
    if not stage:stage=bool(v18_fit.latest(cand,pages,run.eval_date,wanted=run.icp.get('company_stage'),require_announcement=True))
    hq=v19_triage.hq_match(cand,pages,run.icp.get('country',''))
    return {'score':score,'stage_proven':stage,'hq_proven':bool(hq),'cache_only':score==0 and not stage and not hq}

def locate(run,key):
    key=key.removesuffix(':recency')
    from agent.v17_domains import key as candidate_key
    candidates=list(getattr(run,'v20_state',{}).get('decisions',{}).values())+list(getattr(run,'v19_pool',[]))
    return next((c for c in candidates if candidate_key(c)==key or c.get('domain')==key or domain(c.get('domain'))==key or 'name:'+c['company_name'].casefold()==key),None)

def identity_completed(run,cand,home):
    """Commit the whole initial identity procedure, including its fallback fetch."""
    if not v12_llm.enabled('V30_SPEND_GUARD') or not hasattr(run,'v30_input'):return
    if home and home.get('url'):
        run.page_cache[home['url']]=home
        cand['_v30_identity_completed']=True


def cache_only(run,cand):
    return bool(v12_llm.enabled('V30_SPEND_GUARD') and hasattr(run,'v30_input') and
                cand.get('_v30_identity_completed') and evidence(run,cand)['cache_only'])

def before_call(run,operation,trace):
    if not v12_llm.enabled('V30_SPEND_GUARD') or not hasattr(run,'v30_input'):return
    lane,key,_,_=v15_budget.WORK.get()
    if lane not in LANES:return
    cand=locate(run,key)
    if not cand or not cand.get('_v30_identity_completed'):return
    observation=evidence(run,cand)
    if observation['cache_only']:
        trace('spend.skipped_unprovable',{'company':cand['company_name'],'lane':lane,'operation':operation,'after_identity':True,**observation})
        raise UnprovableSpend('unprovable candidate: cached evidence only')
