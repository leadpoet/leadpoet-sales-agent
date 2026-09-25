"""v18 port of the baseline's contact pipeline onto the v16 engine."""

from __future__ import annotations

import json

import httpx
import pytest

from arena_transport import ArenaToolClient
from experiments.harness_bakeoff.adapters import pydantic_ai as adapter


def _client(handle):
    return ArenaToolClient(client=httpx.Client(transport=httpx.MockTransport(handle)))


def test_contact_tools_dispatch_through_the_validated_provider() -> None:
    seen: list[tuple[str, dict]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)["payload"]))
        return httpx.Response(200, request=request, json={"result": {"data": {"elements": []}}})

    tools = _client(handle)
    tools.call("harvestapi_search_leads", {"currentJobTitles": "VP of Product", "search": "Example", "page": 1})
    tools.call("harvestapi_get_profile", {"url": "linkedin.com/in/jane-doe", "findEmail": "true"})
    assert seen[0][0].endswith("/harvestapi_search_leads/execute")
    assert seen[1] == ("/api/v2/integrations/harvestapi_get_profile/execute",
                       {"url": "https://www.linkedin.com/in/jane-doe/", "findEmail": "true"})
    assert tools.deepline_calls == 2


def test_contact_provider_rejects_unsafe_arguments() -> None:
    tools = _client(lambda request: httpx.Response(200, request=request, json={"result": {}}))
    with pytest.raises(ValueError):
        tools.call("harvestapi_get_profile", {"url": "https://www.linkedin.com/company/acme", "findEmail": "true"})
    with pytest.raises(ValueError):
        tools.call("harvestapi_search_leads", {"currentJobTitles": "CTO", "page": 2, "search": "Acme"})
    with pytest.raises(ValueError):
        tools.call("harvestapi_search_leads", {"search": "Acme", "page": 1})


def test_submit_accepts_contacts_only_when_the_run_allows_them() -> None:
    tools = _client(lambda request: httpx.Response(200, request=request, json={"result": {}}))
    company = {
        "company_name": "Acme", "company_website": "https://acme.example/", "industry": "Software",
        "employee_count": "11-50", "company_stage": "Series A", "country": "United States",
        "fit_summary": "fits", "fit_evidence_urls": ["https://acme.example/about"], "intent_signals": [{
            "matched_icp_signal": 0, "description": "raised a Series A", "url": "https://news.example/acme",
            "snippet": "Acme today announced it has raised a Series A round led by Example Ventures to grow",
            "date": "2026-09-01", "why_now": "fresh capital",
        }],
        "contact": {
            "full_name": "Jane Doe", "role": "VP of Product",
            "linkedin_url": "https://www.linkedin.com/in/jane-doe/",
            "location": {"country": "US"}, "email": "jane@acme.example",
            "email_source": {"provider": "harvestapi", "tool": "harvestapi_get_profile", "record_id": "rec-1"},
        },
    }
    tools.allow_contacts = True
    kept = tools.call("submit_companies", {"companies": [company]})["companies"]
    assert kept[0]["contact"]["email"] == "jane@acme.example"


def test_contact_reserve_shrinks_the_research_budget_only_when_required() -> None:
    assert adapter._CONTACT_DEEPLINE_RESERVE == 14
    assert adapter._BASE_RESEARCH_DEEPLINE_CALLS - adapter._CONTACT_DEEPLINE_RESERVE == 14
    assert adapter._ARENA_FINALIZE_ELAPSED_SECONDS < adapter._ARENA_HARD_DEADLINE_SECONDS - 60


def test_deadline_call_refuses_when_no_time_remains() -> None:
    class Client:
        timeout = 90.0

    calls: list[str] = []
    bounded = adapter._DeadlineProviderCall(lambda n, a: calls.append(n), Client(), deadline=100.0, clock=lambda: 99.5)
    with pytest.raises(RuntimeError):
        bounded("harvestapi_get_profile", {})
    assert calls == []
    ok = adapter._DeadlineProviderCall(lambda n, a: calls.append(n), Client(), deadline=100.0, clock=lambda: 10.0)
    ok("harvestapi_get_profile", {})
    assert calls == ["harvestapi_get_profile"]


# --- v19: precise, cheaper contact lookups ---------------------------------

from experiments.harness_bakeoff.contacts import (  # noqa: E402
    _employee_band_conflict,
    _search_company_matches,
    _search_request,
    enrich_contacts,
)


def _icp(**overrides):
    icp = {
        "contact_policy": "contacts_v1",
        "target_roles": ["Operations Manager", "Plant Manager"],
        "target_seniority": "",
        "contact_geography": {"countries": [], "regions": [], "cities": []},
        "employee_count": ["51-200", "201-500"],
    }
    icp.update(overrides)
    return icp


def _company(**overrides):
    company = {"company_name": "Thunes", "company_website": "https://www.thunes.com/", "company_linkedin": ""}
    company.update(overrides)
    return company


