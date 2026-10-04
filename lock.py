"""Final row checks and the locked output list.

``Locker.lock(rows)`` keeps only rows that pass every output invariant, validates the document with the host's own
v6 validator and each row with the judge's internal company model (both imported through gates), and writes the
list as the sandbox checkpoint -- only when it is non-empty.  ``Locker.replace(rows)`` is the one path that may
shorten or empty the list (a company the fit re-check dropped).  ``locked`` always holds the last list written, which
is what ``run_icp`` returns.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from . import gates
from . import scorer_mirror as sm

MAX_ROWS = 5
PRIMARY_URLS = 2
BONUS_URLS = 1
_DECISIONS = {"count": 0}
MAX_DECISIONS = 60


def decide(decision: str, objective: str, *, candidate: str = "", evidence: tuple = (), rationale: str = "",
           next_action: str = "") -> bool:
    """Best-effort decision record through the sandbox's log_decision (bounded; a False return is ignored)."""

    if _DECISIONS["count"] >= MAX_DECISIONS:
        return False
    try:
        from lab_arena_checkpoint import log_decision  # provided beside the entrypoint in the sandbox
    except Exception:  # noqa: BLE001
        return False
    _DECISIONS["count"] += 1
    try:
        return bool(log_decision(objective=str(objective)[:480] or "select companies", evidence=[str(e)[:480] for e in
                                 evidence][:5], rationale=str(rationale)[:480] or "deterministic checks",
                                 next_action=str(next_action)[:480] or "continue", decision=decision,
                                 **({"candidate": str(candidate)[:480]} if candidate else {})))
    except Exception:  # noqa: BLE001
        return False


def reset_decisions() -> None:
    _DECISIONS["count"] = 0


def icp_criteria(icp: Mapping[str, Any]) -> int:
    return max(1, len(sm.icp_signals(icp)))


def allowed_buckets(icp: Mapping[str, Any]) -> list[str]:
    buckets = gates.icp_buckets(icp)
    return list(buckets) if buckets else list(sm.icp_buckets(icp))


def row_problem(row: Mapping[str, Any], icp: Mapping[str, Any], today: Optional[_dt.date] = None) -> str:
    """'' when the row satisfies every output invariant, else the first violated one."""

    if not isinstance(row.get("company_linkedin"), str):
        return "company_linkedin is not a string"
    linkedin = row["company_linkedin"]
    if linkedin and not linkedin.startswith("https://www.linkedin.com/company/"):
        return "company_linkedin is not a company page URL"
    if not str(row.get("country") or "").strip():
        return "country is blank"
    if not str(row.get("company_name") or "").strip() or not str(row.get("company_website") or "").startswith("https://"):
        return "identity fields"
    bucket = row.get("employee_count")
    norm = gates.normalize_bucket(bucket)
    buckets = allowed_buckets(icp)
    if (norm if norm is not None else sm.any_bucket(bucket)) not in buckets:
        return f"employee_count {bucket!r} not in {buckets}"
    attribute = row.get("required_attribute")
    if attribute is not None:
        if not isinstance(attribute, Mapping) or set(attribute) != {"text", "passed", "evidence_url", "evidence_quote",
                                                                     "explanation"}:
            return "required_attribute needs all five fields"
        if "linkedin.com" in str(attribute.get("evidence_url") or "").lower():
            return "required_attribute evidence on LinkedIn"
    n_criteria = icp_criteria(icp)
    today = today or sm.evaluation_date()
    per_index: dict[int, int] = {}
    for signal in row.get("intent_signals") or []:
        index = signal.get("matched_icp_signal")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < n_criteria:
            return f"matched_icp_signal {index!r} out of range"
        per_index[index] = per_index.get(index, 0) + 1
        if not signal.get("date"):
            return "intent signal without a date"
        try:
            day = _dt.date.fromisoformat(str(signal["date"])[:10])
        except ValueError:
            return "intent signal date is not ISO"
        if day > today:
            return "intent signal date in the future"
        if not str(signal.get("url") or "").startswith("https://"):
            return "intent URL is not https"
    if not per_index.get(0):
        return "no primary intent signal"
    if per_index.get(0, 0) > PRIMARY_URLS or any(n > BONUS_URLS for i, n in per_index.items() if i > 0):
        return "too many URLs on one criterion"
    if len(row.get("company_stage_evidence") or []) > 3:
        return "more than three stage evidence items"
    return ""


