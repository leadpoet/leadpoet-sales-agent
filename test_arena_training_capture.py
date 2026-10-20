"""Offline checks for exact, best-effort Arena observation boundaries."""

import hashlib
import importlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

import harness

training = harness.arena_training
transport = importlib.import_module(f"{training.__package__}.arena_transport")
tools_module = importlib.import_module(f"{training.__package__}.arena_tools")


class FakeCheckpoint:
    def __init__(self):
        self.web = []
        self.receipts = []
        self.states = []
        self.fail_web = False
        self.fail_state_names = set()
        self.events = {}
        self.lock = threading.Lock()

    def _id(self, depends_on=None):
        with self.lock:
            event_id = len(self.events)
            assert len(depends_on or []) <= 32
            assert all(int(ref.removeprefix("agent:")) < event_id for ref in depends_on or [])
            self.events[event_id] = list(depends_on or [])
            return event_id

    def record_web_fetch(self, **kwargs):
        self.web.append(kwargs)
        if self.fail_web:
            raise OSError("checkpoint unavailable")
        return self._id()

    def record_response_received(self, **kwargs):
        self.receipts.append(kwargs)
        return self._id()

    def record_state_snapshot(self, name, value, *, depends_on=None):
        if name in self.fail_state_names:
            return None
        self.states.append((name, value, depends_on))
        return self._id(depends_on)


class ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.closed = False

    def __iter__(self):
        yield from self.chunks
        if self.fail:
            raise httpx.ReadError("stream failed")

    def close(self):
        self.closed = True


class ResponseTransport(httpx.BaseTransport):
    def __init__(self, response):
        self.response = response

    def handle_request(self, request):
        return self.response


