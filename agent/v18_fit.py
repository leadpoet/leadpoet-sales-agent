"""Bounded stage observations and explicitly unobserved headcount priors."""
import re
from agent import judge_mirror,v15_budget,v15_stage_proof,v18_budget
from agent.evidence import _clean,_parse_date,BUCKETS
from agent.v92_common import domain
from agent.deadline import BudgetExhausted

STAGES=('Seed','Series A','Series B','Series C+','Private Equity','Public')

def category(value):
    value=str(value or '').strip().lower()
    if value in ('seed','pre-seed','pre seed'):return 'Seed'
    if re.fullmatch(r'series\s+[c-z]\+?',value):return 'Series C+'
    return next((s for s in STAGES if s.lower()==value),'')

def venture(value):return category(value) in STAGES[:4]

def bucket(value):
    # A profile bin ceiling is not permission to move into a larger ICP bin.
    return (judge_mirror.normalize_employee_count_bucket(value,default=None)
            or judge_mirror.normalize_observed_employee_count_bucket(value,default=None))

def cached_headcount_hint(cand,rows):
    hints=[]
    for row in rows:
        if domain(row.get('url'))!=domain(cand.get('domain')):continue
        entity=row.get('entity') or {}
        workforce=entity.get('workforce') or {}
        observed=bucket(workforce.get('total')) if isinstance(workforce,dict) else None
        if observed:hints.append(observed)
    return min(hints,key=BUCKETS.index) if hints else None

def prior(wanted,allowed,hint=None):
    choices=sorted(set(allowed),key=BUCKETS.index)
    stage=category(wanted)
    index=0 if stage in ('Seed','Series A') else min(1,len(choices)-1) if stage=='Series B' else len(choices)//2
    selected=choices[index];ceiling=bucket(hint)
    if ceiling and BUCKETS.index(selected)>BUCKETS.index(ceiling):
        eligible=[b for b in choices if BUCKETS.index(b)<=BUCKETS.index(ceiling)]
        return eligible[-1] if eligible else None
    return selected

def observations(cand,rows,evaluation_date):
    """Only affirmed, company-attributed sentences, never bare stage keywords."""
    names=list(dict.fromkeys([cand['company_name'],cand.get('_bound_name') or cand['company_name']]))
    patterns=[re.compile(r'(?<!\w)'+r'\s*'.join(map(re.escape,n.split()))+r'(?!\w)',re.I) for n in names if len(n)>=3]
    found=[]
    for row in rows:
        published=_parse_date(str(row.get('date') or row.get('publishedDate') or '')[:10])
        if published and published>evaluation_date:continue
        text=str(row.get('text') or '')
        for part in re.split(r'(?<=[.!?;])\s+|\n+',text):
            quote=_clean(part)
            if not quote or len(quote)>1000:continue
            match=next((m for pattern in patterns if (m:=pattern.search(quote))),None)
            own=domain(row.get('url'))==domain(cand.get('domain'))
            if not match and not (own and re.match(r'(?i)^we\s+(?:have\s+)?(?:raised|closed|secured|completed|announced|received)\b',quote)):continue
            if match:
                prefix=quote[:match.start()];tail=quote[match.end():]
                # Another firm's raise with the candidate as investor/customer
                # is not this company's stage; neither is an investor's round.
                if re.search(r'\b(?:raised|closed|secured|completed|received)\b',prefix,re.I):continue
                if re.match(r"(?i)\s*(?:'s|’s)?\s*(?:investor|partner|customer|client|supplier)\b",tail):continue
                if re.match(r'(?i)\s+(?:has\s+)?acquired\s+(?!by\b)',tail):continue
                if re.search(r'\b(?:partner|customer|client|investor|supplier)\b.{0,80}\b(?:raised|closed|secured)\b',tail,re.I):continue
                # Limit the assertion to the named company's clause.
                quote=quote if re.search(r'(?i)\bacquired\b',prefix) else quote[match.start():]
                quote=re.split(r'\b(?:while|whereas|but)\b|,\s+and\s+(?=[A-Z])',quote,maxsplit=1)[0].strip()
            for stage in STAGES:
                if v15_stage_proof.submit_stage_quote(stage,quote):
                    found.append({'value':stage,'quote':quote,'page':row,'date':published})
    return found

