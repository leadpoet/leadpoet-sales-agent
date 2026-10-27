# Shared adapter I/O

Use the native tools for normal sourcing. The later CLI sections are diagnostics
for specific tool failures and compatibility with runtimes without native tools.
This is the shared credential, response-file, and recovery contract;
provider-specific inputs and statuses live in [Deepline](deepline-adapter.md)
and [ScrapingDog](scrapingdog-adapter.md). Examples beginning with `.agents/`
run from the repository root.

## Native tools

The launcher binds these tools to the assigned `results.json`; callers supply
research choices, not paths, route IDs or accounting envelopes.

| Tool | Agent input | Code handles |
| --- | --- | --- |
| `tyche_start` | Interpreted `request`, authorized `max_usd` if supplied | Original clock, files, defaults, criteria, ledger, combined cost cutoff; safe resume |
| `tyche_claim` | Website-domain `target`, optional verified LinkedIn `company_url` | Exclusive company ownership and known-alias deduplication across workers; returns the domain target or an ownership conflict |
| `tyche_lookup` | `checks` (1–3): `target`, `phase`, `purpose`, `tool`, `inputs` | Cached live description, schema checks, observed-cost checks, dispatch, receipts |
| `tyche_review` | Changed findings/source reviews and optional `web`, or the current evidence packet's `review_ref` alone | Company updates, receipt-owned LinkedIn fields, criterion labels, derived signal, bookkeeping, confirmed JSON |
| `tyche_inspect` | No arguments, or `target`, `ref`, `field`, `tool`, `query`, `recover` | Compact state, saved request or detail, catalog search, local receipt recovery; `field="requirements"` lists the criteria and their ids; `field="taxonomy"` lists canonical industries and `field="taxonomy.<industry>"` their subindustries |
| `tyche_finish` | No arguments for review; then `review_ref` and research `commentary` | Mechanical preflight, claims beside saved source excerpts, strict export/readback/preview and cost summary |

At start, supply the company criteria only. A request that carries
`requested_roles`, `contact_role_groups`, `contact_fields` or contacts per
company is refused before anything is saved: buyers and contact data belong to
the contact stage (DEMOS), and the caller keeps them.
Save each requested signal's `importance` (`required` or `preferred`) and any
supplied `product_service` with its `description` and `perspective` (`seller`
or `target`). These are the LLM's interpretation of the current request.
The launcher binds the original request file and saves its text once as
`request.original_text`. Compare it with the interpretation before paid research.
For a dollar budget, supply only `max_usd`; code derives provider credits. New
native runs reject aggregate `deepline_credits` and `scrapingdog_credits` inside
`request.budget`. Only for an explicit user credit limit, use
`provider_credit_limits: {"deepline": 10}`; `{"scrapingdog": 0}` disables that
provider. Saved requests and ledgers keep their original limits on resume.
Each requested `company_types`, `industries` and `geographies` filter needs a
passing required check. Put additional must-haves in `icp.required_attributes`;
do not repeat filters there. Alternatives within one filter share one judgment;
preserve the original geographic scope.

