"""Offline checks for the evidence order, the bare-row rule, the bonus source rule, the own-site round search and the
re-check refill; no paid calls."""

import os
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import harness

admission = harness.admission
bonus = harness.bonus
scout = harness.scout_module

DEEP = {"intent_category": "PRODUCT_LAUNCH", "company_stage": "Series A", "industry": "Health Care",
        "max_companies": 5}
LIGHT = {"intent_category": "HIRING", "company_stage": "Series A", "industry": "Software", "max_companies": 5}
FLAGS = {"fit_tier": "A", "stage_proven": True, "anchored": True, "hq_established": True, "size_source": "linkedin"}


def row(name, label, *, evidenced=True, staged=True):
    """An evidenced row cites its own site and carries a LinkedIn page and a required-attribute claim; the other
    cites a news page and carries neither.  ``staged`` rows carry stage evidence."""

    site = f"https://{label}.example/"
    primary = f"https://{label}.example/news/launch" if evidenced else f"https://dailywire-{label}.example/story"
    out = {"company_name": name, "company_website": site,
           "company_linkedin": f"https://www.linkedin.com/company/{label}" if evidenced else "",
           "intent_signals": [{"matched_icp_signal": 0, "url": primary, "date": "2026-09-01",
                               "description": f"{name} launched a new care platform."}],
           "company_stage_evidence": [], "required_attribute": None,
           "_flags": dict(FLAGS, stage_proven=staged, stage_evidence=1 if staged else 0)}
    if staged:
        out["company_stage_evidence"] = [{"url": primary, "quote": f"{name} raised a Series A round."}]
    if evidenced:
        out["required_attribute"] = {"passed": True, "evidence_url": site, "evidence_quote": f"{name} runs clinics."}
    return out


def names(rows):
    return [r["company_name"] for r in rows]


HIGH = [row(f"Alpha Clinics {n}", f"alpha{n}-clinics") for n in "ABCDE"]
LOW = [row(f"Beta Care {n}", f"beta{n}-care", evidenced=False) for n in "XYZ"]


