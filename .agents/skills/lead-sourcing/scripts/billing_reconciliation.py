
import functools
import hashlib
import json
import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from collections import Counter
from contextlib import ExitStack, contextmanager

import budget_guard as budget
import deepline
import deepline_http
import provider_capture
import run_coordination
from provider_output import load_json
from record_route import mutate


FIELDS = ("id", "request_id", "provider", "operation", "status", "charge_state", "credits", "delta", "created_at",
          "outcome", "provider_units", "pricing_basis", "pricing_model", "reason",
          "source", "charge_credits", "billing_stage", "charge_finality", "billing_mode")
EXACT_LOOKUP = "request_id"
EXACT_FIELDS = FIELDS + ("batch_count",)
ZERO_CHARGE_STATES = frozenset({"hold_release", "free", "failed"})
ASYNC_HOLD = "async_hold"
INPUT_REJECTED = "input_rejected"
FAILED_UNKNOWN = "failed_unknown"
EXACT_READ_SECONDS = 10
MAX_ATTEMPTS = 3
RETRY_AFTER_SECONDS = 60
READ_TIMEOUT_SECONDS = 30


class _RecoveryBusy(Exception):
    pass


class _OrganizationChanged(ValueError):
    pass


def _api_key_or_none():
    try:
        return deepline_http.api_key()
    except (OSError, ValueError):
        return None


def exact_reads_available():
    return _api_key_or_none() is not None


@contextmanager
def _state_update(path):
    with ExitStack() as state:
        try:
            state.enter_context(run_coordination.locked(path, blocking=False))
        except BlockingIOError:
            raise _RecoveryBusy from None
        yield


def _catalog_contract(run_file, receipt, route_id=None, *, before_route=None, bound_call=None):
    from source_receipts import read_receipt
    if bound_call and bound_call.get("catalog_route_id"):
        bound_id = bound_call["catalog_route_id"]
        path = Path(run_file).parent / "receipts" / (bound_id + ".json")
        if (route_id is not None and route_id != bound_id
                or hashlib.sha256(path.read_bytes()).hexdigest() != bound_call.get("catalog_sha256")):
            raise ValueError("billing catalog changed since dispatch; preserve the original contract")
        route_id = bound_id
    routes = budget.read_object(run_file).get("routes", [])
    if before_route is not None:
        position = next((i for i, row in enumerate(routes) if row.get("route_id") == before_route), None)
        if position is None:
            return None, None
        routes = routes[:position]
    for route in reversed(routes):
        if (route.get("provider") != "deepline" or route.get("operation") != "describe"
                or route.get("provider_status") != "ok" or route.get("tool") != receipt.get("tool")
                or (route_id is not None and route.get("route_id") != route_id)):
            continue
        body = read_receipt(run_file, route["route_id"])["result"]
        contracts = [c for c in body.get("results", []) if receipt.get("tool") in _aliases(c)]
        if len(contracts) == 1:
            return contracts[0], route["route_id"]
    if route_id is not None:
        raise ValueError("billing catalog reference is missing or belongs to another tool")
    return None, None


def _aliases(contract):
    if not isinstance(contract, dict):
        return set()
    names = [contract.get(k) for k in ("toolId", "id", "tool", "operation", "operationId")]
    aliases = contract.get("operationAliases", [])
    if isinstance(aliases, list):
        names.extend(aliases)
    return {name for name in names if isinstance(name, str) and name}


FINISHED_FAILURES = frozenset({"timeout", "provider_error"})


def free_call_evidence(run_file, route_id, call):
    from source_receipts import read_receipt
    if not call.get("catalog_route_id") or not call.get("catalog_sha256"):
        return None
    saved = read_receipt(run_file, route_id)
    receipt = saved["result"]
    if (receipt.get("status") == "schema_error" and receipt.get("error_stage") == "response"
            and receipt.get("receipt_status") == "complete" and receipt.get("provider_response")):
        normalized, _ = deepline.normalize_response(receipt["attempt"]["request"], receipt["provider_response"])
        receipt = dict(receipt, **normalized)
    successful = receipt.get("status") == "ok"
    if receipt.get("status") == "no_results" and receipt.get("results") == []:
        try:
            normalized, _ = deepline.normalize_response(receipt["attempt"]["request"], receipt["provider_response"])
            successful = normalized.get("status") == "no_results" and normalized.get("results") == []
        except (KeyError, TypeError, ValueError):
            successful = False
    finished_failure = receipt.get("status") in FINISHED_FAILURES
    if (receipt.get("provider") != "deepline" or receipt.get("operation") != "execute"
            or not (successful or finished_failure) or receipt.get("receipt_status") != "complete"
            or receipt.get("billing")):
        return None
    contract, catalog_id = _catalog_contract(run_file, receipt, call["catalog_route_id"], before_route=route_id)
    pricing = (contract or {}).get("pricing", {})
    if (not contract or not isinstance(pricing, dict)
            or contract.get("callable") is not True or contract.get("disabled")
            or pricing.get("unit") not in {"call", "request"}
            or type(pricing.get("creditsPerUnit")) not in (int, float) or pricing["creditsPerUnit"] != 0
            or (pricing.get("usdPerUnit") is not None and
                (type(pricing["usdPerUnit"]) not in (int, float) or pricing["usdPerUnit"] != 0))
            or pricing.get("summary") or pricing.get("details")):
        return None
    catalog_path = Path(run_file).parent / "receipts" / (catalog_id + ".json")
    digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    if digest != call["catalog_sha256"]:
        return None
    return {"catalog_route_id": catalog_id,
            "catalog_sha256": digest,
            "response_sha256": hashlib.sha256(Path(saved["receipt_file"]).read_bytes()).hexdigest(),
            **({"outcome": receipt["status"]} if finished_failure else {})}


