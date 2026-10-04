"""Industry and required-attribute proof from the company's own pages.

The judge decides industry and the required attribute together from a first-party page: a continuous sentence,
early in the page's visible text, that names the company and says the company itself supplies what the ICP's
sub-industry, product/service and attribute describe (an adjacent activity for the same customers does not count).
This module reads the homepage and, when it says little, up to two same-domain overview pages, keeps sentences from the first 6,000
characters of visible text that name the company, asks one inexpensive model call which sentence (if any) states
that supply, and checks the chosen sentence is verbatim on the fetched page.  For business-model attributes /pricing and
/plans are read first and a seller sentence showing a sale is preferred over a mission or open-source sentence.

Tiers: A = one page proves the sub-industry, the product/service and the attribute; B = the sub-industry sentence is
proven and the attribute is proven by another first-party sentence; C = an in-industry self-description without
full attribute proof; X = the company's own pages describe a different or adjacent business.

A classifier pick is kept for tier A/B only when its own words state the criterion (``states``): the attribute
sentence must name at least one of the attribute's capability terms (one alternative of an OR-list is enough) plus a
second term of the attribute, and the industry sentence must do the same for product_service.  Customer quotes,
testimonial cards and navigation runs are never offered as sentences, and a heading run in front of the company's own
"<Company> is/provides/..." clause is cut off (the rest is still one contiguous span of the page).
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any, Mapping, Optional
from urllib.parse import urljoin, urlsplit

from . import gates
from . import llm
from . import scorer_mirror as sm
from .arena_tools import STRATEGY, BudgetExhausted

MODEL = str(STRATEGY.get("fitproof_model") or "google/gemini-2.5-flash")
WINDOW_CHARS = 6000
MAX_PAGES = 3
ENOUGH_HOME_SENTENCES = 3
MAX_SENTENCES = 18
_NAV_RE = re.compile(r"/(?:about|about-us|company|who-we-are|what-we-do|product|products|platform|solutions?|services?|"
                     r"pricing|plans|customers|features|overview|why-[a-z-]+)(?:/|$)", re.I)
_SKIP_RE = re.compile(r"/(?:blog|news|press|careers?|jobs|legal|privacy|terms|cookie|login|signin|sign-in|signup|contact|"
                      r"support|docs|help|events?|webinars?|partners?|investors?)(?:/|$)", re.I)
_COMMERCIAL_RE = re.compile(r"\b(?:pricing|price|plans?|per (?:month|user|seat|year)|subscription|subscribe|free trial|"
                            r"book a demo|request a demo|get a demo|customers? (?:include|like)|trusted by|used by|"
                            r"clients? (?:include|such as)|case stud(?:y|ies)|\$\s?\d)", re.I)
_SELLS_RE = re.compile(r"^\s*(?:sells|offers|provides|licenses|operates|runs)\b", re.I)
# A business-model attribute: the pricing / plans pages are read first and a sale is required of the chosen sentence.
MAX_PAGES_BUSINESS = 4
_BUSINESS_MODEL_RE = re.compile(r"\b(?:subscriptions?|subscription-based|saas|software[\s-]as[\s-]a[\s-]service|"
                                r"platforms?|recurring|licen[cs](?:e|es|ed|ing)|per[\s-](?:seat|user)|usage[\s-]based|"
                                r"transaction[\s-]based|pay[\s-]as[\s-]you[\s-]go|pricing|plans?|fees?|"
                                r"(?:marketplace\s+)?commissions?)\b", re.I)
# Fee lines (a pricing table): evidence that what a seller sentence on the same page describes is sold.
_FEE_RE = re.compile(r"(?:[$£€]\s?\d[\d,.]*\s*(?:/|per\b|a\s+month|monthly|annually|yearly)|"
                     r"/\s?(?:mo|month|yr|year|user|seat)\b|\bper\s+(?:month|year|user|seat|member|host)\b|"
                     r"\b(?:monthly|annual|yearly)\s+(?:plans?|subscriptions?|billing)\b|\bbilled\s+(?:monthly|annually|"
                     r"yearly)\b|\bpay[\s-]as[\s-]you[\s-]go\b|\bfree\s+trial\b|\b(?:pro|team|teams|business|enterprise|"
                     r"starter|basic|premium|growth|standard|free)\s+(?:plans?|tiers?)\b)", re.I)
# A sale in the sentence's own words.
_SALE_RE = re.compile(_FEE_RE.pattern + r"|[$£€]\s?\d|\bsubscriptions?\b|\bsubscription-based\b|\bpricing\b|"
                      r"\bplans?\s+(?:start|from|begin)|\blicen[cs](?:e|es|ed|ing)\b|\busage[\s-]based\b|"
                      r"\btransaction[\s-]based\b|\b(?:fees?|commissions?)\b|\bsaas\b|\bsoftware[\s-]as[\s-]a[\s-]service\b",
                      re.I)
# Mission, values, open-source or community status: no sale on its own.
_NO_SALE_RE = re.compile(r"\b(?:open[\s-]source|(?:built|driven|backed|led|powered|maintained)\s+by\s+(?:the\s+|a\s+|our\s+)?"
                         r"(?:global\s+|open\s+)?community|community[\s-](?:driven|led|built|edition|project)|"
                         r"(?:our|on\s+a)\s+mission(?!-)|mission\s+is|our\s+vision|vision\s+is|our\s+values|we\s+believe|"
                         r"believes?\s+in|passionate|non-?profit)\b", re.I)
_SELLER_VERBS = (r"(?:offers?|provides?|sells?|delivers?|helps?|lets|enables?|powers?|builds?|makes?|licenses?|operates?|"
                 r"runs?|gives?|supplies|supply|develops?|manufactures?|produces?|brings?|automates?|unifies?)")
_PROVIDER_NOUNS = (r"(?:platform|company|provider|service|solution|software|marketplace|tool|app|application|suite|"
                   r"product|vendor|network|firm|business|maker|manufacturer|developer|saas)")
_OFFER_NOUNS = r"(?:platform|products?|software|solutions?|services?|tools?|apps?|plans?|suite)"
_PRICING_PATH_RE = re.compile(r"/(?:pricing|plans)(?:/|$)", re.I)
_OFFER_PATH_RE = re.compile(r"/(?:products?|platform)(?:/|$)", re.I)
_STOP = frozenset({"that", "with", "used", "uses", "from", "into", "their", "they", "them", "this", "which", "other",
                   "such", "more", "sells", "offers", "provides", "company", "companies", "organizations", "services",
                   "service", "platform", "business", "businesses", "helps", "using", "based"})
TIERS = ("A", "B", "C", "X")


def _stems(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z]{4,}", str(text or "").casefold()) if w not in _STOP}


_TERM_STOP = frozenset({"that", "with", "used", "uses", "using", "from", "into", "their", "they", "them", "this", "which",
                        "other", "such", "more", "also", "your", "ours", "have", "been", "will", "than", "then", "each",
                        "every", "over", "only", "what", "when", "where", "while", "about", "across", "like",
                        "including", "company", "companies", "organization", "organizations", "these", "those"})
# Terms too generic to show a capability on their own (they still count as the second term).
_WEAK_TERMS = frozenset({"manag", "run", "runs", "proce", "help", "make", "build", "deliv", "provi", "suppo", "enabl",
                         "offer", "sell", "servi", "produ", "platf", "softw", "solut", "tool", "busin", "custo", "clien",
                         "team", "user"})
_SUFFIXES = ("ages", "age", "ings", "ing", "ers", "er", "ies", "es", "s")
_CRITERION_VERB_RE = re.compile(r"^\s*(?:sells|offers|provides|licenses|operates|runs|builds|develops|makes|manufactures|"
                                r"produces|supplies|delivers|has|is)\s+", re.I)
_CRITERION_SPLIT_RE = re.compile(r"\s+(?:used\s+by|used\s+to|used\s+for|that|which|with|to\s+help|to)\s+", re.I)


def term(word: str) -> str:
    """A crude stem: up to two common suffixes off (never below four letters), then the first five letters."""

    for _ in range(2):
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        else:
            break
    return word[:5]


def terms(text: Any) -> set[str]:
    return {term(w) for w in re.findall(r"[a-z][a-z0-9]{3,}", str(text or "").casefold()) if w not in _TERM_STOP}


def criterion_parts(criterion: Any) -> dict[str, set[str]]:
    """'Sells <product> used by <customers> to <capability>' (or '<product> that|with <capability> for <customers>')
    as three term sets."""

    text = _CRITERION_VERB_RE.sub("", " ".join(str(criterion or "").split()))
    text = re.sub(r"^(?:an?|the)\s+", "", text, flags=re.I)
    split = _CRITERION_SPLIT_RE.search(text)
    product, rest = (text[:split.start()], text[split.start():]) if split else (text, "")
    customers, capability = "", ""
    used_by = re.match(r"\s+used\s+by\s+(.+?)\s+to\s+(.*)$", rest, re.I)
    if used_by:
        customers, capability = used_by.group(1), used_by.group(2)
    elif rest:
        capability = re.sub(r"^\s+(?:used\s+to|used\s+for|that|which|with|to\s+help|to)\s+", "", rest, flags=re.I)
        tail = re.search(r"\s+for\s+([^,]+)$", capability)
        if tail:
            customers, capability = tail.group(1), capability[:tail.start()]
    return {"product": terms(product), "customers": terms(customers), "capability": terms(capability)}


def states(sentence: Any, criterion: Any, name: Any = "") -> tuple[bool, str]:
    """(True, '') when the sentence's own words state the criterion: one capability term (from the OR-list; the
    product when the criterion names no capability) that is not a generic word, plus at least one more term of the
    criterion.  Words of the company's own name do not count ('Acme Cloud' is not the capability 'cloud').  Else
    (False, what is missing)."""

    if not str(criterion or "").strip():
        return True, ""
    parts = criterion_parts(criterion)
    words = terms(sentence) - terms(name)
    capability = parts["capability"] or parts["product"]
    strong = words & ((capability - _WEAK_TERMS) or capability)
    hits = words & (parts["product"] | parts["customers"] | parts["capability"])
    if not strong:
        return False, "no capability term of the criterion"
    if len(hits) < 2:
        return False, "only one term of the criterion"
    return True, ""


def business_model(icp: Mapping[str, Any]) -> bool:
    """The required attribute or product/service names a business model (subscription, SaaS, licence, pricing ...)."""

    return bool(_BUSINESS_MODEL_RE.search(" ".join(str(icp.get(k) or "") for k in ("required_attribute",
                                                                                     "product_service"))))


def seller_clause(sentence: Any, name: Any) -> bool:
    """The company (name, 'we', 'our platform') is the subject of a seller verb, or '<Company> is a ... platform'."""

    from .scout import _name_source
    text = " ".join(str(sentence or "").split())
    source = _name_source(str(name or ""))
    subject = r"\bwe\b|\bour\s+(?:[A-Za-z-]+\s+){0,2}?" + _OFFER_NOUNS + r"\b"
    if source:
        subject += (r"|(?:" + source + r")(?:['’]s\s+(?:[A-Za-z-]+\s+){0,2}?" + _OFFER_NOUNS + r"\b)?"
                    r"(?:\s+(?:Inc\.?|LLC|Ltd\.?|Corp\.?))?,?")
    if re.search(r"(?:" + subject + r")\s+(?:also\s+|now\s+|today\s+)?" + _SELLER_VERBS + r"\b", text, re.I):
        return True
    return bool(source and re.search(r"(?:" + source + r"),?\s+is\s+(?:a|an|the)\b(?:\s+[^\s.]+){0,8}?\s+" +
                                     _PROVIDER_NOUNS + r"s?\b", text, re.I))


def coverage(sentence: Any, criterion: Any, name: Any = "") -> float:
    """Share of the criterion's clauses the sentence's own words touch (the company name excluded)."""

    groups = [part for part in criterion_parts(criterion).values() if part]
    if not groups:
        return 0.0
    words = terms(sentence) - terms(name)
    return sum(1 for part in groups if words & part) / len(groups)


def _no_sale(sentence: str) -> bool:
    return bool(_NO_SALE_RE.search(sentence)) and not _SALE_RE.search(sentence)


def _sale_evidenced(sentence: str, name: str, page_fees: bool) -> bool:
    """A sale in the sentence's own words, or a seller sentence on a page listing fees (a pricing page)."""

    return bool(_SALE_RE.search(sentence)) or (page_fees and seller_clause(sentence, name))


