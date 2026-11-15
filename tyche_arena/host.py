
from datetime import date, datetime, timedelta, timezone
from collections import Counter
import contextlib
import importlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

from . import ROOT, SKILL
from .broker import DEEPLINE_WAIT_SECONDS
from .input import HIRING, _signal_core, intent_signal_ages, intent_signals, worker_lanes, opening_plan, prepare as prepare_request
from .output import (BONUS_UNREADABLE_HOSTS, HELD_COMPANIES, ICP_FILE, LATER_SIGNAL, MAX_COMPANIES, _LEADERSHIP,
                     company_row, companies, held_companies, publication_gaps,
                     unconfirmed_companies)
from . import pacing
from . import relay
from .pacing import ModelRequestGuard
from .strategy import COVERAGE_STRATEGY
from .native_counts import summary as native_call_summary
from .tools import final_review_status
from scripts import codex_tyche as runner
from validate_run import BONUS_PASS_APPROACH, _company_key
import confirmed_leads

MODEL = runner.SETUP["route_id"]
CODE_MODE = runner.CODE_MODE
REASONING_EFFORT = runner.REASONING_EFFORT
WEB_SEARCH = runner.WEB_SEARCH
SESSION_SETTINGS = ("model", "model_reasoning_effort", "service_tier")
CODEX_VERSION = runner.CODEX_VERSION
DEFAULT_WALL_CLOCK_SECONDS = 45 * 60
EXIT_MARGIN_SECONDS = 150
FINAL_REVIEW_RESERVE_SECONDS = runner.FINALIZATION_SECONDS
PUBLISH_INTERVAL_SECONDS = 2
MODEL_GUARD_DENIALS = {"budget_exhausted", "budget_paced", "quota_exceeded_openrouter_reserve"}
MAX_TRANSIENT_WORKER_FAILURES = 20
RESEARCH_WORKERS = 2
WORKER_LANE_FILE = "worker-{}-lane.txt"
REVIEW_PACKET_COMPANIES = 1
PARALLEL_MIN_ALLOWANCE_MICROUSD = 2_000_000
# Bonus pass: when research ends with the company target met, one Codex session may add an optional later intent
# signal to the published companies (ArenaHost.bonus_pass). False leaves the run exactly as without the pass.
BONUS_PASS = True
BONUS_PASS_MIN_SECONDS = 900           # start only with this much research time left
BONUS_PASS_END_MARGIN_SECONDS = 480    # end this long before the research deadline; final review keeps the rest
BONUS_PASS_MAX_SECONDS = 900
BONUS_PASS_RESTART_SECONDS = 240
BONUS_PASS_WIRES = ("prnewswire.com", "globenewswire.com", "accessnewswire.com")
CAPTURE_TOOLS = ("contextdev_get_web_scrape_markdown", "firecrawl_scrape")
# Words to search the saved pages for, by Leadpoet bonus_intents intent_category.
BONUS_TERMS = {"FUNDING": ["raised", "funding", "Series"], "PRODUCT_LAUNCH": ["launch", "introduc", "unveil"],
               "PARTNERSHIP": ["partner", "agreement"], "MARKET_EXPANSION": ["expan", "enter", "new market"],
               "FACILITY_OPENING": ["open", "facility", "terminal"], "ACQUISITION": ["acqui"],
               "REGULATORY_CLEARANCE": ["approv", "clearance"], "CERTIFICATION": ["certif", "ISO", "SOC 2"]}
BONUS_PASS_PROMPT = (
    "Bonus pass for the saved Leadpoet run. The company target is met and sourcing is closed: do not claim, discover, "
    "hold, reject or call tyche_finish; the tools refuse those calls. For each company below, in order: first search its "
    "saved pages for free with tyche_inspect(ref=<saved_pages ref>, field='markdown', term=<term>); if no saved page "
    "states the later signal, run search_check with tyche_lookup, then capture its best result with capture_check "
    "(set inputs.url to that result's URL). These are the only two paid lookups for that company. When a captured "
    "passage names the company and states the later signal's event and the day it happened, dated on or after "
    "window_start, call tyche_review(companies=[{target, decision: 'accept', reason, qualification_checks: [{criterion_id, "
    "status: 'pass', claim, evidence: [{ref, text, event_date}]}]}]): claim is one plain sentence that opens with the "
    "company's name and prints the day as the page does, text is copied exactly from the captured page (at most 1,000 "
    "characters), event_date is that day as YYYY-MM-DD. Then approve the confirmation packet it returns (tyche_review "
    "with review_ref and review_findings) and check that arena_submission_preview.intent_signals lists the later "
    "signal. Close every bonus-pass lookup with tyche_review(sources=[{ref, state: 'exhausted', reason}]). When no "
    "passage proves the later signal, save nothing for that company. Stop when the list is done.\n\n")


def research_environment(workers):
    return dict(runner.research_environment(), TYCHE_PARALLEL_WORKERS=str(workers),
                TYCHE_REVIEW_PACKET_COMPANIES=str(REVIEW_PACKET_COMPANIES))


def research_workers(run_file):
    snapshot = pacing._snapshot()
    try:
        allowance = pacing.research_stop.allowance(snapshot["sourcing_cost"],
                                                   pacing.research_stop.request_budget(run_file))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 1
    return RESEARCH_WORKERS if allowance >= PARALLEL_MIN_ALLOWANCE_MICROUSD else 1
