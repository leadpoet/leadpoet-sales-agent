"""Vendored copy of leadpoet_verifier.industry_fit and its taxonomy.

The scorer runs the SUBMITTED industry through this exact code
(qualification/scoring/lead_scorer.py: _industry_evidence_decision), and an
explicit taxonomy conflict is a submitted fit MISMATCH: a zero for the company
plus a -10 structured gate failure. Plausible labels conflict -- ICP "Software"
against "Applied AI", "Information Technology" or "Artificial Intelligence" all
come back MISMATCH -- so the harness has to ask the same code the same question
before it submits, not guess at it.

Vendored under a private package name rather than as `leadpoet_verifier`: the
sandbox puts the bundle first on sys.path (lab_arena/agent_entrypoint.py:28),
so a same-named package would shadow any host copy.

Source: github.com/leadpoet/leadpoet, leadpoet_verifier/, MIT License (see
NOTICE). Only change: industry_fit.py imports its sibling relatively.
"""