def settle_free_calls(run_file):
    run_file = Path(run_file).resolve(strict=True)
    ledger = budget.load_ledger(run_file)
    if ledger["version"] != 2:
        return []
    recorded = {row["route_id"] for row in budget.read_object(run_file).get("routes", [])}
    proofs = {}
    for rid, call in ledger["calls"].items():
        if (rid in recorded and call["provider"] == "deepline" and call.get("state") == "pending_billing"
                and call.get("actual_credits") is None and call.get("actual_usd") is None
                and not call.get("billing_evidence") and not call.get("billing_issue")):
            if proof := free_call_evidence(run_file, rid, call):
                proofs[rid] = proof
    settled = []
    if proofs:
        with budget.transaction(budget.ledger_path(run_file)) as saved:
            budget.check_run_identity(run_file, saved)
            for rid, proof in proofs.items():
                call = saved["calls"][rid]
                if (call.get("state") == "pending_billing" and call.get("actual_credits") is None
                        and call.get("actual_usd") is None and not call.get("billing_evidence")):
                    call.update(actual_credits="0", state="settled", free_evidence=proof)
                    settled.append(rid)
    if any(call.get("free_evidence") for call in budget.load_ledger(run_file)["calls"].values()):
        _synchronize(run_file)
    return settled


def _key_refused(receipt):
    response = receipt.get("provider_response") if isinstance(receipt, dict) else None
    return isinstance(response, dict) and bool(response.get("execution_key_refused"))


def forget_refused_keys(run_file):
    run_file = Path(run_file).resolve(strict=True)
    fingerprint = budget.run_fingerprint(run_file)
    refused = []
    for rid, call in budget.load_ledger(run_file)["calls"].items():
        path = run_file.parent / "receipts" / (rid + ".json")
        if (call.get("idempotency_key") and call["actual_credits"] is None and not call.get("execution_recovery")
                and path.is_file()):
            receipt = budget.read_object(path)
            if receipt.get("run_fingerprint") == fingerprint and _key_refused(receipt):
                refused.append(rid)
    if refused:
        with budget.transaction(budget.ledger_path(run_file)) as saved:
            budget.check_run_identity(run_file, saved)
            for rid in refused:
                call = saved["calls"][rid]
                if call.get("idempotency_key"):
                    call["refused_idempotency_key"] = call.pop("idempotency_key")
    return refused


def hold_unbillable_calls(run_file):
    run_file = Path(run_file).resolve(strict=True)
    routes = {row["route_id"]: row for row in budget.read_object(run_file).get("routes", [])}
    fingerprint = budget.run_fingerprint(run_file)
    held = []
    for rid, call in budget.load_ledger(run_file)["calls"].items():
        if rid in routes and call.get("state") == "pending_billing" and budget.unbillable(call, {}):
            receipt = budget.read_object(run_file.parent / "receipts" / (rid + ".json"))
            if (receipt.get("run_fingerprint") == fingerprint
                    and receipt.get("request_fingerprint") == routes[rid].get("request_fingerprint")
                    and budget.unbillable(call, receipt)):
                held.append(rid)
    if held:
        with budget.transaction(budget.ledger_path(run_file)) as saved:
            budget.check_run_identity(run_file, saved)
            for rid in held:
                if saved["calls"][rid].get("state") == "pending_billing" and budget.unbillable(saved["calls"][rid], {}):
                    budget.hold_unknown_charge(saved["calls"][rid])
    if held or any(call.get("unknown_charge") and rid in routes and not routes[rid].get("billing_basis")
                   for rid, call in budget.load_ledger(run_file)["calls"].items()):
        _synchronize(run_file)
    return held


def hold_unpriced_calls(run_file):
    run_file = Path(run_file).resolve(strict=True)
    state = budget.load_ledger(run_file)
    if not any(budget.orphaned_unpriced(state, rid, call) for rid, call in state["calls"].items()):
        return []
    with budget.transaction(budget.ledger_path(run_file)) as saved:
        budget.check_run_identity(run_file, saved)
        held = [rid for rid, call in saved["calls"].items() if budget.orphaned_unpriced(saved, rid, call)]
        for rid in held:
            budget.hold_unpriced(saved["calls"][rid])
    return held


def _free_per_call(contract):
    pricing = (contract or {}).get("pricing", {})
    return (isinstance(pricing, dict) and pricing.get("unit") in {"call", "request"}
            and type(pricing.get("creditsPerUnit")) in (int, float) and pricing["creditsPerUnit"] == 0
            and (pricing.get("usdPerUnit") is None or
                 (type(pricing["usdPerUnit"]) in (int, float) and pricing["usdPerUnit"] == 0))
            and not pricing.get("summary") and not pricing.get("details"))


def _names_tool(receipt, row, contract):
    tool = receipt.get("tool", "")
    if contract and contract.get("provider"):
        return (tool in _aliases(contract) and row.get("provider") == contract["provider"]
                and row.get("operation") in _aliases(contract))
    return bool(row.get("operation") == tool and isinstance(row.get("provider"), str) and row["provider"]
                and tool.casefold().startswith(row["provider"].casefold() + "_"))


def _sole_row(receipt, rows, contract):
    ids = {receipt.get(key) for key in ("job_id", "request_id") if receipt.get(key)}
    matches = [row for row in rows if row.get("request_id") in ids and _names_tool(receipt, row, contract)]
    ledger_matches = [row for row in matches if row.get("source") == "credit_ledger"]
    if ledger_matches:
        matches = ledger_matches
    if len(matches) != 1:
        return None
    row = matches[0]
    metadata = row.get("metadata")
    groups = metadata.get("chargeGroupIds", []) if isinstance(metadata, dict) else [] if metadata is None else None
    if not isinstance(groups, list) or len(groups) > 1 or (groups and groups != [row["request_id"]]):
        return None
    return row, groups