CODEX_LOG_BYTES = 256 * 1024
_CODEX_LOG_LOCK = threading.Lock()
TRANSIENT_RETRY_PAUSE_SECONDS = (5, 10, 20)
PRODUCTIVE_TURN_SECONDS = 90
MODEL_UNAVAILABLE_TURN = (b"last status: 429", b"status 502 Bad Gateway")
TOOL_TIMEOUT_SECONDS = 3 * DEEPLINE_WAIT_SECONDS + 15
AUTO_COMPACT_TOKEN_LIMIT = 128_000


def require_lab():
    if ROOT != Path("/agent/source"):
        raise RuntimeError("This harness runs only in the Leadpoet execute sandbox")
    for name in ("LAB_ARENA_WORKER_SOCKET", "LAB_ARENA_WEB_EGRESS_SOCKET"):
        value = os.environ.get(name, "")
        if not Path(value).is_absolute() or not Path(value).is_socket():
            raise RuntimeError("Leadpoet sandbox socket is missing: " + name)
    runtime = importlib.import_module("lab_arena_codex")
    checkpoint = importlib.import_module("lab_arena_checkpoint")
    if (getattr(runtime, "CODEX_VERSION", None) != CODEX_VERSION or not callable(getattr(runtime, "session", None))
            or not callable(getattr(checkpoint, "write", None)) or not os.access(runtime.CODEX_BINARY, os.X_OK)):
        raise RuntimeError("The Leadpoet Codex " + CODEX_VERSION + " runtime is unavailable")
    return runtime, checkpoint


def instructions():
    skill = (SKILL / "SKILL.md").read_text().replace("(references/", "(" + str(SKILL / "references") + "/")
    standing = runner.worker_instructions() if CODE_MODE else runner.ISOLATION_INSTRUCTIONS
    return standing + "\n\n" + skill + "\n\n" + (
        "Leadpoet execution context: the host owns credentials, provider billing, quotas and the hard deadline. "
        "Each company you confirm with tyche_review is published to Leadpoet immediately, so confirm every "
        "qualified company as soon as its evidence is saved; unconfirmed research is not delivered. "
        "Spend is paced to the request's budget within Leadpoet's limit: when a remote call is refused as budget_paced, confirm the current "
        "company from the evidence already saved (confirming needs no paid call) or stop. When research is done, "
        "tyche_finish can return review_handoff; the host then starts a fresh context to review the saved evidence. "
        "Correct or hold unsupported companies before final approval; the Arena host emits company output "
        "without a workbook. "
        "Final prose is not company output. " + PROVIDER_ROUTES + " " + turn_efficiency() + " " + COVERAGE_STRATEGY)


PROVIDER_ROUTES = (
    "Provider routes in this run: both Deepline and ScrapingDog are available. ScrapingDog is not a Deepline catalog "
    "tool and tyche_inspect(query=...) will not list it: call it as a tyche_lookup check with provider scrapingdog and the "
    "operation inside inputs, for example "
    "{\"target\": \"discovery\", \"phase\": \"account_discovery\", \"provider\": \"scrapingdog\", \"purpose\": \"...\", "
    "\"inputs\": {\"operation\": \"google_search\", \"query\": \"Acme launches card issuing 2026\", \"results\": 10}} "
    "for an exact Google query, {\"inputs\": {\"operation\": \"google_jobs\", \"query\": \"Acme data engineer\", "
    "\"country\": \"us\"}} to find one open posting for a hiring signal, and {\"target\": \"acme.com\", \"phase\": "
    "\"account_verification\", \"provider\": \"scrapingdog\", \"inputs\": {\"operation\": \"scrape\", \"url\": "
    "\"https://acme.com/news/x\", \"dynamic\": true, \"limit\": 1}} to capture a page the Deepline reader failed on or "
    "returned little text for (it renders JavaScript pages such as Workday, Lever and Greenhouse postings). Use Deepline "
    "first for everything else: discover with exa_search or contextdev_post_web_search, capture a page with "
    "contextdev_get_web_scrape_markdown or firecrawl_scrape, take the LinkedIn company record with harvestapi_get_company, "
    "and read one LinkedIn job posting with harvestapi_get_job. When a page reader fails on one URL, try the other reader "
    "once, then cite a different page that states the same fact. Never cite a search result as evidence: capture the page "
    "it points to.")


TURN_EFFICIENCY = (
    "Each model turn uses available execution time, so do more in each turn. The "
    "lead-sourcing skill above is already loaded: do not read SKILL.md again, and read a reference file only for a rule not stated above, opening just the section you need. "
    "In one exec cell, chain the TYCHE tool calls whose inputs you already know, for example every page "
    "capture and the LinkedIn company lookup for the current company, then its review. Batch up to three "
    "independent account-verification checks in one tyche_lookup call; submit exactly one discovery pilot "
    "per lookup. Reuse cached_descriptions instead of describing a "
    "tool again. Captured-page evidence_text returned by lookup is already saved source text: use it directly, "
    "and inspect further only for missing context, a needed field or the indicated continuation. Search excerpts "
    "still require page capture. For a saved result-row ref in tyche_inspect(ref=..., field=...), field starts inside the "
    "displayed facts, so omit the 'facts.' prefix. For literal term search, use that row ref and string field "
    "with term; omit target. A bare route ref cannot be searched with term.")
CHAINED_CALLS = ("In one exec cell, chain the TYCHE tool calls whose inputs you already know, for example every page "
                 "capture and the LinkedIn company lookup for the current company, then its review.")
