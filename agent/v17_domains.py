"""Preserve unresolved names, carry observed domain hints, then bind identity."""
import re
from agent.safe_urls import urlsplit
from urllib.parse import parse_qs, unquote
from agent import p6_identity,judge_mirror,v15_budget
from agent.evidence import _norm,_clean
from agent.v92_common import domain,signal_url_ok
from agent.deadline import BudgetExhausted

PUBLISHERS={'prnewswire.com','businesswire.com','globenewswire.com','newswire.com','einpresswire.com',
            'morningstar.com','reuters.com','techcrunch.com','thenextweb.com','businessinsider.com',
            'crunchbase.com','linkedin.com','facebook.com','wikipedia.org','youtube.com'}

def key(cand):
    # Unverified domains are hints: two blank/same guessed hosts must not merge names.
    name=re.sub(r'\b(?:inc|llc|ltd|limited|corporation)\b','',_norm(cand.get('company_name',''))).strip()
    return 'name:'+name

def related_host(name,host):
    stem=domain(host).split('.')[0]
    compact=re.sub('[^a-z0-9]','',_norm(name))
    return len(stem)>=3 and (stem==compact or stem in {compact+'ai','get'+compact,'use'+compact} or
                            (len(compact)>4 and compact==stem+'ai'))

def eligible(host):
    return bool(host and host not in PUBLISHERS and signal_url_ok('https://'+host+'/'))

def model_hints(row):
    """Bound model-only link hints; retain the full URLs in the evidence cache."""
    hosts=[]
    for link in row.get('links',[]):
        value=link.get('url','') if isinstance(link,dict) else str(link)
        try:targets=parse_qs(urlsplit(value).query).get('u',[])+[value]
        except ValueError:continue
        for target in targets:
            host=domain(unquote(target))
            if eligible(host) and len(host)<=90 and host not in hosts:hosts.append(host)
            if len(hosts)==4:return hosts
    return hosts

def hint(name,guess,rows):
    supplied=domain(guess)
    if eligible(supplied):return supplied,'model_hint'
    # Own-site rows are recognized by brand/host alignment, never by article title alone.
    for row in rows:
        host=domain(row.get('url'))
        if eligible(host) and related_host(name,host):return host,'own_site_row'
    for row in rows:
        text=' '.join(str(row.get(k) or '') for k in ('title','text','snippet'))
        for value in re.findall(r'(?<![\w@])(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9.-]*\.(?:com|io|ai|co\.uk))\b',text,re.I):
            host=domain(value)
            if eligible(host) and related_host(name,host):return host,'cached_domain_mention'
        # Already retrieved outbound links, including common wire redirect targets.
        for link in row.get('links',[]):
            value=link.get('url','') if isinstance(link,dict) else str(link)
            parsed=urlsplit(value);targets=[value]+parse_qs(parsed.query).get('u',[])
            for target in targets:
                host=domain(unquote(target))
                if eligible(host) and related_host(name,host):return host,'cached_outbound_link'
    return '','pending_identity'

def parse(items,chunk,trace):
    result=[]
    for item in items:
        if not isinstance(item,list) or len(item)!=3 or not isinstance(item[2],list):continue
        name=_clean(item[0]);selected=[chunk[i] for i in item[2] if type(i) is int and 0<=i<len(chunk)]
        if not name or not selected:
            trace('candidate.loss',{'company':name,'stage':'candidates_extracted','reason':'missing_name_or_row_reference'});continue
        host,source=hint(name,item[1],selected)
        result.append({'company_name':name,'domain':host,'urls':list(dict.fromkeys(r['url'] for r in selected)),
                       '_pending_identity':not bool(host),'_domain_hint_source':source})
    return result

def bind(page,cand):
    host=domain(page.get('url'));text=_clean(page.get('text'))
    if not eligible(host) or page.get('error') or page.get('ok') is False or len(text)<80:return None
    if urlsplit(page.get('url','')).path.strip('/'):return None
    if judge_mirror.check_antibot_wall(text) or re.search(r'domain (?:is )?for sale|buy this domain',text[:600],re.I):return None
    canonical=domain(page.get('canonical_url'))
    if canonical and canonical!=host:return None
    name=p6_identity.brand(page,cand['company_name'],host)
    if not name and re.search(r'\b(?:every|our|the)\s+'+re.escape(cand['company_name'])+r'\s+(?:product|platform|team|solution)s?\b',text,re.I):
        name=cand['company_name']
    if not name:return None
    return page,{'name':name,'website':page['url'],'source':'company_homepage'},host

async def resolve(run,cand,rows,trace):
    identity_key=key(cand)
    cache=getattr(run,'v17_identities',None)
    if cache is None:run.v17_identities={};cache=run.v17_identities
    if identity_key in cache:return cache[identity_key]
    host,source=hint(cand['company_name'],cand.get('domain'),rows)
    for page in rows:
        if host and domain(page.get('url'))==host:
            bound=bind(page,cand)
            if bound:cache[identity_key]=bound;return bound
    attempted=getattr(run,'v17_identity_attempted',None)
    if attempted is None:run.v17_identity_attempted=set();attempted=run.v17_identity_attempted
    if len(attempted)>=8:
        trace('identity.pending',{'company':cand['company_name'],'reason':'eight_candidate_limit'});return None
    attempted.add(identity_key)
    if host:
        try:
            with v15_budget.work('v17_identity',identity_key,primary=True):
                response=await run.tool('exa_contents',{'urls':['https://'+host+'/']})
            for page in response.get('results',[]):
                bound=bind(page,cand)
                if bound:cache[identity_key]=bound;return bound
        except BudgetExhausted as exc:
            trace('identity.attempt_failed',{'company':cand['company_name'],'step':'homepage','reason':str(exc)})
        except Exception as exc:
            trace('identity.attempt_failed',{'company':cand['company_name'],'step':'homepage','reason':type(exc).__name__})
    try:
        with v15_budget.work('v17_identity',identity_key,primary=True):
            found=await run.tool('exa_company_search',{'query':cand['company_name'],'numResults':3,
                'contents':{'text':{'maxCharacters':3000}}})
        for page in found.get('results',[]):
            # The company search must actually return official-homepage content;
            # a name, entity field or directory snippet alone does not bind it.
            bound=bind(page,cand)
            if bound:
                run.page_cache[page['url']]=page;cache[identity_key]=bound;return bound
    except BudgetExhausted as exc:
        trace('identity.attempt_failed',{'company':cand['company_name'],'step':'company_search','reason':str(exc)})
    except Exception as exc:
        trace('identity.attempt_failed',{'company':cand['company_name'],'step':'company_search','reason':type(exc).__name__})
    cache[identity_key]=None
    return None