def sanitize(row: Mapping[str, Any]) -> dict[str, Any]:
    """The emitted shape: internal keys dropped, state never null, LinkedIn always a string."""

    out = {k: v for k, v in dict(row).items() if not str(k).startswith("_")}
    out.pop("contact", None)
    out["company_linkedin"] = out.get("company_linkedin") if isinstance(out.get("company_linkedin"), str) else ""
    out["state"] = out.get("state") if isinstance(out.get("state"), str) else ""
    out.setdefault("company_stage_evidence", [])
    return out


class Locker:
    def __init__(self, icp: Mapping[str, Any], *, schema: str = gates.V6, limit: int = MAX_ROWS,
                 output_path: Optional[str] = None) -> None:
        self.icp = icp
        self.schema = schema
        self.limit = max(1, min(MAX_ROWS, int(limit)))
        self.output_path = output_path if output_path is not None else str(os.environ.get("LAB_ARENA_OUTPUT_PATH") or "")
        self.locked: list[dict[str, Any]] = []
        self.writes = 0
        self.rejected: list[str] = []

    def check(self, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """The rows that pass every invariant and the judge's row model, in order, capped at the limit."""

        kept: list[dict[str, Any]] = []
        for raw in rows:
            row = sanitize(raw)
            why = row_problem(row, self.icp)
            if not why:
                verdict = gates.validate_row(row)
                why = verdict or ""
            if not why:
                try:
                    sm.validate_output([row], max_companies=1, schema_version=self.schema)
                except Exception as exc:  # noqa: BLE001
                    why = f"mirror contract: {str(exc)[:100]}"
            if why:
                self.rejected.append(f"{str(row.get('company_name') or '?')[:40]}: {why[:120]}")
                continue
            kept.append(row)
            if len(kept) >= self.limit:
                break
        return kept

    def lock(self, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Validate and checkpoint ``rows``; an empty or invalid list never replaces the last locked list."""

        kept = self.check(rows)
        if not kept:
            return self.locked
        verdict = gates.validate_document(kept, schema=self.schema)
        if verdict:
            self.rejected.append(f"document: {verdict[:160]}")
            return self.locked
        if verdict is None:
            try:
                sm.validate_output(kept, max_companies=self.limit, schema_version=self.schema)
            except Exception as exc:  # noqa: BLE001
                self.rejected.append(f"document mirror: {str(exc)[:160]}")
                return self.locked
        self.locked = kept
        self._write(kept)
        return self.locked

    def replace(self, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Adopt ``rows`` after an explicit drop (the fit re-check): unlike ``lock``, the list may shrink or become
        empty, and the shorter list is written as the checkpoint so a deadline cut cannot submit a dropped company.
        When the new list does not validate, the previously locked rows whose websites are still in ``rows`` stay."""

        wanted = {str(r.get("company_website") or "") for r in rows}
        kept = self.check(rows) if rows else []
        before = self.locked
        if kept and self.lock(kept) is not before:
            return self.locked
        self.locked = [r for r in self.locked if str(r.get("company_website") or "") in wanted]
        self._write(self.locked)
        return self.locked

    def _write(self, rows: list[dict[str, Any]]) -> bool:
        path = str(self.output_path or "").strip()
        if not path.startswith("/"):
            return False
        try:
            import lab_arena_checkpoint  # provided beside the entrypoint in the sandbox
        except ImportError:
            return False
        try:
            lab_arena_checkpoint.write(rows, output_path=Path(path))
        except Exception as exc:  # noqa: BLE001
            print(f"[lock] checkpoint write failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            return False
        self.writes += 1
        return True

    def as_dict(self) -> dict[str, Any]:
        return {"locked": [r.get("company_name") for r in self.locked], "writes": self.writes,
                "rejected": self.rejected[:10]}


def dumps(rows: list[Mapping[str, Any]]) -> str:
    return json.dumps({"companies": rows}, ensure_ascii=False)


__all__ = ["Locker", "row_problem", "sanitize", "decide", "reset_decisions", "allowed_buckets", "icp_criteria"]
