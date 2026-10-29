# Provider costs and the run cutoff

New runs use `budget.policy: actual_cost` and a version 2 execution ledger.
The local launcher counts provider charges plus estimated base LLM
cost from individual response usage, including retries and compaction.

A paid call with a documented whole-call price is admitted, under the ledger
lock, only when recorded spend, the documented price bounds of calls still in
flight or unbilled, and this call's own bound fit under the budget. Spend on
priced calls alone therefore cannot pass the budget, even with several in flight
or after a crash. Settling replaces a bound with the exact charge, so a free miss
releases its bound, and for admission an unknown bill keeps its bound instead of
counting as zero. The ledger keeps the bound as `admission_bound_credits`. It
gates admission only: reservations, routes, totals and stop reasons count what
was billed. When no proposed paid call fits the remaining headroom the run ends
as `budget_exhausted`, which can be slightly before recorded spend reaches the
budget, so set the budget above expected spend by about the dearest call.

A call with a documented bound above zero that returns with no billing id (a
timeout before any response, or a reply without a request or job id) can never
be matched to a bill. It is counted at its documented bound as an unknown
charge: the ledger holds it as `held_credits` with `unknown_charge`, its route
records `billing_basis: unknown_charge_at_bound`, and cost reports list it with
the other documented holds. It counts toward the cap like any bill, but it no
longer leaves billing pending, so it never stops later paid calls or the
delivery. It is never sent again. A call that returned an id still waits for its
bill, and a call with no documented bound keeps waiting for billing evidence.
Direct API calls (except Firecrawl batch submissions) save an execution key in
the ledger before dispatch and send it as `Idempotency-Key`. Reconciliation can
later ask Deepline which billing request that key started and settle the held
call from that request's final charge; until then the hold stays. Not every tool
takes a key: Deepline answers such a call HTTP 422 `IDEMPOTENCY_NOT_SUPPORTED`.
Its recovery docs say a keyed launch-only request is rejected before provider
dispatch, and that the reply names the alternative to follow before submitting
work. The adapter then sends the same request once more without the key, within
the call's own time, as every call was sent before keys. The receipt keeps the
refusal under `execution_key_refused`, and reconciliation moves the ledger's key
to `refused_idempotency_key`, so no lookup asks Deepline about it. Deepline's
record of a refused key keeps a request ID with no billing record, so a key
lookup that finds the refusal is ignored too. A reply that states another code,
a charge, a billing ID, output or other errors is not such a refusal, and no
other refusal is sent again.

A call is bounded when the saved catalog lets `provider_pricing.call_credits`
compute its whole-call price: a published rate times a count fixed by the
request. The model supplies no price guesses, and an observed bill never becomes
a price bound; under `hold` (below) it can only set a provisional hold.

A run started by the local launcher admits a paid call only with a whole-call
bound. The launcher hands `TYCHE_UNPRICED_CALLS=hold` to its tool process, and
the ledger saves it once as `unpriced_calls: hold`. Under `refuse` (still
accepted, and saved by earlier launcher runs) a call with no bound is refused:
the same unsent, receipted pricing refusal the `reserved` policy gives; it
spends nothing and the worker can retry with a priced tool or option.

Under `hold`, a Deepline call whose saved contract publishes no rate
(`pricing.unit: usage`, `creditsPerUnit: null`) is admitted at a provisional
hold instead, but only when its contract says `billingSource:
managed_by_deepline`, it takes no caller-chosen quantity input (the list below),
and the tool has not billed above its hold earlier in the run. Otherwise it is
refused unsent as under `refuse`. The hold is the first of: `pricing.observedMax.creditsPerCall`
(the most Deepline billed one call of the tool in 60 days), the run's highest
bill so far for the tool, or 10 credits ($1.00). The public rate card is a base
rate, not a ceiling (page read: 0.01 on the card, billed 0.014), so it is never
the hold. The hold is saved as the call's `admission_bound_credits` with
`admission_bound_source`, so the cap check, in-flight bounds and the
unknown-charge path treat it as a bound, and a hold that would pass the cap is
refused unsent. A reply whose `billing.pricing_status` is `final` settles at
once. Otherwise the call is held at its hold as an unknown charge
(`hold_reason: unpriced_hold`), never left pending, and its bill is read again
by its request ID during and after the run; so is a call whose worker stopped
mid-call. Until that bill is read the call keeps the run's one unpriced slot, so
the run never carries two unknown bills; priced calls are unaffected. Another
unpriced call that finds the slot taken waits up to 30 seconds for that bill
before it takes a provider slot, holding no lock, so priced calls go ahead; a
held call is read once by its ID during that wait. If another waiting call takes
the freed slot first, it waits up to 30 seconds more for that call's bill. It is
refused unsent, as a retryable coordination refusal, if the slot is still taken
then, or at once when waiting cannot free it. A call whose bill
can never be read (no ID and no execution key, or no saved reply) keeps the slot
for the rest of the run, and so does any held call when the run has no API key
for reads by ID (CLI-only credentials, Arena). A bill above the hold counts in
full, refuses that tool for the rest of the run, and is flagged as
`unpriced_hold_overruns` in `run-costs.json` and in `report.md`.

