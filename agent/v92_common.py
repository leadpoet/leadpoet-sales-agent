"""Shared evidence primitives for the independent v9.2 rules.

No company-specific exceptions and no network access outside approved tools.
"""
from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
from agent.safe_urls import urljoin, urlsplit
import tldextract
from copy import deepcopy

from agent.evidence import _clean, _host, _norm, _parse_date, _date_in_text, _snippet_on_page

_PSL = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)
WIRES = ('prnewswire.com', 'businesswire.com', 'globenewswire.com', 'accessnewswire.com', 'techcrunch.com',
         'venturebeat.com', 'tech.eu', 'sifted.eu', 'businesscloud.co.uk', 'cpapracticeadvisor.com')
BLOCKED = {'salestools.io', 'briefglance.com', 'cbinsights.com', 'crunchbase.com', 'zoominfo.com', 'pitchbook.com',
           'facebook.com', 'x.com', 'twitter.com', 'reddit.com', 'instagram.com', 'youtube.com', 'youtu.be', 'tiktok.com'}
# M1 vocabulary only; none of the v8 verification or budget mechanisms.
WORDS = {
    'HIRING': ('hiring', 'open roles', 'jobs'),
    'FACILITY_OPENING': ('opens new facility', 'opens new office plant', 'new headquarters'),
    'REGULATORY_CLEARANCE': ('receives approval', 'regulatory clearance', 'CE mark'),
    'MARKET_EXPANSION': ('enters new market', 'launches in', 'expands into'),
    'PARTNERSHIP': ('partners with', 'announces partnership', 'collaboration'),
    'ACQUISITION': ('acquires', 'to acquire', 'completes acquisition of'),
    'FUNDING': ('raises', 'closes funding round', 'secures funding'),
    'PRODUCT_LAUNCH': ('launches', 'unveils introduces', 'now available'),
    'LEADERSHIP_CHANGE': ('appoints', 'names chief executive', 'joins as'),
}

def enabled(name):
    return os.environ.get('AGENT_RULE_' + name, '1').lower() not in {'0', 'false', 'off'}

def domain(value):
    host = _host(str(value or ''))
    if not host:return ""
    try:parsed = _PSL(host)
    except (ValueError,UnicodeError) as exc:
        from agent.safe_urls import invalid
        invalid(value,exc);return ""
    return parsed.top_domain_under_public_suffix if parsed.suffix else '.'.join(host.split('.')[-2:])

def signal_url_ok(url):
    d = domain(url)
    if not d or urlsplit(url).scheme not in {'http', 'https'}:
        return False
    if d in BLOCKED or any(x in d for x in ('fundup', 'trysignalbase')):
        return False
    if d == 'linkedin.com':
        return bool(re.match(r'^/(posts/|feed/update/)', urlsplit(url).path))
    return not re.search(r'/(?:login|privacy|terms)(?:/|$)', urlsplit(url).path)

def observed_date(page):
    result = _parse_date(page.get('datePublished') or page.get('date'))
    if result:
        return result
    text = _clean(page.get('text'))
    return next((d for i in range(0, len(text), 1000) if (d := _date_in_text(text[i:i+1500]))), None)

def page_metadata(html, url):
    """D2 metadata plus publication dates needed for page-grounded K1."""
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.result = {'links': [], 'site_name': '', 'canonical_url': '', 'datePublished': ''}
            self.anchor = None
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == 'meta':
                key = (a.get('property') or a.get('name') or a.get('itemprop') or '').lower()
                if key == 'og:site_name':
                    self.result['site_name'] = _clean(a.get('content'))
                if key in {'article:published_time', 'datepublished', 'pubdate'} and _parse_date(a.get('content')):
                    self.result['datePublished'] = _parse_date(a['content']).isoformat()
            if tag == 'link' and 'canonical' in str(a.get('rel') or '').split():
                self.result['canonical_url'] = urljoin(url, a.get('href') or '')
            if tag == 'a' and len(self.result['links']) < 500:
                target=urljoin(url,a.get('href') or '')
                self.anchor = {'url':target,'text':''} if target else None
        def handle_data(self, text):
            if self.anchor is not None:
                self.anchor['text'] = (self.anchor['text'] + ' ' + text)[:240]
        def handle_endtag(self, tag):
            if tag == 'a' and self.anchor is not None:
                self.result['links'].append(self.anchor)
                self.anchor = None
    parser = Parser()
    parser.feed(html)
    return parser.result
