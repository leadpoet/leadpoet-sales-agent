"""Company-attributed public workforce evidence and bounded provability ranking."""
import asyncio,re
from html.parser import HTMLParser
from agent.safe_urls import urlsplit
from agent import v12_llm, v15_budget, judge_mirror
from agent.evidence import _clean, _parse_date
from agent.deadline import BudgetExhausted

SEARCH_DOMAINS=['zoominfo.com','rocketreach.co','growjo.com','craft.co','cbinsights.com',
                'tracxn.com','pitchbook.com','crunchbase.com','owler.com','theorg.com']
PREFERRED=['reveliolabs.com','growjo.com','getlatka.com','leadiq.com','zoominfo.com']
V23_PREFERRED=['pitchbook.com','zoominfo.com','tracxn.com','caplight.com','fundup.ai','growjo.com','crunchbase.com']
MAX_SEARCHES_PER_COMPANY=2
MAX_SEARCHES_PER_ICP=6
BAD_PAGE=re.compile(r'\bdepartment\b|email\s+format|org(?:anization)?\s*chart|\bpeople\b',re.I)
NUMBER=r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\s*[-–—]\s*(?:\d{1,3}(?:,\d{3})+|\d+))?'
COUNTS=re.compile(r'(?<![\w$])('+NUMBER+r')\s+(?:full[- ]time\s+)?employees?\b|'
                  r'\b(?:employs|team\s+of|employees?\s*:)\s*(?:approximately\s+|about\s+)?('+NUMBER+r')\b',re.I)
NEGATIVE=re.compile(r'\b(?:plans? to|will (?:hire|employ)|aims? to|former|previously|used to|not|no longer)\b',re.I)
OTHER_OWNER=re.compile(r"(?:'s|’s)\s*(?:customer|client|partner|investor|department|division|subsidiary)\b|"
                       r'\b(?:customers?|clients?|partners?|investors?|departments?|divisions?|subsidiar(?:y|ies))\b',re.I)


def domain(url):
    from agent.v92_common import domain as normalize_domain
    return normalize_domain(url)


def names(text,name):
    return bool(name and re.search(r'(?<!\w)'+r'\s*'.join(map(re.escape,name.split()))+r'(?!\w)',str(text or ''),re.I))


def bucket(value):
    return (judge_mirror.normalize_employee_count_bucket(value,default=None)
            or judge_mirror.normalize_observed_employee_count_bucket(value,default=None))

def linkedin_slug(url):
    parsed=urlsplit(str(url or ''))
    if parsed.scheme not in ('http','https') or (parsed.hostname or '').lower() not in ('linkedin.com','www.linkedin.com'):
        return ''
    match=re.fullmatch(r'/company/([A-Za-z0-9_-]+)/?',parsed.path)
    return match.group(1).lower() if match else ''


def is_linkedin(url):
    host=(urlsplit(str(url or '')).hostname or '').lower()
    return host=='linkedin.com' or host.endswith('.linkedin.com')


def evidence(cand,row,allowed,evaluation_date=None,*,linkedin=False):
    url=str(row.get('url') or '');host=urlsplit(url).hostname
    if not host or urlsplit(url).scheme not in ('http','https') or row.get('error'):
        return None
    if not linkedin and (is_linkedin(url) or BAD_PAGE.search(str(row.get('title') or '')) or re.search(r'\bLinkedIn\b',str(row.get('title') or ''),re.I)):
        return None
    published=_parse_date(str(row.get('date') or row.get('datePublished') or '')[:10])
    if evaluation_date and published and published>evaluation_date:return None
    text=str(row.get('text') or '')
    aliases=list(dict.fromkeys([cand['company_name'],cand.get('_bound_name') or cand['company_name']]))
    # Each quote is an original span. A matching title alone cannot attribute
    # another company's sentence or a department's workforce to this company.
    for segment in re.split(r'(?<=[.!?;])\s+',text):
        if NEGATIVE.search(segment) or OTHER_OWNER.search(segment):continue
        if not any(names(segment,n) for n in aliases):continue
        for match in COUNTS.finditer(segment):
            preceding=[]
            for name in aliases:
                pattern=r'(?<!\w)'+r'\s+'.join(map(re.escape,name.split()))+r'(?!\w)'
                preceding.extend(m.end() for m in re.finditer(pattern,segment[:match.start()],re.I))
            if not preceding:continue
            bridge=segment[max(preceding):match.start()]
            words=re.findall(r'[A-Za-z]+',bridge.lower())
            permitted={'has','have','currently','now','approximately','about','around','a','an','global','worldwide','total',
                       'team','of','full','time','employees','employee','count','company','size','employs','over','under',
                       'more','than','less','at','least','up','to','nearly','inc','ltd','llc'}
            if len(bridge)>100 or any(w not in permitted for w in words):continue
            if match.group(1) and not re.search(r'\b(?:has|have|employs|team|company\s+size)\b',bridge,re.I):continue
            if (match.start()>0 and segment[match.start()-1] in '0123456789,.%') or re.match(r'[+%\d]|\.\d',segment[match.end():]):continue
            literal=(match.group(1) or match.group(2)).replace('–','-').replace('—','-')
            # Lower/upper bounds and percentages are not observed exact counts.
            prefix=segment[max(0,match.start()-25):match.start()]
            if re.search(r'(?:over|under|more than|less than|at least|up to|nearly)\s*$',prefix,re.I):continue
            if re.search(r'\b(?:hired|hiring|added|laid off|layoffs|cut|reduced|lost)\b',segment[:match.start()],re.I):continue
            count=literal if '-' in literal else int(literal.replace(',',''))
            band=bucket(count)
            if not band:continue
            return {'url':url,'host':host.lower(),'quote':_clean(segment),'count':count,'band':band,
                    'in_band':band in allowed,'page':row}
    return None