def latest(cand,rows,evaluation_date,*,wanted='',require_announcement=False):
    found=observations(cand,rows,evaluation_date)
    # Funding rounds advance; old Seed articles cannot undo a later Series A.
    highest=max((STAGES.index(f['value']) for f in found),default=-1)
    if require_announcement and highest>=0 and STAGES[highest]==wanted:
        # A newer syndication of the same round cannot erase a valid original
        # announcement. A genuinely later stage still wins, from any source.
        found=[f for f in found if f['value']==wanted and
               v15_stage_proof.announcement_source(f['page']['url'],cand['domain'])]
    return max(found,key=lambda f:(STAGES.index(f['value']),str(f['date'] or '')),default=None)

async def stage_lookup(run,cand,rows,trace,*,actual='',stage_fact=None,require_announcement=False):
    wanted=category(run.icp.get('company_stage'))
    if not venture(wanted):return {'decision':'skip','actual':actual,'fact':stage_fact or {}}
    cache=getattr(run,'v18_stage_cache',None)
    if cache is None:run.v18_stage_cache={};cache=run.v18_stage_cache
    key=domain(cand['domain'])
    if cand.get('_phase_a'):
        from agent.v17_domains import key as candidate_key
        key=candidate_key(cand)
    if key in cache:return cache[key]
    observed=latest(cand,rows,run.eval_date,wanted=wanted,require_announcement=require_announcement)
    if category(actual) and stage_fact:
        current={'value':category(actual),**stage_fact}
        current['value']=category(actual)
        if (not observed or STAGES.index(current['value'])>STAGES.index(observed['value']) or
            (current['value']==observed['value'] and require_announcement and
             v15_stage_proof.announcement_source(current['page']['url'],cand['domain']))):observed=current
    def admissible(observation):
        return (not require_announcement or not observation or category(observation['value'])!=wanted or
                v15_stage_proof.announcement_source(observation['page']['url'],cand['domain']))
    if not admissible(observed):observed=None
    origin='cache';status='observed' if observed else 'not_attempted';count=0
    from agent import v12_llm
    if v12_llm.enabled('V20R6_PLAUSIBLE'):
        from agent.v20r6_supply import plausibility
        plausible,why=plausibility(cand,rows)
        if plausible is False:
            trace('stage.lookup_skipped',{'company':cand['company_name'],'reason':why,'venture_plausible':False,
                  'cached_stage_available':bool(observed),'scope':'new_paid_lookup_only'})
            status='skipped'
            if not observed:
                return {'decision':'unobserved','actual':'','fact':{},'origin':'plausibility','lookup_status':'skipped'}
    if not observed:
        meter=run.v15_budget
        if len(meter.stage_lookups)>=v18_budget.STAGE_LOOKUP_CAP or sum(meter.lanes.values())>=29-v18_budget.STAGE_CONFIRM_RESERVE:
            status='budget_exhausted'
        elif run.remaining()<8:status='deadline'
        else:
            query='"'+cand['company_name'].replace('"','')+'" funding round'
            if wanted in ('Seed','Series A'):
                query='"'+cand['company_name'].replace('"','')+'" (seed OR pre-seed OR "Series A") raised'
            before=len(meter.stage_lookups)
            try:
                with v15_budget.work('v18_stage',key,primary=True):
                    result=await run.tool('exa_search',{'query':query,'numResults':6,
                        'contents':{'text':{'maxCharacters':2000}}})
                lookup_rows=result.get('results') or []
                observed=latest(cand,lookup_rows,run.eval_date,wanted=wanted,require_announcement=require_announcement)
                if not admissible(observed):observed=None
                status='error' if result.get('error') or result.get('ok') is False else ('ok_nonempty' if lookup_rows else 'empty')
                count=len(lookup_rows)
            except BudgetExhausted:status='budget_exhausted'
            origin='lookup'
            trace('stage.lookup',{'company':cand['company_name'],'domain':key,'query':query,'status':status,
                'search_scope':'news_and_own_newsroom; unrestricted web search, announcement-source acceptance','rows':count,'physical_calls':len(meter.stage_lookups)-before})
    decision='unobserved'
    if observed:decision='match' if category(observed['value'])==wanted else 'conflict'
    result={'decision':decision,'actual':observed['value'] if observed else '',
            'fact':observed or {},'origin':origin,'lookup_status':status}
    cache[key]=result
    trace('stage.screen',{'company':cand['company_name'],'domain':key,'wanted':wanted,
        'observed':result['actual'],'decision':decision,'origin':origin,'lookup_status':status,
        'quote':(observed or {}).get('quote',''),'url':(observed or {}).get('page',{}).get('url','')})
    return result