def test_company_record_tool_dispatches_and_rejects_person_urls() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)["payload"]))
        return httpx.Response(200, request=request, json={"result": {"data": {"element": {}}}})

    tools = _client(handle)
    tools.call("harvestapi_get_company", {"url": "linkedin.com/company/thunespayments"})
    assert seen[0] == ("/api/v2/integrations/harvestapi_get_company/execute",
                       {"url": "https://www.linkedin.com/company/thunespayments/"})
    with pytest.raises(ValueError):
        tools.call("harvestapi_get_company", {"url": "https://www.linkedin.com/in/jane-doe/"})
    with pytest.raises(ValueError):
        tools.call("harvestapi_get_company", {"url": "https://www.linkedin.com/company/x/", "search": "x"})


def test_people_search_sends_the_full_company_page_url() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["payload"])
        return httpx.Response(200, request=request, json={"result": {"data": {"elements": []}}})

    tools = _client(handle)
    tools.call("harvestapi_search_leads", {"currentCompanies": "linkedin.com/company/thunespayments",
                                           "currentJobTitles": "Operations Manager", "page": 1})
    assert seen[0]["currentCompanies"] == "https://www.linkedin.com/company/thunespayments/"


def test_profile_lookup_records_the_company_page_hint() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/free_simple_company_search/execute"):
            row = {"normalized_domain": "thunes.com", "domain": "thunes.com", "company_name": "Thunes",
                   "linkedin_url": "linkedin.com/company/thunespayments", "employee_count": 573}
            return httpx.Response(200, request=request, json={"result": {"data": {"rows": [row]}}})
        return httpx.Response(200, request=request, json={"result": {"data": {"results": []}}})

    tools = _client(handle)
    tools.call("get_company_profile", {"domain": "thunes.com"})
    assert tools.linkedin_hints.get("thunes.com") == "https://www.linkedin.com/company/thunespayments/"


def test_search_request_uses_country_names_and_the_page_url() -> None:
    icp = _icp(contact_geography={"countries": ["SG"], "regions": [], "cities": []})
    request = _search_request(icp, _company(company_linkedin="linkedin.com/company/thunespayments"))
    assert request["currentCompanies"] == "https://www.linkedin.com/company/thunespayments/"
    assert request["locations"] == "Singapore"
    assert "search" not in request


def test_page_scoped_search_accepts_a_trading_name_variant() -> None:
    expected = {"name": "thunes", "domain": "thunes.com", "linkedin_slug": "thunespayments"}
    assert _search_company_matches(expected, {"name": "thunes payments", "domain": "", "linkedin_slug": ""})
    assert not _search_company_matches(expected, {"name": "acme", "domain": "", "linkedin_slug": ""})
    unscoped = {"name": "thunes", "domain": "thunes.com", "linkedin_slug": ""}
    assert not _search_company_matches(unscoped, {"name": "thunes payments", "domain": "", "linkedin_slug": ""})


def test_employee_band_conflict_uses_the_provider_range() -> None:
    assert _employee_band_conflict({"employeeCountRange": {"start": 501, "end": 1000}}, _icp()) == (
        "LinkedIn employee band 501-1,000 is outside the ICP")
    assert _employee_band_conflict({"employeeCountRange": {"start": 51, "end": 200}}, _icp()) == ""
    assert _employee_band_conflict({}, _icp()) == ""
    assert _employee_band_conflict({"employeeCountRange": {"start": 5001}}, _icp(employee_count=[])) == ""


def _search_hit(name: str, title: str):
    return {"id": name.lower(), "linkedinUrl": f"https://www.linkedin.com/in/{name.lower()}/",
            "firstName": name, "lastName": "Lee",
            "currentPositions": [{"companyName": "Thunes", "title": title, "current": True}],
            "location": {"linkedinText": "Singapore"}}


def _profile(name: str, title: str):
    return {"recordId": f"rec-{name.lower()}", "firstName": name, "lastName": "Lee",
            "linkedinUrl": f"https://www.linkedin.com/in/{name.lower()}/",
            "location": {"parsed": {"countryCode": "SG", "city": "Singapore"}},
            "emails": [{"email": f"{name.lower()}@thunes.com"}],
            "currentPosition": [{"companyName": "Thunes", "title": title, "current": True}]}


def test_enrich_uses_hints_prechecks_the_band_and_drops_nothing_by_itself() -> None:
    calls = []

    def provider(tool, arguments):
        calls.append((tool, arguments))
        if tool == "harvestapi_get_company":
            big = "big" in arguments["url"]
            return {"result": {"data": {"element": {
                "name": "Big" if big else "Thunes",
                "website": "https://big.example" if big else "https://www.thunes.com",
                "employeeCountRange": {"start": 501, "end": 1000} if big else {"start": 51, "end": 200}}}}}
        if tool == "harvestapi_search_leads":
            return {"result": {"data": {"elements": [_search_hit("Ann", "Senior Operations Manager"),
                                                     _search_hit("Bob", "Operations Manager")]}}}
        if tool == "harvestapi_get_profile":
            who = arguments["url"].rstrip("/").rsplit("/", 1)[-1].title()
            return {"result": {"data": {"element": _profile(who, "Operations Manager" if who == "Bob" else "Senior Operations Manager")}}}
        raise AssertionError(tool)

    hints = {"thunes.com": "https://www.linkedin.com/company/thunespayments/",
             "big.example": "https://www.linkedin.com/company/big/"}
    companies = [_company(), _company(company_name="Big", company_website="https://big.example/")]
    report = []
    out = enrich_contacts(_icp(), companies, provider, linkedin_hints=hints, report=report)
    assert [c["company_name"] for c in out] == ["Thunes", "Big"]
    assert out[0]["company_linkedin"] == ""  # hints never enter the output rows
    assert out[0]["contact"]["full_name"] == "Bob Lee"  # exact target title looked up first
    assert "contact" not in out[1]
    assert report == ["Big: LinkedIn employee band 501-1,000 is outside the ICP"]
    searched = [a for t, a in calls if t == "harvestapi_search_leads"]
    assert searched == [{"currentCompanies": "https://www.linkedin.com/company/thunespayments/",
                         "currentJobTitles": "Operations Manager,Plant Manager", "page": 1}]
    assert sum(1 for t, _ in calls if t == "harvestapi_get_profile") == 1