def find_evidence(cand,rows,allowed,evaluation_date):
    found=[e for row in rows if (e:=evidence(cand,row,allowed,evaluation_date))]
    def priority(e):
        host=domain(e['url'])
        hosts=V23_PREFERRED if v12_llm.enabled('V23_SIZE_HINTS') else PREFERRED
        return hosts.index(host) if host in hosts else len(hosts)
    good=sorted((e for e in found if e['in_band']),key=priority)
    return (good[0] if good else None),next((e for e in found if not e['in_band']),None)


class Anchors(HTMLParser):
    def __init__(self):super().__init__();self.urls=[]
    def handle_starttag(self,tag,attrs):
        from agent.safe_urls import valid
        if tag.lower()=='a':self.urls.extend(v for k,v in attrs if k.lower()=='href' and valid(v))


def homepage_anchor(home,website):
    if not home or domain(home.get('url'))!=domain(website) or urlsplit(home.get('url','')).path.strip('/'):
        return ''
    parser=Anchors();parser.feed(str(home.get('html') or ''))
    urls=parser.urls+[x.get('url','') for x in home.get('links',[]) if isinstance(x,dict)]
    # Markdown links are produced from the served homepage, not guessed slugs.
    urls+=re.findall(r'\]\((https?://[^\s)]+)\)',str(home.get('text') or ''))
    slugs={slug for url in urls if (slug:=linkedin_slug(url))}
    return next(iter(slugs)) if len(slugs)==1 else ''


def linkedin_size(cand,rows,slug,allowed):
    for row in rows:
        if linkedin_slug(row.get('url'))!=slug:continue
        text=str(row.get('text') or '')
        if not any(names(str(row.get('title',''))+' '+text,n) for n in (cand['company_name'],cand.get('_bound_name') or cand['company_name'])):continue
        match=re.search(r'\bCompany\s+size\s*[:\n]?\s*('+NUMBER+r')\s+employees\b',text,re.I)
        if not match:continue
        count=match.group(1).replace('–','-').replace('—','-');band=bucket(count)
        if band:return {'url':row['url'],'host':'www.linkedin.com','quote':match.group(0),'count':count,
                        'band':band,'in_band':band in allowed,'page':row}
    return None


def state(run):
    if not hasattr(run,'v21_size_state'):
        run.v21_size_state={'searches':{},'linkedin_calls':set(),'records':{}}
    return run.v21_size_state


def room(run):return run.remaining()>=8 and run.used.get('deepline',0)<29


async def lookup(run,name,args):
    # Retain time to record and publish an already-admissible fallback slot.
    async with asyncio.timeout(max(0,min(20,run.remaining()-3))):
        return await run.tool(name,args)


