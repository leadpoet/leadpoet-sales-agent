"""Company-quality policy and bounded contact enrichment with explicit opt-out."""
import re,copy
from agent import v15_budget as budgets,v27_contact_rules as rules,v20r8_strict as subject
from agent.v92_common import domain
from agent.safe_urls import urlsplit
from agent.deadline import BudgetExhausted

TOOLS={'harvestapi_search_leads':70000,'harvestapi_get_profile':100000}

GENERIC_MAILBOXES={'admin','billing','careers','contact','customerservice','hello','help','hr','info','jobs','legal','marketing','office','privacy','recruiting','sales','security','support','team'}

def contact_text(value,limit):
    return bool(value) and len(value)<=limit and not re.search(r'[\x00-\x1f\x7f]|(?i:bearer\s+[a-z0-9._~-]{12,}|sk-[a-z0-9_-]{12,}|(?:api[_ -]?key|password|secret|token)\s*[:=])',value)

def work_email(value):
    if not contact_text(value,254) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",value):return False
    local=value.split('@')[0]
    return len(local)<=64 and not local.startswith('.') and not local.endswith('.') and '..' not in local and re.sub(r'[._-]','',local) not in GENERIC_MAILBOXES

def quality(icp):return icp.get('company_quality_policy')=='company_quality_v1'
def contacts(icp):
    from agent.v12_llm import enabled
    if not enabled('V27_CONTACTS'):return False
    return icp.get('contact_policy')=='contacts_v1' and icp.get('contacts_enabled') is not False

def default_contacts(icp):
    from agent.v12_llm import enabled
    return (enabled('V36_EMPTY_OUTPUT') or enabled('V35_CONTACTS_ALWAYS')) and contacts(icp)
def active(icp):return quality(icp) or contacts(icp)
SUPPORTED_SCHEMAS=('leadpoet.lab_arena.output.v1','leadpoet.lab_arena.output.v2',
                   'leadpoet.lab_arena.output.v3','leadpoet.lab_arena.output.v4',
                   'leadpoet.lab_arena.output.v5')

def schema(icp):
    # The round now hands us its pinned output_schema_version (README, 2026-09-17), and
    # run_icp receives it alongside the policy markers. Read it rather than inferring the
    # version from those markers: inferring it is what produced an unusable document on
    # 2026-09-16. Fall back to inference only when the field is absent or unrecognised.
    from agent.v12_llm import enabled
    given=icp.get('output_schema_version')
    if isinstance(given,str) and given in SUPPORTED_SCHEMAS:
        if given!='leadpoet.lab_arena.output.v5' or enabled('V36_OUTPUT_V5'):
            return given
    if enabled('V36_OUTPUT_V5') and icp.get('intent_details_policy')=='intent_details_v1':
        return 'leadpoet.lab_arena.output.v5'
    selected_contact=icp.get('contact_policy')=='contacts_v1'
    return 'leadpoet.lab_arena.output.v'+('4' if quality(icp) and selected_contact else '3' if quality(icp) else '2' if selected_contact else '1')

