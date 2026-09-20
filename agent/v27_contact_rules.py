"""Pure contact normalization from the public Arena contact verifier (MIT).
No model classification or provider verification runs here.
"""

from __future__ import annotations

import re,unicodedata,json,hashlib

from typing import Any,Mapping,Sequence,Optional

from agent.safe_urls import urlparse

from agent.v92_common import domain

from agent.v27_country_data import COUNTRIES,US_STATES

def _get(value,key,default=None):
    return value.get(key,default) if isinstance(value,Mapping) else getattr(value,key,default)
def _hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
def _registrable_domain(value):return domain(str(value or ''))
def _norm_country(value):
    aliases={'u.k.':'GB','uk':'GB','great britain':'GB','united states of america':'US','usa':'US','u.s.':'US','u.s.a.':'US','south korea':'KR','north korea':'KP','russia':'RU','vietnam':'VN','laos':'LA','bolivia':'BO','tanzania':'TZ','venezuela':'VE'}
    for name,iso,iso3,_ in COUNTRIES:
        for key in (name,iso,iso3):aliases[key.casefold()]=iso
    return aliases.get(str(value or '').strip().casefold(),_norm(value))
def _canonical_linkedin(value):
    p=urlparse(str(value or '') if '://' in str(value or '') else 'https://'+str(value or ''))
    if p.scheme not in ('http','https') or p.username or p.password:return ''
    if not (p.hostname=='linkedin.com' or str(p.hostname).endswith('.linkedin.com')):return ''
    parts=p.path.strip('/').split('/')
    if len(parts)!=2 or parts[0]!='in' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,119}',parts[1]):return ''
    return 'https://www.linkedin.com/in/'+parts[1]+'/'


_TITLE_EXPANSIONS = {
    "ceo": "chief executive officer",
    "coo": "chief operating officer",
    "cto": "chief technology officer",
    "cio": "chief information officer",
    "cfo": "chief financial officer",
    "cmo": "chief marketing officer",
    "cro": "chief revenue officer",
    "ciso": "chief information security officer",
    "chro": "chief human resources officer",
    "svp": "senior vice president",
    "evp": "executive vice president",
    "vp": "vice president",
    "dir": "director",
    "revops": "revenue operations",
    "salesops": "sales operations",
    "gtm": "go to market",
    "bd": "business development",
}

def _text(value: Any) -> str:
    return str(value or "").strip()

