"""Fail-closed attribution and size checks; no searches or replacement claims."""
import re
import unicodedata
from agent import judge_mirror, v21_size
from agent.v92_common import domain


def normalized(text):
    return ' '.join(re.findall(r'[a-z0-9]+', unicodedata.normalize('NFKD', str(text)).casefold()))


def aliases(company):
    name=normalized(company.get('company_name',''))
    values={name}
    short=re.sub(r'(?:\s+(?:inc|llc|ltd|limited|corporation|corp|health|care))+$','',name)
    if len(short)>=4:values.add(short)
    host=domain(company.get('domain') or company.get('company_website') or '')
    if host and not host.startswith('name:'):
        brand=normalized(host.split('.')[0])
        if len(brand)>=4:values.add(brand)
    return {x for x in values if x}


def named(text, names):
    clean=' '+normalized(text)+' '
    return any(' '+n+' ' in clean for n in names)


EVENT=re.compile(r'\b(?:announc(?:e[sd]?|ing)|launch(?:es|ed|ing)?|unveil(?:s|ed|ing)?|introduc(?:es?|ed|ing)|'
                 r'rais(?:es?|ed|ing)|secur(?:es?|ed|ing)|clos(?:es?|ed|ing)|highlight(?:s|ed)|'
                 r'expand(?:s|ed|ing)|hir(?:es?|ed|ing)|receiv(?:es?|ed|ing))\b',re.I)
ANAPHOR=re.compile(r'^(?:the\s+(?:company|platform|team|most|new|program|service)|it\b|its\b|we\b|our\b|'
                   r'during\b.*?\bwe\b|now\b|today\b)',re.I)


def leading_company(text,names):
    # Ignore a standard dateline separator, not an arbitrary article prefix.
    text=re.split(r'\s(?:--|—)\s',text)[-1]
    text=re.sub(r'^(?:today|yesterday|the company)\s+','',normalized(text))
    return any(text==n or text.startswith(n+' ') for n in names)


def subject(company, quote, page=None):
    """Only the quote or preceding sentence; titles/article mentions cannot bind."""
    names=aliases(company);quote=str(quote or '').strip()
    if not quote or not names:return False
    event=EVENT.search(quote)
    if named(quote,names):
        if not event:return True
        before=quote[:event.start()]
        # A named other actor cannot become ours through an object/partner mention.
        if not named(before,names) and not ANAPHOR.search(quote):return False
        if named(before,names) and not leading_company(before,names) and not ANAPHOR.search(quote):return False
        for n in names:
            if re.search(r'\b(?:for|with|customer of|partner of)\s+'+re.escape(n)+r'\b',normalized(before)):
                return False
        return True
    # Pronouns can bind to an immediately preceding company sentence, never to
    # a distant article mention or to a competing explicit event subject.
    if not ANAPHOR.search(quote):return False
    text=str((page or {}).get('text',''))
    match=re.search(r'\s+'.join(map(re.escape,quote.split())),text)
    if not match:return False
    at=match.start()
    paragraph=re.split(r'\n\s*\n',text[max(0,at-500):at].strip())[-1]
    previous=re.split(r'(?<=[.!?])\s+(?=[A-Z])',paragraph)[-1]
    return leading_company(previous,names)


def final_size(company,icp,trace,*,cand=None,run=None):
    if not company:return None
    allowed=judge_mirror.employee_count_buckets_for_icp(icp)
    band=v21_size.bucket(company.get('employee_count'))
    reason='employee_claim_outside_icp' if not band or band not in allowed else ''
    if cand:
        from agent import v15_pipeline as p
        facts=p.grounded_facts(cand.get('_verdict_rows') or [],cand.get('_verdict') or {})
        observed=v21_size.bucket(facts.get('employees',{}).get('value'))
        record=getattr(run,'v21_size_state',{}).get('records',{}).get(domain(company.get('company_website')), {})
        if observed and observed not in allowed and not (record.get('provable') and record.get('supported_band') in allowed):
            reason=reason or 'observed_employee_band_outside_icp'
    if reason:
        trace('candidate.loss',{'company':company.get('company_name'),'stage':'r8_output_guard','reason':reason,
                              'claimed_band':company.get('employee_count'),'allowed':allowed})
        return None
    return company
