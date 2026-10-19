"""Offline quota pacing checks; no provider or model calls."""

import copy
import os
import sys
import types
import unittest
from unittest import mock

import harness


def snapshot(*, allowance=4_000_000, admission=4_000_000, spent=0, unresolved=0):
    return {"sourcing_cost": {
        "per_qualified_pair_cap_microusd": allowance,
        "admission_cap_microusd": admission,
        "successful_microusd": spent,
        "success_unresolved_microusd": unresolved,
        "inflight_calls": 1,
        "success_unresolved_calls": int(unresolved > 0),
    }}


class SpendCapsTest(unittest.TestCase):
    def setUp(self):
        harness.LAST_REPORT.clear()
        self.read = mock.Mock(return_value=snapshot())
        checkpoint = types.SimpleNamespace(quota_usage=self.read)
        self.modules = mock.patch.dict(sys.modules, lab_arena_checkpoint=checkpoint)
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def tools(self):
        return harness.ArenaTools(post=mock.Mock(side_effect=AssertionError("provider dispatch")))

    def test_legacy_and_flat_allowances_keep_fixed_reserves_for_all_company_limits(self):
        for limit in range(1, 6):
            for allowance, research, total in ((800_000, 0.64, 0.72), (4_000_000, 3.84, 3.92)):
                with self.subTest(limit=limit, allowance=allowance), \
                        mock.patch.dict(os.environ, LAB_ARENA_COMPANY_LIMIT=str(limit)):
                    harness.LAST_REPORT.clear()
                    self.read.return_value = snapshot(allowance=allowance)
                    tools = self.tools()
                    harness.arm_spend_guard(tools)
                    self.assertEqual(harness._company_limit(), limit)
                    self.assertEqual(tools.spend_cap_usd, research)
                    self.assertEqual(harness.LAST_REPORT["caps_usd"], {"research": research, "total": total})
                    harness.release_holdback(tools)
                    self.assertEqual(tools.spend_cap_usd, total)
                    tools._post.assert_not_called()
        self.read.assert_called_with(include_sourcing_cost=True)

    def test_lower_host_or_strategy_cap_binds_and_zero_grants_no_paid_work(self):
        tools = self.tools()
        for allowance, admission in ((2_000_000, 4_000_000), (4_000_000, 2_000_000)):
            self.read.return_value = snapshot(allowance=allowance, admission=admission)
            harness.arm_spend_guard(tools)
            self.assertEqual(tools.spend_cap_usd, 1.84)
            harness.release_holdback(tools)
            self.assertEqual(tools.spend_cap_usd, 1.92)
        self.read.return_value = snapshot()
        with mock.patch.object(harness, "ICP_SPEND_CAP_USD", 1.0):
            harness.arm_spend_guard(tools)
            self.assertEqual(tools.spend_cap_usd, 0.84)
            harness.release_holdback(tools)
            self.assertEqual(tools.spend_cap_usd, 0.92)
        self.read.return_value = snapshot(allowance=0)
        harness.refresh_spend(tools)
        self.assertEqual(tools.spend_cap_usd, 0.0)
        self.assertFalse(tools.paid_allowed(0.001))

    def test_quota_precision_does_not_round_up_and_large_caps_stay_bounded(self):
        tools = self.tools()
        self.read.return_value = snapshot(allowance=3_999_999)
        harness.arm_spend_guard(tools)
        self.assertEqual(tools.spend_cap_usd, 3.839999)
        harness.release_holdback(tools)
        self.assertEqual(tools.spend_cap_usd, 3.919999)
        self.read.return_value = snapshot(allowance=10 ** 400, admission=10 ** 400)
        harness.refresh_spend(tools)
        self.assertEqual(tools.spend_cap_usd, 3.92)

    def test_refresh_and_release_use_the_latest_valid_cap(self):
        tools = self.tools()
        harness.arm_spend_guard(tools)
        self.assertEqual(tools.spend_cap_usd, 3.84)
        self.read.return_value = snapshot(allowance=800_000)
        harness.refresh_spend(tools)
        self.assertEqual(tools.spend_cap_usd, 0.64)
        harness.release_holdback(tools)
        self.assertEqual(tools.spend_cap_usd, 0.72)
        self.read.return_value = snapshot()
        harness.refresh_spend(tools)
        self.assertEqual(tools.spend_cap_usd, 3.92)
        self.read.return_value = snapshot(allowance=800_000)
        harness.refresh_spend(tools)
        self.assertEqual(tools.spend_cap_usd, 0.72)

    def test_invalid_allowance_keeps_last_good_or_legacy_fallback(self):
        for value in (None, -1, True, 4_000_000.0, "4000000", float("nan")):
            with self.subTest(value=value):
                harness.LAST_REPORT.clear()
                self.read.return_value = snapshot(allowance=value)
                tools = self.tools()
                harness.arm_spend_guard(tools)
                self.assertEqual(tools.spend_cap_usd, 0.64)
                harness.release_holdback(tools)
                self.assertEqual(tools.spend_cap_usd, 0.72)
                self.read.return_value = snapshot()
                harness.refresh_spend(tools)
                self.assertEqual(tools.spend_cap_usd, 3.92)
                bad = copy.deepcopy(self.read.return_value)
                bad["sourcing_cost"]["admission_cap_microusd"] = value
                self.read.return_value = bad
                harness.refresh_spend(tools)
                self.assertEqual(tools.spend_cap_usd, 3.92)
        missing = snapshot()
        del missing["sourcing_cost"]["admission_cap_microusd"]
        harness.LAST_REPORT.clear()
        self.read.return_value = missing
        tools = self.tools()
        harness.arm_spend_guard(tools)
        self.assertEqual(tools.spend_cap_usd, 0.64)

    def test_unknown_start_stays_closed_through_release_then_recovers_to_host_cap(self):
        tools = self.tools()
        self.read.return_value = None
        with mock.patch.object(harness.time, "sleep"):
            harness.arm_spend_guard(tools)
        self.assertEqual(tools.spend_cap_usd, 0.0)
        harness.release_holdback(tools)
        self.assertEqual(tools.spend_cap_usd, 0.0)
        self.assertFalse(tools.paid_allowed(0.001))
        self.read.return_value = snapshot(allowance=800_000)
        harness.refresh_spend(tools)
        self.assertFalse(tools._unknown_start)
        self.assertEqual(tools.spend_cap_usd, 0.72)

    def test_retry_totals_unresolved_and_local_ledger_share_the_same_cap(self):
        self.read.return_value = snapshot(spent=3_500_000, unresolved=200_000)
        first = self.tools()
        harness.arm_spend_guard(first)
        self.assertEqual(first.spend_usd(), 3.7)
        self.assertTrue(first.paid_allowed(0.14))
        first.note_external_spend(0.14)
        self.assertEqual(first.spend_usd(), 3.84)
        self.assertFalse(first.paid_allowed(0.001))
        # An older snapshot cannot erase charges recorded since its observation.
        harness.refresh_spend(first)
        self.assertEqual(first.spend_usd(), 3.84)
        self.read.return_value = snapshot(spent=3_840_000)
        retry = self.tools()
        harness.arm_spend_guard(retry)
        self.assertEqual(retry.spend_usd(), 3.84)
        self.assertFalse(retry.paid_allowed(0.001))
        harness.release_holdback(retry)
        self.assertTrue(retry.paid_allowed(0.08))
        self.read.return_value = snapshot(spent=3_920_000)
        harness.refresh_spend(retry)
        self.assertFalse(retry.paid_allowed(0.001))


if __name__ == "__main__":
    unittest.main()