def install(run,icp,goal,trace,*,late=False):
    if not active(icp):return
    m=run.v15_budget;run.v27_input=copy.deepcopy(icp)
    m.v27_remaining=2*min(5,goal) if contacts(icp) else 0
    m.v27_sourcing_ceiling=29 if late else 29-m.v27_remaining
    m.v27_lookups=0;m.v27_calls={};m.v27_tickets={};m.v27_spent=0;m.v27_halt=False;m.v27_costs={};run.v27_profiles={}
    # Two identity calls per company remain protected. Quality fallback uses
    # leftover calls only, after protecting every outstanding contact pair.
    from agent.v20r5_primary import category
    discovery=9 if category(icp)=='HIRING' else 7
    lookup_cap=max(0,m.v27_sourcing_ceiling-discovery-2*min(5,goal)) if contacts(icp) else 9
    original=m.claim_dl;settle=m.settle_dl
    def claim(operation):
        with m.lock:
            lane,key,primary,no_tools=budgets.WORK.get();used=sum(m.lanes.values())
            if used>=29 or no_tools:raise BudgetExhausted('V27 physical/no-tools limit')
            if lane in ('v27_contact','v27_quality'):
                if not primary:raise BudgetExhausted('V27 needs admitted company')
                if m.v27_halt or m.overrun:raise BudgetExhausted('V27 prior billing overrun')
                if lane=='v27_quality' and used+1>29-m.v27_remaining:raise BudgetExhausted('V27 contact pairs reserved')
                cap=2 if lane=='v27_contact' else 1
                if m.v27_calls.get((lane,key),0)>=cap:raise BudgetExhausted('V27 per-company cap')
                hold=TOOLS.get(operation) if lane=='v27_contact' else 30000 if operation=='exa_search' else None
                if hold is None:raise BudgetExhausted('V27 operation not allowed')
                exposure=m.v27_spent+sum(m.pending[t][1] for t in m.v27_tickets if t in m.pending)
                if exposure+hold>1000000:raise BudgetExhausted('V27 incremental one-dollar exposure cap')
                m.serial+=1;ticket=m.serial;m.pending[ticket]=('deepline',hold);m.v27_tickets[ticket]=(lane,operation,hold)
                m.peak=max(m.peak,sum(m.settled.values())+sum(v[1] for v in m.pending.values()))
                m.lanes['reserve']+=1;m.v27_calls[lane,key]=m.v27_calls.get((lane,key),0)+1
                if lane=='v27_contact':m.v27_remaining=max(0,m.v27_remaining-1)
                trace('budget.claim',{'lane':lane,'company':key,'operation':operation,'hold_microusd':hold,'price_unknown':operation=='harvestapi_get_profile'})
                return ticket
            if used+1>m.v27_sourcing_ceiling:raise BudgetExhausted('V27 contacts reserved before sourcing')
            if lane in ('v18_stage','v20r6_event') and m.v27_lookups>=lookup_cap:raise BudgetExhausted('V27 lookup allowance exhausted')
            ticket=original(operation)
            if lane in ('v18_stage','v20r6_event'):m.v27_lookups+=1
            return ticket
    def settle_dl(ticket,result):
        with m.lock:
            prior=m.settled['deepline'];settle(ticket,result)
            if ticket in m.v27_tickets:
                lane,operation,hold=m.v27_tickets[ticket];charge=m.settled['deepline']-prior
                m.v27_spent+=charge;m.v27_costs[lane]=m.v27_costs.get(lane,0)+charge
                m.v27_halt |= charge>hold or m.v27_spent>1000000
    m.claim_dl=claim;m.settle_dl=settle_dl
    if run.tools:run.tools.settle_deepline=settle_dl
    trace('policy.plan',{'quality':quality(icp),'contacts':contacts(icp),'schema':schema(icp),'contact_reserved':m.v27_remaining,'sourcing_ceiling':m.v27_sourcing_ceiling,'lookup_cap':lookup_cap,'total_call_ceiling':29})

def company_linkedin(value,company,page_name=""):
    p=urlsplit(str(value or ''));parts=p.path.strip('/').split('/')
    if p.scheme not in ('http','https') or p.username or p.password:return ''
    if p.hostname!='linkedin.com' and not str(p.hostname).endswith('.linkedin.com'):return ''
    if len(parts)<2 or parts[0]!='company' or not re.fullmatch(r'[\w-]+',parts[1]):return ''
    slug=subject.normalized(parts[1]).replace(' ','')
    aliases={subject.normalized(a).replace(' ','') for a in subject.aliases(company)}
    page_name=re.split(r'\s*[|·]\s*|\s+-\s+LinkedIn',str(page_name),maxsplit=1,flags=re.I)[0]
    page_match=subject.normalized(page_name).replace(' ','') in aliases
    slug_aliases={subject.normalized(a).replace(' ','') for a in subject.aliases({'company_name':parts[1]})}
    if slug not in aliases and not (slug_aliases & aliases) and not page_match:return ''
    return 'https://www.linkedin.com/company/'+parts[1]+'/'