def _failed_attempt(receipt, row):
    return (row.get("status") == "error" and row.get("charge_state") == "failed"
            and row.get("reason") == "operation_attempt"
            and receipt.get("status") in deepline._FAILURE_STATUSES
            and not receipt.get("results"))


def unfinal_failed_attempt(receipt, rows, contract=None):
    found = _sole_row(receipt, rows, contract)
    return bool(found) and _failed_attempt(receipt, found[0]) and found[0].get("charge_finality") != "final"


def matching_charge(receipt, rows, contract=None, *, saved=False):
    found = _sole_row(receipt, rows, contract)
    if found is None:
        return None
    row, groups = found
    failed_attempt = _failed_attempt(receipt, row)
    empty_result = (row.get("status") == "no_result" and row.get("charge_state") == "free"
                    and row.get("outcome") == "miss"
                    and ((receipt.get("status") == "no_results" and not receipt.get("results"))
                         or _free_per_call(contract)))
    completed = row.get("status") == "completed" and row.get("charge_state") in {"posted", "free"}
    if not row.get("id") or not (completed or failed_attempt or empty_result):
        return None
    finality = row.get("charge_finality")
    legacy = saved and failed_attempt and finality in (None, "unknown")
    if (finality is not None or failed_attempt) and finality != "final" and not legacy:
        return None
    try:
        charge = budget.amount(row.get("credits"), "posted credits")
        if (isinstance(row.get("delta"), bool) or not isinstance(row.get("delta"), (int, float, str))
                or -Decimal(str(row["delta"])) != charge
                or ((row["charge_state"] == "free" or failed_attempt) and charge != 0)
                or (row.get("billing_mode") == ASYNC_HOLD and charge == 0)):
            return None
        if row.get("source") == "credit_ledger" and (
                row.get("reason") != "charge_settle" or row.get("billing_stage") != "posted"
                or row.get("charge_state") != "posted"
                or budget.amount(row.get("charge_credits"), "ledger credits") != charge):
            return None
    except (ValueError, TypeError, InvalidOperation):
        return None
    proof = {key: row[key] for key in FIELDS if key in row}
    if groups:
        proof["metadata"] = {"chargeGroupIds": groups}
    return proof


def _ledger_rows(rows):
    result = []
    for row in rows:
        if row.get("reason") != "charge_settle":
            continue
        metadata, audit = row.get("metadata") or {}, row.get("billing_audit") or {}
        if not isinstance(metadata, dict) or not isinstance(audit, dict):
            continue
        groups = metadata.get("chargeGroupIds", [])
        if not isinstance(groups, list) or (groups and groups != [row.get("request_id")]):
            continue
        if any(value is not None and value != row.get(field) for field, value in (
                ("request_id", metadata.get("chargeGroupId")),
                ("request_id", metadata.get("requestId")),
                ("request_id", audit.get("request_id")),
                ("provider", audit.get("provider")), ("operation", audit.get("operation")),
                ("charge_credits", audit.get("charge_credits")),
                ("charge_credits", metadata.get("postedCredits")))):
            continue
        result.append({**{key: row[key] for key in FIELDS if key in row},
                       "source": "credit_ledger", "status": row.get("status", "completed"), "credits": row.get("charge_credits")})
    return result


RESULTS_WITHOUT_CHARGE = "Results returned, but billing reports a miss or zero result units; charge remains pending."


def billing_issue(receipt, proof, contract=None):
    if budget.amount(proof["credits"], "posted credits") != 0 or not (proof.get("outcome") == "miss" or
            (proof.get("pricing_basis") == "result" and proof.get("provider_units") == 0)):
        return None
    if _free_per_call(contract):
        return None
    pricing = (contract or {}).get("pricing")
    if (receipt.get("provider") == "deepline" and receipt.get("status") == "ok"
            and isinstance(pricing, dict) and pricing.get("unit") == "usage"
            and proof.get("status") == "completed" and proof.get("charge_state") == "free"
            and proof.get("pricing_basis") == "usage" and proof.get("pricing_model") == "provider_usage"
            and type(proof.get("provider_units")) in (int, float) and proof["provider_units"] == 0):
        return None
    rows = receipt.get("results", [])
    if (receipt.get("provider") == "deepline" and receipt.get("status") == "ok"
            and receipt.get("tool") == "prospeo_enrich_company"
            and isinstance(rows, list) and rows and all(
                isinstance(row, dict) and row.get("error") is False
                and row.get("free_enrichment") is True for row in rows)):
        return None
    def populated(row):
        if isinstance(row, dict):
            for key in ("results", "items", "data"):
                if isinstance(row.get(key), list):
                    return bool(row[key])
        return bool(row)
    if any(populated(row) for row in rows):
        return RESULTS_WITHOUT_CHARGE
    return None


def _save_status(path, status):
    with _state_update(path), budget.transaction(path) as saved:
        saved.clear()
        saved.update(status)


def _holdable(call, saved):
    return (saved["version"] == 2 and call["actual_credits"] is None and call.get("actual_usd") is None
            and not call.get("unknown_charge") and not call.get("tariff")
            and call.get("admission_bound_credits") is not None
            and budget.amount(call["admission_bound_credits"], "call price bound") > 0)


