"""Cache-only candidate order and cautious, explicitly inferred size hints."""
import re
from collections import defaultdict
from agent.safe_urls import urlsplit
from agent import v18_fit,v17_domains,p1_bucket,v15_stage_proof,judge_mirror
from agent.evidence import _norm,_parse_date,_country_ok,BUCKETS
from agent.v92_common import domain,observed_date

def urlkey(url):return str(url or '').split('#')[0]

def names(text,name):
    return bool(name and re.search(r'(?<!\w)'+r'\s*'.join(map(re.escape,name.split()))+r'(?!\w)',str(text or ''),re.I))

def associated(cand,rows):
    selected={urlkey(u) for u in cand.get('urls',[])}
    return [r for r in rows if urlkey(r.get('url')) in selected or
        (domain(r.get('url'))==domain(cand.get('domain')) and domain(cand.get('domain'))) or
        names(str(r.get('title',''))+' '+str(r.get('text','')),cand['company_name'])]

def counts(candidates):
    owners=defaultdict(set)
    for c in candidates:
        for u in c.get('urls',[]):owners[urlkey(u)].add(v17_domains.key(c))
    return {u:len(keys) for u,keys in owners.items()}

def headcount_hint(cand,rows):
    literal=p1_bucket.exact_headcount(cand,rows)
    return v18_fit.bucket(literal) if literal is not None else v18_fit.cached_headcount_hint(cand,rows)

def hq_match(cand,rows,country):
    for row in rows:
        own=domain(row.get('url'))==domain(cand.get('domain')) and bool(domain(cand.get('domain')))
        if own:
            hq=(row.get('entity') or {}).get('headquarters') or {}
            if isinstance(hq,dict) and _country_ok(hq.get('country',''),country) is True:return True
        for sentence in re.split(r'(?<=[.!?])\s+|\n+',str(row.get('text') or '')):
            if not names(sentence,cand['company_name']) and not own:continue
            match=re.search(r'\b(?:headquartered|global headquarters|headquarters are|based)\s+in\s+([^.;\n]{3,100})',sentence,re.I)
            if match and _country_ok(match.group(1),country) is True:return True
    return False

def affirmed(cand,fact):
    return bool(fact and v15_stage_proof.submit_stage_quote(fact['value'],fact['quote']) and
        v15_stage_proof.announcement_source(fact['page']['url'],cand.get('domain','')))

def later_hint(cand,rows,wanted):
    """Ranking hint only: a stage label is never a submission-stage proof."""
    for row in rows:
        own=bool(domain(cand.get('domain'))) and domain(row.get('url'))==domain(cand.get('domain'))
        for sentence in re.split(r'(?<=[.!?])\s+|\n+',str(row.get('text') or '')):
            if not own and not names(sentence,cand['company_name']):continue
            # A known investor/customer's round is not a hint about the subject.
            if re.search(r'\b(?:investor|customer|partner|supplier)\b.{0,80}\b(?:raised|closed|secured)\b',sentence,re.I):continue
            stages=[v18_fit.category(x) for x in re.findall(r'\bseries\s+[a-z]\+?\b',sentence,re.I)]
            if any(s and wanted and v18_fit.STAGES.index(s)>v18_fit.STAGES.index(wanted) for s in stages):return True
            if re.search(r'\bprivate[- ]equity[- ](?:backed|owned|controlled)\b|\bpublicly (?:listed|traded)\b|\b(?:nasdaq|nyse)\s*:',sentence,re.I):return True
    return False

