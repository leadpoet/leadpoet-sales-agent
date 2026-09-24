"""Deterministic contact enrichment for opted-in Arena rounds."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from arena_models import ContactResult


CONTACT_POLICY = "contacts_v1"
_PROFILE_LIMIT_PER_COMPANY = 3

# Why each candidate was refused, oldest first. A refused contact costs the
# whole company under contacts_v1, and a live run used to report only the call
# count, so there was no way to tell which gate did it. The harness drains this
# per company and logs it.
REJECTIONS: list[str] = []


def _validation_fields(error: Exception) -> str:
    """Which fields a validator refused -- never the contact's own values.

    The reason is written to the run log, so a person's name or email must not
    travel with it.
    """
    errors = getattr(error, "errors", None)
    rows = []
    if callable(errors):
        try:
            rows = list(errors())
        except Exception:  # noqa: BLE001
            rows = []
    named = ["%s (%s)" % (".".join(str(part) for part in row.get("loc") or ()) or "contact",
                          row.get("type") or "invalid")
             for row in rows[:5] if isinstance(row, Mapping)]
    return ", ".join(named) or type(error).__name__


def _reject(reason: str) -> None:
    if len(REJECTIONS) < 50:
        REJECTIONS.append(reason)
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
_SALES_FAMILY_PHRASES = (
    "account management",
    "business development",
    "commercial",
    "go to market",
    "growth",
    "gtm",
    "partnerships",
    "alliances",
    "channel",
    "revenue",
    "sales",
    "strategic accounts",
)
_SALES_FAMILY_CONFLICTS = (
    "engineering",
    "marketing",
    "representative",
)
# The judge gained an engineering / R&D family on 2026-09-19
# (role_batch_check.py): at one seniority, Engineering, Software Engineering,
# R&D and Product Development describe the same function, so "VP Product
# Development" can answer a "VP Engineering" target. It explicitly does NOT
# extend to Product Management, Product Marketing, Product Design or a bare
# "VP Product", nor to IT operations, security or sales engineering.
# Spelled as _normalized_title leaves them: it drops "and" and "&", so
# "Research and Development" reads "research development" and "R&D" "r d".
_ENGINEERING_FAMILY_PHRASES = (
    "engineering",
    "research development",
    "r d",
    "product development",
)
_ENGINEERING_FAMILY_CONFLICTS = (
    "sales",
    "product management",
    "product marketing",
    "product design",
    "information security",
    "information technology",
    "it operations",
    "customer",
    "solutions",
)
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
                    in {"error", "failed", "failure", "pending", "queued", "running", "cancelled"}
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


# scrapingdog.google takes a query of at most 500 characters and answers with
# organic_results rows of link/title/snippet (measured 2026-09-18). It spends
# Scrapingdog's own per-ICP quota, never Deepline's, which is the quota that
# binds this harness.
GOOGLE_QUERY_LIMIT = 500
# LinkedIn member URNs as Harvest search returns them: "ACoAA...", "ACwAA...".
_OPAQUE_MEMBER_ID = re.compile(r"AC[A-Za-z][A-Za-z0-9_-]{10,}")
_LINKEDIN_TITLE_TAIL = re.compile(r"\s*[|\-\u2013]\s*linkedin.*$", re.I)


def _google_people_query(company: Mapping[str, Any], targets: Sequence[str]) -> str:
    """The query shape that answered: roles, the company, and the /in/ path.

    Measured: with `site:linkedin.com/in` all ten results were person profiles
    and the first was the company's actual CRO; without it, a looser query
    returned one profile and nine pages about the company.
    """
    roles = " OR ".join('"%s"' % _text(role) for role in targets[:3] if _text(role))
    name = _company_name(_text(company.get("company_name"))) or _text(company.get("company_name"))
    parts = [part for part in (roles, '"%s"' % name if name else "",
                               "site:linkedin.com/in") if part]
    return " ".join(parts)[:GOOGLE_QUERY_LIMIT]


def _google_title_role(title: Any, company: Any = "") -> str:
    """The role a result title states, if it states one.

    Measured shapes: "Lon O'Connor - Chief Revenue Officer | LinkedIn" and
    "Amy Senew - Chief Revenue Officer at Esko - LinkedIn".

    The free web search also returns two shapes Google did not, measured
    2026-09-20: "Maya Kotturi - Infinimmune - LinkedIn", which states NO role,
    and "Wayne Hawkins - Inductive Bio Partner Success Manager - LinkedIn",
    which states the role after the company. Read blindly, the first looked
    like a person whose role is "Infinimmune" and was thrown away as a role
    mismatch -- every profile at four companies died that way in a live run.
    So when the clause begins with the company, strip it: what remains is the
    role, and nothing remaining means the title simply did not state one.
    """
    text = _LINKEDIN_TITLE_TAIL.sub("", _text(title))
    parts = [part.strip() for part in text.split(" - ") if part.strip()]
    if len(parts) < 2:
        return ""
    role = parts[1].split(" at ")[0].strip()
    name = _company_name(_text(company))
    if name:
        without = re.sub(r"^%s\b[\s,|:-]*" % re.escape(name), "", role,
                         flags=re.IGNORECASE).strip()
        if without != role:
            return without
    return role


def _google_title_employer(title: Any) -> str:
    """The employer a result title states after "at", if it states one.

    Measured shape: "Amy Senew - Chief Revenue Officer at Esko - LinkedIn".
    Only the first clause counts, so a second role ("| Board Member at X")
    is not read as the employer.
    """
    text = _LINKEDIN_TITLE_TAIL.sub("", _text(title))
    parts = [part.strip() for part in text.split(" - ") if part.strip()]
    if len(parts) < 2:
        return ""
    # " at X" and " @ X" both name the employer. Measured 2026-09-21: the free
    # search answered a Unify query with "Urvi Munot - Marketing Operations
    # Manager (Analytics) @ AWS", we read no employer at all, and paid to fetch
    # a profile that the employer check then refused.
    match = re.search(r"\s(?:at|@)\s+(.+)$", parts[1])
    if not match:
        return ""
    return re.split(r"\s*[|,\u00b7]\s*", match.group(1))[0].strip()


# A LinkedIn headline is freeform, and its second clause is as often a tagline
# as a job title: measured 2026-09-20 across every recorded people search, 138
# of 341 rows were refused as a "role mismatch" for headlines like "Working
# with Robots", "Supporting Google" and "North America | Projects & SMB-RPO |
# Korn Ferry". A tagline states no role, and refusing it throws away a person
# who may well hold the target one.
_ROLE_WORDS = (
    "manager", "director", "vp", "vice president", "head", "chief", "officer",
    "president", "lead", "principal", "partner", "owner", "founder", "ceo",
    "coo", "cfo", "cto", "cro", "cmo", "cio", "supervisor", "administrator",
    "coordinator", "specialist", "analyst", "engineer", "architect",
    "controller", "superintendent", "foreman", "executive", "associate",
    "consultant", "strategist", "scientist", "designer", "developer",
    "recruiter", "representative", "advisor", "counsel", "treasurer",
)


def _states_a_role(value: str) -> bool:
    """Whether this clause reads as a job title rather than a tagline."""
    words = set(re.findall(r"[a-z]+", _text(value).casefold()))
    normalized = _text(value).casefold()
    return bool(words & set(_ROLE_WORDS)) or any(
        phrase in normalized for phrase in ("vice president", "head of"))


def _employer_is_company(employer: str, name: str, root: str) -> bool:
    stated = _company_name(employer).replace(" ", "")
    target = name.replace(" ", "")
    if not stated:
        return True
    return bool((target and (stated in target or target in stated))
                or (root and len(root) > 2 and root in stated))


def _google_people_candidates(value: Any, company: Mapping[str, Any],
                              targets: Sequence[str], seniority: Any) -> list[str]:
    """LinkedIn profile URLs from a Google answer, the likeliest first.

    A row is kept only when it names the company; one whose title also states
    a matching role leads. Nothing here is trusted as a contact:
    harvestapi_get_profile still proves the employer, role, location and email.
    """
    rows = value.get("organic_results") if isinstance(value, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return []
    name = _company_name(_text(company.get("company_name"))).casefold()
    host = (urlsplit(_text(company.get("company_website"))).hostname or "")
    root = host.removeprefix("www.").split(".")[0].casefold()
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for row in list(rows)[:10]:
        if not isinstance(row, Mapping):
            continue
        url = _canonical_linkedin_profile(row.get("link"))
        if not url or url in seen:
            continue
        blob = " ".join(_text(row.get(key)) for key in
                        ("title", "snippet", "displayed_link")).casefold()
        role = _google_title_role(row.get("title"), company.get("company_name"))
        # A clause that names no job title at all is not a wrong role; it is no
        # role, which the paid profile fetch is there to settle.
        if role and not _states_a_role(role):
            role = ""
        role_fits = bool(role) and _role_matches(role, targets, seniority)
        names_company = bool((name and name in blob)
                             or (root and len(root) > 2 and root in blob))
        # A title that states the wrong role is a refusal, not a maybe: the
        # company's own CTO names the company in every row and would otherwise
        # take the one profile fetch this path can spend.
        if role and not role_fits:
            continue
        # A title that names another employer is a refusal too: the profile
        # check would fail on the employer, after spending a Deepline call.
        # Live runs lost fetches this way to a former executive who had moved.
        employer = _google_title_employer(row.get("title"))
        if employer and not _employer_is_company(employer, name, root):
            continue
        # A row that never names the company is the weakest kind: measured
        # 2026-09-18, the Tackle.io query put forward CROs of Fortanix and of
        # nobody in particular. It used to be dropped outright, but 38 of 341
        # recorded rows landed here and a LinkedIn headline often carries the
        # role while the employer shows only on the profile page. So keep it
        # LAST, behind every row that does name the company: the ranking is
        # what decides which two profiles we actually pay to fetch, and the
        # fetch itself proves the employer.
        seen.add(url)
        ranked.append(((0 if role_fits else 1) if names_company else 2, url))
    ranked.sort(key=lambda row: row[0])
    # A row that never names the company (rank 2) is a long shot, and each one
    # costs a profile fetch: measured 2026-09-21 on the marketing-automation
    # ICP, ten such fetches across five companies produced no contact at all.
    # So it is offered only when nothing better exists, and only once.
    strong = [url for rank, url in ranked if rank < 2]
    if strong:
        return strong[:3]
    return [url for rank, url in ranked if rank == 2][:1]


def _free_search_rows(value: Any) -> dict[str, Any]:
    """A ContextDev web-search answer in the shape the Google filter reads.

    Measured 2026-09-20 against a live zero-priced call: the envelope is
    {"status": "completed", "result": {"data": {"results": [...]}}} and each row
    carries url, title and description -- the same three facts Google returned
    as link, title and snippet, down to the title shape
    ("Name - Role at Company - LinkedIn"). So translate rather than duplicate
    the filtering: _google_people_candidates already refuses the wrong role,
    another employer and a row that never names the company.
    """
    result = value.get("result") if isinstance(value, Mapping) else None
    data = result.get("data") if isinstance(result, Mapping) else None
    if isinstance(data, Mapping):
        rows = data.get("results")
    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        rows = data
    else:
        rows = None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return {}
    return {"organic_results": [
        {"link": row.get("url"), "title": row.get("title"),
         "snippet": row.get("description")}
        for row in list(rows)[:10] if isinstance(row, Mapping)]}


def _people_rows(value: Any, depth: int = 0) -> list[tuple[str, str]]:
    """(canonical LinkedIn URL, the text Exa returned beside it)."""
    if depth > 5:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        out: list[tuple[str, str]] = []
        for item in list(value)[:20]:
            out.extend(_people_rows(item, depth + 1))
        return out
    if not isinstance(value, Mapping):
        return []
    out = []
    url = _canonical_linkedin_profile(
        value.get("url") or value.get("linkedinUrl") or value.get("linkedin_url")
    )
    if url:
        out.append((url, " ".join(
            _text(value.get(key))
            for key in ("title", "text", "summary", "snippet", "author")
        )))
    for key in ("results", "items", "profiles", "data", "result", "output"):
        if key in value:
            out.extend(_people_rows(value[key], depth + 1))
    return out


def _people_candidates(value: Any, company: Mapping[str, Any]) -> list[str]:
    """People-search URLs, those whose own result text names the company first.

    Exactly one profile fetch is spent per company, so this order decides
    whether it is spent on a plausible person or on a stranger who merely
    ranked well for the role.
    """
    ordered: dict[str, str] = {}
    for url, text in _people_rows(value):
        ordered.setdefault(url, text)
    name = _company_name(_text(company.get("company_name"))).casefold()
    host = (urlsplit(_text(company.get("company_website"))).hostname or "")
    root = host.removeprefix("www.").split(".")[0].casefold()

    def names_the_company(text: str) -> bool:
        lowered = text.casefold()
        return bool((name and name in lowered) or (root and len(root) > 2 and root in lowered))

    return [url for url, _text_value in
            sorted(ordered.items(), key=lambda row: 0 if names_the_company(row[1]) else 1)][:5]


# Pages where a small company prints its leadership. One exa.contents call
# reads all of them for about a cent each; a path the site lacks comes back as
# an error row, not a failure. Measured on prismatic.io: /about/ named the CRO
# beside a LinkedIn link, /team/ carried no person links, /company/ did not
# exist.
_TEAM_PAGE_PATHS = ("about", "about-us", "team", "leadership")
# Measured: prismatic.io's /about/ carried 1,385 characters and 8 profile links.
TEAM_PAGE_TEXT_CHARS = 4000
TEAM_PAGE_LINKS = 100
_PERSON_NAME = r"[A-Z][A-Za-z'\u2019.\-]+(?:\s+[A-Z][A-Za-z'\u2019.\-]+){1,3}"
_NAME_TITLE_LINE = re.compile(
    r"^[-*\u2022\u00b7\s]*(%s)(?:\s*[,|:\u2013\u2014]|\s+-)\s*(.{2,80}?)\s*$" % _PERSON_NAME)
_NAME_ONLY_LINE = re.compile(r"^[-*\u2022\u00b7\s]*(%s)\s*$" % _PERSON_NAME)
_LINKEDIN_PERSON = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%\-]+/?")


def _team_page_urls(company: Mapping[str, Any]) -> list[str]:
    website = _text(company.get("company_website"))
    host = (urlsplit(website if "://" in website else "https://" + website).hostname or "").lower()
    return ["https://%s/%s/" % (host, path) for path in _TEAM_PAGE_PATHS] if host else []


def _exa_results(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    """Exa result rows whether the broker unwrapped the Deepline envelope or not.

    Through the broker, exa.contents answers with ``results`` at the top level;
    a direct Deepline execute nests it under ``result.data``.
    """
    if depth > 4 or not isinstance(value, Mapping):
        return []
    rows = value.get("results")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, Mapping)]
    for key in ("result", "data"):
        found = _exa_results(value.get(key), depth + 1)
        if found:
            return found
    return []


def _name_tokens(name: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return [token for token in re.split(r"[^a-z]+", folded) if len(token) >= 2]


def _team_page_people(text: Any, targets: Sequence[str], seniority: Any) -> list[str]:
    """Names a page prints beside a target role, in page order.

    Reads "Anthony Owens, CRO" on one line and a name followed by its title on
    the next. The role test is the same _role_matches the Harvest path uses.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    names: list[str] = []
    for index, line in enumerate(lines):
        paired = _NAME_TITLE_LINE.match(line)
        if paired:
            name, title = paired.group(1), paired.group(2)
        else:
            alone = _NAME_ONLY_LINE.match(line)
            if not alone or index + 1 >= len(lines):
                continue
            name, title = alone.group(1), lines[index + 1]
        if _role_matches(title, targets, seniority) and name not in names:
            names.append(name)
    return names