def test_enrich_falls_back_to_a_name_search_when_the_hint_is_another_company() -> None:
    calls = []

    def provider(tool, arguments):
        calls.append(tool)
        if tool == "harvestapi_get_company":
            return {"result": {"data": {"element": {"name": "Other Co", "website": "https://other.example"}}}}
        return {"result": {"data": {"elements": []}}}

    report = []
    enrich_contacts(_icp(), [_company()], provider,
                    linkedin_hints={"thunes.com": "https://www.linkedin.com/company/wrong/"}, report=report)
    assert calls[:2] == ["harvestapi_get_company", "harvestapi_search_leads"]
    assert report[0] == "Thunes: stored LinkedIn page did not match; searched by name"


# --- v19b: spend only where a verified contact is likely --------------------

from experiments.harness_bakeoff.prompt import build_prompt  # noqa: E402


def test_contact_rounds_skip_early_stage_icps(monkeypatch) -> None:
    monkeypatch.delenv("BAKEOFF_CONTACT_SKIP_EARLY_STAGES", raising=False)
    assert adapter._contact_round_skip_reason({"company_stage": "Seed"}).startswith("skipped: seed")
    assert adapter._contact_round_skip_reason({"company_stage": "Series A"})
    assert adapter._contact_round_skip_reason({"company_stage": "Series B"}) == ""
    assert adapter._contact_round_skip_reason({"company_stage": "Series C+"}) == ""
    assert adapter._contact_round_skip_reason({"company_stage": "Public"}) == ""
    monkeypatch.setenv("BAKEOFF_CONTACT_SKIP_STAGES", "seed,series b")
    assert adapter._contact_round_skip_reason({"company_stage": "Series B"})
    assert adapter._contact_round_skip_reason({"company_stage": "Seed"})
    monkeypatch.delenv("BAKEOFF_CONTACT_SKIP_STAGES")
    monkeypatch.setenv("BAKEOFF_CONTACT_SKIP_EARLY_STAGES", "0")
    assert adapter._contact_round_skip_reason({"company_stage": "Seed"}) == ""


def test_enrich_skips_unknown_pages_small_companies_and_fallback_searches() -> None:
    calls = []

    def provider(tool, arguments):
        calls.append(tool)
        if tool == "harvestapi_get_company":
            return {"result": {"data": {"element": {"name": "Thunes", "website": "https://www.thunes.com",
                                                    "employeeCount": 20,
                                                    "employeeCountRange": {"start": 11, "end": 50}}}}}
        return {"result": {"data": {"elements": [], "status": "OK"}}}

    report = []
    out = enrich_contacts(_icp(employee_count=["11-50", "51-200"]),
                          [_company(), _company(company_name="Nohint", company_website="https://nohint.example/")],
                          provider, linkedin_hints={"thunes.com": "https://www.linkedin.com/company/thunespayments/"},
                          report=report, fallback_search=False, require_page=True, min_employees=50)
    assert all("contact" not in c for c in out)
    assert calls == ["harvestapi_get_company"]  # no people search at 20 staff, none without a page
    assert report == ["Thunes: LinkedIn shows 20 employees; too few for the target titles",
                      "Nohint: no LinkedIn page known; contact search skipped"]
    # a band that allows nothing larger still gets its search, but no fallback
    calls.clear()
    enrich_contacts(_icp(employee_count=["2-10", "11-50"]), [_company()], provider,
                    linkedin_hints={"thunes.com": "https://www.linkedin.com/company/thunespayments/"},
                    fallback_search=False, require_page=True, min_employees=50)
    assert calls == ["harvestapi_get_company", "harvestapi_search_leads"]


def test_prompt_names_the_target_titles_when_contacts_are_scored() -> None:
    icp = {"prompt": "Need fintech operators.", "industry": "Fintech", "country": "United States",
           "company_stage": "Series B", "employee_count": ["51-200"], "max_companies": 5,
           "intent_signal": "raised a round", "intent_category": "FUNDING",
           "contact_policy": "contacts_v1", "target_roles": ["Payments Operations Manager"],
           "target_seniority": "", "contact_geography": {"countries": [], "regions": [], "cities": []}}
    text = build_prompt(icp)
    assert "Payments Operations Manager" in text and "- Contacts:" in text
    plain = {k: v for k, v in icp.items() if k not in ("contact_policy", "target_roles")}
    assert "- Contacts:" not in build_prompt(plain)


