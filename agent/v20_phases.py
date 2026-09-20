"""Assess the whole cache before spending on company confirmation."""
from agent import v20r4_headcount as hc, v18_fit, v19_triage, v12_llm, judge_mirror, v21_size, v20r5_primary as intent, v20r8_strict as strict
from agent.evidence import _parse_date, _country_ok
from agent.v92_common import observed_date, signal_url_ok
from agent.deadline import BudgetExhausted

BATCH_SIZE = 6
CONFIRM_RESERVE = 3


def stage_queries(icp, evaluation_date):
    stage = v18_fit.category(icp.get('company_stage'))
    if not v18_fit.venture(stage):
        return []
    industry = icp.get('industry') or ''
    niche = icp.get('sub_industry') or industry
    country = icp.get('country') or ''
    term = 'Series C' if stage == 'Series C+' else stage
    return [f'{niche} startup raises {term} funding {country}',
            f'"{industry} startup" "{term}" {evaluation_date.year}',
            f'{niche} raises {term}']


def cache_decision(run, cand, raw_icp):
    from agent import v15_pipeline as p, pipeline
    verdict = cand.get('_verdict') or {}
    rows = cand.get('_verdict_rows') or []
    facts = p.grounded_facts(rows, verdict)
    parts = {}; reason = ''; primary = facts.get('primary'); fit_conflicts=[]
    cand.pop('_phase_a_primary', None)
    if verdict.get('primary_status') == 'mismatch':
        reason = 'event_conflict'
    elif verdict.get('primary_status') != 'match' and primary:
        reason = 'category_primary_unobserved'
    elif primary and v12_llm.enabled('V20R10_HIRING') and not __import__('agent.v20r10_evidence',fromlist=['hiring_ok']).hiring_ok(run.icp,primary['quote'],primary['page']):
        reason = 'hiring_source_not_open_or_linkedin'
    elif primary and intent.funding_mismatch(run.icp, primary['quote']):
        reason = 'funding_primary_for_nonfunding'
    elif primary and v12_llm.enabled('V20R8_STRICT') and not strict.subject(cand,primary['quote'],primary['page']):
        reason = 'primary_subject_unverified'
    elif primary and v12_llm.enabled('V20R9_INTENT') and not __import__('agent.v20r9_intent',fromlist=['matches']).matches(run.icp,primary['quote'],primary['page']):
        reason = 'literal_category_not_established'
    elif primary:
        page = primary['page']; when = _parse_date(primary.get('date'))
        observed = observed_date(page)
        if not when or (observed and _parse_date(observed) != when):
            reason = 'event_date_not_supported'
        elif not observed and pipeline._date_in_text(page.get('text', '')) != when:
            reason = 'event_date_unobserved'
        elif not 0 <= (run.eval_date - when).days <= int(run.icp['required_intents'][0].get('max_age_days') or 365):
            reason = 'event_date_outside_window'
        elif not signal_url_ok(page['url']):
            reason = 'invalid_event_source'
        else:
            parts['primary_from_cache'] = 4
            intent.freeze(cand, primary)
    if verdict.get('industry') == 'mismatch':
        fit_conflicts.append('industry_conflict')
        reason = reason or 'industry_conflict'
    want = v18_fit.category(run.icp.get('company_stage'))
    selected = v19_triage.associated(cand, p.cached_rows(run))
    stage = v18_fit.latest(cand, selected, run.eval_date, wanted=want, require_announcement=True)
    if stage and want:
        if v18_fit.category(stage['value']) != want:
            fit_conflicts.append('stage_conflict')
            reason = reason or 'stage_conflict'
        elif v19_triage.affirmed(cand, stage):
            parts['affirmed_stage_from_cache'] = 3
    country = facts.get('country', {}).get('value')
    if country and _country_ok(country, run.icp.get('country', '')) is False:
        fit_conflicts.append('cached_country_conflict')
        reason = reason or 'cached_country_conflict'
    elif country and _country_ok(country, run.icp.get('country', '')) is True:
        parts['country_from_cache'] = 1
    elif v19_triage.hq_match(cand, selected, run.icp.get('country', '')):
        parts['country_from_cache'] = 1
    band = v18_fit.bucket(facts.get('employees', {}).get('value'))
    allowed = judge_mirror.employee_count_buckets_for_icp(raw_icp)
    if band and band not in allowed:
        from agent.pipeline import _trace
        if not v12_llm.enabled('V20R4_HEADCOUNT_HINT') or hc.inspect(run,cand,facts['employees']['value'],allowed,_trace)['conflict']:
            fit_conflicts.append('cached_headcount_conflict')
            reason = reason or 'cached_headcount_conflict'
    hint = band or v19_triage.headcount_hint(cand, selected)
    if hint in allowed:
        parts['headcount_hint_in_band'] = 1
    complete = verdict.get('primary_status') in ('match', 'mismatch', 'unknown') and verdict.get('industry') in ('match', 'mismatch', 'unknown')
    decision={'score': None if reason else sum(parts.values()), 'components': parts,
            'primary_from_cache': bool(parts.get('primary_from_cache')), 'drop_reason': reason,
            'complete': complete, 'headcount_hint': hint, 'stage_fact': stage or {}}
    if v12_llm.enabled('V20R6_EVENT_LOOKUP'):
        decision['event_recoverable']=bool(complete and not parts.get('primary_from_cache') and
            parts.get('affirmed_stage_from_cache') and not fit_conflicts and intent.category(run.icp)!='FUNDING')
    if v12_llm.enabled('V20R6_PLAUSIBLE'):
        from agent.v20r6_supply import plausibility
        value,why=plausibility(cand,selected+rows)
        decision.update(venture_plausible=value,venture_plausible_reason=why)
    return decision