def offer_score(sentence: Any, name: Any, criterion: Any = "", business: bool = False, page_fees: bool = False) -> float:
    """Clause coverage, the criterion stated, the company as seller and a sale; no-sale sentences penalized."""

    text = " ".join(str(sentence or "").split())
    score = 2.0 * coverage(text, criterion, name)
    if str(criterion or "").strip() and states(text, criterion, name)[0]:
        score += 1.0
    seller = seller_clause(text, name)
    if seller:
        score += 1.5
    if business and _SALE_RE.search(text):
        score += 1.0
    elif business and page_fees and seller:
        score += 0.5
    if _no_sale(text):
        score -= 2.0
    return score


_CURLY_RE = re.compile(r"[\u201c\u201d\u00ab\u00bb]")
_CHROME_WORDS_RE = re.compile(r"\b(?:read more|learn more|see more|view all|watch (?:the )?video|book a demo|request a demo|"
                              r"get a demo|get started|talk to (?:an expert|sales)|skip to (?:main )?content|"
                              r"contact sales|start (?:a )?free trial|try (?:it )?(?:for )?free)\b", re.I)
_PERSON_TITLE_RE = re.compile(r"\b[A-Z][A-Za-z'\u2019-]+(?:[ -][A-Z][A-Za-z'\u2019-]+){1,2},?\s+(?:Co-?[Ff]ounder|Founder|"
                              r"CEO|CTO|CFO|COO|CISO|CIO|VP|SVP|EVP|Vice President|Director|Head of|Manager|Engineer|"
                              r"Architect|Lead|Chief|President|Principal|Partner)\b")
