#!/usr/bin/env python3
"""
Offline end-to-end harness test for the SN71 Arena source bundle. Costs $0.

It does NOT call run_icp directly. It runs the repository's own
`lab_arena/agent_entrypoint.py`, which is the code the Arena host actually
executes: it loads harness.py from a source directory, enforces the signature
contract, calls run_icp(icp), and writes /output/companies.json. Testing through
the host entrypoint is the only way to catch contract breaks — an earlier
version returned an exit code instead of a list and passed a direct call while
failing the real host.

A fake broker speaks the real Unix-socket frame protocol, and the result is
validated with the repository's own CompanyOutput model.

    cd ~/sn71/leadpoet
    .venv/bin/python /mnt/g/bittensor/sn71/arena-agent/local_test.py
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import hashlib
import json
import re
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REPO = HERE.parent / "data" / "validator"
if not DEFAULT_REPO.is_dir():
    DEFAULT_REPO = HERE.parent / "leadpoet"
REPO = Path(os.environ.get("LEADPOET_REPO", str(DEFAULT_REPO)))
# LT_SCRAPINGDOG=1 runs the fixture as a submission that carried a Scrapingdog
# credential: the sandbox would then export the brokered handle and the host
# would answer those calls.
SCRAPINGDOG_READY = bool(os.environ.get("LT_SCRAPINGDOG"))
OUTPUT_SCHEMA = "leadpoet.lab_arena.output.v5"

FRAME_SCHEMA = "leadpoet.lab_arena.operation_frame.v1"

# The host passes ONLY the icp dict to run_icp, so this is what the harness sees.
# The Arena ICP shape, per tests/lab_arena/icp_fixtures.py and
# _normalized_icp (competition.py:139-140): country + geography. This fixture
# used to say "company_country", which is a FULFILLMENT field -- icp_checks.py
# reads it as a list for the Tier 1a gate, and nothing on the Arena path looks
# at it at all. With it, the harness filtered on a country the scorer would
# never have compared against, and the scorer's own requirement would have
# defaulted to "United States".
SAMPLE_ICP = {
    "icp_id": "offline-v5-contact-test",
    "output_schema_version": OUTPUT_SCHEMA,
    "integrity_policy": "arena_integrity_v1",
    "intent_details_policy": "intent_details_v1",
    "contact_policy": "contacts_v1",
    "intent_category": "HIRING",
    "target_roles": ["Vice President of Sales"],
    "contact_geography": {"countries": ["United Kingdom"]},
    "industry": "Software",
    "sub_industry": "Applied AI",
    "country": "United Kingdom",
    "geography": "United Kingdom",
    "employee_count": "51-200",
    "company_stage": "Series B",
    "intent_signals": ["hiring", "expansion"],
    "excluded_companies": ["Excluded Ltd"],
    "verified_example_company": "Example AI Ltd",
    "max_companies": 5,
}

DISCOVERY_RESULTS = {
    "results": [
        {"url": "https://synthetiq.ai/about", "title": "Synthetiq - Applied AI for logistics",
         "text": "Synthetiq builds route optimisation models.",
         "publishedDate": "2026-08-21T09:00:00.000Z"},
        {"url": "https://www.northwindml.co.uk/", "title": "Northwind ML | ML consultancy",
         "text": "Northwind ML is a London machine learning consultancy.",
         "publishedDate": "2026-07-30"},
        {"url": "https://en.wikipedia.org/wiki/Artificial_intelligence",
         "title": "Artificial intelligence"},                       # blocked host
        {"url": "https://synthetiq.ai/careers", "title": "Careers at Synthetiq"},  # dup root
        {"url": "https://veritascore.io", "title": "",
         "text": "Credit risk models for lenders."},                # no title
        {"url": "https://lumenpath.dev/", "title": "Lumenpath - developer tooling",
         "text": "Lumenpath builds developer tooling.",
         "publishedDate": "not-a-date"},                            # bad date
        # Four more candidates so the funnel can actually be measured. With only
        # three survivors the harness could never reach the five-company goal,
        # and the per-ICP score divides by that goal whatever we submit -- so a
        # test that cannot distinguish "found three" from "found five" cannot
        # see the largest loss there is.
        {"url": "https://cobaltgrid.co.uk/", "title": "Cobaltgrid - applied AI for utilities",
         "text": "Cobaltgrid builds forecasting models for utilities."},
        {"url": "https://halcyonlabs.co.uk/about", "title": "Halcyon Labs - AI research",
         "text": "Halcyon Labs is an applied AI studio."},
        {"url": "https://driftmark.io/", "title": "Driftmark - AI logistics",
         "text": "Driftmark builds fleet routing software."},
        {"url": "https://tessellate.co.uk/", "title": "Tessellate - ML platform",
         "text": "Tessellate builds an ML feature platform."},
    ]
}

HIRING_RESULTS = {
    "Synthetiq": {"results": [
        {"url": "https://boards.greenhouse.io/synthetiq/jobs/4411",
         "title": "VP of Sales at Synthetiq",
         "text": "Synthetiq is hiring a VP of Sales to build out our commercial team.",
         "publishedDate": "2026-08-28"},
    ]},
    "Northwind ML": {"results": [
        {"url": "https://www.linkedin.com/jobs/view/993311",
         "title": "Northwind ML is hiring a Head of Growth",
         "text": "We are looking for a Head of Growth to join our team in London.",
         "publishedDate": "2026-08-15"},
    ]},
    "Veritascore": {"results": [
        {"url": "https://news.example.com/veritascore", "title": "Veritascore in the news",
         "text": "coverage"},
    ]},
    "Lumenpath": {"results": [
        {"url": "https://lumenpath.dev/careers", "title": "Careers at Lumenpath",
         "text": ""},                                               # -> scrape fallback
    ]},
    # Exa returns publishedDate on real job pages; these three mirror that.
    "Cobaltgrid": {"results": [
        {"url": "https://boards.greenhouse.io/cobaltgrid/jobs/7781",
         "title": "Lead ML Engineer at Cobaltgrid",
         "text": "Cobaltgrid is hiring a Lead ML Engineer in Leeds.",
         "publishedDate": "2026-08-30"},
    ]},
    # Halcyon Labs passes every gate and returns NOTHING here, so _emit drops it
    # and the spare margin has to cover the slot.
    "Halcyon Labs": {"results": []},
    "Driftmark": {"results": [
        {"url": "https://boards.greenhouse.io/driftmark/jobs/3312",
         "title": "Senior Backend Engineer at Driftmark",
         "text": "Driftmark is hiring a Senior Backend Engineer in Cambridge.",
         "publishedDate": "2026-09-01"},
    ]},
}

# Synthetiq gets a SECOND signal so the multi-signal path runs: the intent cap
# rises from 60 (one signal) to 80 (two). The .icu row must be dropped as a
# fabricated-source TLD; the unknown-host blog must be dropped as "other".
LEADERSHIP_RESULTS = {
    "Synthetiq": {"results": [
        {"url": "https://techcrunch.com/2026/08/25/synthetiq-expands-london",
         "title": "Synthetiq expands London team after Series B",
         "text": "Synthetiq is expanding its London engineering team and hiring "
                 "across commercial roles following its Series B.",
         "publishedDate": "2026-08-25"},
        {"url": "https://newswire.icu/article/1756100000",
         "title": "Synthetiq hiring spree", "text": "Synthetiq is hiring aggressively.",
         "publishedDate": "2026-09-01"},
        {"url": "https://randomblogsite.example/p/synthetiq",
         "title": "Notes on Synthetiq", "text": "Synthetiq is hiring.",
         "publishedDate": "2026-08-30"},
    ]},
    "Veritascore": {"results": [
        {"url": "https://www.businesswire.com/news/veritascore-cfo",
         "title": "Veritascore appoints new Chief Financial Officer",
         "text": "Veritascore today appoints Dana Reyes as Chief Financial Officer.",
         "publishedDate": "2026-08-19"},
    ]},
}

CONTENTS_RESULTS = {
    "results": [
        {"url": "https://synthetiq.ai/about",
         "text": "Synthetiq is a London-based applied AI company of about 120 people "
                 "building route optimisation models for logistics operators. "
                 "In 2025 Synthetiq raised $32 million in Series B funding."},
        {"url": "https://www.northwindml.co.uk/",
         "text": "Northwind ML is a machine learning consultancy headquartered in "
                 "Manchester, United Kingdom, with around 80 staff. "
                 "Northwind ML closed a $20 million Series B round last year."},
        {"url": "https://veritascore.io",
         "text": "Veritascore is a New York headquartered credit risk analytics firm "
                 "employing over 6,000 people across the United States."},
        {"url": "https://lumenpath.dev/",
         "text": "Lumenpath builds developer tooling from our Manchester office. "
                 "We are a team of around 90 engineers. "
                 "Lumenpath has raised $25 million in Series B funding."},
        {"url": "https://boards.greenhouse.io/synthetiq/jobs/4411",
         "text": "About Synthetiq. Synthetiq is hiring a VP of Sales to lead our "
                 "commercial team in London. You will own the full sales cycle. "
                 "Apply for this job."},
        {"url": "https://techcrunch.com/2026/08/25/synthetiq-expands-london",
         "text": "Synthetiq is expanding its London engineering team and hiring "
                 "across commercial roles following its Series B round."},
        {"url": "https://www.linkedin.com/jobs/view/993311",
         "text": "Northwind ML is hiring a Head of Growth to join our London team. "
                 "Apply now to help us scale."},
        # https://lumenpath.dev/careers intentionally absent -> scrape fallback
        # Cobaltgrid: complete and in-band. Should reach the output.
        {"url": "https://cobaltgrid.co.uk/",
         "text": "Cobaltgrid is an applied AI company in Leeds, United Kingdom, "
                 "with about 140 employees building utility demand forecasting. "
                 "Cobaltgrid secured $40 million in Series B funding in 2025."},
        {"url": "https://boards.greenhouse.io/cobaltgrid/jobs/7781",
         "text": "About Cobaltgrid. Cobaltgrid is hiring a Lead ML Engineer in "
                 "Leeds to build our forecasting platform. Apply now."},
        # Halcyon Labs: in-band, but NO evidence page is ever returned for it,
        # and its page states no funding round: with the ICP naming Series B,
        # the stage gate sets it aside before any contact is bought.
        {"url": "https://halcyonlabs.co.uk/about",
         "text": "Halcyon Labs is an applied AI studio in Bristol, United Kingdom, "
                 "with around 60 staff."},
        # Driftmark: in-band with usable evidence -- the spare that should fill
        # the slot Halcyon Labs loses.
        {"url": "https://driftmark.io/",
         "text": "Driftmark is a Cambridge, United Kingdom applied AI company of "
                 "roughly 110 people building fleet routing software. "
                 "Driftmark raised $28 million in Series B funding this year."},
        {"url": "https://boards.greenhouse.io/driftmark/jobs/3312",
         "text": "About Driftmark. Driftmark is hiring a Senior Backend Engineer "
                 "in Cambridge to scale our routing platform. Apply now."},
        # Tessellate: out of the ICP's employee band, so the harness must reject
        # it rather than spend a slot the scorer would silently skip.
        {"url": "https://tessellate.co.uk/",
         "text": "Tessellate is a Glasgow, United Kingdom ML platform company "
                 "with more than 3,000 employees."},
    ]
}

SCRAPE_HTML = (
    "<html><head><title>Lumenpath careers</title>"
    "<style>.x{}</style><script>var a=1;</script></head><body>"
    "<h1>Join us</h1><p>Lumenpath is hiring a Senior Platform Engineer to join "
    "our Manchester team. Apply now to help us scale our developer tooling.</p>"
    "<p>Posted 22 August 2026</p>"
    "<footer>Copyright 2011 Lumenpath</footer></body></html>"
)

GOOGLE_RESULTS = {"organic_results": [
    {"link": "https://quantumleaf.co.uk/", "title": "QuantumLeaf - AI for retail",
     "snippet": "QuantumLeaf helps retailers forecast demand."}]}

# A people query -- one ending in site:linkedin.com/in -- answers with person
# profiles instead, in the shape scrapingdog.google returned on 2026-09-18:
# organic_results of link/title/snippet. Synthetiq's VP Sales is here, so the
# Scrapingdog path can seat a contact without spending a Deepline call; the
# other companies fall through to Harvest exactly as before.
GOOGLE_PEOPLE_RESULTS = {"organic_results": [
    {"rank": 1, "link": "https://www.linkedin.com/in/fixture-synthetiq-vp/",
     "title": "Dana Fixture - VP Sales at Synthetiq - LinkedIn",
     "snippet": "VP Sales at Synthetiq. London, England, United Kingdom.",
     "displayed_link": "linkedin.com"}]}

# scrapingdog.google_news (added to the host in 0796f157). Only Northwind ML
# gets a hit, and only Northwind ML lacks an Exa news result, so this proves the
# fallback fires exactly when it should and adds a second, dated signal.
GOOGLE_NEWS_URL = "https://techcrunch.com/2026/08/28/northwind-ml-edinburgh/"
GOOGLE_NEWS_RESULTS = {"news_results": [
    {"link": GOOGLE_NEWS_URL,
     "title": "Northwind ML doubles Edinburgh headcount",
     "snippet": "Northwind ML is hiring across engineering after opening a second office.",
     "lastUpdated": "2026-08-28"}]}
CONTENTS_RESULTS["results"].append({
    "url": GOOGLE_NEWS_URL,
    "text": "Northwind ML doubles Edinburgh headcount\n\nNorthwind ML is hiring across "
            "engineering after opening a second office in Edinburgh.\n\n"
            "Published 28 August 2026",
})

# Company 3 contradicts the ICP twice over -- wrong country and, per the
# profiler, a different line of business. Company 5 is in-country and in-band
# but is the industry gate's own case: only in_target_industry rules it out.
# Profiles keyed by the host the harness lists, not by list position. The
# harness numbers companies in whatever order discovery produced them, and a
# positional fixture silently hands one company's profile to another as soon as
# that order changes -- which is exactly what adding Hunter discovery did.
PROFILES_BY_HOST = {
    'synthetiq.ai': {"industry": "Software", "sub_industry": "Applied AI", "country": "United Kingdom", "employee_count": "51-200", "stage": "", "in_target_industry": "yes", "is_real_company": True},
    'northwindml.co.uk': {"industry": "Software", "sub_industry": "Machine Learning", "country": "United Kingdom", "employee_count": "51-200", "stage": "", "in_target_industry": "unclear", "is_real_company": True},
    'veritascore.io': {"industry": "Financial Services", "sub_industry": "Credit Risk", "country": "United States", "employee_count": "5001-10000", "stage": "", "in_target_industry": "no", "is_real_company": True},
    'lumenpath.dev': {"industry": "Software", "sub_industry": "Developer Tools", "country": "United Kingdom", "employee_count": "51-200", "stage": "", "in_target_industry": "yes", "is_real_company": True},
    'cobaltgrid.co.uk': {"industry": "Software", "sub_industry": "Applied AI", "country": "United Kingdom", "employee_count": "51-200", "stage": "", "in_target_industry": "yes", "is_real_company": True},
    'halcyonlabs.co.uk': {"industry": "Software", "sub_industry": "Applied AI", "country": "United Kingdom", "employee_count": "51-200", "stage": "", "in_target_industry": "yes", "is_real_company": True},
    'driftmark.io': {"industry": "Software", "sub_industry": "Applied AI", "country": "United Kingdom", "employee_count": "51-200", "stage": "", "in_target_industry": "yes", "is_real_company": True},
    'tessellate.co.uk': {"industry": "Software", "sub_industry": "ML Platform", "country": "United Kingdom", "employee_count": "1001-5000", "stage": "", "in_target_industry": "yes", "is_real_company": True},
}


def llm_profile_response(prompt: str) -> dict:
    rows = []
    for number, host in re.findall(r"^(\d+)\. .*? \| site: (\S+) \|", prompt, re.M):
        profile = PROFILES_BY_HOST.get(host.lower().removeprefix("www."))
        if profile is not None:
            rows.append(dict(profile, n=int(number)))
    return {"choices": [{"message": {"content": json.dumps({"companies": rows})}}]}


# hunter_discover rows (free discovery). Only two companies, so the paid Exa
# ladder still has to top up and both discovery paths run. Cobaltgrid carries a
# LinkedIn page; Driftmark's location arrives as a dict, as Hunter can send it.
HUNTER_ROWS = [
    {"organization": "Cobaltgrid", "domain": "cobaltgrid.co.uk", "industry": "Software",
     "linkedin_url": "linkedin.com/company/cobaltgrid", "headcount": "51-200",
     "location": {"city": "Leeds", "country": "GB"}},
    {"organization": "Driftmark", "domain": "https://www.driftmark.io", "industry": "Software",
     "linkedin_url": "", "headcount": "51-200", "location": "Cambridge, United Kingdom"},
    {"organization": "Excluded Ltd", "domain": "excluded.example.co.uk"},
    {"organization": "", "domain": "noname.example.com"},
    {"organization": "Cobaltgrid Duplicate", "domain": "cobaltgrid.co.uk"},
]


# free_simple_company_search rows. Driftmark has no LinkedIn page on record, so
# a company_quality_v1 run (LT_QUALITY=1) must drop it and still fill the slots.
COMPANY_RECORDS = [
    {"normalized_domain": "synthetiq.ai", "linkedin_url": "linkedin.com/company/synthetiq",
     "location": "London, England, United Kingdom"},
    {"normalized_domain": "northwindml.co.uk", "linkedin_url": "https://www.linkedin.com/company/northwind-ml/",
     "location": "Manchester, United Kingdom"},
    {"normalized_domain": "lumenpath.dev", "linkedin_url": "linkedin.com/company/lumenpath",
     "location": "Manchester, United Kingdom"},
    {"normalized_domain": "cobaltgrid.co.uk", "linkedin_url": "linkedin.com/company/cobaltgrid",
     "location": "Leeds, United Kingdom"},
    {"normalized_domain": "driftmark.io", "linkedin_url": "",
     "location": "Cambridge, United Kingdom"},
]


class FakeBroker(threading.Thread):
    """Speaks the exact frame protocol the sandbox worker speaks."""

    daemon = True

    def __init__(self, path: str):
        super().__init__()
        self.calls: list = []
        self.discovery_calls = 0
        self.company_search_calls = 0
        self.hunter_calls = 0
        self.hunter_payloads = []
        self.search_calls = 0
        self.contents_calls = 0
        self.scrape_calls = 0
        self.sd_scrape_calls = 0
        self.sd_people_calls = 0
        self.contact_calls = 0
        self.contact_profiles = {}
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.sock.settimeout(0.5)

    def stop(self) -> None:
        self._stop.set()

    def _respond(self, operation_id: str, params: dict):
        if operation_id == "exa.search":
            self.search_calls += 1
            if params.get("category") == "company":
                # The first, narrowest query returns nothing so the test
                # exercises progressive broadening.
                self.discovery_calls += 1
                return 200, ({"results": []} if self.discovery_calls == 1
                             else DISCOVERY_RESULTS)
            query = str(params.get("query") or "")
            table = LEADERSHIP_RESULTS if params.get("category") == "news" else HIRING_RESULTS
            for name, payload in table.items():
                if query.startswith(name):
                    return 200, payload
            return 200, {"results": []}
        if operation_id == "exa.contents":
            self.contents_calls += 1
            wanted = set(params.get("urls") or [])
            return 200, {"results": [r for r in CONTENTS_RESULTS["results"]
                                     if r["url"] in wanted]}
        if operation_id == "deepline.execute":
            tool = params.get("tool")
            if tool == "harvestapi_search_leads":
                self.contact_calls += 1
                payload = params.get("payload") or {}
                company_url = payload.get("currentCompanies", "")
                domain = next((r["normalized_domain"] for r in COMPANY_RECORDS
                               if company_url and r.get("linkedin_url", "").strip("/").split("/")[-1]
                               == company_url.strip("/").split("/")[-1]), "")
                names = {"cobaltgrid.co.uk": "Cobaltgrid", "driftmark.io": "Driftmark",
                         "synthetiq.ai": "Synthetiq", "northwindml.co.uk": "Northwind ML",
                         "lumenpath.dev": "Lumenpath", "halcyonlabs.co.uk": "Halcyon Labs"}
                if not domain:
                    domain = next((d for d, n in names.items()
                                   if n.lower() == payload.get("search", "").lower()), "")
                if not domain:
                    return 200, {"status": "completed", "result": {"elements": []}}
                name = names[domain]
                slug = name.lower().replace(" ", "-")
                url = "https://www.linkedin.com/in/fixture-" + slug + "/"
                profile = {"id": "fixture-" + slug, "linkedinUrl": url,
                           "firstName": "Fixture", "lastName": slug,
                           "workEmail": "fixture@" + domain,
                           "location": {"countryCode": "GB"},
                           "currentPosition": [{"title": "VP Sales", "companyName": name,
                                                "companyDomain": domain, "isCurrent": True}]}
                self.contact_profiles[url] = profile
                return 200, {"status": "completed", "result": {"elements": [profile]}}
            if tool == "harvestapi_get_profile":
                self.contact_calls += 1
                assert params["payload"].get("findEmail") == "true"
                return 200, {"status": "completed", "result": {
                    "profile": self.contact_profiles.get(params["payload"].get("url"), {})}}
            # The miner-funded contract routes page fetches here. Answer with a
            # real Deepline envelope so the harness's unwrapping is exercised.
            if params.get("tool") == "firecrawl_scrape":
                self.scrape_calls += 1
                assert params["payload"]["formats"] == ["rawHtml"], params
                return 200, {"status": "completed", "result": {"data": {
                    "rawHtml": SCRAPE_HTML,
                    "metadata": {"statusCode": 200,
                                 "sourceURL": params["payload"]["url"]}}}}
            if params.get("tool") == "hunter_discover":
                payload = params.get("payload") or {}
                assert isinstance(payload.get("query"), str) and payload["query"].strip(), payload
                assert set(payload.get("headcount") or []) <= {
                    "1-10", "11-50", "51-200", "201-500", "501-1000",
                    "1001-5000", "5001-10000", "10001+"}, payload
                self.hunter_calls += 1
                self.hunter_payloads.append(payload)
                return 200, {"status": "completed", "result": {"data": {"data": HUNTER_ROWS}}}
            if params.get("tool") == "free_simple_company_search":
                sql = str((params.get("payload") or {}).get("sql") or "")
                assert "FROM companies WHERE normalized_domain IN (" in sql, sql
                self.company_search_calls += 1
                wanted = {d.strip(" '") for d in sql.split("IN (", 1)[1].split(")", 1)[0].split(",")}
                matched = [row for row in COMPANY_RECORDS if row["normalized_domain"] in wanted]
                if "GROUP BY normalized_domain" not in sql:
                    return 200, {"result": {"data": {"rows": matched}}}
                # Answer the grouped query the way the database does: one row
                # per domain, COUNT(DISTINCT) and MAX ignoring empty values.
                grouped = []
                for domain in sorted({row["normalized_domain"] for row in matched}):
                    rows = [row for row in matched if row["normalized_domain"] == domain]
                    def distinct(key):
                        return sorted({row[key] for row in rows if row.get(key) not in (None, "")})
                    sizes = [row["employee_count"] for row in rows if row.get("employee_count")]
                    grouped.append({
                        "normalized_domain": domain, "row_count": len(rows),
                        "employee_count_min": min(sizes) if sizes else None,
                        "employee_count_max": max(sizes) if sizes else None,
                        "linkedin_count": len(distinct("linkedin_url")),
                        "linkedin_url": (distinct("linkedin_url") or [None])[-1],
                        "location_count": len(distinct("location")),
                        "location": (distinct("location") or [None])[-1],
                    })
                return 200, {"result": {"data": {"rows": grouped}}}
            return None, {"error": "unsupported_tool"}
        if operation_id.startswith("scrapingdog.") and not SCRAPINGDOG_READY:
            # The sandbox exports SCRAPINGDOG_API_KEY only when the submission
            # carried that credential. Without it the host refuses every
            # scrapingdog call; returning the real error proves the harness
            # degrades to the Deepline path.
            return 402, {"error": "miner_credentials_unavailable"}
        if operation_id == "scrapingdog.scrape":
            self.sd_scrape_calls += 1
            return 200, SCRAPE_HTML
        if operation_id == "scrapingdog.google":
            query = str((params or {}).get("query") or "")
            if "site:linkedin.com/in" in query:
                self.sd_people_calls += 1
                hit = "synthetiq" in query.casefold()
                return 200, (GOOGLE_PEOPLE_RESULTS if hit else {"organic_results": []})
            return 200, GOOGLE_RESULTS
        if operation_id == "scrapingdog.google_news":
            assert params.get("country") in {"us", "gb", "ca", "au", "de", "fr",
                                             "nl", "ie", "in", "sg"}, params
            if str(params.get("query", "")).startswith("Northwind ML"):
                return 200, GOOGLE_NEWS_RESULTS
            return 200, {"news_results": []}
        if operation_id == "openrouter.chat":
            # Only the first model is offered, proving the harness falls through
            # its model list rather than requiring one specific model.
            if params.get("model") != "google/gemini-2.5-flash-lite":
                return None, {"error": "model_not_allowed"}
            prompt = " ".join(str(m.get("content") or "") for m in params.get("messages") or []
                              if isinstance(m, dict) and m.get("role") == "user")
            prompt = "\n".join(str(m.get("content") or "") for m in params.get("messages") or []
                               if isinstance(m, dict) and m.get("role") == "user")
            return 200, llm_profile_response(prompt)
        return None, {"error": "unsupported_operation"}

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                size = int.from_bytes(self._recv(conn, 4), "big")
                frame = json.loads(self._recv(conn, size).decode("utf-8"))
                assert frame["schema_version"] == FRAME_SCHEMA, "wrong schema_version"
                params = frame.get("parameters") or {}
                self.calls.append((frame["operation_id"], params))
                status, body = self._respond(frame["operation_id"], params)
                payload = body if status is None else {
                    "status": status,
                    "headers": {"content-type": "application/json"},
                    "body_b64": base64.b64encode(
                        (body if isinstance(body, str) else json.dumps(body)).encode()
                    ).decode("ascii"),
                }
                encoded = json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")
                conn.sendall(len(encoded).to_bytes(4, "big") + encoded)
            except Exception as exc:  # noqa: BLE001
                print("  [broker] %s: %s" % (type(exc).__name__, exc))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _recv(conn: socket.socket, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = conn.recv(count - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf


def bundle_checks() -> list:
    """Run the repository's own source-bundle validators over the directory."""
    problems = []
    spec = importlib.util.spec_from_file_location(
        "lab_arena.source_bundle", REPO / "lab_arena" / "source_bundle.py")
    if spec is None or spec.loader is None:
        return ["cannot load source_bundle.py from %s" % REPO]
    sb = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(sb)
    except Exception as exc:  # noqa: BLE001
        return ["source_bundle.py import failed: %s" % exc]
    try:
        sb.validate_source_directory(str(HERE), require_license=True)
        print("  validate_source_directory : PASS")
    except Exception as exc:  # noqa: BLE001
        problems.append("validate_source_directory: %s" % exc)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "bundle.tar.gz"
        try:
            info = sb.write_source_archive(str(HERE), str(target))
            print("  archive                   : %d bytes (limit %d)"
                  % (target.stat().st_size, sb.MAX_SOURCE_ARCHIVE_BYTES))
            sb.validate_source_archive(target.read_bytes(), require_license=True)
            print("  validate_source_archive   : PASS  sha256=%s"
                  % hashlib.sha256(target.read_bytes()).hexdigest()[:19])
        except Exception as exc:  # noqa: BLE001
            problems.append("archive: %s: %s" % (type(exc).__name__, exc))
    return problems


