"""Bounded contact enrichment after the company's sourcing result is complete."""
import asyncio,base64,binascii,re,time
from contextvars import ContextVar
from agent import v27_contact_rules as rules,v15_budget as budgets
from agent.v92_common import domain
from agent.deadline import BudgetExhausted

CONTACT_SEARCH_TIMEOUT_SECONDS=25.0
CONTACT_PROFILE_TIMEOUT_SECONDS=45.0
CONTACT_DEADLINE_MARGIN_SECONDS=2.0
CONTACT_CALL_TIMEOUT=ContextVar('contact_call_timeout',default=None)

def call_timeout(tool,remaining):
    maximum=CONTACT_PROFILE_TIMEOUT_SECONDS if tool=='harvestapi_get_profile' else CONTACT_SEARCH_TIMEOUT_SECONDS
    return min(maximum,max(0.0,remaining-CONTACT_DEADLINE_MARGIN_SECONDS))

class ContactTimeout(TimeoutError):
    def __init__(self,tool,seconds,waited):
        self.tool=tool;self.seconds=seconds;self.waited=waited
        super().__init__(f'{tool} timed out after {waited:.3f}s (budget {seconds:.3f}s)')

BAD_EMAIL={'invalid','disposable','abuse','do_not_mail','undeliverable','unverified','not_valid','failed'}
GOOD_EMAIL={'verified','valid','deliverable','catch_all','catch-all'}

def person_urn_segment(profile):
    """Compare the member segment of the provider's two opaque ID encodings."""
    value=str(profile.get('id') or '')
    if re.fullmatch(r'AC[ow][A-Za-z0-9_-]{9,117}',value):
        try:
            raw=base64.urlsafe_b64decode(value+'='*(-len(value)%4))
            if len(raw)>=9 and raw[:2] in (b'\x00\x2a',b'\x00\x2c') and raw[8]==1:
                return str(int.from_bytes(raw[2:8],'big'))
        except (ValueError,binascii.Error):pass
    value=str(profile.get('objectUrn') or '')
    match=re.fullmatch(r'(?:urn:li:(?:member|person):)?([0-9]+)',value)
    return str(int(match[1])) if match else ''

def profile_link(profile):
    link=rules._canonical_linkedin(rules._profile_linkedin(profile))
    if link and not re.fullmatch(r'AC[ow][A-Za-z0-9_-]{9,117}',link.rstrip('/').split('/')[-1]):return link
    identifier=str(profile.get('publicIdentifier') or '').strip()
    if identifier and not re.fullmatch(r'AC[ow][A-Za-z0-9_-]{9,117}',identifier):
        return rules._canonical_linkedin('https://www.linkedin.com/in/'+identifier)
    return ''

def email_quality(profile,email):
    for row in profile.get('emails') or []:
        if isinstance(row,dict) and str(row.get('email') or '').strip().lower()==email:
            quality=row.get('qualityScore')
            return quality if type(quality) in (int,float) else None
    return None

def emails(profile,company_domain=''):
    from agent.v27_policies import work_email
    status=str(profile.get('emailStatus') or profile.get('email_status') or profile.get('emailVerificationStatus') or '').lower()
    if status in BAD_EMAIL:return []
    values=set()
    for row in profile.get('emails') or []:
        if not isinstance(row,dict):continue
        email=str(row.get('email') or '').strip().lower()
        state=str(row.get('status') or '').strip().lower()
        if state in BAD_EMAIL or state in ('catch_all','catch-all'):continue
        if row.get('catchAllDomain') is not False:continue
        if (state=='valid' or row.get('deliverable') is True) and work_email(email):values.add(email)
    return sorted(values,key=lambda e:(domain('https://'+e.split('@')[1])!=company_domain,e))

def search_person(profile,company,icp):
    """Search locations are hints; full profile geography is checked after enrichment."""
    from agent.v27_policies import contact_text
    link=rules._canonical_linkedin(rules._profile_linkedin(profile));name=rules._profile_name(profile)
    if not link or not contact_text(name,160):return None
    expected=rules._norm_company_name(company.get('company_name') or '')
    if not expected:return None
    for position in rules._current_positions(profile):
        if position.get('current') is not True:continue
        if rules._norm_company_name(position.get('companyName') or '')!=expected:continue
        location=profile.get('location') or {}
        hint=location.get('linkedinText','') if isinstance(location,dict) else ''
        return {'full_name':name,'role':rules._position_title(position),'linkedin_url':link,
                'location_hint':str(hint),'person_urn_segment':person_urn_segment(profile)}
    return None

