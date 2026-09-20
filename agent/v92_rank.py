"""Rank the verified pool without adding calls or a new rejection gate."""
from agent.safe_urls import urlsplit
from agent.v92_common import domain, enabled

def size_anchor(cand, company):
    homepage_link = any(domain(x.get('url')) == 'linkedin.com' and urlsplit(x.get('url', '')).path.startswith('/company/') for x in cand.get('_home', {}).get('links', []))
    try:
        large = float(str(company.get('employee_count') or 0).replace(',', '')) >= 51
    except (TypeError, ValueError):
        large = False
    return homepage_link, large

def order(companies, anchors):
    if not enabled('V92_SIZE_ANCHOR'):
        return list(companies)
    def key(c):
        linked, large = anchors.get(domain(c['company_website']), (False, False))
        return linked, len(c.get('intent_signals', [])) >= 2, large
    return sorted(companies, key=key, reverse=True)