def state_value(value):
    states={k.casefold():v for k,v in rules.US_STATES.items()};states.update({'dc':'District of Columbia','district of columbia':'District of Columbia'})
    return states.get(str(value or '').strip().casefold(),'')

def local_quality(company,rows,profile=None):
    result=dict(company,company_linkedin='',state='');site=domain(company['company_website'])
    for row in rows:
        if domain(row.get('url'))!=site:continue
        from agent.v21_size import homepage_anchor
        slug=homepage_anchor(row,company['company_website'])
        found=company_linkedin('https://linkedin.com/company/'+slug,company) if slug else ''
        if found:result['company_linkedin']=found
        if rules._norm_country(company.get('country'))!='US':continue
        hq=(row.get('entity') or {}).get('headquarters') or {}
        if isinstance(hq,dict):result['state']=state_value(hq.get('state') or hq.get('region')) or result['state']
    if rules._norm_country(company.get('country'))=='US' and not result['state']:
        for row in rows:
            own=domain(row.get('url'))==site
            for sentence in re.split(r'(?<=[.!?])\s+|\n',str(row.get('text') or '')):
                if not own and not subject.leading_company(sentence,subject.aliases(company)):continue
                location=re.search(r'\b(?:headquartered|headquarters|based)\s*(?:in|:)\s*([^.;\n]{3,120})',sentence,re.I)
                if not location:continue
                for token in re.split(r',\s*|\s*\(|\)',location[1]):
                    value=state_value(token)
                    if value:result['state']=value;break
    if rules._norm_country(company.get('country'))=='US' and not result['state'] and profile:
        if domain(profile.get('normalized_domain') or profile.get('domain'))==site:
            hq=profile.get('headquarters') or {}
            if isinstance(hq,dict):result['state']=state_value(hq.get('state') or hq.get('region'))
            location=profile.get('location') or ''
            if isinstance(location,dict):result['state']=state_value(location.get('state') or location.get('region')) or result['state']
            elif isinstance(location,str):
                for token in location.split(','):
                    result['state']=state_value(token) or result['state']
    return result

def search_payload(company,icp):
    # Deepline owns the search payload. The query carries every ICP constraint;
    # the independent local checks below also enforce them on returned people.
    roles=icp.get('target_roles') or []
    geo=icp.get('contact_geography') or {}
    terms=[company['company_name'],company['company_website'],*roles,str(icp.get('target_seniority') or '')]
    terms.extend(str(v) for k in ('countries','regions','cities') for v in geo.get(k,[]))
    return {'query':' '.join('"'+str(v).replace('"','')+'"' for v in terms if v),'limit':5}

def profiles(response):
    return rules._profile_candidates(rules._unwrap_data(response))

def person(profile,company,icp):
    link=rules._canonical_linkedin(rules._profile_linkedin(profile));name=rules._profile_name(profile)
    if not link or not name or (not icp.get('target_roles') and not icp.get('target_seniority')):return None
    if not contact_text(name,160):return None
    location=rules._profile_location(profile)
    if location['country'] not in {r[1] for r in rules.COUNTRIES} or any(len(v)>120 for v in location.values()):return None
    for position in rules._current_positions(profile):
        if not rules._company_matches(rules._company_identifiers(company),rules._position_company(position)):continue
        title=rules._position_title(position)
        if not contact_text(title,200):continue
        if rules._deterministic_role_match(title,icp.get('target_roles') or [],icp.get('target_seniority') or '') is not True:continue
        claim={'full_name':name,'role':title,'linkedin_url':link,'location':{k:v for k,v in location.items() if v}}
        if rules._location_check(claim,profile,icp)[0]=='pass':return claim
    return None

