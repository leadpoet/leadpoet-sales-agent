"""Four category-event queries and three startup stage queries; no extra calls."""
from agent import v18_fit,v20r5_primary,v20_phases,v12_llm

def plan(icp,evaluation_date):
    kind=v20r5_primary.category(icp);niche=icp.get('sub_industry') or icp.get('industry') or ''
    country=icp.get('country') or '';stage=v18_fit.category(icp.get('company_stage'))
    prefix=f'{niche} {country}'+(' startup' if stage in ('Seed','Series A','Series B') else '')
    if kind=='HIRING':
        terms=['"is hiring" "open roles"','"expands team" hiring','careers jobs (site:ashbyhq.com OR site:greenhouse.io OR site:lever.co)','"open positions" sales marketing operations']
    elif kind=='PRODUCT_LAUNCH':
        terms=['launches "new product"','introduces "new service"','unveils "new platform"','launches "new capability"']
    elif kind=='FUNDING':terms=['raises funding','closed investment round','secures financing','announces completed round']
    else:terms=[str(icp['required_intents'][0]['signal'])+' '+suffix for suffix in ('announces','approved','introduces','news')]
    if kind=='HIRING' and v12_llm.enabled('V20R10_HIRING'):
        terms=[t+' -site:linkedin.com/jobs' for t in terms]
    if v12_llm.enabled('V23_EARLY_SUPPLY') and stage == 'Series A':
        # Search the employer's business, not sales/marketing jobs at any firm.
        if kind == 'HIRING' and 'sales' in str(icp.get('industry','')).lower() and 'marketing' in str(icp.get('industry','')).lower():
            prefix=f'(sales software OR marketing software OR revenue intelligence) {country} startup'
        if kind in ('HIRING','PRODUCT_LAUNCH'):
            prefix+=' "Series A"'
    events=[prefix+' '+term for term in terms]
    stage_queries=v20_phases.stage_queries(icp,evaluation_date)
    if stage in ('Seed','Series A','Series B'):
        stage_queries=[q if 'startup' in q.lower() else q+' startup' for q in stage_queries]
    if v12_llm.enabled('V23_EARLY_SUPPLY') and stage == 'Series A' and kind == 'HIRING' and 'sales' in str(icp.get('industry','')).lower() and 'marketing' in str(icp.get('industry','')).lower():
        stage_queries=[f'(sales software OR marketing software OR revenue intelligence) {country} startup "Series A" '+suffix
                       for suffix in ('raises funding','raised investment','closed round')]
    return {'category':kind,'event_queries':events,'stage_queries':stage_queries,
            'queries':events+stage_queries,'exclude_via_plausibility':'public companies, retail chains and public brands; no industry-label-only veto'}

def event_query(cand,icp,year):
    name=cand['company_name'].replace('"','');kind=v20r5_primary.category(icp)
    terms='("is hiring" OR "open roles" OR "expands team") careers jobs' if kind=='HIRING' else '(launches OR unveils OR introduces) new product service capability'
    if kind=='HIRING' and v12_llm.enabled('V20R10_HIRING'):terms+=' -site:linkedin.com/jobs'
    return f'"{name}" {terms} {year}'