def _team_page_candidates(value: Any, targets: Sequence[str], seniority: Any) -> list[str]:
    """LinkedIn URLs of people the company's own pages name in a target role.

    A person is linked only when both the first and the last name token occur
    in the profile slug ("anthony-owens-5531943b", "tannerburson"). Nothing here
    is trusted as a contact: harvestapi_get_profile still has to prove the
    employer, the role, the location and the email.
    """
    out: list[str] = []
    for row in _exa_results(value):
        text = row.get("text")
        extras = row.get("extras") if isinstance(row.get("extras"), Mapping) else {}
        raw_links = [link for link in (extras.get("links") or []) if isinstance(link, str)]
        raw_links += _LINKEDIN_PERSON.findall(str(text or ""))
        profiles: dict[str, tuple[str, set[str]]] = {}
        for link in raw_links:
            canonical = _canonical_linkedin_profile(link)
            if canonical:
                slug = canonical.rstrip("/").rsplit("/", 1)[-1].lower()
                profiles.setdefault(canonical, (
                    re.sub(r"[^a-z]", "", slug),
                    {part for part in re.split(r"[^a-z]+", slug) if part}))
        for name in _team_page_people(text, targets, seniority):
            tokens = _name_tokens(name)
            if len(tokens) < 2:
                continue
            for url, (joined, parts) in profiles.items():
                # A token of three letters or more may sit inside a joined slug
                # ("tannerburson"); a shorter one -- Wu, Li, Ng -- must be a
                # whole slug part, or it matches inside unrelated slugs.
                if (all(token in parts or (len(token) >= 3 and token in joined)
                        for token in (tokens[0], tokens[-1]))
                        and url not in out):
                    out.append(url)
    return out[:3]