def _norm(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", _text(value))
    letters = "".join(char for char in raw if not unicodedata.combining(char))
    return re.sub(r"[\W_]+", " ", letters.casefold()).strip()

def _unwrap_data(value: Any) -> Any:
    """Unwrap only documented/common provider envelopes, with a hard depth cap."""
    current = value
    for _ in range(8):
        if not isinstance(current, Mapping):
            return current
        status = _norm(current.get("status"))
        if status in {"pending", "queued", "processing", "running"}:
            return current
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
            if (
                isinstance(child, (Mapping, list, tuple))
                and child is not current
            ):
                current = child
                moved = True
                break
        if not moved:
            return current
    return current

def _profile_candidates(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    if depth > 5:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        found: list[Mapping[str, Any]] = []
        for item in list(value)[:25]:
            found.extend(_profile_candidates(item, depth + 1))
        return found
    if not isinstance(value, Mapping):
        return []
    profile_keys = {
        "publicIdentifier",
        "linkedinUrl",
        "linkedin_url",
        "firstName",
        "lastName",
        "currentPosition",
        "currentPositions",
        "experience",
    }
    found = [value] if profile_keys.intersection(value) else []
    for key in ("elements", "items", "profiles", "profile", "element", "results"):
        if key in value:
            found.extend(_profile_candidates(value[key], depth + 1))
    return found

def _profile_linkedin(profile: Mapping[str, Any]) -> str:
    direct = _canonical_linkedin(
        profile.get("linkedinUrl")
        or profile.get("linkedin_url")
        or profile.get("profileUrl")
        or profile.get("url")
    )
    if direct:
        return direct
    identifier = _text(profile.get("publicIdentifier") or profile.get("public_identifier"))
    return _canonical_linkedin(f"linkedin.com/in/{identifier}") if identifier else ""

def _profile_name(profile: Mapping[str, Any]) -> str:
    first = _text(profile.get("firstName") or profile.get("first_name"))
    last = _text(profile.get("lastName") or profile.get("last_name"))
    return f"{first} {last}".strip() or _text(profile.get("fullName") or profile.get("full_name"))

def _is_current_experience(item: Mapping[str, Any]) -> bool:
    if item.get("current") is False or item.get("isCurrent") is False:
        return False
    if item.get("current") is True or item.get("isCurrent") is True:
        return True
    end = item.get("endDate", item.get("end_date"))
    if end is None or end == "":
        return True
    if isinstance(end, Mapping):
        if not any(end.values()):
            return True
        if _norm(end.get("text")) in {"present", "current", "now"}:
            return True
    return _norm(end) in {"present", "current", "now"}

def _current_positions(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    positions: list[Mapping[str, Any]] = []
    for key in ("currentPosition", "currentPositions", "current_position"):
        current = profile.get(key)
        if isinstance(current, Mapping) and _is_current_experience(current):
            positions.append(current)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            positions.extend(
                item
                for item in current[:10]
                if isinstance(item, Mapping) and _is_current_experience(item)
            )
    experience = profile.get("experience") or profile.get("experiences") or []
    if isinstance(experience, Sequence) and not isinstance(experience, (str, bytes, bytearray)):
        positions.extend(
            item for item in experience[:25] if isinstance(item, Mapping) and _is_current_experience(item)
        )
    unique: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for position in positions:
        digest = _hash(position)
        if digest not in seen:
            seen.add(digest)
            unique.append(position)
    return unique

def _position_title(position: Mapping[str, Any]) -> str:
    return _text(
        position.get("title")
        or position.get("position")
        or position.get("role")
        or position.get("jobTitle")
    )

def _company_identifiers(company: Any) -> dict[str, str]:
    linkedin = _text(
        _get(company, "linkedin_url")
        or _get(company, "linkedinUrl")
        or _get(company, "linkedin_company_url")
        or _get(company, "company_linkedin")
    )
    linkedin_id = _text(_get(company, "linkedin_id") or _get(company, "linkedinId"))
    if not linkedin_id and linkedin:
        linkedin_id = urlparse(linkedin if "://" in linkedin else f"https://{linkedin}").path.rstrip("/").split("/")[-1]
    domain = _registrable_domain(
        _get(company, "domain")
        or _get(company, "website")
        or _get(company, "company_website")
    )
    return {
        "name": _norm_company_name(
            _get(company, "company_name") or _get(company, "name")
        ),
        "domain": domain,
        "linkedin_slug": _norm(linkedin_id),
    }

def _position_company(position: Mapping[str, Any]) -> dict[str, str]:
    company = position.get("company") if isinstance(position.get("company"), Mapping) else {}
    name = (
        position.get("companyName")
        or position.get("company_name")
        or position.get("employerName")
        or company.get("name")
    )
    domain = position.get("companyDomain") or position.get("company_domain") or company.get("domain") or company.get("website")
    linkedin = position.get("companyLinkedinUrl") or position.get("companyLinkedInUrl") or company.get("linkedinUrl") or company.get("linkedin_url")
    # LinkedIn's profile companyId can be numeric while the canonical company
    # URL uses a slug. Compare the URL slug and never equate those two forms.
    linkedin_slug = ""
    if linkedin:
        linkedin_slug = urlparse(
            _text(linkedin) if "://" in _text(linkedin) else f"https://{linkedin}"
        ).path.rstrip("/").split("/")[-1]
    domain_text = _registrable_domain(domain)
    return {
        "name": _norm_company_name(name),
        "domain": domain_text,
        "linkedin_slug": _norm(linkedin_slug),
    }

def _norm_company_name(value: Any) -> str:
    words = _norm(value).split()
    legal_suffixes = {
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
    while words and words[-1] in legal_suffixes:
        words.pop()
    return " ".join(words)

def _company_matches(expected: Mapping[str, str], observed: Mapping[str, str]) -> bool:
    strong = [
        expected[key] == observed[key]
        for key in ("domain", "linkedin_slug")
        if expected.get(key) and observed.get(key)
    ]
    if strong:
        # One explicit contradiction defeats a weaker name match (and even a
        # second strong match); provider evidence must describe one company.
        return all(strong)
    return bool(
        expected.get("name")
        and observed.get("name")
        and expected["name"] == observed["name"]
    )

def _normalize_title(value: Any) -> str:
    words = _norm(value).split()
    expanded: list[str] = []
    for word in words:
        expanded.extend(_TITLE_EXPANSIONS.get(word, word).split())
    return " ".join(word for word in expanded if word not in {"of", "the", "and"})

def _seniority(value: Any) -> str:
    title = _normalize_title(value)
    if re.search(r"\bchief\b.*\bofficer\b", title):
        return "c_level"
    if (
        "managing partner" in title
        or "managing director" in title
        or re.search(r"\b(owner|founder)\b", title)
        or (re.search(r"\bpresident\b", title) and "vice president" not in title)
    ):
        return "c_level"
    if "vice president" in title:
        return "vp"
    if "head " in f"{title} ":
        return "head"
    if "director" in title:
        return "director"
    if "manager" in title:
        return "manager"
    return "other"

def _deterministic_role_match(actual: str, targets: Sequence[str], target_seniority: str) -> Optional[bool]:
    actual_norm = _normalize_title(actual)
    if not actual_norm:
        return False
    if any(
        re.search(rf"\b{marker}\b", actual_norm)
        for marker in ("assistant", "former", "formerly", "retired", "previous", "previously", "ex")
    ):
        return False
    if not _target_seniority_matches(actual_norm, target_seniority):
        return False
    for target in targets:
        target_norm = _normalize_title(target)
        if actual_norm == target_norm:
            return True
    if not targets and _norm(target_seniority):
        return True
    return None

def _target_seniority_matches(actual: str, requested: str) -> bool:
    requested_raw = _text(requested).casefold()
    target = _norm(requested)
    if not target:
        return True
    actual_level = _seniority(actual)
    canonical = {
        "c level": "c_level",
        "c suite": "c_level",
        "executive": "c_level",
        "vp": "vp",
        "vice president": "vp",
        "head": "head",
        "head of": "head",
        "director": "director",
        "manager": "manager",
    }
    if (
        ("+" in requested_raw and target in {"vp", "vice president"})
        or target in {"vp above", "vp and above", "vice president above", "vice president and above"}
    ):
        return actual_level in {"vp", "c_level"}
    if (
        ("+" in requested_raw and target == "director")
        or target in {"director above", "director and above"}
    ):
        return actual_level in {"director", "head", "vp", "c_level"}
    expected = canonical.get(target)
    return actual_level == expected if expected else False

def _profile_location(profile: Mapping[str, Any]) -> dict[str, str]:
    location = profile.get("location") if isinstance(profile.get("location"), Mapping) else {}
    parsed = location.get("parsed") if isinstance(location.get("parsed"), Mapping) else {}
    return {
        "country": _norm_country(
            profile.get("country")
            or profile.get("countryName")
            or location.get("country")
            or location.get("countryName")
            or location.get("countryCode")
            or parsed.get("country")
            or parsed.get("countryFull")
            or parsed.get("countryCode")
        ),
        "region": _norm(
            profile.get("region")
            or profile.get("state")
            or location.get("region")
            or location.get("state")
            or parsed.get("state")
            or parsed.get("regionCode")
        ),
        "city": _norm(profile.get("city") or location.get("city") or parsed.get("city")),
    }

def _location_check(claim: Mapping[str, Any], profile: Mapping[str, Any], icp: Any) -> tuple[str, str]:
    claimed = claim.get("location") if isinstance(claim.get("location"), Mapping) else {}
    observed = _profile_location(profile)
    country = _norm_country(claimed.get("country"))
    if not observed["country"]:
        return "unknown", "contact_location_unverified"
    if observed["country"] != country:
        return "fail", "contact_location_mismatch"
    for part in ("region", "city"):
        expected = _norm(claimed.get(part))
        if expected:
            if not observed[part]:
                return "unknown", "contact_location_unverified"
            if expected != observed[part]:
                return "fail", "contact_location_mismatch"

    geography = _get(icp, "contact_geography") or {}
    if not isinstance(geography, Mapping):
        geography = {}
    constraints = {
        "country": geography.get("countries") or [],
        "region": geography.get("regions") or [],
        "city": geography.get("cities") or [],
    }
    for part, allowed in constraints.items():
        if not allowed:
            continue
        normalize = _norm_country if part == "country" else _norm
        allowed_values = {normalize(item) for item in allowed if _text(item)}
        value = observed[part]
        if value and value not in allowed_values:
            return "fail", "contact_geography_mismatch"
        if not value:
            return "unknown", "contact_geography_unverified"
    return "pass", "contact_location_verified"

def _extract_emails(profile: Mapping[str, Any]) -> set[str]:
    emails: set[str] = set()
    direct_keys = ("email", "workEmail", "work_email", "professionalEmail", "professional_email")
    for key in direct_keys:
        value = profile.get(key)
        if isinstance(value, str) and "@" in value:
            emails.add(value.strip().casefold())
    collection = profile.get("emails") or profile.get("emailAddresses") or []
    if isinstance(collection, Sequence) and not isinstance(collection, (str, bytes, bytearray)):
        for item in collection[:20]:
            value = item.get("email") or item.get("value") if isinstance(item, Mapping) else item
            if isinstance(value, str) and "@" in value:
                emails.add(value.strip().casefold())
    return emails