def intent_index_checks() -> list:
    """`matched_icp_signal` is an index into the SCORER's signal list.

    _normalized_icp (competition.py:106-132) accepts the singular
    "intent_signal", appends "bonus_intents", reads dict-shaped signals and
    dedupes. Any of those the harness gets wrong shifts our indices, and a
    wrong index 0 fails required_intent_satisfied: the company's whole intent
    score is zeroed and it books the -10 unverified-primary penalty. So diff
    our list against the real one rather than trusting a reading of it.
    """
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(REPO))
    import harness
    from qualification.scoring.competition import _normalized_icp

    shapes = (
        {"intent_signal": "Hired a VP of Engineering in the last 90 days"},
        {"intent_signals": ["Raised a Series A"],
         "bonus_intents": [{"intent_signal": "Opened a new office"}]},
        {"intent_signals": [{"intent_signal": "Posted 5+ engineering roles"},
                            "Announced a partnership"]},
        {"intent_signals": ["Raised a Series A", "Raised a Series A",
                            "Launched a product"]},
    )
    out = []
    for shape in shapes:
        doc = dict(shape, icp_id="icp_1", industry="Software",
                   employee_count="51-200", country="United States")
        ours = harness.Icp(doc).intent_terms
        theirs = _normalized_icp(doc)["intent_signals"]
        if ours != theirs:
            out.append("intent index drift for %r: %r vs scorer %r"
                       % (sorted(shape), ours, theirs))
    print("  intent signal indexing : %s" % ("FAIL" if out else "ok"))
    return out


