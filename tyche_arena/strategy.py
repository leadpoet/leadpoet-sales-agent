"""How to spend a Leadpoet run's 45 minutes, given how Leadpoet scores it. Leadpoet only; it changes the order
of research, never what TYCHE checks or accepts.

Evidence, round arena-2026-10-30: every ICP score of 11 entries fits sum(company points) / 4 exactly, minus 2.5 for
each company whose signal 0 was judged and not verified. A qualified company earns 60 x the source multiplier for one
verified signal (51 own site, 54 news, 60 job posting) and 80 for two (lead_scorer.py SOURCE_TYPE_MULTIPLIERS,
COMPETITION_INTENT_CAP_BY_SIGNAL_COUNT; competition.py _evidence_source). All 10 ICPs offered a second signal.
Published two-signal companies failed the paragraph check 54% of the time against 14% for one signal, mostly from
template paragraphs and unsupported bonus clauses; the seven entries with the best records passed 17 of 20.

Evidence, round arena-2026-11-02, all entries: 0 of 33 two-signal companies qualified against 54 of 143 with one, the
paragraph check passed 0 of 6 against 54 of 63, and the two entries that led 11-01 and 11-02 did no later-signal work.
A later signal is worth 26 points, a new company 54, so every lookup goes to intent signal 0 and new companies.

Evidence, round arena-2026-11-02: hiring was signal 0 on four ICPs and v8 qualified nobody on them. A local rerun of
ICP 5 held McCANN Canada on a LinkedIn "2 weeks ago" date, yet the judge reads job postings itself, a posting needs no
printed date (prompts/_common.py) and "is hiring" is supported wording (intent_details.py).

Evidence, round arena-2026-10-10 (reports/leadpoet-round-20261010):
- Across TYCHE's local Arena runs, ICPs requiring Private Equity or Series C+ published 0.2 companies per run and
  Public 2.2; the commonest blockers were an unproven stage and an unproven intent signal 0.
- Competitors scored on the ICPs where TYCHE published nothing by starting from a page about the required event
  (a facility opening, a seed round, a job board) that named the company and proved the event, and often the stage.
"""

