"""P6: Confirm the final homepage domain and observed brand within three calls."""
import re
from agent.safe_urls import urlsplit
from arena_transport import ArenaToolClient
from agent.v92_common import domain, signal_url_ok, page_metadata
from agent.evidence import _norm, _clean
from agent.deadline import request_timeout
class IdentityToolClient(ArenaToolClient):
    """Use the shared Deepline page cache and physical-request deadline."""
    @property
    def timeout(self):
        # Rechecked for every physical request, including multi-request tools.
        return request_timeout(self._configured_timeout)

    @timeout.setter
    def timeout(self, value):
        self._configured_timeout = float(value)

def brand(page, name, d):
    choices = [_clean(page.get('site_name'))] + re.split(r'\s+[|—–-]\s+|:\s+', _clean(page.get('title')))
    wanted = _norm(name)
    def related(value):
        n = _norm(value)
        return bool(n and wanted and (n == wanted or n.startswith(wanted + ' ') or wanted.startswith(n + ' ')))
    choices = [x for x in choices if 2 < len(x) <= 100 and len(x.split()) <= 8 and related(x)]
    return min(choices, key=len, default='')

async def resolve_identity(run, cand):
    """D2: homepage, optional official-site lookup, confirmed homepage (≤3)."""
    original = domain(cand.get('domain'))
    calls = 0
    async def home(d):
        nonlocal calls
        if not d or calls >= 3 or run.remaining() < 10:
            return {}
        url = f'https://{d}/'
        if url not in run.identity_pages:
            calls += 1
            run.identity_pages[url] = await run.tool('fetch_page', {'url': url, 'max_chars': 4000})
        return run.identity_pages[url]
    def accept(page, requested):
        text = _clean(page.get('text'))
        actual = domain(page.get('url') or requested)
        canonical = domain(page.get('canonical_url'))
        if page.get('error') or page.get('ok') is False or len(text) < 80 or not actual:
            return None
        if canonical and canonical != actual:
            return None  # canonical is not proof of a followed redirect
        if re.search(r'domain (?:is )?for sale|buy this domain|verify (?:that )?you are human|access denied', text[:400], re.I):
            return None
        observed = brand(page, cand['company_name'], actual)
        if not observed or not signal_url_ok(f'https://{actual}/'):
            return None
        result = {**cand, 'domain': actual, 'company_name': observed, '_home': page}
        if actual != original:
            result.pop('entity', None)
        return result
    first = await home(original)
    result = accept(first, original)
    if result:
        return result
    replacement = domain(first.get('canonical_url'))
    if not replacement or replacement == original:
        calls += 1
        lookup = await run.tool('search_web', {'query': f"{cand['company_name']} official website", 'mode': 'search', 'limit': 5})
        replacement = next((domain(r.get('url')) for r in lookup.get('results', [])
                            if domain(r.get('url')) != original and signal_url_ok(r.get('url', ''))
                            and _norm(cand['company_name']) in _norm(r.get('title', '') + ' ' + r.get('snippet', ''))), '')
    return accept(await home(replacement), replacement) if replacement else None
