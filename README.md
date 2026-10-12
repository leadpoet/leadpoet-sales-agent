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
4. Bonus criterion rows only when a dated page proves them (`bonus.py`); cited URLs checked through the judge's fetch
   route and page verdicts, with the judge's Exa-contents fallback before a URL is removed (`preflight.py`).
5. Admission by expected value, up to 5 companies on list-type ICPs and 3 otherwise (`admission.py`), lock and
   checkpoint (`lock.py`), the intent paragraph (`intent_details.py`) and a Sonar re-check (`reverify.py`) that drops
   only a name/website mismatch.

`required_attribute.passed` is true only when the quoted sentence, taken from one HTML block of the company's own page
and found verbatim in its visible text, states the attribute in its own words (`fitproof.states`). The paragraph
states every source date the judge admits (a date shown on the page or the page's own publication metadata).

Page reads (`arena_tools.py`, `identity.py`): HTML pages go through the sandbox's web egress proxy first and are read with the scorer's visible-text helpers; board APIs, anti-bot walls and failed reads fall back to the Deepline scrape. Provider requests on the worker socket carry only the host's allowlisted headers (`arena_transport.py`).

Spend and time (`governor.py`): discovery may use the cap minus earlier attempts' spend minus a $0.10 reserve, and
keeps 30 Deepline calls for the later phases; candidates the triage call rates unlikely wait until after the second
event pass. At most $0.74 of successful sourcing spend per ICP across attempts ($0.78 in rescue mode), at most six
provider calls in flight, no provider call after wall-clock minus seven minutes; every model call goes through
`llm.py` (175-second client timeout, no retry after a timeout).

License: AGPL-3.0 (`LICENSE`); parts adapted from MIT-licensed code (`LICENSE-leadpoet-platform.txt`).