def _slug_names_person(url: str, name: str) -> bool:
    """Whether a LinkedIn slug belongs to this person, by first and last name."""
    tokens = _name_tokens(name)
    if len(tokens) < 2:
        return False
    canonical = _canonical_linkedin_profile(url)
    if not canonical:
        return False
    slug = canonical.rstrip("/").rsplit("/", 1)[-1].lower()
    joined = re.sub(r"[^a-z]", "", slug)
    parts = {part for part in re.split(r"[^a-z]+", slug) if part}
    return all(token in parts or (len(token) >= 3 and token in joined)
               for token in (tokens[0], tokens[-1]))


def team_page_names(value: Any, targets: Sequence[str], seniority: Any) -> list[str]:
    """People the company's own pages name in a target role, linked or not.

    _team_page_candidates can only return someone whose LinkedIn the page also
    links, and most team pages link nobody: measured 2026-09-20, "company pages
    named nobody in a target role beside a LinkedIn link" was the single most
    common contact refusal, 33 times across twenty ICP runs. The name and the
    title are on the page all the same, and a free web search turns a name into
    a profile URL, so return the names too.
    """
    names: list[str] = []
    for row in _exa_results(value):
        for name in _team_page_people(row.get("text"), targets, seniority):
            if name not in names and len(_name_tokens(name)) >= 2:
                names.append(name)
    return names[:3]


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
    expected_slug = expected.get("linkedin_slug", "")
    observed_slug = observed.get("linkedin_slug", "")
    if expected_slug and observed_slug:
        strong.append(expected_slug == observed_slug)
    if strong:
        return all(strong)
    return name_matches