PARALLEL_CALLS = ("In one response, call every TYCHE tool whose inputs you already know, for example every page capture "
                  "and the LinkedIn company lookup for the current company; review it once their results return. Each "
                  "TYCHE tool takes its inputs as named arguments: send arrays and objects as JSON values, with every "
                  "field the tool needs in the same call.")


def turn_efficiency():
    return TURN_EFFICIENCY if CODE_MODE else TURN_EFFICIENCY.replace(CHAINED_CALLS, PARALLEL_CALLS)


def evaluation_date():
    reference = os.environ.get("LAB_ARENA_EVALUATION_DATE")
    try:
        return datetime.strptime(reference, "%Y-%m-%d").date() if reference else datetime.now(timezone.utc).date()
    except ValueError:
        return datetime.now(timezone.utc).date()


def compaction_limit(config):
    lines = config.split("\n")
    found = [i for i, line in enumerate(lines) if line.strip().startswith("model_auto_compact_token_limit")]
    if len(found) != 1:
        return config
    lines[found[0]] = "model_auto_compact_token_limit = " + str(AUTO_COMPACT_TOKEN_LIMIT)
    return "\n".join(lines)


def model_route(config, session, relays):
    """Point Codex's model requests at a loopback relay that selects the faster approved route (see relay.py).

    Any setup problem keeps the bridge's own address, so the run proceeds on the default route.
    """
    bridge = session.get("LAB_ARENA_CODEX_BASE_URL") or ""
    line = "base_url = " + json.dumps(bridge)
    # The broker accepts a route selector only in full-thinking rounds (lab_arena/shim.py uses the same test).
    full_thinking = os.environ.get("LAB_ARENA_REASONING_CAPTURE_POLICY") == "full_generated_thinking_v1"
    if not full_thinking or not bridge or config.count(line) != 1:
        return config
    try:
        local = relays.enter_context(relay.Relay(bridge))
    except (OSError, ValueError):
        return config
    return config.replace(line, "base_url = " + json.dumps(local.base_url))


def codex_settings(environment):
    settings = {key: value for key, value in runner.codex_settings().items() if key not in SESSION_SETTINGS}
    if environment.get("TYCHE_FINALIZATION_ONLY") == "1":
        settings["tool_output_token_limit"] = runner.FINAL_REVIEW_TOOL_OUTPUT_TOKENS
    return [part for key, value in settings.items() for part in ("-c", key + "=" + json.dumps(value))]


def tool_configuration(run_file, deadline, response_deadline):
    args = ["-B", "-m", "tyche_arena.tools", "--run-file", str(run_file),
            "--deadline", str(deadline), "--response-deadline", str(response_deadline)]
    forwarded = ["PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "HOME", "LAB_ARENA_WORKER_SOCKET",
                 "LAB_ARENA_WEB_EGRESS_SOCKET", "LAB_ARENA_OUTPUT_PATH", "LAB_ARENA_EVALUATION_DATE",
                 "SCRAPINGDOG_API_KEY", "TYCHE_ISOLATED_RUN", "TYCHE_REQUEST_FILE", "TYCHE_RUN_STARTED_AT",
                 "TYCHE_FINALIZATION_ONLY", "TYCHE_WIND_DOWN_SECONDS", "TYCHE_UNPRICED_CALLS", "TYCHE_WORKER_ID",
                 "TYCHE_WORKER_GENERATION", "TYCHE_REVIEW_REFUSAL_LIMIT",
                 "TYCHE_REVIEW_PACKET_COMPANIES"] + (["TYCHE_BONUS_PASS", "TYCHE_BONUS_UNTIL"] if BONUS_PASS else [])
    return ("\n[mcp_servers.tyche]\ncommand = " + json.dumps(sys.executable) + "\nargs = " + json.dumps(args)
            + "\ncwd = " + json.dumps(str(ROOT)) + "\nenv_vars = " + json.dumps(forwarded)
            + f"\nrequired = true\nstartup_timeout_sec = 40\ntool_timeout_sec = {TOOL_TIMEOUT_SECONDS}\n"
              'default_tools_approval_mode = "approve"\n')


def _checkpoint_review_status(review, rows, checkpoint_ok):
    if not checkpoint_ok:
        return "checkpoint_error"
    if not review["delivery_allowed"]:
        return review["status"]
    return "validated" if rows == review["rows"] else "checkpoint_mismatch"


def _saved_state_summary(run_file, icp):
    try:
        document = json.loads(run_file.read_text(encoding="utf-8"))
        accepted, unresolved, rejected = (document[key] for key in ("accepted", "unresolved", "rejected"))
        request = document["request"]
        if (not all(isinstance(rows, list) for rows in (accepted, unresolved, rejected))
                or not isinstance(request, dict) or not (run_file.parent / "leads.json").is_file()):
            raise ValueError("Incomplete saved state")
        saved = confirmed_leads.read(run_file, document)
        gaps = Counter(field for issue in publication_gaps(accepted, icp, request)
                       for field in issue["fields"])
        return {"status": "available", "accepted_count": len(accepted),
                "unresolved_count": len(unresolved), "rejected_count": len(rejected),
                "confirmed_count": saved["confirmed_count"],
                "publication_gap_counts": dict(sorted(gaps.items()))}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unavailable"}