_FIRST_PERSON_RE = re.compile(r"\b(?:we|we'?ve|we'?re|our|us|I|me|my)\b", re.I)
# First-person phrases a company uses about itself while naming itself in the third person.
_OWN_VOICE_RE = re.compile(r"\bour\s+(?:customers?|clients?|partners?|platform|products?|solutions?|mission|team|users|"
                           r"members|community|technology|services?)\b", re.I)


def _customer_voice(sentence: str, name: str) -> bool:
    """A customer speaking about the company: first person next to the company named in the third person, unless
    the first person is the company's own ('At Acme, we ...', 'our customers')."""

    from .scout import _name_source
    source = _name_source(name)
    if not source or not _FIRST_PERSON_RE.search(sentence):
        return False
    if re.search(r"\bAt\s+(?:" + source + r"),?\s+we", sentence, re.I):
        return False
    rest = _OWN_VOICE_RE.sub(" ", sentence)
    return bool(_FIRST_PERSON_RE.search(rest))


def junk_sentence(sentence: str, name: str) -> str:
    """'' for a usable self-description, else why not: a customer quote or testimonial card, a navigation or call to
    action run, or a list of title-case labels."""

    text = " ".join(str(sentence or "").split())
    if "..." in text or "\u2026" in text:
        return "elided"
    if _CURLY_RE.search(text) or text[:1] in "\"'":
        return "quotation"
    if _CHROME_WORDS_RE.search(text):
        return "navigation"
    if _PERSON_TITLE_RE.search(text):
        return "testimonial"
    if _customer_voice(text, name):
        return "customer voice"
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\u2019-]*", text)]
    if len(words) >= 10 and sum(1 for w in words if w[:1].isupper()) / len(words) > 0.55:
        return "title-case run"
    run = longest = 0
    for word in words:
        run = run + 1 if word[:1].isupper() else 0
        longest = max(longest, run)
    if longest >= 6 or re.search(r"\s:\s", text):
        return "label run"
    return ""