`target` is the canonical company domain or `discovery`. Phases are
`account_discovery` and `account_verification`. A tool that finds or returns
people, email or phone is refused before any call (see
[the stage boundary](output-contract.md#stage-boundary)). `provider` defaults to
Deepline. For ScrapingDog, pass `provider: "scrapingdog"` and its wrapper input
in `inputs`. `approach` may name a stable strategy. New runs use reported
charges, so omit `max_cost_credits`. ScrapingDog needs its plan conversion at
start. Code never chooses a different provider, criterion or qualification
judgment. No tool is mandatory: when one provider tool is unavailable, choose
another source.

Lookup returns a route and result references such as `lookup-abc:0`. Inspect a
route to see its response status/results, or select a result and `field` for
more detail. Page a saved route with `offset`/`limit` and its returned `next_offset`;
long text also returns `next_offset`. `tyche_inspect(tool=...)` returns cached
native inputs, constraints/pricing and output field names. Long help and enum
lists have explicit detail paths; select those with `field` and page with
`offset`/`limit`. Code validates against the complete saved contract. Learn a
selected tool once and reuse its description. Catalog searches show usable tool
summaries first; explicitly non-callable entries stay in the saved receipt. Use
short provider or capability terms when a search has no callable matches.
Use `field` for a specific nested tool, company or run field; `refresh: true` is
only for a confirmed schema, pricing or access change.

Company decisions: `hold_account` (a required fact is unknown), `reject` (an
evidenced required mismatch or an exclusion) and `accept` (every required
criterion is supported). Example review input, with references selected from
actual results:

```json
{"companies":[{"target":"example.com","decision":"hold_account",
  "reason":"Current funding stage still needs evidence",
  "company":{"ref":"lookup-company:0","industry":"<verified taxonomy parent>","sub_industry":"<verified taxonomy child>"},
  "account_fit":{"ref":"lookup-source:0","fit_claim":"<supported match to the requested business activity>"},
  "qualification_checks":[{"criterion_id":"icp:industries","status":"pass",
    "claim":"<what the source establishes>","evidence":[{"ref":"lookup-source:0"}]}]}],
 "sources":[{"ref":"lookup-source","state":"exhausted","reason":"Reviewed the complete product page"}]}
```

Progress's `review_due` and finish's `pending_sources` come from the same open
saved lookups, including discovery. Use `tyche_inspect(field="pending_sources")`
with `offset`/`limit` to page them. Review the existing receipts; do not repeat a
lookup just to record a decision. Closed aliases do not create review reminders.
Use `sources: [{"refs": [...], "state": "exhausted", "reason": "..."}]` when
several saved lookups share one actual review decision. Unselected sources stay
open. A selected single-result company getter or opened page closes with its
evidence review; search results and pagination still require an explicit
decision. Reuse a returned final review packet until findings change. Repeated
finish calls return `unchanged: true` with the same `review_ref`, without
duplicating the packet or approving it.

Each qualification check selects one saved criterion by `criterion_id`
(`attribute:N`, `icp:<field>`, `signal:N` or `exclusion:N` from
`inspect(field="requirements")`) and states `status` (`pass`, `fail` or
`unknown`), the `claim` that gives the reason for that status, and `evidence`.
Code copies the criterion label, importance and signal kind; supplying them is
an input error. Facts that answer no requested criterion belong in
`supporting_findings`. A check replaces the saved check with the same id;
other checks stay unchanged. Evidence refs expand into saved source, URL, date
and text: usually use `{"ref":"..."}` and put the judgment in `claim`. Put
multiple supporting sources in one check's evidence array. Supply reviewed
`text`, `date` and `date_basis` only when interpreting an event, and supply
`event_date` (YYYY, YYYY-MM or YYYY-MM-DD) for each dated signal. Aviato funding
refs keep the supplied announcement date and round name; for a requested company
attribute a funding ref can use its verified, company-bound receipt without a
public URL; explain the stage judgment in `claim`. Filters and signals still need
a source URL. Compatible `sources` decisions for different results in one lookup
are combined; conflicting states need one explicit decision for that lookup.

Code derives `signal_evidence` from the first passing check of a required signal
kind; never author it. Optional `supporting_findings` add other ICP-relevant
facts: `[{kind: "signal"|"context", label, claim, evidence: [{ref, event_date?}]}]`.
Quote the supporting passage as evidence text, at most 1,000 characters copied
exactly as captured; a whole page is refused. Omit text only when the saved
passage or a structured record's own text is the evidence; a record without a
description is quoted by one of its field values. They appear in Signals and Sources but never
satisfy or change a requested criterion. Use context for business background and
preserve timing and status for actual activity. Supplying the array replaces it;
omit it to keep saved findings. Errors after a web observation was saved return
its reusable reference; correct the judgment without rewriting the observation.

A company review reuses an unambiguous saved LinkedIn company getter with the
exact target domain. Identical repeated getters reuse one selection; conflicting
identities or field values need an explicit company `ref`. With that `ref`, omit
receipt-owned names, URLs, the employee range and its evidence: code supplies
them. Add industry, subindustry and the two-sentence description as reviewed
facts. A requested company size is its own reviewed check: quote whose workforce
the figure counts.

For built-in web tools, execute the chosen search/read, then send its observed
`status` and `results` with `target`, `purpose`, `query` and `operation` under
`web` in the review call. Use `web:<observation index>:<result index>` across the
entire call: the first page of the second observation is `web:1:0`, even for a
different company. Use `web:1` for that whole observation in `sources`. Reuse
shared saved sources by their returned lookup reference. The tool records this
observation; it cannot invoke or independently capture the built-in browser.
Replaying the same observation reuses its receipt. A new observation of the same
URL with different text gets a distinct receipt; the earlier one stays unchanged.

`recover` never redispatches: it finishes recording a saved normalized receipt.
An outcome marked `recorded: true` is already saved; repeated recovery cannot
settle unknown billing. At finish, code matches billing by saved request ID,
provider and catalog-backed operation aliases. It accepts posted charges and
explicit free outcomes; missing billing is never zero. A returned result billed
as a miss or zero units is recorded as observed billing with an issue, while its
charge stays pending. `inspect(field="costs")` and the saved report separate
billed USD and pending call counts. Local reconciliation first reads each
call's own final charge by its saved ID, or by the billing ID Deepline returns
for its saved execution key, then uses individual posted
credit-ledger debits before the usage feed's free records or final failed records. Grouped
usage totals are never split or counted beside individual debits. New response
billing settles only with explicit final pricing; queued ledger posting does not
invalidate a final price. Unknown and estimated prices stay pending.

Billing reads use a 30-second timeout and at most three attempts per saved call
set, including across restarts. A failed read gets one immediate retry; pending
billing can be rechecked after 60 seconds or at final approval within that same
limit. `billing_reconciliation.py results.json --resume` explicitly permits three
more read-only attempts without resetting spend. Pagination continues from its
saved ledger cursor or usage offset when an attempt reaches its four-page-per-feed
limit; API reads first check the newest page for delayed postings. All feeds
share the same 30-second deadline. Repeated pages and invalid offsets leave
unmatched charges pending. A transport outage can fall back to the other billing
feed; malformed records and changed organizations cannot. No paid research is
repeated, and original receipts and caps are preserved. If only a pending or raw
response survived, keep the pending charge and reconcile it locally through
diagnostics. Never retry an uncertain paid call.

`completion_candidates` lists held companies with their named required gaps.
Prefer completing these when affordable. A blocker lets the researcher choose
different work; this advice neither reserves funds nor dispatches calls.

The native relay reconnects a confirmed-dead sandboxed child at most once per
invocation, with identical sandbox policy, checkout and run. Only pure saved-state
inspection is replayed automatically. After a lost lookup/review response, inspect
the saved run and recover receipts; do not resubmit uncertain paid work. A second
connection failure returns a clear operational block with its captured exit code.

Accept as soon as the saved evidence answers the request; there is no extra
research or narrative step. After `decision: "accept"`, `tyche_review` returns
`review_required` with `review_scope: "confirmed_leads"` for the newly accepted
or changed companies. Follow its evidence and writing instructions immediately,
then call `tyche_review` with the current `review_ref` and `review_findings`,
separately from edits. Each finding is `{target, source_refs, finding}`: one
brief factual comparison per company, citing its saved passages and covering
identity, required fit, the initial signal, Signals, source grounding and
client-field QA. Approval returns `confirmed_leads_saved` and atomically updates
`leads.json`. Correct unsupported findings through ordinary review first;
changes need a fresh reference. New lookups return the pending packet without
dispatch until these companies are reviewed or held. Already running lookups may
finish normally. Unchanged confirmed companies need no further incremental review.

`tyche_inspect` includes the confirmed file path, count and pending review
domains. On resume, continue from that state; an approval retry does not repeat
lookups or duplicate rows. The file starts empty and keeps confirmed companies
through later research failures. It excludes changed and withdrawn records. It
does not lower `target_count`, authorize stopping, or replace the final review
and workbook checks. See the [JSON contract](output-contract.md#leadsjson-confirmed-companies).
The diagnostic review CLI saves findings; use the native tool for evidence approval.

`tyche_finish` returns `needs_research`, `review_required`, `needs_repair` or
validated artifact paths. Mechanical errors and pending source reviews appear
before a review reference is issued. On `review_required`, compare each company's
claims with its adjacent `sources` excerpts, then review dates, meaning and
required fit before passing `review_ref` and `review_findings`. Correct
unsupported optional facts without discarding an otherwise qualified company;
reasonable qualified analysis needs no direct citation for every inference.
`inspect(target=..., field="evidence_review")` gives the same view during
research. Changes invalidate that reference: correct named errors through
`tyche_review` and finish again. The approval and company-specific findings are
saved for that exact state. Code checks coverage and source attribution, not
semantic truth. If export is interrupted, code can retry it once when the worker
ends without repeating research or approving an unreviewed snapshot.

`capture_method` distinguishes adapter-saved provider responses from
`agent_recorded_web` discovery notes. Passing web qualifications require an
existing page-reader lookup (ScrapingDog `scrape` or a priced or free Deepline
reader such as `contextdev_get_web_scrape_markdown`), not a model-transcribed
passage. Launcher runs refuse a paid reader without a documented price. Reuse that receipt across checks.
Quotes must occur in the captured body. Source dates and their basis come from
captured metadata and cannot be overridden. Undated pages stay observations;
`event_date` is a separate judgment from the body, preserving month or year
precision. The final reviewer still judges meaning and planned versus completed
status against the original requirement.

The success response includes a concise cost summary; final model usage is
refreshed by the launcher after exit. Use `inspect(field="costs")` for saved costs.

## Diagnostic CLI

Use `-` to supply JSON directly on stdin, normally with a quoted heredoc as
shown below. The helper saves durable records; routine lookups and reviews
do not need temporary input files. Correct the named field in an error before
reading implementation code.

## Start or resume

The LLM interprets the ICP and signals. Put that
[input request](output-contract.md#input-contract) under `request` in a setup
object; supply it as UTF-8 JSON on stdin with `--start-file -`:

```bash
python3 .agents/skills/lead-sourcing/scripts/run_attempt.py \
  reports/<run-id>/results.json --start-file -
```

Optional setup fields are `max_usd`, `scrapingdog_usd_per_credit` and `started_at`.
The helper supplies defaults, the run ID, original clock, criteria, empty result
records and ledger. It validates before writing and keeps existing criteria,
evidence, pending calls and spending on an identical retry. Interrupted writes
keep the original initialization settings. A saved run whose ledger is missing
is refused; it is never rebuilt. A leftover lock still requires checking that
its writer has stopped; locks are never expired automatically.

Use `--status` to resume an existing run without resubmitting its request. Do
not manually construct result bookkeeping or run `budget_guard.py` afterward.

## One attempt

Use `--lookup-file -` with one research lookup or an array of up to
three independent lookups. This composes the existing wrappers, budget guard
and recorder; it does not choose a research strategy. For free catalog search:

```bash
python3 .agents/skills/lead-sourcing/scripts/run_attempt.py \
  reports/<run-id>/results.json --lookup-file - <<'JSON'
{"request": {"operation": "search", "query": "multilingual product-page research"}}
JSON
```

For a chosen provider tool, supply `scope` (canonical company domain or
`discovery`), `phase`, `purpose`, `approach` and `request`. `provider` defaults
to `deepline`; `scrapingdog` and `public_web` use their existing wrapper inputs.
For example:

```json
{
  "scope": "example.com",
  "phase": "account_verification",
  "purpose": "Check the current business and funding stage",
  "approach": "current-company-profile",
  "request": {"operation": "execute", "tool": "<live-described-tool>", "payload": {}}
}
```

The tool name and empty payload above are placeholders, not dispatchable inputs.
Use a live-described tool and its native payload. Native tools and all
`run_attempt.py` lookup CLI formats check the saved same-run description for
availability and validate the full JSON Schema, including nested fields, enums
and array items, before planning or dispatching paid work. Invalid inputs return
the field path and constraint for correction; they create no paid reservation.
Embedded schema references are supported; external references are not fetched.
Field-only descriptions keep their required-field and type checks. Reuse
descriptions until schema or access changes. Unknown catalog pricing does not
block new runs; unknown actual billing pauses further paid work.

Code generates route IDs, fingerprints, receipt paths, paid-call flags and
`spend` metadata. Catalog reads receive their own scope and phase automatically.
`review_due` returns a count and up to three company scopes with completed
research awaiting review. It is a reminder, not another qualification rule.

Use stable `approach` labels describing the source family and search or evidence
strategy, not tool names, batch numbers or cosmetic rewordings. The helper hashes
the actual request, so changing a route ID or approach label cannot repeat a
possibly billed request. Two comparable research attempts within the same scope
and phase without new verified milestones require a changed approach. Progress
at another company does not reset that company's research, and advancing to
another phase stays eligible; finishing one source does not exhaust the company.
Catalog reads do not count as progress.

The helper saves `receipts/<action-id>.json` before updating run state. A crash
leaves the route pending and keeps pending accounting. Resume a saved normalized
response with `--complete <action-id>`; this only records it, never dispatches.
Ledgers and helper receipts carry a fingerprint of the canonical results path.
Resume in place: rewriting paths in a copied ledger or receipt does not make it
belong to another run. Missing or mismatched identities require origin and
billing reconciliation; never erase them, reset spend, or rerun uncertain calls.
The helper derives summary and review counts and cost totals from saved outcomes
and receipts; unknown charges keep provider capacity unknown. Attempt recording
never marks the frontier complete; the export command prepares completion after
review. Each saved response also keeps the action and redacted input in
`attempt`, so recovering a damaged draft does not require inventing labels. If
only raw response bytes survived, normalize that receipt locally first. A
pending or unknown remote outcome is not permission to retry. Async research jobs
need their documented result-retrieval call, not another job submission. For a
described **free job-status getter only**, mark its action `status_read: true`
with a zero cost bound. Repeated reads of the same job are allowed only after a
saved `partial` status response with a zero cost bound; pending transport,
failures and job submissions stay protected. Respect the provider's polling
interval and read limit; never label a submission as a status read. These calls
still use the guarded ledger; missing actual charges stay unknown, even when a
catalog quote is zero.

For built-in public-web tools, plan a single discovery pilot as an object:

```bash
python3 .agents/skills/lead-sourcing/scripts/run_attempt.py \
  reports/<run-id>/results.json --lookup-file - --plan-only <<'JSON'
{"provider":"public_web","scope":"discovery","phase":"account_discovery",
 "purpose":"Find announcements matching requested signals","approach":"official-announcements",
 "request":{"operation":"search_query","query":"<query derived from the current ICP>"}}
JSON
```

Code supplies zero cost and calls and the route ID. Execute the planned search or
read through the available web tool. Include its observed response in the route's
`response` when [saving your review](#save-a-review), together with company
findings. Supply observed status and results only; receipt metadata comes from
code. `operation` is optional but must match the plan; `error` may describe a
failure. Never label a failure `no_results` or a pending outcome complete. Saving
a review does not invoke a browser or provider.

The attempt CLI prints normalized provider results once, with the full receipt
path. Company getter rows show company facts and missing fields; `omitted_fields`
identifies additional saved data. Reopen a receipt with
`run_attempt.py <results.json> --receipt <route-id>` to reuse this compact view
without dispatching or changing state. The view checks run ownership and the
saved route's request and provider identity, including pending receipts;
mismatches require origin reconciliation. Inspect specific raw fields only for a
missing fact or contradiction; full receipts stay saved.

## Save a review

Save each company decision when made, including supported rejections and
unresolved gaps. Refine the record as new facts arrive. A `companies` item
identifies `scope` and only the fields being updated. One call can also save
the observed web response and close its reviewed route:

```bash
python3 .agents/skills/lead-sourcing/scripts/run_attempt.py \
  reports/<run-id>/results.json --review-file - <<'JSON'
{
  "companies": [{
    "scope": "example.com",
    "state": "unresolved",
    "company": {"canonical_name": "Example"},
    "reason_text": "The business fits; the announcement date remains unknown.",
    "qualification_checks": [{
      "criterion_id": "signal:0", "status": "unknown",
      "claim": "The announcement has no verified date yet.", "evidence": []
    }]
  }],
  "routes": [{"route_id": "<returned-route-id>",
    "response": {"status":"ok","results":[{"url":"https://example.com/news","text":"Observed announcement text without a date."}]},
    "reason": "Reviewed the announcement; its date is not established."}]
}
JSON
```

`company` updates the supplied factual fields. `qualification_checks` updates one
judgment per `criterion_id`: supply status, claim and the selected current
evidence, which replaces the previous selection; other checks and the original
receipts stay unchanged. Duplicate criterion updates are refused. Code copies the
criterion label, importance and signal kind, derives `signal_evidence` and the
`reason_code`, and does not decide whether the company fits. Every save goes
through this one update path; a complete saved row cannot be supplied instead.

`account_fit` and `supporting_findings` are complete replacements when supplied,
and untouched when omitted. State defaults to the saved state (new companies
start unresolved); set `state: "accepted"` or `"rejected"` explicitly. Acceptance
keeps every evidence gate; a missing fact cannot become a rejection without an
evidenced required mismatch.

`response` is optional and only for observed public-web results; provider
responses are already saved by their lookup. All attached responses are checked
before any are written. Responses persist before the company and route review:
if that review fails or is interrupted, correct and resubmit the same review.
Already saved responses stay immutable; never repeat research or a paid call to
repair a save. `companies`, `routes` and `next_actions` are optional; omitted
records stay intact. Execute your next choice with `--lookup-file -`; supply
`next_actions` only for concrete work that needs saving.

One atomic update saves the selected company rows, closes reviewed routes,
retires their completed actions, removes speculative follow-ups for reviewed
parked or terminal companies, and refreshes counts and cost summaries. Receipts,
ledger charges and the saved request stay intact. The CLI response includes
`request_file` and the next decision, without repeating the full request. Use the
returned next decision for the next batch; reopen receipts only for a missing
fact or contradiction. `--status` includes the authoritative request for setup
and resume, without a write.

Routes default to `exhausted`; the helper derives the exhaustion basis from the
saved receipt. Use `state: "continuable"` for genuinely unfinished work, or
`"blocked"` for an evidenced provider failure. Existing continuation links are
kept; supply `continuation_route_ids` only to add actual links. Never close a
pending or unknown response. The helper refuses missing receipts, failed calls
presented as exhausted, and a LinkedIn range that contradicts its receipt. It
does not infer source credibility or qualify companies for you. Full strict
output validation still checks delivery.

### Compatibility and recovery

File paths remain supported instead of `-`. The `--input-file` action/request
envelope and `--batch-files` remain for scripted attempts; use the concise forms
above for new work. `--complete <route-id>` recovers an already saved response
without dispatch; `--complete <route-id> --response-file -` can attach an observed
web response alone. Neither recovery path can replace an existing response or
change its run identity.

## Concurrent company checks

After the pilot, use one agent and up to three ready account-verification checks,
including distinct known-input checks for one company. No check may depend on a
sibling response. Use fewer when fewer checks are ready; do not wait to fill a batch. Supply the
same concise lookup objects above as an array on stdin:

```bash
python3 .agents/skills/lead-sourcing/scripts/run_attempt.py \
  reports/<run-id>/results.json --lookup-file -
```

All paths use the same execution, independence and budget checks. The helper
validates every input with the provider validators before planning or spending.
Malformed fields identify their batch item and stop the entire batch without
changing run state.

Give each lookup its canonical company domain as `scope`; code assigns IDs. Free
catalog lookups may share discovery scope in a batch. Keep substantive discovery
pilots on the single-attempt path. Choose only independent work, deduplicate
aliases and owner groups first, and do not run redundant provider requests for
the same company at once.

The helper plans serially, runs up to three provider calls concurrently, and
records results serially. Each call has its own receipt. Dispatch identities and
settlements share the ledger and are serialized. New runs check known spending
before dispatch; already running calls can finish above the threshold.
Completed calls without billing pause new paid work. After input validation, a
member refused by eligibility or budget checks or a failed provider call does not
discard successful siblings. The batch returns all outcomes and exits nonzero if
any member fails; recover saved receipts with `--complete`, never rerun the whole
batch or retry an uncertain billed request. Single attempts follow the same rule:
`ok`, `partial` and `no_results` are successful provider outcomes; provider
failures return nonzero even when the adapter returned JSON. A successful attempt
still requires evidence review and full delivery validation. The batch returns
one final `stop_decision`; use it without a separate status or stop-check command.

For built-in public-web checks, add `--plan-only` to prepare up to three receipts,
then execute those independent searches or reads together through the available
tool's parallel-call facility. Python only plans and records this work; it does
not invoke the built-in browser. Include observed responses in one
`--review-file -` call with company findings and source reviews. Wait for the
batch to finish, then save its decisions and use the returned next step. Batch
completion alone does not qualify companies. Use one check when only one is ready
or a provider requires serial access; a rate limit is a reason to reduce
concurrency, never to increase retries.

## Paid-call budget

New runs use the [actual-cost policy](provider-pricing.md). `max_usd` defaults
to USD 0.80 per requested company and covers reported provider charges plus the
local launcher's estimated base LLM usage. The ledger records each request
identity before dispatch. ScrapingDog reserves its documented tariff ceiling;
other provider calls use the actual-cost cutoff. After the observed total reaches
the threshold, new paid work stops. Already running calls may overshoot it.

Do not supply `max_cost_credits`. Missing billing stays pending and pauses paid
work until reconciled. Catalog prices help select tools but do not become
reported charges. Explicit zero provider allocations still disable a provider.
ScrapingDog requires the saved plan's USD conversion. Optional provider and
per-company thresholds use observed spend.

The native helpers keep the original request, limits, receipts and unique call
IDs across restarts. Never reset the ledger or redispatch an uncertain call. An
explicit `reserved` run keeps its reservation rules. A lock conflict still fails
without sending. All Deepline execute rows are saved; lookup and inspect paginate
their display, and display limits do not limit provider billing. In a `reserved`
run, a reservation override can increase a supported whole-call bound but cannot
establish an unknown price. Never expire a lock automatically: inspect the ledger
and receipts and confirm no writer remains before removing a stale lock.

## Response files

Both adapters accept `--output-file <new-path>`. Use a distinct path per route
in the run's receipt directory. The parent directory must already exist.
The adapter checks the destination before dispatch and refuses existing files.
It atomically saves the full redacted provider body before normalization, then
adds the normalized result. Stdout remains the single JSON response. The saved
`provider_response` includes Deepline exit code, body and stderr or ScrapingDog
HTTP status and body; candidate output limits do not truncate this copy.
Transport size and timeout bounds still apply. Non-finite or malformed JSON is
kept as redacted diagnostic text, never promoted to results. Non-finite input is
rejected before dispatch. Available partial Deepline stdout and stderr are kept
after a timeout; that is not a successful provider result and never triggers a
retry. Interrupted ScrapingDog responses keep available bytes with
`incomplete: true`; neither broken chunks nor a short declared body is a
successful empty result. JSON output escapes non-ASCII text so receipts and stdout
also work with narrow terminal encodings. Invalid UTF-8 CLI bytes are kept as
escaped diagnostic text and yield a response error, not candidates.

`receipt_status` is `pending`, `response_received`, or `complete`; these describe
file processing, not provider success or billing. A local input failure has
`error_stage: request`; an unrecognized response has `error_stage: response`.
An explicit remote schema error may have `error_stage: provider`. Do not assume
every schema error means the request payload was wrong. Native lookup checks
the saved contract and cost before dispatch. Use its exact field errors to
correct inputs; inspect only the missing constraint. Refresh a descriptor when
there is evidence it changed. The adapter does not implement every provider's
schema, so provider-specific response errors may still need investigation.

A failed final save returns a nonzero exit code and `receipt_error` while
keeping the normalized stdout and any previously saved raw response. Check those
artifacts and billing before recovery; never rerun a possibly paid call merely to
recreate a file. File output does not spend credits or retry providers.

```bash
python3 .agents/skills/lead-sourcing/scripts/deepline.py \
  --input '{"operation":"search","query":"small textile wholesalers"}' \
  --output-file 'reports/<run-id>/receipts/catalog-1.json'
```

## Credentials and artifacts

Use the environment or connected credential store. Record only availability,
never a value:

- `DEEPLINE_API_KEY` and optional `DEEPLINE_HOST_URL`.
- `DEEPLINE_BIN` (optional path to the Deepline CLI binary).
- `SCRAPINGDOG_API_KEY`.

For each run, deliver `reports/<run-id>/report.md`,
`reports/<run-id>/results.json`, and `reports/<run-id>/leads.xlsx`. Include the
request and criteria, hypotheses, route commands and filters, pilot observations,
statuses, route cost bases, confirmed and maximum credits, Deepline dollar cost
and cost per accepted company, accepted evidence, and rejected or unresolved rows
with stable reasons. Keep provider receipts separate from output state. Never
infer evidence from memory or present an unverified company as final.