class Publisher:

    def __init__(self, run_dir, icp, limit, write):
        self.run_dir, self.icp, self.limit, self.write = run_dir, icp, limit, write
        self.rows = []
        self.latest_rows = None
        self.held, self.latest_held, self.latest_unconfirmed = False, 0, 0
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def _current_rows(self):
        try:
            document = json.loads((self.run_dir / "results.json").read_text())
            saved = json.loads((self.run_dir / "leads.json").read_text())
            accepted, leads = document["accepted"], saved["leads"]
            if (not isinstance(document["request"], dict) or not isinstance(accepted, list)
                    or not isinstance(leads, list) or saved.get("confirmed_count") != len(leads)
                    or any(not isinstance(row, dict) or not _company_key(row) for row in accepted + leads)
                    or len({_company_key(row) for row in leads}) != len(leads)):
                return None
            current = {_company_key(row): row for row in accepted}
            retained = [row for row in leads if current.get(_company_key(row)) == row]
            rows = companies({"leads": retained}, self.icp, document["request"], self.limit)
            room = min(self.limit, MAX_COMPANIES) - len(rows)
            unconfirmed = unconfirmed_companies(document, self.icp, room, run_file=self.run_dir / "results.json",
                                                published=rows) if self.held and room > 0 else []
            confirmed, rows = len(rows), rows + unconfirmed
            room = min(self.limit, MAX_COMPANIES, len(rows) + HELD_COMPANIES) - len(rows)
            held = held_companies(document, self.icp, room, run_file=self.run_dir / "results.json",
                                  published=rows) if self.held and room > 0 else []
            rows, self.latest_unconfirmed = rows + held, len(unconfirmed)
            self.latest_held = len(rows) - confirmed
            self.latest_rows = rows
            return rows
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def current_rows(self):
        with self.lock:
            rows = self._current_rows()
            return rows if rows is not None else self.latest_rows

    def publish(self):
        with self.lock:
            rows = self._current_rows()
            if rows is None:
                return self.rows
            if rows != self.rows:
                self.write(rows)
                self.rows = rows
                pacing.save_published_count(self.run_dir, len(rows) - self.latest_held)
            return self.rows

    def watch(self):
        while not self.stopped.wait(PUBLISH_INTERVAL_SECONDS):
            try:
                self.publish()
            except Exception as exc:
                print(json.dumps({"tyche_arena_publish_error": type(exc).__name__}), file=sys.stderr, flush=True)


def _codex_once(runtime, environment, run_dir, prompt, remaining, receipt):
    log = Path(run_dir) / "codex.log"
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile(dir=run_dir) as output:
        incoming.write(prompt.encode())
        incoming.seek(0)
        process = subprocess.Popen(
            [runtime.CODEX_BINARY, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
             *codex_settings(environment), "-C", str(run_dir), "-"],
            stdin=incoming, stdout=output, stderr=subprocess.STDOUT, env=environment,
            start_new_session=True)
        receipt.update(process_group_id=process.pid)
        try:
            while True:
                left = remaining()
                if left <= 0:
                    raise subprocess.TimeoutExpired(runtime.CODEX_BINARY, 0)
                try:
                    return process.wait(timeout=min(1.0, left))
                except subprocess.TimeoutExpired:
                    continue
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
            output.seek(max(0, output.seek(0, os.SEEK_END) - 4096))
            ending = output.read()
            receipt.update(model_unavailable=bool(process.returncode) and any(
                marker in ending for marker in MODEL_UNAVAILABLE_TURN))
            output.seek(0)
            try:
                with _CODEX_LOG_LOCK:
                    with log.open("ab") as kept:
                        shutil.copyfileobj(output, kept)
                    _trim(log, CODEX_LOG_BYTES)
            except OSError:
                pass


def _restart_pause(attempt, unavailable):
    pause = TRANSIENT_RETRY_PAUSE_SECONDS[min(attempt, len(TRANSIENT_RETRY_PAUSE_SECONDS) - 1)]
    return pause * random.uniform(0.75, 1.25) if unavailable else pause


def _trim(path, limit):
    try:
        if path.stat().st_size > limit:
            with path.open("rb") as handle:
                handle.seek(-limit, os.SEEK_END)
                tail = handle.read()
            path.write_bytes(tail)
    except OSError:
        pass


