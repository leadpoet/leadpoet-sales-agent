"""Deterministic contact enrichment for opted-in Arena rounds."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from experiments.harness_bakeoff.models import ContactResult


CONTACT_POLICY = "contacts_v1"
# About a third of profile lookups return an email, so where a company has
# several role-matched candidates the pass keeps buying profiles.
_PROFILE_LIMIT_PER_COMPANY = 4
# Email availability is a property of the company (its address pattern is
# either known to the provider or not), so after this many profiles without
# any email the remaining lookups there are not worth their price.
_EMAILLESS_PROFILE_LIMIT = 2
_CONTACT_WORKERS = 3
# HarvestAPI location filters take place names; ISO codes return nothing.
_COUNTRY_NAMES = {
    "AE": "United Arab Emirates",
    "AU": "Australia",
    "CA": "Canada",
    "DE": "Germany",
    "FR": "France",
    "GB": "United Kingdom",
    "IE": "Ireland",
    "IN": "India",
    "NZ": "New Zealand",
    "SG": "Singapore",
    "US": "United States",
}
# LinkedIn employee buckets by the start of the provider's employeeCountRange.
_EMPLOYEE_BUCKETS = (
    (10_001, "10,001+"),
    (5_001, "5,001-10,000"),
    (1_001, "1,001-5,000"),
    (501, "501-1,000"),
    (201, "201-500"),
    (51, "51-200"),
    (11, "11-50"),
    (2, "2-10"),
    (0, "0-1"),
)
_GENERIC_MAILBOXES = frozenset(
    {
        "admin",
        "billing",
        "careers",
        "contact",
        "customerservice",
        "hello",
        "help",
        "hr",
        "info",
        "jobs",
        "legal",
        "marketing",
        "office",
        "privacy",
        "recruiting",
        "sales",
        "security",
        "support",
        "team",
    }
)
_TITLE_EXPANSIONS = {
    "ceo": "chief executive officer",
    "cfo": "chief financial officer",
    "cio": "chief information officer",
    "cmo": "chief marketing officer",
    "coo": "chief operating officer",
    "cro": "chief revenue officer",
    "cto": "chief technology officer",
    "evp": "executive vice president",
    "svp": "senior vice president",
    "vp": "vice president",
}
_SENIORITY_BOILERPLATE = {
    "c_level": frozenset(
        {
            "chief",
            "executive",
            "founder",
            "managing",
            "officer",
            "owner",
            "partner",
            "president",
        }
    ),
    "vp": frozenset({"executive", "president", "senior", "vice"}),
    "head": frozenset({"head"}),
    "director": frozenset({"director", "executive", "managing", "senior"}),
    "manager": frozenset({"manager", "senior"}),
}
_LEGAL_SUFFIXES = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "gmbh",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "plc",
    }
)
_COUNTRY_ALIASES = {
    "australia": "AU",
    "canada": "CA",
    "france": "FR",
    "germany": "DE",
    "great britain": "GB",
    "india": "IN",
    "ireland": "IE",
    "new zealand": "NZ",
    "singapore": "SG",
    "u k": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "united states": "US",
    "united states of america": "US",
    "u s": "US",
    "u s a": "US",
    "usa": "US",
}
_US_REGION_NAMES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AS": "American Samoa",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "GU": "Guam",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "MP": "Northern Mariana Islands",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "PR": "Puerto Rico",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "UM": "United States Minor Outlying Islands",
    "VT": "Vermont",
    "VA": "Virginia",
    "VI": "United States Virgin Islands",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


ProviderCall = Callable[[str, dict[str, Any]], Any]


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _norm(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", _text(value))
    letters = "".join(
        character for character in raw if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[\W_]+", " ", letters.casefold()).split())


_US_REGION_CODES = {_norm(name): code for code, name in _US_REGION_NAMES.items()}
_US_REGION_CODES["washington dc"] = "DC"


def _bounded_strings(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item)
        identity = text.casefold()
        if text and identity not in seen:
            seen.add(identity)
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _canonical_linkedin_profile(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host not in {"linkedin.com", "www.linkedin.com"}
        or len(parts) != 2
        or parts[0].casefold() != "in"
        or not parts[1]
    ):
        return ""
    return urlunsplit(("https", "www.linkedin.com", f"/in/{parts[1]}/", "", ""))


def _linkedin_company_slug(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host not in {"linkedin.com", "www.linkedin.com"}
        or len(parts) != 2
        or parts[0].casefold() != "company"
    ):
        return ""
    return _norm(parts[1])


def _canonical_linkedin_company_url(value: Any) -> str:
    """Return the full company page URL HarvestAPI filters accept, else ""."""

    raw = _text(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host not in {"linkedin.com", "www.linkedin.com"}
        or len(parts) != 2
        or parts[0].casefold() != "company"
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._%+'-]{0,99}", parts[1])
    ):
        return ""
    return f"https://www.linkedin.com/company/{parts[1]}/"


def _employee_bucket(record: Mapping[str, Any]) -> str:
    """Map a provider employeeCountRange to LinkedIn's bucket label, else ""."""

    span = record.get("employeeCountRange")
    start = span.get("start") if isinstance(span, Mapping) else None
    if not isinstance(start, int) or isinstance(start, bool):
        count = record.get("employeeCount")
        start = count if isinstance(count, int) and not isinstance(count, bool) else None
    if start is None or start < 0:
        return ""
    for floor, label in _EMPLOYEE_BUCKETS:
        if start >= floor:
            return label
    return ""


def _bucket_key(value: Any) -> str:
    return _text(value).replace(",", "").replace(" ", "").casefold()


def _employee_band_conflict(record: Mapping[str, Any], icp: Mapping[str, Any]) -> str:
    """Name the ICP band conflict a LinkedIn company record proves, else ""."""

    wanted = {_bucket_key(item) for item in _bounded_strings(icp.get("employee_count"), limit=20)}
    bucket = _employee_bucket(record)
    if not wanted or not bucket or _bucket_key(bucket) in wanted:
        return ""
    return f"LinkedIn employee band {bucket} is outside the ICP"


def _band_allows_larger(icp: Mapping[str, Any], min_employees: int) -> bool:
    """True when the ICP accepts a company of at least ``min_employees`` staff."""

    for item in _bounded_strings(icp.get("employee_count"), limit=20):
        lower = _bucket_key(item).split("-")[0].rstrip("+")
        if lower.isdecimal() and int(lower) >= min_employees:
            return True
    return False