def _search_name_matches(expected: Mapping[str, str], observed_name: str) -> bool:
    """The search row's company name is the expected company's name.

    LinkedIn pages often append the domain: measured 2026-09-18, Humanly's
    VP of Sales sat under "Humanly (humanly.io)", which normalises to
    "humanly humanly io", and an exact comparison refused the one candidate
    holding a target role. Extra words are accepted only when they spell the
    company's own domain; the profile step still checks the employer by
    domain and LinkedIn page before anything is accepted.
    """
    name = expected.get("name", "")
    if not name or not observed_name:
        return False
    if observed_name == name:
        return True
    domain_words = set(re.findall(r"[a-z0-9]+", expected.get("domain", "").lower()))
    rest = observed_name[len(name):].split() if observed_name.startswith(name + " ") else []
    return bool(rest) and set(rest) <= domain_words


def _search_company_matches(
    expected: Mapping[str, str], observed: Mapping[str, str]
) -> bool:
    """Match Harvest search metadata whose company URL uses a numeric ID."""

    if not _search_name_matches(expected, observed.get("name", "")):
        return False
    observed_slug = observed.get("linkedin_slug", "")
    expected_slug = expected.get("linkedin_slug", "")
    if observed_slug and not observed_slug.isdecimal() and expected_slug:
        return observed_slug == expected_slug
    return True


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