def _settle_charges(run_file, receipts, rows, status):
    matched, contracts, unfinal = {}, {}, set()
    ledger = budget.load_ledger(run_file)
    for rid, receipt in receipts.items():
        contract, catalog_id = _catalog_contract(run_file, receipt, bound_call=ledger["calls"][rid])
        proof = matching_charge(receipt, rows, contract)
        if proof and catalog_id:
            proof["catalog_route_id"] = catalog_id
        matched[rid] = proof
        contracts[rid] = contract
        if proof is None and unfinal_failed_attempt(receipt, rows, contract):
            unfinal.add(rid)
    counts = Counter(proof["id"] for proof in matched.values() if proof)
    with budget.transaction(budget.ledger_path(run_file)) as saved:
        budget.check_run_identity(run_file, saved)
        used = {call["billing_evidence"]["id"]: rid for rid, call in saved["calls"].items()
                if call.get("billing_evidence")}
        for rid, receipt in receipts.items():
            call, proof = saved["calls"][rid], matched[rid]
            if rid in unfinal:
                if _holdable(call, saved):
                    budget.hold_unknown_charge(call)
                    call["hold_reason"] = FAILED_UNKNOWN
                continue
            if (proof is None or counts[proof["id"]] != 1 or used.get(proof["id"], rid) != rid
                    or call["actual_credits"] is not None
                    or (saved["version"] == 2 and call.get("actual_usd") is not None)):
                continue
            issue = billing_issue(receipt, proof, contract=contracts[rid])
            if call.get("billing_evidence") and call["billing_evidence"] != proof:
                call.setdefault("billing_history", []).append(call["billing_evidence"])
            call.update(billing_evidence=proof, billing_issue=issue,
                        actual_credits=None if issue else str(budget.amount(proof["credits"], "posted credits")))
            if saved["version"] == 2:
                call["state"] = "pending_billing" if issue else "settled"
                if not issue:
                    for held in ("held_credits", "unknown_charge", "hold_reason"):
                        call.pop(held, None)
            if budget.price_overrun(call, saved):
                saved["blocked"] = budget.PRICE_OVERRUN
            if rid not in status["matched"]:
                status["matched"].append(rid)
            if not issue:
                status["unmatched"].remove(rid)


def _recovery_id(receipt, call, contract):
    recovery = call.get("execution_recovery")
    if (isinstance(recovery, dict) and call.get("idempotency_key")
            and recovery.get("idempotency_key") == call["idempotency_key"]
            and (recovery.get("tool_id") == receipt.get("tool") or recovery.get("tool_id") in _aliases(contract))
            and isinstance(recovery.get("request_id"), str) and recovery["request_id"]):
        return recovery["request_id"]
    return None


def call_ids(receipt, call, contract=None):
    ids = [receipt.get(key) for key in ("job_id", "request_id")
           if isinstance(receipt.get(key), str) and receipt[key]]
    ids.append(_recovery_id(receipt, call, contract))
    return list(dict.fromkeys(value for value in ids if value))


def exact_charge(receipt, row, contract, ids):
    if (not isinstance(row, dict) or not row.get("id") or row.get("request_id") not in ids
            or not _names_tool(receipt, row, contract) or row.get("charge_finality") != "final"
            or row.get("batch_count") not in (None, 1)):
        return None
    metadata = row.get("metadata")
    groups = metadata.get("chargeGroupIds", []) if isinstance(metadata, dict) else [] if metadata is None else None
    if not isinstance(groups, list) or len(groups) > 1 or (groups and groups != [row["request_id"]]):
        return None
    state = row.get("charge_state")
    if state != "posted" and state not in ZERO_CHARGE_STATES:
        return None
    try:
        charge = budget.amount(row.get("credits"), "posted credits")
        if (isinstance(row.get("delta"), bool) or not isinstance(row.get("delta"), (int, float, str))
                or -Decimal(str(row["delta"])) != charge
                or ((state in ZERO_CHARGE_STATES or row.get("billing_mode") == "no_bill") and charge != 0)
                or (row.get("billing_mode") == ASYNC_HOLD and charge == 0)):
            return None
    except (ValueError, TypeError, InvalidOperation):
        return None
    proof = {key: row[key] for key in EXACT_FIELDS if key in row}
    if groups:
        proof["metadata"] = {"chargeGroupIds": groups}
    return dict(proof, lookup=EXACT_LOOKUP)


OUTCOMES = ("settled", "async_zero", "awaiting_final", "failed_unknown", "ambiguous", "not_attributable", "no_record")
NOT_FINAL = frozenset({"async_zero", "awaiting_final", "failed_unknown"})
HELD_OUTCOMES = {"async_zero": ASYNC_HOLD, "failed_unknown": FAILED_UNKNOWN}


def rejected_before_execution(receipt):
    if (not isinstance(receipt, dict) or receipt.get("status") != "provider_error"
            or receipt.get("provider") != "deepline"):
        return False
    response = receipt.get("provider_response")
    body = response.get("body") if isinstance(response, dict) else None
    error = body.get("tool_error") if isinstance(body, dict) else None
    request_id = receipt.get("job_id")
    return (isinstance(error, dict) and response.get("http_status") == 422 and body.get("status") == "failed"
            and isinstance(request_id, str) and bool(request_id)
            and body.get("job_id") == request_id and error.get("requestId") == request_id
            and body.get("code") == error.get("code") == "VALIDATION_ERROR"
            and body.get("error_category") == error.get("category") == "validation"
            and body.get("failure_origin") == error.get("origin") == "caller"
            and error.get("statusCode") == 422 and error.get("retryable") is False
            and isinstance(receipt.get("tool"), str) and error.get("toolId") == receipt["tool"])


def _is_zero(row):
    try:
        return budget.amount(row.get("credits"), "posted credits") == 0
    except (ValueError, TypeError, InvalidOperation):
        return False


def _exact_record(request_id, payload, receipt, contract, ids):
    if payload is None:
        return None, "no_record"
    recent = payload.get("recent") if isinstance(payload, dict) else None
    rows = recent.get("entries") if isinstance(recent, dict) else None
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or recent.get("request_id", request_id) != request_id):
        raise ValueError("Billing lookup returned no recognized record for the requested ID")
    if not rows:
        return None, "no_record"
    if len(rows) != 1:
        return None, "ambiguous"
    proof = exact_charge(receipt, rows[0], contract, ids)
    if proof:
        return proof, "settled"
    row = rows[0]
    finality, state = row.get("charge_finality"), row.get("charge_state")
    if state == "failed" and finality == "unknown":
        return None, "failed_unknown"
    if (row.get("billing_mode") == ASYNC_HOLD and finality == "final" and _is_zero(row)
            and row.get("request_id") in ids and _names_tool(receipt, row, contract)):
        return None, "async_zero"
    if finality != "final" or state in {"temporary_hold", "pending"} or row.get("billing_mode") == ASYNC_HOLD:
        return None, "awaiting_final"
    return None, "not_attributable"


