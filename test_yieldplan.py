"""Offline checks for the per-ICP yield plan; no paid calls."""

import re
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import harness
import yieldplan

governor = harness.governor
admission = harness.admission
scout = harness.scout_module
plans = harness.yieldplan


def snapshot(allowance=4_000_000, *, spent=0, inflight=0, unresolved_calls=0):
    return {"sourcing_cost": {
        "admission_cap_microusd": allowance,
        "per_qualified_pair_cap_microusd": allowance,
        "successful_microusd": spent,
        "success_unresolved_microusd": 0,
        "success_unresolved_calls": unresolved_calls,
        "inflight_calls": inflight,
    }, "providers": {"deepline": {"limit": 0, "used": 0, "remaining": None}}}


def icp(intent, stage, industry="Software"):
    return {"intent_category": intent, "company_stage": stage, "industry": industry}


def call_tools(budget=190):
    """An ArenaTools holding only the call-ceiling state that adopt_quota reads and writes."""

    tools = object.__new__(harness.ArenaTools)
    tools.call_budget, tools.calls, tools._lock = budget, 0, threading.Lock()
    return tools


DEEP = icp("FUNDING", "Series A", "Health Care")
STANDARD = icp("FUNDING", "Seed", "Payments")
LIGHT = icp("HIRING", "Series A")


