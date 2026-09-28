"""v64: the prompt asks for first-party, clause-complete fit proof and stage evidence."""

from __future__ import annotations

from experiments.harness_bakeoff.prompt import SYSTEM_PROMPT, build_prompt

ICP = {"prompt": "payments platforms", "industry": "Payments", "country": "United States",
       "sub_industry": "B2B payments and embedded payments infrastructure",
       "product_service": "A payments platform that helps businesses move money",
       "company_stage": "Seed", "employee_count": ["11-50"], "max_companies": 5,
       "required_attribute": "Sells a subscription or transaction-based payments platform used by businesses",
       "intent_signal": "launched a product", "intent_category": "PRODUCT_LAUNCH",
       "intent_max_age_days": 365, "intent_details_policy": "intent_details_v1"}


def test_prompt_shows_every_fit_clause_and_the_fit_proof_protocol() -> None:
    prompt = build_prompt(ICP, max_companies=5)
    assert "sub-industry: B2B payments and embedded payments infrastructure" in prompt
    assert "Product/service the company must sell: A payments platform" in prompt
    assert "Fit proof, before any intent work" in prompt
    assert "clause by clause" in prompt
    assert "Never cite a job post, customer story, partner page" in prompt
    assert "00f. Fit first" in prompt


def test_stage_evidence_guidance_names_each_stage_proof() -> None:
    prompt = build_prompt(ICP, max_companies=5)
    assert "company_stage_evidence: one or two" in prompt
    assert "(NASDAQ: ACME)" in prompt
    assert "Private Equity: current ownership" in prompt
    assert "never cite them" in prompt


def test_system_prompt_states_the_fit_gate_is_the_main_loss() -> None:
    assert "nine in ten companies now fail" in SYSTEM_PROMPT
    assert "an adjacent business fails" in SYSTEM_PROMPT.lower()


def test_v68_prompt_states_the_judge_reads_clauses_literally() -> None:
    prompt = build_prompt(ICP, max_companies=5)
    assert "The judge is literal" in prompt
    assert "a pricing table without the word subscription" in prompt
    assert "a software vendor to it fails" in prompt
