
from __future__ import annotations

from typing import Any, Mapping

BASE = 0.1198
PAYS = 0.08

INDUSTRY = {
    "Artificial Intelligence":   0.2440,
    "Payments":                  0.2407,
    "Health Care":               0.2325,
    "Information Technology":    0.1965,
    "Energy":                    0.1939,
    "Data and Analytics":        0.1767,
    "Privacy and Security":      0.1513,
    "Financial Services":        0.1309,
    "Transportation":            0.1302,
    "Commerce and Shopping":     0.1210,
    "Lending and Investments":   0.0828,
    "Sales and Marketing":       0.0787,
    "Software":                  0.0757,
    "Biotechnology":             0.0726,
    "Advertising":               0.0708,
    "Education":                 0.0393,
    "Manufacturing":             0.0156,
    "Professional Services":     0.0154,
    "Hardware":                  0.0054,
    "Real Estate":               0.0013,
}

COMPANY_STAGE = {
    "Public":                    0.2239,
    "Series B":                  0.1364,
    "Series C+":                 0.1226,
    "Seed":                      0.0999,
    "Series A":                  0.0919,
    "Private Equity":            0.0255,
}

INTENT_CATEGORY = {
    "REGULATORY_CLEARANCE":      0.1915,
    "LEADERSHIP_CHANGE":         0.1844,
    "FUNDING":                   0.1695,
    "PRODUCT_LAUNCH":            0.1501,
    "PARTNERSHIP":               0.1042,
    "HIRING":                    0.0582,
    "MARKET_EXPANSION":          0.0543,
    "FACILITY_OPENING":          0.0470,
    "ACQUISITION":               0.0088,
}


def _lift(table: Mapping[str, float], value: Any) -> float:
    rate = table.get(" ".join(str(value or "").split()))
    return (rate / BASE) if rate and BASE else 1.0


def prior(icp: Mapping[str, Any]) -> float:

    if not isinstance(icp, Mapping):
        return BASE
    value = BASE
    value *= _lift(INDUSTRY, icp.get("industry"))
    value *= _lift(COMPANY_STAGE, icp.get("company_stage"))
    value *= _lift(INTENT_CATEGORY, icp.get("intent_category"))
    return max(0.0, min(1.0, value))


def pays(icp: Mapping[str, Any]) -> bool:

    return prior(icp) >= PAYS


__all__ = ["prior", "pays", "BASE", "PAYS", "INDUSTRY", "COMPANY_STAGE", "INTENT_CATEGORY"]