def test_enrich_resolves_a_missing_page_by_company_name() -> None:
    calls = []

    def provider(tool, arguments):
        calls.append((tool, dict(arguments)))
        if tool == "harvestapi_get_company":
            assert arguments == {"search": "Lowe's"}
            return {"result": {"data": {"element": {"name": "Lowe's Companies, Inc.", "website": "https://talent.lowes.com",
                                                    "linkedinUrl": "https://www.linkedin.com/company/lowe's-home-improvement/",
                                                    "employeeCount": 114311, "employeeCountRange": {"start": 10001}}}}}
        return {"result": {"data": {"elements": [], "status": "OK"}}}

    report = []
    enrich_contacts(_icp(employee_count=["10,001+"]),
                    [_company(company_name="Lowe's", company_website="https://www.lowes.com/")], provider,
                    report=report, fallback_search=False, require_page=True, min_employees=50, lookup_page_by_name=True)
    assert [t for t, _ in calls] == ["harvestapi_get_company", "harvestapi_search_leads"]  # record fetched once
    assert calls[1][1]["currentCompanies"] == "https://www.linkedin.com/company/lowe's-home-improvement/"
    assert report == ["Lowe's: LinkedIn page resolved by name"]


# --- v20: bind identity from the homepage like the judge ---------------------

from experiments.harness_bakeoff.contacts import bind_homepage_pages, homepage_linkedin_pages  # noqa: E402


def test_homepage_linkedin_links_mirror_the_judge_parser_with_offsets() -> None:
    from experiments.harness_bakeoff.contacts import homepage_linkedin_links
    html = ('<html><head><script type="application/ld+json">{"@type": "Organization", "sameAs": ["http://linkedin.com/company/dash0hq"]}</script></head>'
            '<body>' + "x" * 50_000 + '<a href="https://www.linkedin.com/company/dash0hq/">LinkedIn</a>'
            '<a href="https://www.linkedin.com/in/someone/">person</a><link href="https://linkedin.com/company/other/"></body></html>')
    links = homepage_linkedin_links(html)
    assert links[0][0] == "https://www.linkedin.com/company/dash0hq/" and links[0][1] < 100
    assert links[1][0] == "https://www.linkedin.com/company/other/" and links[1][1] > 50_000
    assert homepage_linkedin_pages("<html>no social links</html>") == []
    # a plain mention in text or a script is not a binding for the judge
    assert homepage_linkedin_pages('<script>var u="https://www.linkedin.com/company/x/"</script>') == []


def test_bind_homepage_pages_orders_companies_by_binding_reliability() -> None:
    def fetch(url):
        if "dash0" in url:
            return '<a href="https://www.linkedin.com/company/dash0hq/">x</a>'
        if "late" in url:
            return "y" * 100_000 + '<a href="https://www.linkedin.com/company/late-co/">x</a>'
        if "burlington" in url:
            return "<html>store finder</html>"
        raise RuntimeError("HTTP 403")

    companies = [_company(company_name="Burlington", company_website="https://www.burlington.com/"),
                 _company(company_name="Late", company_website="https://late.example/"),
                 _company(company_name="Blocked", company_website="https://blocked.example/"),
                 _company(company_name="Dash0", company_website="https://dash0.com/")]
    report = []
    kept, hints = bind_homepage_pages(companies, fetch, report=report)
    assert [c["company_name"] for c in kept] == ["Dash0", "Late", "Blocked", "Burlington"]
    assert hints == {"dash0.com": "https://www.linkedin.com/company/dash0hq/", "late.example": "https://www.linkedin.com/company/late-co/"}
    assert report[0].startswith("Burlington: homepage links no LinkedIn company page")
    assert "late in the page" in report[1] and "every time" in report[3]


def test_publish_homepage_linkedin_fills_only_one_early_homepage_page_in_order() -> None:
    from experiments.harness_bakeoff.contacts import publish_homepage_linkedin

    def fetch(url):
        if "dash0" in url:
            return '<a href="https://www.linkedin.com/company/dash0hq/">x</a>'
        if "late" in url:
            return "y" * 100_000 + '<a href="https://www.linkedin.com/company/late-co/">x</a>'
        if "two" in url:
            return ('<a href="https://www.linkedin.com/company/parent-group/">p</a>'
                    '<a href="https://www.linkedin.com/company/two-co/">c</a>')
        raise RuntimeError("HTTP 403")

    companies = [_company(company_name="Two", company_website="https://two.example/"),
                 _company(company_name="Blocked", company_website="https://blocked.example/"),
                 _company(company_name="Dash0", company_website="https://dash0.com/"),
                 _company(company_name="Late", company_website="https://late.example/")]
    report = []
    rows = publish_homepage_linkedin(companies, fetch, report=report)
    assert [c["company_name"] for c in rows] == ["Two", "Blocked", "Dash0", "Late"]
    assert [c.get("company_linkedin", "") for c in rows] == ["", "", "https://www.linkedin.com/company/dash0hq", ""]
    assert "2 LinkedIn company pages" in report[0] and "homepage unread" in report[1]


