
from datetime import datetime, timezone
from itertools import islice
import hashlib
import json
import os
from pathlib import Path
import tempfile

import budget_guard
from client_caveats import caveats_for
from record_route import mutate
from validate_run import (_company_key, accepted_errors, candidate_fit_errors, criteria, excluded_company,
                          qualification_errors)


SCHEMA = "tyche.confirmed-leads.v2"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


def output_path(run_file):
    path = Path(run_file)
    return path.with_name("leads.json" if path.name == "results.json" else path.stem + ".leads.json")


def _identity(run_file, document):
    return {"schema_version": SCHEMA, "run_id": document.get("run_id", document["request"].get("run_id")),
            "run_fingerprint": budget_guard.run_fingerprint(run_file),
            "request_fingerprint": fingerprint(document["request"]),
            "target_count": document["request"]["target_count"],
            "as_of_date": document["request"]["as_of_date"],
            "criteria": criteria(document["request"])}


def read(run_file, document):
    path = output_path(run_file)
    if path.is_symlink():
        raise ValueError("Confirmed leads must be a regular file, not a symlink")
    identity = _identity(run_file, document)
    if not path.exists():
        return {**identity, "confirmed_count": 0, "leads": []}
    saved = budget_guard.read_object(path)
    rows = saved.get("leads")
    if (any(saved.get(key) != value for key, value in identity.items())
            or not isinstance(rows, list) or saved.get("confirmed_count") != len(rows)
            or any(not isinstance(row, dict) or not _company_key(row) for row in rows)
            or len({_company_key(row) for row in rows}) != len(rows)):
        raise ValueError("Confirmed leads file is invalid or belongs to another run/request; original file preserved")
    return saved


def signal_candidates(document, run_file):
    limit, rows = document["request"].get("signal_candidates_max") or 0, []
    by_criterion = {c["id"]: c for c in criteria(document["request"])}
    for index, held, row, open_ids in _signal_holds(document):
        if len(rows) >= limit:
            break
        if candidate_fit_errors(document, row, f"unresolved[{index}]", run_file):
            continue
        checks = {c.get("criterion_id"): c for c in row.get("qualification_checks", []) if isinstance(c, dict)}
        def open_signal(cid):
            check = checks.get(cid) or {}
            dates = sorted({str(e["event_date"]) for e in check.get("evidence") or [] if isinstance(e, dict) and e.get("event_date")})
            return {"criterion_id": cid, "status": "fail" if check.get("status") == "fail" else "unknown",
                    "claim": check.get("claim") or held.get("reason_text"), "event_dates": dates}
        rows.append({**row, "signal_evidence": [], "hold_reason": held.get("reason_text"),
                     "open_signals": [open_signal(cid) for cid in open_ids],
                     "client_caveats": caveats_for(row, document["request"].get("as_of_date"), by_criterion)})
    return rows


def _signal_holds(document):
    request = document["request"]
    wanted = criteria(request)
    fit = [c["id"] for c in wanted if c["kind"] in ("filter", "size", "attribute")]
    every_signal = {c["id"] for c in wanted if c["kind"] == "signal"}
    signals = [c["id"] for c in wanted if c["kind"] == "signal" and c["importance"] == "required"]
    mode = request.get("signal_match_mode", "any")
    for index, held in enumerate(document.get("unresolved", []) if signals else []):
        company = held.get("candidate")
        checks = [c for c in held.get("qualification_checks", []) if isinstance(c, dict)]
        status = {cid: {c.get("status") for c in checks if c.get("criterion_id") == cid} for cid in fit + signals}
        passed = [cid for cid in signals if "pass" in status[cid]]
        open_ids = [cid for cid in signals if cid not in passed]
        if (held.get("stage") != "account" or not isinstance(company, dict) or not company.get("domain")
                or any(c.get("status") == "fail" for c in checks if c.get("criterion_id") not in every_signal)
                or any(status[cid] != {"pass"} for cid in fit) or not open_ids or (mode == "any" and passed)):
            continue
        yield index, held, {"company": company, **{key: value for key, value in held.items()
                                                   if key not in ("candidate", "stage", "reason_code", "reason_text",
                                                                  "former_scopes")}}, open_ids


def candidate_gaps(document, run_file, scopes):
    request = document["request"]
    limit = request.get("signal_candidates_max") or 0
    holds = [(index, row) for index, _, row, _ in _signal_holds(document)] if limit and scopes else []
    checked = {key: candidate_fit_errors(document, row, f"unresolved[{index}]", run_file)
               for index, row in holds if (key := _company_key(row)) in scopes and not excluded_company(request, row)}
    gaps = {key: errors for key, errors in checked.items() if errors}
    slots = limit - (len(checked) - len(gaps)) if gaps else 0
    others = [(index, row) for index, row in holds if _company_key(row) not in scopes]
    if len(others) > slots - len(gaps):
        clean = (index for index, row in others if not candidate_fit_errors(document, row, f"unresolved[{index}]", run_file))
        slots -= len(list(islice(clean, max(slots, 0))))
    return dict(islice(gaps.items(), max(slots, 0)))