_HOMEPAGE_FETCH_LIMIT = 5
_HOMEPAGE_WORKERS = 3
# The judge parses whatever part of the homepage its first socket read
# returned (about 40-230 KB in measurements), so a LinkedIn link this early
# in the page is found every time and a later one only sometimes.
_RELIABLE_BINDING_OFFSET = 40_000


class _HomepageLinkParser(HTMLParser):
    """The judge's homepage identity parser: a/link hrefs and JSON-LD sameAs."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, int]] = []
        self._json_ld: list[str] | None = None
        self._json_ld_offset = 0
        self._line_starts = [0]
        for index, character in enumerate(text):
            if character == "\n":
                self._line_starts.append(index + 1)

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[min(line, len(self._line_starts)) - 1] + column

    def handle_starttag(self, tag: str, attrs) -> None:
        name = tag.casefold()
        attributes = {str(k or "").casefold(): str(v or "").strip() for k, v in attrs}
        if name in {"a", "link"}:
            page = _canonical_linkedin_company_url(attributes.get("href", ""))
            if page:
                self.found.append((page, self._offset()))
        if name == "script" and attributes.get("type", "").split(";", 1)[0].casefold() == "application/ld+json":
            self._json_ld = []
            self._json_ld_offset = self._offset()

    def handle_data(self, data: str) -> None:
        if self._json_ld is not None:
            self._json_ld.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._json_ld is not None:
            try:
                document = json.loads("".join(self._json_ld))
            except ValueError:
                document = None
            self._json_ld = None
            if document is not None:
                for page in _organization_same_as(document):
                    self.found.append((page, self._json_ld_offset))


def _organization_same_as(node: Any) -> list[str]:
    pages: list[str] = []
    if isinstance(node, dict):
        raw_type = node.get("@type", "")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if any(str(item or "").casefold() == "organization" for item in types):
            same_as = node.get("sameAs", [])
            for candidate in same_as if isinstance(same_as, list) else [same_as]:
                page = _canonical_linkedin_company_url(candidate)
                if page:
                    pages.append(page)
        for child in node.values():
            pages.extend(_organization_same_as(child))
    elif isinstance(node, list):
        for child in node:
            pages.extend(_organization_same_as(child))
    return pages


def homepage_linkedin_links(html: str) -> list[tuple[str, int]]:
    """LinkedIn company pages the judge's parser would find, with byte offsets."""

    text = str(html or "")[:2_000_000]
    parser = _HomepageLinkParser(text)
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed page binds nothing
        return []
    seen: dict[str, int] = {}
    for page, offset in parser.found:
        if page not in seen or offset < seen[page]:
            seen[page] = offset
    return sorted(seen.items(), key=lambda item: item[1])[:10]


def homepage_linkedin_pages(html: str) -> list[str]:
    """Distinct LinkedIn company pages linked from a homepage, earliest first."""

    return [page for page, _ in homepage_linkedin_links(html)]