def test_homepage_fetch_uses_the_generic_http_route() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)["payload"]))
        return httpx.Response(200, request=request, json={"result": {"data": '<a href="https://linkedin.com/company/acme/">a</a>'}})

    tools = _client(handle)
    html = tools.call("fetch_homepage_html", {"url": "http://acme.example/"})
    assert seen == [("/api/v2/integrations/generic_http_request/execute", {"url": "https://acme.example/", "method": "GET"})]
    assert homepage_linkedin_pages(html) == ["https://www.linkedin.com/company/acme/"]
    assert tools.deepline_calls == 1


# --- v21: cite the verified LinkedIn page and its official name --------------

from experiments.harness_bakeoff.contacts import cite_company_records  # noqa: E402


def test_enrich_hands_back_trusted_records_and_cite_uses_them() -> None:
    def provider(tool, arguments):
        if tool == "harvestapi_get_company":
            return {"result": {"data": {"element": {"name": "DBS Bank", "website": "https://www.dbs.com",
                                                    "linkedinUrl": "https://www.linkedin.com/company/dbs-bank/",
                                                    "employeeCount": 40000, "employeeCountRange": {"start": 10001}}}}}
        return {"result": {"data": {"elements": [], "status": "OK"}}}

    resolved = {}
    company = _company(company_name="DBS", company_website="https://www.dbs.com/",
                       fit_evidence_urls=["https://www.dbs.com/newsroom/x", "https://linkedin.com/company/dbs-bank"])
    enrich_contacts(_icp(employee_count=["10,001+"]), [company], provider,
                    linkedin_hints={"dbs.com": "https://www.linkedin.com/company/dbs-bank/"},
                    fallback_search=False, require_page=True, min_employees=50, resolved=resolved)
    assert resolved["dbs.com"]["name"] == "DBS Bank"
    report = []
    cited = cite_company_records([company], resolved, report=report)
    assert cited[0]["company_name"] == "DBS Bank"
    assert cited[0]["fit_evidence_urls"] == ["https://www.linkedin.com/company/dbs-bank", "https://www.dbs.com/newsroom/x"]
    assert report == ["DBS: named 'DBS Bank' as LinkedIn does"]
    # a name that is not a shortening of the official one is left alone
    other = cite_company_records([_company(company_name="Acme", company_website="https://www.dbs.com/")], resolved)
    assert other[0]["company_name"] == "Acme"


# --- v22: validate the email before choosing a contact -----------------------


def test_email_validation_dispatches_and_rejects_bad_arguments() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)["payload"]))
        return httpx.Response(200, request=request, json={"result": {"data": {"status": "valid"}}})

    tools = _client(handle)
    tools.call("zerobounce_validate", {"email": "jane@acme.example"})
    assert seen == [("/api/v2/integrations/zerobounce_validate/execute", {"email": "jane@acme.example"})]
    with pytest.raises(ValueError):
        tools.call("zerobounce_validate", {"email": "not-an-email"})
    with pytest.raises(ValueError):
        tools.call("zerobounce_validate", {"email": "jane@acme.example", "ip_address": "1.1.1.1"})


def test_enrich_moves_to_the_next_candidate_when_the_email_is_invalid() -> None:
    calls = []

    def provider(tool, arguments):
        calls.append(tool)
        if tool == "harvestapi_get_company":
            return {"result": {"data": {"element": {"name": "Thunes", "website": "https://www.thunes.com",
                                                    "employeeCount": 500, "employeeCountRange": {"start": 201, "end": 500}}}}}
        if tool == "harvestapi_search_leads":
            return {"result": {"data": {"elements": [_search_hit("Ann", "Operations Manager"), _search_hit("Bob", "Operations Manager")]}}}
        if tool == "harvestapi_get_profile":
            who = arguments["url"].rstrip("/").rsplit("/", 1)[-1].title()
            return {"result": {"data": {"element": _profile(who, "Operations Manager")}}}
        if tool == "zerobounce_validate":
            return {"result": {"data": {"status": "invalid" if arguments["email"].startswith("ann") else "catch-all"}}}
        raise AssertionError(tool)

    report = []
    out = enrich_contacts(_icp(employee_count=["201-500"]), [_company()], provider,
                          linkedin_hints={"thunes.com": "https://www.linkedin.com/company/thunespayments/"},
                          report=report, fallback_search=False, require_page=True, validate_email=True)
    assert out[0]["contact"]["full_name"] == "Bob Lee"
    assert calls.count("zerobounce_validate") == 2 and calls.count("harvestapi_get_profile") == 2
    assert report == ["Thunes: Ann Lee: email not valid or catch-all; next candidate"]


def test_prompt_orders_fit_evidence_urls_by_stage_proof() -> None:
    icp = {"prompt": "Need fintech operators.", "industry": "Fintech", "country": "United States",
           "company_stage": "Series B", "employee_count": ["51-200"], "max_companies": 5,
           "intent_signal": "raised a round", "intent_category": "FUNDING"}
    assert "fit_evidence_urls, in this order" in build_prompt(icp)


