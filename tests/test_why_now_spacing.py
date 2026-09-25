import pytest
from pydantic import ValidationError

from experiments.harness_bakeoff.models import IntentSignal


def test_why_now_spacing_preserves_claim_and_json_contract():
    signal = IntentSignal(
        matched_icp_signal=0,
        description="New platform launched",
        date="2026-09-07",
        why_now="  New platform launch.\n\tReach out about  rollout support.  ",
        url="https://example.com/news/platform-launch",
        snippet="The company launched its new platform.",
    )
    assert signal.why_now == "New platform launch. Reach out about rollout support."
    assert IntentSignal.model_validate_json(signal.model_dump_json()) == signal
    assert set(signal.model_dump()) == {
        "matched_icp_signal", "description", "date", "why_now", "url", "snippet"
    }
    for invalid in ("\n\t ", None, 123):
        with pytest.raises(ValidationError):
            IntentSignal.model_validate({**signal.model_dump(), "why_now": invalid})
