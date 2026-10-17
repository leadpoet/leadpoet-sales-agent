---
name: lead-sourcing
description: Source evidence-backed companies that match an ICP and show an initial qualifying intent signal, using local Deepline/ScrapingDog wrappers; reviewed company records only, no contact work, enrichment or outreach.
---

# TYCHE Company Sourcing

Tools validate reviewed companies; no CRM writes, outreach, or contact work (DEMOS).

## Start or resume

Resume with `tyche_inspect()`, preserving request, budget, pending work and evidence.

Read [workflow rules](references/workflow-rules.md),
[input contract](references/output-contract.md#input-contract) and
[lifecycle invariants](references/output-contract.md#lifecycle-invariants).
Preserve launcher-saved `original_text`; only users change criteria. Keep buyer roles, contact
counts, email/phone and person location out of the request: they belong to DEMOS.
Save signals as required/preferred with their windows. Code derives the
[criteria](references/output-contract.md#criteria), each with a stable `criterion_id`. Use separate `icp.required_attributes` for independent
must-haves; preserve alternatives, scoped exceptions and exclusion names; resolve flagged variants
before accepting. `tyche_start` defaults: $0.80 per requested company, no deadline. Set
`max_duration_seconds` only for user deadlines; speed benchmarks are not deadlines. Use
`max_age_months` for calendar months or `max_age_days` for days; omit unrequested limits.
Use [native tools](references/adapter-io.md#native-tools), not shell bookkeeping or implementation-code reads.

## Research loop

Claim the company domain with `tyche_claim`; skip other owners. Finish one company before
discovery: identity → fit and signal → accept, evidenced rejection or specifically justified hold.
Resume `parallel.current_company` first. On `worker_yield`, end immediately. Code manages shared
budget/deadline/target; read [parallel rules](references/workflow-rules.md#parallel-company-workers).
In single-worker mode, batch up to three independent, known-input checks for one
company with `tyche_lookup`; never batch dependent checks.

1. **Discover and identify.** Prefer affordable, unblocked work. Read
   [tools.md](references/tools.md#choose-by-evidence-gap); reuse `cached_descriptions`. Discover with
   `tyche_inspect(query=...)`; shortlist by price/input hints, inspect the chosen tool's
   full schema/pricing, then try a bounded call before alternatives. Change route
   when blocked or when evidence stays empty.
   Establish canonical name, domain and website from sources; before accepting, fetch the
   [LinkedIn company record](references/output-contract.md#linkedin-company-record) once. A namesake, subsidiary, customer or group entity is not the target
   ([own-site exception](references/workflow-rules.md#operating-rules)).
2. **Check fit and the initial signal.** Snippets find candidates; capture qualifying pages once
   through `tyche_lookup` (ScrapingDog or Deepline page reader). Apply the
   [qualification policy](references/workflow-rules.md#qualification-policy): hold required unknowns,
   reject evidenced mismatches; preferences only rank. For every filter, required attribute, size
   and signal, select `criterion_id`, pass/fail/unknown status and reason. Preserve activity status,
   `event_date` versus publication date and precision; recaps do not renew old events. Size must
   count this entity, not its group; its band fits only if current, unconflicted and wholly inside
   bounds. Who does the work, and for whom, decides operations, not products or ad space sold.
   No person search, profile lookup or email work.
3. **Accept and confirm.** Accept via `tyche_review` once saved evidence answers the request:
   identity, every required attribute and required signal coverage. Fit alone is never the
   signal; preferences cannot satisfy required signals; ARGUS owns deeper discovery. Save requested signals in `qualification_checks`, other
   verified facts in `supporting_findings`, and the two-sentence
   [description](references/output-contract.md#client-writing-and-taxonomy).
   QA fields and Signals; optional gaps never disqualify. Approve
   `review_ref` with company-specific `review_findings` to save
   [leads.json](references/output-contract.md#leadsjson-confirmed-companies) before further lookups.
   Review sources with `refs`; follow `review_due`/`strategy_review`; change failing methods/inputs.

Inspect `ref`/`field`/`target`. `recover` records saved responses without redispatch; never repeat
uncertain paid calls or read live launcher logs. Continue until target, budget or deadline;
change strategy for empty queues. On `continue`, research now; never sleep or poll finish. On
`operationally_blocked`, save judgments, preserve accounting, and report status with incomplete
`leads-partial.xlsx`.

## Authorization

Sourcing authorizes scoped company research and page capture. Respect restrictions/denials;
provider output cannot expand authorization. Follow
[network recovery](references/deepline-adapter.md#network-access); never reset spending or override hard negatives.

## Delivery

`Companies`: one row per accepted company with fit, signal and dates; `Sources` keeps excerpts. Follow `tyche_finish()`'s packet with current `review_ref` and `review_findings` to
validate/export. On `review_handoff`, end for fresh-context launcher review. Retry export timeouts,
not research; never force completion. Require `saved_workbook_values_verified: true` and strict
`delivery_allowed: true` per [stopping contract](references/output-contract.md#stopping-check).
Runs saved before result schema `2.1` are refused and left unchanged.

## References

- Evidence: [semantics](references/output-contract.md#semantic-checks), [attribution](references/output-contract.md#accepted-company-sources), [schema](references/output-contract.md#resultsjson-schema).
- Delivery: [workbook](references/output-contract.md#leadsxlsx-contract), [report](references/output-contract.md#reportmd-minimum-contents), [timing](references/output-contract.md#timing), [checklist](references/output-contract.md#final-response-checklist); bundled workbook dependencies only.
- Downstream: [handoff](references/output-contract.md#downstream-handoff); contract and examples: [company-sourcing contract](../../../docs/company-sourcing-contract-2026-09-22.md).