# --- v23: the company record replaces the stored-profile query ---------------


def _record_client(handle):
    tools = _client(handle)
    tools.structured_profile = True
    return tools


def test_structured_profile_lookup_uses_exa_and_the_company_record() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/", 2)[1])
        if request.url.path.endswith("/exa_search/execute"):
            return httpx.Response(200, request=request, json={"result": {"data": {"results": [
                {"url": "https://www.linkedin.com/company/thunespayments/", "title": "Thunes | LinkedIn"}]}}})
        if request.url.path.endswith("/harvestapi_get_company/execute"):
            return httpx.Response(200, request=request, json={"result": {"data": {"element": {
                "name": "Thunes", "universalName": "thunespayments", "website": "http://www.thunes.com",
                "linkedinUrl": "https://www.linkedin.com/company/thunespayments/", "employeeCount": 573,
                "employeeCountRange": {"start": 501, "end": 1000}, "industries": ["Financial Services"],
                "foundedOn": {"year": 2016}, "locations": [{"city": "Singapore", "headquarter": True,
                "parsed": {"text": "Singapore, Singapore", "country": "Singapore"}}]}}}})
        raise AssertionError(request.url.path)

    tools = _record_client(handle)
    profile = tools.get_company_profile({"domain": "thunes.com"})
    assert seen == ["exa_search", "harvestapi_get_company"]
    assert profile["company"]["company_name"] == "Thunes"
    assert profile["company"]["employee_count_estimate"] == 573 and "employee_count" not in profile["company"]
    assert profile["company"]["linkedin_url"] == "https://www.linkedin.com/company/thunespayments/"
    assert profile["linkedin_profile_evidence"]["employee_count"] == "501-1,000"
    assert profile["linkedin_profile_evidence"]["listed_headquarters"] == "Singapore, Singapore"
    assert tools.linkedin_hints["thunes.com"] == "https://www.linkedin.com/company/thunespayments/"
    assert tools.company_records["thunes.com"]["employeeCount"] == 573
    assert profile["errors"] == []


def test_structured_profile_falls_back_when_the_record_is_another_company() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/", 2)[1])
        if request.url.path.endswith("/exa_search/execute"):
            return httpx.Response(200, request=request, json={"result": {"data": {"results": [
                {"url": "https://www.linkedin.com/company/other-co/"}]}}})
        if request.url.path.endswith("/harvestapi_get_company/execute"):
            return httpx.Response(200, request=request, json={"result": {"data": {"element": {
                "name": "Other Co", "website": "https://other.example", "linkedinUrl": "https://www.linkedin.com/company/other-co/"}}}})
        if request.url.path.endswith("/free_simple_company_search/execute"):
            return httpx.Response(200, request=request, json={"result": {"data": {"rows": [
                {"domain": "thunes.com", "company_name": "Thunes", "employee_count": 573}]}}})
        return httpx.Response(200, request=request, json={"result": {"data": {"results": []}}})

    tools = _record_client(handle)
    profile = tools.get_company_profile({"domain": "thunes.com"})
    assert seen[:3] == ["exa_search", "harvestapi_get_company", "free_simple_company_search"]
    assert profile["company"]["company_name"] == "Thunes"
    assert "thunes.com" not in tools.company_records


# --- v24: stop buying profiles at a company that yields no emails -------------


def test_enrich_stops_after_two_emailless_profiles_at_one_company() -> None:
    calls = []

    def provider(tool, arguments):
        calls.append(tool)
        if tool == "harvestapi_get_company":
            return {"result": {"data": {"element": {"name": "Thunes", "website": "https://www.thunes.com",
                                                    "employeeCount": 500, "employeeCountRange": {"start": 201, "end": 500}}}}}
        if tool == "harvestapi_search_leads":
            return {"result": {"data": {"elements": [_search_hit(n, "Operations Manager") for n in ("Ann", "Bob", "Cal", "Dee")]}}}
        if tool == "harvestapi_get_profile":
            who = arguments["url"].rstrip("/").rsplit("/", 1)[-1].title()
            profile = _profile(who, "Operations Manager"); profile["emails"] = []
            return {"result": {"data": {"element": profile}}}
        raise AssertionError(tool)

    report = []
    out = enrich_contacts(_icp(employee_count=["201-500"]), [_company()], provider,
                          linkedin_hints={"thunes.com": "https://www.linkedin.com/company/thunespayments/"},
                          report=report, fallback_search=False, require_page=True)
    assert "contact" not in out[0]
    assert calls.count("harvestapi_get_profile") == 2
    assert report == ["Thunes: no emails at this company; stopped buying profiles"]


# --- v25: mirror the judge's hard title rejections ---------------------------

from experiments.harness_bakeoff.contacts import _role_matches  # noqa: E402


def test_role_match_rejects_assistant_and_former_titles_like_the_judge() -> None:
    targets = ["Facilities Manager", "Portfolio Manager"]
    assert _role_matches("Regional Facilities Manager", targets, "")
    assert not _role_matches("Assistant Portfolio Manager", targets, "")
    assert not _role_matches("Former Facilities Manager", targets, "")
    assert not _role_matches("Ex Portfolio Manager", targets, "")


