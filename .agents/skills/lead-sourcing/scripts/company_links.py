"""A company's LinkedIn page, bound by code only from the company's own captured website.

DEMOS finds a company's people through its LinkedIn company page, and a page that only a search result or a company
database names can be a namesake. So code saves company.linkedin_url, exactly as captured, only when pages this run
captured on the company's own domain or website, including where a capture was redirected, name one LinkedIn company
page: in the sameAs list of the site's own JSON-LD organization at its root, or as a link in the page text on the
homepage or on at least two own pages (a site-wide footer or header). A link on one inner page may name a partner, and
two different pages leave it empty. The link it saves identifies no other record; only the HarvestAPI getter's own
page (company.ref, a separate and unchanged path) does.

usage: company_links.py --derive <run dir> <new output dir>
  Read-only for the run: writes a labelled leads.json with only these two fields added, and hashes the run folder
  before and after. The run must be in its saved location: its receipts are bound to it, so a copy is refused.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import deepline
from linkedin_receipts import _entity
from run_coordination import host_key
from source_receipts import content_kind, read_receipt

# A company slug can hold an apostrophe, an ampersand or a letter beyond ASCII (test10/05r saved /company/lowe from
# /company/lowe's-home-improvement/). An ampersand that starts an HTML entity (&amp; &#39;) or a query parameter
# (&trk=) is not the slug's. A slug never ends in an apostrophe. It ends in a full stop only before a / ? or #, or at
# the end of a markdown link target, which runs to its closing ) or its title, or of a JSON-LD sameAs value (test10/03s
# saved /company/car-leasing-made-simple from [..](…/company/car-leasing-made-simple.)). A closing quote, a sentence
# end or a plain parenthetical stays out.
_SLUG = r"(?:[\w%~.'-]|&(?![\w#]+[;=]))"
_COMPANY = r"https?://(?:[a-z0-9-]+\.)*linkedin\.com/company/"
LINK = re.compile(rf"(?<=\]\(){_COMPANY}{_SLUG}*\.(?=[)\s])|{_COMPANY}(?:{_SLUG}*\.(?=[/?#])|{_SLUG}*[\w%~-])/?",
                  re.IGNORECASE)
PASSAGE_LIMIT = 1000
# JSON-LD types a site uses to describe the business itself.
ORGANIZATION = {"organization", "corporation", "localbusiness", "store", "onlinestore", "onlinebusiness"}
_PAGES = {}  # {run file: {route_id: pages}}; a complete receipt never changes.


def _host(url):
    try:
        return host_key(urlsplit(str(url or "")).hostname or "")
    except ValueError:
        return ""


def _homes(company):
    """The company's own website hosts: its key domain and its saved website."""
    return {h for h in (_host("https://" + str(company.get("domain") or "").strip()), _host(company.get("website"))) if h}


def _own(url, homes):
    host = _host(url)
    return bool(host) and any(host == home or host.endswith("." + home) for home in homes)


def _page_urls(row):
    """Every URL a capture reports: the one requested and where it was redirected."""
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    urls = [row.get("evidence_url"), row.get("url")] + [metadata.get(k) for k in ("sourceURL", "sourceUrl", "url", "finalUrl")]
    return [url for url in urls if isinstance(url, str) and url]


def _final_url(row):
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return next((url for url in (metadata.get("finalUrl"), metadata.get("url"), row.get("url"), row.get("evidence_url"))
                 if isinstance(url, str) and url), "")


def _own_page(row, homes):
    return bool(_page_urls(row)) and all(_own(url, homes) for url in _page_urls(row))


def _root(url, homes):
    """The company's own site root: its domain or website host itself, with no path or query."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    return _host(url) in homes and parts.path in ("", "/") and not parts.query


def _page_key(url):
    return _host(url), urlsplit(url).path.rstrip("/")


def captured_rows(saved):
    """A receipt's captured pages as (result index, row), normalized again from the raw provider response."""
    if not any(isinstance(r, dict) and content_kind(r, saved) == "captured_page" for r in saved.get("results") or []):
        return []  # Search results, records and observations never bind; skip re-normalizing them.
    normalized = saved
    if saved.get("provider") == "deepline":
        normalized, _ = deepline.normalize_response(saved["attempt"]["request"], saved["provider_response"])
    return [(index, row) for index, row in enumerate(normalized.get("results") or [])
            if isinstance(row, dict) and content_kind(row, saved) == "captured_page"]


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, dict):
        yield value
        yield from _nodes(value.get("@graph"))


