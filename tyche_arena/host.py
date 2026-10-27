"""Run the main TYCHE supervisor inside a Leadpoet execute sandbox.

Research behavior lives only in the shared skill, native tools and supervisor. This
host supplies what differs in Leadpoet: model calls through lab_arena_codex, provider
calls through the broker (tyche_arena.tools), spend pacing, and output as Leadpoet's
atomic checkpoint, published after every confirmed company and corrected after final review.
"""

from datetime import datetime, timezone
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
import uuid

from . import ROOT, SKILL
from .broker import DEEPLINE_WAIT_SECONDS
from .input import opening_plan, prepare as prepare_request
from .output import HELD_COMPANIES, ICP_FILE, MAX_COMPANIES, companies, held_companies, publication_gaps
from . import pacing
from .pacing import ModelRequestGuard
from .strategy import COVERAGE_STRATEGY
from .native_counts import summary as native_call_summary
from .tools import final_review_status
from scripts import codex_tyche as runner
from validate_run import _company_key
import confirmed_leads

# DeepSeek V4.1 Flash returns its full reasoning, which full-generated-thinking rounds (Leadpoet PR 732) require; Luna
# hides it. On round 10-23's ICPs 4, 6, 8 and 9 it matched Luna on quality at about the same cost per company (tc35).
# The product launcher keeps runner.DEFAULT_MODEL.
MODEL = "deepseek/deepseek-v4.1-flash-1017"
# Codex 0.154.0 runs its own models in code mode: MCP tools are reached only inside a JavaScript exec cell. Any other
# model (for example an open-weight model on OpenRouter) gets fallback metadata and calls each TYCHE tool as a plain
# function whose schema is already in the request, so exec wording and the exec tool card would mislead it.
CODE_MODE = MODEL in {"openai/gpt-5.6-luna", "openai/gpt-6-luna"}
REASONING_EFFORT = runner.REASONING_EFFORT
# Hosted web search is on. The model rarely calls it (once in four tc35 DeepSeek runs), but without a hosted tool in the
# request Fireworks decodes tool arguments against their schemas, and runs with it off scored lower (tc36, tc37).
# Keep it on in full-thinking rounds too: Leadpoet's merged policy (PR 732, b967993) accepts hosted search on its
# approved DeepSeek Responses routes, and PR 784 (a11c966) replays Codex's completed searches in the form OpenRouter
# accepts. Each Codex command repeats the setting, because a project .codex/config.toml above the run directory
# overrides the session's config (2026-10-25: a mismatch made Leadpoet's bridge refuse every request).
WEB_SEARCH = "live"
CODEX_VERSION = runner.CODEX_VERSION
DEFAULT_WALL_CLOCK_SECONDS = 45 * 60
EXIT_MARGIN_SECONDS = 150    # Last in-flight provider call, final checkpoint and process exit.
# Allocate the shared finalization allowance inside Arena's existing hard wall cap.
FINAL_REVIEW_RESERVE_SECONDS = runner.FINALIZATION_SECONDS
PUBLISH_INTERVAL_SECONDS = 2
MODEL_GUARD_DENIALS = {"budget_exhausted", "budget_paced", "quota_exceeded_openrouter_reserve"}
# Leadpoet's Codex config disables Codex's own request retries (an unknown bill must not be
# replayed), so one transient provider failure ends a Codex turn. The host restarts that turn
# itself, after a growing pause, at most this many times per run; the supervisor never sees the
# failed turn, so a short network outage does not count toward its failure or no-progress limits.
# A turn whose model request got no reply (MODEL_UNAVAILABLE_TURN) is restarted without counting toward this cap.
MAX_TRANSIENT_WORKER_FAILURES = 20  # Leadpoet's shared model queue can time out under load.
# Research workers per Leadpoet run. On 2026-10-13 productive ICPs stopped at 2-4 companies with budget left:
# about 7 minutes per company, so time, not money, limited them. Two workers fit the sandbox (1 CPU, 2 GB;
# a one-worker run peaked at 547 MB and ~5% CPU) and Leadpoet's shared model gate (default 200 calls).
RESEARCH_WORKERS = 2
# A refused accept is saved as a hold, and a company refused this many reviews in a row is held so its worker moves on
# (ResearchTools.refusal_limit). In tc35, 281 of 414 refused company reviews came after six in a row for that company;
# 2 of 76 companies were accepted after so many.
REVIEW_REFUSAL_LIMIT = 6
# Leadpoet refuses a model request whose reasoning text exceeds 32,000 characters, and DeepSeek's reasoning after a
# five-company review packet exceeded it in 5 of 8 replays (tc38/tc39 final reviews: 54 such refusals in 26 ICPs, up to
# 10 restarts and 28 minutes in one review). A packet shows this many companies (ResearchTools.packet_companies).
REVIEW_PACKET_COMPANIES = 1
# Two researchers roughly double how far admitted calls can overshoot the pacing line on one 20 s quota reading, so
# they run only when the run's allowance has room for it: the flat $4 per-ICP budget (research stops at $3.60), not
# the legacy one-company $0.80 (research stops at $0.72, leaving $0.08 for final review) or a small request.
PARALLEL_MIN_ALLOWANCE_MICROUSD = 2_000_000