def ascii_only_checks() -> list:
    """Everything that ships must be readable by everyone who receives it.

    The bundle is a public artifact: Arena operators and competing miners read
    it. A comment nobody on that side can read cannot be checked against the
    scorer line it cites, so the submission is English-only -- code, comments,
    docstrings, README and log strings alike. Scan the files the archive
    actually carries rather than the working tree, since
    source_bundle._source_files takes EVERY regular file under the directory
    (rglob("*"), minus a few ignored directory names) -- a stray backup or
    scratch file ships with the rest.

    Em dash and en dash stay allowed: harness.py:769 splits page titles on them
    and real page titles contain them.
    """
    import tarfile
    import tempfile
    import unicodedata
    sys.path.insert(0, str(REPO))
    from lab_arena.source_bundle import (
        validate_source_directory,
        write_source_archive,
    )

    allowed = {"\u2013", "\u2014", "\u2264"}       # en dash, em dash, <=
    archive = Path(tempfile.mkdtemp()) / "bundle.tar.gz"
    write_source_archive(validate_source_directory(HERE), str(archive))
    out = []
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            handle = bundle.extractfile(member)
            if handle is None:
                continue
            try:
                text = handle.read().decode("utf-8")
            except UnicodeDecodeError:
                out.append("%s is not valid UTF-8" % member.name)
                continue
            offenders = sorted({
                char for char in text
                if ord(char) > 127 and char not in allowed
            })
            if offenders:
                out.append("%s carries %s" % (member.name, ", ".join(
                    "%r (%s)" % (char, unicodedata.name(char, "unnamed"))
                    for char in offenders[:6])))
    print("  bundle is english-only : %s" % ("FAIL" if out else "ok"))
    return out