# --- v26: cite the investor page for listed companies ------------------------

from experiments.harness_bakeoff.contacts import add_investor_relations_hints  # noqa: E402


def test_public_companies_get_their_investor_page_as_the_second_hint() -> None:
    queries = []

    def search(query):
        queries.append(query["query"])
        return {"results": [{"url": "https://www.nasdaq.com/market-activity/stocks/cpt", "title": "CPT"},
                            {"url": "https://investors.camdenliving.com/", "title": "Camden Property Trust - Investors"}]}

    public = _company(company_name="Camden Property Trust", company_website="https://www.camdenliving.com/",
                      company_stage="Public", fit_evidence_urls=["https://www.linkedin.com/company/camden-property-trust",
                                                                 "https://news.example/camden"])
    private = _company(company_name="Thunes", company_stage="Series B", fit_evidence_urls=["https://news.example/t"])
    report = []
    out = add_investor_relations_hints([public, private], search, report=report)
    assert queries == ["Camden Property Trust investor relations"]
    assert out[0]["fit_evidence_urls"] == ["https://www.linkedin.com/company/camden-property-trust",
                                           "https://investors.camdenliving.com/", "https://news.example/camden"]
    assert out[1]["fit_evidence_urls"] == ["https://news.example/t"]
    assert report == ["Camden Property Trust: investor page cited for the listing"]


# --- v27: alias slugs never veto a matching website ---------------------------

from experiments.harness_bakeoff.contacts import _company_matches  # noqa: E402


def test_company_match_prefers_the_website_over_an_alias_slug() -> None:
    expected = {"name": "academy sports outdoors", "domain": "academy.com", "linkedin_slug": "academy sports and outdoor"}
    same_site = {"name": "academy sports outdoors", "domain": "academy.com", "linkedin_slug": "academy sports and outdoors"}
    assert _company_matches(expected, same_site)
    other_site = {"name": "academy sports outdoors", "domain": "other.example", "linkedin_slug": "academy sports and outdoor"}
    assert not _company_matches(expected, other_site)
    no_domain_alias = {"name": "academy sports outdoors", "domain": "", "linkedin_slug": "academy sports and outdoors"}
    assert _company_matches(expected, no_domain_alias)
    no_domain_other = {"name": "other co", "domain": "", "linkedin_slug": "other-co"}
    assert not _company_matches(expected, no_domain_other)
    assert _search_company_matches(expected, {"name": "academy sports outdoors", "domain": "", "linkedin_slug": "academy sports and outdoors"})


# --- v30: the intent-details schema carries none of the v4 evidence fields ----

from experiments.harness_bakeoff import evidence as _evidence  # noqa: E402
from experiments.harness_bakeoff.models import validate_companies  # noqa: E402


def _v5_company(**overrides):
    company = {
        "company_name": "Acme", "company_website": "https://acme.example/", "industry": "Software",
        "employee_count": "51-200", "company_stage": "Public", "country": "United States",
        "intent_details": ("Acme launched its workflow platform on August 20, 2026, which could create "
                           "implementation work as customers adopt it, connecting Acme to this ICP."),
        "intent_signals": [{"matched_icp_signal": 0, "description": "Acme launched its workflow platform.",
                            "url": "https://acme.example/news/launch", "date": "2026-08-20"}],
    }
    company.update(overrides)
    return company


def test_intent_details_output_survives_the_evidence_and_citation_passes() -> None:
    page = ("Acme launched its workflow platform on August 20, 2026. " * 20) + "Acme Inc is a software company."
    icp = {"prompt": "workflow software", "industry": "Software", "country": "United States",
           "company_stage": "Public", "employee_count": ["51-200"], "max_companies": 5,
           "intent_signal": "launched a product", "intent_category": "PRODUCT_LAUNCH",
           "intent_max_age_days": 365, "intent_details_policy": "intent_details_v1"}
    verified = _evidence.verify_companies(icp, [_v5_company()], lambda url: page,
                                          seconds_left=lambda: 200.0, min_seconds=5.0, report=[])
    assert verified and "snippet" not in verified[0]["intent_signals"][0]

    # "Acme Corporation" normalizes to "acme" (a legal suffix), so a real
    # rename needs a name the submitted one only prefixes.
    records = {"acme.example": {"name": "Acme Analytics", "website": "https://acme.example",
                                "linkedinUrl": "https://www.linkedin.com/company/acme/"}}
    cited = cite_company_records(verified, records, report=[])
    assert "fit_evidence_urls" not in cited[0]
    assert cited[0]["company_name"] == "Acme Analytics"  # official name still applies

    searched = []
    hinted = add_investor_relations_hints(cited, lambda q: searched.append(q) or {"results": []}, report=[])
    assert searched == []  # no evidence URLs to hint, so no paid search
    assert "fit_evidence_urls" not in hinted[0]

    validate_companies(hinted, 5, allow_contacts=True, intent_details_policy="intent_details_v1")


