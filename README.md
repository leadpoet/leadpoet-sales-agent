# Evidence-first company sourcing

`harness.run_icp(icp)` accepts an ICP dictionary and returns a JSON-compatible
list of up to five companies. An empty list is valid. Credentials and execution
settings come from the environment; provider access uses the host transport.
No provider responses or private data are included in this source bundle.

The default path extracts and ranks candidates from all retained search text,
then assesses cached evidence in batches of six before confirming companies.
Each assessment covers the required event and date, product/industry, country,
funding stage and employees. Only grounded, in-window primary events qualify
for paid confirmation. Cached conflicts are rejected first.

Confirmation prioritizes stage screening before homepage identity and requests
a company profile only when the employee bucket remains unknown. Existing
bounded identity redirects remain available. Venture discovery uses four event
queries and three stage queries, each requesting eight results and 3,000 text
characters. Original company newsroom evidence is preferred when cached; a
funding announcement alone does not prove another event category. No separate
newsroom discovery is added during the cache-only phase.

Extraction and cached phase-A assessment use `google/gemini-3.1-flash-lite`,
with bounded retries and `openai/gpt-5.6-luna` fallback. Per-company final
assessment retains the configured model. Extraction preserves its structured
JSON contract and retries incomplete output before falling back. The active
path has a 138-second planning deadline, a 29-call Deepline ceiling, and an
OpenRouter settled target of $0.225 with a $0.45 transient reservation cap.
The extraction change targets $0.05 OpenRouter per ICP; it is not a guarantee.
After six discovery searches, twenty extracted candidates stop further
discovery. Fewer candidates permit the seventh search; only new cached rows
are then extracted. Confirmation reserves ten identity calls and up to three
workforce-proof calls; candidates without either a category primary or affirmed
stage receive no lookup calls. A P1 workforce page skips LinkedIn size checks.

`AGENT_RULE_V20R10_COST`, `AGENT_RULE_V20R10_EVIDENCE`, and
`AGENT_RULE_V20R10_HIRING` default to enabled. The output's one-URL-per-signal
schema represents corroboration as at most three entries with criterion index
zero. Only pages repeating the same selected event sentence are combined;
these are evidence for one event, not additional event breadth. Hiring evidence
excludes LinkedIn jobs and closed/expired postings, and requires explicit open
hiring. Prefer company careers, ATS and press sources. Cost diagnostics report
settled/reserved dollars and returned-based allowance estimates separately from
independently verified qualifications.

`AGENT_RULE_V20_TWO_PHASE`, `AGENT_RULE_V20_CHEAP_FIRST`, and
`AGENT_RULE_V20_STAGE_SUPPLY` default to enabled. Setting each to `0` disables
that change. Existing `AGENT_RULE_V12_*` through `AGENT_RULE_V19_*` controls
remain available. The fallback legacy path retains its own bounded execution.

Funnel diagnostics report `phase_a.assessed`, `phase_b.started`,
`phase_b.admitted`, and `not_started_before_budget_stop`. Incomplete model
verdicts do not count as assessed. A grounded primary still awaiting confirmation
counts as unfinished when the slot goal has not been reached.

## Workforce evidence and provability

`AGENT_RULE_V21_SIZE_EVIDENCE`, `AGENT_RULE_V21_LINKEDIN_CHECK`, and
`AGENT_RULE_V21_PROVABLE_FIRST` default to enabled. After admission checks,
company-attributed employee evidence is taken from cache or from open-web
search followed by a provider-domain search. Both request five results and
3,000 text characters. The allowance is at most two searches per company and
six searches per ICP, within the existing 29 physical Deepline calls.
Department, people, email-format and organization-chart pages are rejected,
as are another entity's counts, uncertain numerical bounds and LinkedIn hints.

A non-LinkedIn headcount source is submitted first in `fit_evidence_urls`,
followed by the homepage and the primary event. The claimed employee bucket
comes from that cited count. Without it, a company slug observed in a served
homepage anchor permits one LinkedIn contents lookup, capped at 4,000 characters.
Only the literal Company-size line can support this fallback; unobserved slugs
are never submitted. Optional lookups leave three seconds for retaining the
already-admissible fallback company.

Slots rank by non-LinkedIn proof (+3), an anchored LinkedIn Company-size line
(+2), and an affirmed cached stage (+1), with the phase-A score breaking ties.
A full set of unprovable slots may be replaced by later provable admissions
until the existing time/call bounds or five provable slots stop confirmation.
Known out-of-band employee evidence rejects the candidate rather than retaining
an unsupported size prior. Returned-company traces include evidence source,
host and quotation, anchor/size-line status, supported bucket and rank.