class EvidenceRankTest(unittest.TestCase):
    def test_evidence_source_classes(self):
        site = "https://www.alpha-clinics.example/"
        cases = {
            "https://www.linkedin.com/posts/someone_update-activity-1-x": "linkedin",
            "https://www.prnewswire.com/news-releases/launch-302.html": "wire",
            "https://www.accessnewswire.com/newsroom/en/launch": "wire",
            "https://boards.greenhouse.io/alphaclinics/jobs/1": "job",
            "https://alpha-clinics.example/careers/nurse": "job",
            "https://alpha-clinics.example/news/launch": "own",
            "https://investors.alpha-clinics.example/news/launch": "own",
            "https://ir.other-group.example/news/launch": "listing",
            "https://www.sec.gov/Archives/edgar/data/1/x.htm": "listing",
            "https://www.stocktitan.net/news/ABC/launch.html": "listing",
            "https://dailynews.example/2026/01/01/launch/": "news",
            "": "none",
        }
        for url, kind in cases.items():
            with self.subTest(url=url):
                self.assertEqual(admission._evidence_source(url, site), kind)

    def test_evidence_score_sums_the_feature_weights(self):
        self.assertEqual(admission.EVIDENCE_WEIGHTS, {"ra_claim": 1.70, "primary_source": 0.0,
                                                      "primary_not_linkedin": 0.5, "two_signals": 0.88,
                                                      "linkedin": 0.67, "stage_own_site": 0.28})
        self.assertTrue(admission.evidence_features(HIGH[0])["primary_source"])
        self.assertAlmostEqual(admission.evidence_score(HIGH[0]), 1.70 + 0.5 + 0.67 + 0.28)
        self.assertAlmostEqual(admission.evidence_score(LOW[0]), 0.5)
        two = dict(LOW[0], intent_signals=LOW[0]["intent_signals"] + [
            {"matched_icp_signal": 1, "url": "https://www.linkedin.com/posts/x_y-activity-1-z"}])
        self.assertAlmostEqual(admission.evidence_score(two), 0.5 + 0.88)
        post = dict(LOW[0], intent_signals=[{"matched_icp_signal": 0, "url": "https://www.linkedin.com/posts/x_y-1"}])
        self.assertAlmostEqual(admission.evidence_score(post), 0.0)
        self.assertEqual(admission.explain(HIGH[0], DEEP)["evidence"], 3.15)

    def test_a_row_without_the_change_clause_follows_the_others_in_evidence_order(self):
        def graded(base, grade):
            return dict(base, _flags=dict(base["_flags"], change_grade=grade))

        none_high = graded(HIGH[0], "none")
        strong_low = graded(LOW[0], "strong")
        weak_low = graded(LOW[1], "weak")
        ungraded_low = LOW[2]
        none_low = graded(row("Delta Care N", "deltan-care", evidenced=False), "none")
        pool = [none_low, none_high, strong_low, weak_low, ungraded_low]
        self.assertGreater(admission.evidence_score(none_high), admission.evidence_score(strong_low))
        with mock.patch.object(admission, "EVIDENCE_RANK", True):
            ordered = names(sorted(pool, key=lambda r: admission.rank_key(r, DEEP)))
            bonus_first = names(harness._bonus_order(pool, DEEP))
        self.assertEqual(set(ordered[:3]), {strong_low["company_name"], weak_low["company_name"],
                                            ungraded_low["company_name"]})
        self.assertEqual(ordered[3:], [none_high["company_name"], none_low["company_name"]])
        self.assertEqual(bonus_first, ordered)
        with mock.patch.object(admission, "EVIDENCE_RANK", False):
            keys = [admission.rank_key(r, DEEP) for r in pool]
        self.assertEqual({k[5] for k in keys}, {False})
        self.assertEqual({k[6] for k in keys}, {0.0})

    def test_deep_pool_of_eight_admits_the_five_high_evidence_rows(self):
        pool = LOW + HIGH
        with mock.patch.object(admission, "EVIDENCE_RANK", True):
            chosen, notes = admission.select(pool, DEEP, limit=5)
            bonus_first = names(harness._bonus_order(pool, DEEP)[:5])
        self.assertEqual(names(chosen), names(HIGH))
        self.assertEqual(bonus_first, names(HIGH))
        self.assertEqual(notes["cap"], 5)
        with mock.patch.object(admission, "EVIDENCE_RANK", False):
            chosen, _notes = admission.select(pool, DEEP, limit=5)
            bonus_first = names(harness._bonus_order(pool, DEEP)[:3])
        self.assertEqual(names(chosen)[:3], names(LOW))
        self.assertEqual(bonus_first, names(LOW))

    def test_light_plan_keeps_the_expected_value_order(self):
        modules = harness._yield_modules()
        self.addCleanup(harness.yieldplan.apply, harness.yieldplan.plan_for({}), modules)
        harness.yieldplan.apply(harness.yieldplan.plan_for(LIGHT), modules)
        self.assertFalse(admission.EVIDENCE_RANK)
        pool = HIGH + LOW
        by_value = sorted(pool, key=lambda r: (-admission.estimates(r, LIGHT)["q"] * admission.value(r, LIGHT),
                                               -admission.estimates(r, LIGHT)["q"]))
        self.assertEqual(names(sorted(pool, key=lambda r: admission.rank_key(r, LIGHT))), names(by_value))
        chosen, _notes = admission.select(pool, LIGHT, limit=5)
        self.assertEqual(names(chosen)[:3], names(LOW))


class BareRowTest(unittest.TestCase):
    def test_rows_without_stage_evidence_fill_no_slot_unless_switched_on(self):
        bare = [row(f"Gamma Health {n}", f"gamma{n}-health", staged=False) for n in "PQ"]
        pool = HIGH[:3] + bare
        with mock.patch.object(admission, "EVIDENCE_RANK", True):
            chosen, notes = admission.select(pool, DEEP, limit=5)
            self.assertEqual(names(chosen), names(HIGH[:3]))
            self.assertEqual(notes["no_stage_evidence"], names(bare))
            with mock.patch.object(admission, "BARE_ROW_FILL", True):
                chosen, notes = admission.select(pool, DEEP, limit=5)
            self.assertEqual(names(chosen), names(HIGH[:3] + bare))
            self.assertEqual(notes["filled_without_stage_evidence"], names(bare))
            chosen, _notes = admission.select(bare, DEEP, limit=5)
        self.assertEqual(len(chosen), 1)
        self.assertFalse(admission.BARE_ROW_FILL)


