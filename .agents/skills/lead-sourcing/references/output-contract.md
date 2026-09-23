# TYCHE output contract

New runs use `budget.policy: actual_cost`: reported provider charges plus local
base LLM estimates form a soft cutoff. Pending charges stay unknown. ScrapingDog uses documented endpoint
tariffs and separately reported ceiling holds; see its adapter for exceptions. Reservation fields below describe historical version 1
ledgers, which retain their original policy. See [cost policy](provider-pricing.md).

This is the normative, machine-readable contract for one company-sourcing run.
The JSON Schema is draft 2020-12. A run directory is
`reports/<run-id>/` and delivers `report.md`, `results.json`, and `leads.xlsx`.
It also maintains `leads.json` with confirmed companies during research.
Keep provider receipts and the internal `results.json.budget.json` execution
ledger alongside these deliverables; they do not change the result schema.

## Read by phase

This file stays the single normative contract. Do not load it all upfront:
locate the relevant heading and read its section. Native tools own receipt
construction, stored-schema validation and export. Consult raw result-schema
definitions only for a specific unresolved tool error or compatibility work.
All applicable semantic rules still apply.

| When | Sections |
|---|---|
| Before discovery | [Lifecycle invariants](#lifecycle-invariants), [Input contract](#input-contract), [Timing](#timing). |
| Before selecting qualification evidence | [Semantic checks](#semantic-checks), [Accepted-lead sources](#accepted-company-sources). |
| Before accepting and describing a company | [Client writing and taxonomy](#client-writing-and-taxonomy-version-20). |
| Before export and final delivery | [Workbook](#leadsxlsx-contract), [Report minimum contents](#reportmd-minimum-contents), [Final-response checklist](#final-response-checklist); finish [Timing](#timing). |

## Lifecycle invariants

1. Account processing comes first. An accepted account has `account_fit`
   evidence and separate `signal_evidence` for verified intent, and passes every
   required account evidence rule.
2. `accepted`, `rejected`, and `unresolved` are output states. They are not
   provider statuses. A `no_results` provider response is not a rejection;
   `timeout`, quota, authentication, schema, and provider errors are
   unresolved outcomes.
3. Accepted companies are unique by lower-case canonical domain with a leading
   `www.` removed.
4. `target_count` is the completion condition. After account attrition, source
   one replacement from a changed route while the accepted count is short and
   the route frontier is actionable. Do not prefetch or refill by a fixed
   multiplier such as 5x.
5. The route frontier contains paid-provider and public-web paths. Each path is
   `untried`, `continuable`, `exhausted`, or `blocked`. A final shortfall is
   invalid while any path is actionable unless the strict stop check verifies
   an explicit time limit or that no next action fits the budget. Keep unfinished
   routes actionable in those cases; do not relabel them exhausted. A failed or uncertain paid
   call is not retried automatically, but it does not exhaust other paths. The
   frontier is append-only: paths may be added and states updated, but a planned
   or discovered path must not be removed.
6. All dates are ISO calendar dates. A relative provider date may be resolved
   from retrieval time only when the original wording is retained in
   `report.md`; never invent a date.
7. `signal_match_mode` defaults to `any`. For required intent, apply it
   across required entries in `buying_signals`; facts within a query stay
   conjunctive. Apply the [qualification policy](workflow-rules.md#qualification-policy).
   New requests save each signal's `importance` as `required` or `preferred`
   (default required). Keep `kind` stable within the run and use it as the
   qualification check's `signal`; wording belongs in `claim` and evidence.
   Code copies the saved importance and checks required `any`/`all` coverage
   before acceptance and export. Preferred signals never satisfy a required
   alternative. A failed alternative does not reject an `any` request when
   another passes. Existing legacy request metadata is not rewritten on resume.
   Preserve optional hypotheses in the request and mark their existing
   qualification checks preferred; do not silently promote them to must-haves.
   When none are supplied, record a preferred use-case hypothesis in
   `buying_signals`, clearly identified as inferred rather than a user requirement.
   A signal's `min_age_days` and `max_age_days` are measured backwards from
   the effective as-of date. `min_age_days` is optional and defaults to zero;
   when both bounds are present, the minimum must not exceed the maximum.
   Use `max_age_months` instead of `max_age_days` for calendar-month limits,
   with at most one maximum in each signal/shared window. A signal-specific
   maximum overrides the shared maximum, including its unit. Month subtraction
   clamps to the last day of the destination month and uses the saved as-of date.
   Omit unrequested age bounds; an empty `time_window` means no shared limit.
   Native start supplies that empty object when omitted. Current-state wording
   still requires current evidence; an unspecified window does not prove a claim.
8. Each independent must-have needs its own `required_attributes` entry and
   evidence check. Preserve OR alternatives and the scope of exceptions; do not
   merge separate exclusions into one pass. A negative exclusion needs a targeted
   public screen with its limits, not a company biography. Supplied prior contrary
   findings must be resolved or the affected requirement remains unknown.
   A `qualification_check` uses `pass`, `fail`, or `unknown`. `unknown`
   means that public evidence is missing or ambiguous; it is never a
   substitute for `fail`. A required check that fails rejects the account. A
   required check that is unknown is unresolved. Preferred checks affect
   ranking and explanation but do not reject an otherwise qualified account.
   Each entry in `request.icp.required_attributes` and each supplied company-type,
   industry or geography filter needs a passing evidence check before acceptance
   or delivery. Alternatives within one filter share one check. Native `requirement_ref`
   values select these filters, additional attributes or signals from the saved request; the helper
   expands them into the existing criterion, signal and importance fields.

## Input contract

The normalized request must validate against this schema. Defaults are noted in
the schema and must be applied before provider work.
Before schema validation, apply the [default run budget](workflow-rules.md#default-run-budget)
when the user omits a spending budget: USD 0.80 times the requested company count,
shared across providers. Convert a conservative allocation to the existing
credit-cap fields; record its USD basis in the report. Explicit budgets override
this default. No new JSON fields are required.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "tyche://lead-sourcing/input-contract/v2",
  "title": "TYCHE company-sourcing request",
  "type": "object",
  "additionalProperties": false,
  "required": ["target_count", "icp", "buying_signals", "time_window", "budget"],
  "properties": {
    "original_text": {"type": "string", "minLength": 1, "description": "Original sourcing request supplied by the launcher and preserved unchanged on resume."},
    "target_count": {"type": "integer", "minimum": 1},
    "max_duration_seconds": {"type": ["integer", "null"], "minimum": 1},
    "icp": {"$ref": "#/$defs/icp"},
    "product_service": {
      "type": "object",
      "additionalProperties": false,
      "required": ["description", "perspective"],
      "properties": {
        "description": {"type": "string", "minLength": 1},
        "perspective": {"enum": ["seller", "target"]}
      }
    },
    "buying_signals": {
      "type": "array",
      "minItems": 1,
      "items": {"$ref": "#/$defs/signal"}
    },
    "signal_match_mode": {
      "enum": ["any", "all"],
      "default": "any"
    },
    "time_window": {"$ref": "#/$defs/time_window"},
    "budget": {"$ref": "#/$defs/input_budget"},
    "run_id": {
      "type": "string",
      "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
    },
    "as_of_date": {"$ref": "#/$defs/date"}
  },
  "$defs": {
    "date": {
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    },
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
        "max_paid_calls": {"type": "integer", "minimum": 0, "deprecated": true, "description": "Legacy metadata only; ignored. Omit in new runs."},
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

Provider credit caps are separate because Deepline and
ScrapingDog units are not interchangeable; a cap of 0 disables that provider.
At least one provider credit cap is required. `hard_stop` is mandatory and
true. Stop limits are monetary budgets and explicit time limits, never call
counts. New runs omit `max_paid_calls` and `paid_calls_remaining`; these legacy
fields remain accepted but are ignored. Catalog search/describe calls are
read-only. Charge each paid call against its spending caps and record the count
in `paid_calls` for audit. Resuming an old ledger preserves every call, charge,
reservation, and monetary limit; its old `max_paid_calls` value has no effect.
If a provider does not expose usage, set its output `spent` and route
`cost_credits` to `null`; never use `0` to mean unknown. New runs use result
schema version `1.2`. Versions `1.0` and `1.1` remain valid for existing artifacts.

## `leads.json` confirmed leads

`tyche_start` initializes an empty confirmed-company snapshot next to `results.json`.
After company enrichment/writing is complete, accepting it through `tyche_review` returns the saved-source evidence packet
for that completed company. Review source meaning, requirements and writing before
approving its current `review_ref` with company-specific `review_findings` in a
separate `tyche_review` call. Approval
validates receipts and qualification, then atomically replaces `leads.json`.
New lookups wait for pending reviews; no separate checkpoint or export call is
needed. The requesting agent reviews the packet; this is not human approval.

The file contains:

| Field | Meaning |
| --- | --- |
| `schema_version` | `tyche.confirmed-leads.v1` |
| `run_id` | The source run's ID; null for legacy results without an ID. |
| `run_fingerprint` | Identifies the source run path. Preserve the original run directory when resuming. |
| `request_fingerprint` | Identifies the unchanged request. |
| `target_count` | Original requested company count. |
| `confirmed_count` | Number of confirmed company rows in `leads`. May be below the target. |
| `updated_at` | UTC timestamp of the last snapshot change. |
| `leads` | Reviewed rows in the native `results.json.accepted` shape: company, evidence and qualification. |
| `review_findings` | Source references and factual findings for the current approved rows; removed when a row changes or is withdrawn. Absent on legacy snapshots. |

Only complete, reviewed companies enter this file. Unfinished candidates remain in
`results.json`. An accepted row changed or withdrawn through review is removed
from the confirmed snapshot; a changed row needs fresh approval to re-enter it.
Unchanged confirmed rows survive later provider errors and unfinished research.
A failed atomic write preserves the last complete file and reports the error;
retry saving after resolving it. An invalid or foreign snapshot is preserved and
reported instead of silently overwritten. Approval retries are idempotent.

Consumers may read this file at any point and use `leads` as the confirmed partial company
list. It does not assert run completion, change the target or bypass final
stopping, accounting, evidence review and workbook checks. The final review also
saves the confirmed JSON.

On an operational block, the launcher and `tyche_finish` export unchanged confirmed
rows to `leads-partial.xlsx`, with Sources and an explicit incomplete Status sheet.
Receipt and qualification checks still apply. Unreviewed, changed or withdrawn
rows are excluded. `validation-partial.json` records the workbook verification,
counts and hashes with `partial: true` and `delivery_allowed: false`. This read-only
export does not reconcile billing, change research, or overwrite the full workbook
or `validation.json`. With no confirmed rows, no partial workbook is produced.
For local recovery use `export_xlsx.mjs <results.json> --partial` with the usual
bundled workspace runtime. Report an export failure without repeating research.

For diagnostic runs with a different results filename, the snapshot is named
`<results-stem>.leads.json` to avoid collisions. The bundled Leadpoet arena adapter
maps these confirmed rows to its submission schema and publishes
`/output/companies.json` as part of review approval. Updating a running arena
still requires deploying a bundle that includes this implementation.

## `results.json` schema

Write one JSON object that validates against this schema. Do not add a second
top-level result list or hide rejected/unresolved rows in a count.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "tyche://lead-sourcing/results/v2",
  "title": "TYCHE company-sourcing results",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "run_id", "retrieved_at", "request", "budget", "routes", "summary", "accepted", "rejected", "unresolved", "stop_reason"],
  "properties": {
    "schema_version": {"const": "2.0"},
    "run_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"},
    "retrieved_at": {"type": "string", "format": "date-time"},
    "final_review": {
      "type": "object",
      "required": [
        "review_ref",
        "reviewed_at"
      ],
      "additionalProperties": false,
      "properties": {
        "review_ref": {
          "type": "string",
          "pattern": "^[a-f0-9]{64}$"
        },
        "reviewed_at": {
          "type": "string",
          "format": "date-time"
        },
        "findings": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": [
              "target",
              "source_refs",
              "finding"
            ],
            "properties": {
              "target": {
                "type": "string",
                "minLength": 1
              },
              "source_refs": {
                "type": "array",
                "minItems": 1,
                "items": {
                  "type": "string",
                  "minLength": 1
                }
              },
              "finding": {
                "type": "string",
                "minLength": 1
              }
            }
          }
        }
      }
    },
    "request": {"$ref": "#/$defs/request_snapshot"},
    "budget": {"$ref": "#/$defs/output_budget"},
    "routes": {"type": "array", "items": {"$ref": "#/$defs/route"}},
    "cost_summary": {"$ref": "#/$defs/cost_summary"},
    "summary": {"$ref": "#/$defs/summary"},
    "accepted": {"type": "array", "items": {"$ref": "#/$defs/accepted_company"}},
    "rejected": {"type": "array", "items": {"$ref": "#/$defs/outcome_row"}},
    "unresolved": {"type": "array", "items": {"$ref": "#/$defs/outcome_row"}},
    "stop_audit": {"$ref": "#/$defs/stop_audit"},
    "stop_check": {"$ref": "#/$defs/stop_check"},
    "stop_reason": {
      "enum": ["target_met", "budget_exhausted", "time_limit_reached", "no_productive_route", "provider_stop", "input_or_configuration_stop"]
    }
  },
  "allOf": [
    {
      "if": {"properties": {"schema_version": {"const": "2.0"}}, "required": ["schema_version"]},
      "then": {
        "required": ["cost_summary"],
        "properties": {
          "routes": {"items": {"required": ["cost_credits", "cost_upper_bound_credits", "cost_basis"]}},
          "accepted": {"items": {"required": ["intent_details", "qualification_checks"], "properties": {"company": {"required": ["description", "industry", "sub_industry"]}}}}
        }
      }
    }
  ],
  "$defs": {
    "date": {
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    },
    "source_date": {
      "type": "string",
      "pattern": "^[0-9]{4}(-[0-9]{2}){0,2}$",
      "description": "Captured publication precision; observed_current requires a full YYYY-MM-DD observation date."
    },
    "url": {
      "type": "string",
      "pattern": "^https?://[^\\s]+$"
    },
    "provider_status": {
      "enum": ["ok", "no_results", "partial", "rate_limited", "auth_failed", "quota_exceeded", "timeout", "schema_error", "provider_error", "config_error"]
    },
    "company": {
      "type": "object",
      "additionalProperties": false,
      "required": ["canonical_name", "domain", "employee_range", "employee_range_evidence"],
      "properties": {
        "canonical_name": {"type": "string", "minLength": 1},
        "domain": {"type": "string", "minLength": 1},
        "website": {"$ref": "#/$defs/url"},
        "linkedin_url": {"$ref": "#/$defs/url"},
        "industry": {"type": "string", "minLength": 1},
        "sub_industry": {"type": "string", "minLength": 1},
        "classification_note": {"type": "string", "pattern": "\\S"},
        "hq_state": {"type": "string", "minLength": 1},
        "hq_country": {"type": "string", "minLength": 1},
        "employee_count": {"type": "integer", "minimum": 0},
        "employee_range": {"type": "string", "minLength": 3},
        "employee_range_evidence": {"$ref": "#/$defs/linkedin_field_evidence"},
        "owner_group": {"type": "string", "minLength": 1},
        "aliases": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "description": {"type": "string", "minLength": 1}
      }
    },
    "linkedin_field_evidence": {
      "type": "object",
      "additionalProperties": false,
      "required": ["evidence_url", "evidence_date", "evidence_date_basis", "evidence_text", "source"],
      "properties": {
        "evidence_url": {"$ref": "#/$defs/url"},
        "evidence_date": {"$ref": "#/$defs/source_date"},
        "evidence_date_basis": {"const": "observed_current"},
        "evidence_text": {"type": "string", "minLength": 1},
        "source": {"$ref": "#/$defs/source"}
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
        "result_index": {"type": "integer", "minimum": 0, "description": "Helper-supplied index of a verified structured funding record when no public source URL exists."}
      }
    },
    "account_fit": {
      "type": "object",
      "additionalProperties": false,
      "required": ["fit_claim", "evidence_url", "evidence_date", "evidence_date_basis", "evidence_text", "source"],
      "properties": {
        "fit_claim": {"type": "string", "minLength": 1},
        "evidence_url": {"$ref": "#/$defs/url"},
        "evidence_date": {"$ref": "#/$defs/source_date"},
        "evidence_date_basis": {"enum": ["published", "posted", "updated", "observed_current"]},
        "evidence_text": {"type": "string", "minLength": 1},
        "source": {"$ref": "#/$defs/source"}
      }
    },
    "evidence": {
      "type": "object",
      "additionalProperties": false,
      "required": ["url", "date", "date_basis", "text", "source"],
      "properties": {
        "url": {"anyOf": [{"$ref": "#/$defs/url"}, {"type": "null"}], "description": "Null only for a receipt-verified structured company attribute; signals still require HTTP/HTTPS source URLs."},
        "date": {"$ref": "#/$defs/source_date"},
        "date_basis": {"enum": ["published", "posted", "updated", "observed_current"]},
        "text": {"type": "string", "minLength": 1},
        "event_date": {"type": "string", "pattern": "^[0-9]{4}(-[0-9]{2}){0,2}$", "description": "Reviewed date/period of the requested activity; separate from source publication. Preserve YYYY, YYYY-MM or YYYY-MM-DD precision."},
        "source": {"$ref": "#/$defs/source"}
      }
    },
    "qualification_check": {
      "type": "object",
      "additionalProperties": false,
      "required": ["criterion", "importance", "status", "claim", "evidence"],
      "properties": {
        "criterion": {"type": "string", "minLength": 1},
        "signal": {"type": "string", "pattern": "\\S"},
        "importance": {"enum": ["required", "preferred"]},
        "status": {"enum": ["pass", "fail", "unknown"]},
        "claim": {"type": "string", "minLength": 1},
        "evidence": {"type": "array", "items": {"$ref": "#/$defs/evidence"}}
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
      "required": ["signal", "evidence_url", "evidence_date", "evidence_date_basis", "evidence_text", "source"],
      "properties": {
        "criterion": {"type": "string", "minLength": 1, "description": "Code-owned reference to the authoritative qualification check."},
        "signal": {"type": "string", "minLength": 1},
        "evidence_url": {"$ref": "#/$defs/url"},
        "evidence_date": {"$ref": "#/$defs/source_date"},
        "evidence_date_basis": {"enum": ["published", "posted", "updated", "observed_current"]},
        "evidence_text": {"type": "string", "minLength": 1},
        "event_date": {"type": "string", "pattern": "^[0-9]{4}(-[0-9]{2}){0,2}$", "description": "Reviewed date/period of the requested activity; separate from source publication. Preserve YYYY, YYYY-MM or YYYY-MM-DD precision."},
        "source": {"$ref": "#/$defs/source"}
      }
    },
    "accepted_company": {
      "type": "object",
      "additionalProperties": false,
      "required": ["company", "account_fit", "qualification_checks", "intent_details"],
      "properties": {
        "company": {"$ref": "#/$defs/company"},
        "account_fit": {"$ref": "#/$defs/account_fit"},
        "signal_evidence": {"$ref": "#/$defs/signal_evidence"},
        "intent_details": {"type": "string", "pattern": "\\S"},
        "qualification_checks": {"type": "array", "items": {"$ref": "#/$defs/qualification_check"}},
        "supporting_findings": {"type": "array", "items": {"$ref": "#/$defs/supporting_finding"}}
      }
    },
    "candidate": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "company": {"type": ["string", "null"]},
        "domain": {"type": ["string", "null"]},
        "signal": {"type": ["string", "null"]},
        "evidence_url": {"anyOf": [{"$ref": "#/$defs/url"}, {"type": "null"}]},
        "provider": {"type": ["string", "null"]},
        "operation": {"type": ["string", "null"]},
        "tool": {"type": ["string", "null"]}
      }
    },
    "reason_code": {
      "enum": ["explicit_exclusion", "not_icp_fit", "stale_signal", "missing_account_evidence", "invalid_evidence_url", "invalid_evidence_date", "search_result_only", "profile_only", "duplicate_domain", "identity_conflict", "company_mismatch", "route_not_connected", "budget_exhausted", "timeout_unknown", "provider_status", "gate_not_reached"]
    },
    "outcome_row": {
      "type": "object",
      "additionalProperties": false,
      "required": ["stage", "reason_code", "reason_text", "candidate"],
      "properties": {
        "stage": {"enum": ["account", "route"]},
        "reason_code": {"$ref": "#/$defs/reason_code"},
        "reason_text": {"type": "string", "minLength": 1},
        "candidate": {"$ref": "#/$defs/candidate"},
        "qualification_checks": {"type": "array", "items": {"$ref": "#/$defs/qualification_check"}},
        "supporting_findings": {"type": "array", "items": {"$ref": "#/$defs/supporting_finding"}},
        "provider_status": {"$ref": "#/$defs/provider_status"},
        "route_id": {"type": "string", "minLength": 1},
        "scope": {"type": "string", "minLength": 1},
        "provider": {"enum": ["deepline", "scrapingdog", "public_web"]}
      }
    },
    "route": {
      "type": "object",
      "additionalProperties": false,
      "required": ["route_id", "phase", "hypothesis", "provider", "operation", "request_summary", "pilot_max_rows", "paid_calls", "rows_returned", "rows_usable", "provider_status", "cost_credits"],
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
        "catalog_review_route_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": true},
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
      "additionalProperties": false,
      "required": ["route_id", "phase", "provider", "operation", "request_summary", "state", "reason"],
      "properties": {
        "status_read": {"type": "boolean"},
        "scope": {"type": "string", "minLength": 1},
        "approach": {"type": "string", "minLength": 1},
        "request_fingerprint": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "entity_type": {"type": "string", "minLength": 1},
        "route_id": {"type": "string", "minLength": 1},
        "phase": {"enum": ["account_discovery", "account_verification"]},
        "provider": {"enum": ["deepline", "scrapingdog", "public_web"]},
        "operation": {"type": "string", "minLength": 1},
        "request_summary": {"type": "string", "minLength": 1},
        "state": {"enum": ["untried", "continuable", "exhausted", "blocked"]},
        "exhaustion_basis": {"enum": ["no_results", "continuation_exhausted", "no_new_unique_candidates", "query_family_exhausted"]},
        "continuation_route_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": true},
        "reason": {"type": "string", "minLength": 1}
      },
      "allOf": [
        {
          "if": {"properties": {"state": {"const": "exhausted"}}, "required": ["state"]},
          "then": {"required": ["exhaustion_basis"]}
        }
      ]
    },
    "provider_call_capacity": {
      "type": "object",
      "additionalProperties": false,
      "required": ["deepline", "scrapingdog"],
      "properties": {
        "deepline": {"enum": ["available", "unavailable", "unknown"]},
        "scrapingdog": {"enum": ["available", "unavailable", "unknown"]},
        "paid_calls_remaining": {"type": ["integer", "null"], "minimum": 0, "deprecated": true, "description": "Legacy metadata only; ignored. Omit in new runs."}
      }
    },
    "stop_audit": {
      "type": "object",
      "additionalProperties": false,
      "required": ["target_shortfall", "candidate_companies_reviewed", "substantive_account_reviews", "exclusion_only_rejections", "duplicate_candidates", "frontier_complete", "provider_call_capacity", "route_frontier"],
      "properties": {
        "target_shortfall": {"type": "integer", "minimum": 0},
        "candidate_companies_reviewed": {"type": "integer", "minimum": 0},
        "substantive_account_reviews": {"type": "integer", "minimum": 0},
        "exclusion_only_rejections": {"type": "integer", "minimum": 0},
        "duplicate_candidates": {"type": "integer", "minimum": 0},
        "frontier_complete": {"const": true},
        "provider_call_capacity": {"$ref": "#/$defs/provider_call_capacity"},
        "route_frontier": {"type": "array", "items": {"$ref": "#/$defs/route_frontier_item"}}
      }
    },
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
        "exclusions": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "custom_criteria": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
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
        "max_paid_calls": {"type": "integer", "minimum": 0, "deprecated": true, "description": "Legacy metadata only; ignored. Omit in new runs."},
        "max_deepline_credits_per_next_lead": {"type": "number", "minimum": 0},
        "hard_stop": {"const": true}
      },
      "anyOf": [
        {"required": ["deepline_credits"]},
        {"required": ["scrapingdog_credits"]}
      ]
    },
    "request_snapshot": {
      "type": "object",
      "additionalProperties": false,
      "required": ["target_count", "icp", "buying_signals", "time_window", "budget"],
      "properties": {
        "original_text": {"type": "string", "minLength": 1, "description": "Original sourcing request supplied by the launcher and preserved unchanged on resume."},
        "target_count": {"type": "integer", "minimum": 1},
        "max_duration_seconds": {"type": ["integer", "null"], "minimum": 1},
        "icp": {"$ref": "#/$defs/icp"},
        "product_service": {
          "type": "object",
          "additionalProperties": false,
          "required": ["description", "perspective"],
          "properties": {
            "description": {"type": "string", "minLength": 1},
            "perspective": {"enum": ["seller", "target"]}
          }
        },
        "buying_signals": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/signal"}},
        "signal_match_mode": {"enum": ["any", "all"], "default": "any"},
        "time_window": {"$ref": "#/$defs/time_window"},
        "budget": {"$ref": "#/$defs/input_budget"},
        "run_id": {"type": "string"},
        "as_of_date": {"$ref": "#/$defs/date"}
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
            "max_paid_calls": {"type": ["integer", "null"], "minimum": 0, "deprecated": true, "description": "Legacy metadata only; ignored. Omit in new runs."},
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
    "provider_credit_cost": {
      "type": "object",
      "additionalProperties": false,
      "required": ["confirmed_credits", "maximum_credits"],
      "properties": {
        "confirmed_credits": {"type": "number", "minimum": 0},
        "maximum_credits": {"type": ["number", "null"], "minimum": 0}
      }
    },
    "deepline_cost": {
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
    "cost_per_lead": {
      "type": "object",
      "additionalProperties": false,
      "required": ["minimum", "maximum"],
      "properties": {
        "minimum": {"type": ["number", "null"], "minimum": 0},
        "maximum": {"type": ["number", "null"], "minimum": 0}
      }
    },
    "cost_summary": {
      "oneOf": [{"$ref": "#/$defs/observed_cost_summary"}, {"$ref": "#/$defs/legacy_cost_summary"}]
    },
    "observed_provider_cost": {
      "type": "object",
      "additionalProperties": false,
      "required": ["confirmed_credits", "pending_calls"],
      "properties": {
        "confirmed_credits": {"type": "number", "minimum": 0},
        "confirmed_usd": {"type": "number", "minimum": 0},
        "pending_calls": {"type": "integer", "minimum": 0}
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
    "legacy_cost_summary": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status", "accepted_leads", "deepline", "scrapingdog", "deepline_cost_per_lead_usd"],
      "properties": {
        "status": {"enum": ["exact", "estimated_range", "unknown"]},
        "accepted_leads": {"type": "integer", "minimum": 0},
        "deepline": {"$ref": "#/$defs/deepline_cost"},
        "scrapingdog": {"$ref": "#/$defs/provider_credit_cost"},
        "deepline_cost_per_lead_usd": {"$ref": "#/$defs/cost_per_lead"}
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

### Stopping check

For every current run, persist `stop_check.started_at` before discovery and keep
it unchanged on resume. New runs default `request.max_duration_seconds` to null (no research deadline).
An explicit user limit uses a positive number of seconds; no deadline uses
null. Older saved requests without a limit keep their original contract on resume.
The limit includes discovery,
retries and qualification, not just paid tool execution. At expiry, stop sourcing
and finish the necessary persistence and delivery checks. Never promise that an
already dispatched provider call can be cancelled; retain its reservation/result.

Maintain `stop_check.next_actions` separately from historical attempt receipts.
Each entry names a concrete, useful next test, with a unique `id`, `description`,
`scope`, `provider`, `paid_calls` and conservative `cost_upper_bound_credits`.
Use `scope: "discovery"` for finding additional companies and the canonical
domain for each unresolved account (or its normalized `name:` key when no domain
exists). Cover discovery and active company checks. A company can be
parked without another action when its latest substantive attempt is `ok` or
`no_results`, its frontier entry is `exhausted` with a reason and exhaustion
basis, and every unresolved `reason_text` for it records the remaining gaps.
Account gaps require an account discovery/verification review. Retain all
receipts and qualification checks. Parking does not qualify or reject a company,
hide an actionable frontier entry, or establish whole-run exhaustion.
Remove speculative follow-ups; reopen for a concrete new source, verification
target or next phase, explained in the action description. An eligible next
action reopens the company; completing a receipt alone does not exhaust it.

The attempt helper adds optional audit metadata without changing client output:
`scope`, stable `approach`, `request_fingerprint`, and verified `progress_before`
milestones. Two comparable research attempts within the same scope and phase
without new verified milestones require a different approach. A provider switch
or version label such as `-v117` is not a strategy change. Other companies' progress
does not reset a company's research. Distinct company evidence targets and advancement to another phase remain
eligible without renaming the approach.
Request fingerprints still protect against duplicate or uncertain paid calls;
use saved receipts and status continuations rather than resubmitting them.
Actionable frontier entries must retain a matching next action or continuation;
covering only the generic discovery scope cannot hide an untried research path.

Before a shortfall, refresh live tool discovery for both new companies and
missing evidence. Include public/free alternatives, not only paid providers.
Do not repeat a failed identical lookup, spend solely to consume the cap, or
invent a blocker from a missing headcount or rejected company.
Try another relevant identifier, source family, or company as appropriate.

An action that cannot run may carry `blocker` with `kind` (`approval_required`,
`access_unavailable`, or `required_input`), the concrete `reason`, and an
`evidence_route_id` referencing a blocking attempt or route-outcome receipt.
That blocks only that action. A whole-run blocker requires coverage of all
remaining discovery/recovery scopes after alternative-source review. Unknown
prices require a free price-discovery action, not an invented budget failure.
Provider budget allocations may be changed only under the existing shared-cap
rules; lack of allocation to an otherwise useful provider is not tool exhaustion.

Blocker receipts must match the action's provider, scope and tool when specified.
A successful later call on that same route/tool or its continuation invalidates
the old error as a stopping reason. Before a budget/provider/input shortfall,
`catalog_review_route_ids` must reference live catalog search attempt receipts
from this run; reuse an applicable saved review. One discovery review can cover
all remaining evidence gaps; do not repeat identical catalog queries per company.
Recovery actions and blockers still need company-specific coverage.
The helper tags catalog calls `entity_type: "tool_catalog"`. Review the returned
capabilities and add useful alternatives; a catalog receipt is not itself proof
that alternatives were exhausted. If catalog access fails, preserve the failure
and review other accessible sources; an outage must not force endless refreshes
or block another provider. Target/time stops do not require another search.

Company `owner_group` and `aliases` are optional, evidence-backed identities for
exclusions and deduplication, not inferred corporate relationships. Required
qualification checks marked unknown remain account-unresolved. A current
`not_icp_fit` rejection needs an evidenced failed required check.

Before recording `approval_required`, apply the
[authorization rules](../SKILL.md#authorization) and check the current request,
prior user approvals, and trusted application job context. A change of provider is not itself a missing approval. Cite the exact applicable
instruction or actual runtime denial and preserve its source in the report;
an agent-authored blocker is not proof of a platform refusal. On resume, remove
resolved blockers from next actions while retaining historical receipts. A provider denial does not block other permitted company research. The validator checks the recorded blocker structure and references; it
cannot independently verify user authorization or a runtime denial.

The attempt and review helpers return the current stop decision; use that result.
Run `python3 scripts/validate_run.py <results.json> --check-stop` only for work
outside those helpers or recovery. Draft results are allowed; budget/cost receipts must reconcile. The
decision uses qualified accepted rows, the actual current UTC time, confirmed
charges plus uncertain reservations, and each next action's maximum cost/calls.
When an execution ledger is present, this uses the same shared-USD and
verification-allowance calculation as dispatch. Eligibility is a snapshot;
the adapter must still reserve atomically before sending the call. Decisions:

- `target_met`: requested company count reached.
- `time_limit_reached`: the saved default or user-specified duration expired, or research
  has closed `stop_check.closing_seconds` before it so an open model turn can end and report
  its usage. The deadline itself does not move. Target takes precedence if already reached.
  Report any shortfall and unfinished routes.
- `continue`: at least one action fits, discovery/recovery coverage is missing,
  or pricing needs resolution. Dispatch only an `eligible_actions` entry. With
  missing coverage/prices, add the concrete free planning/research action first.
- `budget_exhausted`: all covered, unblocked next actions exceed an applicable
  cap. Never exceed a cap first. A free available action prevents this stop.
- `provider_stop` or `input_or_configuration_stop`: all covered next actions
  have evidenced concrete blockers; request only what is needed to unblock them.
  These are blocked states, not delivery outcomes.
- `no_productive_route` is retained only for read-only historical audits. It
  cannot authorize current delivery. Exhausted searches, diminishing returns
  and empty action queues require another strategy until target, budget or time
  ends research. Do not manufacture expensive actions to claim budget exhaustion.
- `repair_state`: invalid/missing state. Repair it; this is not a sourcing outcome.

After choosing an eligible paid action, persist its reservation before dispatch.
Evaluate again after its result and on resume. Do not reset caps or start times.
Final `validate_run.py` is strict by default: it requires `stop_check` and a
matching `stop_reason`, in addition to all evidence and budget validation.
The schema keeps `stop_check` optional solely for old reports;
`--legacy-stop-policy` is for read-only historical audits, never current delivery.
An empty next-action list returns `continue`, not completion.
Checks validate recorded actions and receipts; they cannot prove completeness of
an open-ended market search. The agent must still honestly discover alternatives
and substantiate blockers, rather than manipulate labels to obtain a passing result.

#### Turn completion and recovery

Treat `continue` as an instruction to execute the next useful eligible action within
the current turn, choosing a different source or method when an approach stalls.
Use commentary for intermediate results, including a saved
partial workbook; do not end the turn with a partial delivery or an offer to
continue. When the decision is `repair_state`, reconcile the reported errors
and run the check again. A correct explanation of a failed stopping check does
not satisfy it. Context checkpoints preserve work so execution can resume;
they do not create a new start time, budget, or stop reason.

Dispatch validates the affected company's account gate before acceptance;
an unrelated draft row must not block discovery, catalog reads or another
qualified company. Full delivery validation still checks every row, all accepted
company requirements, exclusions, receipts and financial accounting. Do not remove
missing evidence merely to make a draft pass.

Full validator output includes `stop_decision` (the computed decision and eligible
actions) and `delivery_allowed`. These are read-only CLI outputs, not new fields
to copy over the saved `stop_check`. Only a passing strict validation may set
`delivery_allowed: true`. `--check-stop` alone does not authorize final delivery;
its zero exit code means the decision was computed successfully, including
when that decision is `continue`. Legacy validation never authorizes delivery.

For client jobs, the host must run the full strict validator after every agent
turn and before publishing artifacts. If `delivery_allowed` is false, retain
the job and resume the same session with the validator decision, errors, and
saved next actions. Reconcile missing or invalid state before further spending.
Do not interpret the model's final message, an exported workbook, or a successful
process exit as job completion. Preserve the ledger and uncertain calls when
recovering a process crash or usage limit; use the host's interruption or error
state when it cannot resume. The skill cannot itself restart a terminated host.
Honor explicit user cancellation and platform limits independently of sourcing.

### Result semantics

The following semantic checks supplement JSON Schema: every signal's
`min_age_days` must be no greater than its `max_age_days` when both are
present; each accepted account domain must be unique; `account_fit` must support
ICP fit; when the request requires intent, `signal_evidence` must support a
requested signal under `signal_match_mode` and its applicable bounds. When
intent is optional (every saved signal explicitly preferred), missing intent
does not block qualification or export. Leave `signal_evidence` absent when no
requested signal is verified. `Signals` may still contain sourced supporting
findings, with background facts labeled as context; leave it empty when neither
is available. Describe any conditional use case in `intent_details`, grounded
in `account_fit` evidence and explicitly identified as inference. Do not create
a signal from a hypothesis. Legacy request metadata and receipts remain
unchanged. If an old signal label differs from a requested kind, explicitly map
that existing check through `requirement_ref` and review its evidence; never
guess synonyms or bypass current source and date checks. Never use this fallback
for a required signal. Evidence URLs and sources may differ. Follow the
[qualification policy](workflow-rules.md#qualification-policy) to corroborate
the same project across sources: use the dated activity in `signal_evidence`,
retain technical corroboration in the relevant
`qualification_checks[].evidence` arrays, and explain the linkage in
`intent_details`. Keep each source's own date and attribution.
When
`qualification_checks` is present, required checks with `fail` reject the
account, required checks with `unknown` make it unresolved, and preferred
checks do not reject an account. A check with `unknown` must not be rewritten
as `fail` merely because no source was found.
`provider_status` belongs to a route or outcome receipt, never in place of
`state`. When accepted companies are below `target_count`, `stop_audit` is
required and `frontier_complete` must be true. The strict stopping check must
permit the stop; a time/budget limit may leave unfinished routes actionable.
Every `exhausted`
frontier item must have a determinate `ok`, `partial`, or `no_results` attempt
receipt in `routes` and an `exhaustion_basis`. A rate limit, authentication,
quota, timeout, schema, provider, or configuration error makes a route
`blocked`, never `exhausted`. Every `blocked` item must have either a blocking
attempt receipt or an unresolved route-outcome receipt;
this records routes that cannot start because of budget, connection, or
configuration. Every item in `routes`, including a no-cost `public_web` query,
must have the same `route_id` in the frontier. `route_id` identifies one route path:
it may appear once in `routes`, once among stage=`route` outcomes, and once in
`route_frontier`. A route receipt and a route outcome may share an ID only when
the receipt itself has a blocking provider status and the outcome records that
same failed attempt. A determinate `ok`, `partial`, or `no_results` receipt must
not share its ID with a later blocked continuation; that continuation needs a
new route ID. Reuse for separate attempts is invalid. `target_shortfall`
equals
`max(0, target_count - accepted_companies)`. Reviewed-company counts use unique
canonical domains, or normalized company names when a domain is not yet known;
`explicit_exclusion` is the only exclusion-only reason. Budget availability
alone is not authority to call a blocked tool; one tool's blocker is not a reason
to end other research. Run `scripts/validate_run.py` against the completed
`results.json` to enforce the stopping check and completion rules. This completion
validator supplements rather than replaces validation against the JSON Schema
and the other semantic checks above.

Use `continuation_route_ids` to link an attempt to separately planned follow-ups.
`continuation_exhausted` and `query_family_exhausted` require nonempty links to
terminal routes; references must exist, be unique and contain no cycles.
An exhausted parent cannot have an actionable follow-up. `no_results` requires
an actual empty `no_results` receipt, not a generic review note. These checks
establish consistency, not market exhaustion. Review the evidence and remaining
promising companies before declaring the frontier complete.

Budget accounting uses actual provider usage, not planning estimates. Output
`paid_calls` must equal the sum of route `paid_calls`. For each provider, a
numeric `spent` value must equal the sum of its numeric route `cost_credits`.
If any paid route has unknown actual cost, that provider's `spent` value, its
call capacity, and the overall budget status must be `unknown`. This remains
true when a conservative upper bound is available. `within_budget` requires
known actual spend for both providers. Known spend or paid calls above a hard
limit are invalid.

For new actual-cost runs, an explicit `max_deepline_credits_per_next_lead`
is a stopping threshold on observed charges. Calls already running can exceed it;
unknown billing pauses paid work. No money is reserved for future research.

For historical version 1 ledgers, the optional
`max_deepline_credits_per_next_lead` is a hard cap only when explicitly requested. Do not insert it by default or change saved caps. When active, every paid Deepline route must
record the non-negative `accepted_leads_before_call` count. Sum each route's
actual `cost_credits`, or its `cost_upper_bound_credits` when
`cost_basis` is `estimated`, by that count. A paid Deepline route with unknown
cost cannot prove the allowance and is invalid while the guard is active. The
sum at each dispatch must include all earlier costs at that count or higher
and must not exceed the configured allowance. Route changes,
rejections, and failed lookups do not change the count; only a fully accepted
company advances it. Before a paid Deepline execution, the agent must add its conservative cost upper bound to
the amount already charged at the current count or higher and must not run the call if
the sum would exceed the allowance. The shared
[paid-call ledger](adapter-io.md#paid-call-budget) enforces this cap before
dispatch; post-run validation independently checks the recorded charges.
Review may reduce the accepted count below historical snapshots. Preserve
those snapshots and all costs; neither demotion nor reacceptance erases spend.
Recompute the remaining allowance without retroactively invalidating calls
that were affordable when made. The output
limit, when present, must match the request limit. Artifacts without this
optional field remain valid for backward compatibility.

Record the retained `accepted_leads_before_call` ledger field on every paid
Deepline route even without that cap; it counts accepted companies in active
company-only runs. At 5 observed credits since the last complete lead, review the strategy;
this is a nonblocking warning. New runs stop on known provider charges plus
locally captured base LLM estimates. Old ledgers retain hard reservation limits. `--show-progress` on
`validate_run.py` derives this warning without modifying the run or its verdict.
Unmarked or unbounded costs are reported as incomplete, never zero.

The same progress output separates unresolved account evidence and
provider/route failures using the existing stage and reason fields. It
deduplicates companies. Account-stage rows must retain their
`qualification_checks`; their `reason_text` must say what company evidence is
missing and the next action or concrete blocker. The agent uses these groups in
the report; only complete accepted companies enter `leads.xlsx`.

Every version `1.1` or `1.2` route has `cost_credits`,
`cost_upper_bound_credits`, and `cost_basis`. Use these combinations:

- `actual`: actual and upper-bound credits are numeric, non-negative, and
  equal. This means billed usage observed from the provider, not a planned
  price; the route is the usage receipt.
- `estimated`: actual credits are `null`; upper-bound credits are a numeric,
  non-negative conservative estimate for every paid call recorded on that
  route.
- `unknown`: actual and upper-bound credits are both `null`. A separately
  reported USD charge is retained in `cost_usd`; unknown credits do not erase it.
- No paid call, including a public-web route: use `actual` with both values set
  to `0`.

New actual-cost runs use `cost_summary.status: calculated|incomplete`, with
`confirmed_credits` and `pending_calls` per provider and `confirmed_usd` for
Deepline. This route summary contains no projected maximum. The saved ledger is
authoritative during execution; it also includes dispatched calls not yet in
routes. `run-costs.json` adds model usage and the saved ScrapingDog conversion.

For historical ledgers, version `1.1` and `1.2` `cost_summary` is derived only
from route fields. Confirmed
credits sum `actual` routes. Maximum credits sum actual costs and estimated
upper bounds; the maximum is `null` for a provider with any `unknown` paid
route. The overall status is `unknown` if any paid route is unknown,
`estimated_range` if at least one paid route is estimated, and `exact`
otherwise. Convert Deepline credits at the configured fixed rate of `$0.10`
per credit. Keep ScrapingDog in credits because no dollar rate is configured.
For active company-only runs, the denominator is `summary.accepted_companies`;
divide Deepline dollars by that value for cost per accepted company, or use
`null` when it is zero. Historical artifacts retain their saved field names. Round derived
dollar values to four decimal places. OpenRouter is not used by this skill, and
Codex model cost is not part of direct provider cost.

Direct provider cost is not the full run cost. The outer launcher saves numeric
worker usage in `model-usage/<invocation-id>.json` beside each request file.
After completion, it automatically writes `run-costs.json` using every
invocation, including sourcing retries and continuations. The repository-root
`scripts/run_costs.py` can recalculate it after reconciliation, as described in
`docs/codex-isolated-testing.md`. The run cost includes provider calls and
sourcing workers only; exclude outer chat, monitoring and development costs.
The isolated worker uses the cost summary returned by `tyche_finish`; its final
model usage cannot exist until it exits. Do not inspect live usage-event files
or try to complete that accounting from inside the worker. The launcher refreshes
the saved report afterward, and the outer caller reports those final run costs.
Report the separate components, combined
base LLM estimate, combined known total and per-accepted-lead estimate. If any component
is missing, show the known subtotal and mark the full total incomplete; do not
price a bare `tokens used` footer or treat unknown usage as zero. Model estimates
are not actual subscription/credit charges. The provider budget does not cap
model usage.

Route cost values are totals, not per-call rates. When `paid_calls` is greater
than one, both cost fields cover all paid calls represented by that route.
A typical, midpoint, or unconfirmed price is not an upper bound; use `unknown`
when the full route cannot be conservatively bounded. In versions `1.1` and `1.2`, a
known provider `maximum_credits` must not exceed the matching
`budget.limits.<provider>_credits`; an unknown maximum remains allowed under
the existing unknown-spend rules.

To calculate the expected block while drafting, run
`scripts/validate_run.py <results.json> --show-cost-summary`, copy
`calculated_cost_summary` to the top-level `cost_summary`, and run the validator
again without the flag. The final report must use those same values and label
them as exact, estimated range, or unknown.

For a legacy version `1.0` route without `cost_basis`, `--show-cost-summary`
keeps any paid cost unclassified and unknown. It does not promote a numeric
legacy `cost_credits` value to confirmed actual usage. Migrate the route to
version `1.1` cost fields before reporting confirmed or estimated cost.

## `report.md` and final response

For every new run, include the following in `report.md`. These are reporting
requirements, not new `results.json` fields or workbook columns.

### Timing

- Persist `started_at` from the system clock before the first discovery or
  provider call. Use an ISO 8601 timestamp with a timezone. Preserve it across
  resumptions; do not infer it from file creation/modification or `retrieved_at`.
- Record `leads_ready_at` when the target's final fully qualified company is
  saved. Leave it unavailable for a shortfall. This is distinct from total run
  completion.
- Record `completed_at` after result validation, workbook export and checks.
  Calculate `elapsed_seconds = completed_at - started_at` and display total
  elapsed time in minutes/seconds. Also show time to `leads_ready_at` when known.
  Elapsed time is wall-clock time, including waits and pauses, not CPU time.
- Native tools record `leads_ready_at` in the existing `stop_check` and the
  exporter records `completed_at` in `validation.json`. The report derives its
  timing from these saved timestamps; the LLM does not calculate them manually.
- A resumed run keeps its original start. If the start was not recorded, say
  runtime is unavailable or explicitly label a known-interval estimate with its
  boundaries. Never present a reconstructed interval as full runtime.

### Accepted-company sources

Maintain one report row per final accepted company, keyed by canonical domain:

| Company / domain | Company discovery | Fit evidence | Intent evidence |
| --- | --- | --- | --- |

For each stage, identify the provider, underlying tool when returned, and route
ID linking to the saved receipt. Include the original evidence URL for public
sources. Distinguish the discovery channel (for example ScrapingDog Google
search) from the publisher that proves intent (for example a company release).
Deepline is the gateway; name the actual company tool when known. Mark missing
attribution unknown; do not invent it or make another paid call solely to label
it.

In `tyche_review`, select `company.discovery_source: {ref}` from the original
account-discovery result. Code preserves the selected result index,
provider/tool, receipt link and source URL. Reuse these saved fields when
patching a company.

For a non-signal check of a requested company attribute, a saved Aviato funding
result may have `url: null`. Its helper-supplied `source.result_index` selects the
raw receipt record. Validation requires a successful receipt bound to this run
and company, with the original funding stage, announcement date and text. Put
interpretation in `claim`; code does not decide whether the stage satisfies the
request. This exception does not apply to account-fit or signal evidence.

Summarize the number of accepted companies first discovered by each channel/tool.
Assign one original discovery source per company so the total reconciles to the
accepted count (use unknown where needed). Evidence counts may overlap and must
be labeled accordingly. Keep failed/unused routes and their costs separate from
accepted-company contribution. Counts alone do not establish provider accuracy
or comparative yield without the corresponding attempted-candidate denominator.

### Final-response checklist

Enter this checklist only after the latest full strict validation returns
`delivery_allowed: true`. If it returns false, act on the computed decision and
keep intermediate updates in commentary. Do not turn validation failure into
a final-answer caveat.

Before sending the final chat response, verify it contains all four items:

1. **Result and files:** accepted count versus target, workbook delivery and a
   report link for per-company attribution.
2. **Sources Used:** name the company-discovery tools and fit/signal evidence
   sources directly in chat. Include accepted-company discovery counts. Name
   underlying tools, not just their gateway. Distinguish evidence publishers
   from discovery tools; mark unknown stages explicitly.
3. **Runtime and full cost:** total elapsed time, provider spend, LLM cost,
   combined total and cost per accepted company from saved run accounting. Distinguish
   billed spend, unresolved reservations and model estimates; exclude monitoring.
   Label unknown amounts explicitly; provider-only spend is not the full sourcing cost.
4. **Caveats:** material limitations and any company-target shortfall; state
   none when there are none.

A report link alone does not satisfy Sources Used. Check timing arithmetic and
source-count reconciliation against the saved receipts. This is a final-answer
self-check, not an additional sourcing step or a JSON-validator guarantee.

## LinkedIn company size

Retrieve the exact matched company LinkedIn profile through HarvestAPI via
Deepline. Discover/describe the current company getter, execute through the
existing budgeted attempt helper, and reuse matching saved receipts. Search
results and another provider's estimates cannot replace this field source.

- Save LinkedIn `employeeCountRange` as `company.employee_range`, e.g. `11-50`
  or `10001+`. LinkedIn is authoritative for company size. Do not use
  `employeeCount` (associated member profiles), an exact headcount, or a range
  endpoint for qualification or export. Compare the whole range with the saved
  ICP: full containment passes, no overlap fails, partial overlap stays unknown.
- Save `company.employee_range_evidence` using `evidence_url`, `evidence_date`,
  `evidence_date_basis: observed_current`, `evidence_text`, and `source:
  {provider: deepline, operation: execute, tool, route_id}`. Keep the original
  range wording in the text. The URL must match the company LinkedIn entity and
  the route must be a successful HarvestAPI company getter. Missing facts or
  evidence stay unresolved.

Review acceptance fills a missing range from the matching saved HarvestAPI
response. A supplied value must agree. The same receipt check runs during strict
validation and workbook export, reading the captured provider body rather than
reviewer-written evidence text. It verifies the entity, run and request identity
without another provider call. Keep `receipts/<route_id>.json` with the run. Old
saved runs may need enrichment or receipt reconciliation before re-export; do
not invent evidence, change their requests, or overwrite historical files to
make them pass. Library-only structural checks without a run path are not a
delivery gate; use the full strict validator CLI before delivery.

## `leads.xlsx` contract

For version `2.0`, write a workbook with `Companies` and `Sources` worksheets.
The first row of `Companies` is the fixed, ordered 11-column header:

```text
Company,Website,Company LinkedIn,Industry,Sub Industry,HQ State,HQ Country,Company Employee Range,Description,Signals,Intent Details
```

Keep `signal_evidence.signal` and qualification signal tags in structured
results; the client sheet displays their types inside `Signals`.

`Companies` contains one row per accepted company in accepted-company order.
Rejected and unresolved companies and route outcomes are not company rows. Keep
the existing `Sources` sheet and include company fit, signal, employee-range and
qualification-check evidence. Verify every saved row against the validated
values before delivery.

| Workbook column | `results.json` source |
|---|---|
| `Company` | `company.canonical_name` |
| `Website` | Direct company URL normalized against `company.domain`; recognized LinkedIn wrappers are unwrapped, and mismatched destinations require correction. |
| `Company LinkedIn` | `company.linkedin_url`, otherwise blank |
| `Industry` | `company.industry`, required canonical label for version `2.0` |
| `Sub Industry` | `company.sub_industry`, required canonical child for version `2.0` |
| `HQ State` | `company.hq_state`, otherwise blank |
| `HQ Country` | `company.hq_country`, otherwise blank |
| `Company Employee Range` | required `company.employee_range` from LinkedIn through HarvestAPI |
| `Description` | required `company.description`, exactly two factual sentences |
| `Signals` | Passed requested signal checks plus optional `supporting_findings`; context/activity labels, factual claims, dates and source URLs; older independent primary signals remain supported |
| `Intent Details` | `intent_details`, a natural paragraph explaining the activity, its context and why the company matters now |

Save supported company headquarters in `company.hq_state` and `company.hq_country`.
If the getter omits headquarters, reuse explicit headquarters evidence from the
existing qualification checks. Do not substitute an activity location or press
dateline; unknown values remain blank and do not introduce an additional
qualification gate.

Rejected/unresolved rows and provider receipts remain in `results.json` and
`report.md`. `Sources` contains the accepted company's fit, signal,
employee-range and qualification-check evidence, with
readable excerpts (at most 2,000 characters) and unchanged source URLs. Select the
supporting passage using the existing evidence `text` field, retaining material
qualifiers and dates; do not copy unrelated page chrome. The selected receipt must
support that passage and claim, not merely mention the company. Remove HTML
markup and common Markdown headings, links and emphasis only in the export view,
preserve literal code and URL text, and label shortened excerpts; full evidence stays in saved receipts. Its columns are `Company,Domain,Field,Signal,Evidence Date,Date Basis,
Observed On,Source URL,Evidence Text`. `Evidence Date` is the stored published,
posted or updated date, not necessarily the event date. For `observed_current`,
leave `Evidence Date` blank and put the original evidence date in `Observed On`.
Otherwise use the run's retrieval date for `Observed On`. Export dates as typed
Excel dates. A classification note gets an `Industry` source row with blank URL
and dates; it explains the selected pair and does not replace source evidence.
Receipt-backed funding attributes leave `Source URL` blank and include the
provider, tool and saved result reference in `Evidence Text`.
The `Signals` cell uses the passed check's concise factual `claim` in one block
per signal/source; readable supporting excerpts remain in `Sources`, with full passages in receipts.
Older independent primary signals retain their evidence-text display. It labels
observation dates
`Observed on` and other evidence dates `Source date`, and omits missing values.
`Activity date` uses the reviewed `event_date`, preserving year/month/day precision.
It does not infer event dates. Dated signals with an age bound require this
activity date separately from source publication; a recap cannot renew an old
event. The entire known period must fit the requested window, otherwise retain
the signal as unknown or obtain narrower evidence. Older saved dated signals
without `event_date` need review of their existing sources before resuming
research or delivery; do not backfill the publication date automatically. For current-state evidence,
`observed_current` uses the observation date and does not establish event timing,
duration or acceleration. The LLM chooses the activity meant by the request
(announcement, opening, etc.) and preserves its status in the claim. Store reviewed requested signals in the existing
`qualification_checks` with an optional short `signal` label and supporting
evidence; only `pass` checks enter `Signals`. Unknown/failed checks remain in
the audit and must not be presented as verified activity. No duplicate prose
field is needed for this column. Native review derives `signal_evidence` from
the first passed signal check for compatibility; callers do not maintain both.
Only intent checks have signal labels; ordinary fit/geography checks do not.
Optional `supporting_findings` add other verified ICP-relevant datapoints to the
same Signals cell, explicitly labeled `Signal:` or `Context:`. Omit unsupported
findings; no additional finding is required. Deduplicate related facts when writing.
The `Sources` rows for requested signals and supporting findings use
`Field: Signals`; they also support the factual claims in `Intent Details`.

Generate the file with `scripts/export_xlsx.mjs` so the spelling, order, types,
and layout stay deterministic. Use the harness-provided `@oai/artifact-tool`; it is not a TYCHE
project dependency and TYCHE does not install or pin it. When accepted rows
exist, format the range as an Excel table with filters, hide gridlines, and wrap
long description and intent text.
Keep employee ranges as text and leave unverified optional values as empty
cells rather than placeholder text.

### Client writing and taxonomy (version `2.0`)

Finalize once per company before acceptance: reuse the saved sources, then use
existing account-verification lookups for focused evidence gaps that could materially
improve ICP-relevant intent. Stay within the run's budget and deadline. There is no
minimum search count, source count or new-finding requirement. Save useful new facts
and the narrative together. Preserve a valid description and unchanged confirmed
records. If extra research finds nothing,
write honestly from existing support and retain the qualified company.

The confirmation packet is the company QA pass: check company geography,
spelling/grammar/capitalization, contradictions, missing required values,
duplicates, placeholders, truncation and formatting artifacts.
Signals and Intent Details must describe the same supported facts and timing.
Check required numeric claims against the source's actual entity, metric,
currency/units and time period; network or group totals do not establish a
company-specific threshold. Keep an unsupported required metric unresolved; omit an unsupported optional
number without disqualifying the company.
Repair actual errors before confirming; trim spaces and avoid em dashes. The exporter
also normalizes em dashes and trailing spaces in display text without changing raw
evidence, receipt-owned identities or source URLs. Optional gaps do not reject a company;
hold missing required support for targeted research, and reject only evidenced failure
of an original requirement. The run's final review retains strict delivery/accounting
checks; it is not another routine enrichment or rewriting pass.

- Write `company.description` as exactly two factual sentences explaining the
  business naturally from verified information. Sentence one describes what it
  does or sells, typically "[Company name] provides...". Sentence two adds its
  customers, specialization or another useful business detail, typically
  "It serves...". These openings are examples, not fixed templates. Support both
  sentences with account-fit or qualification evidence. Keep signals, inferred
  needs, scoring and internal diagnostics out of it.
- Write `intent_details` as one natural paragraph covering every distinct,
  verified signal relevant to the request, including verified preferred signals.
  For each signal, describe what the company did with specific facts and supported
  dates, then explain its relevance to the company's likely needs and the
  requested product/service using supporting business or qualification evidence.
  Use the same reviewed events and dates displayed in `Signals`. Connect the
  activity to the company's situation and requested product/service naturally;
  facts and relevance may share a sentence. Do not impose a sentence pattern,
  word count or separate concluding sentence. A concise explanation does not
  need an added generic prospect or fit label. Combine related evidence without
  repeating one event just because it has multiple sources or labels.
  Save the concise supported activity in the existing signal check's `claim`,
  then explain its business relevance in the paragraph. The final packet
  groups all `signal_checks` separately from other checks and includes the exported website.
  These are saved judgments for review, not independent confirmation of source meaning.
  During the existing source review, check supported signal facts, relevance and
  material uncertainty. Resolve requirement fit and source meaning before writing,
  then save affected evidence and the paragraph together with the company decision.
  Revisit only when evidence changes or a specific error is found. Put repair history,
  qualification-process notes and tool diagnostics in research commentary, not client prose.
  Keep qualification reasoning, conflicting revenue estimates and date-verification
  explanations in `review_findings`; convey inferred relevance conditionally instead
  of appending generic disclaimers about unproven demand. Supporting-finding claims
  state the fact without those audit notes or generic buying-intent disclaimers.
  Label dates by what they establish: a posting expiry is not its publication date
  or the end of the employment contract. Preserve material activity status such as
  an expired posting, maternity cover or planned work.
  Preserve the supplied offering in `request.product_service.description` and
  its `perspective` (`seller` or `target`). When it describes a seller's offering,
  explain the supported relevance to that offering without claiming confirmed
  purchase intent. When the ICP product/service describes the target company's offering, connect
  the signals to that offering and its operations, not an imagined external
  purchase. Focus on the company's activity, not a pitch for our product. Keep inferred
  needs conditional and material uncertainty clear; do not invent urgency,
  purchasing intent or an event date from an observation date. Use `Signals` for
  source URLs and evidence details, not labels such as "Required:" or "Bonus:"
  in the paragraph. Unknown optional hiring is not affirmative hiring intent.
  A new leader is not proof of layoffs, an existing service is not unmet demand,
  and an old opening is not newly dated intent. A matching signal does not waive
  another required condition: completed or historical activity alone does not
  establish an active or upcoming project when the request requires one.
- Keep `signal_evidence.signal` equal to the saved request's signal `kind`.
  Write precise observed facts in evidence and prose; do not rename the kind
  and accidentally detach its required/preferred status or age bounds. Use
  a clearly identified inference in `intent_details` when intent is optional and
  only a grounded hypothesis is available. Do not relabel a requested signal or turn a hypothesis into activity. Preserve detailed
  claims in evidence and prose. Every factual clause in the narrative must be
  supported by the saved signal, qualification or supporting-finding evidence for that company.
  Add corroborating sources to the matching qualification check. Save other useful
  ICP-relevant facts in optional `supporting_findings`, with `kind: signal|context`,
  `label`, `claim` and captured `evidence`. These appear in Signals and Sources,
  but never count toward required signal coverage or override a failed check.
  Context is background, not proof of active demand; do not combine unsupported
  events into a more persuasive story.
- Before export, use the existing source review to check both authored fields:
  the description has two factual sentences; each distinct verified signal has
  a supported relevance explanation; and the paragraph connects the evidence,
  likely need and requested product/service without filler. Unknown or failed preferred
  signals are not affirmative intent; mention a caveat only when material.
  The helpers require
  these fields and preserve the authored text; factual accuracy and natural prose
  are sourcing-agent review responsibilities, not regex or extra model-call gates.

- Use `assets/leadpoet_industry_taxonomy.json`, a versioned PP snapshot with
  pinned provenance. Select the company's business activity from evidence, not
  the customer's industry or the technology merely mentioned in a job posting.
  Every accepted company requires an `industry`/`sub_industry` pair using exact canonical labels
  and a permitted parent-child relationship. Some children have multiple parents;
  choose the evidence-supported one. Membership alone does not prove accuracy.
- Classify during company review using evidence already collected. A broad
  LinkedIn category does not prevent classification from verified product facts.
  When the pair cannot be supported, keep the company in `unresolved` with the
  gap in `reason_text` and an evidence-driven next action when one is available.
  Use the existing retry and budget limits; do not research indefinitely or invent
  a default pair. A `classification_note` may explain a selected pair but cannot
  substitute for either field. Acceptance and export require both fields.
  Required ICP industry evidence still applies.
  Raw provider classifications stay in the saved receipts. Do not import PP's
  heuristic fallback rules or add another AI call to format the output.

The semantic validator enforces these required fields and taxonomy pairs for
version `2.0`; billing and all existing eligibility checks still apply.
The exporter does not write narratives, classify companies, or infer dates.

## `report.md` minimum contents

In native runs, `tyche_finish` renders the audit from saved records plus the
agent's research commentary. The launcher updates costs after worker usage
closes. No additional paid call is needed solely for report attribution.

The Markdown report is the human audit receipt. Include the normalized request,
assumptions and as-of date; signal hypotheses and why routes differ; every
capability search/describe and execute receipt (without secrets); pilot limits,
rows, duplicates, route cost bases, confirmed and maximum provider credits,
Deepline dollars and cost per accepted company, and live provider statuses;
account gate decisions; adaptive reserve/refill decisions; all accepted,
rejected, and unresolved rows with stable reasons; company target shortfalls; the route frontier and call capacity; reviewed-company counts; and
the final stop reason. Do not claim a discovery-candidate endpoint ran, and do
not include raw provider payloads or credentials.