def record_size_checks() -> list:
    """Hunter's free headcount settles sizes the page cannot.

    The judge sizes a company from LinkedIn's employeeCountRange when the page
    has none (qualification/scoring/linkedin_company_size.py). The harness uses
    Hunter's structured band the same way: an unreadable page size becomes a
    known bucket, an out-of-band record is rejected before any paid search, and
    a page and record that disagree about the ICP band are demoted, not trusted.
    """
    sys.path.insert(0, str(HERE))
    import harness
    out = []
    for band, bucket in (("1-10", "2-10"), ("11-50", "11-50"), ("501-1000", "501-1,000"),
                         ("1001-5000", "1,001-5,000"), ("5001-10000", "5,001-10,000"),
                         ("10001+", "10,001+"), ("", "")):
        if harness.employee_bucket(band) != bucket:
            out.append("Hunter band %r -> %r, expected %r" % (band, harness.employee_bucket(band), bucket))
    icp = harness.Icp({"icp_id": "i1", "industry": "Software", "country": "United States",
                       "employee_count": "51-200", "intent_signals": ["hiring"]})
    base = {"country": "United States", "in_target_industry": "yes"}
    cases = (
        ("page unreadable, record in band", dict(base, employee_count="a few"), {"employee_count": "51-200"}, (True, "")),
        ("page unreadable, record out of band", dict(base, employee_count=""), {"employee_count": "1001-5000"}, (False, None)),
        ("no profile, record out of band", {}, {"employee_count": "10001+"}, (False, None)),
        ("page in, record out", dict(base, employee_count="120"), {"employee_count": "1001-5000"}, (True, "unsized")),
        ("page out, record in", dict(base, employee_count="900"), {"employee_count": "51-200"}, (True, "unsized")),
        ("both in", dict(base, employee_count="120"), {"employee_count": "51-200"}, (True, "")),
        ("no record, page unreadable", dict(base, employee_count=""), None, (True, "unsized")),
    )
    for label, profile, record, (want_ok, want_why) in cases:
        ok, why = harness.matches_icp(profile, icp, record=record)
        if ok is not want_ok or (want_why is not None and why != want_why):
            out.append("%s -> %r" % (label, (ok, why)))
    if harness._employee_count_out({"employee_count": "a few"}, {"employee_count": "51-200"}, icp) != "51-200":
        out.append("record bucket not submitted when the page is unreadable")
    if harness._employee_count_out({"employee_count": "900"}, {"employee_count": "51-200"}, icp) != "51-200":
        out.append("in-band record should win over an out-of-band page reading")
    print("  record size gate       : %s" % ("FAIL" if out else "ok"))
    return out


def paid_budget_checks() -> list:
    """The paid ceilings are enforced inside call(), before any transport."""
    sys.path.insert(0, str(HERE))
    import harness
    out = []
    if harness._paid_kind("exa.search", {}) != "search":
        out.append("exa.search not counted as a paid search")
    if harness._paid_kind("deepline.execute", {"tool": "firecrawl_scrape"}) != "scrape":
        out.append("firecrawl not counted as a paid scrape")
    for op, params in (("deepline.execute", {"tool": "hunter_discover"}),
                       ("deepline.execute", {"tool": "free_simple_company_search"}),
                       ("exa.contents", {}), ("openrouter.chat", {})):
        if harness._paid_kind(op, params):
            out.append("%s %s wrongly counted as paid" % (op, params.get("tool", "")))
    saved = dict(harness._paid)
    try:
        harness._paid["search"] = harness.PAID_SEARCH_BUDGET
        if harness.call("exa.search", {"query": "x"}) is not None:
            out.append("exa.search allowed past PAID_SEARCH_BUDGET")
        if harness._paid["search"] != harness.PAID_SEARCH_BUDGET:
            out.append("a refused call was counted")
        if harness._paid_left("search") != 0:
            out.append("_paid_left not zero at the ceiling")
    finally:
        harness._paid.update(saved)
    harness._reset_budget()
    if any(harness._paid.values()):
        out.append("_reset_budget did not clear paid counters")
    if not (harness.DISCOVERY_SEARCH_BUDGET + harness.TOPUP_SEARCH_RESERVE
            < harness.PAID_SEARCH_BUDGET):
        out.append("discovery share and top-up reserve leave no evidence searches")
    print("  paid call ceilings     : %s" % ("FAIL" if out else "ok"))
    return out


def hunter_checks() -> list:
    """Free Hunter discovery: request vocabulary and row parsing.

    hunter_discover is priced at zero (lab_arena/provider_costs.py) and now runs
    before the paid Exa ladder. Its headcount filter uses Hunter's bands, not
    LinkedIn's, and rows can arrive under result.data.data, data.data or rows.
    """
    sys.path.insert(0, str(HERE))
    import harness
    out = []
    icp = harness.Icp({"icp_id": "i1", "industry": "Software", "country": "United Kingdom",
                       "employee_count": ["2-10", "51-200", "501-1,000", "1,001-5,000"],
                       "intent_signals": ["hiring engineers"],
                       "excluded_companies": ["Excluded Ltd"]})
    if harness._hunter_bands(icp) != ["1-10", "51-200", "501-1000", "1001-5000"]:
        out.append("hunter bands %r" % harness._hunter_bands(icp))
    if harness._hunter_locations(icp) != [{"country": "GB"}]:
        out.append("hunter locations %r" % harness._hunter_locations(icp))
    europe = harness.Icp({"icp_id": "i1", "industry": "Software", "geography": "Europe",
                          "intent_signals": ["x"]})
    if len(harness._hunter_locations(europe)) < 10 or {"country": "DE"} not in harness._hunter_locations(europe):
        out.append("continent not expanded into ISO codes")
    rows = [{"organization": "A", "domain": "a.com"}]
    for envelope in ({"result": {"data": {"data": rows}}}, {"result": {"data": rows}},
                     {"data": {"data": rows}}, {"rows": rows}):
        if harness._hunter_rows(envelope) != rows:
            out.append("hunter rows not parsed from %r" % list(envelope))
    if harness._hunter_rows({"error": "x"}) or harness._hunter_rows(None):
        out.append("junk hunter envelope produced rows")
    if harness._location_text({"city": "Leeds", "country": "GB"}) != "Leeds, GB":
        out.append("dict location not flattened")
    print("  hunter discovery       : %s" % ("FAIL" if out else "ok"))
    return out


