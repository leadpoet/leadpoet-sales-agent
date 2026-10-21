# Lab Arena miner (company-only rounds, output v6)

Entry point: `harness.run_icp(icp) -> list[dict]` (synchronous, one argument). No `requirements.txt`: the bundle uses
only the Python standard library and the packages present in the round image (httpx, pydantic), plus the scorer's
own deterministic helpers under `/model`, which `gates.py` appends to `sys.path`.

Pipeline per ICP:

1. Discovery lanes (`scout.py`, `stagefirst.py`, `roster.py`, `hiring.py`): event-first web and news search, a
   funding-round search at the ICP's stage, a list of peer companies with their dated news, and ATS board postings
   (Greenhouse, Ashby, Lever, Teamtailor, Workable) for hiring criteria.
2. Per candidate: the scorer's exclusion and data-quality checks, homepage and LinkedIn company record (size range,
   headquarters), stage proof from quoted pages (for Public: an issuer-bound exchange:ticker or listing line on any
   exchange) plus one current-stage search, and industry/required-attribute proof from the company's own pages
   (`fitproof.py`, pricing/plans first for business-model attributes). A venture ICP gets its stage literal unless
   a conflict is proven. The company's own announcement leads a wire copy of the same event.
3. Verification (`verify.py`): identity binding, evidence pages, dates inside each criterion's window, URL rules.
   A page names the company by its name, domain, domain label, homepage brand or bound ticker; a company whose
   homepage title is the initialism of its legal name is submitted under that brand, with the LinkedIn page its
   homepage links (or, when the fetched homepage links none, the record on its domain whose slug is that brand).
   Every dropped signal is reported with its own code and URL.
4. Bonus criterion rows only when a dated page proves them (`bonus.py`). A LinkedIn article (/pulse/), feed update or
   post is not used as a bonus fact; a /posts/ page is kept only when the company's own LinkedIn page posted it, and
   the search moves on to the next page. Cited URLs checked through the judge's fetch route and page verdicts, with
   the judge's Exa-contents fallback before a URL is removed (`preflight.py`).
5. Admission by expected value, up to 5 companies (`admission.py`), lock and checkpoint (`lock.py`), the intent
   paragraph (`intent_details.py`) and a Sonar re-check (`reverify.py`) that drops only a name/website mismatch. A row
   the re-check contests is not sent beside other rows; with time to spare, admission fills its slot once from the
   verified rows that no re-check has seen, and each new pick gets its own paragraph and re-check. On an ICP that names
   a stage, a row without stage evidence is sent only alone (when no row with stage evidence is admitted); it no
   longer fills free slots beside evidenced rows (strategy flag `bare_row_fill`, default 0). On deep ICPs (below) the
   rows that the hard ranking keys leave tied are ordered by an evidence score before expected value: a
   required-attribute claim, the primary signal not on LinkedIn, two or more signals, a company LinkedIn page, and
   stage evidence on the company's own site, each with a fixed weight (`admission.EVIDENCE_WEIGHTS`); a row whose
   announcement does not establish the required attribute's change clause (graded `none`) follows the others. The
   bonus rows are then tried on the rows in that same order.

`required_attribute.passed` is true only when the quoted sentence, taken from one HTML block of the company's own page
and found verbatim in its visible text, states the attribute in its own words (`fitproof.states`). The paragraph
states every source date the judge admits (a date shown on the page or the page's own publication metadata).

Page reads (`arena_tools.py`, `identity.py`): HTML pages go through the sandbox's web egress proxy first and are read with the scorer's visible-text helpers; board APIs, anti-bot walls and failed reads fall back to the Deepline scrape. Provider requests on the worker socket carry only the host's allowlisted headers (`arena_transport.py`).

Training capture is best effort when the worker mounts `lab_arena_checkpoint`. The agent reports exact returned text
for its direct web egress, a digest and byte count after it fully reads a successful worker-socket response, and its
discovery, verification, admission, and final row state. The final `training_capture_summary` reports missing direct-web
records and provider response identity/acknowledgment gaps. This scope does not include web pages fetched inside a
provider or another process. Older workers without these helpers still run the same research path.

Depth by ICP kind (`yieldplan.py`): qualified companies concentrate in FUNDING, PRODUCT_LAUNCH, LEADERSHIP_CHANGE and
REGULATORY_CLEARANCE ICPs at Series A/B/C+ or Public outside Lending and Investments, Real Estate and Sales and
Marketing. Paying kinds on Seed or in those industries keep the promoted depth under a $1.60 spend cap; every other
ICP keeps the promoted depth under $0.90. Those "deep" ICPs have a $2.60 spend cap, and their depth depends on the
host allowance, which the governor reads from the host's quota snapshot when the ICP starts:

- Flat allowance ($2.00 or more): discovery runs 1.7x the queries, extracted search rows (204 instead of 120),
  candidates and roster, keeps up to 14 drafts, runs the second event pass whenever fewer than 5 drafts are
  fit-proven (time and budget permitting), may run until half the wall clock (at most 30 minutes, instead of 40% and
  at most 25 minutes), has an admission pool of 12 and, in rounds without a Deepline call quota, a ceiling of 800
  Deepline calls, and keeps $0.40 of the cap for the work after discovery (verification, bonus rows, paragraphs, the
  re-check), so discovery may use $2.20.
- Lower allowance, or earlier attempts' spend unknown or still settling (the governor's lean or retry cap): the
  promoted depth, 8 drafts, a pool of 8, 360 calls in rounds without a Deepline call quota, the second event pass only
  when fewer than 3 drafts are fit-proven, the clock's own discovery window, and a $0.10 reserve.

In both cases deep ICPs use the evidence order in admission and, when a venture-stage candidate's stage searches
named no round, one search of the company's own site for the announcement of a round at the ICP's stage; that page
goes through the same round, freshness and later-round checks as any other stage proof. The plan is applied in two
steps: the lower-allowance values before the governor starts, and the final values once it knows the allowance and
the earlier attempts' spend (the call ceiling is adopted again when it rises). The run report's `yield_plan` shows the
tier, the regime, the allowance, the cap, the draft limit, the reserve, the discovery window, the call ceiling and
every value set. The host allowance still bounds every cap.

Spend and time (`governor.py`): discovery may use the cap minus earlier attempts' spend minus a $0.10 reserve ($0.40
on deep ICPs under a flat allowance, or a quarter of what the cap leaves when that is less, never under $0.10), and
keeps 30 Deepline calls for the later phases; candidates the triage call rates unlikely wait until after the second event pass. The host quota
sets the spend limit across attempts, less a $0.06 safety margin ($0.02 in rescue mode): $3.94/$3.98 for flat-$4
rounds, or the unchanged $0.74/$0.78 for legacy rounds. At most six provider calls in flight, no provider call after
wall-clock minus seven minutes; every model call goes through `llm.py` (175-second client timeout, no retry after a
timeout).

License: AGPL-3.0 (`LICENSE`); parts adapted from MIT-licensed code (`LICENSE-leadpoet-platform.txt`).