def test_v4_output_keeps_its_snippet_and_evidence_urls() -> None:
    page = ("Acme launched its workflow platform on August 20, 2026. " * 20) + "Acme Inc is a software company."
    icp = {"prompt": "workflow software", "industry": "Software", "country": "United States",
           "company_stage": "Public", "employee_count": ["51-200"], "max_companies": 5,
           "intent_signal": "launched a product", "intent_category": "PRODUCT_LAUNCH", "intent_max_age_days": 365}
    v4 = _v5_company(fit_summary="Acme fits the ICP.", fit_evidence_urls=["https://acme.example/about"])
    v4.pop("intent_details")
    v4["intent_signals"] = [{**v4["intent_signals"][0], "why_now": "fresh launch", "snippet": "Acme launched its workflow platform on August 20, 2026."}]
    verified = _evidence.verify_companies(icp, [v4], lambda url: page, seconds_left=lambda: 200.0, min_seconds=5.0, report=[])
    assert verified and verified[0]["intent_signals"][0]["snippet"]
    records = {"acme.example": {"name": "Acme Corporation", "website": "https://acme.example",
                                "linkedinUrl": "https://www.linkedin.com/company/acme/"}}
    cited = cite_company_records(verified, records, report=[])
    assert cited[0]["fit_evidence_urls"][0] == "https://www.linkedin.com/company/acme"
    validate_companies(cited, 5, intent_details_policy=None)


# --- v31: no unverified employee band fallback --------------------------------


def test_prompt_never_turns_an_estimate_into_a_band() -> None:
    icp = {"prompt": "Need fintech operators.", "industry": "Fintech", "country": "United States",
           "company_stage": "Public", "employee_count": ["201-500", "501-1,000"], "max_companies": 5,
           "intent_signal": "raised a round", "intent_category": "FUNDING"}
    text = build_prompt(icp)
    assert "nearest the profile" not in text
    assert "an unconfirmed listed band costs nothing" not in text
    assert "If neither (a) nor (b) supports a listed band, omit the company." in text


# --- v49: the judge's BounceBan fallback -------------------------------------


def _bounceban_provider(zb_status, bounce_payload, calls):
    """Provider stub whose ZeroBounce verdict and BounceBan reply are pinned."""

    def provider(tool, arguments):
        calls.append(tool)
        if tool == "harvestapi_get_company":
            return {"result": {"data": {"element": {"name": "Thunes", "website": "https://www.thunes.com",
                                                    "employeeCount": 500,
                                                    "employeeCountRange": {"start": 201, "end": 500}}}}}
        if tool == "harvestapi_search_leads":
            return {"result": {"data": {"elements": [_search_hit("Ann", "Operations Manager"),
                                                     _search_hit("Bob", "Operations Manager")]}}}
        if tool == "harvestapi_get_profile":
            who = arguments["url"].rstrip("/").rsplit("/", 1)[-1].title()
            return {"result": {"data": {"element": _profile(who, "Operations Manager")}}}
        if tool == "zerobounce_validate":
            status = (
                zb_status.get(arguments["email"].split("@")[0].split(".")[0], "unknown")
                if isinstance(zb_status, dict)
                else zb_status
            )
            return {"result": {"data": {"status": status}}}
        if tool == "bounceban_verify_single":
            return {"result": {"data": bounce_payload}}
        raise AssertionError(tool)

    return provider


def _run_bounceban(zb_status, bounce_payload, calls):
    return enrich_contacts(
        _icp(employee_count=["201-500"]), [_company()],
        _bounceban_provider(zb_status, bounce_payload, calls),
        linkedin_hints={"thunes.com": "https://www.linkedin.com/company/thunespayments/"},
        report=[], fallback_search=False, require_page=True, validate_email=True,
    )


def test_bounceban_rescues_a_contact_zerobounce_left_unresolved() -> None:
    calls: list[str] = []
    out = _run_bounceban("unknown", {"status": "success", "result": "deliverable"}, calls)
    # The judge would accept this address, so the agent must not discard it.
    assert out[0]["contact"]["full_name"] == "Ann Lee"
    assert "bounceban_verify_single" in calls


def test_bounceban_is_skipped_for_a_hard_invalid_email() -> None:
    calls: list[str] = []
    out = _run_bounceban(
        {"ann": "invalid", "bob": "catch-all"},
        {"status": "success", "result": "deliverable"},
        calls,
    )
    # `invalid` is the judge's hard reject: no fallback, move to the next candidate.
    assert out[0]["contact"]["full_name"] == "Bob Lee"
    assert "bounceban_verify_single" not in calls


def test_bounceban_undeliverable_does_not_keep_the_contact() -> None:
    calls: list[str] = []
    out = _run_bounceban("unknown", {"status": "success", "result": "undeliverable"}, calls)
    assert not out or not out[0].get("contact")


def test_bounceban_fallback_is_bounded_per_company() -> None:
    calls: list[str] = []
    _run_bounceban("unknown", {"status": "success", "result": "undeliverable"}, calls)
    # Bounded so the extra paid calls cannot push a qualifying ICP over its cap.
    assert calls.count("bounceban_verify_single") <= 2
