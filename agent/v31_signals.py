"""Optional grounded evidence on another domain, ordered by the input policy.

Existing admissions and their first evidence object are immutable. Source
weights rank grounded alternatives, not claims of external verification.
"""
import asyncio
import re
from datetime import timedelta
from agent import v12_llm, v15_budget
from agent.deadline import BudgetExhausted
from agent.evidence import _clean, _parse_date, _date_in_text
from agent.safe_urls import urlsplit
from agent.v92_common import domain, observed_date, signal_url_ok
from agent.v20r8_strict import subject
from agent.v20r10_evidence import canonical, hiring_ok, CLOSED
from agent.v30_signals import same_event

WEIGHTS={'linkedin':1.,'job_board':1.,'github':1.,'news':.9,'company_website':.85,'other':.3}

def source(company,url):
    """URL source classes from the public competition adapter."""
    p=urlsplit(str(url or ''));host=(p.hostname or '').lower().removeprefix('www.')
    own=(urlsplit(company.get('company_website','')).hostname or '').lower().removeprefix('www.')
    if not host:return 'other'
    if host=='linkedin.com' or host.endswith('.linkedin.com'):return 'linkedin'
    if host=='github.com' or host.endswith('.github.com'):return 'github'
    if any(x in p.path.lower() for x in ('/jobs','/job/','/careers')):return 'job_board'
    if own and (host==own or host.endswith('.'+own)):return 'company_website'
    return 'news'

def criteria(raw):
    values=raw.get('intent_signals')
    if not isinstance(values,list):values=raw.get('required_intents') or []
    result=[]
    for i,value in enumerate(values):
        item=dict(value) if isinstance(value,dict) else {'signal':str(value)}
        item['signal']=item.get('signal') or item.get('intent_signal') or item.get('text') or ''
        ages=raw.get('intent_signal_max_age_days') or []
        item['max_age_days']=item.get('max_age_days') or (ages[i] if isinstance(ages,list) and i<len(ages) else None) or raw.get('intent_max_age_days') or 365
        result.append(item)
    return result

def kind(text):
    # Apply only to optional additions; unknown definitions never affect primary.
    for pattern,name in ((r'funding|financing|series [a-z]','FUNDING'),(r'hiring|job postings|careers','HIRING'),
       (r'leadership|appoint|chief executive','LEADERSHIP_CHANGE'),(r'acquir|acquisition','ACQUISITION'),
       (r'new (?:country|region|market)|expan','MARKET_EXPANSION'),(r'facility|manufacturing site|plant','FACILITY_OPENING'),
       (r'regulatory|clearance|certification|clinical milestone','REGULATORY_CLEARANCE'),
       (r'partnership|collaborat','PARTNERSHIP'),(r'launch|new .*product|capabilit','PRODUCT_LAUNCH')):
        if re.search(pattern,text,re.I):return name
    return ''

def matches(criterion,quote,row):
    text=criterion['signal'];k=kind(text)
    if re.search(r'\b(?:plans? to|expects? to|intends? to|will (?:launch|raise|open|acquire)|seeking|rumou?r|not yet|has not|did not)\b',quote,re.I):return False
    patterns={
      'FUNDING':r'\b(?:raised|raises|secured|secures|closed|closes|announced)\b.{0,180}(?:\$[\d,.]+|funding|financing|series [a-z])',
      'PRODUCT_LAUNCH':r'\b(?:launched|launches|unveiled|unveils|introduced|introduces|now available|general availability)\b',
      'LEADERSHIP_CHANGE':r'\b(?:appoints?|appointed|names?|named|joins?|joined|hires?|hired)\b.{0,140}\b(?:ceo|cfo|cto|coo|chief|president|director|chair|officer|partner)\b',
      'ACQUISITION':r'\b(?:acquired|acquires|completed .{0,35}acquisition|closed .{0,35}acquisition)\b',
      'MARKET_EXPANSION':r'\b(?:expanded|expands|entered|enters|launched|launches)\b.{0,100}\b(?:into|in|across|market|country|region|states)\b',
      'FACILITY_OPENING':r'\b(?:opened|opens|inaugurated|inaugurates)\b.{0,100}\b(?:facility|plant|office|site|factory|headquarters)\b',
      'REGULATORY_CLEARANCE':r'\b(?:received|receives|granted|grants|secured|secures|obtained|obtains)\b.{0,100}\b(?:clearance|approval|certification)\b',
      'PARTNERSHIP':r'\b(?:partnered with|partners with|announced .{0,60}partnership|signed .{0,60}agreement)\b'}
    if k=='HIRING':
        p=urlsplit(row.get('url',''));host=p.hostname or ''
        if host=='linkedin.com' or host.endswith('.linkedin.com'):return False
        if not hiring_ok({'required_intents':[{'category':'HIRING'}]},quote,row):return False
        if CLOSED.search(str(row.get('text',''))):return False
        # A general hiring claim cannot satisfy an explicit role requirement.
        roles=re.search(r'hiring for (.+?) roles',text,re.I)
        if roles:
            required=[x.strip() for x in re.split(r',|\bor\b|\band\b',roles[1]) if x.strip()]
            if not any(re.search(re.escape(x),quote,re.I) for x in required):return False
        # Respect the requested current-posting/careers source, not estimates.
        if re.search(r'current job|careers page',text,re.I) and not (source({'company_website':''},row['url'])=='job_board' or any(h in host for h in ('ashbyhq.com','greenhouse.io','lever.co'))):return False
        return True
    if not k or not re.search(patterns[k],quote,re.I):return False
    if k=='ACQUISITION' and re.search(r'\b(?:previously|formerly|historically|already)\b.{0,25}acquir',quote,re.I):return False
    if k=='FUNDING' and re.search(r'Series A or later',text,re.I) and not re.search(r'Series [A-Z]\b',quote,re.I):return False
    if k=='PRODUCT_LAUNCH' and re.search(r'data product|analytics',text,re.I) and not re.search(r'\b(?:data|analytics)\b',quote,re.I):return False
    return True

