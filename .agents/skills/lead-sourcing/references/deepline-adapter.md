# Deepline adapter

Normal sourcing uses [native tools](adapter-io.md#native-tools), which supply
envelopes, descriptions, receipts and accounting. These wrapper contracts are
diagnostics, not setup work for every lookup. Read [Capability discovery](#capability-discovery)
for search ideas.

## Deepline wrapper

`scripts/deepline.py` uses the installed CLI for tool discovery, schemas and
prices. Execution uses Deepline's API directly with `DEEPLINE_API_KEY` or the
existing production SDK login (nearest matching `.env.deepline`, then
`~/.local/deepline/code-deepline-com/.env`). This preserves HTTP error bodies,
request IDs and explicit bills without changing authentication or saving keys
in artifacts. Custom CLI hosts/binaries retain the CLI path. Neither path retries
uncertain executions. A completed upstream error with explicit billing can
settle even when the provider operation timed out; a local timeout stays pending.
A missing charge stays unresolved until an authoritative receipt is available; a validation
error alone does not prove a zero charge. A catalog
hit is not company evidence. A disconnected tool is not an empty
result.

The isolated supervisor waits up to 120 seconds for delayed, attributable bills,
within the original sourcing deadline and saved billing-read allowance. This
uses only billing reads, without model turns or paid-call retries. Missing
request IDs are recorded in `billing-status.json` with an explicit recovery
requirement; waiting or repeating the paid request cannot safely repair them.
For calls with a dispatch-bound catalog, settlement and audit verify the saved
descriptor hash before using its provider or operation aliases.

### Network access

The native tool process and its CLI subprocess use the same workspace permission
profile and provider-domain allowlist as the worker. A failed native call is not
permission to rerun it through an unrestricted shell. For diagnostic CLI use,
the wrapper and its subprocess inherit the host's network restrictions.
When network access is restricted, use the supported approved execution path
for Deepline calls. In Codex, request `sandbox_permissions: "require_escalated"`
with a scoped justification on the host execution tool; this is not a shell
flag or Deepline request field. Reuse applicable existing approvals.

`NETWORK_ERROR`, `fetch failed`, and `Could not resolve host` can reflect local
sandbox restrictions. Before declaring Deepline unavailable, check execution
permissions and retry a no-cost catalog `search` or `describe` through the
approved path. A successful health check alone does not establish catalog
access. Save both attempt receipts. If catalog access succeeds, remove resolved
access blockers, rerun the stop check, and continue eligible work with the
existing budget, start time, and reservations.

If approval is denied, preserve the denial and affected action, honor that
boundary, and continue unaffected work. Do not change global sandbox or DNS
settings or pin host IPs as a workaround. If approved execution still fails,
record that evidence and leave the cause uncertain unless further diagnostics
establish it.

Use free catalog calls for connectivity diagnostics. Never retry a paid
`execute` whose remote outcome is uncertain; preserve its receipt and spend
reservation even when a later catalog check succeeds.

### Capability discovery

Search the live catalog with narrow seeds that match the hypothesis. These are
search seeds, not fixed IDs; use only the tool ID returned by the current search:

- `companies with current hiring or job postings`
- `companies with recent funding, press, or product news`
- `companies with paid advertising or campaign activity`
- `companies with recent patents, facilities, openings, or expansion`
- `companies with official video or event activity`
- `company firmographic search by industry geography and size`

Use the [evidence-gap map](tools.md#choose-by-evidence-gap) and its linked
capability reference for broader seeds. Search by provider plus the missing capability. Do not restrict discovery
to catalog categories: useful public-data reads can be labeled `admin`, while
an `automation` result can be a paid research job. Inspect the actual contract
and side effects; neither category grants execution permission.

Learn each selected tool once with `tyche_inspect(tool=...)`. Native tools save
and reuse its description, then check its inputs, connection and price before
dispatch. Refresh only after evidence that the saved contract or access changed.
Ignore non-callable or monitor-only search hits for this one-shot workflow; do not deploy monitors.

```bash
python3 .agents/skills/lead-sourcing/scripts/deepline.py --input '{"operation":"search","query":"companies with current hiring or job postings"}'
python3 .agents/skills/lead-sourcing/scripts/deepline.py --input '{"operation":"describe","tool":"<id returned by search>"}'
python3 .agents/skills/lead-sourcing/scripts/deepline.py --input-file 'reports/<run-id>/requests/<route-id>.json'
```

The execute request file includes `operation`, `tool`, `payload`, and the
required [spend context](adapter-io.md#paid-call-budget). `execute` is paid.
A company-discovery pilot has one paid call and at most 10 returned rows;
All returned execute rows are retained. Native lookup/inspect displays ten rows
at a time; use its `next_offset` to inspect the rest without another paid call.
The wrapper `limit` does not limit provider billing; set provider-native result/count and page or cursor fields from the live schema,
then bound the cost before execution. Inspect the live price first; expand only
when rows are relevant, diverse, and evidentiary. The wrapper invokes
`deepline tools search`, `describe`, or
`execute`, writes a temporary payload file, redacts secrets, and emits one JSON
object. Catalog calls default to 30 seconds (cap 120); execute calls
default to 240 seconds (cap 780). Never automatically retry an uncertain paid call.
After each execute, use billed usage as `cost_credits` with
`cost_basis: "actual"`. If billed usage is unavailable but the live description
gives a conservative bound for all calls recorded by the route, use
`cost_credits: null`, that total bound as
`cost_upper_bound_credits`, and `cost_basis: "estimated"`. Use `unknown` with
both values `null` only when neither value is available.
A typical or midpoint price is not a conservative bound. Use `unknown` when
unresolved pricing inputs can make the route cost higher.

When Deepline supplies billing, the adapter preserves finite, non-negative
`billing.credits_charged` and `billing.cost_usd`. These are observed amounts,
not estimates inferred from row counts. Missing billing stays missing; keep
the conservative bound until an actual charge is available.

The wrapper normalizes candidate fields to `company`, `domain`, `signal`,
`evidence_url`, `evidence_date`, `evidence_text`, `provider`, and `tool`.
Generic labels such as `search_result`, `web_page`, `company_profile`, and
`hiring` are discovery labels only. For web-search rows, `domain` can be the
source host; resolve the canonical company domain before acceptance.

Successful scraped pages retain their metadata, source URL and Markdown/HTML
content as `web_page` evidence with `content_format`, including pages inside
result lists. Qualification checks use the captured URL, body and date metadata
regardless of the crawler's name; failed or empty HTTP captures cannot qualify.
HTML remains HTML: inspect
its actual fields before writing plain-language qualification evidence. A page
is not a qualified company. Recover response-shape errors from saved
raw responses without another provider call; preserve the original receipt and
record any local normalization continuation with zero new calls and no duplicate
charge.
For required [LinkedIn company size](output-contract.md#linkedin-company-size),
use the matched URL with the live HarvestAPI company getter. The adapter preserves
raw response data and exposes `employee_range` from `employeeCountRange`. It does
not derive a range from `employeeCount`. Save field evidence with the exact
getter's tool and route ID before accepting a company. Reference response shape:
[company](https://elrix.mintlify.app/linkedin-api-reference/company/get).

For recognized event and post envelopes, the wrapper retains optional
top-level `pagination`, `meta`, and `links` metadata with secrets redacted.
HarvestAPI's known `pagination.paginationToken` is exposed separately as
`pagination.next_cursor`. Map this opaque cursor back to the live provider
input field only for an explicitly budgeted continuation; paging is never
automatic. Review the complete saved page with inspect before purchasing another.
Older preview-only receipts can also be inspected in full without redispatch.

HarvestAPI post rows retain the post URL, content, `postedAt`, and author.
Verify the author, company, and original versus reposted source. A post by an
individual does not establish that individual's employer, and a company-authored
post does not establish its canonical domain.

For JSON:API event responses, relationship names such as `company1` and
`company2` are retained in `related_companies`. Related companies are
candidates, not accepted companies: resolve the relationship and account gate
explicitly, never pick the first company. Use a linked source `published_at`
for `evidence_date` when available, keep any effective `event_date` separately,
and never treat ingestion fields such as `found_at` or `updated` as event
freshness.

### Deepline status contract

The wrapper emits exactly: `ok`, `no_results`, `partial`, `rate_limited`,
`auth_failed`, `quota_exceeded`, `timeout`, `schema_error`, `provider_error`,
or `config_error`. Only `ok` and `partial` can supply candidates. `no_results`
does not prove absence. Other statuses are unresolved provider outcomes; stop or
change route and retain `status`, `error` when present, `provider`, `operation`,
`tool`, and normalized `results` in the report.
