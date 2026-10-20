"""Best-effort Arena training observations. Missing SDK support never changes an agent result.

The counts describe this agent's direct web egress and worker-socket responses only.
They do not claim to cover web reads made by a provider or another process.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

try:  # Older worker images do not mount this optional module.
    import lab_arena_checkpoint as CHECKPOINT
except ImportError:
    CHECKPOINT = None

CONTRACT_VERSION = 1
STATE_LIMIT_BYTES = 60 * 1024  # Leave room for the SDK's 64 KiB event envelope.
MAX_DEPENDENCIES = 32  # The mounted SDK rejects a longer depends_on list.
_lock = threading.Lock()
_counts: dict[str, int] = {}
_last_phase_id: int | None = None
_pending_ids: list[int] = []


def reset() -> None:
    global _last_phase_id
    with _lock:
        _counts.clear()
        _last_phase_id = None
        _pending_ids.clear()


def _inc(key: str) -> None:
    with _lock:
        _counts[key] = _counts.get(key, 0) + 1


def _record_result(success_key: str, error_key: str, event_id: Any) -> None:
    success = type(event_id) is int and event_id >= 0
    with _lock:
        key = success_key if success else error_key
        _counts[key] = _counts.get(key, 0) + 1
        if success:
            _pending_ids.append(event_id)


def web_attempt() -> None:
    """A direct egress request passed local guards and consumed a fetch slot."""
    _inc("web_attempts")


def web_failed(url: str, error_class: str) -> None:
    """The request produced no result; keep only its URL, time, and failure class."""
    _inc("web_failures")
    event_id = _stored_snapshot("web_fetch_failure", {
        "url": url, "observed_at": datetime.now(timezone.utc).isoformat(),
        "error_class": error_class[:80],
    })
    _record_result("web_failures_recorded", "web_failure_record_errors", event_id)


def web_result(url: str, result: dict[str, Any]) -> None:
    """Store the precise body handed back by egress_get, before any page parsing."""
    _inc("web_results")
    try:
        event_id = CHECKPOINT.record_web_fetch(
            url=url, final_url=result["final_url"], status=result["status"],
            content_type=result["content_type"], body=result["body"]
        ) if CHECKPOINT is not None else None
    except Exception:  # noqa: BLE001 - observation failure must not affect research
        event_id = None
    _record_result("web_recorded", "web_record_errors", event_id)


def provider_response(status: int, identity: str, digest: str, body_bytes: int) -> None:
    """A fully consumed, successful worker-socket response; record only a digest and size."""
    _inc("provider_2xx_consumed")
    if not identity:
        _inc("provider_missing_identity")
        return
    try:
        event_id = CHECKPOINT.record_response_received(
            call_identity=identity, status=status, body_sha256=digest, body_bytes=body_bytes
        ) if CHECKPOINT is not None else None
    except Exception:  # noqa: BLE001
        event_id = None
    _record_result("provider_acked", "provider_ack_errors", event_id)


def provider_incomplete() -> None:
    _inc("provider_stream_incomplete")


def _stored_snapshot(name: str, value: Any, depends_on: list[str] | None = None) -> int | None:
    try:
        event_id = CHECKPOINT.record_state_snapshot(name, value, depends_on=depends_on) if CHECKPOINT is not None else None
    except Exception:  # noqa: BLE001
        event_id = None
    _inc("state_recorded" if isinstance(event_id, int) and not isinstance(event_id, bool) else "state_record_errors")
    return event_id if isinstance(event_id, int) and not isinstance(event_id, bool) else None


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _boundary_dependencies(name: str, base: list[str], observed_ids: list[int]) -> tuple[list[str], bool]:
    """Join completed observations through bounded barriers, without ordering the observations themselves."""
    refs = list(base)
    remaining = [f"agent:{event_id}" for event_id in observed_ids]
    if len(refs) + len(remaining) <= MAX_DEPENDENCIES:
        return refs + remaining, True
    batch_index = 0
    while remaining:
        count = MAX_DEPENDENCIES - len(refs)
        batch, remaining = remaining[:count], remaining[count:]
        barrier_id = _stored_snapshot(
            f"{name}.barrier.{batch_index}",
            {"completed_observation_count": len(batch), "meaning": "completed_before_boundary"},
            refs + batch)
        if barrier_id is None:
            _inc("boundary_dependency_errors")
            return list(base), False
        refs = [f"agent:{barrier_id}"]
        batch_index += 1
    return refs, True


def _completed_boundary_inputs() -> tuple[list[str], list[int]]:
    with _lock:
        previous = [f"agent:{_last_phase_id}"] if _last_phase_id is not None else []
        return previous, list(_pending_ids)


def _finish_boundary(event_id: int | None, observed_ids: list[int], linked: bool) -> None:
    global _last_phase_id
    if event_id is None:
        return
    with _lock:
        _last_phase_id = event_id
        if linked:
            captured = set(observed_ids)
            _pending_ids[:] = [item for item in _pending_ids if item not in captured]


def phase(name: str, rows: list[dict[str, Any]], *, details: dict[str, Any] | None = None) -> None:
    """Record actual rows at a phase boundary. Split large lists; mark oversized rows explicitly."""
    try:
        value = {"rows": rows, "details": details or {}}
        previous, observed_ids = _completed_boundary_inputs()
        if _size(value) <= STATE_LIMIT_BYTES:
            dependencies, linked = _boundary_dependencies(name, previous, observed_ids)
            event_id = _stored_snapshot(name, value, dependencies or None)
        else:
            refs: list[dict[str, Any]] = []
            row_tail: str | None = None
            for index, row in enumerate(rows):
                size = _size(row)
                if size <= STATE_LIMIT_BYTES:
                    row_id = _stored_snapshot(f"{name}.row.{index}", row, [row_tail] if row_tail else None)
                    refs.append({"index": index, "event": f"agent:{row_id}" if row_id is not None else None})
                else:
                    canonical = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
                    marker = {"index": index, "overflow": True, "body_bytes": len(canonical),
                              "sha256": hashlib.sha256(canonical).hexdigest()}
                    row_id = _stored_snapshot(f"{name}.row_overflow.{index}", marker,
                                              [row_tail] if row_tail else None)
                    refs.append({**marker, "event": f"agent:{row_id}" if row_id is not None else None})
                    _inc("state_overflow_rows")
                if row_id is not None:
                    row_tail = f"agent:{row_id}"
            manifest = {"row_count": len(rows), "row_events": refs, "details": details or {}}
            if _size(manifest) > STATE_LIMIT_BYTES:
                canonical = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
                manifest = {"overflow": True, "body_bytes": len(canonical),
                            "sha256": hashlib.sha256(canonical).hexdigest(), "row_count": len(rows)}
                _inc("state_overflow_manifests")
            base = previous + ([row_tail] if row_tail else [])
            dependencies, linked = _boundary_dependencies(name, base, observed_ids)
            event_id = _stored_snapshot(name, manifest, dependencies or None)
        _finish_boundary(event_id, observed_ids, linked)
    except Exception:  # noqa: BLE001
        _inc("state_local_errors")


def summary() -> dict[str, Any]:
    with _lock:
        counts = dict(_counts)
    attempts = counts.get("web_attempts", 0)
    recorded = counts.get("web_recorded", 0)
    supported = CHECKPOINT is not None and all(callable(getattr(CHECKPOINT, name, None)) for name in (
        "record_web_fetch", "record_response_received", "record_state_snapshot"))
    return {"contract_version": CONTRACT_VERSION, "scope": "agent_direct_egress_and_worker_socket",
            "sdk_available": supported, "web_attempts": attempts,
            "web_results": counts.get("web_results", 0), "web_recorded": recorded,
            "web_no_result": max(0, attempts - counts.get("web_results", 0)),
            "web_failures_recorded": counts.get("web_failures_recorded", 0),
            "web_capture_missing": max(0, counts.get("web_results", 0) - recorded),
            "web_missing": max(0, attempts - recorded), **counts}


def final_summary() -> dict[str, Any]:
    previous, observed_ids = _completed_boundary_inputs()
    dependencies, linked = _boundary_dependencies("training_capture_summary", previous, observed_ids)
    value = summary()
    event_id = _stored_snapshot("training_capture_summary", value, dependencies or None)
    _finish_boundary(event_id, observed_ids, linked)
    return value
