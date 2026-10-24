
from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

MODEL = "google/gemini-3.5-flash-lite"
BATCH = 12
HEADLINE_CHARS = 300
GRADES = ("strong", "weak", "none")
_RANK = {"strong": 0, "weak": 1, "n/a": 1, None: 2, "none": 3}
_MARKS: dict[int, dict[str, Any]] = {}


def prompt(attribute: str, criterion: str, items: list[dict]) -> str:

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
        except Exception as exc:
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

    mark = _MARKS.get(id(row))
    if mark is None and isinstance(row, Mapping):
        mark = row.get("_judge_mirror")
    if not isinstance(mark, Mapping):
        return _RANK[None]
    if mark.get("event_ok") is False:
        return 4
    return _RANK.get(mark.get("change"), _RANK[None])


__all__ = ["grade", "order_key", "prompt", "item", "MODEL"]
