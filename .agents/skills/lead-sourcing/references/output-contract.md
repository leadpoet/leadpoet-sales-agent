# TYCHE output contract

This is the normative contract for one company-sourcing run: the saved request,
the criteria code derives from it, the saved company records and the delivered
files. A run directory `reports/<run-id>/` delivers `report.md`, `results.json`
and `leads.xlsx`. It also keeps `leads.json` (reviewed companies, saved as they
are confirmed), provider receipts under `receipts/` and the execution ledger
`results.json.budget.json`. JSON Schemas below are draft 2020-12.

New runs use `budget.policy: actual_cost`: reported provider charges plus local
base LLM estimates form the run budget. A paid call with a documented price is
admitted only when its bound fits. A run whose ledger saved `unpriced_calls:
refuse` refuses a call the saved catalog cannot bound. Under `hold`, as the
local launcher asks, an unpriced Deepline-billed call is admitted one at a time
(a second waits for the first one's bill) at a provisional hold that its exact
bill replaces. Model usage is a threshold and can cross the budget. Pending charges stay
unknown until billed. See [cost policy](provider-pricing.md).

## Stage boundary

TYCHE finds companies that match the original criteria and initial signals,
qualifies each one independently from captured sources, and saves
source-backed company records for the later stages:

- **DEMOS** (contact stage) owns buyers. Buyer roles, contacts per company,
  email, phone and person location are its inputs. A request that carries
  `requested_roles`, `contact_role_groups`, `contact_fields`,
  `contacts_per_company`, `min_contacts_per_company` or
  `target_contacts_per_company` is refused before anything is saved; the caller
  keeps those requirements for the end-to-end target. Buyer requirements never
  gate a TYCHE company.
- **ARGUS** owns deeper intent discovery and narrative. TYCHE saves the initial
  signal evidence with its activity date and source, never an intent story.
- TYCHE does no person, contact, email or phone work. A tool that finds or
  returns people is refused before any reservation or call. It is recognised
  by its id words (people, person, employee, buyer, prospect, contact, lead,
  email, phone, mobile, role), by a people or email provider's name without a
  company word, by profile/enrich/lookup without a company or social-account
  word, or by person output fields in its saved description. An email or phone
  add-on in a payload is refused the same way. The recogniser is a denylist,
  not proof: every call stays visible in the receipts.

Result schema `2.1` is the only schema this code reads. A run saved by an
earlier TYCHE (`1.x` contact-flow runs, `2.0` company runs without criterion
ids) stays exactly as saved: validation, review, resume and export refuse it by
name and change nothing. Read or resume it with the revision that wrote it; the
last such revision is `dc412f2`.

## Read by phase

This file is the single normative contract. Do not load it all upfront: find
the relevant heading and read that section. Native tools build receipts,
validate stored records and export; consult the raw schemas only for a specific
unresolved tool error.

| When | Sections |
|---|---|
| Before discovery | [Lifecycle invariants](#lifecycle-invariants), [Input contract](#input-contract), [Criteria](#criteria), [Timing](#timing). |
| Before selecting qualification evidence | [Semantic checks](#semantic-checks), [Accepted-company sources](#accepted-company-sources). |
| Before accepting and describing a company | [Client writing and taxonomy](#client-writing-and-taxonomy). |
| Before export and final delivery | [Workbook](#leadsxlsx-contract), [Report minimum contents](#reportmd-minimum-contents), [Final-response checklist](#final-response-checklist); finish [Timing](#timing). |

## Lifecycle invariants

1. A company is accepted when its canonical identity is established; every
   requested filter, required attribute and company-size bound has a passing
   check that quotes a captured source or this company's receipt-verified
   record; required signal coverage holds under `signal_match_mode` with
   in-window activity dates; the company is not excluded; and the row passed
   the independent review packet.
2. `target_count` counts accepted companies. It is the completion condition.
   After attrition, source one replacement from a changed route while the
   accepted count is short and the route frontier is actionable. Do not
   prefetch or refill by a fixed multiplier.
3. `accepted`, `rejected` and `unresolved` are output states, not provider
   statuses. A `no_results` provider response is not a rejection; timeout,
   quota, authentication, schema and provider errors leave the route blocked.
4. Accepted companies are unique by `company.domain`, the key `tyche_claim`
   returns: the lower-case website domain without scheme, port, `www.`, path or
   trailing dot, whatever spelling was typed. It must be the company's own
   website domain, not a name, person profile or social or directory page. A
   careers or jobs section of the company's own site, or any two-letter section
   such as a country or language site (`careers.example.com`, `de.example.com`),
   gives the site's registrable domain (`example.com`); a hosting or recruiting
   platform's subdomain keeps its host, and a public suffix (`gc.ca`, `asso.fr`)
   is never a key.
   That domain is the stable company ID for the claim, the duplicate check, the
   review, `leads.json` and every later stage.
5. The route frontier holds paid-provider and public-web paths. Each path is
   `untried`, `continuable`, `exhausted` or `blocked`. A final shortfall is
   invalid while any path is actionable unless the strict stop check verifies
   an explicit time limit or that no next action fits the budget. Keep
   unfinished routes actionable in those cases. A failed or uncertain paid call
   is not retried automatically, but it does not exhaust other paths. The
   frontier is append-only.
6. All dates are ISO calendar dates. A relative provider date may be resolved
   from retrieval time only when the original wording is kept in `report.md`.
   Never invent a date.
7. `signal_match_mode` defaults to `any` and applies across required signals;
   facts within one signal's query stay conjunctive. Each signal is saved with
   `importance` `required` or `preferred` (default required). Preferred signals
   rank and explain only: they never satisfy a required alternative and an
   unverified one never blocks acceptance. When the user gave no signal, record
   a preferred use-case hypothesis, clearly identified as inferred. A signal's
   `min_age_days`, `max_age_days` or `max_age_months` is measured back from the
   saved as-of date; a signal-specific maximum overrides the shared
   `time_window`, and month subtraction clamps to the destination month's last
   day. Omit unrequested bounds; an empty `time_window` means no shared limit,
   and with no window the date is not a requirement.
8. Each independent must-have is its own `required_attributes` entry. Preserve
   OR alternatives within one filter and the scope of exceptions. A negative
   exclusion needs a targeted public screen, not a company biography. Supplied
   prior contrary findings must be resolved or the criterion stays unknown.
9. Missing evidence is `unknown`, never `fail`. A required `fail` rejects; a
   required `unknown` holds the company in `unresolved`. See [Criteria](#criteria).

## Input contract

`request-options.json` beside `request.txt` is optional: a JSON object that may
hold `signal_candidates_max`, a whole number from 0 to 50, with it
`trait_signals` (below), and, with or without them, `seen_companies`,
`max_duration_seconds` and `single_source_attributes` (below). It may also hold
`required_attributes`, a nonempty array of distinct, verbatim must-haves already
present in `request.txt`. Startup adds missing entries to `icp.required_attributes`
and saves the bound list for resume checks; evidence is still required for every
one. Startup binds these options
without model transcription, as it binds `request-exclusions.json`. A missing
file or `signal_candidates_max` key means 0 (off). A bad value is refused before anything is saved, and
a resume keeps the saved bound. With a bound above 0, each required signal's
guidance in `inspect(field='requirements')` also asks the worker, during
research, to capture a page for any fit check resting on a search excerpt or an
unverified record, and to save the company fields and `account_fit` a lead
carries, before holding a company only for an unfound signal, for at most that
many companies and only when the target does not need that budget or time.

The options file may also hold `max_usd`, a finite nonnegative number for the
caller's total USD cap. Startup uses this value in place of the model's
`tyche_start.max_usd`; the saved ledger keeps that exact cap on resume and
refuses a changed value. Without this key, the existing model-supplied or
default cap applies.

With that bound above 0, the file may also hold `trait_signals`, a list of
buying-signal kinds (`buying_signals[].kind`, matched ignoring case and
punctuation) that are standing traits: what a company is, uses or does. Every
other kind is an event, and so is a signal with an age window, whatever the
list says. Startup binds it the same way, refuses an unknown kind or a non-empty
list without the bound, and a resume keeps it. Each required trait signal
is marked `trait` in `inspect(field='requirements')`, and its guidance says when
it may be likely. A held company's check on such a signal may then keep status
`unknown` and add `support: "likely"` with a one-line `support_reason` (at most
200 characters) stating the inference from at least one quote-checked, captured
passage about that company, never a search excerpt, a generalisation, memory,
another company's facts or what it sells or offers others to do (unless the
signal is that service itself). Such a check is refused on any other check or
status, it never counts as a pass, and an accepted or rejected company carries
none: code drops it from a still-unknown check when a held company is accepted
or rejected, and on a pass or fail it is refused.

`single_source_attributes`, a list of required attribute texts, names the
attributes whose passing checks must cite one passage. Startup binds it as
written, a resume keeps it, and the model never sets it. For named attributes,
guidance asks for the passage that states the distinctive part (how the company
sells, whom it serves or the activity it asks for), with the company's other
saved sources stating the rest. Code refuses a passing check that cites more
than one passage or contains a relative date such as "Posted 2 days ago".
New Arena runs do not set this option automatically; explicit and previously
saved bindings remain unchanged. Arena publishes one primary URL/quote and can
corroborate compound attributes across independently fetched sources. All
requested parts and other source guards still apply.

`seen_companies`, a list of company domains or LinkedIn company URLs, marks a
continuation run: the companies earlier runs for the same request already
accepted, held or rejected (each saved row's `company.domain`, or
`candidate.domain` for a held or rejected row). Startup binds it as written, a
resume keeps it, and the model never sets it. Each entry is keyed as a claim is
(case, `www.`, a careers, jobs or country host and an IDN spelling key alike).
Code refuses `tyche_claim`, a `tyche_lookup` whose target is a listed company,
and a `tyche_review` of one, its company or its web observation, before any call
or save, so the run sources only companies it has not seen. A lookup for another
company is not refused when an input is a listed company's page. A LinkedIn
company URL entry matches only a LinkedIn URL the run supplies (a claim's
`company_url`, a company getter's `url`, or a selected getter record), and a
name entry only the same name used as a target. It is not a criterion or an
exclusion.

`max_duration_seconds`, a whole number of seconds above 0, is the host's
research time limit. Startup binds it as written, in place of whatever the
model's request says (null included), a resume keeps it, and a changed value is
refused. Without the key, the model's reading of the request sets the limit as
before; a host omits it to let the request decide, since a null here could drop a
stated limit.

The normalized request (`results.json.request`) must validate against this
schema. Defaults are applied before provider work. When the user gives no
spending budget, the [default run budget](workflow-rules.md#default-run-budget)
is USD 0.80 per requested company; `tyche_start` takes it as `max_usd` and
derives provider credits. Explicit budgets override the default.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "tyche://lead-sourcing/input-contract/v2",
  "title": "TYCHE company-sourcing request",
  "type": "object",
  "additionalProperties": false,
  "required": ["target_count", "icp", "buying_signals", "time_window", "budget"],
  "properties": {
    "original_text": {"type": "string", "minLength": 1, "description": "The request as the launcher received it, preserved unchanged. Buyer and contact wording in it belongs to the contact stage."},
    "target_count": {"type": "integer", "minimum": 1, "description": "Unique accepted companies."},
    "signal_candidates_max": {"type": "integer", "minimum": 0, "maximum": 50, "description": "Bound at startup from request-options.json beside the run, never set by the model: at most this many held companies are exported as leads.json signal_candidates. Absent means none."},
    "seen_companies": {"type": "array", "items": {"type": "string"}, "description": "Bound at startup from request-options.json, never set by the model: companies earlier runs for this request already saw, which this run never claims, researches or reviews."},
    "trait_signals": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": true, "description": "Bound at startup from request-options.json with signal_candidates_max above 0, never set by the model: the buying-signal kinds that are standing traits and may be likely. Absent means none."},
    "bound_required_attributes": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}, "uniqueItems": true, "description": "Exact must-haves bound from request-options.json required_attributes; saved to detect changed options on resume, never set by the model."},
    "max_duration_seconds": {"type": ["integer", "null"], "minimum": 1, "description": "Null (the default) means no research deadline. A value in request-options.json is bound at startup in place of the model's."},
    "icp": {"$ref": "#/$defs/icp"},
    "product_service": {
      "type": "object",
      "additionalProperties": false,
      "required": ["description", "perspective"],
      "properties": {
        "description": {"type": "string", "minLength": 1},
        "perspective": {"enum": ["seller", "target"]}
      },
      "description": "Carried for the later stages; never widens or narrows the company criteria."
    },
    "buying_signals": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/signal"}},
    "signal_match_mode": {"enum": ["any", "all"], "default": "any"},
    "time_window": {"$ref": "#/$defs/time_window"},
    "budget": {"$ref": "#/$defs/input_budget"},
    "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"},
    "as_of_date": {"$ref": "#/$defs/date"}
  },
  "$defs": {
    "date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
    "icp": {
      "type": "object",
      "additionalProperties": false,
      "minProperties": 1,
      "properties": {
        "company_types": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "industries": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "geographies": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "company_size": {
          "type": "object",
          "additionalProperties": false,
          "minProperties": 1,
          "properties": {
            "min_employees": {"type": "integer", "minimum": 0},
            "max_employees": {"type": "integer", "minimum": 0}
          }
        },
        "required_attributes": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "exclusions": {"type": "array", "items": {"type": "string", "minLength": 1}}
      }
    },
    "signal": {
      "type": "object",
      "additionalProperties": false,
      "not": {"required": ["max_age_days", "max_age_months"]},
      "required": ["kind"],
      "properties": {
        "kind": {"type": "string", "minLength": 1},
        "importance": {"enum": ["required", "preferred"], "default": "required"},
        "query": {"type": "string", "minLength": 1},
        "min_age_days": {"type": "integer", "minimum": 0},
        "max_age_days": {"type": "integer", "minimum": 1},
        "max_age_months": {"type": "integer", "minimum": 1},
        "source_preferences": {"type": "array", "items": {"type": "string", "minLength": 1}}
      }
    },
    "time_window": {
      "type": "object",
      "additionalProperties": false,
      "not": {"required": ["max_age_days", "max_age_months"]},
      "properties": {
        "max_age_days": {"type": "integer", "minimum": 1},
        "max_age_months": {"type": "integer", "minimum": 1},
        "as_of_date": {"$ref": "#/$defs/date"}
      }
    },
    "input_budget": {
      "type": "object",
      "additionalProperties": false,
      "required": ["hard_stop"],
      "properties": {
        "deepline_credits": {"type": "number", "minimum": 0},
        "scrapingdog_credits": {"type": "number", "minimum": 0},
        "max_paid_calls": {"type": "integer", "minimum": 0, "deprecated": true, "description": "Ignored. Omit it."},
        "max_deepline_credits_per_next_lead": {"type": "number", "minimum": 0},
        "hard_stop": {"const": true}
      },
      "anyOf": [
        {"required": ["deepline_credits"]},
        {"required": ["scrapingdog_credits"]}
      ]
    }
  }
}
```

Provider credit caps are separate because Deepline and ScrapingDog units are
not interchangeable; a cap of 0 disables that provider. `hard_stop` is always
true. Stop limits are money and explicit time limits, never call counts:
`max_paid_calls` is accepted and ignored. Catalog search and describe calls are
free reads. A provider that does not expose usage leaves its `spent` and route
`cost_credits` `null`; never use `0` to mean unknown. The saved request is
immutable: resuming with a different request is refused.

## Criteria

Code derives the criteria from the saved request and saves them as
`results.json.criteria` and `leads.json.criteria`. They are never authored.
Each criterion has a stable `id`, its `kind`, a `label`, its `importance` and,
for signals, the requested query and age bounds, plus `trait: true` on an
undated signal that `request-options.json` lists in `trait_signals`:

| `id` | `kind` | Meaning |
| --- | --- | --- |
| `attribute:N` | `attribute` | Entry N of `icp.required_attributes`. Required. |
| `icp:company_types`, `icp:industries`, `icp:geographies` | `filter` | The requested filter; alternatives in one filter share one check. Required. |
| `icp:company_size` | `size` | The requested employee band. Required; reviewed like any attribute. |
| `signal:N` | `signal` | Entry N of `buying_signals`; label is its `kind`. Required or preferred. |
| `exclusion:N` | `exclusion` | Entry N of `icp.exclusions`: the company must be distinct from it. Checked when a candidate resembles that company. |

Ids are positions and fields in the immutable request, so they never change
for the life of a run. `tyche_inspect(field='requirements')` returns them with
`judging_rules`, each kind's rules once; a criterion's `guidance` names its
kind's rules and adds only its own note. Evidence reviews and review packets
carry `judging_rules` the same way. The plain `tyche_inspect()` view lists the
criteria without the rules.

A qualification check answers exactly one criterion: `criterion_id`, then the
code-copied `criterion` label, `importance` and (for signals) `signal` kind,
then the reviewer's `status`, `claim` and `evidence[]`, and on a held company's
unknown trait signal, optionally `support: "likely"` and `support_reason` (see
[Input contract](#input-contract)). Facts that answer no
requested criterion belong in `supporting_findings`.

| Status | Meaning | Effect when required |
| --- | --- | --- |
| `pass` | Captured evidence supports the criterion. | Counts toward acceptance. |
| `fail` | Captured evidence contradicts it. | Rejects the company (`not_icp_fit`, or `explicit_exclusion` for an exclusion). |
| `unknown` | Evidence is missing or ambiguous. | Holds the company in `unresolved` (`missing_account_evidence`). Never rewritten as `fail` for lack of a source. |

The row's list is the decision. Held and rejected rows keep `stage: account`,
`reason_code`, a `reason_text` that names the missing or contradicting fact and
the next step, and their checks. `reason_code` is derived by code from the
decision: `missing_account_evidence` (held), `not_icp_fit` (an evidenced
required failure) or `explicit_exclusion`.

## `leads.json` confirmed companies

`tyche_start` creates an empty confirmed snapshot next to `results.json`.
Accepting a company through `tyche_review` returns its saved-source evidence
packet. Review source meaning, requirements and writing, then approve the
current `review_ref` with company-specific `review_findings` in a separate
`tyche_review` call. Approval validates receipts and qualification, then
atomically replaces `leads.json`. New lookups wait for pending reviews. The
requesting agent reviews the packet; this is not human approval.

| Field | Meaning |
| --- | --- |
| `schema_version` | `tyche.confirmed-leads.v2` |
| `run_id`, `run_fingerprint`, `request_fingerprint` | The source run and its unchanged request. Preserve the run directory when resuming. |
| `target_count`, `as_of_date` | From the saved request. |
| `criteria` | The saved criteria with their stable ids, as above. |
| `confirmed_count` | Number of rows in `leads`; may be below the target. |
| `updated_at` | UTC time of the last snapshot change. |
| `leads` | Reviewed rows in the `results.json.accepted` shape, keyed by `company.domain`. |
| `review_findings` | One `{target, source_refs, finding}` per current row: the review record, not client text. |
| `client_caveats` | `{company.domain: caveats}` that code writes from each row's cited passages, as the workbook's `Review Notes` and `report.md` show them. |
| `signal_candidates_max` | Only when `request-options.json` set it: the bound applied, 0 to 50. |
| `signal_candidates` | With it: held companies for signal research downstream, at most the bound, in review order. Each row has the `leads` row shape (with an empty `signal_evidence`) plus `hold_reason`, TYCHE's reason for holding it, `open_signals` `[{criterion_id, status, claim, event_dates}]` (each required signal not found: `unknown`, or `fail` such as an event dated outside the window, with its evidence's event dates; a failed signal's passage is quote-checked as a rejection's is) and `client_caveats`, the caveats code writes from its cited passages as for a lead. Each passes the checks a lead passes apart from signal coverage, including the company's canonical name, a website on its domain, a canonical industry and sub-industry, a description and an `account_fit` quoting a captured page or record; every required filter, size and attribute passed with quote-checked evidence; no filter, size, attribute or exclusion check failed; and it is not an excluded or resembling-excluded company. A held company without these is not exported; nothing is filled in. A check on an open trait signal may carry `support: "likely"` and `support_reason` (see [Input contract](#input-contract)); the signal stays in `open_signals`. While the bound leaves a slot open, the `tyche_review` response to such a hold lists what keeps it out as `signal_candidate_gaps` `[{target, errors, next}]`, for at most the open slots and never for an excluded company, so the worker can correct it once while the company is current; the hold is saved either way. Its required signals were not found: under `signal_match_mode` `any` none passed, under `all` not every one passed, whether unknown or failed. In such a run a company that passes every filter, size and attribute check, and is not an excluded company, is held, never rejected, when only its required signals fail. They are not leads: they never count toward `confirmed_count` or the target and never appear in the workbook or `report.md`. |

Only reviewed, unchanged companies enter this file. A row changed or withdrawn
through review leaves it and needs fresh approval to re-enter. Unchanged rows
survive later provider errors and unfinished research. A failed write keeps the
last complete file; an invalid or foreign snapshot is preserved and reported.
Approval retries are idempotent.

Consumers may read this file at any time as the confirmed partial list. It
does not assert completion or bypass final stopping, accounting, review and
workbook checks. On an operational block, the launcher and `tyche_finish`
export unchanged confirmed rows to `leads-partial.xlsx` with Sources and an
explicit incomplete `Status` sheet; `validation-partial.json` records the
verification with `partial: true` and `delivery_allowed: false`. That read-only
export does not reconcile billing, change research or overwrite the full
workbook. With no confirmed rows, no partial workbook is produced. For local
recovery use `export_xlsx.mjs <results.json> --partial`. A diagnostic run with
a different results filename names the snapshot `<results-stem>.leads.json`.

## `results.json` schema

Write one JSON object that validates against this schema. Native tools write
it; do not add a second result list or hide rows in a count.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "tyche://lead-sourcing/results/v2",
  "title": "TYCHE company-sourcing results",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "run_id", "retrieved_at", "request", "criteria", "budget", "routes", "summary", "accepted", "rejected", "unresolved", "stop_check", "cost_summary"],
  "properties": {
    "schema_version": {"const": "2.1"},
    "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"},
    "retrieved_at": {"type": "string", "format": "date-time"},
    "request": {"type": "object", "description": "The saved request; see the input contract."},
    "criteria": {"type": "array", "items": {"$ref": "#/$defs/criterion"}},
    "budget": {"$ref": "#/$defs/output_budget"},
    "routes": {"type": "array", "items": {"$ref": "#/$defs/route"}},
    "cost_summary": {"$ref": "#/$defs/cost_summary"},
    "summary": {"$ref": "#/$defs/summary"},
    "accepted": {"type": "array", "items": {"$ref": "#/$defs/accepted_company"}},
    "rejected": {"type": "array", "items": {"$ref": "#/$defs/outcome_row"}},
    "unresolved": {"type": "array", "items": {"$ref": "#/$defs/outcome_row"}},
    "stop_check": {"$ref": "#/$defs/stop_check"},
    "stop_audit": {"$ref": "#/$defs/stop_audit"},
    "final_review": {"$ref": "#/$defs/final_review"},
    "stop_reason": {"enum": ["target_met", "budget_exhausted", "time_limit_reached", "no_productive_route", "provider_stop", "input_or_configuration_stop"]}
  },
  "$defs": {
    "date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
    "source_date": {"type": "string", "pattern": "^[0-9]{4}(-[0-9]{2}){0,2}$", "description": "Captured publication precision; observed_current requires a full YYYY-MM-DD observation date."},
    "url": {"type": "string", "pattern": "^https?://[^\\s]+$"},
    "date_basis": {"enum": ["published", "posted", "updated", "observed_current"]},
    "provider_status": {"enum": ["ok", "no_results", "partial", "rate_limited", "auth_failed", "quota_exceeded", "timeout", "schema_error", "provider_error", "config_error"]},
    "criterion": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "kind", "label", "importance"],
      "properties": {
        "id": {"type": "string", "pattern": "^(attribute:[0-9]+|icp:(company_types|industries|geographies|company_size)|signal:[0-9]+|exclusion:[0-9]+)$"},
        "kind": {"enum": ["attribute", "filter", "size", "signal", "exclusion"]},
        "label": {"type": "string", "minLength": 1},
        "importance": {"enum": ["required", "preferred"]},
        "query": {"type": "string", "minLength": 1},
        "min_age_days": {"type": "integer", "minimum": 0},
        "max_age_days": {"type": "integer", "minimum": 1},
        "max_age_months": {"type": "integer", "minimum": 1},
        "trait": {"const": true, "description": "Only on a signal that request-options.json lists in trait_signals: a standing trait a held company may carry as likely."}
      }
    },
    "source": {
      "type": "object",
      "additionalProperties": false,
      "required": ["provider", "operation", "route_id"],
      "properties": {
        "provider": {"type": "string", "minLength": 1},
        "operation": {"type": "string", "minLength": 1},
        "tool": {"type": "string", "minLength": 1},
        "route_id": {"type": "string", "minLength": 1},
        "result_index": {"type": "integer", "minimum": 0, "description": "Index of the selected result in its receipt: a verified structured record without a public URL (for example a funding round), or the captured page behind linkedin_url_evidence."}
      }
    },
    "company": {
      "type": "object",
      "additionalProperties": false,
      "required": ["canonical_name", "domain", "website", "industry", "sub_industry", "description"],
      "properties": {
        "canonical_name": {"type": "string", "minLength": 1},
        "domain": {"type": "string", "minLength": 1, "description": "Stable company ID: the claim key, the company's own website domain in lower case, without scheme, port, www., path or trailing dot; a careers, jobs or two-letter section of its site gives the registrable domain."},
        "website": {"$ref": "#/$defs/url"},
        "linkedin_url": {"$ref": "#/$defs/url"},
        "linkedin_url_evidence": {"$ref": "#/$defs/linkedin_company_evidence"},
        "industry": {"type": "string", "minLength": 1},
        "sub_industry": {"type": "string", "minLength": 1},
        "classification_note": {"type": "string", "pattern": "\\S"},
        "description": {"type": "string", "minLength": 1},
        "hq_state": {"type": "string", "minLength": 1},
        "hq_country": {"type": "string", "minLength": 1},
        "employee_range": {"type": "string", "minLength": 3},
        "employee_range_evidence": {"$ref": "#/$defs/linkedin_company_evidence"},
        "owner_group": {"type": "string", "minLength": 1},
        "aliases": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "discovery_source": {"type": "object", "additionalProperties": false, "required": ["source"],
          "description": "The original account-discovery result: its saved receipt reference and URL.",
          "properties": {"source": {"$ref": "#/$defs/source"}, "url": {"$ref": "#/$defs/url"}}}
      }
    },
    "candidate": {
      "type": "object",
      "description": "A held or rejected company: the company fields known so far, at least its domain or name."
    },
    "linkedin_company_evidence": {
      "type": "object",
      "additionalProperties": false,
      "required": ["evidence_url", "evidence_date", "evidence_date_basis", "evidence_text", "source"],
      "properties": {
        "evidence_url": {"$ref": "#/$defs/url"},
        "evidence_date": {"$ref": "#/$defs/date"},
        "evidence_date_basis": {"const": "observed_current"},
        "evidence_text": {"type": "string", "minLength": 1},
        "source": {"$ref": "#/$defs/source"}
      }
    },
    "account_fit": {
      "type": "object",
      "additionalProperties": false,
      "required": ["fit_claim", "evidence_url", "evidence_date", "evidence_date_basis", "evidence_text", "source"],
      "properties": {
        "fit_claim": {"type": "string", "minLength": 1},
        "event_date": {"$ref": "#/$defs/source_date", "description": "Optional reviewed date of a dated fit fact."},
        "evidence_url": {"$ref": "#/$defs/url"},
        "evidence_date": {"$ref": "#/$defs/source_date"},
        "evidence_date_basis": {"$ref": "#/$defs/date_basis"},
        "evidence_text": {"type": "string", "minLength": 1},
        "source": {"$ref": "#/$defs/source"}
      }
    },
    "evidence": {
      "type": "object",
      "additionalProperties": false,
      "required": ["url", "date", "date_basis", "text", "source"],
      "properties": {
        "url": {"anyOf": [{"$ref": "#/$defs/url"}, {"type": "null"}], "description": "Null only for a receipt-verified structured attribute; filters and signals need URLs."},
        "date": {"$ref": "#/$defs/source_date"},
        "date_basis": {"$ref": "#/$defs/date_basis"},
        "text": {"type": "string", "minLength": 1},
        "event_date": {"$ref": "#/$defs/source_date", "description": "Reviewed date of the requested activity, separate from publication; YYYY, YYYY-MM or YYYY-MM-DD."},
        "source": {"$ref": "#/$defs/source"}
      }
    },
    "qualification_check": {
      "type": "object",
      "additionalProperties": false,
      "required": ["criterion_id", "criterion", "importance", "status", "claim", "evidence"],
      "properties": {
        "criterion_id": {"type": "string", "minLength": 1, "description": "The id of one saved criterion."},
        "criterion": {"type": "string", "minLength": 1, "description": "Code-copied label of that criterion."},
        "importance": {"enum": ["required", "preferred"]},
        "signal": {"type": "string", "pattern": "\\S", "description": "Code-copied signal kind; present only on signal checks."},
        "status": {"enum": ["pass", "fail", "unknown"]},
        "claim": {"type": "string", "minLength": 1, "description": "The reason for the status."},
        "evidence": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "support": {"enum": ["likely"], "description": "Only on an unknown required trait signal of a held company, with support_reason and quote-checked evidence; absent, the check makes no likely claim."},
        "support_reason": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": "^[^\\r\\n]+$", "description": "With support likely: one line stating the inference from the company's cited facts."}
      }
    },
    "supporting_finding": {
      "type": "object",
      "additionalProperties": false,
      "required": ["kind", "label", "claim", "evidence"],
      "properties": {
        "kind": {"enum": ["signal", "context"]},
        "label": {"type": "string", "pattern": "\\S"},
        "claim": {"type": "string", "pattern": "\\S"},
        "evidence": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/evidence"}}
      }
    },
    "signal_evidence": {
      "type": "object",
      "additionalProperties": false,
      "required": ["criterion_id", "criterion", "signal", "evidence_url", "evidence_date", "evidence_date_basis", "evidence_text", "source"],
      "description": "Derived by code from the first passing check of a required signal kind.",
      "properties": {
        "criterion_id": {"type": "string", "minLength": 1},
        "criterion": {"type": "string", "minLength": 1},
        "signal": {"type": "string", "minLength": 1},
        "evidence_url": {"$ref": "#/$defs/url"},
        "evidence_date": {"$ref": "#/$defs/source_date"},
        "evidence_date_basis": {"$ref": "#/$defs/date_basis"},
        "evidence_text": {"type": "string", "minLength": 1},
        "event_date": {"$ref": "#/$defs/source_date"},
        "source": {"$ref": "#/$defs/source"}
      }
    },
    "accepted_company": {
      "type": "object",
      "additionalProperties": false,
      "required": ["company", "account_fit", "qualification_checks"],
      "properties": {
        "company": {"$ref": "#/$defs/company"},
        "account_fit": {"$ref": "#/$defs/account_fit"},
        "qualification_checks": {"type": "array", "items": {"$ref": "#/$defs/qualification_check"}},
        "signal_evidence": {"$ref": "#/$defs/signal_evidence"},
        "supporting_findings": {"type": "array", "items": {"$ref": "#/$defs/supporting_finding"}}
      }
    },
    "outcome_row": {
      "type": "object",
      "required": ["stage", "reason_code", "reason_text"],
      "properties": {
        "stage": {"enum": ["account", "route"], "description": "account: a held or rejected company; route: a provider outcome, never a company."},
        "reason_code": {"type": "string", "minLength": 1, "description": "account rows: missing_account_evidence, not_icp_fit or explicit_exclusion."},
        "reason_text": {"type": "string", "minLength": 1},
        "candidate": {"$ref": "#/$defs/candidate"},
        "qualification_checks": {"type": "array", "items": {"$ref": "#/$defs/qualification_check"}},
        "account_fit": {"$ref": "#/$defs/account_fit"},
        "signal_evidence": {"$ref": "#/$defs/signal_evidence"},
        "supporting_findings": {"type": "array", "items": {"$ref": "#/$defs/supporting_finding"}},
        "provider_status": {"$ref": "#/$defs/provider_status"},
        "route_id": {"type": "string", "minLength": 1},
        "scope": {"type": "string", "minLength": 1},
        "provider": {"enum": ["deepline", "scrapingdog", "public_web"]},
        "former_scopes": {"type": "array", "items": {"type": "string", "minLength": 1}, "description": "account rows, written by code: earlier claim keys of a parallel worker's claim that moved to the website the company's own LinkedIn record names; the stop check counts the lookups saved under them."}
      }
    },
    "route": {
      "type": "object",
      "required": ["route_id", "phase", "provider", "operation", "paid_calls", "provider_status", "cost_credits", "cost_upper_bound_credits", "cost_basis"],
      "properties": {
        "route_id": {"type": "string", "minLength": 1},
        "phase": {"enum": ["account_discovery", "account_verification"]},
        "hypothesis": {"type": "string", "minLength": 1},
        "provider": {"enum": ["deepline", "scrapingdog", "public_web"]},
        "operation": {"type": "string", "minLength": 1},
        "tool": {"type": "string", "minLength": 1},
        "request_summary": {"type": "string", "minLength": 1},
        "pilot_max_rows": {"type": "integer", "minimum": 1, "maximum": 10},
        "paid_calls": {"type": "integer", "minimum": 0},
        "rows_returned": {"type": "integer", "minimum": 0},
        "rows_usable": {"type": "integer", "minimum": 0},
        "provider_status": {"$ref": "#/$defs/provider_status"},
        "cost_credits": {"type": ["number", "null"], "minimum": 0},
        "cost_upper_bound_credits": {"type": ["number", "null"], "minimum": 0},
        "cost_basis": {"enum": ["actual", "estimated", "unknown"]},
        "cost_usd": {"type": ["number", "null"], "minimum": 0},
        "accepted_leads_before_call": {"type": "integer", "minimum": 0},
        "scope": {"type": "string", "minLength": 1},
        "approach": {"type": "string", "minLength": 1},
        "request_fingerprint": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "progress_before": {"type": "array", "items": {"type": "string"}, "uniqueItems": true},
        "status_read": {"type": "boolean"},
        "entity_type": {"type": "string", "minLength": 1},
        "error": {"type": "string", "minLength": 1}
      }
    },
    "stop_check": {
      "type": "object",
      "additionalProperties": false,
      "required": ["started_at", "next_actions"],
      "properties": {
        "started_at": {"type": "string", "format": "date-time"},
        "closing_seconds": {"type": "integer", "minimum": 0},
        "leads_ready_at": {"type": "string", "format": "date-time"},
        "catalog_review_route_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": true},
        "research_extensions": {"type": "array", "description": "Operator-authorized deadline extensions from --resume-until, in order.",
          "items": {"type": "object", "additionalProperties": false,
                    "required": ["previous_deadline", "deadline", "recorded_at", "authorization"],
                    "properties": {"previous_deadline": {"type": "string", "format": "date-time"},
                                   "deadline": {"type": "string", "format": "date-time"},
                                   "recorded_at": {"type": "string", "format": "date-time"},
                                   "authorization": {"type": "string", "minLength": 1}}}},
        "next_actions": {"type": "array", "items": {"$ref": "#/$defs/next_action"}}
      }
    },
    "next_action": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "scope", "description", "provider", "paid_calls", "cost_upper_bound_credits"],
      "properties": {
        "id": {"type": "string", "minLength": 1},
        "phase": {"enum": ["account_discovery", "account_verification"]},
        "approach": {"type": "string", "minLength": 1},
        "status_read": {"type": "boolean"},
        "operation": {"type": "string", "minLength": 1},
        "tool": {"type": "string", "minLength": 1},
        "request_fingerprint": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "scope": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        "provider": {"enum": ["deepline", "scrapingdog", "public_web"]},
        "paid_calls": {"type": "integer", "minimum": 0},
        "cost_upper_bound_credits": {"type": ["number", "null"], "minimum": 0},
        "entity_type": {"type": "string", "minLength": 1},
        "blocker": {
          "type": "object",
          "additionalProperties": false,
          "required": ["kind", "reason", "evidence_route_id"],
          "properties": {
            "kind": {"enum": ["approval_required", "access_unavailable", "required_input"]},
            "reason": {"type": "string", "minLength": 1},
            "evidence_route_id": {"type": "string", "minLength": 1}
          }
        }
      }
    },
    "route_frontier_item": {
      "type": "object",
      "required": ["route_id", "state", "reason"],
      "properties": {
        "route_id": {"type": "string", "minLength": 1},
        "phase": {"enum": ["account_discovery", "account_verification"]},
        "provider": {"enum": ["deepline", "scrapingdog", "public_web"]},
        "operation": {"type": "string", "minLength": 1},
        "request_summary": {"type": "string", "minLength": 1},
        "scope": {"type": "string", "minLength": 1},
        "approach": {"type": "string", "minLength": 1},
        "request_fingerprint": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "entity_type": {"type": "string", "minLength": 1},
        "status_read": {"type": "boolean"},
        "state": {"enum": ["untried", "continuable", "exhausted", "blocked"]},
        "exhaustion_basis": {"enum": ["no_results", "continuation_exhausted", "no_new_unique_candidates", "query_family_exhausted"]},
        "continuation_route_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": true},
        "reason": {"type": "string", "minLength": 1}
      },
      "allOf": [{"if": {"properties": {"state": {"const": "exhausted"}}, "required": ["state"]}, "then": {"required": ["exhaustion_basis"]}}]
    },
    "stop_audit": {
      "type": "object",
      "required": ["route_frontier"],
      "properties": {
        "target_shortfall": {"type": "integer", "minimum": 0},
        "candidate_companies_reviewed": {"type": "integer", "minimum": 0},
        "substantive_account_reviews": {"type": "integer", "minimum": 0},
        "exclusion_only_rejections": {"type": "integer", "minimum": 0},
        "duplicate_candidates": {"type": "integer", "minimum": 0},
        "frontier_complete": {"const": true},
        "provider_call_capacity": {
          "type": "object",
          "required": ["deepline", "scrapingdog"],
          "properties": {
            "deepline": {"enum": ["available", "unavailable", "unknown"]},
            "scrapingdog": {"enum": ["available", "unavailable", "unknown"]}
          }
        },
        "route_frontier": {"type": "array", "items": {"$ref": "#/$defs/route_frontier_item"}}
      }
    },
    "final_review": {
      "type": "object",
      "additionalProperties": false,
      "required": ["review_ref", "reviewed_at"],
      "properties": {
        "review_ref": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "reviewed_at": {"type": "string", "format": "date-time"},
        "findings": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["target", "source_refs", "finding"],
            "properties": {
              "target": {"type": "string", "minLength": 1},
              "source_refs": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
              "finding": {"type": "string", "minLength": 1}
            }
          }
        }
      }
    },
    "output_budget": {
      "type": "object",
      "additionalProperties": false,
      "required": ["limits", "spent", "paid_calls", "status"],
      "properties": {
        "policy": {"enum": ["actual_cost", "reserved"]},
        "limits": {
          "type": "object",
          "additionalProperties": false,
          "required": ["deepline_credits", "scrapingdog_credits"],
          "properties": {
            "deepline_credits": {"type": ["number", "null"], "minimum": 0},
            "scrapingdog_credits": {"type": ["number", "null"], "minimum": 0},
            "max_paid_calls": {"type": ["integer", "null"], "minimum": 0, "deprecated": true, "description": "Ignored."},
            "max_deepline_credits_per_next_lead": {"type": "number", "minimum": 0}
          }
        },
        "spent": {
          "type": "object",
          "additionalProperties": false,
          "required": ["deepline_credits", "scrapingdog_credits"],
          "properties": {
            "deepline_credits": {"type": ["number", "null"], "minimum": 0},
            "scrapingdog_credits": {"type": ["number", "null"], "minimum": 0}
          }
        },
        "paid_calls": {"type": "integer", "minimum": 0},
        "status": {"enum": ["within_budget", "exhausted", "unknown"]}
      }
    },
    "cost_summary": {
      "oneOf": [{"$ref": "#/$defs/observed_cost_summary"}, {"$ref": "#/$defs/reserved_cost_summary"}]
    },
    "observed_provider_cost": {
      "type": "object",
      "additionalProperties": false,
      "required": ["confirmed_credits", "pending_calls"],
      "properties": {
        "confirmed_credits": {"type": "number", "minimum": 0},
        "confirmed_usd": {"type": "number", "minimum": 0},
        "pending_calls": {"type": "integer", "minimum": 0},
        "documented_tariff_calls": {"type": "integer", "minimum": 0},
        "unknown_charge_calls": {"type": "integer", "minimum": 0},
        "held_credits": {"type": "number", "minimum": 0}
      }
    },
    "observed_cost_summary": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status", "deepline", "scrapingdog"],
      "properties": {
        "status": {"enum": ["calculated", "incomplete"]},
        "deepline": {"$ref": "#/$defs/observed_provider_cost"},
        "scrapingdog": {"$ref": "#/$defs/observed_provider_cost"}
      }
    },
    "reserved_cost_summary": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status", "accepted_companies", "deepline", "scrapingdog", "deepline_cost_per_company_usd"],
      "properties": {
        "status": {"enum": ["exact", "estimated_range", "unknown"]},
        "accepted_companies": {"type": "integer", "minimum": 0},
        "deepline": {
          "type": "object",
          "additionalProperties": false,
          "required": ["usd_per_credit", "confirmed_credits", "maximum_credits", "confirmed_usd", "maximum_usd"],
          "properties": {
            "usd_per_credit": {"const": 0.1},
            "confirmed_credits": {"type": "number", "minimum": 0},
            "maximum_credits": {"type": ["number", "null"], "minimum": 0},
            "confirmed_usd": {"type": "number", "minimum": 0},
            "maximum_usd": {"type": ["number", "null"], "minimum": 0}
          }
        },
        "scrapingdog": {
          "type": "object",
          "additionalProperties": false,
          "required": ["confirmed_credits", "maximum_credits"],
          "properties": {
            "confirmed_credits": {"type": "number", "minimum": 0},
            "maximum_credits": {"type": ["number", "null"], "minimum": 0}
          }
        },
        "deepline_cost_per_company_usd": {
          "type": "object",
          "additionalProperties": false,
          "required": ["minimum", "maximum"],
          "properties": {
            "minimum": {"type": ["number", "null"], "minimum": 0},
            "maximum": {"type": ["number", "null"], "minimum": 0}
          }
        }
      }
    },
    "summary": {
      "type": "object",
      "additionalProperties": false,
      "required": ["target_count", "accepted_companies", "rejected_rows", "unresolved_rows"],
      "properties": {
        "target_count": {"type": "integer", "minimum": 1},
        "accepted_companies": {"type": "integer", "minimum": 0},
        "rejected_rows": {"type": "integer", "minimum": 0},
        "unresolved_rows": {"type": "integer", "minimum": 0}
      }
    }
  }
}
```

### Semantic checks

The validator adds these checks to the schema:

- `criteria` equals the criteria derived from the saved request.
- Every accepted `company.domain` is unique and its `website` resolves to that
  domain; `summary.accepted_companies` equals `len(accepted)`.
- An accepted row has only `company`, `account_fit`, `qualification_checks`,
  `signal_evidence` and `supporting_findings`. Contact fields, an intent
  narrative or any other field make it invalid.
- Each check selects a saved criterion once, and its `criterion`, `importance`
  and `signal` match that criterion. Every required filter, attribute and
  company-size criterion has a `pass` check with evidence; required signal
  coverage holds under `signal_match_mode`; every dated signal has an
  `event_date` whose whole period lies inside its window.
- Every passing required check quotes the captured source body at the cited
  URL, or a field value of this company's receipt-verified record. A search
  snippet or agent-recorded web note cannot qualify. This is a substring
  check: it proves the quote exists, not that it means what the claim says.
- `signal_evidence` is the first passing check of a required signal kind. A
  preferred signal never stands in while the request has a required one.
- A cited LinkedIn company record must be this company's own saved HarvestAPI
  getter result. With `icp.company_size`, a size check that cites that record
  must agree with its range, and one that cites a captured passage quotes whose
  workforce the figure counts. A company's own band decides range fit only when
  the whole band lies inside the bounds, it is current and nothing saved
  conflicts; an overlapping band, an ambiguous entity or conflicting or stale
  evidence stays unknown, and LinkedIn evidence alone (a page, posts or the
  company record) never fails size. A LinkedIn band the size check does not
  cite is not conflicting evidence against a quoted current figure for the
  company's own workforce.
  Two credible current same-company workforce figures or ranges wholly within the
  requested bounds do not conflict merely because the numbers differ;
  check what each counts, and do not compare a LinkedIn member count with a
  headcount band as conflicting workforce figures.
  A network, franchise or group headcount, a
  head office or department count of a company that also employs store staff, or
  a figure from before this entity's restart, bankruptcy, merger, acquisition by
  another company or major downsizing, is not the entity's current size; a LinkedIn band has no as-of
  date, so after this entity's restart, bankruptcy or major downsizing, size
  stays unknown unless a dated later figure agrees.
- An excluded company cannot be accepted. A `not_icp_fit` rejection needs an
  evidenced required failure; `unknown` is unresolved.
- Owner groups are unique among accepted rows. `owner_group` and `aliases` are
  optional, evidence-backed identities for exclusions and deduplication, not
  inferred corporate relationships.
- For a non-signal attribute, a saved Aviato funding record may carry
  `url: null`. Its helper-supplied `source.result_index` selects the raw
  receipt record, and validation requires a successful receipt bound to this
  run and company with the original stage, date and text. Filters, fit and
  signals always need URLs.

### Stopping check

Persist `stop_check.started_at` before discovery and keep it unchanged on
resume. New runs default `request.max_duration_seconds` to null, meaning no
research deadline; an explicit user limit is a positive number of seconds. The
limit covers discovery, retries and verification. At expiry, stop sourcing and
finish the necessary persistence and delivery checks. An already dispatched
provider call cannot be promised cancelled; keep its reservation or result.

Maintain `stop_check.next_actions` separately from historical attempt receipts.
Each entry names a concrete next test with a unique `id`, `description`,
`scope`, `provider`, `paid_calls` and conservative `cost_upper_bound_credits`.
Use `scope: "discovery"` for finding more companies and the canonical domain
for an unresolved company. A company can be parked without another action when
its latest substantive attempt is `ok` or `no_results`, its frontier entry is
`exhausted` with a reason and exhaustion basis, and its `reason_text` records
the remaining gaps. Parking does not qualify or reject a company, hide an
actionable frontier entry or establish whole-run exhaustion. An eligible next
action reopens the company.

The attempt helper adds audit metadata without changing client output:
`scope`, a stable `approach`, `request_fingerprint` and verified
`progress_before` milestones. Two comparable attempts in the same scope and
phase without new milestones require a different approach; a provider switch or
version label is not a strategy change. Request fingerprints protect against
duplicate or uncertain paid calls: use saved receipts and status continuations
instead of resubmitting. Actionable frontier entries keep a matching next
action or continuation.

Before a shortfall, refresh live tool discovery for new companies and missing
evidence, including public and free sources. Do not repeat a failed identical
lookup or spend only to consume the cap. An action that cannot run may carry a
`blocker` (`approval_required`, `access_unavailable` or `required_input`) with
the concrete `reason` and an `evidence_route_id` of a blocking receipt from
this run; that blocks only that action. A successful later call on the same
route or tool invalidates the old error as a stopping reason. Before a budget,
provider or input shortfall, `catalog_review_route_ids` must reference live
catalog-search receipts from this run; one review covers all remaining gaps.
Before recording `approval_required`, apply the
[authorization rules](../SKILL.md#authorization); a change of provider is not
itself a missing approval.

The attempt and review helpers return the current stop decision; use it. Run
`python3 scripts/validate_run.py <results.json> --check-stop` only outside those
helpers or for recovery. The decision uses accepted rows, the current UTC time,
confirmed charges plus uncertain reservations, and each next action's maximum
cost. With an execution ledger it uses the same shared-USD calculation as
dispatch. Decisions:

- `target_met`: the requested company count is reached.
- `time_limit_reached`: the explicit duration expired, or research closed
  `stop_check.closing_seconds` before it so an open model turn can end and
  report usage. The deadline itself does not move. Target takes precedence.
- `continue`: an action fits, discovery or recovery coverage is missing, or a
  price needs resolving. Dispatch only an `eligible_actions` entry; with missing
  coverage, add the concrete free planning or research action first.
- `budget_exhausted`: all covered, unblocked next actions exceed a cap. Never
  exceed a cap first. A free available action prevents this stop.
- `provider_stop` or `input_or_configuration_stop`: all covered next actions
  have evidenced blockers. These are blocked states, not delivery outcomes.
- `no_productive_route` never authorizes current delivery. Exhausted searches
  and empty action queues need another strategy until target, budget or time
  ends research.
- `repair_state`: invalid or missing state. Repair it; this is not an outcome.

Persist each paid action's reservation before dispatch and evaluate again after
its result and on resume. Never reset caps or start times. Final
`validate_run.py` is strict by default: it requires `stop_check` and a matching
`stop_reason` as well as all evidence and budget validation. An empty
next-action list returns `continue`, not completion. These checks validate
recorded actions and receipts; they cannot prove an open-ended market search is
complete.

#### Turn completion and recovery

Treat `continue` as an instruction to run the next useful eligible action in
the current turn, changing source or method when an approach stalls. Use
commentary for intermediate results, including a saved partial workbook; do not
end the turn with a partial delivery or an offer to continue. On
`repair_state`, reconcile the reported errors and check again. Context
checkpoints preserve work; they create no new start time, budget or stop
reason. Dispatch checks only the affected company, so an unrelated draft row
does not block discovery; full delivery validation still checks every row. Do
not remove missing evidence to make a draft pass.

Full validator output includes `stop_decision` and `delivery_allowed`. These are
read-only outputs, not fields to copy into the saved `stop_check`. Only a
passing strict validation sets `delivery_allowed: true`; `--check-stop` alone
never authorizes delivery. The host runs the strict validator after every
agent turn and before publishing. If `delivery_allowed` is false, keep the job
and resume the same run with the decision, errors and next actions. A model's
final message, an exported workbook or a process exit is not job completion.
Preserve the ledger and uncertain calls when recovering a crash or usage limit.
Honor explicit user cancellation and platform limits independently.

### Result semantics

Route and frontier rules:

- `provider_status` belongs to a route or route outcome, never in place of a
  company's state. When accepted companies are below `target_count`,
  `stop_audit` is required, `frontier_complete` is true and the strict stop
  check must permit the stop.
- Every `exhausted` frontier item has a determinate `ok`, `partial` or
  `no_results` receipt in `routes` and an `exhaustion_basis`. A rate limit,
  authentication, quota, timeout, schema, provider or configuration error makes
  a route `blocked`, never `exhausted`. Every `blocked` item has a blocking
  receipt or an unresolved route outcome.
- Every item in `routes`, including a no-cost `public_web` query, has the same
  `route_id` in the frontier. A `route_id` identifies one path: it may appear
  once in `routes`, once among route outcomes and once in `route_frontier`.
- `continuation_route_ids` link an attempt to planned follow-ups.
  `continuation_exhausted` and `query_family_exhausted` need links to terminal
  routes; references exist, are unique and contain no cycles. `no_results`
  needs an actual empty `no_results` receipt.
- `target_shortfall` equals `max(0, target_count - accepted_companies)`.
  Reviewed-company counts use unique domains; `explicit_exclusion` is the only
  exclusion-only reason.

Budget accounting uses actual provider usage, not planning estimates. Output
`paid_calls` equals the sum of route `paid_calls`. A numeric provider `spent`
equals the sum of its numeric route `cost_credits`. If any paid route has an
unknown actual cost, that provider's `spent`, its call capacity and the budget
status are `unknown`. Known spend or paid calls above a hard limit are invalid.

Every route has `cost_credits`, `cost_upper_bound_credits` and `cost_basis`:

- `actual`: both numeric, non-negative and equal: billed usage observed from
  the provider.
- `estimated`: actual credits `null`; the upper bound is a conservative
  estimate covering every paid call on the route.
- `unknown`: both `null`. A separately reported USD charge stays in `cost_usd`.
- No paid call, including public web: `actual` with both values `0`.

Route cost values are totals, not per-call rates. A typical or unconfirmed
price is not an upper bound.

`actual_cost` runs (the default) use `cost_summary.status: calculated|incomplete`
with `confirmed_credits` and `pending_calls` per provider and `confirmed_usd`
for Deepline. The saved ledger is authoritative during execution and includes
dispatched calls not yet in routes. An explicit
`max_deepline_credits_per_next_lead` is a stopping threshold on observed
charges since the last accepted company; unknown billing pauses paid work.
Every paid Deepline route records `accepted_leads_before_call`, the accepted
company count when it was dispatched. At 5 observed credits since the last
accepted company, review the strategy; `--show-progress` derives this
nonblocking warning.

A `reserved` run (explicit `TYCHE_BUDGET_POLICY=reserved`) is a hard
provider-only cap with automatic reservations. Its `cost_summary` is derived
from route fields: confirmed credits sum `actual` routes; maximum credits add
estimated upper bounds and are `null` for a provider with an unknown paid
route; status is `unknown`, `estimated_range` or `exact`. Deepline credits
convert at USD 0.10; `deepline_cost_per_company_usd` divides by accepted
companies, or is `null` at zero. Its optional
`max_deepline_credits_per_next_lead` is a hard cap enforced by the
[paid-call ledger](adapter-io.md#paid-call-budget) before dispatch; route
changes and rejections do not advance the count, and neither demotion nor
re-acceptance erases spend.

To calculate the summary while drafting, run
`scripts/validate_run.py <results.json> --show-cost-summary`.

Direct provider cost is not the full run cost. The launcher saves worker usage
in `model-usage/<invocation-id>.json` and writes `run-costs.json` after
completion from every invocation, including retries and continuations;
`scripts/run_costs.py` recalculates it after reconciliation (see
`docs/codex-isolated-testing.md`). The run cost covers provider calls and
sourcing workers only. The isolated worker uses the cost summary returned by
`tyche_finish`; its own final usage is recorded after it exits, and the outer
caller reports those final costs. Report provider spend, the base LLM estimate,
the combined known total and the per-accepted-company estimate. When a
component is missing, show the known subtotal and mark the total incomplete;
never treat unknown usage as zero. Model estimates are not subscription
charges, and the provider budget does not cap model usage.

## `report.md` and final response

These are reporting requirements, not new `results.json` fields.

### Timing

- `started_at` is persisted from the system clock before the first discovery
  or provider call and preserved across resumptions.
- `leads_ready_at` is recorded in `stop_check` when the target's final accepted
  company is confirmed; it is absent for a shortfall.
- `completed_at` is recorded in `validation.json` after validation, export and
  checks. Elapsed time is wall-clock `completed_at - started_at`, shown in
  minutes and seconds, plus time to `leads_ready_at` when known.
- The report derives timing from these saved timestamps. A resumed run keeps
  its original start; never present a reconstructed interval as full runtime.

### Accepted-company sources

Keep one report row per accepted company, keyed by domain:

| Company / domain | Company discovery | Fit evidence | Signal evidence |
| --- | --- | --- | --- |

For each stage, name the provider, the underlying tool when returned and the
route ID linking to the saved receipt, with the original evidence URL for
public sources. Distinguish the discovery channel (for example a ScrapingDog
Google search) from the publisher that proves the signal (for example a company
release). Deepline is the gateway; name the actual tool. In `tyche_review`,
select `company.discovery_source: {ref}` from the original discovery result;
code keeps the provider, tool, receipt link and source URL. Mark missing
attribution unknown; do not invent it or pay to label it.

Summarize accepted companies first discovered by each channel or tool so the
total reconciles to the accepted count (use unknown where needed). Keep failed
and unused routes and their costs separate from accepted-company contribution.
Counts alone do not establish provider accuracy.

### Final-response checklist

Enter this checklist only after the latest full strict validation returns
`delivery_allowed: true`. Otherwise act on the computed decision.

1. **Result and files:** accepted companies versus target, the workbook and a
   report link for per-company attribution.
2. **Sources used:** the company-discovery tools and the fit and signal evidence
   sources, with accepted-company discovery counts. Name underlying tools, not
   just their gateway; distinguish publishers from discovery tools.
3. **Runtime and full cost:** elapsed time, provider spend, LLM cost, combined
   total and cost per accepted company from saved accounting. Label unknown
   amounts; provider-only spend is not the full cost.
4. **Caveats:** material limitations and any shortfall, or none.

A report link alone does not satisfy Sources used. This is a final-answer
self-check, not another sourcing step.

## LinkedIn company record

`company.linkedin_url` is optional, and code saves it only from this company's
own evidence, never from a search result or a company database row. A company
can be accepted without it.

- **Own site.** At acceptance, code saves the link exactly as captured when
  pages this run captured on the company's own domain or website (every URL
  the capture reports, including a redirect target) name exactly one
  LinkedIn company page. It counts the `sameAs` list of the site's own JSON-LD
  organization at its root, or a link in the page text on the homepage (the
  site root, with no query) or on at least two own pages, counted by host and
  path. A link on one inner page may name a partner, and two different pages
  leave it empty. `linkedin_url_evidence` records the own page
  (`evidence_url`), the passage or JSON-LD record naming the link
  (`evidence_text`), the run's as-of date, `observed_current` and the
  capture's `source` with `result_index`. Validation re-reads the cited
  capture and re-applies the rule to the run's saved pages. Review writes
  neither field from input: it refuses a supplied `linkedin_url_evidence` and
  a supplied `linkedin_url` that neither this rule nor a getter proves. Such a
  link can be a network's or a brand's page, so it never identifies another
  LinkedIn record as this company's; only the getter's own page does.
  `company_links.py --derive <run> <new folder>` writes a labelled copy of a
  finished run's `leads.json` with these two fields bound from its saved
  receipts. It only reads the run, which must be in its saved location.
- **Getter.** Before accepting a company, fetch its record once with the
  HarvestAPI company getter (`harvestapi_get_company` through Deepline, a
  documented 0.03 credits): the `url` of the LinkedIn company page a saved own
  page links, else `search` by the canonical name. Select it as `company.ref`
  when its website is this company's domain. One call per company, never
  retried: a failure, a timeout or another company's record never holds the
  company, and a call that returns with no billing id is counted at its
  documented bound as an unknown charge
  ([provider costs](provider-pricing.md)). When `employee_range` or
  `employee_range_evidence` are saved, with the link, they must match this
  company's own saved getter receipt (`company.ref` at review), whose domain
  is this company's. Review writes them only from `company.ref`. A selected
  getter owns the link. A company can have several LinkedIn pages, so when
  the run's captured own pages name one (by the own-site rule above, or as
  the page of the organisation the site names as its copyright holder), only
  that page's record can be selected; its numeric id, current name, or a
  getter asked for the named page that returns the record under a newer name
  all count as that page. If the named page's record lists another website,
  size stays unknown. Auto-selection skips a refused record with a review
  note, and a record selected before the page naming another was captured has
  its size, link and matching HQ cleared at the company's next review, and a
  size check citing it becomes unknown.

- `employee_range` is LinkedIn's `employeeCountRange` label, such as `11-50` or
  `10001+`; `employeeCount` (associated member profiles) is never used. The
  range counts whatever organisation the LinkedIn page describes, so a
  requested size is decided by its reviewed check. The range decides size only
  when that check cites the record; a range that disagrees with a current
  figure for this company's own workforce, which the check quotes, does not by
  itself make size unknown.
- `employee_range_evidence` uses `evidence_url` (the LinkedIn company page),
  `evidence_date`, `evidence_date_basis: observed_current`, `evidence_text` and
  `source: {provider: deepline, operation: execute, tool, route_id}` of a
  successful getter route. Review acceptance fills these from the matching
  receipt; supplied values must agree.
- `hq_state` and `hq_country` are reviewer-saved from the getter or another
  saved headquarters source, never a press dateline; they are not
  receipt-verified. Unknown values stay blank.

Strict validation and export re-read the captured provider body, not
reviewer-written text, and verify entity, run and request identity without a
provider call. Keep `receipts/<route_id>.json` with the run. Library-only
structural checks without a run path are not a delivery gate; use the full
strict validator CLI before delivery.

## `leads.xlsx` contract

A host that reads `results.json` only (ARETE) binds `workbook: false` in
`request-options.json`. Then `tyche_finish` runs the same strict validation and
saves `validation.json` with the results hash, but writes no workbook or
preview, and strict `delivery_allowed: true` is the delivery. The model never
sets this option.

The workbook has `Companies` and `Sources` worksheets. The first row of
`Companies` is this fixed, ordered 15-column header, and each accepted company
is one row:

```text
Company,Domain,Website,Company LinkedIn,Industry,Sub Industry,HQ State,HQ Country,Company Employee Range,Description,Fit,Signal,Signal Date,Signal Summary,Signals,Review Notes
```

| Workbook column | `results.json` source |
|---|---|
| `Company`, `Domain` | `company.canonical_name`, `company.domain` |
| `Website` | Direct company URL normalized against `company.domain` |
| `Company LinkedIn`, `HQ State`, `HQ Country`, `Company Employee Range` | Optional LinkedIn and headquarters facts, otherwise blank |
| `Industry`, `Sub Industry` | The canonical taxonomy pair |
| `Description` | The passage cited for the company type (else for a required attribute), in quotes |
| `Fit` | Each passing required non-signal check as `criterion: “passage”`, its cited saved passages in quotes |
| `Signal`, `Signal Date`, `Signal Summary` | The initial signal (`signal_evidence`): its kind, its date as known (below) and its cited passage in quotes |
| `Signals` | Every passing signal check and supporting finding with labels, dates and source URLs |
| `Review Notes` | Caveats code writes from the cited passages' metadata (`client_caveats.py`), never the model: date kind, year-only or old signal dates, a size figure's year when the passage ties one to it, a criterion shown only by a registry record or only by sites other than the company's saved website, and brand or partner pages. Blank when code finds none: code checks dates and sources, not meaning. The same text is in `leads.json` `client_caveats` and `report.md` |

Client columns show the cited saved passages, never TYCHE's claims, so they cannot
say more than the sources; the claims stay in `results.json` and `leads.json` as
the review record. `Signals` shows each passing check's passage, one block per
signal and source, labelled `Signal:` or `Context:` for supporting findings; a
supporting finding that repeats a signal's own passage is not shown again. Each
block shows one `Date:`, the date as known, as precise as the source gives: the
reviewed `event_date` (`2024`, `March 2025`, `3 October 2026`); `as of` the
capture for a page live when captured, whose event is dated by the capture or not
at all, and for a dated listing still open on the run's as-of date, with its own
date beside it (`as of 2 October 2026 (published 23 June 2022)`); otherwise `by`
the source date, which only bounds the event. A missing date is omitted. It never
infers event dates. Unknown and failed
checks stay in the audit and never appear as verified activity. The workbook
has no intent narrative column; ARGUS owns that interpretation.

`Sources` columns are
`Company,Domain,Field,Signal,Evidence Date,Date Basis,Observed On,Source URL,Evidence Text`.
They hold the fit, signal, employee-range and qualification-check evidence with
readable excerpts (at most 2,000 characters) and unchanged source URLs. A check
that did not pass is listed as `<criterion> (<status>)`, never as a signal. Select
the supporting passage using the evidence `text`, keeping material qualifiers
and dates. HTML markup and common Markdown are removed only in the export view,
and shortened excerpts are labelled; full evidence stays in the receipts.
`Evidence Date` is the stored publication, posting or update date; for
`observed_current`, it is blank and the observation date is in `Observed On`,
otherwise `Observed On` is the retrieval date. Dates export as typed Excel
dates. A classification note gets an `Industry` row with blank URL and dates.
Receipt-backed funding attributes leave `Source URL` blank and name the
provider, tool and result reference in `Evidence Text`.

Generate the file with `scripts/export_xlsx.mjs` so spelling, order, types and
layout stay deterministic. It uses the harness-provided `@oai/artifact-tool`,
which TYCHE neither installs nor pins. Accepted rows form an Excel table with
filters, hidden gridlines and wrapped long text; unverified optional values are
empty cells. The exporter re-reads the saved workbook and verifies every value
before delivery. Rejected and unresolved rows, route outcomes and provider
receipts stay in `results.json` and `report.md`.

### Client writing and taxonomy

Accept a company as soon as its saved evidence answers the request; there is
no finalization research step and no minimum source count. The confirmation
packet is the company QA pass: check identity, requirement fit, the initial
signal, geography, spelling, grammar, capitalization, contradictions, missing
required values, duplicates, placeholders, truncation and formatting. Check
required numeric claims against the source's entity, metric, units and period;
network or group totals do not establish a company-specific threshold. Repair
actual errors before confirming; trim spaces and avoid em dashes. The exporter
also normalizes em dashes and trailing spaces in display text without changing
raw evidence, receipt-owned identities or URLs.

- Write `company.description` as exactly two factual sentences from verified
  information. Sentence one says what the company does or sells; sentence two
  adds its customers, specialization or another useful business detail. Keep
  signals, inferred needs, scoring and diagnostics out of it.
- Keep the check's `claim` factual: what happened, to whom, with its supported
  date and status (announced, planned, completed, current). Keep source wording
  in evidence. Label dates by what they establish; a posting expiry is not its
  publication date. Current-state observations do not establish duration or
  acceleration, a recap does not renew an old event, and completed or
  historical activity alone does not establish an active or upcoming project
  when the request requires one.
- Save other useful ICP-relevant facts as optional `supporting_findings` with
  `kind: signal|context`, `label`, `claim` and captured `evidence`. They appear
  in Signals and Sources but never count toward required coverage or override a
  failed check. Context is background, not proof of current buying activity.
- Use `assets/leadpoet_industry_taxonomy.json`, a versioned snapshot with pinned
  provenance. Every accepted company needs an `industry`/`sub_industry` pair
  with exact canonical labels and a permitted parent-child relationship; choose
  the evidence-supported parent when a child has several. Classify the
  company's own business activity from evidence, not its customers' industry.
  When the pair cannot be supported, hold the company with the gap in
  `reason_text`. A `classification_note` may explain a pair but never replaces
  it. Raw provider classifications stay in the receipts.

The helpers require these fields and keep the authored text; factual accuracy
is the sourcing agent's review responsibility. The exporter writes no prose,
classifies nothing and infers no dates.

## Downstream handoff

Later stages read the saved run directory; TYCHE calls none of them.

- **Join key.** Every row in `accepted`, `unresolved`, `rejected` and
  `leads.json` is keyed by `company.domain` (`candidate.domain` on held and
  rejected rows). Join by domain, never by name or list position.
- **Criteria.** `criteria` gives each criterion's stable id, kind, label and
  importance. Every qualification check names one by `criterion_id`, so a
  consumer can tell which fit and signal criteria passed, failed or stayed
  unknown, and why (`claim`), without re-parsing labels.
- **Sources.** Each check's `evidence[]` carries the URL, publication date,
  date basis, quoted text, `event_date` for signals and `source.route_id`; the
  receipt `receipts/<route_id>.json` holds the captured body. Quoted text from a
  captured page is at most 1,000 characters on every saved row, so no whole page
  rides along; a structured record (LinkedIn company record, funding round)
  keeps its own saved text, and a record without one is quoted by a field value. A quote matches its source after collapsing each run of
  whitespace to one space and removing soft hyphens and zero-width characters;
  a reader re-checking quotes normalizes both sides the same way.
- **ARGUS** may seed deeper intent research from `company` identity,
  `account_fit` and the signal checks' evidence. TYCHE writes no intent
  narrative.
- **Signal candidates.** With a bound from `request-options.json`,
  `leads.json` `signal_candidates` lists held companies whose fit TYCHE
  established but whose required signals it did not find (unknown, or failed
  such as an event dated outside the window), in the `leads` row shape with
  `hold_reason`, `open_signals` (each signal's status, claim and event dates)
  and `client_caveats`. A caller may send them for signal
  research and decide them with its findings; TYCHE never qualified them. A check
  on an open trait signal may carry `support: "likely"` and a one-line
  `support_reason`, the inference from the company's cited facts; the signal
  stays open and unconfirmed.
- **Email domain.** `company.domain` is the website key. When the correct entity
  is a central company behind a network brand, the worker captures the brand or
  central site's legal notice or contact page with the identity checks. When it
  shows the central's own role address at another domain, a supporting finding
  of kind `context` labelled `Email domain` gives that domain (written plainly
  in `claim`), citing the captured legal notice, contact page or registry. A
  missing notice does not hold the company. A brand relationship alone does not prove an email
  domain, and no person's name or address is quoted.
- **DEMOS** receives the accepted companies (domain, name, LinkedIn URL when
  present, aliases, and any `Email domain` supporting finding) from the caller,
  together with the original buyer and
  contact requirements the caller kept. DEMOS must not reinterpret company fit.

## `report.md` minimum contents

In native runs, `tyche_finish` renders the audit from saved records plus the
agent's research commentary, and the launcher updates costs after worker usage
closes. The Markdown report is the human audit receipt: the normalized request,
criteria, assumptions and as-of date; signal hypotheses and why routes differ;
every capability search, describe and execute receipt (without secrets); pilot
limits, rows, duplicates, route cost bases, confirmed and maximum provider
credits, Deepline dollars, cost per accepted company and live provider
statuses; company decisions; all accepted, rejected and unresolved rows with
stable reasons; the route frontier and call capacity; reviewed-company counts;
and the final stop reason. Each accepted row reads `Qualified.` followed by its
code-written caveats, as in `Review Notes`. Do not claim a discovery-candidate endpoint ran, and
never include raw provider payloads or credentials.