def profile_person(profile,company,icp,picked=None):
    from agent.v27_policies import contact_text
    link=profile_link(profile);name=rules._profile_name(profile)
    if not link or not contact_text(name,160):return None,'profile_identity_mismatch'
    if picked and rules._norm(name)!=rules._norm(picked['full_name']):
        return None,'profile_identity_mismatch'
    segment=person_urn_segment(profile)
    if picked and segment and picked.get('person_urn_segment') and segment!=picked['person_urn_segment']:
        return None,'profile_identity_mismatch'
    location=rules._profile_location(profile)
    if location['country'] not in {r[1] for r in rules.COUNTRIES} or any(len(v)>120 for v in location.values()):
        return None,'profile_location_unsupported'
    reason='profile_current_employee_unavailable'
    for position in rules._current_positions(profile):
        if not rules._company_matches(rules._company_identifiers(company),rules._position_company(position)):continue
        title=rules._position_title(position)
        if not contact_text(title,200):continue
        claim={'full_name':name,'role':title,'linkedin_url':link,'location':{k:v for k,v in location.items() if v}}
        if rules._location_check(claim,profile,icp)[0]=='pass':return claim,None
        reason='profile_location_unsupported'
    return None,reason

def person(profile,company,icp):
    return profile_person(profile,company,icp)[0]

def preference(profile,claim,icp):
    targets=icp.get('target_roles') or []
    exact=rules._deterministic_role_match(claim['role'],targets,icp.get('target_seniority') or '') is True
    words=set(re.findall(r'[a-z]{3,}',str(icp.get('buyer_description') or '').lower()))
    overlap=len(words & set(re.findall(r'[a-z]{3,}',claim['role'].lower())))
    known=str(profile.get('emailStatus') or profile.get('email_status') or profile.get('emailVerificationStatus') or '').lower() in GOOD_EMAIL
    known=known or any(isinstance(e,dict) and (e.get('verified') is True or str(e.get('status') or e.get('verificationStatus') or '').lower() in GOOD_EMAIL) for e in profile.get('emails') or [])
    return (not exact,-overlap,0 if known and emails(profile) else 1 if emails(profile) else 2)

def search_payload(run,company,icp):
    from agent.v27_policies import company_linkedin
    key=domain(company['company_website']);record=getattr(run,'v33_size_records',{}).get(key,{})
    link=company_linkedin(company.get('company_linkedin'),company) or record.get('url')
    request={'page':1}
    if link:request['currentCompanies']=link
    else:request['search']=company['company_name']
    if icp.get('target_roles'):request['currentJobTitles']=','.join(icp['target_roles'])
    geo=icp.get('contact_geography') or {}
    locations=geo.get('cities') or geo.get('regions') or geo.get('countries')
    if locations:request['locations']=','.join(locations)
    return request