def clean_sentence(sentence: str, name: str) -> str:
    """Cut a heading or navigation run in front of the company's own clause ("About - Acme Build for teams Acme is a
    platform ..." -> "Acme is a platform ..."); the result is a suffix, so it is still one contiguous page span."""

    from .scout import _name_source
    text = " ".join(str(sentence or "").split())
    source = _name_source(name)
    if not source:
        return text
    clause = re.search(r"(?:\bAt\s+(?:" + source + r"),\s+we\b|(?:" + source + r")(?:\s+(?:Inc\.?|LLC|Ltd\.?|Corp\.?))?,?\s+"
                       r"(?:is|are|was|provides|offers|helps|lets|enables|powers|delivers|builds|makes|gives|sells|runs|"
                       r"operates|develops|designs|manufactures|produces|supplies|serves|specializes|unifies|automates|"
                       r"manages)\b)", text, re.I)
    if clause and clause.start() > 0 and len(text[:clause.start()].split()) >= 3 and \
            len(text[clause.start():].split()) >= 8:
        return text[clause.start():]
    return text


def focus_terms(icp: Mapping[str, Any]) -> set[str]:
    return _stems(" ".join(str(icp.get(k) or "") for k in ("sub_industry", "product_service", "required_attribute",
                                                          "industry")))


def _registrable(url: str) -> str:
    return gates.registrable_domain(url) or sm.registrable_host(url)


_BOUNDARY_TAGS = frozenset({"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
                            "header", "footer", "main", "aside", "blockquote", "figcaption", "figure", "td", "th", "tr",
                            "table", "dd", "dt", "dl", "br", "hr", "nav", "form", "button", "label", "option", "select",
                            "title", "summary", "details"})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head", "button", "select", "option", "form"})


