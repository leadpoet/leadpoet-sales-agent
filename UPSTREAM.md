# Reused upstream components

`arena_models.py` and `contacts.py` are derived from the official LeadPoet
Pydantic harness `lab` branch, commit
`33744512` (retrieved 2026-09-15):
https://github.com/leadpoet/pydantic-harness/tree/lab

Original paths: `experiments/harness_bakeoff/models.py` and
`experiments/harness_bakeoff/contacts.py`. Local changes adapt the import,
reject failed provider envelopes and unrelated profile responses, and bound
contact work through the harness broker. The model adapter also preserves a
singular primary intent before bonuses. Keep these changes when refreshing.
`tests/test_contacts.py` derives from the same upstream revision and retains
coverage for employer, title, country/region and email-source verification.

The complete Tyche AGPL-3.0 LICENSE is included as required by Arena admission
(https://github.com/gzaentz/tyche/blob/main/LICENSE). The original Pydantic
harness MIT notice is preserved in `UPSTREAM_LICENSE`.
`_lp_industry/NOTICE` records the existing taxonomy provenance.
