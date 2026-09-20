"""P2: Bounded company-owned event retrieval, then domain-bound press search."""
from __future__ import annotations
import re
from agent import v92_common as v9
from agent.evidence import _bucket_for, _clean, _parse_date, _US_STATE_RE
from datetime import timedelta
from agent.safe_urls import urlsplit
HUB = re.compile(r'(?<![a-z])(?:news|press|newsroom|blog|changelog|releases)(?![a-z])', re.I)


def brand_bound(cand, row):
    """Exact token plus compound-name boundary, or an explicit domain mention."""
    text = _clean(' '.join(str(row.get(k) or '') for k in ('title', 'text', 'snippet')))
    name = _clean(cand['company_name'])
    pattern = r'(?<![\w])' + re.escape(name) + r'(?![\w])'
    matches = list(re.finditer(pattern, text, re.I))
    if not matches:
        return False
    dom = v9.domain(cand['domain'])
    if v9.domain(row.get('url')) == dom or re.search(r'(?<![\w.-])' + re.escape(dom) + r'(?![\w.-])', text, re.I):
        return True
    for match in matches:
        before = text[:match.start()]
        # A leading proper-name modifier is a different compound brand, e.g.
        # "Fender Motion". Sentence starters/reporting words are not modifiers.
        modifier = re.search(r'\b([A-Z][A-Za-z]+)\s+$', before)
        if modifier and modifier.group(1).lower() not in {'the', 'startup', 'company', 'about', 'with', 'and', 'by', 'from'}:
            continue
        return True
    return False


def category_words(category):
    return ('launches', 'introduces', 'unveils', 'announces') if category == 'PRODUCT_LAUNCH' else v9.WORDS.get(category, ('announces',))


def dated_item(cand, page, category, evaluation, max_age):
    published = v9.observed_date(page)
    if not published or not 0 <= (evaluation-published).days <= max_age:
        return None
    text = _clean(page.get('title', '') + ' ' + page.get('text', ''))
    words = category_words(category)
    if not any(re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', text, re.I) for word in words):
        return None
    if not brand_bound(cand, page):
        return None
    return {'url': page['url'], 'date': published.isoformat(), 'text': text[:1400], 'via': 'own_page'}


async def event_evidence(run, cand):
    """D2 must precede this call. Two own pages, one own search on miss, one wire search."""
    intent = run.icp['required_intents'][0]
    category = str(intent.get('category') or '').upper()
    max_age = int(intent.get('max_age_days') or 365)
    dom = v9.domain(cand['domain'])
    words = category_words(category)
    own = []
    visited = {cand.get('_home', {}).get('url', '')}

    def available(name):
        provider = 'deepline'
        return run.remaining() > 35 and run.used[provider] < run.budget[provider]

    async def call(name, args):
        return await run.tool(name, args) if available(name) else {}

    def links(page):
        choices = []
        for item in page.get('links', []):
            url = item.get('url', '')
            if url in visited or v9.domain(url) != dom or not v9.signal_url_ok(url) or not url.startswith('https://'):
                continue
            career = bool(re.search(r'/(?:jobs|job/|careers)', urlsplit(url).path, re.I))
            if HUB.search(url + ' ' + item.get('text', '')) or (v9.enabled('V13_SOURCE_WEIGHT') and career):
                # Prefer a concrete dated/event article over a generic hub.
                label = url + ' ' + item.get('text', '')
                event = any(re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', label, re.I) for word in words)
                dated = bool(re.search(r'\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b', label))
                depth = len(urlsplit(url).path.strip('/').split('/'))
                # Without an event/date clue read the hub, not an arbitrary
                # deep tutorial or opinion article. It can expose the dated URL.
                choices.append((not career if v9.enabled('V13_SOURCE_WEIGHT') else False, not event, not dated, depth, url))
        return [item[-1] for item in sorted(set(choices))]

    queue = links(cand.get('_home', {}))
    for _ in range(2):
        queue = [url for url in queue if url not in visited]
        if not queue:
            break
        url = queue.pop(0)
        visited.add(url)
        page = run.page_cache.get(url)
        if page is None:
            page = await call('fetch_page', {'url': url, 'max_chars': 4000})
            error = str(page.get('error', ''))
            if re.search(r'frame|4194304|response.{0,20}(?:large|size)', error, re.I):
                result = await call('exa_contents', {'urls': [url], 'max_chars': 4000})
                page = next(iter(result.get('results', [])), {})
            if page.get('text') and not page.get('error'):
                run.page_cache[url] = page
        if not page.get('error') and v9.domain(page.get('url')) == dom:
            row = dated_item(cand, page, category, run.eval_date, max_age)
            if row:
                own.append(row)
            queue = links(page) + queue

    def rows(result, own_only=False):
        accepted = []
        for page in result.get('results', []):
            if not v9.signal_url_ok(page.get('url', '')) or not brand_bound(cand, page):
                continue
            if own_only and v9.domain(page.get('url')) != dom:
                continue
            published = _parse_date(page.get('date') or page.get('publishedDate'))
            if published and not 0 <= (run.eval_date-published).days <= max_age:
                continue
            if v9.enabled('V13_SOURCE_WEIGHT') and page.get('text') and len(page['text']) >= 200:
                run.page_cache.setdefault(page['url'], page)
            accepted.append({'url': page['url'], 'date': published.isoformat() if published else '',
                             'text': _clean(page.get('title', '') + ' ' + (page.get('text') or page.get('snippet') or ''))[:1400],
                             'via': 'own_search' if own_only else 'domain_search'})
        return accepted

    common = {'numResults': 8, 'startPublishedDate': (run.eval_date-timedelta(days=max_age)).isoformat(),
              'endPublishedDate': run.eval_date.isoformat(), 'contents': {'text': {'maxCharacters': 1800}}}
    if not own:
        found = await call('exa_search', {**common, 'query': f"{cand['company_name']} {' '.join(words)}", 'includeDomains': [dom]})
        own = rows(found, True)
    found = await call('exa_search', {**common, 'query': f"{cand['company_name']} {dom} {' '.join(words)}"})
    external = rows(found)
    # Reserve room for both classes; no second-primary padding.
    combined = own[:4] + external[:4] + own[4:] + external[4:]
    result, seen = [], set()
    for row in combined:
        if row['url'] not in seen:
            seen.add(row['url'])
            result.append(row)
    return result[:8]
