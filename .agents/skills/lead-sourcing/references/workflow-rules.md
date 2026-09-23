# Detailed Workflow Rules

Use this skill for a company-first request: a target count, ICP and current buying
signal. The run produces unique, evidence-backed companies. Use [tools.md](tools.md) to select a route and load
only that adapter's required sections. Read the exact input, output, and Excel
contracts by the phases in [output-contract.md](output-contract.md#read-by-phase),
not as an upfront bundle.

The main workflow is the research loop in [SKILL.md](../SKILL.md). This reference
preserves the detailed qualification, spending, receipt, and completion rules.
Native tools implement the receipt, ID and budget mechanics below; supply research
choices and judgments rather than rebuilding those records.
Command paths below are relative to the skill directory, not this reference.

## Operating rules

- Keep the strategy open and the evidence gate closed. Form several materially
  different hypotheses (for example hiring, funding/news, paid activity,
  patents, facilities, official video, or firmographic fit) and change route
  when rows repeat or lack evidence.
- Use one agent to check up to three independent companies concurrently via
  [bounded batches](adapter-io.md#concurrent-company-checks). Resolve and
  deduplicate canonical domains, aliases and owner groups before batching.
  Keep each company's identity, qualification and signal checks in order.
- Discover live Deepline capabilities with `search`, inspect a chosen tool with
  `tyche_inspect(tool=...)` once, and reuse its saved description. Native lookup
  code checks inputs, availability and pricing before dispatch. Read omitted
  detail with `field` only when needed; refresh after a confirmed contract or
  access change. Never invent a Deepline tool ID. Optional hypotheses such as
  PredictLeads events, HarvestAPI LinkedIn posts, TheirStack jobs/projects, or
  DiscoLike niche discovery are choices to test, not a mandatory fanout.
- Choose economical research routes within the budget by expected evidence value,
  coverage and total effort. Receipt convenience alone should not choose the source.
  Native lookups save provider responses; built-in web observations use the existing
  review tool. Record observed source objects, not serialized transcripts.
  Web research still consumes model/tool usage; paid scraping is not free.
  Pilot company-discovery routes with at most 10 returned rows and one paid call.
  Inspect rows, evidence, duplicates, misses, provider status, and cost before
  expanding. No automatic retry; a timeout or other uncertain paid outcome is
  unresolved and needs a different route.
- Resolve flagged exclusion-name variants before this gate. Confirmed same-company
  names belong in `company.aliases` and remain excluded. For distinct entities,
  use the gate's `Distinct from excluded company: <original name>` criterion in
  an existing required qualification check, with identity evidence. Similarity
  alone cannot reject a company; broad substring matching is not identity proof.
- Keep accepted, rejected, and unresolved output states separate from provider
  statuses. Reuse the existing `stage`, `reason_code`, and
  `qualification_checks` fields. Every unresolved company must state its
  missing evidence in `reason_text`; park reviewed gaps without inventing another
  action. Every unresolved result remains at the account stage. Provider failures are not
  companies and must not be counted in reviewed or accepted company totals.
  `no_results` is valid only when the provider actually returned no results;
  an input error, response error, timeout, or uncertain response is not
  `no_results` and remains unresolved or blocked as appropriate.
- Prefer affordable completion of already qualified candidates before new
  discovery. `completion_candidates` shows missing company evidence. If completion is blocked by an
  uncertain call, unavailable evidence or insufficient remaining budget, record
  that concrete reason and choose another productive route. This is research
  guidance, not a queue, a fixed company order or a new qualification gate.
- Treat `target_count` as the completion condition. While accepted companies
  remain below it, refill from a changed route, query, page, tool, or provider.
  Check at most three companies at once, reducing the batch to the remaining
  lead shortfall. Do not use a fixed 5x multiplier or any other fixed over-fetch.
- When an approach is exhausted or the queue is empty, choose a materially
  different source or method under the [stopping contract](output-contract.md#stopping-check).
  Deliver a shortfall only when the actual budget or saved deadline stops work.
  Keep its candidates unresolved, preserve every receipt and report missing
  evidence; do not turn missing evidence into acceptance or invent a provider
  outage to finish. Do not repeat full-state validation between ordinary reads
  or after an attempt helper has already returned the current stop decision.
- The lookup helper maintains the frontier for paid and no-cost public-web work. Each concrete
  route/query path is `untried`, `continuable`, `exhausted`, or `blocked`. A
  failed or uncertain provider call blocks automatic retry of that call, but it
  does not end the run while another route is untried or continuable. Mark a
  provider error or uncertain outcome as `blocked`, never `exhausted`. Keep the
  frontier append-only: add paths and update states, but never remove a path.
- Use the unique route ID returned by the helper for each attempt or continuation. A failed
  attempt receipt may share its ID only with the outcome for that same failure;
  a later continuation always needs a new ID. Store
  `continuation_route_ids` on a route to cross-link future searches to the
  route that produced them. Do not mark a route exhausted while a promised
  continuation is unresolved; `continuation_exhausted` must reference
  successors that were actually resolved. Before stopping, review promising
  unresolved paths and record why each is no longer actionable.
- Record provider usage only from a usage or billing receipt. An unknown charge
  stays `null` and pending; do not substitute a quote or assume it was free.
- Honor an explicitly requested `budget.max_deepline_credits_per_next_lead`.
  For new runs, stop new calls once observed spending at the current accepted-company
  count reaches that threshold. The last call or concurrent batch may exceed it.
  When absent, 5 credits is only a strategy-review warning. Record
  `accepted_leads_before_call` (the retained ledger field); rejected candidates, failed calls and demotions
  never reset spending. Historical version 1 ledgers retain their original rules.

## Inputs and workflow

### Default run budget

New runs use one combined stopping threshold: reported provider charges plus
estimated base LLM cost captured by the local launcher. The default is USD 0.80
multiplied by `target_count`; an explicit budget overrides it, including zero.
Do not ask for approval solely because the user omitted a budget. Preserve
explicit provider credit limits; call counts remain audit data only.

The [start helper](adapter-io.md#start-or-resume) persists the original threshold,
provider limits and credit-to-USD conversions. Reported USD takes precedence
when available; otherwise convert reported credits using the saved plan rate.
An unused provider has a zero limit. ScrapingDog requires its plan conversion.
No money is reserved for future research. ScrapingDog dispatches
retain documented tariff ceilings until the response establishes an exact charge.

Before dispatch, check the known combined total. Once it reaches the threshold,
start no more paid calls or model invocations. Calls already in flight can
finish above the threshold. Missing billing pauses new paid work for read-only
reconciliation; never replay an uncertain paid request. Report the known subtotal
and pending charges separately. The launcher saves reviewed partial output
without starting another finalizer when spending stops.

Requested companies determine the original threshold; rejected companies, retries,
refills and resumptions never reset or enlarge it. Arena owns model transport
and model billing outside local receipts. Historical version 1 ledgers retain
their original provider-only budget and reservation rules; do not migrate or
reset them implicitly.

### Request normalization

The normalized request must state the target count, ICP and exclusions,
geography, buying-signal kinds and freshness window, and per-provider budget caps.
Company geography does not restrict activity location unless requested.
Do not add `budget.max_deepline_credits_per_next_lead` when it is omitted; carry
it through only when the user explicitly requests a per-next-lead hard cap.
It may set `signal_match_mode` (`any` or `all`, default `any`), per-signal
`min_age_days`/`max_age_days`, run ID, and as-of date. Validate that
each signal's lower bound is no greater than its upper bound. Resolve obvious
company identity ambiguity before paid work.
Numeric employee filters use inclusive range semantics: a company passes when
its verified count is between the requested minimum and maximum. Translate that
range to the provider's live field semantics. For discovery, include every
provider bucket that overlaps the requested range. For acceptance, one credible
count or range wholly within the requested bounds suffices. A bucket crossing
a boundary needs further verification, not automatic rejection.

### Review and delivery details

Use the [research loop](../SKILL.md#research-loop) and
[review helper](adapter-io.md#save-a-review). The helper records company decisions,
closes reviewed sources and refreshes totals; do not maintain a second plan or
copy calculated summaries by hand. Preserve historical receipts. With existing
continuation links, close children before parents; reopen the parent before a
child. An unrecoverable receipt stays blocked with the audit gap stated.

Use the [stopping contract](output-contract.md#stopping-check) for final delivery.
Keep unfinished routes open when an actual budget/time limit ends work. A
reviewed company can remain parked while fresh discovery continues; do not
invent another action to satisfy a checklist. Missing evidence never proves
failed fit. New runs have no research deadline unless the user specifies one;
there is no minimum-spend target and no reason to make wasteful calls.

Use `tyche_finish` to write version `2.0` results, the workbook and report through
the [existing exporter](output-contract.md#leadsxlsx-contract). It runs full strict
validation and saved-file checks. Inspect the preview and require explicit
`delivery_allowed: true`; do not assemble reports, rerun export, or copy costs by hand.

## Qualification policy

Keep one qualification policy and the existing outcomes; research models supply
evidence, not a separate acceptance standard. Do not add lead scores or gates.
Work small batches through company checks before broadening
discovery. After a blocked or unproductive stage, record its recovery action and
change source for that gap; do not leave promising qualified accounts untouched
while repeatedly starting new country searches.

- Separate user must-haves from preferences at normalization. Preserve explicit
  constraints, required signals and their windows; do not silently add them.
  Optional intent improves ranking, not eligibility. Record the distinction in
  the report and existing required/preferred qualification checks.
- Verify must-haves. Ordinary workflows may be inferred from sourced business
  facts when a plausible use case is enough for the request. Label the inference
  and its basis in existing evidence/prose; never imply observed pain, intent,
  incumbent tools or manual processes. Likely handling agreements does not
  establish paper signing. A specifically required workflow needs evidence.
  Before acceptance, review required web claims against the relevant source
  body, reusing a saved read when available. Check its actual date and activity
  status against the request; a search summary may omit a qualification or
  future completion date. If the body is inaccessible, use another credible
  source or keep the gap unresolved. Structured provider evidence remains
  eligible without an extra web read. Save the finding once in the existing
  qualification checks. Capture opened passages as `text` with the actual
  `open`/`click`/`find` operation; search summaries belong in `snippet`. Required
  web evidence must reference that saved passage. Reuse it; put interpretation
  in `claim` rather than rewriting the source text.
- One credible source can suffice. Use the same standard for every candidate;
  Resolve material contradictions, not merely overlapping headcount ranges.
  A requested funding stage describes current status: check for a later round,
  acquisition or IPO before treating a historical financing as a stage match.
  Compare announcement dates, not array order or the highest stage label. If
  stage labels and dates conflict, corroborate the chronology; the conflict
  alone does not establish a later financing round.
  Resolve these checks before acceptance and save the result once in the
  existing qualification evidence; conflicting must-haves stay unresolved or
  receive an evidenced rejection.
- Corroborate facts across credible sources for the same identified project.
  A recent announcement or substantive progress update can establish activity
  within the requested window; drawings, specifications or another project
  report can establish its technical requirement. Technical evidence may be
  older when it still applies to that project. Record the project linkage,
  each source's actual date and the fact it supports in existing evidence
  arrays and prose. Never date old technical work as new, combine unrelated
  projects, or substitute general service capability for required recent intent.
  Installed work shows project activity, not an outstanding purchase; preserve
  any explicit request for future demand or a new award.
- Accept supported must-haves, even without optional intent. Keep missing
  must-haves unresolved; reject evidenced mismatches. Recover the specific gap
  through another source or signal within existing limits. A bad signal
  does not reject the company. Do not count unresolved rows as qualified.

## Gates, statuses, and artifacts

`results.json.request` is the authoritative normalized ICP. Check decisions
against it, not an earlier candidate's band or a rewritten interpretation.
Do not turn a service area into an office requirement or a preferred signal
into a requirement. Use the HarvestAPI LinkedIn `employee_range` for company-size
decisions, retaining field evidence. Full containment in `request.icp.company_size`
passes; no overlap fails; partial overlap stays unknown. Member counts and other
providers' estimates do not override LinkedIn's range. Follow the
[field contract](output-contract.md#linkedin-company-size).
Missing intent remains unknown even when size passes.

The account gate requires canonical `company` and `domain`, an evidenced
`account_fit`, and any required signal evidence. Native reviews store signals once
in `qualification_checks`; code derives `signal_evidence`. Apply the qualification policy:
require a current observed signal only when the request requires it. Otherwise
leave unverified signals unknown and explain any inferred use case in
`intent_details`, grounded in sourced business facts. Do not label it as an event.
Preserve URL, date, date basis, text and source for each object's supporting
facts. An observation date dates the business facts, never an inferred event.
Unsupported facts, search-results pages and stale required signals cannot pass. Resolve relative dates from retrieval time
and retain the original wording in the report. When `qualification_checks` is
present, record each criterion as `pass`, `fail`, or `unknown` with its
`required`/`preferred` importance and an evidence array. Reject only an
explicit failure of a required criterion; keep an unknown required criterion
as unresolved so missing evidence does not become a silent false negative.
Save resolved criteria as evidence arrives; do not collapse several known facts
and one missing field into a single unknown `complete_account_fit` check.

Write `reports/<run-id>/report.md`, `reports/<run-id>/results.json`, and
`reports/<run-id>/leads.xlsx`. The report must contain the request, assumptions,
hypotheses, route and evidence receipts, pilot observations, route cost bases,
confirmed and maximum credits, Deepline dollars and cost per accepted company,
statuses, accepted rows, rejected rows, unresolved rows, and stop
reason. For a target shortfall it must also show the full route frontier,
continuation decisions, remaining call capacity, reviewed-company counts, and
the reason each remaining route is exhausted or blocked. Record timing and
accepted-company provenance as specified in the output contract's
[`report.md` and final response section](output-contract.md#reportmd-and-final-response).
Maintain these as work proceeds; do not reconstruct discovery attribution from
the final evidence URL. Do not store
credentials or raw secrets.

Use the output contract's [client workbook format](output-contract.md#leadsxlsx-contract)
for company rows, headers and evidence display. Unverified optional fields stay
blank. Keep full evidence, run status and rejection details in
`results.json` and the report.

This is a small direct-wrapper workflow. It has no `Sourcing_model` or `pp`
runtime dependency, browser harness, server, database, queue, CRM write,
outreach action, required subagent, or hidden API. Do not add one to complete a
run.

### Request coverage

Before `tyche_start`, compare the interpreted request with every original constraint.
Retain independent financial and employee thresholds, exact event-role lists, windows,
exclusions, any/all logic, alternatives and scoped exceptions. Do not substitute a
financial metric the user did not name. Save separate must-haves in
`icp.required_attributes`; signal alternatives do not waive them. Resolve material
ambiguity before paid research. Resume the bound request rather than reinterpreting it.

### Evidence-meaning review

Match the strength of the claim to the evidence. These cases belong to the same
LLM source review, not a separate rule engine:

- **Specific activity:** a link to a collection or a general category description
  does not establish a matching current activity. Use a matching item or an
  explicit statement of that activity, preserving the requested actor and location
  relationship. Inspect linked details only when the current source leaves that
  fact unresolved; do not require a particular source format or add a recency window.
- **Hiring:** a generic careers page, job categories or an empty listings shell
  does not establish a current vacancy. Before acceptance, open the
  matching listing or use explicit current hiring evidence for the requested roles.
  One current vacancy supports a single observed
  opening. It does not establish rapid hiring or a surge. If rapid hiring is
  required, keep that criterion unknown until stronger evidence is found.
- **Metrics:** increased loss totals can reflect catastrophe frequency or exposure,
  not claims-cost inflation. Premium inflation is a different metric. A subsidiary's
  event only qualifies when it meets the request's entity/geography relationship.
  For every numeric company threshold, match the metric, entity, currency/units
  and time period. Member-network sales, customer transaction volume and parent/group
  revenue do not establish the target company's own revenue without evidence of
  the requested scope. Keep a missing company-specific number unknown, not below
  threshold. Apply this review to required company attributes as well as signals
  before acceptance. Optional numbers need no extra qualification check.
- **Repeated hiring:** one posting copied by several aggregators is one
  observation. Repeated-vacancy claims need distinct, dated observations.
- **Geography:** an ambiguous aggregator location does not establish a company
  operation in the requested geography. Use evidence for the location relationship
  the user actually requested, such as headquarters, service area or operations.
- **Expansion:** an announced partnership, conditional approval, planned rollout
  and completed launch are different claims. Preserve the source's status.

Before delivery, follow the final packet from `tyche_finish`: compare required
company fit and material output claims with the saved sources. Preserve reasonable
qualified analysis. Correct optional factual errors without rejecting a qualifying
company. Use `tyche_review` for changed fields, then review the fresh packet. Preserve
unchanged records, receipts and budget. Return its `review_ref` with source-based
`review_findings` only after resolving the affected requirements and claims.
Strict validation and review hashes verify structure and version, not source meaning.


### Parallel company workers

File-backed launcher runs default to two parallel researchers using this same
loop. In a parallel worker, claim the real website domain with `tyche_claim`,
including its verified LinkedIn `company_url` when known, before company-specific research. If another worker owns it,
skip it. Use the returned target throughout qualification.
Work **one company at a time**: find → claim → qualify → confirm, reject, or hold → next company. Resume `parallel.current_company` first after a restart.
Before another claim or broad discovery, confirm the completed company, reject an
evidenced mismatch, or save `hold_account` with the specific missing
evidence and why available routes cannot resolve it. Do not hold just to open
more candidates. A hold retains ownership and evidence for later follow-up;
missing evidence is not rejection. Company-scoped searches can resolve gaps.
Broad discovery uses `target: discovery`; start with your assigned search approach
when there is no current company. Discovery can return many prospects; claim and
process one, then reuse the saved discovery results for the next.
Use `parallel.owned_companies` to resume your own work. All workers share one
budget, deadline and target. Save only your own company/source decisions; the
launcher waits for researchers to exit before a single final review/export.

On `worker_yield`, end immediately without polling. Code reduces concurrency near the shared budget cutoff; finish your current company before yielding. A retired worker's recoverable company may be reassigned with its saved evidence, but never steal live ownership or replay an uncertain paid call.
