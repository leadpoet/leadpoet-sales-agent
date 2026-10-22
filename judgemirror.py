"""Judge mirror, step 1: grade each verified row's cited announcement the way the judge reads it, before the five
slots are filled.

Ported from the rival V-line's ``eventgrade.py`` (v110, AGPL, round 10-15 build) onto this agent's own LLM client.
Why: on rich ICPs this agent drafts 4-11 companies the field qualifies and sends 2-4 of them -- inside a re-check
vote tier the order falls back to draft order (reverify.rerank). The judge's receipts show what decides a row: is
the cited text really the event the intent criterion names, and when the required attribute carries a condition
about a current change, does the announcement itself establish it. The V-line measured on published verdicts:
rows graded ``strong`` qualified 73%, ``weak`` 54%, ``none`` 11%, and "not such an event" 0 of 8.

The grade only orders rows already verified from their own sources; it adds no company and no fact. A failed or
malformed reply leaves the rows ungraded, which sorts them exactly as before.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

MODEL = "google/gemini-3.5-flash-lite"   # gemini-2.5-flash has no zero-data-retention route (2026-10-20)
BATCH = 12
HEADLINE_CHARS = 300
GRADES = ("strong", "weak", "none")
# Sort weight per grade, lower first: a real event that proves the change clause leads its vote tier, an announcement
# graded "not such an event" goes last. Ungraded rows sit between "weak" and "none" so a failed call changes nothing
# relative to draft order among themselves.
_RANK = {"strong": 0, "weak": 1, "n/a": 1, None: 2, "none": 3}
# Grades live here, keyed by id(row), never inside the row: the output validator forbids unknown keys, and a
# "_judge_mirror" field on a pool row made sm.validate_output reject the whole re-checked list. One ICP runs per
# process, and grade() clears the map before each pass, so a recycled id cannot carry a stale grade.
_MARKS: dict[int, dict[str, Any]] = {}


def prompt(attribute: str, criterion: str, items: list[dict]) -> str:
    """items: [{"i": int, "company": str, "headline": str, "source": str, "date": str}]"""

    return (
        "You check announcements that a sales-research tool wants to cite for companies.\n\n"
        f"REQUIRED ATTRIBUTE of the target companies:\n{attribute}\n\n"
        f"INTENT CRITERION the cited announcement must meet:\n{criterion}\n\n"
        "First decide, for the required attribute as a whole: does it contain a condition about something the company is "
        "CURRENTLY doing or going through (a change, expansion, transformation, growth, an active strategy), in addition "
        "to what the company is or sells?  Call that condition the change clause.\n\n"
        "Then, for each announcement below, using ONLY the text given, answer two things:\n"
        "1. event_ok - is the text itself an announcement of the kind the intent criterion names?  Answer false for a "
        "filing cover line, a grant or listing-rule notice, an earnings date, a conference appearance, a generic company "
        "description, or text that names no such event.\n"
        "2. change - does the announcement, in its own words, concretely establish the change clause for that company?\n"
        "   strong = it states a substantial, completed or announced change of that kind directly (for example a "
        "top-level leadership succession, a reorganisation, a newly created senior role, a completed merger, a new market "
        "entered);\n"
        "   weak   = it is related but minor or indirect (for example one appointment below the top level with no stated "
        "wider change);\n"
        "   none   = it does not establish it (for example a routine, regional or local appointment, a pending or planned "
        "transaction, or text that describes no change).\n"
        "   If the attribute has no change clause, answer n/a for every row.\n\n"
        "ANNOUNCEMENTS:\n" + json.dumps(items, ensure_ascii=False, indent=1) + "\n\n"
        'Return JSON only: {"has_change_clause": true|false, "change_clause": "<its words or empty>", '
        '"rows": [{"i": <i>, "event_ok": true|false, "change": "strong"|"weak"|"none"|"n/a"}]}')


def _criterion(icp: Mapping[str, Any]) -> str:
    signals = icp.get("intent_signals")
    if isinstance(signals, list) and signals:
        first = signals[0]
        if isinstance(first, Mapping):
            return str(first.get("text") or first.get("signal") or first.get("description") or "")
        return str(first)
    return str(icp.get("intent_signal") or "")


def _primary(row: Mapping[str, Any]) -> Mapping[str, Any]:
    for signal in row.get("intent_signals") or []:
        if isinstance(signal, Mapping) and signal.get("matched_icp_signal") in (0, None):
            return signal
    return {}


def _slug(url: Any) -> str:
    path = urlsplit(str(url or "")).path
    return " ".join(path.replace("-", " ").replace("_", " ").replace("/", " ").split())[:140]


def item(index: int, row: Mapping[str, Any]) -> dict[str, Any]:
    signal = _primary(row)
    headline = " ".join(str(signal.get("description") or "").split())[:HEADLINE_CHARS] or _slug(signal.get("url"))
    host = urlsplit(str(signal.get("url") or "")).netloc.lower()
    return {"i": index, "company": str(row.get("company_name") or "").strip(), "headline": headline,
            "source": host[4:] if host.startswith("www.") else host, "date": str(signal.get("date") or "")[:10]}


def grade(rows: list[dict[str, Any]], icp: Mapping[str, Any], *,
          llm_json: Callable[..., Optional[Any]], deadline: Optional[float] = None) -> dict[str, Any]:
    """Record ``{'event_ok': bool, 'change': grade}`` for each graded row (side map, see _MARKS).

    ``llm_json(prompt, max_tokens=..., model=..., deadline=...)`` is the agent's own JSON chat client
    (scout.llm_json with the run's HTTP client factory bound). Returns notes for the run report."""

    notes: dict[str, Any] = {"rows": len(rows), "graded": 0, "model": MODEL}
    _MARKS.clear()
    if not rows:
        return notes
    attribute = " ".join(str(icp.get("required_attribute") or "").split()) or "(none stated)"
    criterion = _criterion(icp)
    for start in range(0, len(rows), BATCH):
        if deadline is not None and time.monotonic() > deadline - 20.0:
            notes["stopped"] = "deadline"
            break
        batch = rows[start:start + BATCH]
        try:
            reply = llm_json(prompt(attribute, criterion, [item(n, row) for n, row in enumerate(batch)]),
                             max_tokens=900, model=MODEL, deadline=deadline)
        except Exception as exc:  # noqa: BLE001 - grading is optional
            notes.setdefault("errors", []).append(type(exc).__name__)
            continue
        if not isinstance(reply, Mapping):
            notes.setdefault("errors", []).append("no reply")
            continue
        has_clause = reply.get("has_change_clause") is True
        notes["change_clause"] = str(reply.get("change_clause") or "")[:120] if has_clause else ""
        for entry in reply.get("rows") or []:
            try:
                row = batch[int(entry.get("i"))]
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
            mark: dict[str, Any] = {}
            if isinstance(entry.get("event_ok"), bool):
                mark["event_ok"] = entry["event_ok"]
            change = str(entry.get("change") or "").strip().lower()
            mark["change"] = change if has_clause and change in GRADES else "n/a"
            _MARKS[id(row)] = mark
            notes["graded"] += 1
            notes.setdefault("grades", {})[str(row.get("company_name") or "")[:40]] = \
                f"{'event' if mark.get('event_ok') is not False else 'no event'} / {mark['change']}"
    return notes


def order_key(row: Mapping[str, Any]) -> int:
    """Lower sorts first. Used inside a re-check vote tier, before draft order."""

    mark = _MARKS.get(id(row))
    if mark is None and isinstance(row, Mapping):
        mark = row.get("_judge_mirror")   # offline evaluation passes grades on a copy of the row
    if not isinstance(mark, Mapping):
        return _RANK[None]
    if mark.get("event_ok") is False:
        return 4
    return _RANK.get(mark.get("change"), _RANK[None])


__all__ = ["grade", "order_key", "prompt", "item", "MODEL"]