def quality_checks() -> list:
    """company_quality_v1: company LinkedIn page and US headquarters state.

    A quality round zeroes any company whose company_linkedin does not
    normalize to a LinkedIn company page, or a United States company without a
    canonical headquarters state (qualification/company_quality.py). The
    harness mirrors both normalizers and must build rows that pass them, while
    leaving company_linkedin unsubmitted outside a quality round.
    """
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(REPO))
    import harness
    out = []
    try:
        from qualification import company_quality as cq
        from qualification.competition_models import CompetitionCompanyV3
    except Exception as exc:  # noqa: BLE001
        return ["cannot import company_quality (%s); LEADPOET_REPO is stale" % exc]

    for value in ("California", "CA", "ca", "washington, d.c.", "DC", "Texas (TX)",
                  "Texas (CA)", "Ontario", "", None, "New York", "  new   york ", "IN"):
        if harness.canonical_us_state(value) != cq.canonical_us_state(value):
            out.append("state %r: ours %r, repo %r" % (value, harness.canonical_us_state(value),
                                                       cq.canonical_us_state(value)))
    for url in ("https://www.linkedin.com/company/acme-labs", "https://uk.linkedin.com/company/Acme/about/",
                "https://www.linkedin.com/company/acme?trk=x", "https://www.linkedin.com/in/jane",
                "https://linkedin.com/company/acme/jobs", "https://linkedin.com/company/acme/foo",
                "https://notlinkedin.com/company/acme", "http://www.linkedin.com/company/12345/",
                "https://www.linkedin.com:8443/company/acme", ""):
        try:
            theirs = cq.canonical_company_linkedin(url)
        except Exception:  # noqa: BLE001
            theirs = ""
        if harness.canonical_company_linkedin(url) != theirs:
            out.append("linkedin %r: ours %r, repo %r"
                       % (url, harness.canonical_company_linkedin(url), theirs))
    if harness.canonical_company_linkedin("linkedin.com/company/acme") != "https://linkedin.com/company/acme":
        out.append("scheme-less database LinkedIn value not canonicalized")

    rows = [{"normalized_domain": "acme.io", "linkedin_url": "linkedin.com/company/acme"}]
    for envelope in ({"result": {"data": {"rows": rows}}}, {"toolResponse": {"raw": {"rows": rows}}},
                     {"data": {"rows": rows}}, {"result": {"data": {"data": {"rows": rows}}}}):
        if harness._deepline_rows(envelope) != rows:
            out.append("Deepline rows not parsed from %s" % sorted(envelope))
    if harness._deepline_rows({"error": "x"}) or harness._deepline_rows(None):
        out.append("junk Deepline envelope produced rows")

    base_icp = {"icp_id": "i1", "industry": "Software", "country": "United States",
                "employee_count": "51-200", "intent_signals": ["hiring engineers"]}
    quality_icp = harness.Icp(dict(base_icp, company_quality_policy="company_quality_v1"))
    plain_icp = harness.Icp(base_icp)
    if not quality_icp.company_quality or plain_icp.company_quality:
        out.append("company_quality_policy marker not detected from the icp")
    fresh = (__import__("datetime").date.today()
             - __import__("datetime").timedelta(days=5)).isoformat()
    comp = {"name": "Acme", "host": "acme.io", "root": "acme.io",
            "url": "https://acme.io/", "text": "Acme builds software in Austin."}
    signals = [{"source": "news", "kind": "hiring", "matched": 0, "date": fresh,
                "url": "https://news.example.com/acme-hiring",
                "snippet": "Acme is hiring senior engineers to grow its platform team."}]
    profile = {"country": "United States", "employee_count": "51-200", "industry": "Software"}
    record = {"linkedin_url": "linkedin.com/company/acme", "location": "Austin, Texas, United States"}

    try:
        row = harness.build_company_multi(comp, quality_icp, [dict(x) for x in signals], profile,
                                          record=record)
        if row.get("company_linkedin") != "https://linkedin.com/company/acme":
            out.append("quality row company_linkedin %r" % row.get("company_linkedin"))
        if row.get("state") != "Texas":
            out.append("quality row state %r (expected Texas from the record location)"
                       % row.get("state"))
        claim, errors = cq.normalize_company_claim(
            CompetitionCompanyV3.model_validate(row).model_dump(mode="json"))
        if errors:
            out.append("quality row fails company_quality: %s" % (errors,))
    except Exception as exc:  # noqa: BLE001
        out.append("quality row could not be built: %r" % exc)

    for label, rec, prof in (("no LinkedIn", {"location": "Austin, Texas"}, profile),
                             ("no US state", {"linkedin_url": "linkedin.com/company/acme"}, profile)):
        try:
            harness.build_company_multi(comp, quality_icp, [dict(x) for x in signals], prof,
                                        record=rec)
            out.append("quality round accepted a company with %s" % label)
        except ValueError:
            pass
    try:
        conflict = [dict(signals[0]), dict(signals[0], url="https://www.linkedin.com/company/other-co",
                                                   snippet="Acme is hiring senior engineers now.")]
        harness.build_company_multi(comp, quality_icp, conflict, profile, record=record)
        out.append("conflicting LinkedIn slugs were submitted")
    except ValueError:
        pass
    try:
        row = harness.build_company_multi(comp, plain_icp, [dict(x) for x in signals], profile,
                                          record=record)
        if row.get("company_linkedin"):
            out.append("company_linkedin submitted outside a quality round")
    except Exception as exc:  # noqa: BLE001
        out.append("plain row could not be built: %r" % exc)
    print("  company quality        : %s" % ("FAIL" if out else "ok"))
    return out


