# SN71 Arena challenger

This submission bundle targets the live v5 contract checked on 2026-09-15.
Entrypoint: `harness.run_icp(icp) -> list[dict]`, at most five companies.

## Pipeline

1. Free Hunter discovery, followed by at most two paid discovery searches.
2. Batch company profiling and structured company records. Reject contradictory
   fit, unreadable/conflicting requested headcount, and unknown headquarters.
3. In contact-required rounds, search HarvestAPI for requested current roles and
   fetch provider-supported emails. Only companies with supported contacts
   proceed to paid intent research. Retain a spare when the call budget allows.
4. Route primary intent research by category: hiring, leadership, funding,
   expansion, product, technology adoption, social activity, or podcast.
5. Quote source text, reject stale primary evidence, and preserve criterion
   indexes. Source titles cannot substitute for support in the quoted text.
6. Produce one evidence-grounded `intent_details` paragraph and validate every
   row with Pydantic before emitting it. Bad rows do not invalidate good rows.

`contacts.py` and `arena_models.py` reuse official baseline components;
see [UPSTREAM.md](UPSTREAM.md) and the preserved upstream MIT notice.
`LICENSE` is the complete Tyche AGPL text required by Arena admission.

## Frozen policies

The live round currently requires:

- `leadpoet.lab_arena.output.v5`
- `arena_integrity_v1`
- `contacts_v1`
- `intent_details_v1`

v5 emits `intent_details` and contact claims. It omits `fit_summary`,
`fit_evidence_urls`, and per-signal `snippet` / `why_now`.
Legacy company-only ICPs without policy markers retain the older output shape.
Unknown explicit policies stop before provider work.

The gateway injects policies into each ICP; the sandbox cannot query the public
Gateway directly. The external preflight checks the live round before upload.

## Verify

Use Python 3.11+ with Pydantic 2.12+ and pytest. Official parity tests also need
the LeadPoet project's dependencies. Set `LEADPOET_REPO` to a current official
checkout, not an old copy which only understands v1.

From the project root:

```bash
export LEADPOET_REPO=/absolute/path/to/current/leadpoet
leadpoet/.venv/bin/python tools/verify_arena.py --repo "$LEADPOET_REPO"
```

This reads the current public round, checks its policies and submission window,
verifies the full license and source archive, runs the real Arena host entrypoint
against a fake broker, then runs contact and regression tests. No paid providers
or wallet operations are used. An outdated official validator must fail.

For offline tests only:

```bash
LEADPOET_REPO=/absolute/path/to/current/leadpoet leadpoet/.venv/bin/python arena-agent-174/local_test.py
LEADPOET_REPO=/absolute/path/to/current/leadpoet leadpoet/.venv/bin/python -m pytest tests -q
```

The fake broker proves the transport and contract, not factual correctness,
email deliverability, real provider latency, or champion eligibility.

## Budgets

Per ICP: at most 8 Exa searches, 2 dynamic scrapes, 12 contact calls,
4 contact calls per candidate, 28 Deepline attempts, and 4 OpenRouter attempts.
Contact work reserves 8 remaining Deepline calls and 100 seconds for evidence.
All broker attempts count locally, including lost replies. The host's limits
remain authoritative. The model is Gemini 2.5 Flash Lite with GPT-4.1 Mini as
fallback; both were listed in the live OpenRouter catalog on 2026-09-15.

Call ceilings are not dollar ceilings. Actual contact, LLM, search and scraping
costs must all be measured. Current cost eligibility is bounded by $80 overall
and $0.80 per independently qualified company/contact pair. A contact claim
created by this harness is not yet an independently qualified pair.

## Submission

`submit.sh` resolves this directory rather than a hardcoded WSL path. It runs
preflight once and delegates to the current official submission helper only
with an explicit `--send` argument. Credentials stay outside the source bundle.

```bash
LEADPOET_REPO=/absolute/path/to/current/leadpoet bash arena-agent-174/submit.sh
# After real provider/scorer validation and with funded runtime credentials:
LEADPOET_REPO=/absolute/path/to/current/leadpoet bash arena-agent-174/submit.sh --send
```

Set `ARENA_PYTHON` if the virtualenv is separate from the official checkout.
Set `ARENA_WALLET` and `ARENA_HOTKEY` for the registered miner wallet.
Do not use the historical `tools/retry_submit.py` flow for this bundle.
Accepted sources may be published by the subnet after evaluation; a private
GitHub repository does not make an Arena submission permanently private.

## Champion target

A challenger needs the highest eligible score AND at least baseline +1.0.
Company fit, primary evidence, intent details and contact verification all have
to pass. Cost eligibility and reward eligibility are separate checks. No king
or `epoch_eligible=false` in a public snapshot does not by itself determine
whether a future winning round can activate rewards.

Next performance gate: measure actual qualified pairs and full successful-call
cost on representative public ICPs using real providers and the official scorer.
Then compare against the same frozen baseline and ICPs. Offline tests cannot
provide an honest win probability.