async def contact(run,company,icp,trace):
    key=domain(company['company_website']);result=None;before=sum(run.v15_budget.lanes.values())
    try:
        with budgets.work('v27_contact',key,primary=True):
            response=await run.tool('harvestapi_search_leads',search_payload(company,icp))
            eligible=[(p,person(p,company,icp)) for p in profiles(response)]
            eligible=[(p,c) for p,c in eligible if c]
            if not eligible:return None
            targets=[rules._normalize_title(t) for t in icp.get('target_roles',[])]
            _,picked=sorted(eligible,key=lambda pc:(targets.index(rules._normalize_title(pc[1]['role'])) if rules._normalize_title(pc[1]['role']) in targets else len(targets),pc[1]['linkedin_url']))[0]
            response=await run.tool('harvestapi_get_profile',{'url':picked['linkedin_url'],'findEmail':'true'})
        for profile in profiles(response):
            claim=person(profile,company,icp)
            if not claim or claim['linkedin_url']!=picked['linkedin_url'] or rules._norm(claim['full_name'])!=rules._norm(picked['full_name']):continue
            record=str(profile.get('recordId') or profile.get('record_id') or profile.get('id') or '')
            if not contact_text(record,200) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/~-]{0,199}',record):continue
            emails=sorted(e for e in rules._extract_emails(profile) if work_email(e))
            if not emails:continue
            result={**claim,'email':emails[0],'email_source':{'provider':'harvestapi','tool':'harvestapi_get_profile','record_id':record}}
            return result
        return None
    finally:
        spent=sum(run.v15_budget.lanes.values())-before
        run.v15_budget.v27_remaining=max(0,run.v15_budget.v27_remaining-max(0,2-spent))
        trace('contact.result',{'company':company['company_name'],'decision':'supported_claim' if result else 'unavailable','calls':spent,'email_source_present':bool(result),'independently_verified':False})

async def finish(run,companies,raw,trace):
    if default_contacts(raw):
        from agent.v35_contacts import finish as attach_contacts
        return await attach_contacts(run,companies,raw,trace)
    if not active(raw):return companies
    from agent.v15_pipeline import cached_rows
    from agent.deadline import policy_limits
    # Finish sourcing first, then use only the parent's existing time envelope.
    run.deadline=max(run.deadline,run.started+policy_limits(raw)[0]-5)
    m=run.v15_budget;m.v27_remaining=2*min(5,len(companies)) if contacts(raw) else 0
    results=[];before=sum(m.lanes.values());start_cost=m.settled['deepline']
    for original in companies[:5]:
        company=local_quality(original,cached_rows(run),run.v27_profiles.get(domain(original['company_website']))) if quality(raw) else dict(original)
        if contacts(raw):
            try:company['contact']=await contact(run,company,raw,trace)
            except (BudgetExhausted,TimeoutError):company['contact']=None
        results.append(company)
    # Contact pairs have priority over optional LinkedIn searches.
    for company in results:
        if quality(raw) and not company['company_linkedin']:
            try:
                with budgets.work('v27_quality',domain(company['company_website']),primary=True):
                    response=await run.tool('exa_search',{'query':'site:linkedin.com/company "'+company['company_name'].replace('"','')+'"','numResults':3})
                for row in response.get('results',[]):
                    link=company_linkedin(row.get('url'),company,row.get('title',''))
                    if link and subject.named(row.get('title','')+' '+row.get('text',''),subject.aliases(company)):
                        company['company_linkedin']=link;break
            except (BudgetExhausted,TimeoutError):pass
        if quality(raw):trace('quality.result',{'company':company['company_name'],'company_linkedin_present':bool(company['company_linkedin']),'hq_state_present':bool(company['state'])})
    inflight={lane:sum(m.pending[t][1] for t,(l,operation,hold) in m.v27_tickets.items() if l==lane and t in m.pending) for lane in ('v27_contact','v27_quality')}
    trace('policy.cost',{'policy_inflight_usd':sum(inflight.values())/1e6,'by_step_inflight_usd':{k:v/1e6 for k,v in inflight.items()},'calls':sum(m.lanes.values())-before,'deepline_usd_settled_or_reserved':(m.settled['deepline']-start_cost)/1e6,'total_calls':sum(m.lanes.values()),'schema':schema(raw),'by_step_usd_settled_or_reserved':{k:v/1e6 for k,v in m.v27_costs.items()},'by_step_calls':{lane:sum(n for (l,k),n in m.v27_calls.items() if l==lane) for lane in ('v27_contact','v27_quality')}})
    return results
