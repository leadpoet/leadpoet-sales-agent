"""Offline pacing checks for the current promoted agent; no paid calls."""

import os
from types import SimpleNamespace
import unittest
from unittest import mock

import harness

governor = harness.governor


def snapshot(allowance=4_000_000, *, admission=4_000_000, spent=0, pending=0, inflight=0):
    return {"sourcing_cost": {
        "admission_cap_microusd": admission,
        "per_qualified_pair_cap_microusd": allowance,
        "successful_microusd": spent,
        "success_unresolved_microusd": pending,
        "success_unresolved_calls": int(pending > 0),
        "inflight_calls": inflight,
    }}


class FlatCostGovernorTest(unittest.TestCase):
    def setUp(self):
        harness.LAST_REPORT.clear()
        governor.install(None)
        self.addCleanup(governor.install, None)

    def start(self, snap):
        tools = SimpleNamespace(adopt_quota=mock.Mock())
        with mock.patch.object(governor, "read_snapshot", return_value=snap):
            return harness._start_governor(None, tools)

    def test_normal_legacy_and_flat_limits_preserve_closing_and_discovery_reserves(self):
        for limit in range(1, 6):
            for allowance, cap, research, discovery in ((800_000, 0.74, 0.70, 0.60),
                                                       (4_000_000, 3.94, 3.90, 3.80)):
                with self.subTest(limit=limit, allowance=allowance), \
                        mock.patch.dict(os.environ, LAB_ARENA_COMPANY_LIMIT=str(limit)):
                    gov = self.start(snapshot(allowance))
                    self.assertEqual(harness._company_limit(), limit)
                    self.assertAlmostEqual(gov.cap, cap)
                    self.assertAlmostEqual(gov.headroom(), research)
                    self.assertAlmostEqual(gov.headroom(closing=True), cap)
                    self.assertAlmostEqual(harness.scout_budget(gov), discovery)
                    token = gov.acquire(research, wait_s=0)
                    with self.assertRaises(governor.Refused):
                        gov.acquire(0.001, wait_s=0)
                    gov.release(token, research)
                    gov.acquire(0.04, closing=True, wait_s=0)

    def test_rescue_uses_host_allowance_and_refresh_cannot_restore_legacy_ceiling(self):
        for allowance, spent, cap in ((800_000, 610_000, 0.78), (4_000_000, 3_810_000, 3.98)):
            with self.subTest(allowance=allowance):
                gov = self.start(snapshot(allowance, spent=spent))
                self.assertTrue(harness.LAST_REPORT["rescue"])
                self.assertAlmostEqual(gov.cap, cap)
                self.assertEqual(gov.reserve, 0.02)
                governor.install(gov)
                with mock.patch.object(governor, "read_snapshot", return_value=snapshot(allowance, spent=spent)):
                    self.assertTrue(governor.refresh(force=True))
                self.assertAlmostEqual(gov.cap, cap)
                self.assertAlmostEqual(gov.mode_cap, cap)
        gov.adopt_quota(snapshot(800_000))
        self.assertAlmostEqual(gov.cap, 0.78)
        self.assertAlmostEqual(gov.mode_cap, 0.78)

    def test_cap_refresh_respects_lower_admission_missing_values_and_explicit_lean_mode(self):
        gov = self.start(snapshot())
        gov.adopt_quota(snapshot(admission=2_000_000))
        self.assertAlmostEqual(gov.cap, 1.94)
        for invalid in (None, -1, True, 4_000_000.0, "4000000"):
            gov.adopt_quota(snapshot(invalid))
            self.assertAlmostEqual(gov.cap, 1.94)
        gov.adopt_quota(None)
        self.assertAlmostEqual(gov.cap, 1.94)
        gov.set_mode_cap(0.40)
        gov.adopt_quota(snapshot())
        self.assertAlmostEqual(gov.cap, 3.94)
        self.assertEqual(gov.mode_cap, 0.40)
        gov.adopt_quota(snapshot(0))
        self.assertEqual(gov.cap, 0.0)
        self.assertEqual(gov.mode_cap, 0.0)
        gov.adopt_quota(snapshot())
        self.assertAlmostEqual(gov.cap, 3.94)
        self.assertEqual(gov.mode_cap, 0.40)
        gov.adopt_quota(snapshot(200_000))
        self.assertAlmostEqual(gov.mode_cap, 0.14)
        gov.adopt_quota(snapshot())
        self.assertEqual(gov.mode_cap, 0.40)

    def test_missing_allowance_keeps_legacy_fallback_and_unknown_start_keeps_lean_mode(self):
        snap = snapshot()
        del snap["sourcing_cost"]["per_qualified_pair_cap_microusd"]
        self.assertAlmostEqual(self.start(snap).cap, 0.74)
        gov = self.start(None)
        self.assertAlmostEqual(gov.cap, 0.74)
        self.assertEqual(gov.mode_cap, harness.LEAN_CAP_USD)

    def test_prior_attempts_pending_charges_and_local_reservations_share_one_cap(self):
        gov = self.start(snapshot(spent=3_000_000, pending=200_000))
        self.assertAlmostEqual(gov.committed(), 3.22)  # Existing unresolved-call pad.
        first = gov.acquire(0.40, wait_s=0)
        second = gov.acquire(0.28, wait_s=0)
        with self.assertRaises(governor.Refused):
            gov.acquire(0.001, wait_s=0)
        gov.release(first, 0.35)
        gov.release(second, 0.28)
        self.assertAlmostEqual(gov.committed(), 3.85)
        marker = gov.marker()
        gov.charge(0.01)
        gov.note_snapshot(3.85, marker)
        self.assertAlmostEqual(gov.committed(), 3.86)
        retry = self.start(snapshot(spent=3_980_000))
        self.assertAlmostEqual(retry.committed(), 3.98)
        with self.assertRaises(governor.Refused):
            retry.acquire(0.001, closing=True, wait_s=0)

    def test_micro_usd_precision_large_values_and_late_settlement(self):
        gov = self.start(snapshot(3_999_999))
        self.assertAlmostEqual(gov.cap, 3.939999)
        gov.adopt_quota(snapshot(10 ** 400, admission=10 ** 400))
        self.assertAlmostEqual(gov.cap, 3.94)
        gov.note_snapshot(3.940001)
        with self.assertRaises(governor.Refused):
            gov.acquire(0.001, closing=True, wait_s=0)


if __name__ == "__main__":
    unittest.main()