def _read_ids(ids, lookup, status, receipt, contract):
    best, failure = None, None
    for request_id in ids:
        try:
            payload = lookup(request_id)
            if isinstance(payload, dict) and payload.get("org_id"):
                if status.get("billing_org_id", payload["org_id"]) != payload["org_id"]:
                    raise _OrganizationChanged("Billing organization changed; preserve the original run")
                status["billing_org_id"] = payload["org_id"]
            proof, outcome = _exact_record(request_id, payload, receipt, contract, ids)
        except _OrganizationChanged:
            raise
        except (ValueError, KeyError, TypeError, OSError) as exc:
            failure = failure or exc
            continue
        if proof:
            return proof, outcome
        if best is None or OUTCOMES.index(outcome) < OUTCOMES.index(best):
            best = outcome
    if failure is not None and best not in NOT_FINAL:
        raise failure
    return None, best


def _recovered_request(record, call, receipt, contract):
    if record is None:
        return None, "key_not_found"
    recovery = record.get("executionRecovery") if isinstance(record, dict) else None
    if not isinstance(recovery, dict) or recovery.get("idempotencyKey") != call["idempotency_key"]:
        return None, "key_mismatch"
    if record.get("toolId") != receipt.get("tool") and record.get("toolId") not in _aliases(contract):
        return None, "key_other_tool"
    if (record.get("responseStatus") == 422
            and deepline_http.KEY_REFUSED in deepline_http.stated_codes(record.get("response"))):
        return None, "key_refused"
    request_id = record.get("requestId")
    if not isinstance(request_id, str) or not request_id or request_id != request_id.strip() or len(request_id) > 200:
        return None, "key_without_request_id"
    return {"idempotency_key": call["idempotency_key"], "request_id": request_id, "tool_id": record["toolId"],
            "state": recovery.get("state") if isinstance(recovery.get("state"), str) else None}, "key_found"


def _kept_reply(receipts, calls, raw, url=None):
    try:
        text = raw.decode("utf-8")
        payload = deepline._json_from_text(text, billing_feed=True) if url is None else load_json(text, billing_feed=True)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if "executionRecovery" in payload or "toolId" in payload:
        recovery = payload.get("executionRecovery")
        return {"executionRecovery": ({key: recovery[key] for key in ("idempotencyKey", "state") if key in recovery}
                                      if isinstance(recovery, dict) else None),
                **{key: payload[key] for key in ("requestId", "toolId", "responseStatus") if key in payload}}
    wanted = (set(parse_qs(urlsplit(url or "").query).get(EXACT_LOOKUP, ()))
              or {i for rid, receipt in receipts.items() for i in call_ids(receipt, calls[rid])})
    def rows(entries):
        if not isinstance(entries, list):
            return None
        kept = []
        for row in entries:
            if isinstance(row, dict) and row.get("request_id") in wanted:
                proof = {key: row[key] for key in EXACT_FIELDS if key in row}
                groups = row.get("metadata", {}).get("chargeGroupIds") if isinstance(row.get("metadata"), dict) else None
                if isinstance(groups, list) and all(group in wanted for group in groups):
                    proof["metadata"] = {"chargeGroupIds": groups}
                kept.append(proof)
        return kept
    kept = {key: payload[key] for key in ("org_id", "has_more", "next_cursor") if key in payload}
    if "entries" in payload:
        kept["entries"] = rows(payload["entries"])
    if isinstance(payload.get("recent"), dict):
        recent = payload["recent"]
        kept["recent"] = dict({key: recent[key] for key in ("request_id", "has_more", "offset", "next_offset",
                                                            "next_cursor") if key in recent},
                              entries=rows(recent.get("entries")))
    return kept


def _exact_reads(run_file, calls, status, attempt, *, deadline, lookup, recover):
    ledger = budget.load_ledger(run_file)
    found, recoveries, outcomes, unavailable = {}, {}, {}, False
    for rid, receipt in sorted(calls.items()):
        provider_capture.reading_for(rid)
        if time.monotonic() >= deadline:
            outcomes[rid] = "deadline"
            continue
        call = ledger["calls"][rid]
        try:
            contract, catalog_id = _catalog_contract(run_file, receipt, bound_call=call)
            ids = call_ids(receipt, call, contract)
            proof, outcome = _read_ids(ids, lookup, status, receipt, contract)
            if (proof is None and outcome in (None, "no_record") and call.get("idempotency_key")
                    and not _key_refused(receipt) and _recovery_id(receipt, call, contract) is None):
                recovery, key_outcome = _recovered_request(recover(call["idempotency_key"]), call, receipt, contract)
                if recovery is not None:
                    recoveries[rid] = recovery
                    proof, outcome = _read_ids([recovery["request_id"]], lookup, status, receipt, contract)
                elif key_outcome != "key_refused":
                    outcome = key_outcome
            outcomes[rid] = outcome or "no_id"
            if proof:
                if catalog_id:
                    proof["catalog_route_id"] = catalog_id
                found[rid] = proof
        except _OrganizationChanged:
            raise
        except deepline_http.BillingUnavailable:
            outcomes[rid], unavailable = "unavailable", True
        except (ValueError, KeyError, TypeError, OSError):
            outcomes[rid] = "invalid"
    provider_capture.reading_for(None)
    attempt["exact"] = outcomes
    return found, recoveries, unavailable