class _Blocks(HTMLParser):
    """Text segments between block-level boundaries (a heading, a nav label and a paragraph become separate
    segments instead of one run of the page's flattened visible text)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buffer: list[str] = []
        self._skip = 0

    def _flush(self) -> None:
        text = " ".join("".join(self._buffer).split())
        if text:
            self.blocks.append(text)
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in _BOUNDARY_TAGS:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        if tag in _BOUNDARY_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in _BOUNDARY_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buffer.append(data)


def page_blocks(html: str, limit: int = 400) -> list[str]:
    parser = _Blocks()
    try:
        parser.feed(str(html or "")[:2_000_000])
        parser.close()
        parser._flush()
    except Exception:  # noqa: BLE001 - malformed markup keeps what was read
        pass
    return parser.blocks[:limit]


def _remember_blocks(tools: Any, url: str, html: str) -> None:
    try:
        tools.__dict__.setdefault("_fit_blocks", {})[url] = page_blocks(html)
    except Exception:  # noqa: BLE001
        pass


def blocks_for(tools: Any, url: str) -> list[str]:
    try:
        return list((tools.__dict__.get("_fit_blocks") or {}).get(url) or [])
    except Exception:  # noqa: BLE001
        return []


def _html_of(tools: Any, url: str, first_party: str = "") -> Optional[str]:
    """Raw HTML ('' when unavailable); None when the read ended off ``first_party``."""

    from .identity import probe
    got = probe(tools, url)
    status = got.get("status") or 500
    final = str(got.get("final_url") or "")
    if first_party and final and _registrable(final) != first_party:
        return None
    return got.get("html") or "" if status < 400 else ""


def _page(tools: Any, url: str, first_party: str = "") -> tuple[str, str, list[str]]:
    """(final url, visible text, links) via the free raw-HTML probe, else the free scrape; off-site reads give no text."""

    html = _html_of(tools, url, first_party)
    if html is None:
        return url, "", []
    if html:
        text = gates.visible_text(html)
        links = gates.visible_links(html) or []
        if text and len(text) >= 200:
            _remember_blocks(tools, url, html)
            return url, " ".join(text.split()), [urljoin(url, link) for link in links]
    page = tools.pages.get(url)
    if page is None or not getattr(page, "ok", False):
        page = tools._contextdev_page(url, 12000)
        if getattr(page, "ok", False):
            tools.pages[url] = page
    if first_party and _registrable(str(getattr(page, "final_url", "") or url)) != first_party:
        return url, "", []
    if getattr(page, "ok", False):
        return url, " ".join(str(page.text or "").split()), list(getattr(page, "links", None) or [])
    return url, "", []


def overview_links(links: list[str], domain: str, limit: int = MAX_PAGES - 1) -> list[str]:
    out: list[str] = []
    for link in links:
        try:
            parts = urlsplit(str(link))
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or _registrable(link) != domain:
            continue
        path = parts.path or "/"
        if path in ("", "/") or _SKIP_RE.search(path) or not _NAV_RE.search(path) or path.count("/") > 3:
            continue
        clean = f"https://{parts.hostname}{path}"
        if clean not in out:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


def _block_sentences(window: str, blocks: list[str]) -> list[tuple[int, str]]:
    """Sentences of the page's block segments that occur verbatim in the flattened window, with their position."""

    from .scout import split_sentences

    low = window.casefold()
    out: list[tuple[int, str]] = []
    for block in blocks or []:
        for sentence in split_sentences(block):
            sentence = " ".join(sentence.split())
            where = low.find(sentence.casefold()) if len(sentence) >= 20 else -1
            if where >= 0:
                out.append((where, sentence))
    return out


def candidate_sentences(text: str, name: str, focus: set[str], limit: int = 8,
                        blocks: Optional[list[str]] = None, criterion: str = "", business: bool = False,
                        page_fees: bool = False) -> list[str]:
    """Up to ``limit`` sentences naming the company from the page window, ranked by term overlap (+ offer_score)."""

    from .scout import _CHROME_RE, _URLISH_RE, names_company, split_sentences

    window = " ".join(str(text or "")[:WINDOW_CHARS].split())
    from_blocks = _block_sentences(window, blocks or [])
    low = window.casefold()
    flat = [(low.find(" ".join(s.split()).casefold()), s) for s in split_sentences(window)]
    taken = [s.casefold() for _p, s in from_blocks]
    sources = from_blocks + [(p, s) for p, s in flat if not any(t in s.casefold() for t in taken)]
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for position, sentence in sources:
        sentence = clean_sentence(sentence, name)
        if sentence.casefold() in seen:
            continue
        seen.add(sentence.casefold())
        words = sentence.split()
        if not 8 <= len(words) <= 70 or _URLISH_RE.search(sentence) or _CHROME_RE.search(sentence):
            continue
        if not names_company(name, sentence) or junk_sentence(sentence, name):
            continue
        overlap = len(_stems(sentence) & focus)
        if overlap:
            rank = overlap + (offer_score(sentence, name, criterion, business, page_fees) if criterion else 0.0)
            scored.append((-rank, position, " ".join(words)))
    scored.sort()
    return [s for _o, _p, s in scored[:limit]]