class BonusSourceTest(unittest.TestCase):
    PAGE = "https://www.linkedin.com/company/alpha-clinics"

    def test_linkedin_post_rule(self):
        for url, page, skipped in (
                ("https://www.linkedin.com/posts/jane-doe-12345_great-news-activity-1-abcd", self.PAGE, True),
                ("https://www.linkedin.com/pulse/alpha-clinics-opens-jane-doe", self.PAGE, True),
                ("https://www.linkedin.com/feed/update/urn:li:activity:1/", self.PAGE, True),
                ("https://uk.linkedin.com/posts/other-group_launch-activity-1-x", self.PAGE, True),
                ("https://www.linkedin.com/posts/alpha-clinics-fan_launch-activity-1-x", self.PAGE, True),
                ("https://www.linkedin.com/posts/alpha-clinics_launch-activity-1-x", "", True),
                ("https://www.linkedin.com/posts/alpha-clinics_launch-activity-1-x", self.PAGE, False),
                ("https://www.linkedin.com/posts/alpha_clinics_new-site-activity-1-x", self.PAGE, False),
                ("https://www.linkedin.com/company/alpha-clinics/", self.PAGE, False),
                ("https://alpha-clinics.example/posts/launch", self.PAGE, False)):
            with self.subTest(url=url, page=page):
                self.assertEqual(bonus.linkedin_post(url, page), skipped)

    PRIMARY = "https://alpha-clinics.example/news/launch"
    PERSON_POST = "https://www.linkedin.com/posts/jane-doe-12345_partner-news-activity-1-x"
    OTHER_POST = "https://www.linkedin.com/posts/john-roe-678_alpha-clinics-partner-activity-2-y"
    OWN_POST = "https://www.linkedin.com/posts/alpha-clinics_partner-news-activity-3-z"
    RELEASE = "https://alpha-clinics.example/news/partnership"
    SENTENCE = ("Alpha Clinics announced a partnership with a regional hospital network to open new care sites "
                "across the state.")
    PARTNER = "Announced a partnership in the last 12 months, per a press release."

    def icp(self):
        return dict(DEEP, intent_signals=["Launched a new care service in the last 12 months, per a press release.",
                                          self.PARTNER],
                    bonus_intents=[{"intent_category": "PARTNERSHIP", "intent_signal": self.PARTNER}])

    @staticmethod
    def story(url, date):
        return {"url": url, "title": "Alpha Clinics announces a hospital partnership", "excerpt": "partnership",
                "published_date": date}

    def run_attach(self, stories, searched=()):
        """bonus.attach on one company whose free news feed lists ``stories``; a paid search answers ``searched``."""

        page = lambda text: SimpleNamespace(ok=True, text=text, published="")  # noqa: E731
        fact = f"October 1, 2026. {self.SENTENCE} The sites open next year."
        tools = SimpleNamespace(
            pages={self.PRIMARY: page("Alpha Clinics launched a new care platform for its clinics across the region."),
                   **{url: page(fact) for url in (self.PERSON_POST, self.OTHER_POST, self.OWN_POST, self.RELEASE)}},
            search_web=mock.Mock(return_value={"results": list(searched)}),
            _roster_news={"alpha-clinics.example": list(stories)})
        company = {"company_name": "Alpha Clinics", "company_website": "https://alpha-clinics.example/",
                   "company_linkedin": self.PAGE,
                   "intent_signals": [{"matched_icp_signal": 0, "url": self.PRIMARY, "date": "2026-09-01",
                                       "description": "Alpha Clinics launched a new care platform"}]}
        verify = mock.Mock(side_effect=lambda f, **_kw: dict(f, _quote=f["description"]))
        confirm = mock.Mock(return_value=True)
        with mock.patch.object(bonus, "confirm", confirm), \
                mock.patch.dict(os.environ, {"LAB_ARENA_EVALUATION_DATE": "2026-10-20"}):
            notes = bonus.attach([company], SimpleNamespace(evidence={}), self.icp(), tools, verify_signal=verify,
                                 deadline=time.monotonic() + 60.0, clock=time.monotonic)
        bonus_urls = [s["url"] for s in company["intent_signals"] if s["matched_icp_signal"] == 1]
        return notes, bonus_urls, confirm, tools.search_web

    def test_a_post_page_is_passed_over_before_it_is_read_and_the_next_page_proves_the_fact(self):
        notes, urls, confirm, search = self.run_attach([self.story(self.PERSON_POST, "2026-10-02"),
                                                        self.story(self.RELEASE, "2026-10-01")])
        self.assertEqual(urls, [self.RELEASE])
        self.assertEqual(notes["linkedin_post_skips"], 1)
        self.assertEqual(confirm.call_count, 1)
        self.assertFalse(search.called)

    def test_the_company_pages_own_post_is_used(self):
        notes, urls, _confirm, _search = self.run_attach([self.story(self.OWN_POST, "2026-10-02"),
                                                          self.story(self.RELEASE, "2026-10-01")])
        self.assertEqual(urls, [self.OWN_POST])
        self.assertNotIn("linkedin_post_skips", notes)

    def test_the_paid_search_still_runs_when_the_free_feed_holds_only_a_post(self):
        found = [{"url": self.OTHER_POST, "title": "Alpha Clinics partner news", "excerpt": self.SENTENCE,
                  "published_date": "2026-10-03"},
                 {"url": self.RELEASE, "title": "Alpha Clinics announces partnership", "excerpt": self.SENTENCE,
                  "published_date": "2026-10-01"}]
        notes, urls, confirm, search = self.run_attach([self.story(self.PERSON_POST, "2026-10-02")], searched=found)
        self.assertEqual(urls, [self.RELEASE])
        self.assertEqual(search.call_count, 1)
        self.assertEqual(notes["paid_searches"], 1)
        self.assertEqual(notes["linkedin_post_skips"], 2)
        self.assertEqual(confirm.call_count, 1)

    def test_attach_guard_skips_a_post_fact_and_counts_it(self):
        found = {"matched_icp_signal": 1, "url": self.PERSON_POST, "date": "2026-10-01", "description": self.SENTENCE,
                 "snippet": self.SENTENCE}
        verify = mock.Mock()
        company = {"company_name": "Alpha Clinics", "company_website": "https://alpha-clinics.example/",
                   "company_linkedin": self.PAGE,
                   "intent_signals": [{"matched_icp_signal": 0, "url": self.PRIMARY, "date": "2026-09-01",
                                       "description": "Alpha Clinics launched a new care platform"}]}
        with mock.patch.object(bonus, "_news_candidate", return_value=found):
            notes = bonus.attach([company], SimpleNamespace(evidence={}), self.icp(), SimpleNamespace(pages={}),
                                 verify_signal=verify, deadline=time.monotonic() + 60.0, clock=time.monotonic)
        self.assertEqual(len(company["intent_signals"]), 1)
        self.assertFalse(verify.called)
        self.assertEqual(notes["linkedin_post_skips"], 1)