def research_workers(run_file):
    """RESEARCH_WORKERS when the current quota snapshot's allowance reaches PARALLEL_MIN_ALLOWANCE, else one."""
    snapshot = pacing._snapshot()
    try:
        allowance = pacing.research_stop.allowance(snapshot["sourcing_cost"],
                                                   pacing.research_stop.request_budget(run_file))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 1  # No readable allowance: keep the serial path.
    return RESEARCH_WORKERS if allowance >= PARALLEL_MIN_ALLOWANCE_MICROUSD else 1
CODEX_LOG_BYTES = 256 * 1024
_CODEX_LOG_LOCK = threading.Lock()  # Parallel researchers share codex.log.
TRANSIENT_RETRY_PAUSE_SECONDS = (15, 30, 60)
# How Codex 0.154.0 (Leadpoet's pin) ends a turn whose model request got no reply: "exceeded retry limit, last status:
# 429 ..." when Leadpoet's bridge refuses the request (one already in progress, or unavailable; local tests also pass a
# provider's 429 through), and "unexpected status 502 Bad Gateway: ..." when Leadpoet's broker reports the provider
# unavailable (a provider 429 after the broker's own two retries at 20 and 40 s, a provider 5xx or an account failure)
# or is unavailable itself. On 2026-10-25 OpenRouter moved DeepSeek from Fireworks to DeepInfra, the cheapest ZDR
# endpoint, which rate-limits often: in local tests (tc37) at least 248 turns ended on a 429, against 9 in all earlier
# Fireworks runs, and ICPs lost both researchers once the shared restarts ran out. Such a turn is restarted on the usual
# pauses, with jitter so researchers do not retry together, outside MAX_TRANSIENT_WORKER_FAILURES; the research and
# response deadlines still bound it, and a guard refusal still stops it first. Longer pauses would not help: a worker
# needs one turn without a refusal to start the run within the launcher's STARTUP_SECONDS (600 s), and on one ICP 9 of
# 13 requests there were refused.
MODEL_UNAVAILABLE_TURN = (b"last status: 429", b"status 502 Bad Gateway")
TOOL_TIMEOUT_SECONDS = 3 * DEEPLINE_WAIT_SECONDS + 15  # Conservatively covers a three-check batch plus broker overhead.
# Leadpoet's runtime compacts Codex history at 64,000 tokens. TYCHE starts near 20,000 and adds about 4,000 a turn, so
# local runs on round arena-2026-10-14 compacted about 16 times per ICP: each compaction call re-sent about 61,000
# mostly uncached tokens, and the turns after it re-read the run. Together that was about half of the model spend that
# counts against the $0.80-per-company allowance. Large requests measured 5.15 characters per token, so 128,000
# tokens is about 660 KB, plus one tool output (at most ~45 KB), inside the bridge's 1 MB request cap.
AUTO_COMPACT_TOKEN_LIMIT = 128_000


def require_lab():
    """Refuse to start outside the Leadpoet execute sandbox, before any call."""
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
    """The local worker instructions and skill, plus the Leadpoet output contract."""
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
        "Final prose is not company output. " + turn_efficiency() + " " + COVERAGE_STRATEGY)


# Each model turn uses available execution time; this changes pacing, not what TYCHE checks.
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
    """TURN_EFFICIENCY in the call form this model gets: exec cells in code mode, else parallel function calls."""
    return TURN_EFFICIENCY if CODE_MODE else TURN_EFFICIENCY.replace(CHAINED_CALLS, PARALLEL_CALLS)