def page_links(row, homes):
    """(link exactly as captured, passage, structured) for each LinkedIn company page a captured page names."""
    text = row.get("evidence_text") or row.get("text")
    text = text if isinstance(text, str) else ""
    for match in LINK.finditer(text):
        start = max(text.rfind("\n", 0, match.start()) + 1, match.start() - 300)
        end = text.find("\n", match.end())
        end = min(len(text) if end < 0 else end, match.end() + 200)
        yield match.group(0), text[start:end].strip(), False
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for node in _nodes(metadata.get("jsonLd")):
        same = node.get("sameAs")
        same = [same] if isinstance(same, str) else same if isinstance(same, list) else []
        # A sameAs value is one whole URL, so its end closes the slug as a markdown target's ) does.
        links = [m.group(0) for m in (LINK.match(f"]({s})", 2) for s in same if isinstance(s, str)) if m]
        kinds = node.get("@type") if isinstance(node.get("@type"), list) else [node.get("@type")]
        if (not links or not ORGANIZATION & {str(kind).casefold() for kind in kinds}
                or not any(_root(node.get(key), homes) for key in ("url", "@id"))):
            continue  # Only the site's own organization, at its root, speaks for the company.
        record = {key: node[key] for key in ("@type", "@id", "name", "url") if key in node}
        passage = json.dumps({**record, "sameAs": same}, ensure_ascii=False)
        if len(passage) > PASSAGE_LIMIT:
            passage = json.dumps({**record, "sameAs": links}, ensure_ascii=False)
        for link in links:
            yield link, passage, True


def bind(company, pages, as_of):
    """{linkedin_url, linkedin_url_evidence} when the company's own captured pages name exactly one company page.

    pages: (source, result index, row) for this run's captured pages, in capture order.
    """
    homes, found = _homes(company), {}
    for source, index, row in pages:
        if not _own_page(row, homes):
            continue
        final = _final_url(row)
        homepage = _root(final, homes)
        for link, passage, structured in page_links(row, homes):
            entry = found.setdefault(_entity(link, "company"), {"pages": set(), "proof": None})
            entry["pages"].add(_page_key(final))
            # Prefer the site-wide proof, then the shortest page path.
            rank = (not (structured or homepage), len(urlsplit(final).path.strip("/")))
            if entry["proof"] is None or rank < entry["proof"][0]:
                entry["proof"] = (rank, link, row.get("evidence_url") or row.get("url"), passage, source, index)
    if len(found) != 1:
        return None
    entry = next(iter(found.values()))
    (weak, _), link, url, passage, source, index = entry["proof"]
    if weak and len(entry["pages"]) < 2:
        return None
    # The link is what the page showed when captured, whatever the page's own publication date.
    return {"linkedin_url": link, "linkedin_url_evidence": {
        "evidence_url": url, "evidence_text": passage, "evidence_date": as_of, "evidence_date_basis": "observed_current",
        "source": {**source, "result_index": index}}}


def _kinds(node):
    kinds = node.get("@type") if isinstance(node.get("@type"), list) else [node.get("@type")]
    return {str(kind).casefold() for kind in kinds}


def _root_organization(row, homes):
    """Whether a captured page declares an organization at the site's own root, whatever its links."""
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return any(ORGANIZATION & _kinds(node) and any(_root(node.get(key), homes) for key in ("url", "@id"))
               for node in _nodes(metadata.get("jsonLd")))


def _site_root(url):
    """The host of url when url is a site's root, with no path or query; otherwise empty."""
    host = _host(url)
    return host if host and _root(url, {host}) else ""


def _holder_links(row, homes):
    """(LinkedIn page, website host) for the organization a site names as its own copyright holder (at its root, or in
    a WebSite node on its own host, which some generators give the page's own url), when that organization's url or
    @id is the root of that website. A partner the site only describes, the agency it names as creator or publisher
    (rangewaterre.com names one), or the vendor of a help-centre subdomain is not the site's owner."""
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    nodes = list(_nodes(metadata.get("jsonLd")))
    holders = set()
    for node in nodes:
        site = "website" in _kinds(node)
        if any(_root(node.get(key), homes) or (site and _host(node.get(key)) in homes) for key in ("url", "@id")):
            values = node.get("copyrightHolder") if isinstance(node.get("copyrightHolder"), list) else [node.get("copyrightHolder")]
            holders.update(ref for value in values if isinstance(value, dict) for ref in (value.get("@id"), value.get("url"))
                           if isinstance(ref, str))
    for node in nodes:
        hosts = {_site_root(node.get(key)) for key in ("url", "@id")} - {""}
        if holders & {ref for ref in (node.get("@id"), node.get("url")) if isinstance(ref, str)} and ORGANIZATION & _kinds(node):
            same = node.get("sameAs")
            same = [same] if isinstance(same, str) else same if isinstance(same, list) else []
            links = [m.group(0) for m in (LINK.match(f"]({s})", 2) for s in same if isinstance(s, str)) if m]
            yield from ((link, host) for link in links for host in hosts)