def classify_prompt(icp: Mapping[str, Any], name: str, items: list[dict[str, Any]]) -> str:
    brief = {k: icp.get(k) for k in ("industry", "sub_industry", "product_service", "required_attribute")}
    return (
        "Each numbered sentence below comes from the website of the company named %s. Decide from these sentences "
        "alone, without outside knowledge:\n"
        "- industry_id: the sentence that says %s ITSELF supplies or operates what sub_industry and product_service "
        "describe (a customer of such products, an internal team, a partner, or an adjacent activity for the same "
        "customers does not count), or null;\n"
        "- attribute_id: the sentence that shows %s meets required_attribute (every AND-part; for an OR-list one part "
        "is enough; a 'sells' attribute needs a sale, a subscription, a price, plans or named paying customers), or "
        "null; its own words must name what is offered and what it does; a sentence about selling to customers, "
        "prices alone, or what customers achieve does not count; prefer a sentence in which the company (or 'we') "
        "offers, sells or provides it -- on a pricing or plans page the listed prices and plans show the paid "
        "offering -- over a mission, open-source or community sentence;\n"
        "- tier: A when one sentence or two sentences from the SAME page prove both, B when both are proven from "
        "different pages, C when the company is clearly in the sub-industry but the attribute is not shown, X when "
        "the sentences show a different or adjacent business;\n"
        "- reason: at most 20 words.\n"
        "Return {\"industry_id\": <int|null>, \"attribute_id\": <int|null>, \"tier\": \"A|B|C|X\", \"reason\": \"...\"}."
        "\n\nCRITERIA: %s\n\nSENTENCES: %s"
        % (json.dumps(name), name, name, json.dumps(brief, default=str)[:2500], json.dumps(items)[:12000]))


def industry_criterion(icp: Mapping[str, Any]) -> str:
    return str(icp.get("product_service") or icp.get("sub_industry") or "")


GUESSED_PATHS = ("/about", "/platform", "/product", "/pricing", "/plans")
BUSINESS_PATHS = ("/pricing", "/plans", "/product", "/platform", "/about")


def _path_rank(url: str) -> int:
    """0 pricing / plans, 1 product / platform, 2 any other overview page."""

    try:
        path = urlsplit(str(url)).path or "/"
    except ValueError:
        return 3
    return 0 if _PRICING_PATH_RE.search(path) else 1 if _OFFER_PATH_RE.search(path) else 2


def more_pages(links: list[str], domain: str, home_url: str, business: bool = False) -> list[tuple[str, bool]]:
    """More first-party pages: homepage overview links, else guessed paths (egress only); business: pricing first."""

    try:
        host = urlsplit(str(home_url or "")).hostname or domain
    except ValueError:
        host = domain
    if business:
        linked = sorted(overview_links(links, domain, limit=12), key=_path_rank)[:MAX_PAGES_BUSINESS - 1]
        tries = [(link, False) for link in linked]
        for path in BUSINESS_PATHS:
            if all(urlsplit(link).path.rstrip("/") != path for link, _g in tries):
                tries.append((f"https://{host}{path}", True))
        return sorted(tries, key=lambda item: (_path_rank(item[0]), item[1]))[:MAX_PAGES_BUSINESS + 2]
    found = [(link, False) for link in overview_links(links, domain)]
    for path in GUESSED_PATHS:
        if len(found) >= MAX_PAGES - 1:
            break
        guess = f"https://{host}{path}"
        if all(urlsplit(link).path.rstrip("/") != path for link, _g in found):
            found.append((guess, True))
    return found[:MAX_PAGES - 1]


def _egress_page(tools: Any, url: str) -> tuple[str, str, list[str]]:
    """(final url, visible text, links) through web egress only; ('', '', []) when egress does not serve the page."""

    fetch = getattr(tools, "egress_get", None)
    if not callable(fetch):
        return "", "", []
    try:
        got = fetch(url)
    except Exception:  # noqa: BLE001
        got = None
    if not isinstance(got, dict) or got.get("status") != 200 or "html" not in str(got.get("content_type") or ""):
        return "", "", []
    html = str(got.get("body") or "")
    final = str(got.get("final_url") or url)
    text = gates.visible_text(html) or ""
    wall = gates.antibot(html)
    if _registrable(final) != _registrable(url) or len(text.strip()) < 200 or wall is None or wall:
        return "", "", []
    _remember_blocks(tools, final, html)
    return final, " ".join(text.split()), [urljoin(final, link) for link in gates.visible_links(html) or []]