class ArenaTrainingCaptureTest(unittest.TestCase):
    def setUp(self):
        training.reset()
        self.checkpoint = FakeCheckpoint()
        patcher = mock.patch.object(training, "CHECKPOINT", self.checkpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_receipt_waits_for_complete_chunked_response_and_keeps_bytes(self):
        chunks = [b'{"ok":', b'true}', b'']
        response = httpx.Response(200, headers={"x-leadpoet-call-identity": "sha256:" + "a" * 64},
                                  stream=ChunkStream(chunks))
        with httpx.Client(transport=transport.ArenaSocketTransport(inner=ResponseTransport(response))) as client:
            with client.stream("GET", "http://openrouter.ai/test") as got:
                self.assertEqual(self.checkpoint.receipts, [])
                self.assertEqual(list(got.iter_raw()), chunks[:2])  # HTTPX drops empty chunks.
        self.assertEqual(len(self.checkpoint.receipts), 1)
        receipt = self.checkpoint.receipts[0]
        self.assertEqual(receipt["body_sha256"], "sha256:" + hashlib.sha256(b"".join(chunks)).hexdigest())
        self.assertEqual(receipt["body_bytes"], len(b"".join(chunks)))
        self.assertNotIn("body", receipt)
        self.assertEqual(training.summary()["provider_acked"], 1)

    def test_early_close_and_stream_error_never_ack(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                training.reset()
                self.checkpoint.receipts.clear()
                stream = ChunkStream([b"one", b"two"], fail=fail)
                response = httpx.Response(200, headers={"x-leadpoet-call-identity": "sha256:" + "b" * 64},
                                          stream=stream)
                with httpx.Client(transport=transport.ArenaSocketTransport(inner=ResponseTransport(response))) as client:
                    with client.stream("GET", "http://openrouter.ai/test") as got:
                        if fail:
                            with self.assertRaises(httpx.ReadError):
                                list(got.iter_raw())
                        else:
                            self.assertEqual(next(got.iter_raw()), b"one")
                self.assertEqual(self.checkpoint.receipts, [])
                self.assertEqual(training.summary()["provider_stream_incomplete"], 1)
                self.assertTrue(stream.closed)

    def test_missing_identity_and_already_read_response_are_counted(self):
        for headers in ({}, {"x-leadpoet-call-identity": "sha256:" + "c" * 64}):
            response = httpx.Response(200, headers=headers, content=b"already read")
            with httpx.Client(transport=transport.ArenaSocketTransport(inner=ResponseTransport(response))) as client:
                self.assertEqual(client.get("http://openrouter.ai/test").content, b"already read")
        counts = training.summary()
        self.assertEqual(counts["provider_missing_identity"], 1)
        self.assertEqual(counts["provider_acked"], 1)

    def test_egress_returns_identical_body_when_capture_fails(self):
        body = "caf\u00e9 \ufffd"
        raw = body.encode("utf-8")
        def handler(request):
            return httpx.Response(206, headers={"content-type": "text/plain; charset=utf-8"}, content=raw,
                                  request=request)
        agent_tools = tools_module.ArenaTools(post=lambda *_: {})
        agent_tools._egress_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
        first = agent_tools.egress_get("https://example.org/page")
        self.assertEqual(first["body"], body)
        self.assertEqual(self.checkpoint.web[0]["body"], first["body"])
        self.checkpoint.fail_web = True
        second = agent_tools.egress_get("https://example.org/page")
        self.assertEqual(second, first)
        self.assertEqual(training.summary()["web_missing"], 1)
        self.assertEqual(training.summary()["web_capture_missing"], 1)
        agent_tools.close()

    def test_phase_rows_split_without_silent_prefix_loss_and_summary_is_explicit(self):
        rows = [{"company_name": "A", "proof": "x" * 31_000},
                {"company_name": "B", "proof": "y" * 31_000}]
        training.phase("after_discovery", rows)
        names = [name for name, _, _ in self.checkpoint.states]
        self.assertEqual(names, ["after_discovery.row.0", "after_discovery.row.1", "after_discovery"])
        self.assertEqual(self.checkpoint.states[0][1], rows[0])
        self.assertEqual(self.checkpoint.states[-1][1]["row_count"], 2)
        summary = training.final_summary()
        self.assertEqual(summary["contract_version"], 1)
        self.assertEqual(summary["web_missing"], 0)
        self.assertEqual(self.checkpoint.states[-1][0], "training_capture_summary")

    def test_egress_transport_failure_is_not_misreported_as_capture_failure(self):
        def handler(request):
            raise httpx.ConnectError("offline", request=request)
        agent_tools = tools_module.ArenaTools(post=lambda *_: {})
        agent_tools._egress_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
        self.assertIsNone(agent_tools.egress_get("https://example.org/page"))
        counts = training.summary()
        self.assertEqual((counts["web_attempts"], counts["web_results"], counts["web_no_result"]), (1, 0, 1))
        self.assertEqual(counts["web_capture_missing"], 0)
        agent_tools.close()

    def test_oversized_single_row_is_marked_with_digest(self):
        row = {"proof": "z" * 70_000}
        training.phase("after_verification", [row])
        name, marker, _ = self.checkpoint.states[0]
        self.assertEqual(name, "after_verification.row_overflow.0")
        self.assertTrue(marker["overflow"])
        self.assertEqual(training.summary()["state_overflow_rows"], 1)

    def test_many_rows_and_parallel_observations_keep_dependencies_bounded(self):
        def one_observation(index):
            if index % 2:
                training.web_result(f"https://example.org/{index}", {
                    "status": 200, "final_url": f"https://example.org/{index}",
                    "content_type": "text/plain", "body": str(index)})
            else:
                training.provider_response(200, "sha256:" + f"{index:064x}",
                                           "sha256:" + hashlib.sha256(str(index).encode()).hexdigest(), 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(one_observation, range(80)))
        observed_ids = set(self.checkpoint.events)
        rows = [{"company_name": f"Company {i}", "proof": "x" * 2000} for i in range(40)]
        training.phase("after_discovery", rows)
        self.assertEqual(self.checkpoint.states[-1][0], "after_discovery")
        self.assertGreater(len([name for name, _, _ in self.checkpoint.states if ".barrier." in name]), 1)
        terminal_id = max(self.checkpoint.events)
        reachable = set()
        def visit(event_id):
            if event_id in reachable:
                return
            reachable.add(event_id)
            for reference in self.checkpoint.events[event_id]:
                visit(int(reference.removeprefix("agent:")))
        visit(terminal_id)
        self.assertTrue(observed_ids.issubset(reachable))
        self.assertEqual(reachable, set(self.checkpoint.events))
        self.assertEqual(training.summary().get("boundary_dependency_errors", 0), 0)

    def test_failed_phase_keeps_completed_observations_for_next_boundary(self):
        training.web_result("https://example.org/1", {"status": 200, "final_url": "https://example.org/1",
                            "content_type": "text/plain", "body": "one"})
        web_id = max(self.checkpoint.events)
        self.checkpoint.fail_state_names.add("after_discovery")
        training.phase("after_discovery", [])
        self.checkpoint.fail_state_names.clear()
        training.phase("after_verification", [])
        final_id = max(self.checkpoint.events)
        self.assertIn(f"agent:{web_id}", self.checkpoint.events[final_id])

    def test_no_draft_harness_run_keeps_result_and_records_boundaries(self):
        fake_tools = SimpleNamespace(call_budget=10, remaining=lambda: 10, deadline=None,
                                     calls=0, calls_by_tool={}, egress={}, close=lambda: None)
        fake_governor = SimpleNamespace(mode_cap=0.4, prior=0.0, allowance_usd=0.8,
                                        committed=lambda: 0.0, as_dict=lambda: {})
        with mock.patch.dict(os.environ, ARENA_TOOLS_FACTORY="fixture:factory"), \
                mock.patch.object(harness, "_tools_factory", return_value=lambda: fake_tools), \
                mock.patch.object(harness, "_start_governor", return_value=fake_governor), \
                mock.patch.object(harness, "_print_gates", return_value={}), \
                mock.patch.object(harness, "discovery_outage", return_value=""), \
                mock.patch.object(harness.scout_module, "run_scout", return_value=[]), \
                mock.patch.object(harness.governor, "install"):
            self.assertEqual(harness.run_icp({"icp_id": "offline-fixture"}), [])
        names = [name for name, _, _ in self.checkpoint.states]
        self.assertEqual(names, ["after_discovery", "final", "training_capture_summary"])
        self.assertEqual(harness.LAST_REPORT["training_capture_summary"]["web_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