class OwnRoundSearchTest(unittest.TestCase):
    DOMAIN = "alpha-clinics.example"
    OWN = "https://alpha-clinics.example/news/alpha-clinics-raises-series-a"

    def setUp(self):
        for state in (scout.LAST, scout.PROOF_DATE, scout.STAGE_CONFLICT, scout.STAGE_EXTRA):
            patcher = mock.patch.dict(state)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []

    def search_web(self, query, *, limit=5, include_domains=None, **_kw):
        self.calls.append((query, include_domains))
        if not include_domains:
            return {"results": []}
        return {"results": [
            {"url": "https://other-news.example/story", "published_date": "2026-09-02",
             "excerpt": "Alpha Clinics today announced it has raised $40 million in Series B funding."},
            {"url": self.OWN, "published_date": "2026-09-01",
             "excerpt": "Alpha Clinics today announced it has raised $20 million in Series A funding to open clinics."}]}

    def proof(self, icp, enabled):
        tools = SimpleNamespace(search_web=self.search_web, pages={})
        with mock.patch.object(scout, "OWN_ROUND_SEARCH", enabled):
            return scout._search_stage_proof(tools, "Alpha Clinics", {"domain": self.DOMAIN}, icp,
                                             deadline=time.monotonic() + 60.0)

    def test_own_site_search_proves_the_round_of_a_stage_less_venture_draft(self):
        self.assertEqual(self.proof(DEEP, False), ("", "", ""))
        self.assertFalse(any(domains for _query, domains in self.calls))
        stage, url, quote = self.proof(DEEP, True)
        self.assertEqual((stage, url), ("Series A", self.OWN))
        self.assertIn("Series A", quote)
        self.assertIn(('"Alpha Clinics" Series A', [self.DOMAIN]), self.calls)
        self.assertEqual(scout.LAST.get("own_round_searches"), 1)
        self.assertEqual(scout.PROOF_DATE.get(harness.sm.company_name_key("Alpha Clinics")), "2026-09-01")

    def test_no_own_site_search_off_venture_stages_or_without_a_domain(self):
        self.proof(dict(DEEP, company_stage="Public"), True)
        tools = SimpleNamespace(search_web=self.search_web, pages={})
        with mock.patch.object(scout, "OWN_ROUND_SEARCH", True):
            scout._search_stage_proof(tools, "Alpha Clinics", {"domain": ""}, DEEP, deadline=time.monotonic() + 60.0)
        self.assertFalse(any(domains for _query, domains in self.calls))