def _stated_tier(out: dict[str, Any], industry: Mapping[str, Any], attribute: Mapping[str, Any], attribute_text: str,
                 industry_text: str, tier: str, name: str = "") -> str:
    """Keep A/B only when the industry sentence states product_service and the attribute sentence states the
    required attribute in their own words; one sentence may serve both.  Else C, with the reason recorded."""

    ind_quote, att_quote = out["industry"]["quote"], out["attribute"]["quote"]
    ind_ok, ind_why = states(ind_quote, industry_text, name)
    att_ok, att_why = states(att_quote, attribute_text, name)
    if not att_ok and ind_ok and states(ind_quote, attribute_text, name)[0]:
        out["attribute"] = {"url": out["industry"]["url"], "quote": ind_quote, "page": industry["page"],
                            "verbatim": out["industry"].get("verbatim") or ""}
        att_ok, att_why = True, ""
    if not ind_ok and att_ok and states(att_quote, industry_text, name)[0]:
        out["industry"] = {"url": out["attribute"]["url"], "quote": out["attribute"]["quote"],
                           "verbatim": out["attribute"].get("verbatim") or ""}
        ind_ok, ind_why = True, ""
    out["checks"] = {"industry": ind_why or "stated", "attribute": att_why or "stated"}
    if ind_ok and att_ok:
        return tier
    if not att_ok:
        out["attribute"] = None
    out["reason"] = ("attribute sentence: %s" % att_why) if not att_ok else ("industry sentence: %s" % ind_why)
    return "C"


def _in_window(sentence: str, text: str) -> bool:
    return " ".join(sentence.split()).casefold() in " ".join(str(text or "")[:WINDOW_CHARS + 400].split()).casefold()


def _same_span(a: Any, b: Any) -> bool:
    return bool(str(a or "").strip()) and " ".join(str(a).split()).casefold() == " ".join(str(b or "").split()).casefold()


def _better_attribute(items: list[dict[str, Any]], picked: Optional[Mapping[str, Any]], attribute_text: str, name: str,
                      business: bool, fees: list[bool]) -> Optional[dict[str, Any]]:
    """A better attribute sentence (company as seller, a sale for business models) when the pick is weak, else None."""

    if not str(attribute_text or "").strip():
        return None

    def sale(item: Mapping[str, Any]) -> bool:
        return _sale_evidenced(item["text"], name, fees[item["page"]])

    pool = [item for item in items if states(item["text"], attribute_text, name)[0] and
            seller_clause(item["text"], name) and not _no_sale(item["text"]) and (not business or sale(item))]
    if not pool:
        return None
    best = max(pool, key=lambda item: (offer_score(item["text"], name, attribute_text, business, fees[item["page"]]),
                                       -item["id"]))
    if picked is None or picked["id"] == best["id"]:
        return None
    weak = not states(picked["text"], attribute_text, name)[0] or _no_sale(picked["text"]) or \
        (business and not sale(picked))
    return best if weak else None