So in a launcher run provider spend cannot pass the budget unless a provider
bills above its bound or hold; that charge is recorded as billed and only
reduces the remaining headroom, and an unpriced tool can do it at most once. No
tool is mandatory, so an unpriced tool never stops a run from starting. Before
`hold`, in nine saved runs the refused paid tools were `discolike_extract` (no catalog rate; a flat
$0.30 in all 25 bills), `firecrawl_scrape` and `exa_contents`. That is a real
cost: page capture through `discolike_extract` was 64 to 89 percent of provider
spend in those runs. The catalog's free page readers price at zero and stay
available; `contextdev_get_web_scrape_markdown` has 69 saved calls across four
runs. Whether they fully replace the paid reader for a given ICP is not shown
here. A ScrapingDog call is bounded by its documented endpoint tariff, which the
locked dispatch holds and checks; an option set with no verified tariff is
refused. People and email tools are refused before pricing, whatever their rate.

A host that does not ask keeps the earlier behavior, and a resume never changes
a saved run's setting. There, a call this module has no rule to bound is billed as before and
stores no bound, so provider spend can exceed the budget by what such calls
billed while in flight. The model chooses the inputs, so even there a call whose
catalog publishes a rate and whose schema has a numeric quantity input from
`provider_pricing.QUANTITY_INPUTS` (`limit`, `page_size`, `pageSize`, `num`,
`numResults`, `pages`, `maxPages`, `max_pages`, `max_results`, `maxResults`,
`perPage`, `per_page`, `count`, `size`) must be priced or it is refused; an
omitted limit is not a way around the budget. An input counts as numeric when
it is untyped, typed `integer` or `number`, a union that includes one of them
such as `["integer", "null"]`, or declared differently in the two schema forms.
A quantity input under a name outside that list is not caught there. Malformed
catalog pricing is refused. No money is set aside for future calls.

Model usage is a stopping threshold, not a cap. The launcher reads it about once
a second and then stops the session, so responses already under way can cross
it, and nothing limits model usage in dollars before the ledger exists. It is
counted at base rates; the pinned Fast tier is listed at twice that.

An explicit `reserved` run keeps catalog-backed reservations and hard provider caps.

Use live catalog pricing to choose suitable tools. Actual billing settles each
saved Deepline request. New Deepline responses must explicitly report
`pricing_status: final`; `settlement_status: queued` means posting is pending,
not that the price is unknown. Estimated or missing price finality stays pending.
Previously saved legacy receipts retain their original interpretation.
A failed or empty result is not proof of a free call. Missing
billing pauses further paid work, without inventing an upper bound. A final
posted zero charge can settle a call as free. For version 2 runs, a successfully
completed call can also settle at zero when its saved pre-call provider contract
explicitly sets an unconditional zero per-call credit price. A completed empty
search follows the same rule when replaying its captured response confirms
`no_results`; the empty result alone is never price evidence. A finished timeout
or provider error (its receipt complete) on such a contract also settles at zero,
because that contract prices every call at zero; the proof records its outcome.
Preserve hashes of that contract and response as `free_evidence`; report its
route under `catalog_free_calls`. This is contract evidence, not a billing
receipt. Paid, variable, conditional or incompletely captured calls, and failures
on any other contract, still require billing.
An exact matching billing record with `status: error`, `charge_state: failed`,
`reason: operation_attempt` and explicit zero credits/delta can settle a failed
response with no returned results. The error response alone cannot settle it.
Preserve IDs, receipts and the
original limits; never repeat a paid call to discover its cost.

`run-costs.json` shows `provider_usd`, `estimated_llm_usd`, `total_usd` and
`pending_provider_calls`. The total is the known subtotal when status is
`incomplete`. Base LLM estimates exclude Fast premiums, hosted tools and
subscription allocation; they are not an invoice.

