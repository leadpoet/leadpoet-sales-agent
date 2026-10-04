"""Headquarters checks against the ICP geography, using the judge's own country and US-region/state helpers."""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import gates
from . import scorer_mirror as sm

_US = {"united states", "united states of america", "us", "usa", "u.s.", "u.s.a."}


def constraint(icp: Mapping[str, Any]) -> dict[str, Any]:
    """{country, states (US region or state set, or empty), city ('' unless a non-US city is named), known}."""

    geography = str(icp.get("geography") or "").strip()
    country = str(icp.get("country") or "").strip() or geography.split(",")[0].strip()
    regions = gates.region_states(geography)
    states = gates.us_states(geography)
    known = regions is not None and states is not None
    if not known:
        from .scout import region_states
        regions, states = region_states(geography), frozenset()
    wanted = frozenset(regions or ()) | frozenset(states or ())
    city = ""
    parts = [p.strip() for p in geography.split(",") if p.strip()]
    if len(parts) >= 2 and parts[0].casefold() not in _US and not wanted:
        city = parts[1]
    return {"country": country, "states": wanted, "city": city, "known": known}


def country_name(code_or_name: Any) -> str:
    """'US' -> 'United States'; a full name is title-cased; '' when unknown."""

    from .scout import iso_country
    text = str(code_or_name or "").strip()
    name = iso_country(text) if len(text) == 2 else text
    return " ".join(w if w.isupper() else w.capitalize() for w in str(name or "").split())


def check(icp: Mapping[str, Any], *, hq_country: Any, hq_state: Any = "", hq_city: Any = "") -> dict[str, Any]:
    """{'drop': reason or '', 'country': the ICP country string when the observed HQ matches, 'state': observed
    state name or '', 'unresolved': True when the HQ area is not established}."""

    want = constraint(icp)
    observed = country_name(hq_country)
    out: dict[str, Any] = {"drop": "", "country": "", "state": "", "unresolved": False, "observed": observed}
    if not observed:
        out["drop"] = "no observed HQ country"
        return out
    verdict = gates.country_ok(observed, want["country"])
    if verdict is None:
        allowed, _ = sm.allowed_countries(want["country"])
        verdict = not allowed or sm.normalize_country(observed) in allowed
    if not verdict:
        out["drop"] = f"HQ country {observed} outside {want['country']}"
        return out
    out["country"] = want["country"] or observed
    state = infer_state(hq_state, hq_city) if sm.normalize_country(observed) == sm.normalize_country("United States") \
        else ""
    out["state"] = state
    if want["states"]:
        if not state:
            out["unresolved"] = True
        elif state not in want["states"]:
            out["drop"] = f"HQ state {state} outside {icp.get('geography')}"
    if want["city"]:
        city = " ".join(str(hq_city or "").split())
        if not city:
            out["unresolved"] = True
        elif re.sub(r"[^a-z]", "", city.casefold()) != re.sub(r"[^a-z]", "", want["city"].casefold()):
            out["drop"] = f"HQ city {city} is not {want['city']}"
    return out


def infer_state(hq_state: Any, hq_city: Any = "") -> str:
    """The US state of a headquarters record: the state field ('CA', 'California'), a state inside a 'City, State'
    string, else a known city ('San Francisco' -> California); '' when none names one."""

    from .hiring import _place
    from .scout import canonical_state

    text = " ".join(str(hq_state or "").split())
    state = canonical_state(text)
    if state:
        return state
    for piece in re.split(r"[,/;]", text):
        state = canonical_state(piece.strip())
        if state:
            return state
    for value in (text, " ".join(str(hq_city or "").split())):
        found, state = _place(value) if value else (set(), "")
        if state and found <= {"united states"}:
            return state
    return ""


def established(row_flags: Mapping[str, Any]) -> bool:
    return bool(row_flags.get("country")) and not row_flags.get("unresolved") and not row_flags.get("drop")


__all__ = ["constraint", "check", "country_name", "established", "infer_state"]