def prove(tools: Any, icp: Mapping[str, Any], name: str, website: str) -> dict[str, Any]:
    """{tier, industry: {url, quote}, attribute: {url, quote}, commercial, pages, reason}; tier '' when nothing read."""

    out: dict[str, Any] = {"tier": "", "industry": None, "attribute": None, "pages": [], "reason": ""}
    domain = _registrable(website)
    if not domain:
        out["reason"] = "no domain"
        return out
    focus = focus_terms(icp)
    attribute_text = str(icp.get("required_attribute") or "")
    industry_text = industry_criterion(icp)
    business = business_model(icp)
    page_cap = MAX_PAGES_BUSINESS if business else MAX_PAGES
    pages: list[tuple[str, str]] = []
    try:
        url, text, links = _page(tools, website)
        if text:
            pages.append((url, text))
        home_fees = bool(_FEE_RE.search(text or ""))
        home = candidate_sentences(text, name, focus, blocks=blocks_for(tools, url), criterion=attribute_text,
                                   business=business, page_fees=home_fees)
        both = [sentence for sentence in home
                if states(sentence, attribute_text, name)[0] and states(sentence, industry_text, name)[0]]
        # A business-model attribute also needs a sale on the homepage, else the pricing / plans pages are read.
        stated = bool(both) and (not business or any(_sale_evidenced(sentence, name, home_fees) and
                                                     not _no_sale(sentence) for sentence in both))
        if len(home) < ENOUGH_HOME_SENTENCES or not stated:
            for link, guessed in more_pages(links, domain, url or website, business=business):
                if len(pages) >= page_cap:
                    break
                sub_url, sub_text, _ = _egress_page(tools, link) if guessed else _page(tools, link, first_party=domain)
                # Only the company's own registrable domain; a redirect to a page already read is not a new page.
                if sub_text and _registrable(sub_url) == domain and all(sub_url != u for u, _t in pages):
                    pages.append((sub_url, sub_text))
    except BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable site leaves the tier empty
        out["reason"] = f"fetch {type(exc).__name__}"
    out["pages"] = [u for u, _t in pages]
    items: list[dict[str, Any]] = []
    fees = [bool(_FEE_RE.search(text or "")) for _u, text in pages]
    per_page = 8 if len(pages) <= MAX_PAGES else 6
    for page_no, (url, text) in enumerate(pages):
        for sentence in candidate_sentences(text, name, focus, limit=per_page, blocks=blocks_for(tools, url),
                                            criterion=attribute_text, business=business, page_fees=fees[page_no]):
            if len(items) >= MAX_SENTENCES:
                break
            items.append({"id": len(items), "page": page_no, "path": urlsplit(url).path or "/", "text": sentence})
    if not items:
        out["tier"] = "C" if pages else ""
        out["reason"] = out["reason"] or ("no sentence names the company" if pages else "no page read")
        out["weak"] = True
        return out
    parsed = llm.chat_json(classify_prompt(icp, name, items), model=MODEL, max_tokens=300, purpose="fitproof")
    if not isinstance(parsed, dict):
        out.update(tier="C", reason="classifier unavailable", weak=True)
        return out
    tier = str(parsed.get("tier") or "").strip().upper()[:1]
    tier = tier if tier in TIERS else "C"

    def pick(key: str) -> Optional[dict[str, Any]]:
        try:
            index = int(parsed.get(key))
        except (TypeError, ValueError):
            return None
        return items[index] if 0 <= index < len(items) else None

    industry, attribute = pick("industry_id"), pick("attribute_id")
    if tier != "X":
        better = _better_attribute(items, attribute, attribute_text, name, business, fees)
        if better is not None:
            attribute = better
            out["preferred"] = "attribute: the company as seller" + (" with a sale" if business else "")
    # verbatim: the span found in the fetched text of that first-party page (attribute_claim passes only its quote).
    if industry is not None:
        url, text = pages[industry["page"]]
        if _registrable(url) == domain and _in_window(industry["text"], text):
            out["industry"] = {"url": url, "quote": industry["text"], "verbatim": industry["text"]}
    if attribute is not None:
        url, text = pages[attribute["page"]]
        if _registrable(url) == domain and _in_window(attribute["text"], text):
            out["attribute"] = {"url": url, "quote": attribute["text"], "page": attribute["page"],
                                "verbatim": attribute["text"]}
    if tier in ("A", "B") and out["industry"] is not None and out["attribute"] is not None:
        tier = _stated_tier(out, industry, attribute, attribute_text, industry_text, tier, name)
    if tier in ("A", "B") and (out["industry"] is None or out["attribute"] is None):
        tier = "C"
    if tier in ("A", "B") and _SELLS_RE.search(str(icp.get("required_attribute") or "")):
        page_text = pages[out["attribute"]["page"]][1]
        if not _COMMERCIAL_RE.search(page_text):
            tier = "C"
            out["reason"] = "no commercial wording on the attribute page"
    if tier == "A" and out["industry"]["url"] != out["attribute"]["url"]:
        tier = "B"
    if tier == "C" and out["industry"] is None:
        out["weak"] = True
    out["tier"] = tier
    out["reason"] = out["reason"] or str(parsed.get("reason") or "")[:160]
    return out


def attribute_claim(icp: Mapping[str, Any], name: str, website: str, proof: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The required_attribute object (five fields) or None; passed only for A/B with the verbatim span as quote."""

    text = str(icp.get("required_attribute") or "").strip()
    if not text:
        return None
    chosen = proof.get("attribute") or proof.get("industry")
    attribute = proof.get("attribute") or {}
    passed = proof.get("tier") in ("A", "B") and bool(attribute) and \
        _same_span(attribute.get("verbatim"), attribute.get("quote")) and states(attribute.get("quote"), text, name)[0]
    url = str((chosen or {}).get("url") or website)
    quote = str((chosen or {}).get("quote") or "")
    if not quote:
        return None
    host = sm.registrable_host(url) or url
    explanation = (f"The quoted sentence on {host} states what {name} itself offers." if passed else
                   f"The quoted sentence on {host} describes {name}'s offering; it does not show every part of the "
                   f"attribute.")
    return {"text": text[:2000], "passed": bool(passed and quote), "evidence_url": url, "evidence_quote": quote[:2000],
            "explanation": explanation[:2000]}


__all__ = ["prove", "attribute_claim", "candidate_sentences", "overview_links", "focus_terms", "TIERS", "states",
           "criterion_parts", "junk_sentence", "clean_sentence", "more_pages", "industry_criterion", "business_model",
           "seller_clause", "coverage", "offer_score"]