def integrity_parity_checks() -> list:
    """arena_integrity_v1 grouping and the optional date, against the repo.

    The live policy judges up to three distinct URLs per requested criterion as
    one claim and counts only the strongest row per criterion, and the contract
    now accepts an undated signal. The harness shapes and estimates evidence on
    both assumptions, so diff them against the repository's own code.
    """
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(REPO))
    import harness
    out = []
    try:
        from qualification.scoring.arena_integrity import (
            MAX_EVIDENCE_PER_CRITERION, bounded_criterion_evidence)
        from qualification.competition_models import CompetitionCompany
    except Exception as exc:  # noqa: BLE001
        return ["cannot import arena_integrity (%s); LEADPOET_REPO is stale" % exc]

    if MAX_EVIDENCE_PER_CRITERION != harness.MAX_EVIDENCE_PER_CRITERION:
        out.append("MAX_EVIDENCE_PER_CRITERION %d vs scorer %d"
                   % (harness.MAX_EVIDENCE_PER_CRITERION, MAX_EVIDENCE_PER_CRITERION))
    rows = [{"matched_icp_signal": i, "url": u} for i, u in (
        (0, "https://a.example.com/1"), (0, "https://b.example.com/2"),
        (0, "https://a.example.com/1"), (1, "https://c.example.com/3"),
        (0, "https://d.example.com/4"), (0, "https://e.example.com/5"),
        (1, "https://c.example.com/6"), (2, "https://f.example.com/7"))]
    theirs = [[r["url"] for r in g] for g in bounded_criterion_evidence(rows)]
    mine = [[r["url"] for r in g] for g in harness._criterion_groups(rows).values()]
    if theirs != mine:
        out.append("criterion grouping differs: ours %r, scorer %r" % (mine, theirs))

    icp = harness.Icp({"icp_id": "i1", "industry": "Software", "country": "United States",
                       "employee_count": "51-200",
                       "intent_signals": ["hiring engineers", "raised funding"],
                       "intent_max_age_days": 90})
    site = "https://acme.io"
    today = __import__("datetime").date.today()
    fresh = (today - __import__("datetime").timedelta(days=10)).isoformat()
    stale = (today - __import__("datetime").timedelta(days=400)).isoformat()
    three = [{"matched_icp_signal": 0, "url": "https://news%d.example.com/a" % n,
              "date": fresh} for n in range(3)]
    if harness._breadth_total(three, icp, site) != 54.0:
        out.append("three articles on one criterion should count once (54), got %s"
                   % harness._breadth_total(three, icp, site))
    two_criteria = three[:1] + [{"matched_icp_signal": 1,
                                 "url": "https://other.example.com/b", "date": fresh}]
    if harness._breadth_total(two_criteria, icp, site) != 80.0:
        out.append("two distinct criteria should reach the 80 cap, got %s"
                   % harness._breadth_total(two_criteria, icp, site))
    if harness._breadth_total([{"matched_icp_signal": 1, "url": "https://x.example.com/",
                                "date": fresh}], icp, site) != 0.0:
        out.append("a company with no primary criterion must score 0")
    if harness._expected_score({"url": "https://y.example.com/", "matched_icp_signal": 0},
                               icp, site) <= 0.0:
        out.append("undated evidence is estimated at zero; integrity keeps it eligible")
    if harness._expected_score({"url": "https://y.example.com/", "matched_icp_signal": 0,
                                "date": stale}, icp, site) != 0.0:
        out.append("evidence dated past the buyer window must estimate zero")
    if harness._effective_cap({"url": "https://boards.greenhouse.io/acme/jobs/1"}, site) != 54.0:
        out.append("an unverified job path must estimate at the news multiplier")

    company = {"company_name": "Acme", "company_website": site + "/", "industry": "Software",
               "employee_count": "51-200", "country": "United States",
               "fit_summary": "Acme builds software.", "fit_evidence_urls": [site + "/"],
               "intent_signals": [{"matched_icp_signal": 0, "description": "Hiring.",
                                   "why_now": "Now.", "url": "https://y.example.com/job",
                                   "snippet": "Acme is hiring engineers."}]}
    try:
        CompetitionCompany.model_validate(company)
    except Exception as exc:  # noqa: BLE001
        out.append("an undated signal no longer validates: %s" % str(exc)[:160])
    # End to end through the builder: an undated row must survive, and a
    # future or unparseable date must be REMOVED rather than submitted --
    # Optional[date] rejects a non-ISO string, and one bad row fails the whole
    # document at CompetitionScorerInputError.
    comp = {"name": "Acme", "host": "acme.io", "root": "acme.io",
            "url": site + "/", "text": "Acme builds software."}
    future = (today + __import__("datetime").timedelta(days=9)).isoformat()
    raw = [{"source": "news", "kind": "hiring", "matched": 0,
            "url": "https://news%d.example.com/acme" % n,
            "snippet": "Acme is hiring senior engineers to grow its platform team.",
            "date": d} for n, d in enumerate(("", future, "not-a-date",
                                              fresh + "T09:00:00.000Z"))]
    try:
        built = harness.build_company_multi(comp, icp, raw, {})
    except Exception as exc:  # noqa: BLE001
        built = None
        out.append("builder raised on undated/odd dates: %r" % exc)
    if built is not None:
        dates = [sig.get("date") for sig in built["intent_signals"]]
        if len(built["intent_signals"]) != 3:
            out.append("expected 3 rows kept for one criterion, got %d"
                       % len(built["intent_signals"]))
        if any(d not in (None, fresh) for d in dates):
            out.append("builder emitted an unusable date: %r" % dates)
        try:
            CompetitionCompany.model_validate(built)
        except Exception as exc:  # noqa: BLE001
            out.append("builder output fails the contract: %s" % str(exc)[:160])
    print("  integrity parity       : %s" % ("FAIL" if out else "ok"))
    return out


def industry_parity_checks() -> list:
    """Our submitted-industry verdict must equal the scorer's, pair for pair.

    lead_scorer._industry_evidence_decision runs the SUBMITTED industry through
    leadpoet_verifier.industry_fit's taxonomy, and an explicit conflict is a
    submitted fit MISMATCH -- zero for the company plus -10. The taxonomy is
    stricter than intuition ("Applied AI" conflicts with "Software"), so the
    harness vendors the same code and must reproduce the same verdicts. Diff
    against the repository function directly rather than trusting a reading.
    """
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(REPO))
    import harness
    from qualification.scoring.lead_scorer import _industry_evidence_decision

    pairs = (
        ("Software", "Software"), ("Software", "Computer Software"),
        ("Software", "Information Technology"), ("Software", "Applied AI"),
        ("Software", "Artificial Intelligence"), ("Software", "Logistics"),
        ("Software", "Financial Services"), ("Software", "Machine Learning"),
        ("Software", "SaaS"), ("Financial Services", "Fintech"),
        ("Financial Services", "Banking"), ("Financial Services", "Software"),
        ("Healthcare", "Biotechnology"), ("Healthcare", "Medical Devices"),
        ("Healthcare", "Software"), ("Manufacturing", "Industrial Machinery"),
        ("Manufacturing", "Software"), ("Software", ""), ("", "Software"),
    )
    out = []
    for icp_industry, candidate in pairs:
        theirs = _industry_evidence_decision(candidate, "", icp_industry)
        ours = harness.submitted_industry_decision(candidate, icp_industry)
        if ours != theirs:
            out.append("industry %r vs ICP %r: ours %s, scorer %s"
                       % (candidate, icp_industry, ours, theirs))

    icp = harness.Icp({"icp_id": "i1", "industry": "Software",
                       "country": "United States", "employee_count": "51-200",
                       "intent_signals": ["hiring engineers"]})
    # An in-industry company the profiler labelled with a conflicting term is
    # rescued by the ICP's own term, which the scorer then calls a MATCH.
    label, verdict = harness.choose_industry(
        {"industry": "Applied AI", "sub_industry": "", "in_target_industry": "yes"}, icp)
    if (label, verdict) != ("Software", "match"):
        out.append("confirmed in-industry company not rescued: %r" % ((label, verdict),))
    # A conflicting label with no confirmation must be rejected, not submitted
    # for a -10.
    ok, _ = harness.matches_icp({"industry": "Logistics", "in_target_industry": "unclear",
                                 "country": "United States", "employee_count": "120"}, icp)
    if ok:
        out.append("taxonomy-conflicting industry accepted without confirmation")
    # Having no label is not a conflict.
    ok, why = harness.matches_icp({"industry": "", "in_target_industry": "unclear",
                                   "country": "United States", "employee_count": "120"}, icp)
    if not ok:
        out.append("empty industry label treated as a conflict: %s" % why)
    print("  industry parity        : %s" % ("FAIL" if out else "ok"))
    return out


def evidence_safety_checks() -> list:
    """Two ways a real page can destroy the signal it was meant to prove.

    lead_scorer.py:4004-4029 runs a negation regex over the description AND
    snippet WE submit; one hit flags the signal as self-contradicting, because
    "evidence URL appears to NOT support the claim". A stale job posting is the
    trap: its on-topic sentence is often the one that kills it. And
    check_future_date (verification_helpers.py:1364-1380) rejects a future date
    as fabricated -- while the harness used to clamp a negative age to zero,
    which made the worst possible date sort ahead of every real one.
    """
    sys.path.insert(0, str(HERE))
    import datetime as dt
    import harness

    out = []
    for text, expected in (
        ("Acme is hiring a Senior Platform Engineer in London. Apply now.", False),
        ("This position is no longer open.", True),
        ("There are 0 open positions at this time.", True),
        ("We are not currently hiring for this role.", True),
        ("404 - page not found", True),
        ("Unable to verify this listing.", True),
        ("No evidence of recent hiring was found.", True),
    ):
        if harness.self_contradicting(text) is not expected:
            out.append("self_contradicting(%r) != %s" % (text[:40], expected))

    dead = ("Careers at Acme\n"
            "Acme is hiring a Staff Engineer to join the platform team in Leeds.\n"
            "This position is no longer open.\n")
    snippet = harness.pick_snippet(dead, harness.HIRING_TERMS)
    if harness.self_contradicting(snippet):
        out.append("pick_snippet quoted a self-contradicting sentence: %r" % snippet)
    if not snippet:
        out.append("pick_snippet found no usable sentence on a page that has one")

    only_dead = "This role is no longer open and the posting has been removed.\n"
    if harness.pick_snippet(only_dead, harness.HIRING_TERMS):
        out.append("pick_snippet quoted a page whose every sentence contradicts")

    today = dt.date.today()
    if harness._age_days((today + dt.timedelta(days=5)).isoformat()) is not None:
        out.append("a future date is still treated as usable")
    if harness._age_days((today - dt.timedelta(days=10)).isoformat()) != 10:
        out.append("a past date no longer ages correctly")
    print("  evidence safety        : %s" % ("FAIL" if out else "ok"))
    return out


