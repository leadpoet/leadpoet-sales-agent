"""Grounded category judgment with an optional stricter lexical precheck."""
import re
from agent.v20r5_primary import category
from agent.v12_llm import enabled

NEW=re.compile(r'\bnew\s+(?:\w+[ -]){0,5}(?:product|service|capabilit\w*|program|platform|solution|initiative|agent|tool|feature)\b',re.I)
OFFERING=re.compile(r'\b(?:program|platform|service|product|capabilit\w*|solution|initiative|agent|tool|feature)\b',re.I)
LAUNCH=re.compile(r'\b(?:launch(?:es|ed|ing)?|unveil(?:s|ed|ing)?|introduc(?:es?|ed|ing)|general availability)\b',re.I)
NO_EVENT=re.compile(r'\b(?:award|partnership|partnered|distribution|now available through|now available via|expands? access|conference recap)\b',re.I)

def instruction(icp):
    if category(icp)!='PRODUCT_LAUNCH':return ''
    if enabled('V20R11_CATEGORY'):
        return ('Use the company-attributed sentence and immediate context to decide whether the requested event is plausibly established. '
                'Distribution or newly available access to a named offering may establish a product/service launch. '
                'Funding alone, an unrelated company event, or a bare company mention does not. ')
    return ('Apply the ICP definition literally: launched a NEW product, care service, or major capability. '
            'Name the new offering and quote its launch/introduction. Availability through a partner, '
            'distribution deals, partnerships, awards, conference recaps and expansion of an existing '
            'service are not matches unless the sentence or immediate context establishes a named new offering. '
            'Never infer a launch from the article type or the word announces alone. ')

def matches(icp,quote,page=None):
    if enabled('V20R11_CATEGORY'):return True  # R8: category support comes from the grounded model verdict.
    if category(icp)!='PRODUCT_LAUNCH':return True
    quote=str(quote or '')
    if re.search(r'\bexisting\b|\bexpan(?:ds?|sion)\b',quote,re.I) and not NEW.search(quote):return False
    # The new offering must be in the chosen quote, not elsewhere in an article.
    if LAUNCH.search(quote) and OFFERING.search(quote):
        return not NO_EVENT.search(quote) or bool(NEW.search(quote))
    # A recap may describe an actual new initiative. Require its introduction
    # in the directly preceding sentence and a named offering in the quote.
    if not OFFERING.search(quote) or NO_EVENT.search(quote):return False
    text=str((page or {}).get('text',''));m=re.search(r'\s+'.join(map(re.escape,quote.split())),text)
    if not m:return False
    context=re.split(r'\n\s*\n',text[max(0,m.start()-500):m.start()].strip())[-1]
    previous=re.split(r'(?<=[.!?])\s+(?=[A-Z])',context)[-1]
    return bool(NEW.search(previous) and LAUNCH.search(previous))