def site_pages(domain, pages):
    """{LinkedIn company page: the website hosts it stands for there, or None for any} for each page that pages captured
    on domain's own site name as the company's: its bound page (above), or else the pages of the organizations its site
    names as copyright holder. 2026-10-26 Arena: rangewaterre.com's homepage named liverangewater.com's organization,
    with its LinkedIn page, as its copyright holder. A site that links other pages without binding one, or declares its
    own organization, is its own company, so its copyright holder is a parent or a brand owner and names nothing. A page
    found by a search or guessed from a name is never named here."""
    homes = _homes({"domain": domain})
    own = [page for page in pages if _own_page(page[2], homes)]
    bound = bind({"domain": domain}, own, None)
    if bound:
        return {_entity(bound["linkedin_url"], "company"): None}
    linked = {_entity(found, "company") for _, _, row in own for found, _, _ in page_links(row, homes)}
    if len(linked) > 1 or any(_root_organization(row, homes) for _, _, row in own):
        return {}
    named = {}
    for _, _, row in own:
        for found, host in _holder_links(row, homes):
            page = _entity(found, "company")
            if page and linked <= {page}:
                named.setdefault(page, set()).add(host)
    return named


def names_page(domain, link, operator, pages):
    """Whether pages captured on domain's own site name LinkedIn company page link as the company's, for operator, the
    website domain that page's record names."""
    target = _entity(link, "company") if isinstance(link, str) else None
    named = site_pages(domain, pages)
    if not target or target not in named:
        return False
    return named[target] is None or bool(_homes({"domain": operator}) & named[target])


def record_pages(record, url=None):
    """The spellings of the one LinkedIn company page a getter record answers for: its link, LinkedIn's universal name,
    its numeric id, and the URL it was fetched by (an old name redirects). Sites link all of them: braze.com links
    /company/792882, the record's id, while the record's link is /company/braze/."""
    links = [record.get("company_linkedin_url"), record.get("linkedinUrl"), url]
    links += [f"https://www.linkedin.com/company/{record[key]}/" for key in ("universalName", "id")
              if isinstance(record.get(key), (str, int)) and str(record[key]).strip()]
    return {page for page in (_entity(link, "company") for link in links if isinstance(link, str)) if page}


def run_pages(run_file, routes):
    """This run's captured pages, read from its saved receipts without dispatch."""
    cache = _PAGES.setdefault(str(Path(run_file).resolve()), {})
    for route in routes:
        rid = route.get("route_id") if isinstance(route, dict) else None
        if rid not in cache:
            if not isinstance(route, dict) or route.get("provider_status") not in {"ok", "partial"}:
                continue
            saved = read_receipt(run_file, rid)["result"]
            if saved.get("receipt_status") != "complete":
                continue
            source = {"provider": saved.get("provider"), "operation": saved.get("operation"), "tool": saved.get("tool"),
                      "route_id": rid}
            cache[rid] = [(source, index, row) for index, row in captured_rows(saved)]
        yield from cache[rid]