def score(cand,rows,row_counts,icp,evaluation_date):
    selected=associated(cand,rows);parts={};article=False
    list_urls=[u for u in cand.get('urls',[]) if row_counts.get(urlkey(u),0)>=3]
    for row in selected:
        when=_parse_date(observed_date(row));u=row.get('url','')
        if (when and when<=evaluation_date and urlsplit(u).path.strip('/') and
            names(row.get('title',''),cand['company_name']) and row_counts.get(urlkey(u),0)<3):
            article=True;break
    if article:parts['dated_subject_article']=3
    if list_urls:parts['multi_subject_row']=-3
    observations=v18_fit.observations(cand,selected,evaluation_date)
    wanted=v18_fit.category(icp.get('company_stage'))
    if any(v18_fit.category(f['value'])==wanted and affirmed(cand,f) for f in observations):
        parts['affirmed_requested_stage']=3
    for observation in observations:
        stage=v18_fit.category(observation['value'])
        if stage in ('Private Equity','Public') or (wanted and v18_fit.STAGES.index(stage)>v18_fit.STAGES.index(wanted)):
            parts['later_round_or_ownership']=-2
    if later_hint(cand,selected,wanted):parts['later_round_or_ownership']=-2
    if any(v17_domains.related_host(cand['company_name'],domain(r.get('url'))) for r in selected):parts['own_domain']=2
    hint=headcount_hint(cand,selected)
    if hint in judge_mirror.employee_count_buckets_for_icp(icp):parts['headcount_inside_icp']=1
    if hq_match(cand,selected,icp.get('country','')):parts['hq_country']=1
    return {'score':sum(parts.values()),'components':parts,'list_only':bool(list_urls) and not article,
            'single_subject':article,'list_urls':list_urls}

def rank(candidates,rows,icp,evaluation_date,trace,*,pool=None,phase='main'):
    # Count extracted company subjects, not title keywords or duplicate rows.
    merged={v17_domains.key(c):c for c in (pool or [])}
    for c in candidates:
        k=v17_domains.key(c)
        merged[k]=dict(c,urls=list(dict.fromkeys(merged.get(k,{}).get('urls',[])+c.get('urls',[]))))
    row_counts=counts(merged.values())
    scored=[dict(c,_triage=score(c,rows,row_counts,icp,evaluation_date),_triage_index=i) for i,c in enumerate(candidates)]
    scored.sort(key=lambda c:(c['_triage']['list_only'],-c['_triage']['score'],c['_triage_index']))
    for n,c in enumerate(scored,1):c['_triage'].update(rank=n,phase=phase)
    # Bounded trace pages preserve the whole ranking under the harness frame
    # limit; long list-source URLs stay in the internal candidate record.
    for start in range(0,max(1,len(scored)),15):
        trace('triage.rank',{'phase':phase,'cache_only':True,'rows':len(rows),
            'page':start//15+1,'pages':max(1,(len(scored)+14)//15),'candidates':[
                {'company':c['company_name'],'domain_hint':c['domain'],
                 **{k:v for k,v in c['_triage'].items() if k!='list_urls'},
                 'list_source_count':len(c['_triage']['list_urls'])} for c in scored[start:start+15]]})
    return scored

def chunks(candidates,size=3):
    # Never include list rows in the last single-subject model batch.
    for is_list in (False,True):
        group=[c for c in candidates if c['_triage']['list_only']==is_list]
        for start in range(0,len(group),size):yield group[start:start+size]

def smaller_hint(cand,rows,icp,guess):
    allowed=sorted(judge_mirror.employee_count_buckets_for_icp(icp),key=BUCKETS.index)
    if not allowed or guess!=allowed[0]:return ''
    selected=associated(cand,rows);hint=headcount_hint(cand,selected)
    if hint and BUCKETS.index(hint)<BUCKETS.index(allowed[0]):return 'cached_employee_hint_below_smallest_allowed'
    if '2-10' in allowed or v18_fit.category(icp.get('company_stage')) not in ('Seed','Series A'):return ''
    for row in selected:
        text=str(row.get('text') or '')
        for sentence in re.split(r'(?<=[.!?])\s+|\n+',text):
            if not names(sentence,cand['company_name']) or not re.search(r'\bstart[- ]?up\b',sentence,re.I):continue
            if not re.search(r'\b(?:raised|closed|secured|funding|round)\b',sentence,re.I):continue
            amounts=re.findall(r'(?:US\s*)?\$\s*(\d+(?:\.\d+)?)\s*(?:million|m)\b',sentence,re.I)
            if any(0<float(value)<40 for value in amounts):return 'startup_sub40m_small_size_prior_not_observed_count'
    return ''