def _save_exact(run_file, found, recoveries, status, hold=None):
    hold = hold or {}
    counts = Counter(proof["id"] for proof in found.values())
    with budget.transaction(budget.ledger_path(run_file)) as saved:
        budget.check_run_identity(run_file, saved)
        for rid, recovery in recoveries.items():
            call = saved["calls"][rid]
            if (call.get("idempotency_key") == recovery["idempotency_key"] and not call.get("execution_recovery")
                    and call["actual_credits"] is None):
                call["execution_recovery"] = recovery
        for rid in hold:
            call = saved["calls"][rid]
            if _holdable(call, saved):
                budget.hold_unknown_charge(call)
                call["hold_reason"] = hold[rid]
        used = {call["billing_evidence"]["id"]: rid for rid, call in saved["calls"].items()
                if call.get("billing_evidence")}
        for rid, proof in found.items():
            call = saved["calls"][rid]
            if (counts[proof["id"]] != 1 or used.get(proof["id"], rid) != rid
                    or call["actual_credits"] is not None
                    or (saved["version"] == 2 and call.get("actual_usd") is not None)):
                continue
            if call.get("billing_evidence") and call["billing_evidence"] != proof:
                call.setdefault("billing_history", []).append(call["billing_evidence"])
            call.update(billing_evidence=proof, billing_issue=None,
                        actual_credits=str(budget.amount(proof["credits"], "posted credits")))
            if saved["version"] == 2:
                for held in ("held_credits", "unknown_charge", "hold_reason"):
                    call.pop(held, None)
                call["state"] = "settled"
            if budget.price_overrun(call, saved):
                saved["blocked"] = budget.PRICE_OVERRUN
            if rid not in status["matched"]:
                status["matched"].append(rid)
            if rid in status["unmatched"]:
                status["unmatched"].remove(rid)


def reconcile(run_file, *, fetch=None, refresh=False, resume=False, timeout_seconds=READ_TIMEOUT_SECONDS,
              lookup=None, recover=None):
    run_file = Path(run_file).resolve(strict=True)
    with ExitStack() as owner:
        owner.enter_context(provider_capture.route(None, paid=False))
        try:
            owner.enter_context(run_coordination.locked(run_file, 'billing-reconciliation', blocking=False))
        except BlockingIOError:
            path = run_file.parent / 'billing-status.json'
            return {**(budget.read_object(path) if path.exists() else {}), 'in_progress': True}
        try:
            return _reconcile(run_file, fetch=fetch, refresh=refresh, resume=resume, timeout_seconds=timeout_seconds,
                              lookup=lookup, recover=recover)
        except _RecoveryBusy:
            path = run_file.parent / 'billing-status.json'
            return {**(budget.read_object(path) if path.exists() else {}),
                    'in_progress': True, 'waiting_for': 'run_state'}