def _evidence_error(company, run_file, as_of, pages):
    evidence = company.get("linkedin_url_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    saved = read_receipt(run_file, source.get("route_id"))["result"]
    if (saved.get("receipt_status") != "complete" or saved.get("status") not in {"ok", "partial"}
            or any(source.get(k) != saved.get(k) for k in ("provider", "operation", "tool"))):
        return "requires the cited completed page capture from this run"
    row = dict(captured_rows(saved)).get(source.get("result_index"))
    homes = _homes(company)
    if row is None or evidence.get("evidence_url") != (row.get("evidence_url") or row.get("url")) or not _own_page(row, homes):
        return "evidence_url must be the cited capture of a page on the company's own domain or website"
    if (company.get("linkedin_url"), evidence.get("evidence_text")) not in {(l, p) for l, p, _ in page_links(row, homes)}:
        return "the cited page must name company.linkedin_url, exactly as captured, in evidence_text"
    if evidence.get("evidence_date_basis") != "observed_current" or evidence.get("evidence_date") != as_of:
        return "evidence_date must be the run's as-of date, observed_current"
    bound = bind(company, pages, as_of)
    if not bound or _entity(bound["linkedin_url"], "company") != _entity(company.get("linkedin_url"), "company"):
        return ("the company's own captured pages do not name exactly this LinkedIn page; review the company again "
                "so code saves or clears it")
    return None


def evidence_errors(document, run_file):
    """A saved own-site LinkedIn page must be named by the cited capture of the company's own page."""
    errors, pages = [], None
    as_of = (document.get("request") or {}).get("as_of_date")
    for index, row in enumerate(document.get("accepted") or []):
        company = row.get("company") if isinstance(row, dict) else None
        if not isinstance(company, dict) or "linkedin_url_evidence" not in company:
            continue
        try:
            pages = list(run_pages(run_file, document.get("routes") or [])) if pages is None else pages
            error = _evidence_error(company, run_file, as_of, pages)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            error = str(exc)
        if error:
            errors.append(f"accepted[{index}].company.linkedin_url_evidence: {error}")
    return errors


def _folder_hash(folder):
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    listing = "".join(f"{p.relative_to(folder)}\0{hashlib.sha256(p.read_bytes()).hexdigest()}\n" for p in files)
    return hashlib.sha256(listing.encode()).hexdigest(), len(files)


def derive(run_dir, out_dir):
    """Write out_dir/leads.json: the recorded rows with only the two code-checked fields added. The run is only read."""
    run, out = Path(run_dir).resolve(strict=True), Path(out_dir).resolve()
    if out.exists() or out.is_relative_to(run):
        raise ValueError("the output folder must be new and outside the run folder")
    before = _folder_hash(run)
    results_file = run / "results.json"
    document = json.loads(results_file.read_text(encoding="utf-8"))
    recorded = json.loads((run / "leads.json").read_text(encoding="utf-8"))
    accepted = {row["company"]["domain"]: row for row in document.get("accepted", [])}
    pages = list(run_pages(results_file, document.get("routes", [])))
    leads, bound = [], {}
    for row in recorded["leads"]:
        domain = row["company"]["domain"]
        if accepted.get(domain) != row:
            raise ValueError(f"{domain}: the recorded lead differs from results.json; nothing was written")
        row = json.loads(json.dumps(row))
        if not row["company"].get("linkedin_url"):
            fields = bind(row["company"], pages, document["request"]["as_of_date"])
            if fields:
                row["company"].update(fields)
                bound[domain] = fields["linkedin_url"]
        leads.append(row)
    check = {**document, "accepted": leads}
    if errors := evidence_errors(check, results_file):
        raise ValueError("; ".join(errors))
    if _folder_hash(run) != before:
        raise ValueError("the run folder changed during derivation; nothing was written")
    try:
        commit = subprocess.run(["git", "-C", str(Path(__file__).parent), "describe", "--always", "--dirty", "--abbrev=40"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    derived = {**recorded, "leads": leads, "derived_from": {
        "run": str(run), "results_sha256": hashlib.sha256(results_file.read_bytes()).hexdigest(),
        "leads_sha256": hashlib.sha256((run / "leads.json").read_bytes()).hexdigest(),
        "run_folder_sha256": before[0], "run_folder_files": before[1], "code_commit": commit,
        "derived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fields_added": ["company.linkedin_url", "company.linkedin_url_evidence"], "linkedin_url": bound,
        "note": "Derived offline from the run's saved receipts; not a new TYCHE delivery. Rows are the recorded "
                "leads with only these two code-checked fields added."}}
    out.mkdir(parents=True)
    (out / "leads.json").write_text(json.dumps(derived, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    after = _folder_hash(run)
    if after != before:
        shutil.rmtree(out)
        raise ValueError("the run folder changed during derivation; the output was removed")
    return {"out": str(out), "leads_sha256": hashlib.sha256((out / "leads.json").read_bytes()).hexdigest(),
            "run_folder_sha256_before": before[0], "run_folder_sha256_after": after[0], "run_folder_files": after[1],
            "linkedin_url": bound}


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--derive":
        sys.exit(__doc__)
    try:
        print(json.dumps(derive(sys.argv[2], sys.argv[3]), indent=2))
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"derive refused: {exc}")
