"""(v110) One cheap model call grades each verified row's cited announcement before admission.

Why: on the paying ICP of the published 2026-10-12 round the agent verified seven to nine companies per run and sent
the wrong five - every row carried the same fit tier, so the order came from incidental flags.  The judge's own
receipts show what it looks at: is the cited text really the event the criterion names, and, when the required
attribute carries a condition about a current change ("is undergoing a material business or organizational change",
"is actively expanding its commercial footprint"), does the announcement itself establish that condition.

Checked against published verdicts before use (model calls only): on that ICP's 68 on-chain rows, rows graded
``strong`` qualified 73% (required attribute unproven 9%), ``weak`` 54% (31%), ``none`` 11%; rows whose text was graded
"not such an event" qualified 0 of 8.  On two earlier rounds (1,675 rows) the intent check passed 68% of rows graded a
real event and 15% of the others.  The grade only orders and weights rows already verified from their own sources;
it adds no company and no fact.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import criteria
from . import llm

MODEL = "google/gemini-2.5-flash"
BATCH = 12
HEADLINE_CHARS = 300
GRADES = ("strong", "weak", "none")


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


def _primary(row: Mapping[str, Any]) -> Mapping[str, Any]:
    for signal in row.get("intent_signals") or []:
        if signal.get("matched_icp_signal") in (0, None):
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


def grade(rows: list[dict[str, Any]], icp: Mapping[str, Any]) -> dict[str, Any]:
    """Set ``_flags['event_ok']`` (bool) and, when the attribute has a change clause, ``_flags['change_grade']`` on each
    row, in place.  A failed or malformed reply leaves the rows untouched.  Returns notes for the run report."""

    notes: dict[str, Any] = {"rows": len(rows), "graded": 0}
    if not rows:
        return notes
    try:
        criterion = str(criteria.signals(icp)[0].get("text") or "")
    except Exception:  # noqa: BLE001
        criterion = str(icp.get("intent_signal") or "")
    attribute = " ".join(str(icp.get("required_attribute") or "").split()) or "(none stated)"
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        try:
            reply = llm.chat_json(prompt(attribute, criterion, [item(n, row) for n, row in enumerate(batch)]),
                                  model=MODEL, max_tokens=900, purpose="eventgrade")
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
            flags = row.setdefault("_flags", {})
            if isinstance(entry.get("event_ok"), bool):
                flags["event_ok"] = entry["event_ok"]
            change = str(entry.get("change") or "").strip().lower()
            if has_clause and change in GRADES:
                flags["change_grade"] = change
            notes["graded"] += 1
            notes.setdefault("grades", {})[str(row.get("company_name") or "")[:40]] = \
                f"{'event' if flags.get('event_ok') is not False else 'no event'} / {flags.get('change_grade') or 'n/a'}"
    return notes


__all__ = ["grade", "prompt", "item", "MODEL"]