class YieldPlanTest(unittest.TestCase):
    def setUp(self):
        harness.LAST_REPORT.clear()
        governor.install(None)
        self.addCleanup(governor.install, None)
        self.addCleanup(plans.apply, plans.plan_for({}), harness._yield_modules())

    def start(self, row, snap, *, reads=None):
        """start_plan with every snapshot read answering ``snap`` (or the answers in ``reads``, in order); the
        governor's wait for calls in flight to settle is cut to one read."""

        tools = call_tools()
        answers = {"side_effect": list(reads)} if reads is not None else {"return_value": snap}
        real_read_prior = governor.read_prior

        def read_prior(**kwargs):
            return real_read_prior(max_wait_s=0.0, sleep=lambda _s: None, **kwargs)

        with mock.patch.object(governor, "read_snapshot", **answers) as read, \
                mock.patch.object(governor, "read_prior", read_prior):
            plan, gov = harness.start_plan(row, None, tools)
        return plan, gov, tools, read.call_count

    def budget(self, plan, gov):
        return harness.scout_budget(gov, reserve=harness.discovery_reserve(plan, gov))

    def test_tiers(self):
        cases = (
            (icp("FUNDING", "Series A", "Health Care"), "deep"),
            (icp("PRODUCT_LAUNCH", "Series C+", "Artificial Intelligence"), "deep"),
            (icp("LEADERSHIP_CHANGE", "Public", "Professional Services"), "deep"),
            (icp("REGULATORY_CLEARANCE", ["Series B"], "Financial Services"), "deep"),
            (icp("FUNDING", "Seed", "Payments"), "standard"),
            (icp("FUNDING", "Series A", "Lending and Investments"), "standard"),
            (icp("PRODUCT_LAUNCH", "Series A", "Sales and Marketing"), "standard"),
            (icp("FUNDING", "Private Equity"), "light"),
            (icp("HIRING", "Series A"), "light"),
            (icp("FACILITY_OPENING", "Series B"), "light"),
            (icp("MARKET_EXPANSION", "Series C+"), "light"),
            (icp("PARTNERSHIP", "Public"), "light"),
            (icp("ACQUISITION", "Series B"), "light"),
            ({}, "light"),
        )
        for row, tier in cases:
            with self.subTest(row=row):
                self.assertEqual(yieldplan.tier_for(row), tier)

    def test_apply_scales_from_defaults_and_resets_between_icps(self):
        scout_ns = SimpleNamespace(QUERY_COUNT=8, QUERY_COUNT_VENTURE=8, MAX_CANDIDATES=24, ROSTER_WITH_STAGE_FIRST=24,
                                   EXTRACT_MAX_ROWS=120, SECOND_ROUND_BELOW=3, OWN_ROUND_SEARCH=False)
        roster = SimpleNamespace(ROSTER_SIZE=30, EVENT_COMPANIES=16, FEED_COMPANIES=24)
        stagefirst = SimpleNamespace(MAX_QUERIES=8, MAX_COMPANIES=10)
        admission_ns = SimpleNamespace(MAX_POOL=8, EVIDENCE_RANK=False)
        tools = SimpleNamespace(UNCAPPED_CALL_BUDGET=360)
        modules = {"scout": scout_ns, "roster": roster, "stagefirst": stagefirst, "admission": admission_ns,
                   "arena_tools": tools}
        saved = dict(yieldplan._DEFAULTS)
        yieldplan._DEFAULTS.clear()
        self.addCleanup(lambda: (yieldplan._DEFAULTS.clear(), yieldplan._DEFAULTS.update(saved)))

        def state():
            return (scout_ns.QUERY_COUNT, scout_ns.MAX_CANDIDATES, roster.ROSTER_SIZE, stagefirst.MAX_COMPANIES,
                    scout_ns.EXTRACT_MAX_ROWS, scout_ns.SECOND_ROUND_BELOW, admission_ns.MAX_POOL,
                    tools.UNCAPPED_CALL_BUDGET, admission_ns.EVIDENCE_RANK, scout_ns.OWN_ROUND_SEARCH)

        deep_flat = (14, 41, 51, 17, 204, 5, 12, 800, True, True)
        light = (8, 24, 30, 10, 120, 3, 8, 360, False, False)
        deep_legacy = (8, 24, 30, 10, 120, 3, 8, 360, True, True)
        deep = yieldplan.plan_for(DEEP)
        steps = ((deep, deep_flat), (yieldplan.plan_for(LIGHT), light), (yieldplan.legacy_plan(deep), deep_legacy),
                 (yieldplan.for_allowance(deep, 4.0), deep_flat), (yieldplan.for_allowance(deep, 0.8), deep_legacy),
                 (yieldplan.plan_for(STANDARD), light))
        for plan, expected in steps:
            applied = yieldplan.apply(plan, modules)
            self.assertEqual(state(), expected)
            self.assertEqual(applied["scout.EXTRACT_MAX_ROWS"], expected[4])

    def test_regime_follows_the_host_allowance(self):
        deep = yieldplan.plan_for(DEEP)
        legacy = yieldplan.for_allowance(deep, 0.80)
        self.assertEqual(legacy["regime"], "legacy")
        self.assertEqual({k: legacy[k] for k in yieldplan.LEGACY_DEEP},
                         {"depth": 1.0, "draft_limit": 8, "max_pool": 8, "call_budget": 360,
                          "second_round_below": 3, "post_scout_reserve_usd": 0.10, "discovery_max_s": None})
        self.assertEqual((legacy["cap_usd"], legacy["evidence_rank"], legacy["own_round_search"]), (2.6, True, True))
        flat = yieldplan.for_allowance(deep, 4.0)
        self.assertEqual((flat["regime"], flat["depth"], flat["draft_limit"], flat["max_pool"], flat["call_budget"],
                          flat["second_round_below"], flat["post_scout_reserve_usd"], flat["cap_usd"],
                          flat["discovery_max_s"]),
                         ("flat", 1.7, 14, 12, 800, 5, 0.40, 2.6, 1800.0))
        for allowance, regime in ((2.0, "flat"), (1.99, "legacy"), (None, "legacy"), ("x", "legacy")):
            self.assertEqual(yieldplan.for_allowance(deep, allowance)["regime"], regime)
        for row in (STANDARD, LIGHT):
            plan = yieldplan.plan_for(row)
            for allowance in (0.80, 4.0):
                self.assertEqual({k: v for k, v in yieldplan.for_allowance(plan, allowance).items() if k != "regime"},
                                 plan)
        self.assertEqual((yieldplan.TIERS["standard"]["cap_usd"], yieldplan.TIERS["light"]["cap_usd"]), (1.6, 0.9))

    def test_start_plan_takes_deep_depth_only_under_a_flat_allowance(self):
        for allowance, regime, draft, pool, calls, rows, below, cap, budget, reads in (
                (4_000_000, "flat", 14, 12, 800, 204, 5, 2.6, 2.20, 3),
                (800_000, "legacy", 8, 8, 360, 120, 3, 0.74, 0.60, 2)):
            with self.subTest(allowance=allowance):
                plan, gov, tools, read_count = self.start(DEEP, snapshot(allowance))
                self.assertEqual((plan["tier"], plan["regime"], plan["draft_limit"], admission.MAX_POOL,
                                  tools.call_budget, scout.EXTRACT_MAX_ROWS, scout.SECOND_ROUND_BELOW),
                                 ("deep", regime, draft, pool, calls, rows, below))
                self.assertTrue(admission.EVIDENCE_RANK and scout.OWN_ROUND_SEARCH)
                self.assertEqual(scout.QUERY_COUNT, 14 if regime == "flat" else 8)
                self.assertAlmostEqual(gov.mode_cap, cap)
                self.assertAlmostEqual(self.budget(plan, gov), budget)
                self.assertEqual(read_count, reads)
                report = harness.LAST_REPORT["yield_plan"]
                self.assertEqual((report["tier"], report["regime"], report["call_budget"], report["cap_usd"],
                                  report["discovery_max_s"]),
                                 ("deep", regime, calls, 2.6, 1800.0 if regime == "flat" else None))
                self.assertEqual(report["knobs"]["scout.SECOND_ROUND_BELOW"], below)

    def test_start_plan_standard_and_light_tiers(self):
        for row, tier, flat_cap, flat_budget in ((STANDARD, "standard", 1.6, 1.50), (LIGHT, "light", 0.9, 0.80)):
            for allowance, regime, cap, budget in ((4_000_000, "flat", flat_cap, flat_budget),
                                                   (800_000, "legacy", 0.74, 0.60)):
                with self.subTest(tier=tier, allowance=allowance):
                    plan, gov, tools, read_count = self.start(row, snapshot(allowance))
                    self.assertEqual((plan["tier"], plan["regime"], plan["draft_limit"], tools.call_budget,
                                      admission.MAX_POOL, scout.EXTRACT_MAX_ROWS, scout.SECOND_ROUND_BELOW),
                                     (tier, regime, 8, 360, 8, 120, 3))
                    self.assertFalse(admission.EVIDENCE_RANK or scout.OWN_ROUND_SEARCH)
                    self.assertAlmostEqual(gov.mode_cap, cap)
                    self.assertAlmostEqual(self.budget(plan, gov), budget)
                    self.assertEqual(read_count, 2)

    def test_start_plan_resets_knobs_between_icps(self):
        sequence = ((DEEP, 4_000_000, (14, 204, 5, 12, True, 800)), (LIGHT, 4_000_000, (8, 120, 3, 8, False, 360)),
                    (DEEP, 800_000, (8, 120, 3, 8, True, 360)), (DEEP, 4_000_000, (14, 204, 5, 12, True, 800)),
                    (STANDARD, 4_000_000, (8, 120, 3, 8, False, 360)))
        for row, allowance, expected in sequence:
            _plan, _gov, tools, _reads = self.start(row, snapshot(allowance))
            self.assertEqual((scout.QUERY_COUNT, scout.EXTRACT_MAX_ROWS, scout.SECOND_ROUND_BELOW, admission.MAX_POOL,
                              admission.EVIDENCE_RANK, tools.call_budget), expected)

    def test_lean_start_gets_the_legacy_plan(self):
        plan, gov, tools, _reads = self.start(DEEP, None)
        self.assertEqual((plan["regime"], plan["draft_limit"], tools.call_budget, scout.SECOND_ROUND_BELOW),
                         ("legacy", 8, 190, 3))
        self.assertAlmostEqual(gov.mode_cap, min(harness.LEAN_CAP_USD, gov.cap))

    def test_failed_first_read_with_a_flat_allowance_keeps_the_legacy_plan(self):
        snap = snapshot(4_000_000)
        plan, gov, tools, reads = self.start(DEEP, None, reads=[None, snap, snap])
        self.assertEqual((plan["tier"], plan["regime"], plan["draft_limit"], plan["discovery_max_s"]),
                         ("deep", "legacy", 8, None))
        self.assertAlmostEqual(gov.allowance_usd, 4.0)
        self.assertAlmostEqual(gov.mode_cap, harness.LEAN_CAP_USD)
        self.assertEqual((tools.call_budget, admission.MAX_POOL, scout.QUERY_COUNT, scout.SECOND_ROUND_BELOW),
                         (360, 8, 8, 3))
        self.assertEqual(reads, 2)
        self.assertAlmostEqual(harness.discovery_reserve(plan, gov), 0.10)
        self.assertAlmostEqual(self.budget(plan, gov), 0.30)
        self.assertEqual(harness.LAST_REPORT["yield_plan"]["regime"], "legacy")

    def test_retry_with_unsettled_prior_spend_keeps_the_legacy_plan(self):
        plan, gov, tools, _reads = self.start(DEEP, snapshot(4_000_000, spent=650_000, inflight=2, unresolved_calls=2))
        self.assertFalse(harness.LAST_REPORT["prior_settled"])
        self.assertEqual((plan["regime"], plan["draft_limit"], tools.call_budget), ("legacy", 8, 360))
        self.assertAlmostEqual(gov.mode_cap, 1.15)
        self.assertAlmostEqual(gov.committed(), 0.69)
        self.assertAlmostEqual(self.budget(plan, gov), 0.36)

    def test_settled_retry_keeps_the_promoted_discovery_budget(self):
        for spent, reserve, budget in ((2_300_000, 0.10, 0.20), (1_000_000, 0.40, 1.20), (0, 0.40, 2.20)):
            with self.subTest(spent=spent):
                harness.LAST_REPORT.clear()
                plan, gov, tools, _reads = self.start(DEEP, snapshot(4_000_000, spent=spent))
                self.assertEqual((plan["regime"], plan["draft_limit"], tools.call_budget), ("flat", 14, 800))
                self.assertAlmostEqual(gov.mode_cap, 2.6)
                self.assertAlmostEqual(harness.discovery_reserve(plan, gov), reserve)
                self.assertAlmostEqual(self.budget(plan, gov), budget)

    def test_discovery_window(self):
        deep = yieldplan.for_allowance(yieldplan.plan_for(DEEP), 4.0)
        lean = dict(yieldplan.legacy_plan(deep), regime="legacy")
        light = yieldplan.for_allowance(yieldplan.plan_for(LIGHT), 4.0)
        for wall, deep_end, other_end in ((3600.0, 1800.0, 1440.0), (7200.0, 1800.0, 1500.0),
                                          (2000.0, 1000.0, 800.0), (600.0, 240.0, 240.0)):
            clock = governor.Clock(wall=wall, t0=100.0)
            with self.subTest(wall=wall):
                self.assertAlmostEqual(clock.discovery_end, 100.0 + other_end)
                self.assertAlmostEqual(harness.discovery_end(deep, clock), 100.0 + deep_end)
                for plan in (lean, light, yieldplan.legacy_plan(deep)):
                    self.assertAlmostEqual(harness.discovery_end(plan, clock), 100.0 + other_end)
                if wall >= 3600.0:
                    self.assertGreaterEqual(clock.paid_cutoff - harness.discovery_end(deep, clock), 1380.0 - 1e-6)

    def test_extraction_reads_the_row_limit_at_call_time(self):
        rows = [{"id": i, "title": f"Result {i}", "url": f"https://site{i}.example/a", "date": None, "excerpt": "x"}
                for i in range(240)]
        for limit in (120, 204):
            with mock.patch.object(scout, "EXTRACT_MAX_ROWS", limit):
                prompts = scout.extraction_prompts(rows, DEEP)
            self.assertEqual(sum(len(re.findall(r'"id": \d+', p)) for p in prompts), limit)

    def test_tier_cap_lowers_mode_cap_under_flat_and_legacy_allowances(self):
        for allowance, tier, mode_cap in ((4_000_000, "deep", 2.6), (4_000_000, "light", 0.9),
                                          (4_000_000, "standard", 1.6), (800_000, "deep", 0.74),
                                          (800_000, "light", 0.74)):
            with self.subTest(allowance=allowance, tier=tier):
                tools = SimpleNamespace(adopt_quota=mock.Mock())
                with mock.patch.object(governor, "read_snapshot", return_value=snapshot(allowance)):
                    gov = harness._start_governor(None, tools, tier_cap_usd=yieldplan.TIERS[tier]["cap_usd"])
                    self.assertAlmostEqual(gov.mode_cap, mode_cap)
                    gov.adopt_quota(snapshot(allowance))
                    self.assertAlmostEqual(gov.mode_cap, mode_cap)
                    self.assertLessEqual(harness.scout_budget(gov), mode_cap - harness.POST_SCOUT_RESERVE_USD + 1e-9)

    def test_lean_start_keeps_the_lower_cap(self):
        tools = SimpleNamespace(adopt_quota=mock.Mock())
        with mock.patch.object(governor, "read_snapshot", return_value=None):
            gov = harness._start_governor(None, tools, tier_cap_usd=yieldplan.TIERS["deep"]["cap_usd"])
        self.assertAlmostEqual(gov.mode_cap, min(harness.LEAN_CAP_USD, gov.cap))


if __name__ == "__main__":
    unittest.main()
