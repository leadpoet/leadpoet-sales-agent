"""Score cached size provability for confirmation order; never change output claims."""
import re
from agent import v12_llm,v21_size as size,v20r8_strict as subject,v18_fit
from agent.v92_common import domain
from agent.evidence import _parse_date,_country_ok

CITED=('zoominfo.com','pitchbook.com','tracxn.com','caplight.com','fundup.ai','growjo.com','crunchbase.com','leadiq.com','getlatka.com','rocketreach.co','rocketreach.com')
def enabled(flag):return v12_llm.enabled(flag)
def rows(run,cand=None):
    from agent.v15_pipeline import cached_rows
    result=cached_rows(run)+list((cand or {}).get('_verdict_rows') or [])
    fact=((cand or {}).get('_phase_a') or {}).get('stage_fact') or {}
    if fact.get('page'):result.append(fact['page'])
    return result

def bound_anchor(company,pages):
    from agent.v27_policies import company_linkedin
    for page in pages:
        slug=size.homepage_anchor(page,company.get('company_website') or company.get('domain',''))
        if not slug:continue
        url='https://www.linkedin.com/company/'+slug
        if company_linkedin(url,company):return url
        # A cached LinkedIn company-page title may bind an observed homepage slug.
        for linked in pages:
            if size.linkedin_slug(linked.get('url'))==slug and company_linkedin(url,company,linked.get('title','')):return url
    return ''

def lower(band):return int(re.search(r'\d[\d,]*',band)[0].replace(',',''))
def count_fact(company,row,allowed,today):
    existing=size.evidence(company,row,allowed,today)
    if existing:return existing
    own=domain(row.get('url'))==domain(company.get('company_website') or company.get('domain'))
    when=_parse_date(str(row.get('date') or row.get('datePublished') or '')[:10])
    if row.get('error') or not domain(row.get('url')) or (when and when>today):return None
    # On an owned page a first-person team statement is attributable without
    # inserting a company name into the original quote.
    for part in re.split(r'(?<=[.!?;])\s+|\n+',str(row.get('text') or '')):
        if size.NEGATIVE.search(part) or size.OTHER_OWNER.search(part):continue
        if own and re.match(r'(?i)^\s*(?:we\b|our\b|team of\b)',part):
            match=size.COUNTS.search(part)
        elif domain(row.get('url')) in CITED and subject.leading_company(row.get('title',''),subject.aliases(company)):
            # A directory's structured size label is evidence; arbitrary body
            # numbers or employee counts belonging to a related company are not.
            match=re.fullmatch(r'\s*(?:Company size|Number of employees|Employees|Employee count)\s*[:\n]?\s*('+size.NUMBER+r')\s*(?:employees)?\s*',part,re.I)
        else:match=None
        if not match:continue
        if re.search(r'\b(?:hired|hiring|added|laid off|layoffs|cut|reduced|lost|over|under|more than|less than|at least|up to|nearly)\b',part[:match.end()],re.I):continue
        if re.match(r'[+%\d]|\.\d',part[match.end():]):continue
        literal=next((x for x in match.groups() if x),None)
        band=size.bucket(literal)
        if not band:continue
        prefix=part[max(0,match.start()-30):match.start()]
        if re.search(r'\b(?:over|under|more than|less than|at least|up to|nearly|hired|hiring|added|laid off)\b',prefix,re.I):continue
        return {'url':row['url'],'host':domain(row['url']),'quote':part.strip(),'count':literal,'band':band,'in_band':band in allowed,'page':row}
    return None

def inspect(company,pages,icp,today,a_score=0):
    allowed=size.judge_mirror.employee_count_buckets_for_icp(icp)
    cand=dict(company,domain=domain(company.get('company_website') or company.get('domain')))
    anchor=bound_anchor(cand,pages);facts=[]
    if anchor:
        fact=size.linkedin_size(cand,pages,size.linkedin_slug(anchor),allowed)
        if fact and not fact['page'].get('error'):
            when=_parse_date(str(fact['page'].get('date') or '')[:10])
            if not when or when<=today:facts.append(dict(fact,kind='linkedin'))
    for page in pages:
        host=domain(page.get('url'));own=host==cand['domain']
        if not own and host not in CITED:continue
        fact=count_fact(cand,page,allowed,today)
        if fact:facts.append(dict(fact,kind='own' if own else 'cited'))
    good=[f for f in facts if f['in_band']];outside=bool(facts) and not good
    parts={'homepage_linkedin':3 if anchor else 0,'linkedin_size':2 if any(f['kind']=='linkedin' for f in good) else 0,
           'cited_size':2 if any(f['kind']=='cited' for f in good) else 0,'own_size':1 if any(f['kind']=='own' for f in facts) else 0,
           'only_outside':-3 if outside else 0}
    chosen=min(good,key=lambda f:({'linkedin':0,'cited':1,'own':2}[f['kind']],lower(f['band']),f['url']),default=None)
    smallest=min((b for b in allowed if lower(b)<=1000),key=lower,default=None)
    band=chosen['band'] if chosen else None if outside else smallest
    return {'score':sum(parts.values()),'components':parts,'a_score':a_score or 0,'anchor':anchor,'facts':facts,
            'band':band,'basis':'observed' if chosen else 'fallback_smallest' if band else 'none','chosen':chosen,'only_outside':outside}

def a_score(run,company):
    cs=getattr(run,'v20_state',{}).get('decisions',{}).values()
    c=next((c for c in cs if domain(c.get('domain'))==domain(company.get('company_website')) or c['company_name']==company['company_name']),{})
    return (c.get('_phase_a') or {}).get('score') or (c.get('_triage') or {}).get('score') or 0