def evaluation_date():
    """Leadpoet's evaluation date (LAB_ARENA_EVALUATION_DATE), else today in UTC."""
    reference = os.environ.get("LAB_ARENA_EVALUATION_DATE")
    try:
        return datetime.strptime(reference, "%Y-%m-%d").date() if reference else datetime.now(timezone.utc).date()
    except ValueError:
        return datetime.now(timezone.utc).date()


def compaction_limit(config):
    """The runtime's Codex config with TYCHE's compaction limit; a config without the runtime's line is unchanged."""
    lines = config.split("\n")
    found = [i for i, line in enumerate(lines) if line.strip().startswith("model_auto_compact_token_limit")]
    if len(found) != 1:
        return config
    lines[found[0]] = "model_auto_compact_token_limit = " + str(AUTO_COMPACT_TOKEN_LIMIT)
    return "\n".join(lines)


def tool_configuration(run_file, deadline, response_deadline):
    args = ["-B", "-m", "tyche_arena.tools", "--run-file", str(run_file),
            "--deadline", str(deadline), "--response-deadline", str(response_deadline)]
    forwarded = ["PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "HOME", "LAB_ARENA_WORKER_SOCKET",
                 "LAB_ARENA_WEB_EGRESS_SOCKET", "LAB_ARENA_OUTPUT_PATH", "LAB_ARENA_EVALUATION_DATE",
                 "SCRAPINGDOG_API_KEY", "TYCHE_ISOLATED_RUN", "TYCHE_REQUEST_FILE", "TYCHE_RUN_STARTED_AT",
                 "TYCHE_FINALIZATION_ONLY", "TYCHE_WIND_DOWN_SECONDS", "TYCHE_UNPRICED_CALLS", "TYCHE_WORKER_ID",
                 "TYCHE_WORKER_GENERATION", "TYCHE_REVIEW_REFUSAL_LIMIT",
                 "TYCHE_REVIEW_PACKET_COMPANIES"]
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
    """Only numeric saved progress and fixed mapper gap names reach the exit log."""
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
    """Publish confirmed companies from leads.json to Leadpoet's atomic checkpoint.

    Once research has ended (held=True), slots the confirmed companies leave empty take held companies after them
    (output.held_companies); they never count toward pacing.
    """

    def __init__(self, run_dir, icp, limit, write):
        self.run_dir, self.icp, self.limit, self.write = run_dir, icp, limit, write
        self.rows = []
        self.latest_rows = None  # Last readable projection; separate from the acknowledged checkpoint.
        self.held, self.latest_held = False, 0
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def _current_rows(self):
        """Project only still-accepted saved confirmations; None means unreadable."""
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
            room = min(self.limit, MAX_COMPANIES, len(rows) + HELD_COMPANIES) - len(rows)
            held = held_companies(document, self.icp, room, run_file=self.run_dir / "results.json",
                                  published=rows) if self.held and room > 0 else []
            rows, self.latest_held = rows + held, len(held)
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
                self.write(rows)  # Atomic: Leadpoet keeps the last complete file at its deadline.
                self.rows = rows
                # Pacing counts confirmed companies Leadpoet can score, not every confirmed row or held company.
                pacing.save_published_count(self.run_dir, len(rows) - self.latest_held)
            return self.rows

    def watch(self):
        while not self.stopped.wait(PUBLISH_INTERVAL_SECONDS):
            try:
                self.publish()
            except Exception as exc:  # Keep publishing; the final publish reports a persistent error.
                print(json.dumps({"tyche_arena_publish_error": type(exc).__name__}), file=sys.stderr, flush=True)


