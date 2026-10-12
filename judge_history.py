"""What the judge decided about a company before, from the published results of earlier rounds (v98).

Every scored entry's company diagnostics are public (``/arena/v1/rounds/<round>/results/<submission>``).  Many entries
send the same companies, so one round judges a company up to fifty times: the verdicts are stable per company and
ICP stage (10-05 + 10-07 Public ICPs: MongoDB qualified 44 of 51, Dynatrace 6 of 36, Palantir 0 of 21, Cloudflare
0 of 17; 10-07 Series A: OneByZero 30 of 34, Pints AI 0 of 20 - wrong stage).  ``judge_prior.json`` holds the counts
per ``<company key>|<stage>`` with a per-industry breakdown; it is rebuilt before each submission.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SUFFIXES = {"inc", "corp", "corporation", "co", "ltd", "llc", "plc", "limited", "holdings", "ai", "technologies",
             "technology", "group", "labs", "software", "systems"}
MIN_SENT = 4            # fewer verdicts say nothing
WITHHOLD_SENT = 5       # (almost) never qualified in this many verdicts, and penalised or wrong-stage: not sent beside another row
# (five of five penalised: S&P Global and Galatek on 10-07, each sent again by v98 for -10)
WITHHOLD_MAX_Q_RATE = 0.1   # Nexl on 10-07 ICP9: 1 of 20 qualified, 16 penalised - a row worth about -5 points
# (v100) A company the judge qualifies now and then but penalises far more often is worth less than nothing: Saviynt
# on the 10-09 Series B launch ICP, 2 qualified of 16 sent, 14 penalised -> 2/16 x 54 - 14/16 x 10 = -2.0 a row.
EV_SENT = 6                 # verdicts needed before the expected value counts
EV_REWARD = 54.0            # a verified primary signal on a press / news host (51 on the company's own host)
EV_PENALTY = 10.0           # a failed check or an unproven required attribute
_PRIOR: Optional[dict[str, Any]] = None


def company_key(name: Any) -> str:
    words = re.findall(r"[a-z0-9]+", str(name or "").casefold())
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return "".join(words)


def load(path: str = "") -> dict[str, Any]:
    global _PRIOR
    if _PRIOR is None or path:
        try:
            with open(path or os.path.join(_HERE, "judge_prior.json"), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            entries = data.get("entries") if isinstance(data, dict) else None
            found = entries if isinstance(entries, dict) else {}
        except (OSError, ValueError):
            found = {}
        if path:
            return found
        _PRIOR = found
    return _PRIOR


def record(name: Any, icp: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """{sent, q, pen, stage_u, same_industry} for this company at the ICP's stage, or None when unseen (< MIN_SENT)."""

    stage = " ".join(str(icp.get("company_stage") or "").split()).lower()
    item = load().get(f"{company_key(name)}|{stage}")
    if not isinstance(item, Mapping) or int(item.get("sent") or 0) < MIN_SENT:
        return None
    industry = " ".join(str(icp.get("industry") or "").split())
    ind = item.get("ind") if isinstance(item.get("ind"), Mapping) else {}
    return {"sent": int(item.get("sent") or 0), "q": int(item.get("q") or 0), "pen": int(item.get("pen") or 0),
            "stage_u": int(item.get("stage_u") or 0), "same_industry": bool(industry) and industry in ind}


def adjust(q: float, r: float, name: Any, icp: Mapping[str, Any]) -> tuple[float, float]:
    """The model's (q, r) blended with the judge's record: weight 0.5 from four verdicts, 0.8 from ten, scaled by
    0.7 when the record comes from another industry (the attribute text differs there)."""

    rec = record(name, icp)
    if rec is None:
        return q, r
    sent = rec["sent"]
    q_hist = (rec["q"] + 1.0) / (sent + 2.0)
    misses = sent - rec["q"]
    r_hist = (rec["pen"] + 1.0) / (misses + 2.0) if misses > 0 else r
    weight = min(0.8, 0.5 + 0.3 * (sent - MIN_SENT) / 6.0) * (1.0 if rec["same_industry"] else 0.7)
    return (1.0 - weight) * q + weight * q_hist, (1.0 - weight) * r + weight * r_hist


def withhold_reason(name: Any, icp: Mapping[str, Any]) -> str:
    """Why this row should not be sent beside another ('' when it may): the judge qualified the company at this stage
    in at most a tenth of WITHHOLD_SENT or more verdicts and either penalised it half the time or found it at another stage."""

    rec = record(name, icp)
    if rec is None or rec["sent"] < WITHHOLD_SENT:
        return ""
    if rec["q"] <= WITHHOLD_MAX_Q_RATE * rec["sent"]:
        if rec["stage_u"] >= 0.8 * rec["sent"]:
            return f"judge found another stage {rec['stage_u']} of {rec['sent']} times"
        if rec["pen"] >= 0.5 * rec["sent"]:
            return f"judge penalised {rec['pen']} of {rec['sent']} rows, none qualified"
    if rec["sent"] >= EV_SENT:
        ev = (rec["q"] * EV_REWARD - rec["pen"] * EV_PENALTY) / rec["sent"]
        if ev < 0:
            return (f"expected value {ev:.1f} a row: {rec['q']} of {rec['sent']} qualified, "
                    f"{rec['pen']} penalised")
    return ""


def strong(name: Any, icp: Mapping[str, Any]) -> bool:
    """Qualified in at least half of MIN_SENT or more verdicts at this stage."""

    rec = record(name, icp)
    return rec is not None and rec["q"] * 2 >= rec["sent"]


__all__ = ["company_key", "load", "record", "adjust", "withhold_reason", "strong", "MIN_SENT", "WITHHOLD_SENT", "EV_SENT"]