async def enrich(run,cand,company,raw_icp,trace):
    if v12_llm.enabled('V33_SIZE_PATH'):
        from agent import v33_size
        return await v33_size.enrich(run,cand,company,raw_icp,trace)
    from agent import v15_pipeline as p
    st=state(run);key=domain(company['company_website']);allowed=judge_mirror.employee_count_buckets_for_icp(raw_icp)
    home=next((r for r in p.cached_rows(run) if r.get('url')==company['company_website']),{})
    if v12_llm.enabled('V23_SIZE_HINTS'):
        home=next((r for r in p.cached_rows(run) if homepage_anchor(r,company['company_website'])),home)
    bound=dict(cand,_bound_name=company['company_name'],domain=key)
    slug=homepage_anchor(home,company['company_website']) if (v12_llm.enabled('V21_LINKEDIN_CHECK') or v12_llm.enabled('V23_SIZE_HINTS')) else ''
    proof=None;conflict=None;source='none';line='unchecked';li_proof=None
    if v12_llm.enabled('V21_SIZE_EVIDENCE'):
        proof,conflict=find_evidence(bound,p.cached_rows(run),allowed,run.eval_date)
        if proof:source='cache'
        if not proof and not conflict:
            queries=[{'query':f'{company["company_name"]} {key} employees headcount company size'},
                     {'query':f'"{company["company_name"]}" {key} number of employees','includeDomains':SEARCH_DOMAINS}]
            if v12_llm.enabled('V23_SIZE_HINTS'):
                queries=[{**queries[1],'includeDomains':V23_PREFERRED},queries[0]]
            for args in queries[:MAX_SEARCHES_PER_COMPANY]:
                if not room(run) or sum(st['searches'].values())>=MAX_SEARCHES_PER_ICP:break
                if st['searches'].get(key,0)>=MAX_SEARCHES_PER_COMPANY:break
                st['searches'][key]=st['searches'].get(key,0)+1
                try:
                    with v15_budget.work('v21_size',key,primary=True):
                        result=await lookup(run,'exa_search',{**args,'numResults':5,'contents':{'text':{'maxCharacters':3000}}})
                except (BudgetExhausted,TimeoutError):break
                # Provider projections are shorter than the actual retained text.
                returned=result.get('results') or []
                urls={r.get('url') for r in returned}
                full=[r for r in p.cached_rows(run) if r.get('url') in urls]
                by_url={r['url']:r for r in returned}
                for row in full:
                    if len(row.get('text',''))>len(by_url.get(row['url'],{}).get('text','')):by_url[row['url']]=row
                proof,conflict=find_evidence(bound,list(by_url.values()),allowed,run.eval_date)
                trace('size_evidence.lookup',{'company':company['company_name'],'route':'providers' if 'includeDomains' in args else 'open_web',
                    'rows':len(returned),'found':bool(proof),'conflict':bool(conflict)})
                if proof:source='exa';break
                if conflict:break
    if proof and v12_llm.enabled('V20R10_COST'):
        trace('linkedin_check.skipped',{'company':company['company_name'],'reason':'p1_page_found'})
    if not proof and not conflict and slug and v12_llm.enabled('V21_LINKEDIN_CHECK') and key not in st['linkedin_calls'] and room(run):
        st['linkedin_calls'].add(key)
        try:
            with v15_budget.work('v21_linkedin',key,primary=True):
                response=await lookup(run,'exa_contents',{'urls':['https://www.linkedin.com/company/'+slug], 'max_chars':4000})
            li_proof=linkedin_size(bound,(response.get('results') or [])+p.cached_rows(run),slug,allowed)
            line='present' if li_proof else 'absent'
            if li_proof and not li_proof['in_band']:conflict=li_proof
        except (BudgetExhausted,TimeoutError):line='unchecked'
    stage_cached=bool(cand.get('_phase_a',{}).get('components',{}).get('affirmed_stage_from_cache'))
    score=3*bool(proof)+2*bool(li_proof and li_proof['in_band'])+int(stage_cached)
    record={'company':company['company_name'],'website':company['company_website'],
            'size_evidence_source':source,'size_evidence_host':proof['host'] if proof else '',
            'linkedin_anchor':bool(slug),'linkedin_slug':slug,'linkedin_size_line':line,'provability_rank':score,
            'a_score':cand.get('_phase_a',{}).get('score') or 0,'non_linkedin_proven':bool(proof),
            'provable':bool(proof or (li_proof and li_proof['in_band'])),
            'size_evidence_url':proof['url'] if proof else '', 'size_evidence_quote':proof['quote'] if proof else '',
            'supported_band':(proof or li_proof or {}).get('band'),'primary_quote':company['intent_signals'][0]['snippet'],
            'primary_url':company['intent_signals'][0]['url'],'conflict':bool(conflict and not proof)}
    st['records'][key]=record
    if record['conflict']:
        trace('candidate.loss',{'company':company['company_name'],'stage':'size_evidence','reason':'size_evidence_conflict',
                               'observed_band':conflict['band'],'url':conflict['url'],'quote':conflict['quote']})
        return None
    updated=dict(company)
    if proof or li_proof:
        selected=proof or li_proof
        updated['employee_count']=selected['band']
        # Preserve the exact hint positions; stage remains in fit_summary.
        updated['fit_evidence_urls']=[selected['url'],company['company_website'],company['intent_signals'][0]['url']]
        updated['fit_summary']=re.sub(r'^Employee bucket .*?; headquarters',f"Employee bucket {selected['band']} (observed); headquarters",company['fit_summary'])
        updated['fit_summary']+=' Headcount evidence: '+selected['quote'][:600]
    if v12_llm.enabled('V23_SIZE_HINTS'):
        hints=list(updated.get('fit_evidence_urls') or [company['company_website']])
        linkedin='https://www.linkedin.com/company/'+slug if slug else ''
        if linkedin:
            # P1 remains first; the actual homepage anchor fixes the company slug.
            hints=[hints[0],linkedin]+hints[1:]
        updated['fit_evidence_urls']=list(dict.fromkeys(hints))[:3]
        trace('size_evidence.hints',{'company':company['company_name'],
              'urls':updated['fit_evidence_urls'],'homepage_linkedin_anchor':bool(slug)})
    trace('size_evidence.assessed',record)
    if v12_llm.enabled('V20R8_STRICT'):
        from agent.v20r8_strict import final_size
        return final_size(updated,raw_icp,trace,cand=cand,run=run)
    return updated


def rank(run,company):
    r=state(run)['records'].get(domain(company['company_website']),{})
    return (r.get('provability_rank',0),r.get('a_score',0))


def slots_provable(run,companies):
    records=state(run)['records']
    return bool(companies) and all(records.get(domain(c['company_website']),{}).get('provable') for c in companies)
