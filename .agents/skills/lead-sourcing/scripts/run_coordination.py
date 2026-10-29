"""Small, local coordination layer for workers sharing one sourcing run.

OS locks serialize short state changes and limit concurrent provider calls.
They do not replace receipt recovery or expire company ownership on a timer.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import unicodedata
from urllib.parse import urlsplit


_locks = {}
_registry_lock = threading.Lock()
_held = threading.local()
_worker_context = ContextVar("tyche_worker", default=None)


@contextmanager
def worker_context(run_file, worker, generation):
    token = _worker_context.set((run_file, worker, generation) if worker else None)
    try:
        yield
    finally:
        _worker_context.reset(token)


def check_current_worker():
    current = _worker_context.get()
    if current:
        run_file, worker, generation = current
        check_worker(snapshot(run_file), worker, generation)


def _os_lock(fd, blocking=True):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        except PermissionError:
            if blocking:
                raise
            raise BlockingIOError("This run is already owned by another invocation") from None
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def _unlock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Coordination lock must be a regular file")
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"0")
    return fd


@contextmanager
def locked(run_file, name="state", *, blocking=True):
    """Reentrant across local threads, exclusive across worker processes."""
    directory = Path(run_file).resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (".tyche-" + hashlib.sha256(name.encode()).hexdigest()[:16] + ".guard")
    key = str(path)
    with _registry_lock:
        mutex = _locks.setdefault(key, threading.RLock())
    if not mutex.acquire(blocking=blocking):
        raise BlockingIOError("This run is already owned by another invocation")
    try:
        held = getattr(_held, "keys", set())
        if key in held:
            yield
            return
        fd = _open_lock(path)
        try:
            _os_lock(fd, blocking=blocking)
            try:
                _held.keys = held | {key}
                yield
            finally:
                _held.keys = held
                _unlock(fd)
        finally:
            os.close(fd)
    finally:
        mutex.release()


@contextmanager
def provider_slot(run_file, limit=3):
    """A global limit, not three new slots for each model worker."""
    directory = Path(run_file).resolve().parent
    acquired = None
    started = time.monotonic()
    while acquired is None:
        for index in range(limit):
            fd = _open_lock(directory / f".tyche-provider-{index}.guard")
            try:
                _os_lock(fd, blocking=False)
                acquired = fd
                break
            except (BlockingIOError, PermissionError):
                os.close(fd)
        if acquired is None:
            if time.monotonic() - started > 900:
                raise TimeoutError("Provider slots busy; no call dispatched")
            time.sleep(.05)
    try:
        yield
    finally:
        _unlock(acquired)
        os.close(acquired)


def state_path(run_file):
    return Path(run_file).with_name(Path(run_file).name + ".workers.json")


def snapshot(run_file):
    from budget_guard import read_object
    path = state_path(run_file)
    with locked(run_file):
        state = read_object(path) if path.exists() else None
        if state is not None:
            validate_state(state, run_file)
        return state


def update(run_file, change):
    from budget_guard import transaction
    with transaction(state_path(run_file)) as state:
        if state:
            validate_state(state, run_file)
        change(state)


def validate_state(state, run_file=None):
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("run_file"), str)
            or type(state.get("worker_count")) is not int or not 1 <= state["worker_count"] <= 3
            or state.get("phase") not in {"research", "finalization", "blocked"}
            or type(state.get("ready")) is not bool
            or type(state.get("conflicts")) is not int
            or any(not isinstance(state.get(key), dict) for key in ("workers", "claims", "aliases"))):
        raise ValueError("Missing or invalid worker coordination state; preserve the saved run and recover its original state before resuming")
    if run_file is not None and state["run_file"] != str(Path(run_file).resolve()):
        raise ValueError("Worker coordination belongs to a different run; preserve its original state and claims")
    for row in state["workers"].values():
        if not isinstance(row, dict) or not isinstance(row.get("generation"), str) or row.get("status") not in {"running", "stopped"}:
            raise ValueError("Invalid saved worker invocation; preserve coordination state for recovery")
    for key, row in state["claims"].items():
        if (not isinstance(row, dict) or row.get("worker") not in state["workers"]
                or row.get("status") not in {"active", "accepted", "rejected"}
                or not isinstance(row.get("aliases"), list)
                or any(not isinstance(alias, str) or state["aliases"].get(alias) != key for alias in row["aliases"])):
            raise ValueError("Invalid saved company ownership; preserve coordination state for recovery")
    if any(not isinstance(key, str) or not isinstance(value, str) or value not in state["claims"]
           or key not in state["claims"][value]["aliases"] for key, value in state["aliases"].items()):
        raise ValueError("Invalid saved company aliases; preserve coordination state for recovery")
    if state.get("serial_worker") is not None and state["serial_worker"] not in state["workers"]:
        raise ValueError("Serial research owner is not a configured worker")
    for worker, row in state["workers"].items():
        target = row.get("current_company")
        if target is not None and (not isinstance(target, str) or state["claims"].get(target, {}).get("worker") != worker):
            raise ValueError("Invalid current company; preserve coordination state for recovery")


def configure(run_file, count):
    existing = state_path(run_file).exists()
    def initialize(state):
        if existing or state:
            validate_state(state)
            if state.get("run_file") != str(Path(run_file).resolve()) or state.get("worker_count") != count:
                raise ValueError("Resume the original worker count and run; do not reset company claims")
            return
        state.update(version=1, run_file=str(Path(run_file).resolve()), worker_count=count,
                     phase="research", ready=False, workers={}, claims={}, aliases={}, conflicts=0)
    update(run_file, initialize)


def register(run_file, worker, generation):
    def assign(state):
        state["workers"].setdefault(worker, {}).update(generation=generation, status="running")
        # Recover a crash between the saved outcome/confirmation and slot release.
        current = state["workers"][worker].get("current_company")
        if current and Path(run_file).exists():
            from budget_guard import read_object
            from confirmed_leads import read
            from validate_run import _company_key
            document = read_object(run_file)
            rejected = any(_company_key(row) == current for row in document.get("rejected", []))
            accepted = next((row for row in document.get("accepted", []) if _company_key(row) == current), None)
            confirmed = accepted is not None and accepted in read(run_file, document)["leads"]
            if rejected or confirmed:
                state["claims"][current]["status"] = "rejected" if rejected else "accepted"
                state["workers"][worker]["current_company"] = None
    update(run_file, assign)


def check_worker(state, worker, generation):
    validate_state(state)
    current = state.get("workers", {}).get(worker, {})
    if (state.get("phase") != "research" or current.get("generation") != generation
            or current.get("status") != "running" or current.get("disabled")):
        raise ValueError("This worker no longer owns an active research invocation; no research dispatched")


# Sites where many companies each have a page: such a page is not a company's own website.
PLATFORM_HOSTS = ("facebook.com", "fb.com", "fb.me", "m.me", "instagram.com", "x.com", "twitter.com", "tiktok.com",
                  "youtube.com", "youtu.be", "linktr.ee", "lnkd.in", "g.page", "goo.gl", "pagesjaunes.fr",
                  "sites.google.com", "societe.com", "pappers.fr", "annuaire-entreprises.data.gouv.fr", "northdata.com",
                  "northdata.de", "kompass.com", "crunchbase.com", "yelp.com", "yelp.fr", "yelp.co.uk",
                  "find-and-update.company-information.service.gov.uk")
# A careers or jobs section of a company's own site, or any two-letter (country, language or similar) section, keys
# the company by the site's registrable domain, as its website and mail do (test10/04s keyed mycareer.verizon.com, 08b
# de.erwinmueller.com).
SITE_SECTION = re.compile(r"(?:my)?(?:careers?|jobs?)|karriere|stellen|emplois?|empleos?|recrutement|rejoins|vacatures"
                          r"|werkenbij|[^\W\d_]{2}")
# Hosting and recruiting platforms give each customer, or each customer's job pages, a subdomain or path of their own.
SHARED_HOSTS = ("netlify.app", "vercel.app", "github.io", "gitlab.io", "herokuapp.com", "pages.dev", "web.app",
                "firebaseapp.com", "azurewebsites.net", "appspot.com", "wordpress.com", "blogspot.com", "wixsite.com",
                "squarespace.com", "webflow.io", "myshopify.com", "weebly.com", "jimdosite.com", "godaddysites.com",
                "business.site", "hubs.ly", "notion.site", "carrd.co", "myworkdayjobs.com", "workday.com", "lever.co",
                "greenhouse.io", "smartrecruiters.com", "ashbyhq.com", "jobvite.com", "workable.com", "recruitee.com",
                "teamtailor.com", "personio.de", "personio.com", "bamboohr.com", "breezy.hr", "icims.com",
                "applytojob.com", "zohorecruit.com", "taleo.net", "successfactors.com", "successfactors.eu",
                "ultipro.com", "dayforcehcm.com", "indeed.com", "glassdoor.com", "stepstone.de",
                "welcometothejungle.com")
# Second-level labels of country suffixes (example.co.uk, example.com.au): never a registrable domain alone.
SUFFIX_SECOND = {"co", "com", "net", "org", "gov", "edu", "ac", "ne", "or", "go", "gob", "gouv", "gv", "nic", "mil",
                 "ltd", "plc", "sch", "me", "nom", "info", "biz", "asso", "nhs", "police", "govt", "asn", "ind", "adv",
                 "eng", "firm", "gen", "res"}


def _site_domain(host):
    """host without its leading careers, jobs or country labels, while what remains is a registrable domain that is
    not a shared host."""
    while True:
        first, _, rest = host.partition(".")
        labels = rest.split(".")
        if (not SITE_SECTION.fullmatch(first) or len(labels) < 2
                or len(labels[-1]) == 2 and labels[0] in SUFFIX_SECOND
                or len(labels) == 2 and len(first) == len(labels[0]) == len(labels[1]) == 2
                or any(rest == shared or rest.endswith("." + shared) for shared in SHARED_HOSTS)):
            return host
        host = rest


def host_key(host):
    """One form for a host: lower case, no www. or trailing dot, internationalized labels decoded (NFC)."""
    labels = []
    for label in str(host).strip().lower().removeprefix("www.").rstrip(".").split("."):
        if label.startswith("xn--"):
            try:
                label = label[4:].encode("ascii").decode("punycode")
            except UnicodeError:
                pass
        labels.append(unicodedata.normalize("NFC", label))
    return ".".join(labels)


def company_key(value):
    """The one company key for claims and saved rows: the website host in any spelling, or a LinkedIn
    company page. A name, a person profile or a page on a social or directory site is refused."""
    value = str(value).strip()
    parsed = urlsplit(value if "://" in value else "https://" + value)
    host = host_key(parsed.hostname or "")
    if parsed.username or parsed.password or not re.fullmatch(r"[\w.-]+\.(?:[^\W\d_]{2,}|xn--[a-z0-9-]+)", host):
        raise ValueError("Claim a company by its website domain or verified LinkedIn company URL, not its name")
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        match = re.fullmatch(r"/company/([^/]+)/?", parsed.path)
        if not match:
            raise ValueError("Use a LinkedIn company URL, not a person profile")
        return "linkedin.com/company/" + match[1].lower()
    maps = re.fullmatch(r"(?:[\w-]+\.)*google\.[a-z.]+", host) and parsed.path.startswith("/maps")
    if maps or any(host == platform or host.endswith("." + platform) for platform in PLATFORM_HOSTS):
        raise ValueError("A page on a social or directory site is not the company's website; use its own domain")
    return _site_domain(host)


class WorkerYield(ValueError):
    """This worker finished its company after concurrency was reduced."""


def refresh_pacing(run_file, *, reconcile=None):
    """Drain at 80%; restore parallel work below 70% after settlement."""
    from budget_guard import load_ledger, accounting_summary
    ledger = load_ledger(run_file)
    totals = accounting_summary(ledger)
    spent = (totals.get("budget_total_usd", totals["total_usd"]) if ledger["version"] == 2 else
             sum(v["maximum_usd"] for v in totals["providers"].values()))
    if spent >= float(ledger["usd_limit"]) * .8 and reconcile:
        reconcile(run_file)  # Outside all state locks; only posted evidence releases a reservation.
        ledger = load_ledger(run_file)
        totals = accounting_summary(ledger)
        spent = (totals.get("budget_total_usd", totals["total_usd"]) if ledger["version"] == 2 else
                 sum(v["maximum_usd"] for v in totals["providers"].values()))
    def adjust(state):
        if state.get("serial_worker") and spent < float(ledger["usd_limit"]) * .7:
            state.setdefault("pacing_history", []).append({"serial_at": state.get("serial_at"),
                "serial_spend_usd": state.get("serial_spend_usd"), "resumed_at": datetime.now(timezone.utc).isoformat(),
                "resumed_spend_usd": spent})
            for key in ("serial_worker", "serial_at", "serial_spend_usd"):
                state.pop(key, None)
        if not state.get("serial_worker") and spent >= float(ledger["usd_limit"]) * .8:
            eligible = [key for key, row in state["workers"].items() if row.get("status") == "running" and not row.get("disabled")]
            if eligible:
                state.update(serial_worker=min(eligible), serial_at=datetime.now(timezone.utc).isoformat(),
                             serial_spend_usd=spent)
        if state.get("serial_worker") and (state["workers"][state["serial_worker"]].get("disabled") or state["workers"][state["serial_worker"]].get("status") != "running"):
            eligible = [key for key, row in state["workers"].items() if row.get("status") == "running" and not row.get("disabled")]
            if eligible:
                state["serial_worker"] = min(eligible)
    update(run_file, adjust)


def should_yield(state, worker):
    return bool(state.get("serial_worker") and state["serial_worker"] != worker
                and not state["workers"][worker].get("current_company"))


def _focus(state, worker, target):
    current = state["workers"][worker].get("current_company")
    if should_yield(state, worker):
        raise WorkerYield("Near the shared budget cutoff, this worker has finished its company. End this invocation now; the remaining worker continues. Do not poll or start another company.")
    if current and current != target:
        raise ValueError(f"Finish current company {current} before starting another company or broad discovery. "
                         "Accept and confirm it, reject an evidenced mismatch, or save hold_account with the "
                         "specific missing evidence and why work cannot proceed.")
    state["workers"][worker]["current_company"] = target


def resume_abandoned(run_file, worker, generation):
    """Adopt one retired worker's company; never steal live work or an unresolved dispatch."""
    from budget_guard import read_object
    def adopt(state):
        check_worker(state, worker, generation)
        if state["workers"][worker].get("current_company") or should_yield(state, worker):
            return
        document = read_object(Path(run_file))
        recorded = {r["route_id"] for r in document.get("routes", [])}
        pending = {r.get("scope") for r in document.get("stop_audit", {}).get("route_frontier", [])
                   if r["route_id"] not in recorded}
        for target, claim in state["claims"].items():
            old = state["workers"][claim["worker"]]
            if (claim["status"] != "active" or not old.get("disabled") or old.get("status") != "stopped"
                    or pending.intersection([target, *claim["aliases"]])):  # A moved claim keeps its old key's lookups.
                continue
            claim.setdefault("previous_owners", []).append(claim["worker"])
            claim.update(worker=worker, reassigned_at=datetime.now(timezone.utc).isoformat())
            if old.get("current_company") == target:
                old["current_company"] = None
            state["workers"][worker]["current_company"] = target
            break
    update(run_file, adopt)


