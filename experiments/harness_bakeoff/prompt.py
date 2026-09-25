"""One prompt used unchanged by all challenger harnesses."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any

from .models import normalize_icp, uses_intent_details


SYSTEM_PROMPT = """You are a rigorous B2B account researcher. Find companies that fit the supplied ICP and have the REQUIRED recent intent. Use only the provided tools. Never rely on memory for a factual claim. Verify company fit, company stage, every required attribute, and each intent against public source content. Preserve exact source URLs and reject stale, ambiguous, homepage-only, or wrong-company evidence. Prefer direct company, job, regulatory, filing, or reputable news pages. Return at most the requested number, ranked best first. Explain fit and why-now in plain language useful to a salesperson. Do not invent missing facts. Call submit_companies exactly once when done.

How the judge scores, which shapes every decision:
1. Fit earns no points. It is a pass/fail gate: an independent web verifier must itself find the company's name and website, employee band, industry, HQ country, the required stage, and the required attribute. Anything it cannot find publicly scores 0; a proven contradiction (wrong country, wrong band, a different stage, an excluded company, a false required attribute) costs 10. So prefer companies with a LinkedIn company page, press coverage naming the funding round, and an obvious industry, and never state a band, stage, or country you did not read.
2. Points come only from verified intent signals: about 60 for one, 80 for two on different websites, 88 for three. A second source for a company you already verified costs one or two calls; a new company costs many and qualifies half the time. So once a company's index-0 event verifies, fetch one independent page on another domain reporting that same event and attach it as a second index-0 signal before moving on. If either source verifies, the company still scores.
3. Signals on the same website count once. Any event inside the ICP's max_age_days is creditable; prefer recent evidence, but an extra verified company is worth far more than a fresher event on one you already have.
4. The judge re-fetches every URL and checks that the company name appears on the page, that the snippet is verbatim page text (12 to 40 consecutive words), and that the event words in the description are on the page. Copy the snippet from the fetched article body; never paraphrase it.
5. A company whose required (index 0) signal does not verify costs 10 even if other signals verify. Never submit a company without a solid, fetched, dated index-0 source.