Local billing reconciliation uses the execution API credentials. It first reads
each uncertain call's own billing record by its saved ID
(`/api/v2/billing/usage?request_id=`). Only a final charge filed under that ID
and tool settles: a posted charge, or a released hold, free or failed outcome
at zero. A final charge is the charge even when the call returned rows. A
temporary hold or a pending, unknown or grouped charge waits, and a missing
record is never zero. An asynchronous (`async_hold`) zero is not taken as the
charge either: two verifications that first read as free later posted one. A
priced call with such a zero is held at its documented bound, like an unknown
charge, until a posted charge replaces the hold. A call whose input Deepline
refused before any provider work is held at its bound the same way when its
billing ID has no record (a keyed call without a saved ID first resolves it
from its key; a key that names no execution leaves the call pending). Its saved
reply must be an HTTP 422 caller validation error, not retryable, naming the
call's own tool and request. Deepline says such calls are free but may have no
billing record; left pending, one would stop every later paid call. Any final
record or history row that later names it replaces that hold. A call whose own record is not
final (a record that does not say `final` is not) is kept off the history pages,
and a history row that states a non-final charge never settles. A failed attempt
whose own record says its finality is `unknown` is held at its bound the same
way and read again by its ID: Deepline says such a failure can still be paid
provider work, and some stay unknown for weeks. A history row settles a failed
attempt at zero only when it says `final`; without reads by ID (CLI-only
credentials), a failed attempt whose history row is not final is held at its
bound instead of left pending, and it is read again only once API credentials
can read it by its ID. Only a call with a bound (a price, or an unpriced call's
provisional hold) can be held; an unpriced call admitted without one stays pending. Saved proof that settled a failed attempt from a row that stated
no finality or `unknown`, as the earlier rule allowed, stays valid when a saved
run is audited. A key's billing request ID is saved the first time
it resolves, since key lookups last about a day and ID lookups longer.
Calls still unmatched fall back to the history scan below:
the credit ledger for individual `charge_settle` debits, then the usage feed for
explicit free/failed outcomes. Grouped usage totals are never divided among calls
or added to individual debits. Rounded display fields such as `rough_usd_cost`
are never accounting inputs. Reported credits and exact USD remain separate;
the configured credit conversion is used only when exact USD is absent.

Reads match exact request IDs and provider operations, with up to four 100-row
pages per feed within one shared 30-second deadline. CLI-only credentials retain
the bounded usage fallback. Automatic reads retain a three-attempt
allowance and cooldown. Each validated page settles attributable charges before
saving its continuation cursor. A later-page failure preserves those charges;
the next API read checks the newest page for late postings before continuing
older history. The usage API uses its documented numeric offsets: its opaque
cursor repeated pages during live validation. Offsets must advance, repeated
page identities are rejected, and identical cross-page overlaps are not counted
twice. A ledger transport outage can use a healthy usage feed; malformed records
or organization changes fail closed. `billing-status.json`
records each attempt's page numbers, elapsed seconds, failure categories and
unmatched calls without copying provider output. After an outage, explicitly
resume billing-only reads:

```bash
python3 .agents/skills/lead-sourcing/scripts/billing_reconciliation.py reports/<run-id>/results.json --resume
```

This grants three further read attempts and preserves their history. It never
dispatches research, clears missing charges, raises a limit or resets spend.
Missing request IDs still require authoritative provider evidence. Transport
receipts retain the failure stage, exception type, elapsed time and numeric
errno without exception messages or credentials. They do not prove a call was
unsent. ScrapingDog uses versioned documented endpoint tariffs for completed requests,
including empty results. Explicit provider failures cost zero under its published
policy. Variable tariffs and unresolved responses retain the documented maximum
as `held_credits`, separate from actual charges. The ledger reserves that ceiling
before dispatch and counts it against both limits, so concurrent calls cannot
reuse it. This allows a different request to continue without replaying the
uncertain call. Unverified option combinations still pause for billing evidence.

`held_provider_usd` and `budget_total_usd` appear when tariff holds remain; the
latter includes charges, holds and model estimates. The known subtotal never
includes a hold as billed spend. Sources, version, original request and raw
response are checked by the strict ledger audit. ScrapingDog stays disabled by
default and requires a saved plan conversion; account-wide balance differences
must not be attributed to one concurrent run. See [its adapter](scrapingdog-adapter.md).

Spending comparisons retain decimal precision until the JSON report boundary.
Each guarded dispatch holds an OS lease for its lifetime. Concurrent live calls
remain permitted; after a crash, an unowned `in_flight` reservation is counted as
pending billing and prevents further spending. The reservation and original
receipt remain intact. Ledger writes reuse crash-released OS locks; unidentified
legacy lock files still require owner verification rather than time-based expiry.

## Reserved runs and overrun repair

A `reserved` run (version 1 ledger) keeps its reservation checks. Its caps,
holds and price-overrun blocks stay auditable; `provider_pricing.py` bounds
calls from live catalog rates only. After an actual pricing repair,
`budget_guard.py --reconcile-receipt` can validate an overrun receipt without
changing its original limits or charges.