def require_discovery(run_file, worker, generation):
    resume_abandoned(run_file, worker, generation)
    state = snapshot(run_file)
    check_worker(state, worker, generation)
    _focus(state, worker, None)  # Read-only check; discovery does not own a company.


def claim(run_file, worker, generation, target, aliases=(), *, allow_owned_complete=False, rekey_from=()):
    """rekey_from: claim keys the caller found may move to target (research_tools checks that the company's own saved
    LinkedIn record names target as its website and that nothing is saved under the old key)."""
    if Path(run_file).exists() and not allow_owned_complete:
        resume_abandoned(run_file, worker, generation)
    keys = list(dict.fromkeys(company_key(value) for value in (target, *aliases) if value))
    result = {}
    def reserve(state):
        check_worker(state, worker, generation)
        matches = {state["aliases"][key] for key in keys if key in state["aliases"]}
        if not matches and keys[0].startswith("linkedin.com/"):
            result.update(claimed=False, status="domain_required", target=keys[0],
                          next="Find the company's website domain in discovery, then claim that domain with company_url as its LinkedIn alias.")
            return
        for canonical in matches:
            row = state["claims"][canonical]
            if row["worker"] != worker or not allow_owned_complete and row.get("status") in {"accepted", "rejected"}:
                state["conflicts"] += 1
                result.update(claimed=False, target=canonical, owner=row["worker"], status=row["status"])
                return
        canonical = sorted(matches)[0] if matches else keys[0]
        # The explicit target becomes the key of this worker's open claim, with its aliases and focus; otherwise every
        # review under the old key refuses the company's own record as another company's.
        current = state["workers"][worker].get("current_company")
        previous = canonical if matches else current
        if (previous in rekey_from and matches <= {previous} and current in {None, previous} and keys[0] != previous
                and not allow_owned_complete and not keys[0].startswith("linkedin.com/")
                and state["claims"][previous]["worker"] == worker and state["claims"][previous]["status"] == "active"):
            moved = state["claims"][keys[0]] = state["claims"].pop(previous)
            moved["rekeyed_from"] = [key for key in dict.fromkeys([*moved.get("rekeyed_from", []), previous])
                                     if key != keys[0]]  # A claim moved back drops its own key.
            if current == previous:
                state["workers"][worker]["current_company"] = keys[0]
            result["rekeyed_from"] = previous
            matches, canonical = {keys[0]}, keys[0]
        if not allow_owned_complete:
            _focus(state, worker, canonical)
        row = state["claims"].setdefault(canonical, {"worker": worker, "status": "active",
            "claimed_at": datetime.now(timezone.utc).isoformat(), "aliases": []})
        for old in matches - {canonical}:
            row["aliases"].extend(state["claims"].pop(old)["aliases"])
            if state["workers"][worker].get("current_company") == old:
                state["workers"][worker]["current_company"] = canonical
        row["aliases"] = sorted(set(row["aliases"] + keys))
        for key in row["aliases"]:
            state["aliases"][key] = canonical
        result.update(claimed=True, target=canonical, owner=worker, status=row["status"])
    update(run_file, reserve)
    return result


