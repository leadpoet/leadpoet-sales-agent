"""How to spend a Leadpoet run's 45 minutes, given how Leadpoet scores it. Leadpoet only; it changes the order
of research, never what TYCHE checks or accepts.

Evidence, round arena-2026-10-10 (reports/leadpoet-round-20261010):
- Leadpoet scores an ICP as the mean quality of the first five companies times (qualified / 5)
  (lab_arena/verify.py per_icp_score), so coverage multiplies quality.
- Across TYCHE's local Arena runs, ICPs requiring Private Equity or Series C+ published 0.2 companies per run and
  Public 2.2; the commonest blockers were an unproven stage and an unproven intent signal 0.
- Competitors scored on the ICPs where TYCHE published nothing by starting from a page about the required event
  (a facility opening, a seed round, a job board) that named the company and proved the event, and often the stage.
"""

COVERAGE_STRATEGY = (
    "Leadpoet scores each ICP as the average quality of your first five published companies times the share of "
    "five that qualify, so each additional qualified company adds as much as improving the others; an "
    "unverifiable intent signal 0 costs 10 points. Spend the time on candidates whose hardest requirement is "
    "already proven. Discover them event-first: search news, press releases, regulator notices or job boards for "
    "the event intent signal 0 describes, in the requested industry and geography and within its age limit, and "
    "take the companies those pages name and date. When the request names a Private Equity, Seed or Series stage, "
    "also search announcements that state it, such as a funding round naming its series or a private-equity firm "
    "acquiring or taking a controlling stake in the company (a minority investor is not a Private Equity stage), "
    "then confirm that no later round, acquisition or listing supersedes it. Only then check size, headquarters and the required attribute. Give each candidate about four "
    "lookups: if its stage or intent signal 0 is still unproven, hold it with the named gap and move to the next "
    "candidate instead of searching further. Confirm each qualified company as soon as its evidence is saved and "
    "keep sourcing until five are confirmed or time ends. Cite pages Leadpoet's judge can open: never a LinkedIn "
    "page as the URL for intent signal 0, the company stage or the required attribute when another captured page "
    "states it.")