def bind_homepage_pages(
    companies: Sequence[Mapping[str, Any]],
    fetch_html: Callable[[str], str],
    *,
    report: list[str] | None = None,
    limit: int = _HOMEPAGE_FETCH_LIMIT,
    workers: int = _HOMEPAGE_WORKERS,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Keep companies whose homepage links a LinkedIn company page.

    The judge proves a company's identity only from its homepage, and it
    needs that LinkedIn link to read the employee band it checks; a company
    without the link scores zero however good its contact is. Returns the kept
    rows and the page per website domain for the contact search. A homepage
    that cannot be fetched keeps its row: the judge's own fetch may succeed.
    """

    rows = [dict(company) for company in companies]

    def fetch(company: Mapping[str, Any]) -> tuple[str, list[tuple[str, int]] | None]:
        website = _text(company.get("company_website"))
        try:
            return "", homepage_linkedin_links(fetch_html(website))
        except Exception as exc:  # noqa: BLE001 - the caller keeps the row
            return f"{type(exc).__name__}", None

    checked = rows[:limit]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(checked) or 1))) as pool:
        outcomes = list(pool.map(fetch, checked))
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    hints: dict[str, str] = {}
    for position, (company, (error, links)) in enumerate(zip(checked, outcomes)):
        name = _text(company.get("company_name"))
        if links is None:
            ranked.append((2, position, company))
            if report is not None:
                report.append(f"{name}: homepage fetch failed ({error}); kept")
            continue
        if not links:
            ranked.append((3, position, company))
            if report is not None:
                report.append(f"{name}: homepage links no LinkedIn company page; last for contacts")
            continue
        page, offset = links[0]
        domain = _domain(company.get("company_website"))
        if domain:
            hints[domain] = page
        early = offset <= _RELIABLE_BINDING_OFFSET
        ranked.append((0 if early else 1, position, company))
        if report is not None:
            report.append(
                f"{name}: LinkedIn link at byte {offset} "
                + ("(the judge finds it every time)" if early else "(late in the page; the judge may miss it)")
            )
    ranked.sort(key=lambda item: (item[0], item[1]))
    kept = [company for _, _, company in ranked]
    kept.extend(rows[limit:])
    return kept, hints


def publish_homepage_linkedin(
    companies: Sequence[Mapping[str, Any]],
    fetch_html: Callable[[str], str],
    *,
    report: list[str] | None = None,
    limit: int = _HOMEPAGE_FETCH_LIMIT,
    workers: int = _HOMEPAGE_WORKERS,
) -> list[dict[str, Any]]:
    """Fill company_linkedin only from the company's own homepage, keeping order.

    The judge can reuse an exactly bound LinkedIn company profile as evidence,
    but a wrong page fails identity. So a row gets a page only when its live
    homepage links exactly one LinkedIn company page, early enough for the
    judge's partial read to find it. Anything else leaves the field empty.
    """

    rows = [dict(company) for company in companies]

    def fetch(company: Mapping[str, Any]) -> list[tuple[str, int]] | None:
        try:
            return homepage_linkedin_links(fetch_html(_text(company.get("company_website"))))
        except Exception:  # noqa: BLE001 - an unread homepage publishes nothing
            return None

    checked = rows[:limit]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(checked) or 1))) as pool:
        outcomes = list(pool.map(fetch, checked))
    for row, links in zip(checked, outcomes):
        name = _text(row.get("company_name"))
        pages = {_canonical_linkedin_company_url(page) for page, _ in (links or [])} - {""}
        if links is None or len(pages) != 1 or links[0][1] > _RELIABLE_BINDING_OFFSET:
            if report is not None:
                reason = ("homepage unread" if links is None
                          else f"{len(pages)} LinkedIn company pages" if len(pages) != 1
                          else "LinkedIn link late in the page")
                report.append(f"{name}: company_linkedin left empty ({reason})")
            continue
        row["company_linkedin"] = pages.pop().rstrip("/")
        if report is not None:
            report.append(f"{name}: company_linkedin from homepage {row['company_linkedin']}")
    return rows


def cite_company_records(
    companies: Sequence[Mapping[str, Any]],
    records: Mapping[str, Mapping[str, Any]],
    *,
    report: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Put each trusted LinkedIn page first among fit evidence and use its name.

    The judge reads the LinkedIn company page for the employee band when the
    output points it there, and it binds identity by exact normalized name, so
    an abbreviation ("DBS" for "DBS Bank") is a mismatch. The official name is
    used only when the submitted name is a shortening of it.
    """

    output: list[dict[str, Any]] = []
    for company in companies:
        row = dict(company)
        record = records.get(_domain(row.get("company_website")))
        if record:
            # Evidence URL hints exist only in the pre-intent-details schema.
            page = _canonical_linkedin_company_url(record.get("linkedinUrl"))
            if page and not _canonical_linkedin_company_url(row.get("company_linkedin")):
                # The judge binds identity and reads the employee band from the
                # company's LinkedIn page; publish the trusted record's page so
                # it can verify fit instead of marking it unproven.
                row["company_linkedin"] = page.rstrip("/")
            if page and "fit_evidence_urls" in row:
                urls = [
                    _text(url)
                    for url in (row.get("fit_evidence_urls") or [])
                    if _text(url) and _linkedin_company_slug(url) != _linkedin_company_slug(page)
                ]
                row["fit_evidence_urls"] = [page.rstrip("/"), *urls][:5]
            official = _text(record.get("name"))
            submitted = _company_name(row.get("company_name"))
            normalized_official = _company_name(official)
            if (
                official
                and submitted
                and submitted != normalized_official
                and normalized_official.startswith(submitted)
            ):
                if report is not None:
                    report.append(f"{row.get('company_name')}: named {official!r} as LinkedIn does")
                row["company_name"] = official
        output.append(row)
    return output


def add_investor_relations_hints(
    companies: Sequence[Mapping[str, Any]],
    search: Callable[[Mapping[str, Any]], Any],
    *,
    report: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Give the judge a listed company's investor page as its second lookup hint.

    The judge proves a Public stage only from a sentence naming the listing,
    and it reads just three evidence URLs. Listed REITs and banks failed with
    "stage unproven" behind a LinkedIn page and a news article; the investor
    relations page names the exchange and ticker.
    """

    output: list[dict[str, Any]] = []
    for company in companies:
        row = dict(company)
        output.append(row)
        if _norm(row.get("company_stage")) != "public" or "fit_evidence_urls" not in row:
            # The intent-details schema submits no evidence URLs, so there is
            # no hint to place and no reason to pay for the search.
            continue
        name = _text(row.get("company_name"))
        domain = _domain(row.get("company_website"))
        if not name or not domain:
            continue
        try:
            found = search({"query": f"{name} investor relations", "mode": "search", "limit": 5})
        except Exception as exc:  # noqa: BLE001 - a missing hint is not an error
            if report is not None:
                report.append(f"{name}: investor page search failed ({type(exc).__name__})")
            continue
        results = found.get("results") if isinstance(found, Mapping) else None
        chosen = ""
        for item in results if isinstance(results, list) else []:
            url = _text(item.get("url")) if isinstance(item, Mapping) else ""
            host = _domain(url)
            if not url or not host:
                continue
            same_company = host == domain or host.endswith("." + domain)
            if same_company and ("investor" in url.casefold() or host.startswith(("ir.", "investors."))):
                chosen = url
                break
            if same_company and not chosen and "investor" in _text(item.get("title")).casefold():
                chosen = url
        if not chosen:
            if report is not None:
                report.append(f"{name}: no investor page on its domain")
            continue
        urls = [_text(u) for u in (row.get("fit_evidence_urls") or []) if _text(u) and _text(u) != chosen]
        row["fit_evidence_urls"] = [*urls[:1], chosen, *urls[1:]][:5]
        if report is not None:
            report.append(f"{name}: investor page cited for the listing")
    return output


def _company_record(call_provider: ProviderCall, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """One LinkedIn company record, or {} when the provider has none."""

    unwrapped = _unwrap(call_provider("harvestapi_get_company", dict(arguments)))
    element = unwrapped.get("element") if isinstance(unwrapped, Mapping) else None
    return dict(element) if isinstance(element, Mapping) else {}


def _record_matches_company(record: Mapping[str, Any], company: Mapping[str, Any]) -> bool:
    """A stored LinkedIn hint is trusted only when its record names this company."""

    expected = _expected_company(company)
    observed_domain = _domain(record.get("website"))
    if expected["domain"] and observed_domain:
        return observed_domain == expected["domain"] or (
            observed_domain.endswith("." + expected["domain"])
            or expected["domain"].endswith("." + observed_domain)
        )
    return bool(expected["name"]) and expected["name"] == _company_name(record.get("name"))


def _domain(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlsplit(raw).hostname or "").casefold().rstrip(".")
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""
    host = host.removeprefix("www.")
    if "." not in host:
        return ""
    return host


def _company_name(value: Any) -> str:
    words = _norm(value).split()
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _us_region_code(value: Any) -> str:
    normalized = _norm(value)
    code = _US_REGION_CODES.get(normalized, "")
    if not code:
        parts = normalized.split()
        candidate = parts[-1].upper() if len(parts) in {1, 2} else ""
        if len(parts) == 2 and parts[0] != "us":
            candidate = ""
        code = candidate if candidate in _US_REGION_NAMES else ""
    return code


def _explicit_us_region_code(value: Any) -> str:
    parts = _norm(value).split()
    if len(parts) != 2 or parts[0] != "us":
        return ""
    code = parts[1].upper()
    return code if code in _US_REGION_NAMES else ""


def _harvestapi_region(value: Any, *, allow_bare_us_region: bool) -> str:
    code = _explicit_us_region_code(value)
    if not code and allow_bare_us_region:
        code = _us_region_code(value)
    return _US_REGION_NAMES.get(code, _text(value))


def _unwrap(value: Any, *, require_success: bool = False) -> Any:
    current = value
    for _ in range(8):
        if not isinstance(current, Mapping):
            return current
        if require_success:
            status = current.get("status")
            if (
                current.get("ok") is False
                or current.get("success") is False
                or bool(current.get("error"))
                or (
                    isinstance(status, str)
                    and status.strip().casefold()
                    in {"error", "failed", "failure"}
                )
            ):
                return None
        moved = False
        for key in (
            "toolResponse",
            "tool_response",
            "rawV2",
            "raw_v2",
            "raw",
            "result",
            "data",
            "output",
        ):
            child = current.get(key)
            if isinstance(child, (Mapping, list, tuple)) and child is not current:
                current = child
                moved = True
                break
        if not moved:
            return current
    return current


def _profiles(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 5:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result: list[Mapping[str, Any]] = []
        for item in list(value)[:25]:
            result.extend(_profiles(item, depth + 1))
        return result
    if not isinstance(value, Mapping):
        return []
    profile_keys = {
        "publicIdentifier",
        "public_identifier",
        "linkedinUrl",
        "linkedin_url",
        "firstName",
        "lastName",
        "currentPosition",
        "currentPositions",
        "experience",
    }
    result = [value] if profile_keys.intersection(value) else []
    for key in ("elements", "items", "profiles", "profile", "element", "results"):
        if key in value:
            result.extend(_profiles(value[key], depth + 1))
    return result


def _profile_linkedin(profile: Mapping[str, Any]) -> str:
    direct = _canonical_linkedin_profile(
        profile.get("linkedinUrl")
        or profile.get("linkedin_url")
        or profile.get("profileUrl")
        or profile.get("profile_url")
        or profile.get("url")
    )
    if direct:
        return direct
    identifier = _text(
        profile.get("publicIdentifier") or profile.get("public_identifier")
    )
    return (
        _canonical_linkedin_profile(f"linkedin.com/in/{identifier}")
        if identifier
        else ""
    )


def _profile_name(profile: Mapping[str, Any]) -> str:
    first = _text(profile.get("firstName") or profile.get("first_name"))
    last = _text(profile.get("lastName") or profile.get("last_name"))
    return f"{first} {last}".strip() or _text(
        profile.get("fullName") or profile.get("full_name") or profile.get("name")
    )


def _is_current(position: Mapping[str, Any]) -> bool:
    if position.get("current") is False or position.get("isCurrent") is False:
        return False
    if position.get("current") is True or position.get("isCurrent") is True:
        return True
    end = position.get("endDate", position.get("end_date"))
    if end in (None, ""):
        return True
    if isinstance(end, Mapping):
        if not any(end.values()):
            return True
        end = end.get("text")
    return _norm(end) in {"present", "current", "now"}


def _current_positions(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for key in ("currentPosition", "currentPositions", "current_position"):
        current = profile.get(key)
        if isinstance(current, Mapping) and _is_current(current):
            result.append(current)
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            result.extend(
                item
                for item in current[:10]
                if isinstance(item, Mapping) and _is_current(item)
            )
    experience = profile.get("experience") or profile.get("experiences") or []
    if isinstance(experience, Sequence) and not isinstance(
        experience, (str, bytes, bytearray)
    ):
        result.extend(
            item
            for item in experience[:25]
            if isinstance(item, Mapping) and _is_current(item)
        )
    return result


def _position_title(position: Mapping[str, Any]) -> str:
    return _text(
        position.get("title")
        or position.get("position")
        or position.get("role")
        or position.get("jobTitle")
    )


def _position_company(position: Mapping[str, Any]) -> dict[str, str]:
    nested = position.get("company")
    company = nested if isinstance(nested, Mapping) else {}
    return {
        "name": _company_name(
            position.get("companyName")
            or position.get("company_name")
            or position.get("employerName")
            or company.get("name")
        ),
        "domain": _domain(
            position.get("companyDomain")
            or position.get("company_domain")
            or company.get("domain")
            or company.get("website")
        ),
        "linkedin_slug": _linkedin_company_slug(
            position.get("companyLinkedinUrl")
            or position.get("companyLinkedInUrl")
            or company.get("linkedinUrl")
            or company.get("linkedin_url")
        ),
    }


def _expected_company(company: Mapping[str, Any]) -> dict[str, str]:
    return {
        "name": _company_name(company.get("company_name")),
        "domain": _domain(company.get("company_website")),
        "linkedin_slug": _linkedin_company_slug(company.get("company_linkedin")),
    }


def _company_matches(expected: Mapping[str, str], observed: Mapping[str, str]) -> bool:
    name_matches = bool(
        expected.get("name") and expected["name"] == observed.get("name")
    )
    strong: list[bool] = []
    expected_domain = expected.get("domain", "")
    observed_domain = observed.get("domain", "")
    if expected_domain and observed_domain:
        strong.append(
            expected_domain == observed_domain
            or (
                name_matches
                and (
                    observed_domain.endswith("." + expected_domain)
                    or expected_domain.endswith("." + observed_domain)
                )
            )
        )
    if strong:
        # A matching website decides. LinkedIn keeps alias slugs for one page
        # ("academy-sports-and-outdoor" and "...-outdoors"), and the judge
        # compares domains and names, never the research hint's slug.
        return all(strong)
    expected_slug = expected.get("linkedin_slug", "")
    observed_slug = observed.get("linkedin_slug", "")
    if expected_slug and observed_slug and not (
        expected_slug.isdecimal() or observed_slug.isdecimal()
    ):
        return expected_slug == observed_slug or name_matches
    return name_matches


def _search_company_matches(
    expected: Mapping[str, str], observed: Mapping[str, str]
) -> bool:
    """Match Harvest search metadata whose company URL uses a numeric ID."""

    observed_slug = observed.get("linkedin_slug", "")
    expected_slug = expected.get("linkedin_slug", "")
    expected_name = expected.get("name", "")
    observed_name = observed.get("name", "")
    if expected_name and expected_name == observed_name:
        return True
    if observed_slug and not observed_slug.isdecimal() and expected_slug:
        return observed_slug == expected_slug
    if not expected_name or not observed_name:
        return False
    # A search scoped to the company page returns only its staff, so a
    # trading-name variant ("Thunes" vs "Thunes Payments") is the same company.
    return bool(expected_slug) and (
        expected_name in observed_name or observed_name in expected_name
    )


def _normalized_title(value: Any) -> str:
    expanded: list[str] = []
    for word in _norm(value).split():
        expanded.extend(_TITLE_EXPANSIONS.get(word, word).split())
    return " ".join(word for word in expanded if word not in {"and", "of", "the"})


def _seniority(value: Any) -> str:
    title = _normalized_title(value)
    if re.search(r"\bchief\b.*\bofficer\b", title):
        return "c_level"
    if (
        "managing partner" in title
        or "managing director" in title
        or re.search(r"\b(owner|founder)\b", title)
        or ("president" in title and "vice president" not in title)
    ):
        return "c_level"
    if "vice president" in title:
        return "vp"
    if "head" in title.split():
        return "head"
    if "director" in title:
        return "director"
    if "manager" in title:
        return "manager"
    return "other"


def _seniority_matches(title: str, requested: Any) -> bool:
    raw = _text(requested).casefold()
    target = _norm(requested)
    if not target:
        return True
    actual = _seniority(title)
    if ("+" in raw and target in {"vp", "vice president"}) or target in {
        "vp above",
        "vp and above",
        "vice president above",
        "vice president and above",
    }:
        return actual in {"vp", "c_level"}
    if ("+" in raw and target == "director") or target in {
        "director above",
        "director and above",
    }:
        return actual in {"director", "head", "vp", "c_level"}
    expected = {
        "c level": "c_level",
        "c suite": "c_level",
        "executive": "c_level",
        "vp": "vp",
        "vice president": "vp",
        "head": "head",
        "head of": "head",
        "director": "director",
        "manager": "manager",
    }.get(target)
    return actual == expected if expected else False


# The judge rejects these title markers before any model review.
_REJECTED_TITLE_MARKERS = frozenset(
    {"assistant", "former", "formerly", "retired", "previous", "previously", "ex"}
)


def _role_matches(title: str, targets: Sequence[str], requested_seniority: Any) -> bool:
    actual = _normalized_title(title)
    if not actual or not _seniority_matches(title, requested_seniority):
        return False
    if _REJECTED_TITLE_MARKERS & set(actual.split()):
        return False
    actual_words = actual.split()
    for target in targets:
        normalized = _normalized_title(target)
        if normalized == actual:
            return True
        target_words = normalized.split()
        width = len(target_words)
        if width < 2:
            continue
        # Allow modifiers such as "VP Software Engineering", while keeping
        # the title words in order. The independent scorer still judges fit.
        matched = 0
        for word in actual_words:
            if word == target_words[matched]:
                matched += 1
                if matched == width:
                    return True
    return False


def _functional_title(value: Any) -> str:
    """Keep the role function while removing recognized seniority boilerplate."""

    normalized = _normalized_title(value)
    boilerplate = _SENIORITY_BOILERPLATE.get(_seniority(value))
    if not normalized or boilerplate is None:
        return ""
    return " ".join(word for word in normalized.split() if word not in boilerplate)


def _profile_location(profile: Mapping[str, Any]) -> dict[str, str]:
    location = profile.get("location")
    location = location if isinstance(location, Mapping) else {}
    parsed = location.get("parsed")
    parsed = parsed if isinstance(parsed, Mapping) else {}
    country_code = _text(
        profile.get("countryCode")
        or profile.get("country_code")
        or location.get("countryCode")
        or location.get("country_code")
        or parsed.get("countryCode")
        or parsed.get("country_code")
    ).upper()
    direct_country = _text(
        profile.get("country")
        or location.get("country")
        or parsed.get("country")
        or parsed.get("countryFull")
        or parsed.get("country_full")
    )
    if not country_code and len(direct_country) == 2 and direct_country.isalpha():
        country_code = direct_country.upper()
    if not country_code:
        country_code = _COUNTRY_ALIASES.get(_norm(direct_country), "")
    if len(country_code) != 2 or not country_code.isalpha():
        country_code = ""
    return {
        "country": country_code,
        "country_full": _text(
            profile.get("countryName")
            or profile.get("country_name")
            or location.get("countryName")
            or location.get("country_name")
            or parsed.get("countryFull")
            or parsed.get("country_full")
            or (direct_country if len(direct_country) != 2 else "")
        ),
        "region": _text(
            profile.get("region")
            or profile.get("state")
            or location.get("region")
            or location.get("state")
            or parsed.get("state")
            or parsed.get("regionCode")
            or parsed.get("region_code")
        ),
        "city": _text(
            profile.get("city") or location.get("city") or parsed.get("city")
        ),
    }


def _location_matches(
    location: Mapping[str, str], geography: Mapping[str, Any]
) -> bool:
    if not location.get("country"):
        return False
    constraints = {
        "country": _bounded_strings(geography.get("countries"), limit=70),
        "region": _bounded_strings(geography.get("regions"), limit=70),
        "city": _bounded_strings(geography.get("cities"), limit=70),
    }
    if constraints["country"]:
        allowed_codes = {
            _COUNTRY_ALIASES.get(_norm(item), _text(item).upper())
            for item in constraints["country"]
            if len(_text(item)) == 2 or _norm(item) in _COUNTRY_ALIASES
        }
        allowed_names = {
            _norm(item) for item in constraints["country"] if len(_text(item)) != 2
        }
        if (
            location["country"].upper() not in allowed_codes
            and _norm(location.get("country_full")) not in allowed_names
        ):
            return False
    if constraints["region"]:
        actual_region = _norm(location.get("region"))
        exact_match = any(
            actual_region == _norm(item)
            and (
                not _explicit_us_region_code(item)
                or location["country"].upper() == "US"
            )
            for item in constraints["region"]
        )
        equivalent_us_region = False
        if location["country"].upper() == "US":
            actual_code = _us_region_code(location.get("region"))
            equivalent_us_region = bool(actual_code) and actual_code in {
                _us_region_code(item) for item in constraints["region"]
            }
        if not exact_match and not equivalent_us_region:
            return False
    if constraints["city"] and _norm(location.get("city")) not in {
        _norm(item) for item in constraints["city"]
    }:
        return False
    return True


def _emails(profile: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for key in (
        "workEmail",
        "work_email",
        "professionalEmail",
        "professional_email",
        "email",
    ):
        value = profile.get(key)
        if isinstance(value, str):
            result.append(value)
    collection = profile.get("emails") or profile.get("emailAddresses") or []
    if isinstance(collection, Sequence) and not isinstance(
        collection, (str, bytes, bytearray)
    ):
        for item in collection[:20]:
            value = (
                item.get("email") or item.get("value")
                if isinstance(item, Mapping)
                else item
            )
            if isinstance(value, str):
                result.append(value)
    clean: list[str] = []
    seen: set[str] = set()
    for value in result:
        email = _text(value).casefold()
        local = email.partition("@")[0]
        mailbox = local.replace(".", "").replace("_", "").replace("-", "")
        valid_local = (
            len(local) <= 64
            and not local.startswith(".")
            and not local.endswith(".")
            and ".." not in local
        )
        if (
            _EMAIL_RE.fullmatch(email)
            and valid_local
            and mailbox not in _GENERIC_MAILBOXES
            and email not in seen
        ):
            seen.add(email)
            clean.append(email)
    return clean


def _search_request(
    icp: Mapping[str, Any], company: Mapping[str, Any]
) -> dict[str, Any]:
    roles = _bounded_strings(icp.get("target_roles"), limit=70)
    request: dict[str, Any] = {
        "currentJobTitles": ",".join(roles),
        "page": 1,
    }
    company_linkedin = _canonical_linkedin_company_url(company.get("company_linkedin"))
    if company_linkedin:
        request["currentCompanies"] = company_linkedin
    else:
        company_name = _text(company.get("company_name"))
        request["search"] = _company_name(company_name) or company_name
    geography = icp.get("contact_geography")
    geography = geography if isinstance(geography, Mapping) else {}
    countries = _bounded_strings(geography.get("countries"), limit=70)
    allow_bare_us_region = bool(countries) and all(
        _COUNTRY_ALIASES.get(_norm(country), _text(country).upper()) == "US"
        for country in countries
    )
    regions = [
        _harvestapi_region(region, allow_bare_us_region=allow_bare_us_region)
        for region in _bounded_strings(geography.get("regions"), limit=70)
    ]
    locations = (
        _bounded_strings(geography.get("cities"), limit=70)
        or list(dict.fromkeys(regions))
        or [
            _COUNTRY_NAMES.get(
                _COUNTRY_ALIASES.get(_norm(country), _text(country).upper()), country
            )
            for country in countries
        ]
    )
    if not locations:
        # When the ICP does not restrict contact geography, scope the people
        # search to the company's HQ country. The judge recognizes only a
        # limited set of countries, so a foreign contact (e.g. a US company's
        # Netherlands employee) is rejected anyway; prefer an in-country one.
        company_country = _text(company.get("country"))
        if company_country:
            locations = [
                _COUNTRY_NAMES.get(
                    _COUNTRY_ALIASES.get(_norm(company_country), company_country.upper()),
                    company_country,
                )
            ]
    if locations:
        request["locations"] = ",".join(locations)
    return request


def _fallback_search_request(
    icp: Mapping[str, Any], company: Mapping[str, Any]
) -> dict[str, Any] | None:
    request = _search_request(icp, company)
    roles: list[str] = []
    seen: set[str] = set()
    for target in _bounded_strings(icp.get("target_roles"), limit=70):
        role = _functional_title(target)
        if role and role not in seen:
            seen.add(role)
            roles.append(role)
    fallback_titles = ",".join(roles)
    if not fallback_titles or fallback_titles.casefold() == str(
        request["currentJobTitles"]
    ).casefold():
        return None
    request["currentJobTitles"] = fallback_titles
    return request


def _successful_empty_search(value: Any) -> bool:
    unwrapped = _unwrap(value, require_success=True)
    if not isinstance(unwrapped, Mapping):
        return False
    elements = unwrapped.get("elements")
    status = unwrapped.get("status")
    return (
        (
            (isinstance(status, str) and status.strip().casefold() == "ok")
            or (type(status) is int and status == 200)
        )
        and isinstance(elements, Sequence)
        and not isinstance(elements, (str, bytes, bytearray))
        and not elements
    )


def _contact_from_profile(
    profile: Mapping[str, Any],
    *,
    company: Mapping[str, Any],
    icp: Mapping[str, Any],
) -> dict[str, Any] | None:
    name = _profile_name(profile)
    linkedin = _profile_linkedin(profile)
    record_id = _text(
        profile.get("recordId") or profile.get("record_id") or profile.get("id")
    )
    email = next(iter(_emails(profile)), "")
    if not all((name, linkedin, record_id, email)):
        return None

    targets = _bounded_strings(icp.get("target_roles"), limit=70)
    expected = _expected_company(company)
    position = next(
        (
            item
            for item in _current_positions(profile)
            if _company_matches(expected, _position_company(item))
            and _role_matches(
                _position_title(item), targets, icp.get("target_seniority")
            )
        ),
        None,
    )
    if position is None:
        return None

    location = _profile_location(profile)
    geography = icp.get("contact_geography")
    geography = geography if isinstance(geography, Mapping) else {}
    if not _location_matches(location, geography):
        return None
    claim_location: dict[str, str] = {"country": location["country"]}
    for key in ("region", "city"):
        if location[key]:
            claim_location[key] = location[key]
    return {
        "full_name": name,
        "role": _position_title(position),
        "linkedin_url": linkedin,
        "location": claim_location,
        "email": email,
        "email_source": {
            "provider": "harvestapi",
            "tool": "harvestapi_get_profile",
            "record_id": record_id,
        },
    }


# The judge accepts a provider email only when its own ZeroBounce check says
# valid or catch-all, so the same check decides whether a candidate is kept.
_ACCEPTED_EMAIL_STATUSES = frozenset({"valid"})
_CATCH_ALL_EMAIL_STATUSES = frozenset(
    {"catch_all", "catch-all", "accept_all", "valid_accept_all", "ok_for_all"}
)


def _email_acceptable(validation: Any) -> bool:
    record = _unwrap(validation)
    if not isinstance(record, Mapping):
        return False
    status = _norm(record.get("status")).replace(" ", "_")
    if status in _ACCEPTED_EMAIL_STATUSES or status in _CATCH_ALL_EMAIL_STATUSES:
        return True
    flag = record.get("catchall_domain", record.get("catch_all_domain"))
    return flag is True or _norm(flag) in {"true", "1", "yes"}


# The judge does not discard an address ZeroBounce leaves unresolved: its own
# contact check accepts valid/catch_all, hard-rejects only `invalid`, and
# otherwise falls back to BounceBan (polling a pending job) before deciding.
# Mirroring that here stops this agent from throwing away a contact the judge
# would have accepted, which previously cost the company - and so the whole
# ICP - its score. The fallback is bounded per company because each call is
# paid and must not push a qualifying ICP past its sourcing cost cap.
_ZEROBOUNCE_HARD_REJECT_STATUSES = frozenset({"invalid"})
_BOUNCEBAN_ACCEPTED_VERDICTS = frozenset(
    {"deliverable", "valid", "safe", "catch_all", "accept_all"}
)
_BOUNCEBAN_PENDING_STATES = frozenset(
    {"pending", "queued", "queue", "processing", "running", "verifying"}
)
_BOUNCEBAN_INVALID_MARKERS = ("invalid", "disposable", "spamtrap", "abuse", "do not mail")
_BOUNCEBAN_FALLBACKS_PER_COMPANY = 2


def _bounceban_enabled() -> bool:
    return os.environ.get("BAKEOFF_BOUNCEBAN_FALLBACK", "1").strip().lower() not in (
        "0",
        "false",
        "",
    )


def _zerobounce_hard_reject(validation: Any) -> bool:
    """Only an explicit `invalid` forecloses the judge's BounceBan fallback."""

    record = _unwrap(validation)
    if not isinstance(record, Mapping):
        return False
    status = _norm(record.get("status")).replace(" ", "_").replace("-", "_")
    return status in _ZEROBOUNCE_HARD_REJECT_STATUSES


def _bounceban_payload(response: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    record = _unwrap(response)
    if not isinstance(record, Mapping):
        return {}, {}
    raw_result = record.get("result")
    result = raw_result if isinstance(raw_result, Mapping) else record
    return record, result


def _bounceban_verdict(response: Any) -> str:
    """Normalize BounceBan's verdict the way the judge reads it."""

    record, result = _bounceban_payload(response)
    if not record:
        return ""
    raw_result = record.get("result")
    if isinstance(raw_result, Mapping):
        value = (
            result.get("status")
            or result.get("result")
            or result.get("verdict")
            or result.get("classification")
        )
    else:
        value = (
            raw_result
            or result.get("verdict")
            or result.get("classification")
            or result.get("status")
        )
    return _norm(value).replace(" ", "_").replace("-", "_")


def _bounceban_pending(response: Any) -> bool:
    record, _ = _bounceban_payload(response)
    if not record:
        return False
    api_state = _norm(record.get("status") or record.get("state"))
    verdict = _bounceban_verdict(response)
    return api_state in _BOUNCEBAN_PENDING_STATES and verdict in (
        _BOUNCEBAN_PENDING_STATES | {""}
    )


def _bounceban_accepts(response: Any) -> bool:
    """Accept exactly what the judge accepts from a BounceBan verdict."""

    verdict = _bounceban_verdict(response)
    if not verdict:
        return False
    if verdict == "risky":
        # The judge keeps a risky address only when it is provably accept-all
        # and nothing contradicts that.
        _, result = _bounceban_payload(response)
        explanation = _norm(
            result.get("reason") or result.get("sub_status") or result.get("details")
        )
        catchall = (
            result.get("is_accept_all") is True
            or "catch all" in explanation
            or "accept all" in explanation
        )
        contradicted = result.get("is_disposable") is True or any(
            marker in explanation for marker in _BOUNCEBAN_INVALID_MARKERS
        )
        return bool(catchall and not contradicted)
    return verdict in _BOUNCEBAN_ACCEPTED_VERDICTS


def _bounceban_job_id(response: Any) -> str:
    record, result = _bounceban_payload(response)
    for source in (record, result):
        if isinstance(source, Mapping):
            value = _norm(
                source.get("id") or source.get("job_id") or source.get("request_id")
            )
            if value:
                return value
    return ""


def _bounceban_rescues(call_provider: ProviderCall, email: str) -> bool:
    """Run the judge's BounceBan fallback once, polling a pending job once."""

    try:
        response = call_provider("bounceban_verify_single", {"email": email})
    except Exception:
        return False
    if _bounceban_pending(response):
        job_id = _bounceban_job_id(response)
        if not job_id:
            return False
        try:
            response = call_provider("bounceban_get_single_status", {"id": job_id})
        except Exception:
            return False
    return _bounceban_accepts(response)


def _exact_title(title: str, targets: Sequence[str]) -> bool:
    normalized = _normalized_title(title)
    return bool(normalized) and any(normalized == _normalized_title(t) for t in targets)


def _find_contact(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    call_provider: ProviderCall,
    notes: list[str] | None = None,
    *,
    verify_page: bool = False,
    fallback_search: bool = True,
    min_employees: int = 0,
    record: Mapping[str, Any] | None = None,
    on_record: Callable[[Mapping[str, Any]], None] | None = None,
    validate_email: bool = False,
) -> dict[str, Any] | None:
    company = dict(company)
    page_url = _canonical_linkedin_company_url(company.get("company_linkedin"))
    if page_url and verify_page:
        # A page taken from research is unverified: one cheap company record
        # proves it is this company and gives the employee band the judge
        # checks. A band conflict ends the search before the costly people
        # lookups.
        element = record if record is not None else _company_record(
            call_provider, {"url": page_url}
        )
        if not element or not _record_matches_company(element, company):
            company["company_linkedin"] = ""
            if notes is not None:
                notes.append("stored LinkedIn page did not match; searched by name")
        else:
            if on_record is not None:
                on_record(element)
            conflict = _employee_band_conflict(element, icp)
            if conflict:
                if notes is not None:
                    notes.append(conflict)
                return None
            count = element.get("employeeCount")
            if (
                min_employees
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count < min_employees
                and _band_allows_larger(icp, min_employees)
            ):
                # Title-filtered people searches at very small companies come
                # back empty; the search costs more than the record.
                if notes is not None:
                    notes.append(f"LinkedIn shows {count} employees; too few for the target titles")
                return None
    search = call_provider("harvestapi_search_leads", _search_request(icp, company))
    candidates = _profiles(_unwrap(search))
    if not candidates and fallback_search and _successful_empty_search(search):
        fallback = _fallback_search_request(icp, company)
        if fallback is not None:
            search = call_provider("harvestapi_search_leads", fallback)
            candidates = _profiles(_unwrap(search))
    expected = _expected_company(company)
    targets = _bounded_strings(icp.get("target_roles"), limit=70)
    seniority = icp.get("target_seniority")
    selected: list[Mapping[str, Any]] = []
    for candidate in candidates:
        positions = _current_positions(candidate)
        matching_positions = [
            position
            for position in positions
            if _search_company_matches(expected, _position_company(position))
            and _role_matches(_position_title(position), targets, seniority)
        ]
        if matching_positions:
            selected.append(candidate)
    # The judge accepts an exact target title without a model review, so
    # spend the bounded profile lookups on those candidates first.
    selected.sort(
        key=lambda item: 0
        if any(
            _exact_title(_position_title(position), targets)
            for position in _current_positions(item)
        )
        else 1
    )
    emailless = 0
    bounceban_calls = 0
    for candidate in selected[:_PROFILE_LIMIT_PER_COMPANY]:
        if emailless >= _EMAILLESS_PROFILE_LIMIT:
            if notes is not None:
                notes.append("no emails at this company; stopped buying profiles")
            break
        linkedin = _profile_linkedin(candidate)
        if not linkedin:
            continue
        profile_response = call_provider(
            "harvestapi_get_profile",
            {"url": linkedin, "findEmail": "true"},
        )
        profiles = _profiles(_unwrap(profile_response))
        if not any(_emails(profile) for profile in profiles):
            emailless += 1
        for profile in profiles:
            contact = _contact_from_profile(profile, company=company, icp=icp)
            if contact is None:
                continue
            if validate_email:
                validation = call_provider("zerobounce_validate", {"email": contact["email"]})
                if not _email_acceptable(validation):
                    # The judge hard-rejects only `invalid`; for anything else
                    # it runs BounceBan before deciding, so do the same rather
                    # than discard a contact it would have accepted.
                    rescued = False
                    if (
                        _bounceban_enabled()
                        and not _zerobounce_hard_reject(validation)
                        and bounceban_calls < _BOUNCEBAN_FALLBACKS_PER_COMPANY
                    ):
                        bounceban_calls += 1
                        rescued = _bounceban_rescues(call_provider, contact["email"])
                        if rescued and notes is not None:
                            notes.append(
                                f"{contact['full_name']}: ZeroBounce unresolved, BounceBan deliverable; kept"
                            )
                    if not rescued:
                        if notes is not None:
                            notes.append(
                                f"{contact['full_name']}: email not valid or catch-all; next candidate"
                            )
                        continue
            return contact
    return None


def enrich_contacts(
    icp: Mapping[str, Any],
    companies: Sequence[Mapping[str, Any]],
    call_provider: ProviderCall,
    *,
    linkedin_hints: Mapping[str, str] | None = None,
    report: list[str] | None = None,
    workers: int = _CONTACT_WORKERS,
    fallback_search: bool = True,
    require_page: bool = False,
    min_employees: int = 0,
    lookup_page_by_name: bool = False,
    resolved: dict[str, dict[str, Any]] | None = None,
    validate_email: bool = False,
    known_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Attach supported contacts while preserving every company row and order.

    ``linkedin_hints`` maps a website domain to the company's LinkedIn page seen
    during research. It scopes the people search to that page but never enters
    the returned rows, whose ``company_linkedin`` stays as submitted.
    """

    output = [deepcopy(dict(company)) for company in companies]
    for company in output:
        company.pop("contact", None)
    if _text(icp.get("contact_policy")) != CONTACT_POLICY:
        return output
    if not _bounded_strings(icp.get("target_roles"), limit=70):
        return output
    hints = linkedin_hints or {}

    def lookup(company: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        notes: list[str] = []
        working = dict(company)
        hinted = False
        record: dict[str, Any] | None = None
        records: dict[str, dict[str, Any]] = {}
        if resolved is not None:
            # Hand back every trusted company record so the caller can cite
            # the LinkedIn page and its official name in the output.
            def remember(element: Mapping[str, Any]) -> None:
                key = _domain(working.get("company_website"))
                if key and element:
                    records[key] = dict(element)
        else:
            def remember(element: Mapping[str, Any]) -> None:
                return None
        if not _canonical_linkedin_company_url(working.get("company_linkedin")):
            hint = hints.get(_domain(working.get("company_website")), "")
            if hint:
                working["company_linkedin"] = hint
                hinted = True
                known = (known_records or {}).get(_domain(working.get("company_website")))
                if known and _canonical_linkedin_company_url(known.get("linkedinUrl")) == hint:
                    # Research already verified this record; skip the refetch.
                    record = dict(known)
                    remember(record)
            elif lookup_page_by_name and _text(working.get("company_name")):
                # The research DB lacks many large companies' pages; the
                # provider resolves well-known names directly.
                try:
                    found = _company_record(
                        call_provider, {"search": _text(working.get("company_name"))}
                    )
                except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
                    notes.append(f"page lookup failed: {type(exc).__name__}")
                    found = {}
                page = _canonical_linkedin_company_url(found.get("linkedinUrl"))
                if page and _record_matches_company(found, working):
                    working["company_linkedin"] = page
                    hinted = True
                    record = found
                    remember(found)
                    notes.append("LinkedIn page resolved by name")
        if require_page and not _canonical_linkedin_company_url(working.get("company_linkedin")):
            # A name keyword search returns strangers ("henry": 419 people)
            # at the same price as a page-scoped one; skip it.
            notes.append("no LinkedIn page known; contact search skipped")
            return None, notes
        try:
            contact = _find_contact(
                icp,
                working,
                call_provider,
                notes,
                verify_page=hinted,
                fallback_search=fallback_search,
                min_employees=min_employees,
                record=record,
                on_record=remember,
                validate_email=validate_email,
            )
        except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
            notes.append(f"lookup failed: {type(exc).__name__}")
            contact = None
        if resolved is not None:
            resolved.update(records)
        return contact, notes

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(output) or 1))) as pool:
        results = list(pool.map(lookup, output))
    for company, (contact, notes) in zip(output, results):
        name = _text(company.get("company_name"))
        if report is not None:
            for note in notes:
                report.append(f"{name}: {note}")
        if contact is not None:
            try:
                company["contact"] = ContactResult.model_validate(contact).model_dump(
                    mode="json", exclude_none=True
                )
            except (TypeError, ValueError):
                pass
    return output


__all__ = [
    "CONTACT_POLICY",
    "add_investor_relations_hints",
    "bind_homepage_pages",
    "cite_company_records",
    "enrich_contacts",
    "homepage_linkedin_links",
    "homepage_linkedin_pages",
]