def require_claim(run_file, worker, generation, target, aliases=(), *, focus=False):
    state = snapshot(run_file)
    check_worker(state, worker, generation)
    key = company_key(target)
    canonical = state["aliases"].get(key)
    row = state["claims"].get(canonical, {})
    if row.get("worker") != worker:
        raise ValueError("Claim this company with tyche_claim before research; it is unclaimed or owned by another worker")
    if aliases:
        result = claim(run_file, worker, generation, target, aliases, allow_owned_complete=True)
        if not result["claimed"]:
            raise ValueError("Company alias already belongs to " + result["owner"] + "; skip duplicate company research")
        canonical = result["target"]
    if focus:
        def activate(state):
            check_worker(state, worker, generation)
            _focus(state, worker, canonical)
        update(run_file, activate)
    return canonical


def reviewed(run_file, worker, generation, target, decision):
    def mark(state):
        check_worker(state, worker, generation)
        canonical = state["aliases"].get(company_key(target))
        if canonical:
            if state["claims"][canonical]["worker"] != worker:
                raise ValueError("Another worker owns this company")
            state["claims"][canonical]["status"] = {"accept": "accepted", "reject": "rejected"}.get(decision, "active")
            if decision == "confirm":
                state["claims"][canonical]["status"] = "accepted"
            if (decision in {"hold_account", "reject", "confirm"}
                    and state["workers"][worker].get("current_company") == canonical):
                state["workers"][worker]["current_company"] = None
    update(run_file, mark)
