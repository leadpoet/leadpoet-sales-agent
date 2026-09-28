"""Checks for the pre-submission evidence pass."""

from __future__ import annotations

from experiments.harness_bakeoff import evidence

PAGE = (
    "Acme Robotics today announced it has raised $12 million in Series A funding led by "
    "Example Ventures. The round, announced on August 20, 2026, will fund expansion of the "
    "Austin, Texas team, which currently employs about 40 people. Acme Robotics builds "
    "autonomous inspection robots for warehouses. " * 3
)
ICP = {"intent_signal": "Raised a funding round in the last 12 months", "intent_signals": ["Raised a funding round in the last 12 months"]}
COMPANY = {"company_name": "Acme Robotics", "company_website": "https://www.acmerobotics.com/"}


def _signal(url: str, snippet: str, description: str = "Acme Robotics raised a $12 million Series A.") -> dict:
    return {"matched_icp_signal": 0, "date": "2026-08-20", "url": url, "snippet": snippet,
            "description": description, "why_now": "Fresh capital."}


def test_domain_key_uses_last_two_labels() -> None:
    assert evidence.evidence_domain("https://blog.acme.com/post") == "acme.com"
    assert evidence.evidence_domain("https://www.acme.com/") == "acme.com"


def test_verbatim_snippet_passes_unchanged() -> None:
    snippet = "Acme Robotics today announced it has raised $12 million in Series A funding led by Example Ventures."
    repaired, note = evidence.verify_signal(_signal("https://news.example/acme", snippet), company=COMPANY, page_text=PAGE, focus=[ICP["intent_signal"]])
    assert repaired is not None and note == "ok"
    assert repaired["snippet"] == snippet


def test_paraphrased_snippet_is_recut_from_the_page() -> None:
    repaired, note = evidence.verify_signal(_signal("https://news.example/acme", "Acme closed a twelve million dollar round this summer."), company=COMPANY, page_text=PAGE, focus=[ICP["intent_signal"]])
    assert repaired is not None
    assert "snippet re-cut" in note
    assert evidence.snippet_overlap(repaired["snippet"], PAGE) >= 0.9
    assert "raised" in repaired["snippet"].lower()


def test_company_absent_from_page_is_dropped() -> None:
    repaired, reason = evidence.verify_signal(_signal("https://news.example/x", PAGE[:200]), company={"company_name": "Zeta Corp", "company_website": "https://zeta.example/"}, page_text=PAGE, focus=[])
    assert repaired is None and "company name" in reason


def test_login_wall_and_negation_are_dropped() -> None:
    repaired, reason = evidence.verify_signal(_signal("https://news.example/x", "x"), company=COMPANY, page_text="Sign in to LinkedIn to see this content", focus=[])
    assert repaired is None and "login wall" in reason
    negated = _signal("https://news.example/x", PAGE[:220], description="Acme Robotics raised a Series A but the posting is no longer open.")
    repaired, reason = evidence.verify_signal(negated, company=COMPANY, page_text=PAGE, focus=[])
    assert repaired is None and "negates" in reason


def test_verify_companies_drops_same_domain_duplicates_and_unverified_required() -> None:
    company = dict(COMPANY, intent_signals=[
        _signal("https://news.example/a", PAGE[:200]),
        _signal("https://news.example/b", PAGE[:200]),
        _signal("https://www.acmerobotics.com/blog/series-a", PAGE[:200]),
    ])
    pages = {"https://news.example/a": PAGE, "https://www.acmerobotics.com/blog/series-a": PAGE}
    report: list[str] = []
    kept = evidence.verify_companies(ICP, [company], pages.get, seconds_left=lambda: 100.0, report=report)
    assert len(kept) == 1
    assert [s["url"] for s in kept[0]["intent_signals"]] == ["https://news.example/a", "https://www.acmerobotics.com/blog/series-a"]
    assert any("same domain" in line for line in report)

    # A page our fetcher cannot read is kept as submitted: the judge's fetcher
    # may read it, and an unverified signal can still score while a dropped one never can.
    unavailable = dict(COMPANY, intent_signals=[_signal("https://news.example/gone", PAGE[:200])])
    report = []
    kept = evidence.verify_companies(ICP, [unavailable], lambda url: None, seconds_left=lambda: 100.0, report=report)
    assert kept == [unavailable]
    assert any("unreadable" in line for line in report)


def test_out_of_time_keeps_companies_unverified() -> None:
    company = dict(COMPANY, intent_signals=[_signal("https://news.example/a", "anything at all here")])
    kept = evidence.verify_companies(ICP, [company], lambda url: None, seconds_left=lambda: 5.0)
    assert kept == [company]
