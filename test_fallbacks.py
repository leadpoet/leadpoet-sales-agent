"""Force the two new fallback paths to run.

local_test.py's fixture makes the primary searches succeed, so the fallbacks
added for the miner-funded contract never execute there. This drives them
directly: the pinned hiring search and the category="news" search both return
nothing, which is exactly the condition the fallbacks exist for.
"""
import sys
import types

sys.path.insert(0, r"g:\bittensor\sn71\arena-agent-174")
import harness  # noqa: E402

SEEN = []

NEWS_HIT = {"results": [{
    "url": "https://synthetiq.co.uk/blog/series-a",
    "title": "Synthetiq raises Series A to expand its applied AI team",
    "publishedDate": "2026-07-14",
    "text": "Synthetiq announces a Series A and is hiring across engineering.",
}]}
ATS_HIT = {"results": [{
    "url": "https://boards.greenhouse.io/synthetiq/jobs/551",
    "title": "Senior ML Engineer",
    "publishedDate": "2026-08-02",
    "text": "We are hiring a Senior ML Engineer to join the applied AI team.",
}]}


def fake_call(operation_id, parameters, timeout_ms=None):
    SEEN.append((operation_id, dict(parameters)))
    if operation_id != "exa.search":
        return None
    # Primary hiring search is domain-pinned; primary news search sets category.
    if parameters.get("includeDomains"):
        return {"results": []}          # pinned ATS search finds nothing
    if parameters.get("category") == "news":
        return {"results": []}          # news slice finds nothing
    # Whatever is left is one of the two new fallbacks.
    if "hiring" in parameters["query"]:
        return ATS_HIT
    return NEWS_HIT


harness.call = fake_call
harness.seconds_left = lambda: 999

icp = harness.Icp({
    "industry": "Applied AI",
    "countries": ["United Kingdom"],
    "intent_signals": ["hiring machine learning engineers"],
})
company = {"name": "Synthetiq", "root": "synthetiq.co.uk",
           "url": "https://synthetiq.co.uk"}

picked = harness.gather_signals(company, icp)

print("exa.search calls made: %d" % sum(1 for o, _ in SEEN if o == "exa.search"))
for op, params in SEEN:
    tag = ("pinned-ats" if params.get("includeDomains")
           else "news-category" if params.get("category") == "news"
           else "FALLBACK")
    print("  %-12s %s" % (tag, params.get("query", "")[:62]))

print()
print("signals recovered: %d" % len(picked))
for row in picked:
    print("  kind=%-11s source=%-11s %s" % (row["kind"], row["source"], row["url"]))

fallbacks = [p for _, p in SEEN
             if not p.get("includeDomains") and p.get("category") != "news"]
assert len(fallbacks) == 2, "both fallbacks should have fired, got %d" % len(fallbacks)
assert len(picked) >= 2, "fallbacks produced no usable signals"
kinds = {row["kind"] for row in picked}
assert kinds == {"hiring", "leadership"}, kinds
print("\nOK -- both fallbacks fired and each produced a usable signal.")
