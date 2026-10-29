# Detailed Workflow Rules

Use this skill for a company-sourcing request: a target count, an ICP and the
initial qualifying intent signal. The run produces unique, evidence-backed,
independently reviewed companies. Buyers and contact data belong to the contact
stage (DEMOS) and are refused here; deeper intent research belongs to ARGUS.
Use [tools.md](tools.md) to select a route and load only that adapter's required
sections. Read the input, output and Excel contracts by the phases in
[output-contract.md](output-contract.md#read-by-phase), not as an upfront bundle.

The main workflow is the research loop in [SKILL.md](../SKILL.md). This reference
keeps the detailed qualification, spending, receipt and completion rules.
Native tools implement the receipt, ID and budget mechanics below; supply research
choices and judgments rather than rebuilding those records.
Command paths below are relative to the skill directory, not this reference.

## Operating rules

- Keep the strategy open and the evidence gate closed. Form several materially
  different hypotheses (for example hiring, funding/news, paid activity,
  patents, facilities, official video, or firmographic fit) and change route
  when rows repeat or lack evidence.
- Use one agent to run up to three independent company checks concurrently via
  [bounded batches](adapter-io.md#concurrent-company-checks), including distinct
  known-input checks for one company. Resolve and deduplicate canonical domains,
  aliases and owner groups before batching; never depend on a sibling result.
- Discover live Deepline capabilities with `search`, inspect a chosen tool with
  `tyche_inspect(tool=...)` once, and reuse its saved description. Native lookup
  code checks inputs, availability and pricing before dispatch. Read omitted
  detail with `field` only when needed; refresh after a confirmed contract or
  access change. Never invent a Deepline tool ID. Optional hypotheses such as
  PredictLeads events, HarvestAPI LinkedIn posts, TheirStack jobs/projects, or
  DiscoLike niche discovery are choices to test, not a mandatory fanout.
- Tools that find, verify or return people, email or phone are refused before
  any call. Company research never needs them: a person named in a captured
  company source is part of that source, not a lookup target.
- Choose economical research routes within the budget by expected evidence value,
  coverage and total effort. Receipt convenience alone should not choose the source.
  Native lookups save provider responses; built-in web observations use the review
  tool. Record observed source objects, not serialized transcripts. Web research
  still consumes model and tool usage; paid scraping is not free.
  Pilot company-discovery routes with at most 10 returned rows and one paid call.
  Inspect rows, evidence, duplicates, misses, provider status and cost before
  expanding. No automatic retry: a timeout or other uncertain paid outcome is
  unresolved and needs a different route.
- Resolve flagged exclusion-name variants before accepting. Confirmed same-company
  names belong in `company.aliases` and remain excluded. For a distinct entity,
  answer its `exclusion:N` criterion (`Distinct from excluded company: <name>`)
  with identity evidence. Similarity alone cannot reject a company; broad
  substring matching is not identity proof.
- An accepted company needs current evidence that identifies the same company for
  its fit and its required signals. A namesake, subsidiary, customer or group
  entity does not qualify the target, with one exception for its own site:
  On the company's own site (its saved domain), an offer sold by one of its own
  group entities that runs this same business (such as its e-commerce arm)
  counts as the company's; a third-party marketplace seller, a franchisee, a
  member store or a group company trading under another brand is a separate
  business, and a shared brand name alone does not make a franchisee or member
  store a group entity.
- Keep accepted, rejected and unresolved output states separate from provider
  statuses. Every unresolved company states its missing evidence and next step in
  `reason_text`; park reviewed gaps without inventing another action. Provider
  failures are not companies and never count in reviewed or accepted totals.
  `no_results` is valid only when the provider actually returned no results; an
  input error, response error, timeout or uncertain response stays unresolved or
  blocked.
- Treat `target_count` as the completion condition. While accepted companies
  remain below it, refill from a changed route, query, page, tool or provider.
  Start no more new companies than the remaining shortfall; the three-call
  lookup batch cap is separate. Do not use a fixed 5x multiplier or other fixed over-fetch.
- When an approach is exhausted or the queue is empty, choose a materially
  different source or method under the [stopping contract](output-contract.md#stopping-check).
  Deliver a shortfall only when the actual budget or saved deadline stops work.
  Keep its candidates unresolved, preserve every receipt and report missing
  evidence; do not turn missing evidence into acceptance or invent a provider
  outage to finish. Do not repeat full-state validation between ordinary reads
  or after an attempt helper has already returned the current stop decision.
- The lookup helper maintains the frontier for paid and no-cost public-web work.
  Each concrete route or query path is `untried`, `continuable`, `exhausted` or
  `blocked`. A failed or uncertain provider call blocks automatic retry of that
  call, but it does not end the run while another route is untried or
  continuable. Mark a provider error or uncertain outcome `blocked`, never
  `exhausted`. Keep the frontier append-only.
- Use the unique route ID the helper returns for each attempt or continuation. A
  failed attempt receipt may share its ID only with the outcome for that same
  failure; a later continuation always needs a new ID. `continuation_route_ids`
  cross-link future searches to the route that produced them. Do not mark a
  route exhausted while a promised continuation is unresolved. Before stopping,
  review promising unresolved paths and record why each is no longer actionable.
- Record provider usage only from a usage or billing receipt. An unknown charge
  stays `null` and pending; do not substitute a quote or assume it was free.
- Honor an explicitly requested `budget.max_deepline_credits_per_next_lead`:
  stop new calls once observed spending since the last accepted company reaches
  it. The last call or concurrent batch may exceed it. When absent, 5 credits is
  only a strategy-review warning. Rejected candidates, failed calls and demotions
  never reset spending.

## Inputs and workflow

### Default run budget

New runs use one combined stopping threshold: reported provider charges plus
estimated base LLM cost captured by the local launcher. The default is USD 0.80
multiplied by `target_count`; an explicit budget overrides it, including zero.
Do not ask for approval solely because the user omitted a budget. Preserve
explicit provider credit limits; call counts remain audit data only.

The [start helper](adapter-io.md#start-or-resume) persists the original threshold,
provider limits and credit-to-USD conversions. Reported USD takes precedence
when available; otherwise reported credits convert at the saved plan rate. An
unused provider has a zero limit. ScrapingDog requires its plan conversion and
keeps documented tariff ceilings until a response establishes the exact charge.

Before dispatch, code checks the known combined total. Once it reaches the
threshold, no more paid calls or model invocations start. Calls already in
flight can finish above it. Missing billing pauses new paid work for read-only
reconciliation; never replay an uncertain paid request. Report the known
subtotal and pending charges separately. The launcher saves reviewed partial
output without another finalizer when spending stops.

Requested companies determine the original threshold; rejected companies,
retries, refills and resumptions never reset or enlarge it.

### Request normalization

The normalized request states the target count, ICP filters, required
attributes and exclusions, the buying signals with their importance and
freshness windows, and the budget. It carries no roles, contacts per company or
contact fields: those are the contact stage's inputs, the caller keeps them, and
`tyche_start` refuses them. `original_text` keeps the user's wording, including
any buyer requirements, unchanged.

- Save each signal with `importance: required` or `preferred`. When the user
  names no signal, record an inferred use-case hypothesis as `preferred`.
- Copy each company type, industry, geography, required attribute, exclusion
  and required signal's `query` verbatim from `original_text` as one contiguous
  passage in the user's own words, keeping its named sources, alternatives,
  dates and scope; a signal's query runs to the end of its sentence, bullet or
  line, and its `source_preferences` only repeat sources that query names. Omit
  a filter the user did not state. `tyche_start` refuses a paraphrase of a new
  run before anything is saved; a `request-exclusions.json` list is bound as
  written, and a resumed run keeps its saved request.
- Companies the brief names as the only ones to qualify (a named-account list,
  not examples) are alternatives: save each as a company type (any one is
  enough), never each as its own required attribute. When the brief relates
  the company to named companies (uses, sells, belongs to, works with), save
  its words for that relation with the list as one criterion. A company has one
  domain, so `tyche_start` refuses a new run whose required attributes include
  two or more that each only name a different company (its name and one domain).
- `signal_match_mode` is `any` (default) or `all`. Per-signal
  `min_age_days`/`max_age_days` or `max_age_months`, the shared `time_window`,
  the run ID and the as-of date are optional. A lower bound never exceeds its
  upper bound.
- Add `budget.max_deepline_credits_per_next_lead` only when the user asks for a
  per-company hard cap.
- `product_service` with its `perspective` is carried for the later stages; it
  never widens or narrows the company criteria.
- Numeric employee filters use inclusive bounds. For discovery, include every
  provider bucket that overlaps the requested range. For acceptance, the
  `icp:company_size` check decides from evidence of whose workforce a figure
  counts; a bucket crossing a boundary needs more evidence, not rejection.

Code derives the [criteria](output-contract.md#criteria) from this request, each
with a stable `criterion_id`. Resume the saved request rather than infer it
again. Resolve obvious company identity ambiguity before paid work.

### Review and delivery details

Use the [research loop](../SKILL.md#research-loop) and
[review helper](adapter-io.md#save-a-review). The helper records company
decisions, closes reviewed sources and refreshes totals; do not maintain a
second plan or copy calculated summaries by hand. Preserve historical receipts.
With continuation links, close children before parents; reopen the parent
before a child. An unrecoverable receipt stays blocked with the gap stated.

Use the [stopping contract](output-contract.md#stopping-check) for final delivery.
Keep unfinished routes open when an actual budget or time limit ends work. A
reviewed company can remain parked while fresh discovery continues. Missing
evidence never proves failed fit. New runs have no research deadline unless the
user sets one; there is no minimum-spend target and no reason to make wasteful
calls.

`tyche_finish` writes the results, workbook and report through the
[exporter](output-contract.md#leadsxlsx-contract). It runs full strict
validation and saved-file checks. Inspect the preview and require explicit
`delivery_allowed: true`; do not assemble reports, rerun export or copy costs by
hand.

## Qualification policy

Keep one qualification policy; research models supply evidence, not a separate
acceptance standard. Do not add scores or gates. After a blocked or unproductive
stage, record its recovery action and change source for that gap; do not leave
promising companies untouched while repeatedly starting new searches.

- Separate user must-haves from preferences at normalization. Preserve explicit
  constraints, required signals and their windows; do not silently add them.
  Optional intent improves ranking, not eligibility.
- Verify must-haves. Ordinary workflows may be inferred from sourced business
  facts when a plausible use case is enough for the request; label the inference
  and its basis in the claim, and never imply observed pain, intent, incumbent
  tools or manual processes. A specifically required workflow needs evidence.
  Review required web claims against the captured source body, reusing a saved
  read when available. Check its actual date and activity status against the
  request; a search summary may omit a qualification or completion date. If the
  body is inaccessible, use another credible source or keep the criterion
  unknown. Structured provider evidence remains eligible without an extra web
  read. Required web evidence quotes the captured passage; interpretation goes
  in `claim`, never into the source text.
- One credible source can suffice; use the same standard for every candidate.
  Resolve material contradictions, not merely overlapping headcount ranges.
  A requested funding stage describes current status.
  For a requested latest/current funding stage, check current funding status before
  detailed qualification; a historical round alone does not establish it is still latest.
  Reuse credible current evidence and resolve later completed priced-equity rounds,
  acquisitions or IPOs; plans or debt do not change that stage unless requested,
  and missing proof stays unknown.
  Compare announcement dates, not array order or the highest label.
  For Public, check the exact company's current status, not its parent's.
  First reuse a relevant captured announcement if it states that company's
  listing; otherwise seek credible current evidence within the research limits.
  Save the supporting passage in the requested stage check. An undated provider
  Public Company label does not resolve unclear identity or current status.
  Keep missing proof unknown and record the gap. A ticker or exchange is not an
  ICP condition.
- Corroborate facts across credible sources for the same identified project. A
  recent announcement or progress update can establish activity within the
  window; drawings, specifications or another project report can establish its
  technical requirement even when older. Record the linkage, each source's own
  date and the fact it supports. Never date old technical work as new, combine
  unrelated projects or substitute general capability for required recent
  intent. Installed work shows project activity, not an outstanding purchase.
- Accept supported must-haves, even without optional intent. Keep missing
  must-haves unresolved; reject evidenced mismatches. Recover a specific gap
  through another source or signal within existing limits. Never count
  unresolved rows as qualified.

## Gates, statuses and artifacts

`results.json.request` is the authoritative normalized ICP. Check decisions
against it, not an earlier candidate's band or a rewritten interpretation. Do
not turn a service area into an office requirement or a preferred signal into a
requirement.

The company gate requires canonical `company.canonical_name`, `domain` and
`website`, an evidenced `account_fit`, and one check per requested criterion
selected by `criterion_id`. Native reviews store signals once in
`qualification_checks`; code derives `signal_evidence` from the first passing
required signal. Record each criterion as `pass`, `fail` or `unknown` with the
reason in `claim` and an evidence array. Reject only an evidenced failure of a
required criterion; keep a required unknown unresolved so missing evidence never
becomes a silent false negative. In a run that exports signal candidates, a
company that passes every filter, size and attribute check and is not excluded
stays held, never rejected, when only its required signals fail, so it is
offered downstream. Save criteria as evidence arrives; do not
collapse several known facts and one missing field into a single unknown check.
Fit alone never counts as the signal, and a preferred signal never satisfies a
required one. An observation date dates the business facts, never an inferred
event. Date an event by the most precise date its source gives: a news story
that reports it as new dates it to the article, a recap of an earlier event does
not, and a listing still open when captured, such as a vacancy or tender, is
current, dated by the capture whenever it was first posted. A site build/deployment
timestamp or page-modification timestamp alone does not date the reported event.
A window the request gives is a requirement: an event dated inside it passes, one dated wholly
outside it fails, and one whose date range straddles the window's edge stays
unknown, never rejected. With no window, the date is not a requirement.
Unsupported facts and search-results pages cannot pass. Resolve relative dates
from retrieval time and keep the original wording in the report.

With `icp.company_size`, size is a reviewed required check. Whose workforce a
figure counts decides it: a network, franchise, cooperative or group headcount
is not the central entity's, a head office or department count is not that of a
company that also employs store staff, and a LinkedIn member count is not a
headcount.
The correctly identified company's own size band, such as its LinkedIn
company-size band, supports range fit only when the whole band lies inside the
requested bounds, it is current and applicable, and no saved evidence
conflicts; it is not an exact headcount. An overlapping band, an ambiguous
entity, or conflicting or stale evidence stays unknown. A LinkedIn band the size
check does not cite is not conflicting evidence against a quoted current figure
for the company's own workforce.
Two credible, current workforce figures or ranges for the same company that both lie
wholly inside the requested bounds do not make range fit unknown merely because
their numbers differ; check what each counts. Do not compare a LinkedIn member
count with a headcount band as conflicting workforce figures. Current means after this
entity's restart, bankruptcy, merger, acquisition by another company or major
downsizing that saved evidence mentions; an earlier figure is historical. A LinkedIn band has no as-of date:
after this entity's restart, bankruptcy or major downsizing, keep size unknown
unless a dated figure from after it agrees. A network, franchise or cooperative brand's LinkedIn page usually counts
the whole network, not its central; a stated central headcount decides a
central's size. LinkedIn evidence alone (a page, posts or the company record)
never fails size: keep it unknown. A request that names stricter size sources or
excludes bands is followed as written. Who does the work, and for whom, decides
operations: a product, service or software this company offers that enables an
activity does not show that it performs that activity, and selling others the
means for their own activity (such as ad space, media or audiences sold to
advertisers) does not show that it does that activity itself. When a
requirement is itself a service for others (such as placing nurses or providing
IT support), performing that service meets it. A requested kind of business,
such as a company type or an industry, is met by the company's own business,
what it is and mainly does, in any wording: a synonym, a translation or a
narrower kind of a requested one counts, and so do one of several main
businesses and a chain of that kind the company runs as franchisor, cooperative
or central. A provider's industry label can use a different taxonomy. Unless the
request specifies that provider category, compare the described main business
with the requested business, not the label's exact wording. A neighbouring kind
that only sells some of the same goods or serves
the same customers is not a requested kind unless the request names it: a
department or range inside another kind of business does not make it that kind.
When a check names kinds of business and none of the company's main businesses is one of
them, that check fails; when its kind is unclear, keep it unknown. When a
required attribute names a function, such as central purchasing or category
buying, quote a passage that states that function for the correct company
(equivalent wording counts); store counts, network membership, brand pages, a
head office that supports its stores or a list of establishments alone leave it
unknown. A request that names no such function gains no such requirement. When a
requirement joins parts with "and", quote words for each part; an unqualified
or neighbouring category does not state a narrower one. A passage whose subject
is another company (a supplier's or partner brand's own range, a member store's
own offer) does not support this company's criterion; keep it unresolved. Never
cite a page whose link names a person (an interview, testimonial or profile
page). Follow the [LinkedIn company record](output-contract.md#linkedin-company-record)
rules for optional LinkedIn facts.

Write `reports/<run-id>/report.md`, `results.json` and `leads.xlsx`. The report
contains the request, criteria, assumptions, hypotheses, route and evidence
receipts, pilot observations, route cost bases, confirmed and maximum credits,
Deepline dollars and cost per accepted company, statuses, accepted, rejected and
unresolved rows, and the stop reason. For a shortfall it also shows the route
frontier, continuation decisions, remaining call capacity, reviewed-company
counts and why each remaining route is exhausted or blocked. Record timing and
accepted-company provenance as in the
[`report.md` and final response section](output-contract.md#reportmd-and-final-response),
maintained as work proceeds. Never store credentials or raw secrets.

Unverified optional workbook fields stay blank. Full evidence, run status and
rejection details stay in `results.json` and the report.

This is a small direct-wrapper workflow. It has no `Sourcing_model` or `pp`
runtime dependency, browser harness, server, database, queue, CRM write,
outreach action, required subagent or hidden API. Do not add one to complete a
run.

### Request coverage

Before `tyche_start`, compare the interpreted request with every original
constraint. Keep independent financial and employee thresholds, exact event
lists, windows, the evidence sources a signal names (for example "per press
release, trade press or LinkedIn"), exclusions, any/all logic, alternatives and
scoped exceptions. Only a passage from a signal's named sources supports it.
Do not substitute a financial metric the user did not name. Save separate
must-haves in `icp.required_attributes`; signal alternatives do not waive them.
Resolve material ambiguity before paid research. Resume the bound request
rather than reinterpreting it.

### Evidence-meaning review

Match the strength of the claim to the evidence. These cases belong to the same
source review, not a separate rule engine:

- **Specific activity:** a link to a collection or a general category description
  does not establish a matching current activity. Use a matching item or an
  explicit statement of that activity, preserving the requested relationship.
- **Hiring:** a generic careers page, job categories or an empty listings shell
  does not establish a current vacancy. Open the matching listing or use explicit
  current hiring evidence. One current vacancy supports a single observed
  opening, not rapid hiring or a surge; keep a required surge unknown until
  stronger evidence is found.
- **Metrics:** match the metric, entity, currency or units and time period of
  every numeric threshold. Increased loss totals can reflect frequency or
  exposure, not claims-cost inflation. Member-network sales, customer
  transaction volume and parent or group revenue do not establish the target
  company's own revenue. A subsidiary's event qualifies only when it meets the
  requested entity or geography relationship. A missing company-specific number
  is unknown, not below threshold.
- **Repeated hiring:** one posting copied by several aggregators is one
  observation. Repeated-vacancy claims need distinct, dated observations.
- **Geography:** an ambiguous aggregator location does not establish a company
  operation in the requested geography. Use evidence for the relationship the
  user asked for, such as headquarters, service area or operations.
- **Expansion:** an announced partnership, conditional approval, planned rollout
  and completed launch are different claims. Preserve the source's status.

Before delivery, follow the final packet from `tyche_finish`: compare identity,
required fit, the initial signal and material output claims with the saved
sources. For requested Public stage, check that the stage passage supports
current listed status for that company; return a concrete research gap if it
does not. Final review makes no new lookup. Correct optional factual errors
without rejecting a qualifying company. Use `tyche_review` for changed fields,
then review the fresh packet.
Preserve unchanged records, receipts and budget. Return its `review_ref` with
source-based `review_findings` only after resolving the affected criteria and
claims. Strict validation and review hashes verify structure and version, not
source meaning.

### Parallel company workers

File-backed launcher runs use one researcher unless `--workers 2` or
`--workers 3` asks for parallel researchers, which use this same loop. In a
parallel worker, claim the real website domain with `tyche_claim`,
including its verified LinkedIn `company_url` when known, before
company-specific research. If another worker owns it, skip it. Use the returned
target throughout qualification. Work **one company at a time**: find → claim →
qualify → accept and confirm, reject or hold → next company. Resume
`parallel.current_company` first after a restart. Before another claim or broad
discovery, confirm the accepted company, reject an evidenced mismatch, or save
`hold_account` with the specific missing evidence and why available routes
cannot resolve it. Do not hold just to open more candidates. A hold keeps
ownership and evidence for later follow-up; missing evidence is not rejection.
Company-scoped searches can resolve gaps. Broad discovery uses
`target: discovery`; start with your assigned search approach when there is no
current company. Discovery can return many prospects; claim and process one,
then reuse the saved discovery results for the next. Use
`parallel.owned_companies` to resume your own work. All workers share one budget,
deadline and target. Save only your own company and source decisions; the
launcher waits for researchers to exit before a single final review and export.

On `worker_yield`, end immediately without polling. Code reduces concurrency near
the shared budget cutoff; finish your current company before yielding. A retired
worker's recoverable company may be reassigned with its saved evidence, but never
steal live ownership or replay an uncertain paid call.