def _role_matches(title: str, targets: Sequence[str], requested_seniority: Any) -> bool:
    actual = _normalized_title(title)
    if not actual or not _seniority_matches(title, requested_seniority):
        return False

    def sales_family(value: str) -> bool:
        normalized = _normalized_title(value)
        return (
            any(phrase in normalized for phrase in _SALES_FAMILY_PHRASES)
            and not any(conflict in normalized for conflict in _SALES_FAMILY_CONFLICTS)
        )

    def engineering_family(value: str) -> bool:
        normalized = _normalized_title(value)
        return (
            any(phrase in normalized for phrase in _ENGINEERING_FAMILY_PHRASES)
            and not any(conflict in normalized
                        for conflict in _ENGINEERING_FAMILY_CONFLICTS)
        )

    actual_words = actual.split()
    for target in targets:
        normalized = _normalized_title(target)
        # Do not let the generic ordered-word rule turn "VP Sales
        # Engineering" into a Sales match. The scorer treats these as
        # different functions even though the target words are a subsequence.
        if sales_family(normalized) and not sales_family(actual):
            continue
        # The same for engineering: "VP Sales Engineering" carries every word
        # of "VP Engineering" in order, and the judge still calls it a
        # different function.
        if engineering_family(normalized) and not engineering_family(actual):
            continue
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
    # The official judge treats Sales, Revenue, GTM, Business Development,
    # Partnerships and adjacent commercial titles as one function family.
    # Mirror that rule at discovery time so we do not discard a profile which
    # the judge could accept.  Seniority remains a hard gate: without an
    # explicit ICP seniority, infer allowed buckets from the requested titles.
    if sales_family(actual) and any(sales_family(target) for target in targets):
        if _text(requested_seniority):
            return True  # already checked by _seniority_matches above
        actual_level = _seniority(actual)
        target_levels = {_seniority(target) for target in targets}
        if actual_level in target_levels:
            return True
        # The scorer's role rubric treats Head-of as VP- or Director-level.
        if actual_level == "head" and target_levels & {"vp", "director"}:
            return True
        if actual_level in {"vp", "director"} and "head" in target_levels:
            return True
    # The same mirroring for the engineering family. Seniority is unchanged:
    # _seniority_matches above has already refused a VP against a C-level
    # target, and the rubric's own example keeps a Product Development Manager
    # away from a VP target.
    if engineering_family(actual) and any(engineering_family(t) for t in targets):
        if _text(requested_seniority):
            return True
        actual_level = _seniority(actual)
        target_levels = {_seniority(target) for target in targets}
        if actual_level in target_levels:
            return True
        if actual_level == "head" and target_levels & {"vp", "director"}:
            return True
        if actual_level in {"vp", "director"} and "head" in target_levels:
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


