
import json
import re
import sys
from urllib.parse import urlsplit

from run_coordination import host_key

REGISTRY_HOSTS = frozenset({
    "annuaire-entreprises.data.gouv.fr", "recherche-entreprises.api.gouv.fr", "pappers.fr", "societe.com",
    "infogreffe.fr", "verif.com", "manageo.fr", "sirene.fr", "bodacc.fr", "northdata.com", "northdata.de",
    "opencorporates.com", "find-and-update.company-information.service.gov.uk", "handelsregister.de",
})
BRAND_SEGMENT = re.compile(r"^(?:nos-)?(?:marques?|brands?|partenaires?|partners?|fournisseurs?|suppliers?)$", re.I)
FIGURE_YEAR = re.compile(
    r"(?:salari|employ|collaborat|effectif|staff|people|personnes|headcount|workforce|mitarbeiter)\w*"
    r"[^.\n]{0,25}?(?:donn[ée]es?\s+|\ben\s+|\bin\s+|\bas of\s+|\bau\s+|\(\s*)(19[5-9]\d|20[0-4]\d)\b", re.I)
ORDER = {"signal": 0, "size": 1, "attribute": 2, "icp": 3}


def _host(url):
    try:
        return host_key(urlsplit(str(url or "")).hostname or "")
    except ValueError:
        return ""


def _homes(company):
    return sorted({h for h in (_host("https://" + str(company.get("domain") or "").strip()), _host(company.get("website"))) if h})


def _registry(host):
    return any(host == name or host.endswith("." + name) for name in REGISTRY_HOSTS)


def _kind(check):
    criterion_id = str(check.get("criterion_id") or "")
    return "size" if criterion_id == "icp:company_size" else criterion_id.split(":")[0]


def _criterion_text(check, criteria):
    saved = criteria.get(check.get("criterion_id")) or {}
    words = str(saved.get("query") or saved.get("label") or check.get("criterion") or check.get("criterion_id") or "")
    return re.sub(r"\s*—\s*", " - ", " ".join(words.split()))


def _label(check, criteria):
    if _kind(check) == "size":
        return "company size"
    words = _criterion_text(check, criteria).split(" ")
    text = " ".join(words[:7]) + ("…" if len(words) > 7 else "")
    return ("signal “" if _kind(check) == "signal" else "“") + text + "”"


def _date(item):
    return str(item.get("event_date") or item.get("date") or "")


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December")


def readable(date):
    parts = str(date).split("-")
    if not parts[0].isdigit() or len(parts[0]) != 4 or any(not p.isdigit() for p in parts[1:]) or len(parts) > 3:
        return str(date)
    if len(parts) == 1 or not 1 <= int(parts[1]) <= 12:
        return parts[0]
    month = MONTHS[int(parts[1]) - 1]
    return f"{int(parts[2])} {month} {parts[0]}" if len(parts) == 3 else f"{month} {parts[0]}"


def _brand_page(url):
    try:
        segments = [s for s in urlsplit(str(url or "")).path.split("/") if s]
    except ValueError:
        return ""
    for index, segment in enumerate(segments):
        if BRAND_SEGMENT.match(segment):
            return _host(url) + "/" + "/".join(segments[:index + 1]) + ("/…" if index + 1 < len(segments) else "")
    return ""


def caveats_for(row, as_of, criteria=None):
    criteria = criteria or {}
    company = row.get("company") if isinstance(row.get("company"), dict) else {}
    homes = _homes(company)
    year_now = int(str(as_of)[:4]) if str(as_of)[:4].isdigit() else None
    checks = sorted((c for c in row.get("qualification_checks") or []
                     if isinstance(c, dict) and c.get("status") == "pass" and c.get("importance") == "required"
                     and _kind(c) in ORDER), key=lambda c: ORDER[_kind(c)])
    notes, captured = [], set()
    for check in checks:
        evidence = [e for e in check.get("evidence") or [] if isinstance(e, dict)]
        hosts = sorted({_host(e.get("url")) for e in evidence} - {""})
        parts = []
        if _kind(check) == "signal":
            for item in evidence[:1]:
                date = _date(item)
                if not date:
                    parts.append("undated")
                elif item.get("date_basis") == "observed_current" and not item.get("event_date"):
                    parts.append(f"dated only by capture, as of {readable(date)}")
                elif item.get("date_basis") == "observed_current" and item.get("event_date") == item.get("date"):
                    parts.append(f"current as of {readable(date)}")
                elif item.get("date") and item.get("event_date") == str(as_of) and str(item["date"]) < str(as_of):
                    date = str(item["date"])
                    parts.append(f"current as of {readable(as_of)}, {item.get('date_basis') or 'dated'} {readable(date)}")
                else:
                    parts.append(("dated " if item.get("event_date") else "dated by ") + readable(date)
                                 + (" (year only)" if len(date) == 4 else ""))
                if year_now and date[:4].isdigit() and year_now - int(date[:4]) >= 2:
                    parts.append(f"{year_now - int(date[:4])} years before the run")
        elif _kind(check) == "size":
            if hosts and all(h == "linkedin.com" or h.endswith(".linkedin.com") for h in hosts):
                parts.append("LinkedIn band, undated")
            else:
                years = sorted({m.group(1) for e in evidence for m in FIGURE_YEAR.finditer(str(e.get("text") or ""))})
                parts.append(f"figure dated {', '.join(years)}" if years else "no year beside the figure")
        elif evidence and all(e.get("date_basis") == "observed_current" and not e.get("event_date") for e in evidence):
            captured.add(_date(evidence[0]))
        if (_kind(check) != "size" and check.get("criterion_id") != "icp:geographies" and hosts
                and not any(h == home or h.endswith("." + home) for h in hosts for home in homes)):
            if all(_registry(h) for h in hosts):
                parts.append("shown only by a registry record (" + ", ".join(hosts) + ")")
            else:
                parts.append(f"not on {' or '.join(homes) or 'the company website'} (" + ", ".join(hosts) + ")")
        brand = sorted({page for page in (_brand_page(e.get("url")) for e in evidence) if page})
        if brand:
            parts.append("brand or partner page (" + ", ".join(brand) + ")")
        if parts:
            notes.append(f"{_label(check, criteria)}: " + ", ".join(parts))
    if captured:
        notes.append("current-page passages are dated only by capture (as of " + ", ".join(readable(d) for d in sorted(captured)) + ")")
    return "Caveats: " + "; ".join(notes) + "." if notes else ""


def caveats(document):
    as_of = (document.get("request") or {}).get("as_of_date") or str(document.get("retrieved_at") or "")[:10]
    criteria = {c.get("id"): c for c in document.get("criteria") or [] if isinstance(c, dict)}
    return {str(row["company"].get("domain") or "").strip(): caveats_for(row, as_of, criteria)
            for row in document.get("accepted") or [] if isinstance(row, dict) and isinstance(row.get("company"), dict)}


if __name__ == "__main__":
    json.dump(caveats(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