async def stage_supply(run,eligible,trace,*,fallback=(),raw_icp=None):
    """Screen in A-score order, reserving confirmation calls for five proofs."""
    from agent import v15_pipeline as p
    accepted=[]
    for cand in eligible:
        if len(accepted)>=5 or run.remaining()<12:break
        try:
            rows=p.cached_rows(run)+list(cand.get('_verdict_rows') or [])
            screen=await v18_fit.stage_lookup(run,cand,rows,trace,require_announcement=True)
        except BudgetExhausted:
            break
        if screen['decision']=='match' and v19_triage.affirmed(cand,screen['fact']):
            accepted.append(cand)
            trace('stage.supply',{'company':cand['company_name'],'affirmed':len(accepted),'goal':5})
        else:
            trace('candidate.loss',{'company':cand['company_name'],'stage':'stage_supply',
                'reason':'stage_conflict_lookup' if screen['decision']=='conflict' else 'stage_required_unobserved',
                'lookup_status':screen.get('lookup_status')})
    # Funding-only rows cannot displace the category-matching list. Recover an
    # event only after that list has been exhausted and fewer than five remain.
    if len(accepted)<5 and v12_llm.enabled('V20R6_EVENT_LOOKUP') and intent.category(run.icp)!='FUNDING':
        from agent.v20r6_supply import event_lookup
        for cand in fallback:
            if len(accepted)>=5:break
            recovered=await event_lookup(run,cand,raw_icp,trace)
            if recovered:
                accepted.append(recovered)
                key=p.v17_domains.key(recovered)
                run.v20_state['decisions'][key]=recovered
                run.v20_state['eligible'].add(key)
    return accepted