class ArenaHost:

    receipts_directory = "worker-executions"

    def __init__(self, runtime, run_dir, session, research_deadline, response_deadline, publisher, guard,
                 session_factory=None):
        self.runtime, self.run_dir, self.session = runtime, run_dir, session
        self.session_factory = session_factory
        self.research_deadline, self.response_deadline = research_deadline, response_deadline
        self.publisher, self.guard = publisher, guard
        self.transient_failures = 0
        self.research_transient_failures, self.active_researchers = 0, 0
        self._research_lock = threading.Lock()
        self._bonus_pass_done = False

    def export_partial(self, run_file, env):
        return {"partial": True, "companies": len(self.publisher.publish())}

    def prepare(self, request_file, env):
        if time.monotonic() >= self.response_deadline:
            return {"status": "blocked", "delivery_allowed": False, "reason": "host_deadline"}
        return None

    def research_receipt(self, request_file):
        return _WorkerReceipt(self.run_dir / self.receipts_directory)

    def execute_research(self, command, request_file, env, receipt, *, profile=None, deadline, output=None,
                         cost_stop=None):
        opened = self.session_factory() if self.session_factory else contextlib.nullcontext(self.session)
        with self._research_lock:
            self.active_researchers += 1
        try:
            with opened as session:
                data = self._execute_research(session, command, env, receipt, deadline=deadline, cost_stop=cost_stop)
        finally:
            with self._research_lock:
                self.active_researchers -= 1
                last = self.active_researchers == 0
        if last and isinstance(self.guard, pacing.ModelRequestGuard):
            try:
                self.guard.close_research()
            except (OSError, ValueError, KeyError, TypeError):
                data["failure_kind"] = "invalid_saved_state"
                receipt.save()
        return data

    def _execute_research(self, session, command, env, receipt, *, deadline, cost_stop):
        environment = dict(session, **{k: v for k, v in env.items() if k.startswith("TYCHE_")})
        environment["TYCHE_FINALIZATION_ONLY"] = "0"

        def remaining():
            left = self.research_deadline - time.monotonic()
            limit = deadline()
            left = left if limit is None else min(left, limit - time.time())
            return 0 if cost_stop is not None and cost_stop() else left

        code, retries, waits, step = None, 0, 0, 0
        try:
            if remaining() <= 0:
                receipt.data["failure_kind"] = "deadline_reached"
            while remaining() > 0:
                turn_started = time.monotonic()
                code = _codex_once(self.runtime, environment, self.run_dir, command[-1], remaining,
                                   _Receipt(receipt.data, receipt.save))
                if not code or self.guard.denial:
                    break
                unavailable = bool(receipt.data.get("model_unavailable"))
                if unavailable and time.monotonic() - turn_started >= PRODUCTIVE_TURN_SECONDS:
                    step = 0
                pause = _restart_pause(step if unavailable else retries, unavailable)
                with self._research_lock:
                    if remaining() <= pause + 60 or (
                            not unavailable and self.research_transient_failures >= MAX_TRANSIENT_WORKER_FAILURES):
                        break
                    if not unavailable:
                        self.research_transient_failures += 1
                if unavailable:
                    waits += 1
                    step += 1
                else:
                    retries += 1
                time.sleep(pause)
            if retries:
                receipt.data["host_retries"] = retries
            if waits:
                receipt.data["unavailable_restarts"] = waits
        except subprocess.TimeoutExpired:
            receipt.data["failure_kind"] = "deadline_reached"
        if self.guard.denial and receipt.data.get("failure_kind") is None:
            receipt.data["failure_kind"] = "deadline_reached"
        receipt.data["status"] = "complete" if code == 0 else "failed"
        receipt.finish(1 if code is None else code)
        print(json.dumps({"tyche_arena_worker_exit": {
            "phase": "research", "worker": env.get("TYCHE_WORKER_ID"), "exit_code": code,
            "failure_kind": receipt.data.get("failure_kind") or "none", "host_retries": retries,
            "unavailable_restarts": waits,
            "remaining_response_seconds": round(max(0, self.response_deadline - time.monotonic()), 3)}}),
            file=sys.stderr, flush=True)
        return receipt.data

    @staticmethod
    def reconcile_research(run_file):
        return None

    def research_report(self, run_dir):
        self.publisher.publish()

    def bonus_pass(self, request_file, env):
        """Once per run, after research ends with the company target met: one Codex session in which the model may add
        an optional later intent signal to the published companies through the ordinary lookup and review tools
        (tools.ArenaResearchTools._bonus_pass_guard, validate_run.bonus_pass_refusal). It ends
        BONUS_PASS_END_MARGIN_SECONDS before the research deadline, so final review then runs as usual."""
        if not BONUS_PASS or self._bonus_pass_done:
            return None
        self._bonus_pass_done = True
        run_file = self.run_dir / "results.json"
        started = time.monotonic()
        until = min(self.research_deadline - BONUS_PASS_END_MARGIN_SECONDS, started + BONUS_PASS_MAX_SECONDS)
        report = {"status": "skipped"}
        try:
            if self.research_deadline - started < BONUS_PASS_MIN_SECONDS:
                report["reason"] = "too_little_research_time"
            elif pacing.closure(run_file) is not None or self.guard.denial in pacing.research_stop.CAUSES:
                report["reason"] = "research_paced"
            elif not (candidates := bonus_candidates(self.run_dir)):
                report["reason"] = "no_candidates"
            else:
                before = later_signal_count(run_file)
                environment = dict(self.session, **{key: value for key, value in env.items() if key.startswith("TYCHE_")
                                                    and key not in ("TYCHE_WORKER_ID", "TYCHE_WORKER_GENERATION")})
                environment.update(TYCHE_FINALIZATION_ONLY="0", TYCHE_BONUS_PASS="active",
                                   TYCHE_BONUS_UNTIL=str(round(time.time() + until - time.monotonic(), 3)))
                prompt = BONUS_PASS_PROMPT + json.dumps(candidates, ensure_ascii=True)
                receipt = _WorkerReceipt(self.run_dir / self.receipts_directory)
                receipt.data["phase"] = "bonus_pass"
                code, restarts = None, 0
                try:
                    while True:
                        code = _codex_once(self.runtime, environment, self.run_dir, prompt,
                                           lambda: until - time.monotonic(), _Receipt(receipt.data, receipt.save))
                        if (not code or restarts or self.guard.denial or not receipt.data.get("model_unavailable")
                                or until - time.monotonic() < BONUS_PASS_RESTART_SECONDS):
                            break
                        restarts += 1
                        time.sleep(_restart_pause(0, True))
                except subprocess.TimeoutExpired:
                    receipt.data["failure_kind"] = "deadline_reached"
                receipt.finish(1 if code is None else code)
                report = {"status": "ran", "companies": [item["target"] for item in candidates], "exit_code": code,
                          "restarts": restarts, "later_signals_before": before,
                          "later_signals_after": later_signal_count(run_file)}
        except Exception as exc:
            report = {"status": "error", "error_kind": type(exc).__name__, "detail": str(exc)[:300]}
        finally:
            if report.get("status") == "ran" and isinstance(self.guard, pacing.ModelRequestGuard):
                try:
                    self.guard.close_research()
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            try:
                self.publisher.publish()
            except Exception:
                pass
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        print(json.dumps({"tyche_arena_bonus_pass": report}), file=sys.stderr, flush=True)
        return None

    def run_once(self, command, request_file, worker_env, profile, *, deadline, terminal, attempt):
        started = time.monotonic()
        receipts = self.run_dir / self.receipts_directory
        receipts.mkdir(exist_ok=True)
        receipt_path = receipts / (str(uuid.uuid4()) + ".json")
        receipt = {"started_at": time.time(), "finished_at": None}
        save = lambda: receipt_path.write_text(json.dumps(receipt))
        execution = {"status": "failed", "exit_code": 1}
        retries = waits = 0

        def remaining():
            left = (self.response_deadline if terminal else self.research_deadline) - time.monotonic()
            limit = deadline()
            return left if limit is None else min(left, limit - time.time())

        def research_retry_has_time(backoff=0):
            if terminal:
                return True
            closes = runner.research_close(request_file)
            return closes is None or closes - time.time() - backoff >= runner.MIN_RESEARCH_SECONDS

        if remaining() <= 0:
            execution["failure_kind"] = "deadline_reached"
        else:
            save()
            environment = dict(self.session, **{k: v for k, v in worker_env.items() if k.startswith("TYCHE_")})
            environment["TYCHE_FINALIZATION_ONLY"] = "1" if terminal else "0"
            try:
                while True:
                    self.guard.final_review = terminal
                    try:
                        code = _codex_once(self.runtime, environment, self.run_dir, command[-1], remaining,
                                           _Receipt(receipt, save))
                    finally:
                        self.guard.final_review = False
                    unavailable = bool(receipt.get("model_unavailable"))
                    if (not code or self.guard.denial
                            or not unavailable and self.transient_failures >= MAX_TRANSIENT_WORKER_FAILURES):
                        break
                    pause = _restart_pause(waits if unavailable else retries, unavailable)
                    if remaining() <= pause + 60:
                        break
                    if not research_retry_has_time(pause):
                        break
                    time.sleep(pause)
                    if not research_retry_has_time():
                        break
                    if unavailable:
                        waits += 1
                    else:
                        self.transient_failures += 1
                        retries += 1
                execution.update(status="complete" if code == 0 else "failed", exit_code=code,
                                 **({"host_retries": retries} if retries else {}),
                                 **({"unavailable_restarts": waits} if waits else {}))
            except subprocess.TimeoutExpired:
                execution["failure_kind"] = "deadline_reached"
            receipt.update(finished_at=time.time(), process_group_id=None)
            save()
            if self.guard.denial and execution.get("failure_kind") is None:
                execution["failure_kind"] = "deadline_reached"
            if not terminal and isinstance(self.guard, pacing.ModelRequestGuard):
                try:
                    self.guard.close_research()
                except (OSError, ValueError, KeyError, TypeError):
                    execution["failure_kind"] = "invalid_saved_state"
        review = final_review_status(self.run_dir / "results.json", limit=self.publisher.limit) if terminal else None
        try:
            published = self.publisher.publish()
            checkpoint_ok = True
            latest = self.publisher.current_rows()
            if latest is not None and latest != published:
                published, checkpoint_ok = latest, False
        except Exception:
            if not terminal:
                raise
            published = self.publisher.current_rows()
            if published is None:
                published = self.publisher.rows
            checkpoint_ok = False
        review_status = _checkpoint_review_status(review, published, checkpoint_ok) if terminal else None
        if terminal and review_status == "validated":
            status = {"status": "complete", "delivery_allowed": True, "reason": "verified_final_review",
                      "published_count": len(published)}
        else:
            status = {"status": "incomplete", "delivery_allowed": False,
                      "host_reason": self.guard.denial,
                      **({"final_review_status": review_status} if terminal else {})}
        runner.write_worker_status(request_file, status)
        kind = execution.get("failure_kind")
        if type(kind) is not str or kind not in {"deadline_reached", "cancelled", "model_usage_limit",
                                                   "invalid_saved_state", "startup_timeout", "host_limit"}:
            kind = "none" if kind is None else "other"
        code = execution.get("exit_code")
        print(json.dumps({"tyche_arena_worker_exit": {
            "phase": "final_review" if terminal else "research", "attempt": attempt,
            "exit_code": code if type(code) is int else None,
            "failure_kind": kind, "host_retries": retries, "unavailable_restarts": waits,
            "elapsed_seconds": round(max(0, time.monotonic() - started), 3),
            "remaining_response_seconds": round(max(0, self.response_deadline - time.monotonic()), 3)}}),
            file=sys.stderr, flush=True)
        return status, execution


