"""Append a cached independent-domain source for the same grounded event."""
import re
from datetime import date as Date
import os
from decimal import Decimal
from agent import v12_llm,v18_fit
from agent.v92_common import domain,observed_date,signal_url_ok
from agent.evidence import _clean,_parse_date
from agent.v20r8_strict import subject
from agent.v20r5_primary import funding_mismatch,category
from agent.v20r10_evidence import canonical,hiring_ok,event_detail

MONEY=re.compile(r'\$\s*([\d,.]+)\s*(billion|million|bn|mm|[mb])\b',re.I)
ACTION=re.compile(r'\b(?:announc\w*|rais\w*|secur\w*|clos\w*|launch\w*|introduc\w*|unveil\w*|expand\w*|appoint\w*|open\w*|partner\w*|hir\w*|receiv\w*)\b',re.I)

def money(text):
    result=set()
    for number,unit in MONEY.findall(text):
        try:result.add(Decimal(number.replace(',',''))*(1000 if unit.lower() in ('b','bn','billion') else 1))
        except Exception:pass
    return result

def same_event(company,first,quote):
    a=_clean(first).casefold();b=_clean(quote).casefold()
    if a in b or b in a:return bool(event_detail(company,first) and event_detail(company,quote) and ACTION.search(quote))
    sa,sb=v18_fit.category(__import__('agent.pipeline',fromlist=['_funding_stage'])._funding_stage(first)),v18_fit.category(__import__('agent.pipeline',fromlist=['_funding_stage'])._funding_stage(quote))
    if sa and sa==sb and money(first)&money(quote):
        from agent.v15_stage_proof import submit_stage_quote
        return submit_stage_quote(sa,first) and submit_stage_quote(sb,quote)
    # Regulatory corroboration must name the same device and clearance type.
    ka,kb=clearance_key(first),clearance_key(quote)
    return bool(ka and ka==kb)

def clearance_key(text):
    text=re.sub(r'[®™]','',_clean(text)).casefold()
    if not re.search(r'\bfda\b|food and drug administration',text):return None
    if not re.search(r'510\s*\(?k\)?',text):return None
    if re.search(r'\b(?:revok\w*|withdraw\w*|denied|denies|rejected|seeking|pending|applied|expects?|plans?|not|never)\b',text):return None
    if not re.search(r'\b(?:grants?|granted|receives?|received|obtains?|obtained|secures?|secured)\b',text):return None
    found=re.search(r'\bclearance\s+for\s+(?:the\s+)?([^,.;]+)',text)
    if not found:return None
    product=found[1].strip()
    return ('FDA 510(k)',product) if len(product.split())>=2 else None

def add_second(company,icp,rows,trace):
    if not company or not v12_llm.enabled('V30_SECOND_SIGNAL'):return company
    signals=company.get('intent_signals') or []
    if not signals:return company
    first=signals[0];seen={domain(s.get('url')) for s in signals if s.get('matched_icp_signal')==0}
    if len(seen-{''})>=2:
        trace('signal.second_domain',{'company':company['company_name'],'decision':'already_present','domains':sorted(seen)});return company
    date=_parse_date(first.get('date'))
    today=_parse_date(os.environ.get('LAB_ARENA_EVALUATION_DATE') or os.environ.get('BAKEOFF_EVALUATION_DATE')) or Date.today()
    if not date:return company
    for row in rows:
        host=domain(row.get('url'));when=_parse_date(observed_date(row))
        if not host or host in seen or row.get('error') or not signal_url_ok(row.get('url','')):continue
        if when and (when>today or abs((when-date).days)>7):continue
        text=str(row.get('text') or '')
        for part in re.split(r'(?<=[.!?;])\s+|\n+',text):
            quote=_clean(part)
            if not 20<=len(quote)<=600:continue
            if not subject(company,quote,row) or funding_mismatch(icp,quote) or not hiring_ok(icp,quote,row):continue
            if not same_event(company,first['snippet'],quote):continue
            second={**first,'matched_icp_signal':0,'url':row['url'],'snippet':quote,'description':quote[:350]}
            trace('signal.second_domain',{'company':company['company_name'],'decision':'added','primary_domain':domain(first['url']),'second_domain':host,'url':row['url'],'quote':quote,'basis':'same event, cached original sentence'})
            return dict(company,intent_signals=[*signals,second])
    return company

def cap_summary(company):
    text=company.get('fit_summary','')
    if len(text)<=500:return company
    ends=list(re.finditer(r'[.!?](?:\s|$)',text[:500]));end=ends[-1].start()+1 if ends else 500
    return dict(company,fit_summary=text[:end].rstrip())

def finish(companies,icp,rows,trace):
    if not any(v12_llm.enabled(f) for f in ('V30_MULTI','V30_SECOND_SIGNAL','V30_SPEND_GUARD')):return companies
    return [cap_summary(add_second(c,icp,rows,trace)) for c in companies]