async def process(run, candidates, raw_icp, trace, remember, filled, goal, *, slots_provable=None):
    from agent import v15_pipeline as p, v15_budget
    state = run.v20_state
    before = run.used['deepline']
    try:
        for chunk in p.batches(candidates, BATCH_SIZE):
            if getattr(run,'v20r2_phase_a_exhausted',False) or run.remaining() < 8:
                break
            trace('phase_a.batch', {'size': len(chunk), 'cache_only': True})
            # Deny every provider lane; OpenRouter has its independent meter.
            with v15_budget.work('phase_a', no_tools=True):
                assessed = await p.assess_batch(run, chunk)
            if getattr(run,'v20r2_phase_a_exhausted',False):
                break
            for cand in assessed:
                key = p.v17_domains.key(cand)
                decision = cache_decision(run, cand, raw_icp)
                cand['_phase_a'] = decision
                state['decisions'][key] = cand
                if decision['complete']:
                    state['assessed'].add(key)
                    if decision['primary_from_cache'] and not decision['drop_reason']:
                        state['eligible'].add(key)
                trace('phase_a.decision', {'company': cand['company_name'], 'domain': key,
                      **{k: v for k, v in decision.items() if k != 'stage_fact'}})
                if decision['drop_reason']:
                    trace('candidate.loss', {'company': cand['company_name'], 'stage': 'phase_a', 'reason': decision['drop_reason']})
                elif decision['complete'] and not decision['primary_from_cache']:
                    trace('candidate.loss', {'company': cand['company_name'], 'stage': 'phase_a', 'reason': 'cached_primary_unobserved'})
    except (BudgetExhausted, v12_llm.LLMTruncated) as exc:
        trace('phase_a.stop', {'reason': type(exc).__name__})
    finally:
        assert run.used['deepline'] == before, 'phase A must not use Deepline'
        trace('phase_a.done', {'assessed': len(state['assessed']), 'ranked': len(candidates), 'deepline_calls': 0})
    if v12_llm.enabled('V20R7_ALLOCATION') and v18_fit.venture(run.icp.get('company_stage')):
        from agent.v20r7_allocation import process as allocate
        return await allocate(run,raw_icp,trace,remember,filled,goal)
    eligible = [c for c in state['decisions'].values() if c['_phase_a']['complete'] and
                c['_phase_a']['primary_from_cache'] and not c['_phase_a']['drop_reason']]
    eligible.sort(key=lambda c: (-c['_phase_a']['score'], c.get('_triage', {}).get('rank', 0)))
    state['eligible'] = {p.v17_domains.key(c) for c in eligible}
    if v12_llm.enabled('V20R3_STAGE_SUPPLY') and v12_llm.enabled('V19_STAGE_REQUIRED') and v18_fit.venture(run.icp.get('company_stage')):
        fallback=[c for c in state['decisions'].values() if c['_phase_a'].get('event_recoverable')]
        fallback.sort(key=lambda c:(-sum(c['_phase_a']['components'].values()),c.get('_triage',{}).get('rank',0)))
        eligible=await stage_supply(run,eligible,trace,fallback=fallback,raw_icp=raw_icp)
    prepared={}
    defer=v12_llm.enabled('V20R4_HEADCOUNT_HINT')
    if defer:
        # Register the confirmation cohort before any profile can veto or set
        # a size claim, so the first two copies of a repeated default are unknown too.
        for index,cand in enumerate(eligible):
            r6=v12_llm.enabled('V20R6_EVENT_LOOKUP')
            if run.remaining()<12 or run.used['deepline']>(29 if r6 else 29-CONFIRM_RESERVE):break
            if r6:run.v15_budget.r6_confirmation_remaining=len(eligible)-index-1
            key=p.v17_domains.key(cand)
            state['started'].add(key)
            trace('phase_b.started',{'company':cand['company_name'],'domain':key,'score':cand['_phase_a']['score'],'profile_preparation':True})
            try:prepared[key]=await p.candidate(run,cand,raw_icp,trace,defer_profile=True)
            except (BudgetExhausted,v12_llm.LLMTruncated):break
        if v12_llm.enabled('V20R6_EVENT_LOOKUP'):run.v15_budget.r6_confirmation_remaining=0
    for rank, cand in enumerate(eligible, 1):
        if filled() >= goal and (not v12_llm.enabled('V21_PROVABLE_FIRST') or slots_provable is None or slots_provable()):
            break
        r6=v12_llm.enabled('V20R6_EVENT_LOOKUP')
        if not defer and (run.remaining() < 12 or run.used['deepline'] > (29 if r6 else 29-CONFIRM_RESERVE)):
            trace('phase_b.stop', {'reason': 'confirmation_reserve', 'remaining_seconds': run.remaining(), 'deepline_calls': run.used['deepline']})
            break
        key = p.v17_domains.key(cand)
        if not defer and r6:run.v15_budget.r6_confirmation_remaining=len(eligible)-rank
        if defer and key not in prepared:continue
        state['started'].add(key)
        if not defer:trace('phase_b.started', {'company': cand['company_name'], 'domain': key, 'rank': rank, 'score': cand['_phase_a']['score']})
        try:
            result = (await prepared[key]() if callable(prepared[key]) else prepared[key]) if defer else await p.candidate(run, cand, raw_icp, trace)
            if result and any(v12_llm.enabled(flag) for flag in ('V21_SIZE_EVIDENCE','V21_LINKEDIN_CHECK','V21_PROVABLE_FIRST')):
                result=await v21_size.enrich(run,cand,result,raw_icp,trace)
            if result and cand.get('_phase_a_primary'):
                result=intent.output(result,run.icp,trace,primary=cand['_phase_a_primary'])
            if result:
                state['admitted'].add(key)
                remember(result)
                trace('phase_b.admitted', {'company': cand['company_name'], 'domain': key,
                      'primary_quote':result['intent_signals'][0]['snippet'] if result.get('intent_signals') else ''})
        except (BudgetExhausted, v12_llm.LLMTruncated) as exc:
            trace('candidate.loss', {'company': cand['company_name'], 'stage': 'phase_b', 'reason': type(exc).__name__})
            if isinstance(exc, BudgetExhausted):
                break