def later_signal_count(run_file):
    try:
        accepted = json.loads(Path(run_file).read_text(encoding="utf-8"))["accepted"]
        return sum(any(LATER_SIGNAL.match(str(c.get("criterion_id") or "")) and c.get("status") == "pass"
                       for c in row.get("qualification_checks") or []) for row in accepted)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def bonus_candidates(run_dir):
    """The published companies without a passing later intent signal, with the inputs of their bonus-pass lookups.

    Read-only: saved run, confirmed leads and the ICP. Hiring and leadership later signals are skipped (judgedb Oct 28 -
    Nov 9: no qualified two-signal company with a hiring bonus; leadership bonuses verified 0 of 3)."""
    run_dir = Path(run_dir)
    document = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    leads = json.loads((run_dir / "leads.json").read_text(encoding="utf-8"))["leads"]
    icp = json.loads((run_dir / ICP_FILE).read_text(encoding="utf-8"))
    request = document["request"]
    current = {_company_key(row): row for row in document["accepted"]}
    published, seen = [], set()
    for lead in leads:
        if current.get(_company_key(lead)) != lead:
            continue
        row = company_row(lead, icp, request.get("buying_signals") or [])
        website = (row or {}).get("company_website", "").lower().rstrip("/")
        if row and website not in seen:
            seen.add(website)
            published.append(lead)
        if len(published) >= MAX_COMPANIES:
            break
    signals, ages = intent_signals(icp), intent_signal_ages(icp)
    kinds = {" ".join(str(item.get("intent_signal") or "").split()): str(item.get("intent_category") or "").upper()
             for item in icp.get("bonus_intents") or [] if isinstance(item, dict)}
    criteria_ids = {c.get("id") for c in document.get("criteria") or [] if isinstance(c, dict)}
    as_of = date.fromisoformat(request["as_of_date"])
    routes = [r for r in document.get("routes") or [] if isinstance(r, dict)]
    entries = []
    for index in range(1, len(signals)):
        signal, cid = signals[index], f"signal:{index}"
        kind = kinds.get(" ".join(signal.split()), "")
        if cid not in criteria_ids or HIRING.search(signal) or _LEADERSHIP.search(signal) or "HIRING" in kind \
                or "LEADERSHIP" in kind:
            continue
        start = (as_of - timedelta(days=ages[index] or 365)).isoformat()
        # The whole event list ('a new office, terminal, or distribution facility'), not the text up to its first comma.
        event = _signal_core(signal).rstrip(" ,.;")
        for lead in published:
            checks = {c.get("criterion_id"): c for c in lead.get("qualification_checks") or [] if isinstance(c, dict)}
            if any(LATER_SIGNAL.match(str(key or "")) and c.get("status") == "pass" for key, c in checks.items()):
                continue
            company = lead.get("company") or {}
            target, name = _company_key(lead), " ".join(str(company.get("canonical_name") or "").split())
            if any(item["target"] == target for item in entries):
                continue  # Leadpoet scores the strongest later signal; one per company is enough
            domain = str(company.get("domain") or target).lower()
            first = [e for e in (checks.get("signal:0") or {}).get("evidence") or [] if isinstance(e, dict)]
            first_routes = {(e.get("source") or {}).get("route_id") for e in first}
            hosts = []
            for item in first:
                host = (urlsplit(str(item.get("url") or "")).hostname or "").lower().removeprefix("www.")
                if (host and host != domain and not host.endswith("." + domain) and host not in hosts
                        and not any(host == bad or host.endswith("." + bad) for bad in BONUS_UNREADABLE_HOSTS)):
                    hosts.append(host)
            saved = [{"ref": r["route_id"] + ":0", **({"signal_0_page": True} if r["route_id"] in first_routes else {})}
                     for r in routes if r.get("scope") == target and r.get("tool") in CAPTURE_TOOLS
                     and r.get("provider_status") == "ok" and r.get("rows_returned") == 1]
            saved.sort(key=lambda item: not item.get("signal_0_page"))
            base = {"target": target, "phase": "account_verification", "provider": "deepline"}
            entries.append({
                "target": target, "name": name, "criterion_id": cid, "criterion": signal, "window_start": start,
                "earlier_check": (checks.get(cid) or {}).get("status"), "saved_pages": saved[:6],
                "terms": BONUS_TERMS.get(kind) or [word for word in event.split() if len(word) > 4][:2],
                "search_check": {**base, "tool": "exa_search", "approach": BONUS_PASS_APPROACH + ": search",
                                 "purpose": f"{cid} for {name}: own site or press wire",
                                 "inputs": {"query": " ".join(f"{name} {event}".split()), "type": "auto",
                                            "numResults": 8,
                                            "includeDomains": [domain, *hosts, *BONUS_PASS_WIRES],
                                            "startPublishedDate": start + "T00:00:00Z"}},
                "capture_check": {**base, "tool": CAPTURE_TOOLS[0], "approach": BONUS_PASS_APPROACH + ": capture",
                                  "purpose": f"{cid} for {name}: capture the search result", "inputs": {"url": ""}},
            })
    # Companies never checked for the later signal first, then those whose earlier check stayed unknown or failed.
    entries.sort(key=lambda item: item["earlier_check"] is not None)
    return entries