def sentences(row):
    text=str(row.get('text') or '')
    for part in re.split(r'(?<=[.!?;])\s+(?=[A-Z"“])|\n+',text):
        quote=_clean(part)
        if 20<=len(quote)<=600:yield quote

def event_date(row,quote):
    def parse(text):
        text=re.sub(r'([A-Za-z]{3})\.(\d{1,2})\.(20\d\d)',r'\1 \2, \3',text)
        return _date_in_text(text)
    text=_clean(str(row.get('text') or ''));at=text.find(quote)
    inline=parse(quote)
    if inline:return inline
    if at>=0:
        tail=text[at+len(quote):].lstrip()[:80]
        # A date caption immediately following this event, not another tile.
        if re.match(r'(?:20\d\d-\d\d-\d\d|[A-Za-z]{3,9}[. ]\d)',tail):
            dated=parse(tail)
            if dated:return dated
    metadata=_parse_date(row.get('datePublished') or row.get('date'))
    if metadata:return metadata
    if urlsplit(row.get('url','')).path in ('','/'):return None
    return parse(text[:500])

def candidates(company,criterion,index,rows,today):
    found=[]
    for row in rows:
        if row.get('error') or not canonical(row.get('url')) or not signal_url_ok(row.get('url','')):continue
        for quote in sentences(row):
            if not matches(criterion,quote,row) or not subject(company,quote,row):continue
            when=event_date(row,quote)
            if not when or not 0<=(today-when).days<=int(criterion['max_age_days']):continue
            found.append({'date':when.isoformat(),'description':quote[:350],'matched_icp_signal':index,
                'snippet':quote,'url':row['url'],'why_now':'A separately dated event supporting another requested criterion.'})
            break
    return found

def choose(company,options,trace,index):
    ordered=sorted(options,key=lambda s:-WEIGHTS[source(company,s['url'])]) if v12_llm.enabled('V31_SOURCE_UPGRADE') else options
    if v12_llm.enabled('V31_SOURCE_UPGRADE'):
        trace('signal.source_choice',{'company':company['company_name'],'criterion_index':index,
          'alternatives':[{'url':s['url'],'source_type':source(company,s['url']),'multiplier':WEIGHTS[source(company,s['url'])]} for s in options],
          'selected_url':ordered[0]['url'] if ordered else None,'qualification':'cached source grounding; external verification not observed'})
    return ordered[0] if ordered else None

def integrity(raw):
    return raw.get('integrity_policy')=='arena_integrity_v1'

def same_candidates(company,raw,rows,today):
    first=company['intent_signals'][0];date=_parse_date(first.get('date'))
    if not date:return []
    used={domain(x['url']) for x in company['intent_signals']};items=criteria(raw);found=[]
    for row in rows:
        host=domain(row.get('url'))
        if not host or host in used or row.get('error') or not signal_url_ok(row.get('url','')):continue
        for quote in sentences(row):
            if not subject(company,quote,row) or not same_event(company,first['snippet'],quote):continue
            when=event_date(row,quote)
            if not when or abs((when-date).days)>7 or not 0<=(today-when).days<=int(items[0]['max_age_days'] if items else 365):continue
            if items and kind(items[0]['signal'])=='HIRING' and not matches(items[0],quote,row):continue
            found.append({**first,'url':row['url'],'snippet':quote,'description':quote[:350],
                'date':when.isoformat(),'why_now':'An independently hosted account of the same dated event.'});break
    return found

def different_candidates(company,raw,rows,today):
    signals=company['intent_signals']
    if any(x.get('matched_icp_signal',0)>0 for x in signals):return []
    used={domain(x['url']) for x in signals};found=[]
    for index,item in enumerate(criteria(raw)[1:],1):
        found.extend(x for x in candidates(company,item,index,rows,today) if domain(x['url']) not in used)
    return found

def satisfied(company,raw):
    signals=company.get('intent_signals') or []
    if not signals:return False
    if integrity(raw) and len(criteria(raw))>1:
        return any(x.get('matched_icp_signal',0)>0 and domain(x['url'])!=domain(signals[0]['url']) for x in signals)
    return len({domain(x['url']) for x in signals}-{''})>=2