async def attach(run,company,icp,trace):
    from agent.v27_policies import profiles,contact_text
    m=run.v15_budget;key=domain(company['company_website']);history=run.v35_contacts
    if key in history:return history[key]
    history[key]=None;result=None;reason='no_supported_current_employee';failure_details={};selected_email_quality=None
    before=m.v27_calls.get(('v27_contact',key),0);cost_before=m.v27_spent
    async def call(tool,payload):
        seconds=call_timeout(tool,run.remaining());started=time.monotonic()
        token=CONTACT_CALL_TIMEOUT.set(seconds)
        try:
            async with asyncio.timeout(seconds):
                with budgets.work('v27_contact',key,primary=True):response=await run.tool(tool,payload)
            # The shared tool wrapper may report a transport timeout as error data.
            error=str(response.get('error') or '') if isinstance(response,dict) else ''
            if error.startswith(('TimeoutError:', 'ReadTimeout:', 'ConnectTimeout:', 'WriteTimeout:', 'PoolTimeout:')):
                raise TimeoutError(error)
            return response
        except TimeoutError as exc:
            raise ContactTimeout(tool,seconds,time.monotonic()-started) from exc
        finally:
            CONTACT_CALL_TIMEOUT.reset(token)
    try:
        if sum(m.lanes.values())+2>29:
            reason='two_contact_calls_unavailable';return None
        response=await call('harvestapi_search_leads',search_payload(run,company,icp))
        eligible=[(p,c) for p in profiles(response) if (c:=search_person(p,company,icp))]
        if not eligible:return None
        profile,picked=min(eligible,key=lambda pc:preference(*pc,icp))
        trace('contact.search_selected',{'company':company['company_name'],'eligible_count':len(eligible),
          'full_name':picked['full_name'],'title':picked['role'],'linkedin_url':picked['linkedin_url'],
          'location_hint':picked['location_hint'],'location_verified':False})
        available=run.remaining()
        if call_timeout('harvestapi_get_profile',available)<CONTACT_PROFILE_TIMEOUT_SECONDS:
            reason='contact_profile_skipped_no_time'
            failure_details={'remaining_seconds':available,'required_seconds':CONTACT_PROFILE_TIMEOUT_SECONDS,
                             'deadline_margin_seconds':CONTACT_DEADLINE_MARGIN_SECONDS}
            return None
        reason='profile_response_empty'
        response=await call('harvestapi_get_profile',{'url':picked['linkedin_url'],'findEmail':'true'})
        for profile in profiles(response):
            claim,failure=profile_person(profile,company,icp,picked)
            if not claim:
                reason=failure;continue
            record=str(profile.get('recordId') or profile.get('record_id') or profile.get('id') or '')
            if not contact_text(record,200) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/~-]{0,199}',record):
                reason='profile_record_id_unavailable';continue
            available=emails(profile,key)
            if not available:
                reason='profile_email_unavailable';continue
            selected_email_quality=email_quality(profile,available[0])
            result={**claim,'email':available[0],'email_source':{'provider':'harvestapi','tool':'harvestapi_get_profile','record_id':record}}
            history[key]=result;return result
        return None
    except ContactTimeout as exc:
        reason='contact_profile_timeout' if exc.tool=='harvestapi_get_profile' else 'contact_search_timeout'
        failure_details={'tool':exc.tool,'waited_seconds':round(exc.waited,3),'timeout_seconds':exc.seconds}
        return None
    except (Exception,asyncio.CancelledError) as exc:
        reason=type(exc).__name__+': '+str(exc)[:120];return None
    finally:
        calls=m.v27_calls.get(('v27_contact',key),0)-before
        m.v27_remaining=max(0,m.v27_remaining-max(0,2-calls))
        if result:
            trace('contact.attached',{'company':company['company_name'],'title':result['role'],
              'email_domain_matches_company':domain('https://'+result['email'].split('@')[1])==key,
              'calls_used':calls,'cost':(m.v27_spent-cost_before)/1e6,'email_quality':selected_email_quality,
              'evidence':'provider-returned email and record ID; mailbox checked independently by scorer','independently_verified':False})
        else:trace('contact.failed',{'company':company['company_name'],'reason':reason,'calls_used':calls,**failure_details})

# Contacts are attempted for at most this many companies per ICP, in output order.
# Measured 2026-09-19: contact search for every returned company cost ~59 credits per
# round across three hotkeys (harvestapi_search_leads ~0.7 credits each) while no pair
# had yet qualified; two attempts per ICP keep the upside at ~40% of the cost.
CONTACT_COMPANY_LIMIT=2

async def finish(run,companies,raw,trace):
    from agent import v27_policies as v
    from agent.deadline import policy_limits
    if not hasattr(run,'v35_contacts'):run.v35_contacts={}
    if not hasattr(run.v15_budget,'v27_calls'):v.install(run,raw,len(companies),trace,late=True)
    run.deadline=max(run.deadline,run.started+policy_limits(raw)[0]-5)
    if v.quality(raw):
        try:companies=await v.finish(run,companies,dict(raw,contacts_enabled=False),trace)
        except (Exception,asyncio.CancelledError):pass
    run.v15_budget.v27_remaining=2*min(CONTACT_COMPANY_LIMIT,len(companies))
    results=[]
    for original in companies:
        company=dict(original)
        if len(results)<CONTACT_COMPANY_LIMIT:
            value=await attach(run,company,raw,trace)
            if value is not None:company['contact']=value
        else:trace('contact.failed',{'company':company['company_name'],'reason':'contact_company_limit','calls_used':0})
        results.append(company)
    return results