# The contact judge folds these place names together before comparing
# (contact_verification.py _LOCATION_PART_ALIASES, 2026-09-20). Mirror it, or
# we drop a profile the judge would have accepted: an ICP asking for "New York
# City" against a profile that says "New York" is one contact lost.
_LOCATION_ALIASES = {
    ("US", "city", "new york"): "new york city",
    ("PL", "city", "cracow"): "krakow",
    # Spelled as an escape: the source bundle must stay ASCII.
    ("PL", "region", "ma\u0142opolskie"): "lesser poland voivodeship",
    ("PL", "region", "malopolskie"): "lesser poland voivodeship",
}


def _place(value: Any, country: str, part: str) -> str:
    """One place name as the judge compares it: normalized, then aliased."""
    normalized = _norm(value)
    return _LOCATION_ALIASES.get(
        (_text(country).upper(), part, normalized), normalized)


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
    country_code = location["country"].upper()
    if constraints["region"]:
        actual_region = _place(location.get("region"), country_code, "region")
        exact_match = any(
            actual_region == _place(item, country_code, "region")
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
    if constraints["city"] and _place(location.get("city"), country_code, "city") not in {
        _place(item, country_code, "city") for item in constraints["city"]
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
    company_linkedin = _text(company.get("company_linkedin"))
    if _linkedin_company_slug(company_linkedin):
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
        or countries
    )
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


def _combined_search_request(
    icp: Mapping[str, Any], company: Mapping[str, Any]
) -> dict[str, Any]:
    """One Harvest lead search carrying the exact AND the functional titles.

    Asking for them in two searches reserves 0.7 credits twice for a single
    company. ``currentJobTitles`` is a comma separated list, so both fit in one
    call. Acceptance still applies the strict role and company checks in
    :func:`_find_contact`, so the wider query never weakens acceptance.
    """
    request = _search_request(icp, company)
    roles: list[str] = []
    seen: set[str] = set()
    targets = _bounded_strings(icp.get("target_roles"), limit=70)
    # Exact titles first, then the functional terms they reduce to, so the
    # precise request still leads the query.
    for candidate in list(targets) + [_functional_title(t) for t in targets]:
        role = _text(candidate)
        key = role.casefold()
        if role and key not in seen:
            seen.add(key)
            roles.append(role)
    if roles:
        request["currentJobTitles"] = ",".join(roles)
    return request


def _successful_search(value: Any) -> bool:
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
    )


def _email_source(record_id: str) -> dict[str, str]:
    source = {"provider": "harvestapi", "tool": "harvestapi_get_profile"}
    if record_id:
        source["record_id"] = record_id
    return source


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
    # record_id looks optional field by field, but ContactEmailSource carries a
    # model validator: "email_source requires broker_call_id or record_id"
    # (qualification/contact_models.py). harness.call discards the broker's call
    # metadata, so record_id is the only id we can supply -- it IS required.
    missing = [label for label, value in
               (("full_name", name), ("linkedin_url", linkedin), ("email", email),
                ("record_id", record_id))
               if not value]
    if missing:
        _reject("profile is missing " + ", ".join(missing))
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
        positions = _current_positions(profile)
        role_hits = [item for item in positions
                     if _role_matches(_position_title(item), targets, icp.get("target_seniority"))]
        if role_hits:
            # The role matched and the employer did not. Say which identity
            # field disagreed: a same-named different company is a correct
            # refusal, a formatting difference is a bug, and the two read alike
            # otherwise.
            seen = _position_company(role_hits[0])
            _reject("role matched but employer did not: expected name=%r domain=%r "
                    "slug=%r, observed name=%r domain=%r slug=%r"
                    % (expected.get("name"), expected.get("domain"),
                       expected.get("linkedin_slug"), seen.get("name"),
                       seen.get("domain"), seen.get("linkedin_slug")))
        else:
            observed = ", ".join(
                title for title in (_position_title(item) for item in positions) if title
            )[:160]
            _reject("no current role matches the target roles (observed: %s)"
                    % (observed or "none"))
        return None

    location = _profile_location(profile)
    geography = icp.get("contact_geography")
    geography = geography if isinstance(geography, Mapping) else {}
    if not _location_matches(location, geography):
        _reject("contact location %s is outside the requested contact geography"
                % (location.get("country") or "unknown"))
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
        "email_source": _email_source(record_id),
    }