### R2 OpenRouter admission and cache assessment

`V20R2_EXPECTED_COST` and `V20R2_CHEAP_PHASE_A` default ON through `enabled()`.
OpenRouter admission uses settled micro-USD plus fixed pending expectations and
this request's expected cost, capped at 225,000. The learned ratio is completed
settled / completed maximum reservations, initially 0.35 and clamped to
[0.20, 1.00]. Settled plus the new request maximum must not exceed 450,000.
At 225,000 settled, no further OpenRouter request starts. Actual overspend,
missing billing or a charge above its maximum is an explicit overrun, not a
successful cost gate. Deepline physical-call limits and tool ceilings are unchanged.

Extraction stays on the configured model (sol in the R2 experiment). Cache-only
phase A starts on `google/gemini-3.1-flash-lite`, with initial output tokens
`150 * batch + 200`. JSON truncation has one recovery at 4,096 tokens, then
`LLMTruncated`. Non-OpenAI models receive no `reasoning_effort`. Gemini maximum
reservations include completion plus internal reasoning, each 1.50 micro-USD
per output token, and prompt input at 0.25 micro-USD per bounded input token.

Top-level OpenRouter error envelopes settle locally at zero, with code/type and
`provider_error` telemetry. `provider_error_reserved_microusd` records the full
reservation the validator would debit; it is informational locally. A transport
timeout is instead conservatively charged at its full reservation and recorded
separately. Only a real completion missing usage is unreported completion billing.

Each phase A call is capped at 30 seconds, preserving 20 seconds for phase B.
Timeouts and in-body errors get one retry after two seconds on the same model,
then a sticky fallback to `openai/gpt-5.6-luna`. If luna also exhausts its retry,
phase A stops and phase B uses previously assessed candidates. There is no
phase A sol or deepseek fallback. Physical-call, deadline and money guards
apply to all retries. `money.funnel` includes timeout reservation totals,
expected ratio, refusal reasons and extraction/phase-A/other costs.

R3 stage supply (`V20R3_STAGE_SUPPLY`, default ON) screens phase-A eligible
candidates in score order before identity/P1 confirmation. Stop at five affirmed
requested stages; at most 12 stage searches per ICP and ten of the 29 physical
Deepline calls remain reserved for confirmation. Seed/Series A queries explicitly
search seed, pre-seed and Series A completed raises with 2,000-character text.
News and company newsrooms remain eligible under the unchanged v19 affirmation
and announcement-source requirements; speculative rounds still fail.

R4 profile headcounts (`V20R4_HEADCOUNT_HINT`, default ON) are estimates.
Sentinels 10, 0, 1 and missing values, or a count shared by at least three
distinct profiles in the run, are unknown and supply neither a veto nor a
claimed bucket. Confirmation profiles are gathered before size decisions so
the first two repeated values are also handled as unknown, without refetching.
Other profile values only conflict below half the lowest allowed lower bound
or above twice the highest allowed upper bound. Cached headcount conflicts
use the same margin; public P1 evidence retains its own strict validation.
Claims prefer P1 evidence, then the nearest allowed bucket to a usable hint,
then the v18 prior. Raw values and decisions are traced as headcount.hint;
headcount_conflict_margin_saved counts unique candidates rescued from the old
bucket conflict. Collection order changes, but call and money caps are retained.

## Category support and stage uncertainty

`AGENT_RULE_V20R11_CATEGORY` restores category decisions from grounded model
verdicts, including distribution/new availability of an offering. Company
subject checks and non-funding primary headline checks remain mandatory.
`AGENT_RULE_V20R11_T1_RECENCY` retains an already stage-affirmed T1 candidate
when the recent-round search fails, is unavailable or cannot fit the lookup
budget. Only a proven subsequent stage or PE/IPO contradicts its cached stage;
uncertainty does not erase the existing fact. Other tiers retain their recency
checks. Both flags default on; R10 cost, evidence and hiring controls remain.

## Evidence hints and primary validation

V23 defaults on through `AGENT_RULE_V23_SIZE_HINTS`, `AGENT_RULE_V23_IDENTITY`,
`AGENT_RULE_V23_PRIMARY` and `AGENT_RULE_V23_EARLY_SUPPLY`. Workforce searches
prefer public company-profile sources within the existing query limits. A
company LinkedIn URL comes only from a unique homepage anchor. Missing plain
HTTP status or final URL, non-200 responses and cross-domain redirects rank
below clean identity observations. Capital-raising and company-renaming
sentences cannot substitute for a non-funding primary event. Series A searches
combine the stage with the event, and sales/marketing hiring searches describe
the employer's software business. No additional provider-call lanes are added.