def _codex_once(runtime, environment, run_dir, prompt, remaining, receipt):
    """One Codex worker, stopped when remaining() reaches zero; its process group is always stopped.

    remaining() is re-read while Codex runs: the supervisor's deadline moves from the
    startup limit to the research deadline once tyche_start saves the run. Codex's own
    output is kept, bounded, in codex.log beside the run for diagnosis, one whole turn at a
    time so parallel researchers do not interleave; receipt["model_unavailable"] says whether
    a failed turn ended on a model request that got no reply (MODEL_UNAVAILABLE_TURN).
    """
    log = Path(run_dir) / "codex.log"
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile(dir=run_dir) as output:
        incoming.write(prompt.encode())
        incoming.seek(0)
        process = subprocess.Popen(
            [runtime.CODEX_BINARY, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
             "-c", "features.image_generation=false", "-c", "agents.enabled=false",
             "-c", "features.multi_agent_v2=false", "-c", "web_search=" + json.dumps(WEB_SEARCH),
             *(["-c", f"tool_output_token_limit={runner.FINAL_REVIEW_TOOL_OUTPUT_TOKENS}"]
               if environment.get("TYCHE_FINALIZATION_ONLY") == "1" else []),
             "-C", str(run_dir), "-"],
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
                pass  # Already gone, or only exited (zombie) members remain: nothing left to stop.
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
                pass  # Diagnostics only: never replaces the turn's result.


def _restart_pause(attempt, unavailable):
    """Seconds before restarting a failed turn; one whose model request got no reply gets jitter."""
    pause = TRANSIENT_RETRY_PAUSE_SECONDS[min(attempt, len(TRANSIENT_RETRY_PAUSE_SECONDS) - 1)]
    return pause * random.uniform(0.75, 1.25) if unavailable else pause


def _trim(path, limit):
    """Keep only the last `limit` bytes of a log."""
    try:
        if path.stat().st_size > limit:
            with path.open("rb") as handle:
                handle.seek(-limit, os.SEEK_END)
                tail = handle.read()
            path.write_bytes(tail)
    except OSError:
        pass


class ArenaHost:
    """The supervisor's host hooks; every continuation decision stays in codex_tyche."""

    receipts_directory = "worker-executions"

    def __init__(self, runtime, run_dir, session, research_deadline, response_deadline, publisher, guard,
                 session_factory=None):
        self.runtime, self.run_dir, self.session = runtime, run_dir, session
        # Leadpoet's Responses bridge admits one in-flight model request per Codex session and answers a second
        # with 429 "request already in progress" (2026-10-13: two workers on one session exhausted Codex's
        # retries). Each research worker therefore opens its own isolated session; the spend guard is shared.
        self.session_factory = session_factory
        self.research_deadline, self.response_deadline = research_deadline, response_deadline
        self.publisher, self.guard = publisher, guard
        self.transient_failures = 0  # run_once (serial research and final review).
        # Parallel researchers have their own restart allowance, so research cannot use up final review's, and the
        # pacing closure is written only when no researcher can still reserve a provider call.
        self.research_transient_failures, self.active_researchers = 0, 0
        self._research_lock = threading.Lock()

    def export_partial(self, run_file, env):
        return {"partial": True, "companies": len(self.publisher.publish())}

    def prepare(self, request_file, env):
        if time.monotonic() >= self.response_deadline:
            return {"status": "blocked", "delivery_allowed": False, "reason": "host_deadline"}
        return None

    # Hooks for scripts/parallel_sourcing.run_research. Each researcher is the same Codex call run_once makes,
    # under the research deadline and the shared spend guard; final review still runs once through run_once.
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
        # arena_research_stop records the ledger's provider calls and later requires that exact set, so close research
        # only when no researcher remains to reserve another call (the shared guard denies every session's next
        # model request, so the others end promptly).
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

        code, retries, waits = None, 0, 0
        try:
            if remaining() <= 0:
                receipt.data["failure_kind"] = "deadline_reached"
            # Like run_once: a transient provider failure ends a Codex turn (requests are never replayed), so restart
            # it after a growing pause within the run's shared allowance. On 2026-10-19 two researchers without this
            # ended every ICP after 326-720 s on a few dropped connections (invalid_or_unavailable_runtime_state).
            while remaining() > 0:
                code = _codex_once(self.runtime, environment, self.run_dir, command[-1], remaining,
                                   _Receipt(receipt.data, receipt.save))
                if not code or self.guard.denial:
                    break
                unavailable = bool(receipt.data.get("model_unavailable"))
                pause = _restart_pause(waits if unavailable else retries, unavailable)
                with self._research_lock:
                    if remaining() <= pause + 60 or (
                            not unavailable and self.research_transient_failures >= MAX_TRANSIENT_WORKER_FAILURES):
                        break
                    if not unavailable:
                        self.research_transient_failures += 1
                if unavailable:
                    waits += 1
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
            receipt.data["failure_kind"] = "deadline_reached"  # Paced out: close research, keep output.
        # parallel_sourcing counts an invocation whose status is not "complete" as a failure, and two in a row disable
        # the worker; run_once sets the same status from the exit code.
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
        return None  # Leadpoet's broker settles provider billing; there is no local feed to read.

    def research_report(self, run_dir):
        self.publisher.publish()  # Keep the checkpoint current when the research pool ends.

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
            limit = deadline()  # Re-read: startup limit first, then the saved research deadline.
            return left if limit is None else min(left, limit - time.time())

        def research_retry_has_time(backoff=0):
            if terminal:
                return True  # Final review keeps its existing response-window retry policy.
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
                        break  # Too little worker time left for another turn.
                    if not research_retry_has_time(pause):
                        break  # Let the supervisor hand off the settled research failure.
                    time.sleep(pause)
                    if not research_retry_has_time():
                        break  # Sleep may outlast the available replacement window.
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
                execution["failure_kind"] = "deadline_reached"  # Paced out: close research, keep output.
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


class _WorkerReceipt:
    """A parallel researcher's execution receipt, in run_once's format (Leadpoet bills model use itself)."""

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
    # Bind the saved request and every research worker/tool to this same deadline.
    # The existing saved wind-down closes new research before this hard cutoff.
    research_seconds = max(60, int(response_deadline - started - FINAL_REVIEW_RESERVE_SECONDS))
    research_deadline = started + research_seconds
    run_dir = Path(tempfile.mkdtemp(prefix="tyche-arena-", dir="/tmp"))
    run_file = run_dir / "results.json"
    request_file = prepare_request(run_dir, icp, limit, research_seconds)
    (run_dir / ICP_FILE).write_text(json.dumps(icp), encoding="utf-8")  # Tool feedback uses the publisher's exact ICP.
    publisher = Publisher(run_dir, icp, limit, checkpoint.write)
    guard = ModelRequestGuard(run_file)
    watcher = threading.Thread(target=publisher.watch, daemon=True)
    watcher.start()
    try:
        _research(runtime, run_dir, run_file, request_file, publisher, guard, research_deadline, response_deadline)
    except Exception as exc:
        # Confirmed companies are already published; a late failure must not discard them.
        print(json.dumps({"tyche_arena_error": type(exc).__name__, "detail": str(exc)[:500]}),
              file=sys.stderr, flush=True)
    finally:
        publisher.stopped.set()
        watcher.join(timeout=5)
    publisher.held = True  # Research is over: slots the confirmed companies leave empty may take held ones.
    try:
        rows = publisher.publish()
        checkpoint_ok = True
        latest = publisher.current_rows()
        if latest is not None and latest != rows:
            rows, checkpoint_ok = latest, False
    except Exception:  # The host returns the current saved projection even if checkpoint.write failed.
        rows = publisher.current_rows()
        if rows is None:
            rows = publisher.rows
        checkpoint_ok = False
    # The sandbox keeps stderr privately; report only fixed guard state and a count.
    denial = guard.denial if guard.denial in MODEL_GUARD_DENIALS or guard.denial is None else "unknown_guard_denial"
    review = final_review_status(run_file, limit=limit)
    held = publisher.latest_held if rows else 0
    review_status = _checkpoint_review_status(review, rows[:len(rows) - held], checkpoint_ok)
    print(json.dumps({"tyche_arena_exit": {"last_model_guard_denial": denial,
                                           "published_count": len(rows), "held_published_count": held,
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
                             request_guard=guard, response_deadline=response_deadline) as session:
            # Keep the session's PYTHONPATH: it already names /agent, the source and the
            # installed requirements (/agent/deps) for every process Codex starts.
            session.update(
                TYCHE_ISOLATED_RUN="1", TYCHE_REQUEST_FILE=str(request_file), TYCHE_RUN_STARTED_AT=started_at,
                TYCHE_WIND_DOWN_SECONDS=str(runner.WIND_DOWN_SECONDS),
                TYCHE_UNPRICED_CALLS=runner.UNPRICED_CALLS, TYCHE_PARALLEL_WORKERS=str(workers),
                TYCHE_REVIEW_REFUSAL_LIMIT=str(REVIEW_REFUSAL_LIMIT),
                TYCHE_REVIEW_PACKET_COMPANIES=str(REVIEW_PACKET_COMPANIES),
                **({} if CODE_MODE else {"TYCHE_CODE_MODE": "0"}))  # Plain function tools: no exec directive.
            config = Path(session["CODEX_HOME"]) / "config.toml"
            config.write_text("developer_instructions = " + json.dumps(instructions()) + "\n"
                              + compaction_limit(config.read_text())
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