def country_gate_checks() -> list:
    """The scorer compares against ONE geography value, and we must not be broader.

    competition.py:139-140 takes icp["country"], else icp["geography"], else
    "United States"; _submitted_geography_decision (lead_scorer.py:1439) runs
    check_country_match against exactly that. A submitted MISMATCH is a zero
    plus -10. Reading a plural field the scorer never looks at, or ignoring the
    "geography" it falls back to, both produced companies it would reject.
    """
    sys.path.insert(0, str(HERE))
    import harness

    out = []
    for raw, expected in (
        ({"country": "United Kingdom"}, ["united kingdom"]),
        ({"geography": "United Kingdom"}, ["united kingdom"]),
        # "countries" is not an Arena field: the scorer defaults to the US here.
        ({"countries": ["United Kingdom", "Ireland"]}, ["united states"]),
        ({"country": "United States, West Coast"}, ["united states"]),
        ({"country": "United States or Canada"}, ["united states", "canada"]),
        ({}, ["united states"]),
    ):
        icp = harness.Icp(dict(raw, icp_id="i1", industry="Software",
                               intent_signals=["hiring engineers"]))
        if icp.countries != expected:
            out.append("ICP %r -> countries %r, expected %r"
                       % (raw, icp.countries, expected))
    europe = harness.Icp({"icp_id": "i1", "industry": "Software",
                          "geography": "Europe", "intent_signals": ["hiring"]})
    if "germany" not in europe.countries:
        out.append("continent not expanded: %r" % (europe.countries[:5],))
    if harness.matches_icp({"country": "United States"}, europe)[0]:
        out.append("US company accepted under a Europe ICP")
    # An unresolvable region must leave NO filter, not a filter nothing matches.
    emea = harness.Icp({"icp_id": "i1", "industry": "Software",
                        "geography": "EMEA", "intent_signals": ["hiring"]})
    if emea.countries or not harness.matches_icp({"country": "Germany"}, emea)[0]:
        out.append("unresolvable geography rejected everything: %r" % (emea.countries,))
    # Never echo a region back as the company's country.
    if harness._country_out("", europe) or harness._country_out("Kenya", europe):
        out.append("emitted a country the ICP geography does not allow")
    if harness._country_out("", harness.Icp(
            {"icp_id": "i1", "industry": "Software", "country": "United Kingdom",
             "intent_signals": ["hiring"]})):
        out.append("missing headquarters country must not copy the ICP")
    print("  country gate           : %s" % ("FAIL" if out else "ok"))
    return out


def industry_gate_checks() -> list:
    """industry is the one submitted fit dimension with no arithmetic answer.

    _industry_evidence_decision (lead_scorer.py:434-519) defers to
    leadpoet_verifier.industry_fit, and only an explicit taxonomy conflict is a
    MISMATCH -- which short-circuits the fit verification into a zero plus a
    -10 gate failure. The harness asks the profiler the same question and must
    reject on an unambiguous "no" ONLY; anything unresolved has to pass, the
    way UNAVAILABLE does on the scorer side.
    """
    sys.path.insert(0, str(HERE))
    import harness

    out = []
    for raw, expected in ((True, "yes"), (False, "no"), ("yes", "yes"),
                          ("NO", "no"), ("unclear", "unclear"), (None, "unclear"),
                          ("maybe", "unclear"), (1, "unclear")):
        got = harness._tri(raw)
        if got != expected:
            out.append("_tri(%r) = %r, expected %r" % (raw, got, expected))

    icp = harness.Icp({"icp_id": "i1", "industry": "Software",
                       "country": "United States", "employee_count": "51-200",
                       "intent_signals": ["hiring engineers"]})
    base = {"country": "United States", "employee_count": "120"}
    for verdict, should_pass in (("no", False), ("yes", True),
                                 ("unclear", True), (None, True)):
        profile = dict(base)
        if verdict is not None:
            profile["in_target_industry"] = verdict
        ok, why = harness.matches_icp(profile, icp)
        if ok is not should_pass:
            out.append("in_target_industry=%r -> %r (%s)" % (verdict, ok, why))
    # An ICP with no industry has nothing to contradict.
    blank = harness.Icp({"icp_id": "i1", "industry": "", "country": "United States",
                         "intent_signals": ["hiring engineers"]})
    if not harness.matches_icp({"in_target_industry": "no"}, blank)[0]:
        out.append("industry gate fired on an ICP that declares no industry")
    print("  industry gate          : %s" % ("FAIL" if out else "ok"))
    return out


def employee_bucket_checks() -> list:
    """The scorer reads employee_count as a BUCKET, not a range.

    competition.py:272-277 normalizes the submitted value and drops the company
    outright when the result is not one the ICP declared -- no row, no score,
    no penalty, and the per-ICP denominator unchanged. So an unmappable or
    neighbouring-bucket value silently costs a full company slot, which is why
    the harness has to answer exactly the question the scorer asks.
    """
    sys.path.insert(0, str(HERE))
    import harness

    out = []
    for raw, expected in (("51-200", "51-200"), ("150", "51-200"),
                          ("501-1000", "501-1,000"), ("1-10", "2-10"),
                          ("10001+", "10,001+"), ("unknown", "")):
        got = harness.employee_bucket(raw)
        if got != expected:
            out.append("employee_bucket(%r) = %r, expected %r" % (raw, got, expected))
    for raw, expected in (("approximately 150", "51-200"),
                          ("~200 staff", "51-200"), ("2,500", "1,001-5,000")):
        got = harness.employee_bucket(raw, loose=True)
        if got != expected:
            out.append("employee_bucket(%r, loose) = %r, expected %r"
                       % (raw, got, expected))

    icp = harness.Icp({"industry": "Software", "employee_count": "11-50|51-200",
                       "intent_signals": ["hiring engineers"]})
    if icp.employee_buckets != ["11-50", "51-200"]:
        out.append("pipe-joined ICP bands -> %r" % (icp.employee_buckets,))
    ok, _ = harness.matches_icp({"country": "", "employee_count": "120"}, icp)
    if not ok:
        out.append("in-bucket company rejected")
    ok, why = harness.matches_icp({"country": "", "employee_count": "900"}, icp)
    if ok:
        out.append("out-of-bucket company accepted (would be dropped unscored)")
    ok, why = harness.matches_icp({"country": "", "employee_count": "a few"}, icp)
    if not ok or why != "unsized":
        out.append("unreadable size should be demoted, not rejected: %r" % ((ok, why),))
    print("  employee bucket mapping: %s" % ("FAIL" if out else "ok"))
    return out


def validate_companies(companies) -> list:
    """Both halves of the chain the scorer actually runs on our output.

    This used to validate against gateway.qualification.models.CompanyOutput,
    which is the WRONG model: that is the internal shape the scorer builds
    AFTER normalising, and it requires a per-signal `source` and forbids
    `fit_summary`. The submission contract is CompetitionCompany
    (qualification/competition_models.py) -- extra="forbid", requiring
    fit_summary, fit_evidence_urls and a per-signal why_now and date, and
    forbidding `source`, `description` and `sub_industry`. Validating against
    the wrong model is what shaped the harness output to the wrong schema, and
    every submission would have been rejected before a company was scored.

    So check the real contract first, then push the result through
    _normalized_company -> CompanyOutput exactly as competition.py:306-311
    does. The first raises CompetitionScorerInputError and kills the whole
    submission; the second is caught per company and only zeroes that one.
    """
    sys.path.insert(0, str(REPO))
    try:
        from qualification.competition_models import (
            validate_companies as validate_submission,
        )
        from qualification.scoring.competition import _normalized_company
        from gateway.qualification.models import CompanyOutput
    except Exception as exc:  # noqa: BLE001
        return ["cannot import the competition contract (%s: %s); run from the "
                "leadpoet venv with LEADPOET_REPO set"
                % (type(exc).__name__, str(exc)[:120])]

    problems = []
    try:
        validate_submission(list(companies), max_companies=5, schema_version=OUTPUT_SCHEMA)
    except Exception as exc:  # noqa: BLE001
        problems.append("submission contract rejected the document: %s: %s"
                        % (type(exc).__name__, str(exc)[:600]))
        return problems
    for i, item in enumerate(companies):
        try:
            CompanyOutput(**_normalized_company(dict(item), integrity_policy=True,
                                                contacts_required=True))
        except Exception as exc:  # noqa: BLE001
            problems.append("company %d fails the scorer's model contract: "
                            "%s: %s" % (i, type(exc).__name__, str(exc)[:300]))
    return problems