class _WorkerReceipt:

    def __init__(self, directory):
        directory.mkdir(exist_ok=True)
        self.path = directory / (str(uuid.uuid4()) + ".json")
        self.data = {"started_at": time.time(), "finished_at": None}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data))

    def finish(self, code):
        self.data.update(finished_at=time.time(), exit_code=code, process_group_id=None)
        self.save()


class _Receipt:
    def __init__(self, data, save):
        self.data, self.save = data, save

    def update(self, **values):
        self.data.update(values)
        self.save()


def run(icp):
    runtime, checkpoint = require_lab()
    limit = int(os.environ.get("LAB_ARENA_COMPANY_LIMIT") or icp.get("max_companies") or 5)
    if not 1 <= limit <= 5:
        raise ValueError("LAB_ARENA_COMPANY_LIMIT must be 1 through 5")
    wall_clock = int(os.environ.get("LAB_ARENA_WALL_CLOCK_SECONDS") or DEFAULT_WALL_CLOCK_SECONDS)
    started = time.monotonic()
    response_deadline = started + wall_clock - EXIT_MARGIN_SECONDS
    research_seconds = max(60, int(response_deadline - started - FINAL_REVIEW_RESERVE_SECONDS))
    research_deadline = started + research_seconds
    run_dir = Path(tempfile.mkdtemp(prefix="tyche-arena-", dir="/tmp"))
    run_file = run_dir / "results.json"
    request_file = prepare_request(run_dir, icp, limit, research_seconds)
    (run_dir / ICP_FILE).write_text(json.dumps(icp), encoding="utf-8")
    for position, lane in worker_lanes(icp, evaluation_date(), RESEARCH_WORKERS).items():
        (run_dir / WORKER_LANE_FILE.format(position)).write_text(lane, encoding="utf-8")
    publisher = Publisher(run_dir, icp, limit, checkpoint.write)
    guard = ModelRequestGuard(run_file)
    watcher = threading.Thread(target=publisher.watch, daemon=True)
    watcher.start()
    try:
        _research(runtime, run_dir, run_file, request_file, publisher, guard, research_deadline, response_deadline)
    except Exception as exc:
        print(json.dumps({"tyche_arena_error": type(exc).__name__, "detail": str(exc)[:500]}),
              file=sys.stderr, flush=True)
    finally:
        publisher.stopped.set()
        watcher.join(timeout=5)
    publisher.held = True
    try:
        rows = publisher.publish()
        checkpoint_ok = True
        latest = publisher.current_rows()
        if latest is not None and latest != rows:
            rows, checkpoint_ok = latest, False
    except Exception:
        rows = publisher.current_rows()
        if rows is None:
            rows = publisher.rows
        checkpoint_ok = False
    denial = guard.denial if guard.denial in MODEL_GUARD_DENIALS or guard.denial is None else "unknown_guard_denial"
    review = final_review_status(run_file, limit=limit)
    held, unconfirmed = (publisher.latest_held, publisher.latest_unconfirmed) if rows else (0, 0)
    review_status = _checkpoint_review_status(review, rows[:len(rows) - held], checkpoint_ok)
    print(json.dumps({"tyche_arena_exit": {"last_model_guard_denial": denial,
                                           "published_count": len(rows), "held_published_count": held - unconfirmed,
                                           "unconfirmed_published_count": unconfirmed,
                                           "final_review_status": review_status,
                                           "saved_state": _saved_state_summary(run_file, icp),
                                           "native_calls": native_call_summary(run_file)}}), file=sys.stderr, flush=True)
    return rows