COVERAGE_STRATEGY = (
    "Leadpoet adds up points per qualified company. One verified signal earns 51 cited from the company's own site, "
    "54 from a press wire or news site and 60 from a job posting (a URL path with /jobs, /job/ or /careers), but job "
    "postings verified only half the time against 87% for press wires, so use one only when the signal is hiring. Prove intent signal 0 only: leave later intent signals unknown and spend no lookup on them, because each published signal is one more claim the judge must find, and a new company earns more. An empty slot earns nothing, while "
    "a published company that fails costs at most a fifth of a company, and nothing if it fails before its intent is "
    "judged. So when signal 0 is proven and another check is only uncertain, hold the company rather than reject it: "
    "held companies fill empty slots. Spend the time on candidates whose hardest requirement is "
    "already proven. Discover them event-first: search news, press releases, regulator notices or job boards for "
    "the event intent signal 0 describes, in the requested industry and geography and within its age limit, and "
    "take the companies those pages name and date. When the request names a Private Equity, Seed or Series stage, "
    "also search announcements that state it, such as a funding round naming its series or a private-equity firm "
    "acquiring or taking a controlling stake in the company (a minority investor is not a Private Equity stage), "
    "then confirm that no later round, acquisition or listing supersedes it. Only then check size, headquarters and the required attribute. Give each candidate about four "
    "lookups: if its stage or intent signal 0 is still unproven, hold it with the named gap and move to the next "
    "candidate instead of searching further. Confirm each qualified company as soon as its evidence is saved and "
    "keep sourcing until five are confirmed or time ends. Cite pages Leadpoet's judge can open: never a LinkedIn "
    "page, other than a job posting for a hiring signal, as the URL for intent signal 0, the company stage or the required attribute when another captured page "
    "states it. "
    "Leadpoet's published judgments show where companies are lost. In round arena-2026-10-28 the required attribute "
    "was the first failed check for a third of rejected companies across all fourteen entries: before researching a "
    "candidate's signals, find a page of its own (product, About, locations or investor page) with a sentence that "
    "states each part of the required attribute in plain words, and cite that sentence as the attribute's quote; a "
    "slogan, navigation text or a third-party list is not proof. Across about 6,000 judged companies since October "
    "14, industry was the commonest blocker: keep a candidate when its own pages describe a real offering in the "
    "requested industry and sub-industry, even when it is not the company's only business, and drop adjacent or "
    "holding companies before spending lookups on their signals. Company stage came next: cite a dated page the judge can open that "
    "states the stage, such as the announcement naming the funding series, or the investor-relations page naming "
    "the exchange and ticker for a Public stage; the quoted sentence must contain the round label itself (a stage "
    "quote without it passed about 7% of the time), and an adviser's or law firm's deal page rarely passes. Never "
    "cite sec.gov, which refuses automated readers, when another copy of the same fact exists. Companies published "
    "with their verified LinkedIn company record qualified more often (26% against 20% in arena-2026-10-28), so "
    "obtain it whenever a lookup can prove it. For intent signal 0 cite the page that states the event itself, dated "
    "within its age limit and naming the company, rather than a secondary summary: for a product launch the "
    "company's own changelog, release notes or blog post counts and is often the only dated source, so check those "
    "pages; for a hiring event cite the exact URL of one open "
    "posting whose title names the requested role family, never a board or search list. A posting from the last 90 "
    "days passed far more often than an older one: 66% within 30 days, 24% past 90, so take the newest. Its own "
    "applicant-tracking page is best (for Greenhouse the boards-api.greenhouse.io/v1/boards/<board>/jobs/<id> URL also "
    "counts), then a job site naming the company, role and city: they verified 14 of 27 and 20 of 40. A posting with no "
    "printed date still counts, and a linkedin.com/jobs/view page is the last choice but counts too, because the "
    "judge reads the posting. Quote the role title, never a relative date such as '2 weeks ago'. Write the claim as "
    "'X is hiring a Y in Z.' with no date: a hiring claim with a posting date passed the paragraph 8 of 17 times, one "
    "without 8 of 11. If a company ends with only "
    "signal 0 and its event is on both its own site and a press wire, cite the press wire, and prefer either to a "
    "blog or news aggregator: over four rounds those verified 61% against 79% for a company's own site and 77% for a wire. "
    "Unless the signal is hiring, write signal 0's claim as one plain sentence that opens with the company's name and "
    "says what it did and when, "
    "using only facts on that page, with no benefit, metric or forecast it does not state: the judge checks every "
    "clause of the published paragraph, and one unsupported clause fails the whole company even with signal 0 proven. "
    "A strategic partnership needs a named partner and concrete joint work, not a partner-programme listing or an "
    "ordinary customer, and when the signal names a kind of partner, the partner itself must be that kind. "
    "Leadpoet's fit verdict follows the company, not the page: across 81 companies seen in three or more entries, 36 "
    "always passed and 21 never did, and those were near neighbours of the request. Prefer a company whose own homepage "
    "plainly states the requested sub-industry and required attribute over an adjacent one, and cite the required "
    "attribute from a short About or product page: pages four or more path levels deep passed 39% of the time, others "
    "about 66%. "
    "After Leadpoet's first web check, a stricter reviewer rereads every passing company against the whole request: "
    "the industry, the sub-industry, the product or service and the required attribute. It answers unproven for an "
    "adjacent activity for the same customers, and it wants every function and every AND condition in those clauses "
    "supported by a page about the company's own supplied product, where equivalent wording counts: a security platform that must detect "
    "threats, manage vulnerabilities and respond to incidents is not proved by a code-scanning product, and a "
    "business bank is not a card-issuing API. Before spending lookups on a candidate's signals, read those clauses "
    "against its homepage or product page; if a function is missing, move to the next candidate. Cite pages that "
    "show their text without JavaScript, because the judge reads quotes from a plain fetch. "
    "With about eight minutes of research left, start no new candidate: finish the size, headquarters and attribute "
    "checks of held companies whose intent signal 0 is proven, because an empty slot earns nothing.")