def pending(run_file, document, scopes=None):
    saved = {_company_key(row): row for row in read(run_file, document)["leads"]}
    return [row for row in document["accepted"]
            if (scopes is None or _company_key(row) in scopes) and saved.get(_company_key(row)) != row]


def export_view(run_file, document):
    saved = read(run_file, document)
    current = {_company_key(row): row for row in document["accepted"]}
    rows = [row for row in saved["leads"] if current.get(_company_key(row)) == row]
    if not rows:
        raise ValueError("No unchanged confirmed leads are available for partial export")
    confirmed_bytes = output_path(run_file).read_bytes()
    if json.loads(confirmed_bytes) != saved:
        raise ValueError("Confirmed leads changed during validation; retry from the current saved review")
    projected = dict(document, accepted=rows, unresolved=[], rejected=[])
    errors = preflight(run_file, projected)
    if errors:
        raise ValueError("; ".join(errors))
    return projected, {"partial": True, "delivery_allowed": False,
                       "confirmed_count": len(rows), "target_count": saved["target_count"],
                       "shortfall": max(0, saved["target_count"] - len(rows)),
                       "confirmed_path": str(output_path(run_file).resolve()),
                       "confirmed_sha256": hashlib.sha256(confirmed_bytes).hexdigest()}


def review_ref(run_file, document, scopes=None):
    return "confirmed:" + fingerprint({"request": document["request"],
                                       "accepted": pending(run_file, document, scopes)})


def preflight(run_file, document):
    scoped = dict(document, unresolved=[], rejected=[])
    errors = (qualification_errors(scoped, run_file=run_file)
              + accepted_errors(scoped, run_file=run_file))
    rows = scoped["accepted"]
    if len({_company_key(row) for row in rows}) != len(rows):
        errors.append("Confirmed leads require one row per company")
    return errors


def status(run_file, document, scopes=None):
    saved = read(run_file, document)
    current = {_company_key(row): row for row in document["accepted"]}
    retained = [row for row in saved["leads"] if current.get(_company_key(row)) == row]
    waiting = pending(run_file, document, scopes)
    return {"path": str(output_path(run_file)), "confirmed_count": len(retained),
            "pending_review": [_company_key(row) for row in waiting],
            "sync_required": len(retained) != len(saved["leads"]),
            "next": "Review the returned evidence with tyche_review before another lookup." if waiting else None}


def write_snapshot(path, document):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".leads-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def update(run_file, *, approval=None, scopes=None, findings=()):
    result = {}

    def publish(document):
        saved = read(run_file, document)
        previous = {_company_key(row): row for row in saved["leads"]}
        rows = [row for row in document["accepted"] if previous.get(_company_key(row)) == row]
        waiting = pending(run_file, document, scopes)
        if approval is not None and waiting:
            if approval != review_ref(run_file, document, scopes):
                raise ValueError("Confirmed lead review changed; review the current packet")
            ledger = budget_guard.load_ledger(run_file)
            errors = budget_guard.audit_ledger(run_file, document, state=ledger, allow_pending=True)
            errors = [error for error in errors if error != ledger.get("blocked")]
            errors += preflight(run_file, dict(document, accepted=waiting) if scopes is not None else document)
            if errors:
                raise ValueError("; ".join(errors))
            approved = {_company_key(row) for row in waiting}
            rows = [row for row in document["accepted"]
                    if _company_key(row) in approved or previous.get(_company_key(row)) == row]
        retained = {_company_key(row) for row in rows if previous.get(_company_key(row)) == row}
        reviewed = {f["target"]: f for f in saved.get("review_findings", []) if f["target"] in retained}
        reviewed.update({f["target"]: f for f in findings})
        by_id = {c["id"]: c for c in criteria(document["request"])}
        caveats = {row["company"]["domain"]: caveats_for(row, document["request"]["as_of_date"], by_id) for row in rows}
        research = ({"signal_candidates_max": document["request"]["signal_candidates_max"],
                     "signal_candidates": signal_candidates(document, run_file)}
                    if "signal_candidates_max" in document["request"] else {})
        if (not output_path(run_file).exists() or rows != saved["leads"]
                or list(reviewed.values()) != saved.get("review_findings", [])
                or caveats != saved.get("client_caveats", {})
                or any(saved.get(key) != value for key, value in research.items())):
            saved = {**_identity(run_file, document), "confirmed_count": len(rows),
                     "updated_at": datetime.now(timezone.utc).isoformat(), "leads": rows,
                     "review_findings": list(reviewed.values()), "client_caveats": caveats, **research}
            write_snapshot(output_path(run_file), saved)
        result.update(path=str(output_path(run_file)), confirmed_count=len(rows), saved=True)
        return document

    mutate(run_file, publish)
    return result
