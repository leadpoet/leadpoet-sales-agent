"""Retain open hiring evidence and corroborating pages for the same event."""
import re
from agent.safe_urls import urlsplit, urlunsplit
from urllib.parse import parse_qsl, urlencode
from agent import v12_llm
from agent.v20r5_primary import category
from agent.evidence import _clean,_snippet_on_page

CLOSED=re.compile(r'\b(?:no longer accepting applications|no longer available|position (?:has been |is )?(?:filled|closed)|job (?:has |is )?(?:expired|closed)|applications? (?:are )?closed|not accepting applications|not currently hiring|no (?:current |open )?(?:vacancies|positions|roles))\b',re.I)
OPEN=re.compile(r'\b(?:is hiring|are hiring|we.re hiring|now hiring|currently hiring|open (?:roles|positions|vacancies)|apply (?:now|today|for this)|accepting applications|join (?:our|the) team|expands? (?:its |the )?team.{0,100}(?:hiring|recruit))\b',re.I)

def hiring_ok(icp,quote,page):
    if category(icp)!='HIRING':return True
    parsed=urlsplit(str((page or {}).get('url') or ''));host=(parsed.hostname or '').lower()
    if (host=='linkedin.com' or host.endswith('.linkedin.com')) and re.search(r'/(?:jobs|job)(?:/|$)',parsed.path,re.I):return False
    text=str((page or {}).get('text') or '')
    if CLOSED.search(text+' '+str(quote)):return False
    # A careers path alone says nothing about whether any role remains open.
    return bool(OPEN.search(str(quote)))

def instruction(icp):
    if category(icp)!='HIRING':return ''
    return ('HIRING evidence must establish currently open hiring. Exclude ALL LinkedIn job postings, '
            'even when open. Prefer employer careers pages, Ashby/Greenhouse/Lever ATS pages or press '
            'with explicit open roles, accepting applications or current hiring. Closed/expired/filled '
            'postings and "no longer accepting applications" are not evidence. A careers URL alone is insufficient. ')

def canonical(url):
    p=urlsplit(str(url or ''))
    if p.scheme not in ('https','http') or not p.hostname:return ''
    query=urlencode([(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True)
                     if not k.lower().startswith('utm_') and k.lower() not in ('gclid','fbclid')])
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path.rstrip('/') or '/',query,''))

def event_detail(company,quote):
    # Generic "company launched a new platform" wording cannot distinguish
    # separate launches. Keep specific event terms after removing boilerplate.
    ignored=set(re.findall(r"[a-z]+",company.get('company_name','').lower()))
    ignored.update('the a an and of for to its our their today yesterday company announced announces announce launched launches launch unveils unveiled introduces introduced new product service services capability capabilities program platform solution initiative feature tool with now available general availability'.split())
    terms={w for w in re.findall(r'[a-z]+',quote.lower()) if len(w)>2 and w not in ignored}
    return len(terms)>=2

def broaden(company,icp,rows,trace):
    """The public one-URL schema groups repeated criterion indexes as evidence.

    Only a verbatim repeat of the selected event sentence is safe to join
    without another assessment. Similar topics or dates alone are insufficient.
    """
    if not company or not v12_llm.enabled('V20R10_EVIDENCE'):return company
    from agent.v20r8_strict import subject
    from agent.v20r9_intent import matches
    from agent.v20r5_primary import funding_mismatch
    first=company['intent_signals'][0];primary_page=next((r for r in rows if canonical(r.get('url'))==canonical(first['url'])),{})
    selected=[first];seen={canonical(first['url'])}
    # A copied original event sentence gives corroboration, never another event.
    for row in rows:
        url=canonical(row.get('url'))
        if not url or url in seen or row.get('error'):continue
        if not event_detail(company,first['snippet']):continue
        if _clean(first['snippet']).casefold() not in _clean(str(row.get('text') or '')).casefold():continue
        quote=_snippet_on_page(first['snippet'],str(row.get('text') or ''))
        if not quote or not subject(company,quote,row) or funding_mismatch(icp,quote) or not matches(icp,quote,row):continue
        if v12_llm.enabled('V20R10_HIRING') and not hiring_ok(icp,quote,row):continue
        # Explicitly different event dates cannot corroborate the same event.
        from agent.v92_common import observed_date
        d=observed_date(row);original=observed_date(primary_page)
        if d and original and d!=original:
            from agent.evidence import _parse_date
            a,b=_parse_date(d),_parse_date(original)
            if a and b and abs((a-b).days)>7:continue
        selected.append({**first,'url':row['url'],'snippet':quote[:600],'description':quote[:350]})
        seen.add(url)
        if len(selected)==3:break
    # Preserve separate buyer criteria. Other events with index 0 are not
    # silently combined with this event, and identical URLs get no extra entry.
    rest=[s for s in company['intent_signals'][1:] if s.get('matched_icp_signal')!=0]
    trace('evidence.bundle',{'company':company['company_name'],'matched_icp_signal':0,
        'urls':[s['url'] for s in selected],'same_event_basis':'verbatim selected event sentence'})
    return dict(company,intent_signals=selected+rest)