def _research(runtime, run_dir, run_file, request_file, publisher, guard, research_deadline, response_deadline):
    started_at = datetime.now(timezone.utc).isoformat()
    workers = research_workers(run_file)

    @contextlib.contextmanager
    def configured_session():
        with runtime.session(model=MODEL, reasoning_effort=REASONING_EFFORT, web_search=WEB_SEARCH,
                             request_guard=guard, response_deadline=response_deadline) as session, \
                contextlib.ExitStack() as relays:
            session.update(
                TYCHE_ISOLATED_RUN="1", TYCHE_REQUEST_FILE=str(request_file), TYCHE_RUN_STARTED_AT=started_at,
                TYCHE_WIND_DOWN_SECONDS=str(runner.WIND_DOWN_SECONDS),
                TYCHE_UNPRICED_CALLS=runner.UNPRICED_CALLS, **research_environment(workers),
                **({"TYCHE_BONUS_PASS": "enabled"} if BONUS_PASS else {}))
            config = Path(session["CODEX_HOME"]) / "config.toml"
            config.write_text("developer_instructions = " + json.dumps(instructions()) + "\n"
                              + model_route(compaction_limit(config.read_text()), session, relays)
                              + tool_configuration(run_file, research_deadline, response_deadline))
            yield session

    with configured_session() as session:
        prompt = ("Source companies for this saved Leadpoet request with the TYCHE native tools. "
                  "Use the supplied lead-sourcing skill, then call tyche_inspect(). If it reports not_started, call tyche_start "
                  "with the interpreted request below and omit request.original_text; the launcher binds it. "
                  "If inspection returns a saved request, continue that run without reconstructing its request; "
                  "otherwise follow the returned blocker or error guidance. Research once the run is ready.\n\n"
                  + request_file.read_text(encoding="utf-8") + opening_plan(publisher.icp, evaluation_date()))
        host = ArenaHost(runtime, run_dir, session, research_deadline, response_deadline, publisher, guard,
                         session_factory=configured_session)
        code = runner.supervise_worker([runtime.CODEX_BINARY, "exec", prompt], request_file, session,
                                       Path(session["CODEX_HOME"]), host=host)
        if code not in (0, 2):
            status = run_dir / "worker-status.json"
            reason = json.loads(status.read_text()).get("reason") if status.exists() else None
            print(json.dumps({"tyche_arena_supervisor_exit": code, "reason": reason}), file=sys.stderr, flush=True)