def cached(company,raw,rows,today,trace,*,from_cache=True):
    if not company or not company.get('intent_signals') or company['intent_signals'][0].get('matched_icp_signal')!=0:return company
    enabled=v12_llm.enabled('V31_SECOND_CRITERION')
    if not enabled and not v12_llm.enabled('V31_SOURCE_UPGRADE'):return company
    # Original evidence is never replaced or removed, including V30's sources.
    if enabled and satisfied(company,raw):return company
    order=('different','same') if integrity(raw) else ('same','different')
    if not enabled:order=('same',)
    for mode in order:
        options=(different_candidates if mode=='different' else same_candidates)(company,raw,rows,today)
        if not enabled:
            first=company['intent_signals'][0]
            options=[x for x in options if WEIGHTS[source(company,x['url'])]>WEIGHTS[source(company,first['url'])]]
        best=choose(company,options,trace,0 if mode=='same' else 'secondary')
        if not best:continue
        event=('signal.second_domain_cached' if from_cache else 'signal.second_domain_fetched') if mode=='same' else 'signal.second_criterion'
        trace(event,{'company':company['company_name'],'criterion_index':best['matched_icp_signal'],
          'source_type':source(company,best['url']),'from_cache':from_cache,'url':best['url'],
          'primary_domain':domain(company['intent_signals'][0]['url']),'second_domain':domain(best['url']),
          'integrity_policy':raw.get('integrity_policy'),'qualification':'admitted primary; external verification not observed'})
        return dict(company,intent_signals=[*company['intent_signals'],best])
    return company

def query_plan(company,raw):
    items=criteria(raw);first=company['intent_signals'][0]
    if integrity(raw) and len(items)>1:
        return 'different','"'+company['company_name']+'" '+items[1]['signal'],int(items[1]['max_age_days'])
    # Keep the event's amounts, round/product/party terms and date, while
    # discarding generic announcement prose. Never query a fabricated event.
    stop={'today','announced','announces','announce','the','a','an','it','has','have','that','its','and','of','in','to','for','with'}
    terms=list(dict.fromkeys(t for t in re.findall(r"[$\w’'-]+",first.get('snippet','')) if t.casefold() not in stop))[:24]
    query='"'+company['company_name']+'" '+' '.join(terms)+' '+str(first.get('date') or '')+' -site:'+domain(first['url'])
    return 'same',query,int(items[0]['max_age_days']) if items else 365

def claim(run,operation):
    b=run.v15_budget;lane,key,primary,no_tools=v15_budget.WORK.get()
    with b.lock:
        used=getattr(run,'v31_searches',set())
        if not v12_llm.enabled('V31_SECOND_CRITERION') or not getattr(run,'v31_confirmations_complete',False) or lane!='v31_signal' or not primary or no_tools or operation!='exa_search':raise BudgetExhausted('secondary requires completed admission')
        if key in used or len(used)>=5 or sum(b.lanes.values())>=29 or b.overrun:raise BudgetExhausted('secondary call allowance exhausted')
        ticket=b._reserve('deepline',v15_budget.DL_CEILINGS[operation])
        used.add(key);run.v31_searches=used;b.lanes['reserve']+=1
        b.trace('budget.claim',{'lane':lane,'charged_lane':'reserve','company':key,'primary':True,'operation':operation})
        return ticket

async def finish(run,companies,raw,rows,trace):
    if not any(v12_llm.enabled(f) for f in ('V31_SECOND_CRITERION','V31_SOURCE_UPGRADE')):return companies
    result=list(companies);run.v31_confirmations_complete=True
    for pos,company in enumerate(companies):
        try:
            enriched=cached(company,raw,rows,run.eval_date,trace);result[pos]=enriched
            if not v12_llm.enabled('V31_SECOND_CRITERION') or not enriched.get('intent_signals') or enriched['intent_signals'][0].get('matched_icp_signal')!=0 or satisfied(enriched,raw):continue
            key=domain(company['company_website'])
            if key in getattr(run,'v31_searches',set()) or run.remaining()<3 or sum(run.v15_budget.lanes.values())>=29:continue
            mode,query,age=query_plan(enriched,raw)
            trace('signal.search_plan',{'company':company['company_name'],'mode':mode,'query':query[:500],'integrity_policy':raw.get('integrity_policy')})
            with v15_budget.work('v31_signal',key,primary=True):
                async with asyncio.timeout(min(8,max(0,run.remaining()-1))):
                    response=await run.tool('exa_search',{'query':query[:500],'numResults':3,
                      'startPublishedDate':(run.eval_date-timedelta(days=age)).isoformat(),
                      'endPublishedDate':run.eval_date.isoformat(),'contents':{'text':{'maxCharacters':2000}}})
            result[pos]=cached(enriched,raw,response.get('results',[]) if isinstance(response,dict) else [],run.eval_date,trace,from_cache=False)
        except (Exception,asyncio.CancelledError) as exc:
            trace('signal.second_unavailable',{'company':company.get('company_name'),'reason':type(exc).__name__,'original_retained':True})
    return result
