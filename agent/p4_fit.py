"""P4: Only observed fit fields and own-domain stage hints."""
from agent.safe_urls import urlsplit
from agent.v92_common import domain, enabled
from agent.evidence import _clean, _norm
def fit_fields(result, cand, stage_page, profile, primary_text):
    """Only observed hints; the summary repeats accepted fields, not guesses."""
    result['company_linkedin'] = ''
    if not enabled('V92_FIT_HINTS'):
        return
    home = result['company_website']
    stage_url = stage_page.get('url', '') if stage_page else ''
    if domain(stage_url) != domain(home):
        stage_url = ''
    if not stage_url:
        result['company_stage'] = ''
    links = cand.get('_home', {}).get('links', []) + cand.get('_stage_links', [])
    linkedin = next((x['url'] for x in links if domain(x.get('url')) == 'linkedin.com'
                     and urlsplit(x['url']).path.startswith('/company/')), '')
    if cand.get('_omit_linkedin_hint'):
        linkedin = ''
    result['fit_evidence_urls'] = list(dict.fromkeys(u for u in (home, stage_url, linkedin) if u))[:3]
    state = result.get('state', '')
    observed_text = ' '.join((_clean(primary_text), _clean(profile.get('location')), _clean(cand.get('_home', {}).get('text'))))
    if state and _norm(state) not in _norm(observed_text):
        result['state'] = ''
    result['fit_summary'] = (f"{result['company_name']}: {result['industry']}; observed employee band {result['employee_count']}; "
                             f"stage {result['company_stage']}; headquarters {result['country']}"
                             + (f", {result['state']}" if result['state'] else '') + '.')[:500]