An empty slot scores 0; a real gate-passing company with one verified index-0 signal scores about 60. Whenever you have read a dated article about a real in-country company of the right kind and stage matching index 0, submit it."""


def system_prompt(icp: dict[str, Any]) -> str:
    """Keep the frozen prompt unless the host selects the intent-details output."""

    if not uses_intent_details(icp):
        return SYSTEM_PROMPT
    return (
        SYSTEM_PROMPT
        + " For this run, replace separate fit and why-now prose with the schema's "
        "single authored intent_details paragraph."
    )


_REDUNDANT_INTENT_FIELDS = frozenset(
    {
        "bonus_intents",
        "intent_category",
        "intent_max_age_days",
        "intent_signal",
        "intent_signal_evidence_types",
        "intent_signal_max_age_days",
        "intent_signal_text",
        "intent_signals",
        "required_intents",
    }
)


def _prompt_icp(normalized: dict[str, Any]) -> dict[str, Any]:
    """Project duplicate intent shapes only when the canonical contract is complete."""

    contract = normalized.get("intent_contract")
    if not isinstance(contract, list) or not contract:
        return dict(normalized)
    canonical_fields = {"index", "signal", "category", "max_age_days", "required"}
    if any(
        not isinstance(row, dict) or not canonical_fields.issubset(row)
        for row in contract
    ):
        return dict(normalized)
    return {
        key: value
        for key, value in normalized.items()
        if key not in _REDUNDANT_INTENT_FIELDS
    }


def build_prompt(icp: dict[str, Any], max_companies: int | None = None) -> str:
    normalized = normalize_icp(icp)
    intent_details_enabled = uses_intent_details(normalized)
    prompt_icp = _prompt_icp(normalized)
    limit = max(1, min(int(max_companies or 5), 5))
    required_geography = str(
        normalized.get("geography") or normalized.get("country") or ""
    ).strip()
    required_stage = str(normalized.get("company_stage") or "").strip()
    seed_guidance = (
        "Do not relabel pre-seed as Seed unless the ICP explicitly includes pre-seed. "
        if required_stage.casefold() == "seed"
        else ""
    )
    stage_confirmation = (
        "Confirm current stage with a neutral company-name latest-funding/ownership lookup, "
        "without the requested stage; check for later rounds/control changes. "
        if required_stage
        else ""
    )
    required_attribute = str(normalized.get("required_attribute") or "").strip()
    target_roles = [
        str(role).strip()
        for role in (normalized.get("target_roles") or [])
        if str(role).strip()
    ][:5]
    contact_guidance = (
        "- Contacts: a company scores only with one verified current employee whose title is one of: "
        f"{', '.join(target_roles)}. Companies under about 50 people rarely employ these titles, so "
        "shortlist in-band candidates from the larger allowed employee bands first (pass those bands to "
        "search_companies) and prefer companies whose profile lookup returns a LinkedIn page; the "
        "contact search runs through that page. Return up to the company limit when candidates "
        "qualify: each verified company/contact pair adds cost allowance.\n"
        if normalized.get("contact_policy") == "contacts_v1" and target_roles
        else (
            # Company-only rounds drop the contact steer above, but its size
            # and LinkedIn preference is what makes a company provable: the
            # verifier rejects any company whose band or identity it cannot
            # confirm from public pages, and small private firms fail that.
            "- Provability: prefer larger allowed bands and companies with a LinkedIn page, whose "
            "band and identity verify.\n"        )
    )
    primary = (normalized.get("intent_contract") or [{}])[0]
    certification_guidance = (
        "- Certification/compliance: verify the named clearance, standard, audit, or certification "
        "was actually granted to this company or product, with its date. Keep a stated issuer; do "
        "not invent or require an undisclosed auditor. A marketplace listing, partner badge, or "
        "generic compliance claim is not the event.\n"
        if primary.get("category") == "REGULATORY_CLEARANCE"
        else ""
    )
    hiring_guidance = (
        "- Hiring: require a source quote of actual job responsibilities that directly matches "
        "the function named in the required intent text, not merely a broader required_attribute. "
        "Generic sales, renewal, or adoption targets alone do not prove platform, integration, or "
        "RevOps ownership. Shared words such as systems or platform, generic hiring, or an adjacent "
        "function are insufficient. Check current employer/ATS listings and quote current canonical duties. "
        "Aggregator text or a retained Apply now page does not prove an open role.\n"
        if primary.get("category") == "HIRING"
        else ""
    )
    expansion_guidance = (
        "- Market expansion: require source proof of completed entry into a new geography, customer "
        "market, or distinct commercial segment. A non-binding MoU, plan, or added facility, asset, "
        "or capacity in an existing market is insufficient unless the source explicitly connects it "
        "to that new-market entry. Raising capital in a new country, issuing bonds, or accessing a new "
        "investor market alone is financing, not commercial market entry, unless the ICP explicitly "
        "requests financing-market access. A financial-services company entering a new customer market "
        "can qualify with direct evidence of that commercial entry. Verify each country separately; "
        "do not combine actual and planned entry.\n"
        if primary.get("category") == "MARKET_EXPANSION"
        else ""
    )
    funding_guidance = (
        "- Funding: verify capital raised by the target company itself. An investment fund close, "
        "LP commitments, assets under management, or loans the company makes to customers are not "
        "a company funding round unless the ICP explicitly requests those events. Corporate debt or "
        "equity financing can qualify when the ICP does not restrict the financing type.\n"
        if primary.get("category") == "FUNDING"
        else ""
    )
    facility_guidance = (
        "- Facility opening: require source proof that this company opened, broke ground on, expanded, or "
        "acquired a specific named site, plant, warehouse, store, office, or capacity, with its location and "
        "date. The company must be the operator of that site. An acquisition OF this company, a landlord or "
        "developer announcing a tenant, a lease, permit, plan, or proposal is not an opening unless the source "
        "says the site is open or operational.\n"
        if primary.get("category") == "FACILITY_OPENING"
        else ""
    )
    launch_guidance = (
        "- Product launch: require source proof that this company made a specific named product, platform, "
        "model, or major capability available, with its date. A funding round, partnership, customer win, "
        "award, rebrand, hire, or roadmap statement is not a launch.\n"
        if primary.get("category") == "PRODUCT_LAUNCH"
        else ""
    )
    leadership_guidance = (
        "- Leadership change: require source proof naming the person, the specific role, and this company, "
        "with the announcement or effective date. Prefer the company's own release or a regulatory filing. A "
        "board-only appointment, interim cover, or a departure with no named successor qualifies only when the "
        "index-0 text covers it.\n"
        if primary.get("category") == "LEADERSHIP_CHANGE"
        else ""
    )
    partnership_guidance = (
        "- Partnership: require source proof of a concluded agreement between this company and a named "
        "counterparty, with its date and what it covers. A customer purchase, reseller listing, integration "
        "availability, membership, sponsorship, or letter of intent is not a partnership unless the source "
        "calls it one.\n"
        if primary.get("category") == "PARTNERSHIP"
        else ""
    )
    raw_day = (
        os.environ.get("BAKEOFF_EVALUATION_DATE")
        or os.environ.get("LAB_ARENA_EVALUATION_DATE")
        or ""
    ).strip()
    evaluation_date = date.fromisoformat(raw_day) if raw_day else date.today()
    try:
        max_age_days = int(primary.get("max_age_days") or normalized.get("intent_max_age_days") or 365)
    except (TypeError, ValueError):
        max_age_days = 365
    max_age_days = max(1, min(max_age_days, 3650))
    full_credit_start = evaluation_date - timedelta(days=60)
    oldest_allowed = evaluation_date - timedelta(days=max_age_days)
    excluded = [
        str(item).strip()
        for item in (normalized.get("excluded_companies") or [])
        if str(item).strip()
    ]
    example_company = str(normalized.get("verified_example_company") or "").strip()
    exclusion_line = (
        "- Never return these excluded companies or their domains: "
        + ", ".join(excluded)
        + ".\n"
        if excluded
        else ""
    )
    example_line = (
        f"- {example_company} is the buyer's own example. It is context only; do not return it.\n"
        if example_company
        else ""
    )
    explanation_guidance = (
        "- Write intent_details as one concise, natural paragraph that covers every distinct "
        "supported signal in intent_signals. The judge checks every factual clause (amount, date, "
        "product, partner, plan, headcount) against the pages you cite: state only what those pages "
        "say, and leave out product features, customers or background they do not state. A page's "
        "publication date is not the event date: write that the source dated it reported the event "
        "unless the page says when it happened. A job posting is not a hire; a plan is not a "
        "completed expansion. Use null for a signal date that cannot be verified. End with one "
        "conditional sentence (may, could) connecting the activity to the ICP product_service, "
        "often the target's own offering. Do not invent buying intent, urgency, budget, demand, "
        "tools, evaluation, procurement, or purchase plans; do not paste quotes, labels, or notes "
        "about scoring or verification.\n"
        if intent_details_enabled
        else (
            "- Fit and activity are not buying intent. In why_now, state the verified event, then label one commercial "
            "implication as possible; separate sourced fact from inference.\n"
            "- product_service is what the target sells, not the seller's pitch or a target purchase need. Tie why_now to "
            "the event's effect on the target's operations/growth. Never copy unrelated offerings. Do not invent procurement, "
            "budget, demand, evaluation, or purchase plans; avoid benchmark/scoring jargon and vague 'growing' claims.\n"
        )
    )
    return (
        f"Evaluation date: {evaluation_date.isoformat()}\n"
        f"Return up to {limit} companies. Omit a company without verified required intent.\n"
        f"Score is the sum over {limit} slots divided by {limit}, so an empty slot is a lost company. A "
        "company the verifier cannot confirm scores zero and costs nothing; only a contradicted fact or a "
        "missing index-0 event costs points. Once a candidate has a fetched, dated index-0 article, submit "
        "it and move to the next: fill every slot rather than perfect one company.\n\n"
        "Recency targets for the primary intent event:\n"
        f"- Full credit: event dated {full_credit_start.isoformat()} or later. Search this window first "
        f"(search_web with mode news and recency_days=60), then widen only if it is empty.\n"
        f"- Also fully creditable: {oldest_allowed.isoformat()} through {full_credit_start.isoformat()}. "
        "Prefer the most recent event you can verify, but never leave a slot empty to chase a fresher one.\n"
        f"- Invalid: anything before {oldest_allowed.isoformat()} or dated after the evaluation date. Do not "
        "submit a company whose only primary evidence is outside the valid window.\n"
        f"{exclusion_line}"
        f"{example_line}"
        "\n"
        "Intent contract:\n"
        "- matched_icp_signal must preserve the listed index. Index 0 is the required primary. Research "
        "and verify every required=true row before any bonus; required=false is optional and never "
        "replaces required evidence.\n"
        "- Apply each max_age_days from the evaluation date.\n"
        "- Index-0 evidence must prove the event type named in that row's category. A different event "
        "type scores zero however fresh or well sourced: a funding article does not prove a launch. "
        "When the index-0 text offers alternatives, any one qualifies.\n"
        f"{json.dumps(prompt_icp, ensure_ascii=False, separators=(',', ':'), sort_keys=True)}\n\n"
        "Required fit:\n"
        f"- Geography: {required_geography or 'not specified'}\n"
        f"- Company stage: {required_stage or 'not specified'}\n"
        f"- Required attribute: {required_attribute or 'not specified'}\n"
        f"{certification_guidance}"
        f"{hiring_guidance}"
        f"{expansion_guidance}"
        f"{funding_guidance}"
        f"{facility_guidance}"
        f"{launch_guidance}"
        f"{leadership_guidance}"
        f"{partnership_guidance}"
        f"{contact_guidance}"
        "- Verify required industry, company-HQ geography, current employee band, stage, attribute, and "
        "other stated fit requirements from public evidence; omit missing or conflicting required facts. "
        "An office, facility, job, or served market is not HQ.\n"
        "- The verifier proves stage by quoting a public sentence naming it, so prefer a candidate whose "
        "round is stated outright in coverage you already fetched.\n"
        "- For stage, use latest funding/ownership. Preserve the proven stage; normalize only true synonyms "
        "of Seed, Series A, Series B, Series C+, Private Equity, Public, or Bootstrapped. Series C+ requires "
        "Series C or later. Private Equity requires current majority/controlling PE ownership, not an "
        "investment; Public requires listed shares. "
        f"{stage_confirmation}"
        f"{seed_guidance}"
        "Never copy an unproven requested stage.\n"
        "- Employee estimates from discovery/profile are shortlist clues, not bands or current exact staff. "
        "Verify a current public band. If the ICP lists buckets, return one listed bucket after formatting-only "
        "normalization; never infer it from an estimate, boundary, or ICP request. Put only the supported band, "
        "never an exact estimate, in fit_summary. Acceptable band sources, in order: the profile's "
        "linkedin_profile_evidence Company size label; a headcount stated in the fetched primary article or a "
        "dated press release from the last 12 months ('employs about 180 people' supports 51-200); a current "
        "company page stating team size. One such source is enough; do not spend more than one extra call on it.\n"
        "- If required_attribute exists, return its literal text, passed=true, direct evidence URL, quote, "
        "and explanation; omit the company if that evidence cannot be verified. With no requirement, no "
        "required_attribute object is needed.\n\n"
        "Research order and limits:\n"
        "00. Yield: the score is the sum over five slots, so aim for three to five verified companies per ICP. "
        "The fastest source is a dated roundup (weekly funding roundups, launch or expansion digests, lists of new "
        "facilities or hires) that names several companies with the required event: one fetch_page can seed several "
        "candidates, and that page is valid index-0 evidence for each company it names with the event and a date. "
        "After each verified company, move straight to the next hit until the status line says research stops.\n"
        "0. The budget is about 26 provider calls: search_web, fetch_page and get_company_events cost 1 each, "
        "get_company_profile costs 2 (the company record and its LinkedIn size); pass include_financing=true only "
        "when no article you read names the company's latest round, because it costs about five searches. Alternate "
        "strictly: one discovery search, then verify its best one or two hits before any further search. Verify "
        "a candidate by calling fetch_page on the dated hit and get_company_profile on its domain together in one "
        "step; profile at most two companies per step. Never run two discovery searches in a row while a dated hit "
        "from the last search is still unverified; when the budget and the clock allow, keep alternating. Take the "
        "first dated hit that names the required "
        "event and a company of the right kind, verify it, then move to the next hit. Never spend calls on giants "
        "or on a candidate whose stage or country already conflicts. A run that ends without submit_companies "
        "scores nothing. A status line after each tool result reports elapsed time and calls left; use it.\n"
        "0h. HIRING or JOBS intent: for each candidate call get_company_events with categories ['HIRING'] and the "
        "matching job_category first; a returned open listing whose title or duties match the named function is "
        "complete index-0 evidence by itself: cite its url, use its posted date, and quote its returned duties as "
        "the snippet. If it returns no openings, run one search_web in jobs mode naming the company and the "
        "function (Greenhouse, Lever, Ashby and careers pages are readable by the judge) and cite the posting it "
        "returns; do not spend a fetch_page on a job board that returns little text. Prove stage separately with "
        "the funding article or profile financing.\n"
        "0b. Employee band decision, with no extra searching: prefer (a) a page stating the headcount or team "
        "size (primary article, dated press release, or about page), cited first in fit_evidence_urls, "
        "else (b) the profile's linkedin_profile_evidence.employee_count label. employee_count_estimate is a shortlist clue only: never "
        "turn it, a boundary, or the ICP's requested buckets into a band. If neither (a) nor (b) supports a "
        "listed band, omit the company.\n"
        "0a. Company domain: take company_website from the fetched article body, the company's own newsroom "
        "URL, or a search result on the company's own site. Never invent or vary a domain (name.ai, name.com, "
        "namelabs.ai). If get_company_profile returns an empty company for a domain you inferred, the domain is "
        "wrong: run one search_web query in search mode with the company name and the word official, take the "
        "company's own URL from the results, then profile that domain once.\n"
        "0c. When the primary intent is a dated event (funding, launch, hiring, expansion, opening, acquisition, "
        "appointment, clearance), do not call search_companies at all; it returns large incumbents. Discover only "
        "with search_web in news mode, naming the event, the required stage, the industry, and the country.\n"
        "1. Keep fit discovery separate from event verification. For a narrow dated primary event, start with "
        "one focused search_web news/jobs query using business context and required stage. For Series C+, "
        "search Series C, Series D, or later. Queue distinct dated hits; prioritize one naming the required "
        "stage and primary event. Profile its domain and fetch_page on the exact returned URL before more discovery.\n"
        "2. Otherwise use search_companies with a short business-type query and structured fit filters; omit "
        "intent and literal stage. After empty discovery, change the query and loosen one discovery filter or "
        "use one broad search_web fallback; never loosen final fit.\n"
        "3. Make at most two search_companies and three total candidate-finding calls before verifying a plausible "
        "candidate. Verify the strongest untested queued hit before revisiting an empty/failed domain; revisit only "
        "with new direct evidence for its failed fact. Never repeat an equivalent query, domain, or URL.\n"
        f"4. Shortlist at most {min(limit + 2, 7)} domains; expand once only if needed. A verified_example_company "
        "is context, not proof. Use get_company_profile or one focused fit search only for a missing required fact. "
        "Prefer current page evidence over stale profile data. Stored LinkedIn URLs are unverified; establish the "
        "canonical company URL from a current page or leave company_linkedin empty.\n"
        "5. For a named candidate whose site: search is empty, use the one allowed non-site alternate instead of a "
        "near-repeat; it is not an extra call. Verify a resulting dated hit before abandoning the candidate.\n\n"
        "Event evidence:\n"
        "- Per candidate, try get_company_events or one focused primary-intent search; use the other only if needed. "
        "Fetch the best returned URL, with one alternate after a failed/unsupported page. Never construct a URL. "
        "A homepage proves identity/fit, not a dated event.\n"
        "- Quote the fetched article body, not snippets, navigation, or related cards. From an annual report or "
        "announcement index, fetch the original dated announcement. A linked event uses its own page, URL, and date.\n"
        "- Use the actual event/announcement date, never crawl, update, or index dates. Preserve source status: beta, "
        "preview, pilot, planned, future, or merely announced is not completed/operational when completion is required. "
        "For appointments, distinguish announcement, effective, and start dates; a future start is not completed. "
        "For a completed launch/opening, use its stated event date, not a later article publication date.\n\n"
        "Explanation and output:\n"
        "- For index 0 attach the fetched primary event with the article's own date, its exact URL, and a verbatim "
        "12-40 word quote from the article body. A second index-0 signal is worth more only on a different website "
        "(for example the company's own announcement plus a news article); never attach two URLs from one site.\n"
        "- fit_evidence_urls, in this order: the page stating the headcount or band, then the page naming "
        "the current stage (round announcement, exchange listing, or controlling PE owner), then the "
        "page stating HQ. The verifier reads only the first three and rejects an unprovable band.\n"
        "- company_name must be the name exactly as the company writes it on its own site, not a legal-entity form. "
        "Leave company_linkedin empty.\n"
        f"{explanation_guidance}"
        "- Submit ranked, schema-valid JSON when enough companies pass or further work cannot help. A company "
        "whose index-0 event you read in a fetched article, with a band, country, and stage evidence, is complete: "
        "submit it. Do not withhold it for a missing bonus signal or a second source. An empty submission is the "
        "worst outcome."
    )