def main() -> int:
    argparse.ArgumentParser(description="Offline Arena harness test").parse_args()

    print("=" * 70)
    print("1. source bundle contract (repo validators)")
    print("=" * 70)
    failures = bundle_checks()
    failures += employee_bucket_checks()
    failures += intent_index_checks()
    failures += industry_gate_checks()
    failures += country_gate_checks()
    failures += ascii_only_checks()
    failures += evidence_safety_checks()
    failures += industry_parity_checks()
    failures += integrity_parity_checks()
    failures += quality_checks()
    failures += hunter_checks()
    failures += paid_budget_checks()
    failures += record_size_checks()

    work = Path(tempfile.mkdtemp(prefix="arena-"))
    (work / "input").mkdir(); (work / "output").mkdir()
    (work / "input" / "icp.json").write_text(
        json.dumps({"schema_version": "leadpoet.lab_arena.icp_input.v1",
                    "icp": (dict(SAMPLE_ICP, company_quality_policy="company_quality_v1")
                            if os.environ.get("LT_QUALITY") == "1" else SAMPLE_ICP),
                    # host contract since 0796f157: company_limit rides in the document
                    "company_limit": int(os.environ.get("LT_COMPANY_LIMIT", "5"))}), encoding="utf-8")

    sock_path = str(Path(tempfile.mkdtemp(prefix="s-")) / "w.sock")
    broker = FakeBroker(sock_path)
    broker.start()

    # Run the HOST entrypoint, not run_icp directly: this is what the Arena does,
    # and it is the only way a return-type or signature break shows up.
    # agent_entrypoint.run() takes Path objects and calls .read_text on them.
    # Every lab_arena_checkpoint.write -- the harness's own checkpoints and the
    # entrypoint's final write -- is also copied to a numbered snapshot, so each
    # document the host could have frozen at a deadline gets validated below.
    snapshots = work / "checkpoints"
    snapshots.mkdir()
    runner = "\n".join([
        "import json, sys",
        "from pathlib import Path",
        "sys.path.insert(0, %r)" % str(REPO),
        "sys.path.insert(0, %r)" % str(REPO / "lab_arena"),
        "import lab_arena_checkpoint as ck",
        "_write = ck.write",
        "def _recorded(companies, *, output_path=ck.OUTPUT_PATH):",
        "    index = len(list(Path(%r).glob('*.json')))" % str(snapshots),
        "    Path(%r, '%%03d.json' %% index).write_text(json.dumps(companies))" % str(snapshots),
        "    return _write(companies, output_path=output_path)",
        "ck.write = _recorded",
        "import lab_arena.agent_entrypoint as ae",
        "ae.run(source_dir=Path(%r), input_path=Path(%r), output_path=Path(%r))"
        % (str(HERE), str(work / "input" / "icp.json"),
           str(work / "output" / "companies.json")),
    ])
    print()
    print("=" * 70)
    print("2. host entrypoint -> harness.run_icp")
    print("=" * 70)
    proc = subprocess.run(
        [sys.executable, "-c", runner],
        env=dict(os.environ, LAB_ARENA_WORKER_SOCKET=sock_path,
                 LAB_ARENA_OUTPUT_PATH=str(work / "output" / "companies.json")),
        capture_output=True, text=True, timeout=300)
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print("--- stderr ---")
        print(proc.stderr.rstrip()[-2000:])
    broker.stop()
    # Paid spend for this ICP, from the calls the broker actually answered.
    # Exa search is priced at the upper fixture charge, 0.14 Deepline credits
    # ($0.014); batched exa.contents at 0.02 credits ($0.002). firecrawl_scrape
    # is dynamically priced, so it is counted but not priced.
    sys.path.insert(0, str(HERE))
    import harness as _h
    spend = broker.search_calls * 0.014 + broker.contents_calls * 0.002
    print("  paid calls: exa.search=%d (ceiling %d)  exa.contents=%d  firecrawl=%d (ceiling %d)  hunter=%d"
          % (broker.search_calls, _h.PAID_SEARCH_BUDGET, broker.contents_calls,
             broker.scrape_calls, _h.PAID_SCRAPE_BUDGET, broker.hunter_calls))
    print("  partial fixture cost: $%.3f this ICP, $%.2f over 20 ICPs"
          " (EXCLUDES contacts, LLM and dynamic scrape fees; not an eligibility estimate)"
          % (spend, spend * 20))
    print("  contact calls: %d (ceiling %d)" % (broker.contact_calls, _h.CONTACT_CALL_BUDGET))
    print("  scrapingdog: key %s, page reads %d (ceiling %d), people searches %d (ceiling %d)"
          % ("present" if SCRAPINGDOG_READY else "absent",
             broker.sd_scrape_calls, _h.SCRAPINGDOG_SCRAPE_BUDGET,
             broker.sd_people_calls, _h.SCRAPINGDOG_PEOPLE_BUDGET))
    if not SCRAPINGDOG_READY and broker.sd_people_calls:
        failures.append("a Google people search ran without the credential")
    if broker.sd_people_calls > _h.SCRAPINGDOG_PEOPLE_BUDGET:
        failures.append("google people searches %d exceed the ceiling %d"
                        % (broker.sd_people_calls, _h.SCRAPINGDOG_PEOPLE_BUDGET))
    if not SCRAPINGDOG_READY and broker.sd_scrape_calls:
        failures.append("scrapingdog was called without its credential")
    if broker.sd_scrape_calls > _h.SCRAPINGDOG_SCRAPE_BUDGET:
        failures.append("scrapingdog page reads %d exceed the ceiling %d"
                        % (broker.sd_scrape_calls, _h.SCRAPINGDOG_SCRAPE_BUDGET))
    if SCRAPINGDOG_READY and broker.scrape_calls:
        failures.append("firecrawl ran %d time(s) while Scrapingdog was available"
                        % broker.scrape_calls)
    if broker.contact_calls > _h.CONTACT_CALL_BUDGET:
        failures.append("contact call budget exceeded")
    if broker.search_calls > _h.PAID_SEARCH_BUDGET:
        failures.append("exa.search %d exceeds PAID_SEARCH_BUDGET %d"
                        % (broker.search_calls, _h.PAID_SEARCH_BUDGET))
    if broker.scrape_calls > _h.PAID_SCRAPE_BUDGET:
        failures.append("firecrawl %d exceeds PAID_SCRAPE_BUDGET %d"
                        % (broker.scrape_calls, _h.PAID_SCRAPE_BUDGET))
    if proc.returncode != 0:
        failures.append("host entrypoint exited %d" % proc.returncode)

    print()
    print("=" * 70)
    print("3. output contract")
    print("=" * 70)
    sys.path.insert(0, str(HERE))
    import harness
    out_file = work / "output" / "companies.json"
    companies = None
    if not out_file.exists():
        failures.append("no companies.json written")
    else:
        raw = out_file.read_bytes()
        print("  size: %d bytes" % len(raw))
        doc = json.loads(raw.decode("utf-8"))
        # The host wraps the list: {"companies": [...]}
        companies = doc.get("companies") if isinstance(doc, dict) else doc
        if not isinstance(companies, list):
            failures.append("companies is not a list")
            companies = None
        elif len(companies) > 5:
            failures.append("more than 5 companies (%d)" % len(companies))

    # Checkpoints. The last snapshot is the entrypoint's final write; every one
    # before it is a harness checkpoint the host could have frozen at the
    # deadline, so each must pass the same contract as the final output, must
    # never shrink, and the last must equal what was finally returned.
    saved = [json.loads(path.read_text(encoding="utf-8"))
             for path in sorted(snapshots.glob("*.json"))]
    checkpoints = saved[:-1]
    print("  checkpoints: %s company count(s) before the final write"
          % ([len(doc) for doc in checkpoints] or "none"))
    if companies:
        if not checkpoints:
            failures.append("no checkpoint was written before the final output")
        elif checkpoints[-1] != companies:
            failures.append("last checkpoint differs from the final output")
        for number, doc in enumerate(checkpoints):
            problems = validate_companies(doc)
            if problems:
                failures.append("checkpoint %d invalid: %s" % (number, problems[0]))
        counts = [len(doc) for doc in checkpoints]
        if counts != sorted(counts):
            failures.append("a checkpoint shrank: %s" % counts)
        # Slot companies must be saved before the final emission announces any
        # company; saving only at the end left nothing if the run stopped first.
        lines = proc.stdout.splitlines()
        first_save = next((i for i, l in enumerate(lines) if "checkpoint saved" in l), None)
        first_final = next((i for i, l in enumerate(lines) if l.rstrip().endswith("signal(s)")), None)
        if first_save is None or (first_final is not None and first_save > first_final):
            failures.append("no checkpoint was saved before the final emission")

    if companies is not None:
        failures.extend(validate_companies(companies))
        if not companies:
            failures.append("happy-path v5 fixture returned no companies")
        if any(not c.get("contact") for c in companies):
            failures.append("contact-required output contains an unsupported company")
        caps = {0: 0.0, 1: 60.0, 2: 80.0, 3: 88.0, 4: 92.0, 5: 96.0, 6: 100.0}
        print("\n  %-20s %-3s %-5s %-13s %-11s %s"
              % ("company", "sig", "cap", "sources", "matched_icp", "dates"))
        print("  " + "-" * 76)
        unmatched = undated = others = 0
        for c in companies:
            sigs = c.get("intent_signals") or []
            n = len(sigs)
            dated = sum(1 for s in sigs if s.get("date"))
            if not any(s.get("matched_icp_signal") == 0 for s in sigs):
                unmatched += 1
            undated += n - dated
            # The submitted document carries no `source` -- the contract forbids
            # it. Show what the SCORER will derive from each URL instead, since
            # that is what picks the multiplier (competition.py:206).
            site = c.get("company_website") or ""
            derived = [harness.scorer_source(s.get("url"), site) for s in sigs]
            others += sum(1 for d in derived if d == "company_website")
            print("  %-20s %-3d %-5.0f %-13s %-11s %d/%d"
                  % (str(c.get("company_name"))[:20], n, caps.get(min(n, 6), 100.0),
                     ",".join(sorted(set(derived)))[:13],
                     ",".join(str(s.get("matched_icp_signal", "X")) for s in sigs)[:11],
                     dated, n))
        if unmatched:
            print("\n  WARNING: %d company(ies) carry no matched_icp_signal=0 "
                  "-> unverified_primary penalty" % unmatched)
        if undated:
            # Informational. Under arena_integrity_v1 an absent date is
            # "uncertain" and stays eligible for ordinary verification.
            print("  note: %d signal(s) undated -> eligible, judged on content "
                  "alone" % undated)
        if others:
            # company_website is the LOWEST multiplier the scorer can derive,
            # 0.85 -- below the 0.9 that any unrecognised third-party host
            # gets. Citing the company's own site is the one avoidable loss.
            print("  note: %d signal(s) resolve to company_website (0.85x, the "
                  "lowest); a third-party URL scores 0.9x or better" % others)
        if not unmatched:
            print("\n  no scoring warnings")

    print("\n" + "=" * 70)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("RESULT: PASS -- valid Arena source bundle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