def _reconcile(run_file, *, fetch, refresh, resume, timeout_seconds, lookup=None, recover=None):
    run_file = Path(run_file).resolve(strict=True)
    with _state_update(run_file):
        forget_refused_keys(run_file)
        settle_free_calls(run_file)
        hold_unpriced_calls(run_file)
        hold_unbillable_calls(run_file)
    document = budget.read_object(run_file)
    ledger = budget.load_ledger(run_file)
    routes = {row["route_id"]: row for row in document.get("routes", [])}
    exact = lookup is not None or (fetch is None and _api_key_or_none() is not None)
    receipts, keyed, missing_ids = {}, {}, []
    for rid, call in ledger["calls"].items():
        if (call["provider"] != "deepline" or call["actual_credits"] is not None
                or (ledger["version"] == 2 and call.get("actual_usd") is not None) or rid not in routes
                or (call.get("unknown_charge")
                    and not (exact and (call.get("idempotency_key")
                                        or call.get("hold_reason") in (ASYNC_HOLD, FAILED_UNKNOWN,
                                                                       INPUT_REJECTED, budget.UNPRICED_HOLD))))):
            continue
        receipt = budget.read_object(run_file.parent / "receipts" / (rid + ".json"))
        if (receipt.get("run_fingerprint") != budget.run_fingerprint(run_file)
                or receipt.get("request_fingerprint") != routes[rid].get("request_fingerprint")):
            raise ValueError("Billing reconciliation requires this run's matching receipt")
        if receipt.get("job_id") or receipt.get("request_id"):
            receipts[rid] = receipt
        elif exact and call.get("idempotency_key") and not _key_refused(receipt):
            keyed[rid] = receipt
            if not call.get("unknown_charge") and not call.get("execution_recovery"):
                missing_ids.append(rid)
        else:
            missing_ids.append(rid)
    provider_capture.keeping(functools.partial(_kept_reply, {**receipts, **keyed}, ledger["calls"]))
    signature = hashlib.sha256(json.dumps(sorted(ledger["calls"])).encode()).hexdigest()
    status_path = run_file.parent / "billing-status.json"
    status = budget.read_object(status_path) if status_path.exists() else {}
    if status.get("attempt_signature") != signature:
        status = {"attempt_signature": signature, "attempts": 0,
                  **({"billing_org_id": status["billing_org_id"]} if status.get("billing_org_id") else {})}
    status["missing_request_ids"] = sorted(missing_ids)
    if missing_ids:
        status["action_required"] = "Provider omitted billing correlation IDs. Preserve receipts; obtain attributable billing evidence. Never replay the paid calls."
    else:
        status.pop("action_required", None)
    if resume:
        status["attempt_limit"] = status.get("attempts", 0) + MAX_ATTEMPTS
        status.pop("next_cursor", None)
    attempt_limit = status.get("attempt_limit", MAX_ATTEMPTS)
    attempts = status.get("attempts", 0)
    due = resume or refresh or time.time() >= status.get("last_attempt_at", 0) + RETRY_AFTER_SECONDS
    if (receipts or keyed) and attempts < attempt_limit and due:
        read_deadline = time.monotonic() + max(0, min(timeout_seconds, READ_TIMEOUT_SECONDS))
        status.update(matched=[], unmatched=sorted(receipts.keys() | keyed.keys()))
        for _ in range(2):
            if status.get("attempts", 0) >= attempt_limit or time.monotonic() >= read_deadline:
                break
            status.update(attempts=status.get("attempts", 0) + 1, last_attempt_at=time.time())
            status.pop("error", None)
            attempt = {"attempt": status["attempts"], "pages": []}
            status.setdefault("read_attempts", []).append(attempt)
            _save_status(status_path, status)
            entries, cursor = [], status.get("next_cursor")
            cursors = {cursor} if cursor else set()
            deadline = read_deadline
            try:
                key = deepline_http.api_key() if fetch is None else None
                exact_failed = transport_failed = False
                exact_until = deadline - min(EXACT_READ_SECONDS, max(0, deadline - time.monotonic()) / 3)
                reads = lookup, recover
                if lookup is None and key:
                    def read_timeout():
                        return max(.001, min(EXACT_READ_SECONDS, exact_until - time.monotonic()))
                    reads = (lambda request_id: deepline_http.charge_by_request(request_id, key=key, timeout=read_timeout()),
                             lambda execution_key: deepline_http.execution_by_key(execution_key, key=key, timeout=read_timeout()))
                if reads[0] is not None:
                    found, recoveries, exact_failed = _exact_reads(
                        run_file, {rid: receipt for rid, receipt in {**receipts, **keyed}.items()
                                   if rid in status["unmatched"]},
                        status, attempt, deadline=exact_until, lookup=reads[0], recover=reads[1])
                    read = {**receipts, **keyed}
                    hold = {rid: HELD_OUTCOMES.get(outcome, INPUT_REJECTED)
                            for rid, outcome in attempt["exact"].items()
                            if outcome in HELD_OUTCOMES
                            or (outcome == "no_record" and rejected_before_execution(read[rid]))}
                    with _state_update(run_file):
                        if found or recoveries or hold:
                            _save_exact(run_file, found, recoveries, status, hold)
                            _synchronize(run_file)
                        _save_status(status_path, status)
                waiting = {rid for rid, outcome in attempt.get("exact", {}).items() if outcome in NOT_FINAL}
                pending = {rid: receipt for rid, receipt in receipts.items()
                           if rid in status["unmatched"] and rid not in waiting}
                def unsettled():
                    return [rid for rid in status["unmatched"] if rid in pending]
                if not pending:
                    if exact_failed and status["unmatched"]:
                        continue
                    break
                if key and status.get("http_scan_version") != 2:
                    status.update(http_scan_version=2, ledger_cursor=None, api_usage_offset=None)
                for source in (("ledger", "usage") if key else ("usage",)):
                    cursor_field = "ledger_cursor" if source == "ledger" else "api_usage_offset" if key else "next_cursor"
                    backlog = status.get(cursor_field)
                    cursor = None if key else backlog
                    cursors = {cursor} if cursor else set()
                    page_identities = set()
                    for page_number in range(1, 2 if fetch else 5):
                        started = time.monotonic()
                        page_read = {"page": page_number, "continued": bool(cursor), "source": source}
                        attempt["pages"].append(page_read)
                        failure_kind = "response"
                        try:
                            if key:
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise deepline.CallTimeout("Read-only billing lookup timed out; charges remain pending", "", "")
                                failure_kind = "command"
                                payload = deepline_http.billing_page(source, key=key, cursor=cursor, timeout=remaining)
                                failure_kind = "response"
                                if source == "ledger":
                                    raw_rows = payload.get("entries") if isinstance(payload, dict) else None
                                    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
                                        raise ValueError("Billing response has no recognized ledger rows")
                                    payload = dict(payload, recent={"entries": _ledger_rows(raw_rows),
                                        "next_cursor": payload.get("next_cursor") if payload.get("has_more") else None})
                            elif fetch is None:
                                command = [os.environ.get("DEEPLINE_BIN") or "deepline", "billing", "usage", "--limit", "50", "--json"]
                                if cursor:
                                    command += ["--cursor", cursor]
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise deepline.CallTimeout("Read-only billing lookup timed out; charges remain pending", "", "")
                                failure_kind = "command"
                                code, stdout, _ = deepline._invoke(command, remaining)
                                if code:
                                    raise ValueError("Read-only billing lookup unavailable; charges remain pending")
                                failure_kind = "response"
                                payload = deepline._json_from_text(stdout, billing_feed=True)
                            else:
                                payload = fetch()
                            if not isinstance(payload, dict) or not isinstance(payload.get("recent"), dict):
                                raise ValueError("Billing response has no recognized recent-call rows")
                            page = payload["recent"].get("entries")
                            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                                raise ValueError("Billing response has no recognized recent-call rows")
                            failure_kind = "organization"
                            if payload.get("org_id"):
                                if status.get("billing_org_id", payload["org_id"]) != payload["org_id"]:
                                    raise ValueError("Billing organization changed; preserve the original run")
                                status["billing_org_id"] = payload["org_id"]
                            failure_kind = "pagination"
                            next_cursor = payload["recent"].get("next_cursor")
                            if key and source == "usage":
                                recent = payload["recent"]
                                next_cursor = None
                                if recent.get("has_more"):
                                    offset, next_offset = recent.get("offset"), recent.get("next_offset")
                                    if (type(offset) is not int or offset != int(cursor or 0)
                                            or type(next_offset) is not int or next_offset <= offset):
                                        raise ValueError("Invalid or non-advancing billing usage offset")
                                    next_cursor = str(next_offset)
                            if next_cursor and (not isinstance(next_cursor, str) or next_cursor in cursors):
                                raise ValueError("Invalid or repeated billing cursor")
                            identities = frozenset(json.dumps(row, sort_keys=True) for row in page)
                            if identities and identities in page_identities:
                                raise ValueError("Billing pagination repeated records without advancing")
                            page_identities.add(identities)
                            entries.extend([row for row in page if row not in entries])
                            failure_kind = "settlement"
                            with _state_update(run_file):
                                _settle_charges(run_file, pending, entries, status)
                                _synchronize(run_file)
                                cursor = next_cursor if unsettled() else None
                                if key and page_number == 1 and backlog and unsettled():
                                    cursor = backlog
                                status[cursor_field] = cursor
                                page_read.update(outcome="ok", rows=len(page),
                                                 elapsed_seconds=round(time.monotonic() - started, 3))
                                status.setdefault("feed_errors", {}).pop(source, None)
                                attempt["unmatched"] = list(status["unmatched"])
                                _save_status(status_path, status)
                            if cursor:
                                cursors.add(cursor)
                        except deepline_http.BillingUnavailable as exc:
                            page_read.update(outcome="error", failure_kind="transport")
                            status.setdefault("feed_errors", {})[source] = str(exc)
                            transport_failed = True
                            break
                        except (ValueError, OSError, KeyError, TypeError, deepline.ConfigError, deepline.CallTimeout) as exc:
                            page_read.update(outcome="error", failure_kind="timeout" if isinstance(exc, deepline.CallTimeout) else failure_kind)
                            raise
                        finally:
                            page_read["elapsed_seconds"] = round(time.monotonic() - started, 3)
                            attempt["unmatched"] = list(status["unmatched"])
                            _save_status(status_path, status)
                        if not cursor:
                            break
                    if not unsettled():
                        break
                if transport_failed and unsettled():
                    continue
                break
            except (ValueError, OSError, KeyError, TypeError, deepline.ConfigError, deepline.CallTimeout) as exc:
                status["error"] = str(exc)[:500]
            finally:
                _save_status(status_path, status)
    if missing_ids:
        _save_status(status_path, status)
    with _state_update(run_file):
        _synchronize(run_file)
    return status