def _find_contact(
    icp: Mapping[str, Any],
    company: Mapping[str, Any],
    call_provider: ProviderCall,
) -> dict[str, Any] | None:
    """Find one contact, cheapest provider first.

    A Harvest lead search reserves a published 0.7 credits, about seven times
    an observed Exa search, and it was previously the first call for every
    company and could run twice. The people search runs first now; Harvest is
    the fallback and runs at most once. Every accepted field still comes from a
    successful HarvestAPI profile, so acceptance is unchanged. No email
    guessing.
    """
    expected = _expected_company(company)
    targets = _bounded_strings(icp.get("target_roles"), limit=70)
    seniority = icp.get("target_seniority")

    def select(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        selected: list[Mapping[str, Any]] = []
        for candidate in rows:
            positions = _current_positions(candidate)
            matching_positions = [
                position
                for position in positions
                if _search_company_matches(expected, _position_company(position))
                and _role_matches(_position_title(position), targets, seniority)
            ]
            if matching_positions:
                selected.append(candidate)
        return selected

    def verify(linkedin_candidates: Sequence[str], source: str) -> dict[str, Any] | None:
        for linkedin in linkedin_candidates:
            if not linkedin:
                continue
            profile_response = call_provider(
                "harvestapi_get_profile",
                {"url": linkedin, "findEmail": "true"},
            )
            profiles = _profiles(_unwrap(profile_response, require_success=True))
            if profile_response is None:
                _reject("profile fetch for %s not run (call refused or failed)" % linkedin)
            elif not profiles:
                _reject("profile fetch for %s returned no profile record" % linkedin)
            for profile in profiles:
                # Harvest search may return an opaque member ID that redirects
                # to the canonical slug in the independently fetched profile
                # record. Measured 2026-09-18: every such ID in nine live lead
                # searches began "ACw", not "ACo", and a check for "ACo" alone
                # skipped each fetched profile here without a word.
                opaque = _OPAQUE_MEMBER_ID.fullmatch(linkedin.rstrip("/").rsplit("/", 1)[-1])
                if not opaque and _profile_linkedin(profile).casefold() != linkedin.casefold():
                    _reject("profile fetch for %s returned a different profile (%s)"
                            % (linkedin, _profile_linkedin(profile) or "no URL"))
                    continue
                contact = _contact_from_profile(profile, company=company, icp=icp)
                if contact is not None:
                    return contact
            # Name the path that proposed a failed candidate: a live run lost
            # a fetch to a person who had moved, and the log could not say
            # whether Google or the Harvest lead search had put them forward.
            _reject("candidate %s came from %s" % (linkedin, source))
        return None

    # Cheapest first: the company's own leadership pages (about a cent each)
    # before a people search (0.12 credits) or a Harvest lead search (0.7).
    # The harness answers this from one batched read of every candidate's
    # pages (read_leadership_pages) with this same request shape.
    if icp.get("_contact_team_pages") is True:
        pages = _team_page_urls(company)
        if pages:
            response = call_provider("exa_team_pages", {
                "urls": pages, "text": {"maxCharacters": TEAM_PAGE_TEXT_CHARS},
                "extras": {"links": TEAM_PAGE_LINKS},
            })
            named = _team_page_candidates(response, targets, seniority)
            if response is None:
                _reject("company pages not fetched (call refused or failed)")
            elif not named:
                _reject("company pages named nobody in a target role beside a LinkedIn link")
            contact = verify(named[:1], "company pages")
            if contact is not None:
                return contact
            # The page named somebody in a target role but linked no profile.
            # The free web search turns that name into a LinkedIn URL for
            # nothing, and the slug has to carry both their names before we
            # pay to fetch it.
            for person in team_page_names(response, targets, seniority)[:2]:
                answer = call_provider("contextdev_people_search", {
                    "query": '"%s" "%s" site:linkedin.com/in'
                             % (person, _text(company.get("company_name")))})
                if answer is None:
                    _reject("name search for %s not run (call refused or failed)" % person)
                    break
                rows = _free_search_rows(answer).get("organic_results") or []
                urls = [_canonical_linkedin_profile(row.get("link")) for row in rows]
                matched = [url for url in urls if url and _slug_names_person(url, person)]
                if not matched:
                    _reject("name search for %s found no profile of theirs" % person)
                    continue
                contact = verify(matched[:1], "company pages plus name search")
                if contact is not None:
                    return contact

    # The free web search leads. contextdev_post_web_search is priced at zero
    # credits (provider_costs._DEEPLINE_FIXED_CREDITS, confirmed live on
    # 2026-09-20 with the balance unchanged) and answers the same query shape
    # Google did, so it is the one people search that costs nothing at all --
    # and our Scrapingdog credit is spent, which left this harness with no
    # people search whatsoever.
    if icp.get("_contact_free_people") is True:
        answer = call_provider("contextdev_people_search",
                               {"query": _google_people_query(company, targets)})
        named = _google_people_candidates(_free_search_rows(answer), company,
                                          targets, seniority)
        if answer is None:
            _reject("free people search not run (call refused or failed)")
        elif not named:
            _reject("free people search returned no usable LinkedIn profile")
        contact = verify(named[:2], "free people search")
        if contact is not None:
            return contact

    # Google through Scrapingdog next: it spends that provider's own quota, so
    # unlike every other search here it costs no Deepline call -- and the
    # Deepline quota is what stops this harness from filling its slots.
    if icp.get("_contact_google_people") is True:
        answer = call_provider("scrapingdog_people_search",
                               {"query": _google_people_query(company, targets),
                                "country": "us"})
        named = _google_people_candidates(answer, company, targets, seniority)
        if answer is None:
            _reject("google people search not run (call refused or failed)")
        elif not named:
            _reject("google people search returned no usable LinkedIn profile")
        contact = verify(named[:1], "google people search")
        if contact is not None:
            return contact

    # Only when Google is unavailable. The two do the same job -- find a
    # LinkedIn profile for a target role at this company -- but this one spends
    # a Deepline call, and running both left no call for the Harvest fallback's
    # own profile fetch: measured, Tackle.io walked leadership pages, Google and
    # Exa, then lost its last fetch to CONTACT_CALLS_PER_COMPANY.
    if (icp.get("_contact_people_fallback") is True
            and icp.get("_contact_google_people") is not True):
        roles = " OR ".join('"%s"' % role for role in targets[:5])
        company_name = _text(company.get("company_name"))
        geography = icp.get("contact_geography")
        geography = geography if isinstance(geography, Mapping) else {}
        locations = _bounded_strings(geography.get("cities"), limit=3)
        locations += _bounded_strings(geography.get("regions"), limit=3)
        locations += _bounded_strings(geography.get("countries"), limit=3)
        query = " ".join(part for part in (
            roles, ('"%s"' % company_name) if company_name else "",
            " OR ".join('"%s"' % item for item in locations[:5]),
        ) if part)[:2000]
        people = call_provider("exa_people_search", {
            "query": query, "category": "people", "type": "auto",
            "numResults": 5, "includeDomains": ["linkedin.com"],
        })
        candidates = _people_candidates(people, company)
        if people is None:
            _reject("people search not run (call refused or failed)")
        elif not candidates:
            _reject("people search returned no LinkedIn profile URL")
        contact = verify(candidates[:1], "people search")
        if contact is not None:
            return contact

    search = call_provider(
        "harvestapi_search_leads", _combined_search_request(icp, company)
    )
    rows = _profiles(_unwrap(search, require_success=True))
    selected = select(rows)
    linkedin_candidates = [
        url for url in (_profile_linkedin(candidate) for candidate in selected)
        if url
    ][:_PROFILE_LIMIT_PER_COMPANY]
    if search is None:
        _reject("lead search not run (call refused or failed)")
    elif not linkedin_candidates:
        _reject("lead search returned %d row(s); none held the target role at the company"
                % len(rows))
    return verify(linkedin_candidates, "lead search")


def enrich_contacts(
    icp: Mapping[str, Any],
    companies: Sequence[Mapping[str, Any]],
    call_provider: ProviderCall,
) -> list[dict[str, Any]]:
    """Attach supported contacts while preserving every company row and order."""

    output = [deepcopy(dict(company)) for company in companies]
    for company in output:
        company.pop("contact", None)
    if _text(icp.get("contact_policy")) != CONTACT_POLICY:
        return output
    if not _bounded_strings(icp.get("target_roles"), limit=70):
        return output
    for company in output:
        try:
            contact = _find_contact(icp, company, call_provider)
        except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
            # Both failures here used to pass without a word, and each costs the
            # company: contacts_v1 drops a company that has no contact. A live
            # run reported two provider calls for a company and no reason at all.
            _reject("contact search failed: %s" % type(exc).__name__)
            contact = None
        if contact is not None:
            try:
                company["contact"] = ContactResult.model_validate(contact).model_dump(
                    mode="json", exclude_none=True
                )
            except (TypeError, ValueError) as exc:
                _reject("contact refused by the output model: %s"
                        % _validation_fields(exc))
    return output


__all__ = ["CONTACT_POLICY", "enrich_contacts"]
