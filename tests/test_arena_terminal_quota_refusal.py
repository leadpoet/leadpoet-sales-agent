"""One real Arena quota refusal after finalization consumes its call quota."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit

import pytest

from tyche_arena import host


class QuotaUnavailable(RuntimeError):
    pass


def quota_snapshot(used):
    return {
        "providers": {
            "openrouter": {
                "limit": 200,
                "used": used,
                "remaining": 200 - used,
                "inflight": 0,
            },
        },
    }


def headroom_guard(reader, monkeypatch, *, clock=time.monotonic):
    monkeypatch.setattr(host, "QUOTA_SNAPSHOT_FRESHNESS_SECONDS", 0)
    guard = host.ArenaQuotaGuard(
        reader,
        QuotaUnavailable,
        clock() + 60,
        clock() + 120,
        clock=clock,
    )
    guard.preflight()
    assert guard() is False
    assert guard.research_denial == host.ARENA_FINALIZATION_HEADROOM
    guard.set_phase("finalization")
    return guard


def reviewed_checkpoint(directory, monkeypatch):
    document = {
        "request": {"original_text": "{}"},
        "accepted": [],
        "final_review": {"review_ref": "review-1"},
    }
    run_bytes = json.dumps(document, sort_keys=True).encode()
    validation = {
        "valid": True,
        "scope": host.ARENA_HEADROOM_VALIDATION_SCOPE,
        "partial": True,
        "delivery_allowed": False,
        "host_stop_reason": host.ARENA_FINALIZATION_HEADROOM,
        "results_sha256": hashlib.sha256(run_bytes).hexdigest(),
        "review_ref": "review-1",
    }
    files = {
        "results.json": run_bytes,
        "validation.json": json.dumps(validation, sort_keys=True).encode(),
        "checkpoint-results.json": json.dumps(document, sort_keys=True).encode(),
        "companies.json": b'{"companies":[]}',
    }
    for name, contents in files.items():
        (directory / name).write_bytes(contents)
    monkeypatch.setattr(host.run_attempt, "review_fingerprint", lambda _document: "review-1")
    monkeypatch.setattr(host.budget_guard, "audit_ledger", lambda *_args: [])
    monkeypatch.setattr(host, "checkpointed_companies", lambda *_args: [])
    monkeypatch.setenv("LAB_ARENA_OUTPUT_PATH", str(directory / "companies.json"))
    assert host.headroom_partial_delivery(directory) is True
    return files


def load_arena_stack():
    source = os.environ.get("LAB_ARENA_REFERENCE_SOURCE")
    if not source:
        pytest.skip("set LAB_ARENA_REFERENCE_SOURCE for the real bridge seam")
    root = Path(source)
    helper_path = root / "tests" / "lab_arena" / "test_lab_arena_broker.py"
    if not helper_path.is_file():
        pytest.skip("authoritative Arena broker fixture is unavailable")
    inserted = str(root) not in sys.path
    if inserted:
        sys.path.insert(0, str(root))
    try:
        from lab_arena import lab_arena_codex, runner
        spec = importlib.util.spec_from_file_location(
            "arena_terminal_refusal_broker_fixture", helper_path,
        )
        fixtures = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(fixtures)
    finally:
        if inserted:
            sys.path.remove(str(root))
    return lab_arena_codex, runner, fixtures


def response(index):
    return {
        "id": "response-%03d" % index,
        "object": "response",
        "created_at": 1789488000,
        "status": "completed",
        "model": "openai/gpt-4o-mini",
        "error": None,
        "output": [{
            "id": "message-%03d" % index,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": "fixture",
                "annotations": [],
            }],
        }],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 10,
            "total_tokens": 20,
            "cost": 0.000012,
        },
    }


@contextmanager
def arena_worker(path):
    arena_codex, runner, fixtures = load_arena_stack()
    upstream = [
        (503, {"error": {"code": "server_error"}}),
        *[(200, response(index)) for index in range(1, 200)],
    ]
    store = fixtures.FakeLedgerStore(per_icp_quota=200)
    broker, store, transport = fixtures.make_broker(
        store=store,
        transport=fixtures.FakeTransport(upstream),
    )

    class Api:
        @staticmethod
        def provider(_run_id, _lease_token, frame):
            return broker.execute(fixtures.CONTEXT, **frame).to_document()

    state = runner.RunState(
        lease={"run_id": "r1", "kind": "execute"},
        lease_token="tok-r1",
    )
    server = runner.WorkerSocketServer(path, Api(), state)
    server.start()
    try:
        yield arena_codex, state, store, transport
    finally:
        server.stop()


def post(bridge):
    target = urlsplit(bridge.base_url)
    body = json.dumps(
        {
            "model": "openai/gpt-4o-mini",
            "input": "Finish the Arena result.",
            "stream": False,
            "store": False,
        },
        separators=(",", ":"),
    )
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=3)
    connection.request(
        "POST",
        "/v1/responses",
        body=body,
        headers={
            "Authorization": "Bearer " + bridge.token,
            "Content-Type": "application/json",
        },
    )
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def test_missing_output_records_one_real_refusal_after_200_dispatches(
        tmp_path, monkeypatch):
    used = [160]
    guard = headroom_guard(lambda: quota_snapshot(used[0]), monkeypatch)
    request_guard = host.HeadroomRequestGuard(guard, tmp_path)

    with tempfile.TemporaryDirectory(prefix="arena-terminal-", dir="/tmp") as directory:
        socket_path = Path(directory) / "worker.sock"
        with arena_worker(socket_path) as (
                arena_codex, state, store, transport):
            with arena_codex.ResponsesBridge(str(socket_path)) as research_bridge:
                statuses = [post(research_bridge)[0] for _ in range(200)]
            assert statuses == [502] + [200] * 199
            assert len(transport.sent) == 200

            used[0] = 200
            with arena_codex.ResponsesBridge(
                    str(socket_path), request_guard=request_guard) as final_bridge:
                refused_status, refused_body = post(final_bridge)
                assert refused_status == 402
                assert json.loads(refused_body) == {
                    "error": {"code": "budget_refused"},
                }
                assert post(final_bridge)[0] == 429

    assert len(transport.sent) == 200
    assert len(state.calls) == 201
    assert state.calls[0]["error_code"] == "provider_unavailable"
    assert state.calls[-1]["error_code"] == "budget_refused"
    assert state.calls[-1]["reason"] == "per_icp_quota"
    assert state.calls[-1]["action_sequence"] > state.calls[0]["action_sequence"]
    assert sum(call.get("kind") in {"settlement", "uncertain"}
               for call in store.calls.values()) == 200


def test_reviewed_checkpoint_stays_closed_at_exhaustion(tmp_path, monkeypatch):
    checkpoint = reviewed_checkpoint(tmp_path, monkeypatch)
    used = [160]
    guard = headroom_guard(lambda: quota_snapshot(used[0]), monkeypatch)
    used[0] = 200
    request_guard = host.HeadroomRequestGuard(guard, tmp_path)

    assert request_guard() is False
    assert request_guard() is False
    assert all((tmp_path / name).read_bytes() == contents
               for name, contents in checkpoint.items())


def test_finalization_admits_work_then_one_exhaustion_refusal(monkeypatch):
    used = [160]
    guard = headroom_guard(lambda: quota_snapshot(used[0]), monkeypatch)

    used[0] = 199
    assert guard() is True
    used[0] = 200
    assert guard() is True
    assert guard() is False


@pytest.mark.parametrize("failure", ["unavailable", "regressed", "malformed"])
def test_final_refusal_preserves_snapshot_failure_guards(
        tmp_path, monkeypatch, failure):
    reads = [quota_snapshot(160), quota_snapshot(160)]

    def reader():
        if reads:
            return reads.pop(0)
        if failure == "unavailable":
            raise QuotaUnavailable("unavailable")
        if failure == "regressed":
            return quota_snapshot(159)
        return {"providers": {"openrouter": {"limit": 200}}}

    guard = headroom_guard(reader, monkeypatch)
    request_guard = host.HeadroomRequestGuard(guard, tmp_path)

    if failure == "malformed":
        with pytest.raises(ValueError, match="invalid Arena quota snapshot"):
            request_guard()
    else:
        assert request_guard() is False
        assert request_guard() is False