@provider_capture.route(None, paid=False)
def wait_for_billing(run_file, *, deadline=None, max_wait_seconds=120):
    until = time.monotonic() + max_wait_seconds
    if deadline is not None:
        until = min(until, time.monotonic() + max(0, deadline - time.time()))
    status = {}
    while True:
        remaining = until - time.monotonic()
        if remaining <= 0:
            return status
        status = reconcile(run_file, timeout_seconds=remaining)
        state = budget.load_ledger(run_file)
        if budget.spending_stop(state) != "billing_pending":
            return status
        if status.get("in_progress"):
            time.sleep(min(1, max(0, until - time.monotonic())))
            continue
        if (status.get("missing_request_ids") or not status.get("unmatched")
                or status.get("attempts", 0) >= status.get("attempt_limit", MAX_ATTEMPTS)):
            return status
        delay = max(0, status.get("last_attempt_at", 0) + RETRY_AFTER_SECONDS - time.time())
        remaining = until - time.monotonic()
        if delay >= remaining:
            return status
        time.sleep(min(delay, RETRY_AFTER_SECONDS))


def _synchronize(run_file):
    ledger = budget.load_ledger(run_file)
    def synchronize(saved):
        for route in saved.get("routes", []):
            call = ledger["calls"].get(route["route_id"], {})
            if (call.get("billing_evidence") or call.get("free_evidence")) and call["actual_credits"] is not None:
                actual = budget.report_amount(call["actual_credits"])
                route.update(cost_credits=actual, cost_upper_bound_credits=actual, cost_basis="actual")
                if route.get("billing_basis") == budget.UNKNOWN_CHARGE_BASIS:
                    route.pop("billing_basis")
            elif call.get("unknown_charge") and call.get("held_credits") is not None and call["actual_credits"] is None:
                route.update(cost_upper_bound_credits=budget.report_amount(call["held_credits"]), cost_basis="estimated",
                             billing_basis=call["unknown_charge"])
        from run_attempt import refresh
        refresh(saved)
        return saved
    if any(call.get("billing_evidence") or call.get("free_evidence") or call.get("unknown_charge")
           for call in ledger["calls"].values()):
        mutate(run_file, synchronize)


def evidence_error(run_file, route, call):
    proof = call.get("billing_evidence")
    if not proof:
        return None
    receipt = budget.read_object(Path(run_file).resolve().parent / "receipts" / (route["route_id"] + ".json"))
    contract, _ = _catalog_contract(Path(run_file), receipt, proof.get("catalog_route_id"), bound_call=call)
    if proof.get("lookup") == EXACT_LOOKUP:
        matched = exact_charge(receipt, proof, contract, call_ids(receipt, call, contract))
        if matched is None or call["actual_credits"] is None:
            matched = None
        issue = None
    else:
        matched = matching_charge(receipt, [proof], contract, saved=True)
        issue = billing_issue(receipt, matched, contract=contract) if matched else None
    if (matched and issue is None and call["actual_credits"] is None and call.get("actual_usd") is None
            and call.get("billing_issue") == RESULTS_WITHOUT_CHARGE):
        issue = RESULTS_WITHOUT_CHARGE
    if (receipt.get("run_fingerprint") != budget.run_fingerprint(run_file)
            or receipt.get("request_fingerprint") != route.get("request_fingerprint")
            or matched is None or call.get("billing_issue") != issue
            or (issue and call["actual_credits"] is not None)
            or (not issue and budget.amount(matched["credits"], "posted credits") != budget.amount(call["actual_credits"], "ledger credits"))):
        return "posted billing evidence does not match the saved request and charge"
    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Reconcile saved billing only; never dispatch research or reset spend.")
    parser.add_argument("run_file", type=Path)
    parser.add_argument("--resume", action="store_true", help="Allow up to three more read-only attempts, retaining the audit history")
    args = parser.parse_args()
    from validate_run import refuse_earlier_results
    try:
        refuse_earlier_results(args.run_file)
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(reconcile(args.run_file, resume=args.resume), indent=2))