class RefillTest(unittest.TestCase):
    """The re-check step: contested rows are held back beside other rows and their slots are filled once."""

    POOL = [row(f"Omega Clinics {n}", f"omega{n}-clinics") for n in "ABCDEFG"]

    def setUp(self):
        self.pool = [dict(r, _flags=dict(r["_flags"]), intent_signals=list(r["intent_signals"])) for r in self.POOL]
        self.paragraphs = []
        self.checked = []

    def recheck(self, contest=(), drop=(), move=None):
        """A stand-in for harness._reverify: drops the rows named in ``drop``, contests those in ``contest``, and
        moves the website of the row named in ``move`` (name, new website)."""

        def fake(rows, icp):
            out = []
            for r in rows:
                self.checked.append(r["company_name"])
                if r["company_name"] in drop:
                    continue
                if move and r["company_name"] == move[0]:
                    r["company_website"] = move[1]
                if r["company_name"] in contest:
                    r.setdefault("_flags", {})["rank_last"] = True
                    r["_flags"]["recheck_mismatch"] = True
                out.append(r)
            return out, {"companies": [{"company": r["company_name"], "overall": "match"} for r in rows]}
        return fake

    def step(self, fake, *, seconds_left=900.0):
        def write(rows, icp, **_kw):
            self.paragraphs.extend(r["company_name"] for r in rows)
            return {"accepted": len(rows), "fallback": 0}

        chosen, _notes = admission.select(self.pool, DEEP, limit=5)
        clock = SimpleNamespace(paid_cutoff=time.monotonic() + seconds_left)
        with mock.patch.object(harness, "_reverify", side_effect=fake), \
                mock.patch.object(harness.details_module, "write_paragraphs", side_effect=write):
            final, notes = harness._reverify_step(chosen, self.pool, SimpleNamespace(evidence={}), DEEP, clock, 5)
        return names(chosen), names(final), notes

    def test_withheld_slots_are_filled_from_rows_no_recheck_has_seen(self):
        chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics B", "Omega Clinics C"}))
        self.assertEqual(chosen, names(self.POOL[:5]))
        self.assertEqual(notes["withheld_contested"], ["Omega Clinics B", "Omega Clinics C"])
        self.assertEqual(final, ["Omega Clinics A", "Omega Clinics D", "Omega Clinics E", "Omega Clinics F",
                                 "Omega Clinics G"])
        self.assertEqual(notes["refill"]["added"], ["Omega Clinics F", "Omega Clinics G"])
        self.assertEqual(self.paragraphs, ["Omega Clinics F", "Omega Clinics G"])
        self.assertEqual(self.checked, chosen + ["Omega Clinics F", "Omega Clinics G"])

    def test_a_new_pick_the_recheck_contests_stays_out(self):
        _chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics B", "Omega Clinics C",
                                                                "Omega Clinics F"}))
        self.assertEqual(final, ["Omega Clinics A", "Omega Clinics D", "Omega Clinics E", "Omega Clinics G"])
        self.assertEqual(notes["refill"]["left_out"], ["Omega Clinics F"])

    def test_no_refill_without_time(self):
        _chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics B"}), seconds_left=60.0)
        self.assertEqual(final, ["Omega Clinics A", "Omega Clinics C", "Omega Clinics D", "Omega Clinics E"])
        self.assertNotIn("refill", notes)
        self.assertEqual(self.paragraphs, [])

    def test_a_dropped_row_still_takes_the_replacement_path(self):
        _chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics B"}, drop={"Omega Clinics A"}))
        self.assertEqual(final, ["Omega Clinics C", "Omega Clinics D", "Omega Clinics E", "Omega Clinics F",
                                 "Omega Clinics G"])
        self.assertIn("replacement", notes)
        self.assertNotIn("withheld_contested", notes)
        self.assertNotIn("refill", notes)

    def test_a_row_contested_in_the_replacement_pass_is_not_checked_again(self):
        self.pool = self.pool[:6]
        _chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics F"}, drop={"Omega Clinics A"}))
        self.assertEqual(notes["replacement"]["new"], ["Omega Clinics F"])
        self.assertEqual(notes["withheld_contested"], ["Omega Clinics F"])
        self.assertEqual(final, ["Omega Clinics B", "Omega Clinics C", "Omega Clinics D", "Omega Clinics E"])
        self.assertEqual(notes["refill"]["new"], [])
        self.assertEqual(self.checked.count("Omega Clinics F"), 1)

    def test_a_company_whose_website_moved_is_not_picked_twice(self):
        twin = row("Omega Clinics A2", "omegaA-new")
        self.pool.insert(5, twin)
        moved = ("Omega Clinics A", "https://www.omegaA-new.example/")
        _chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics B"}, move=moved))
        self.assertEqual(final, ["Omega Clinics A", "Omega Clinics C", "Omega Clinics D", "Omega Clinics E",
                                 "Omega Clinics F"])
        self.assertEqual(notes["refill"]["new"], ["Omega Clinics F"])

    def test_a_new_pick_moved_onto_a_kept_company_site_stays_out(self):
        moved = ("Omega Clinics F", "https://www.omegaA-clinics.example/")
        _chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics B"}, move=moved))
        self.assertEqual(notes["refill"]["new"], ["Omega Clinics F"])
        self.assertEqual(notes["refill"]["left_out"], ["Omega Clinics F"])
        self.assertEqual(final, ["Omega Clinics A", "Omega Clinics C", "Omega Clinics D", "Omega Clinics E"])

    def test_all_contested_rows_stay_when_nothing_else_is_left(self):
        self.pool = self.pool[:2]
        _chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics A", "Omega Clinics B"}))
        self.assertEqual(final, ["Omega Clinics A", "Omega Clinics B"])
        self.assertNotIn("withheld_contested", notes)

    def test_a_refill_error_keeps_the_rows_held_back_out(self):
        with mock.patch.object(harness, "_refill", side_effect=RuntimeError("boom")):
            _chosen, final, notes = self.step(self.recheck(contest={"Omega Clinics B"}))
        self.assertEqual(final, ["Omega Clinics A", "Omega Clinics C", "Omega Clinics D", "Omega Clinics E"])
        self.assertIn("RuntimeError", notes["refill"]["error"])


if __name__ == "__main__":
    unittest.main()
