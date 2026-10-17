"""Run the main TYCHE supervisor inside a Leadpoet execute sandbox.

Research behavior lives only in the shared skill, native tools and supervisor. This
host supplies what differs in Leadpoet: model calls through lab_arena_codex, provider
calls through the broker (tyche_arena.tools), spend pacing, and output as Leadpoet's
atomic checkpoint, published after every confirmed company and corrected after final review.
"""

from datetime import datetime, timezone
from collections import Counter
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import ROOT, SKILL
from .broker import DEEPLINE_WAIT_SECONDS
from .input import prepare as prepare_request
from .output import HELD_COMPANIES, ICP_FILE, companies, held_companies, publication_gaps
from . import pacing
from .pacing import ModelRequestGuard
from .strategy import COVERAGE_STRATEGY
from .native_counts import summary as native_call_summary
from .tools import final_review_status
from scripts import codex_tyche as runner
from validate_run import _company_key
import confirmed_leads

MODEL = "openai/" + runner.DEFAULT_MODEL
REASONING_EFFORT = runner.REASONING_EFFORT
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
MAX_TRANSIENT_WORKER_FAILURES = 20  # Leadpoet's per-key model queue (2 slots for all ICPs) can time out often.
CODEX_LOG_BYTES = 256 * 1024
TRANSIENT_RETRY_PAUSE_SECONDS = (15, 30, 60)
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
    return runner.worker_instructions() + "\n\n" + skill + "\n\n" + (
        "Leadpoet execution context: the host owns credentials, provider billing, quotas and the hard deadline. "
        "Each company you confirm with tyche_review is published to Leadpoet immediately, so confirm every "
        "qualified company as soon as its evidence is saved; unconfirmed research is not delivered. "
        "Spend is paced to published companies: when a remote call is refused as budget_paced, confirm the current "
        "company from the evidence already saved (confirming needs no paid call) or stop. When research is done, "
        "tyche_finish can return review_handoff; the host then starts a fresh context to review the saved evidence. "
        "Correct or hold unsupported companies before final approval; the Arena host emits company output "
        "without a workbook. "
        "Final prose is not company output. " + TURN_EFFICIENCY + " " + COVERAGE_STRATEGY)


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
                 "TYCHE_WORKER_GENERATION"]
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

    Once research has ended (held=True), an ICP with no confirmed company publishes held companies instead
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
            if not rows and self.held:
                rows = held_companies(document, self.icp, min(self.limit, HELD_COMPANIES))
                self.latest_held = len(rows)
            else:
                self.latest_held = 0
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
    output is kept, bounded, in codex.log beside the run for diagnosis.
    """
    log = Path(run_dir) / "codex.log"
    with tempfile.TemporaryFile() as incoming, log.open("ab") as output:
        incoming.write(prompt.encode())
        incoming.seek(0)
        process = subprocess.Popen(
            [runtime.CODEX_BINARY, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
             "-c", "features.image_generation=false", "-c", "agents.enabled=false",
             "-c", "features.multi_agent_v2=false",
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
            _trim(log, CODEX_LOG_BYTES)


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

    def __init__(self, runtime, run_dir, session, research_deadline, response_deadline, publisher, guard):
        self.runtime, self.run_dir, self.session = runtime, run_dir, session
        self.research_deadline, self.response_deadline = research_deadline, response_deadline
        self.publisher, self.guard = publisher, guard
        self.transient_failures = 0

    def export_partial(self, run_file, env):
        return {"partial": True, "companies": len(self.publisher.publish())}

    def prepare(self, request_file, env):
        if time.monotonic() >= self.response_deadline:
            return {"status": "blocked", "delivery_allowed": False, "reason": "host_deadline"}
        return None

    def run_once(self, command, request_file, worker_env, profile, *, deadline, terminal, attempt):
        started = time.monotonic()
        receipts = self.run_dir / self.receipts_directory
        receipts.mkdir(exist_ok=True)
        receipt_path = receipts / (str(uuid.uuid4()) + ".json")
        receipt = {"started_at": time.time(), "finished_at": None}
        save = lambda: receipt_path.write_text(json.dumps(receipt))
        execution = {"status": "failed", "exit_code": 1}
        retries = 0

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
                    if (not code or self.guard.denial
                            or self.transient_failures >= MAX_TRANSIENT_WORKER_FAILURES):
                        break
                    pause = TRANSIENT_RETRY_PAUSE_SECONDS[min(retries, len(TRANSIENT_RETRY_PAUSE_SECONDS) - 1)]
                    if remaining() <= pause + 60:
                        break  # Too little worker time left for another turn.
                    if not research_retry_has_time(pause):
                        break  # Let the supervisor hand off the settled research failure.
                    time.sleep(pause)
                    if not research_retry_has_time():
                        break  # Sleep may outlast the available replacement window.
                    self.transient_failures += 1
                    retries += 1
                execution.update(status="complete" if code == 0 else "failed", exit_code=code,
                                 **({"host_retries": retries} if retries else {}))
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
            "failure_kind": kind, "host_retries": retries,
            "elapsed_seconds": round(max(0, time.monotonic() - started), 3),
            "remaining_response_seconds": round(max(0, self.response_deadline - time.monotonic()), 3)}}),
            file=sys.stderr, flush=True)
        return status, execution


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
    publisher.held = True  # Research is over: an ICP with no confirmed company may publish held ones.
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
    with runtime.session(model=MODEL, reasoning_effort=REASONING_EFFORT, web_search="live",
                         request_guard=guard, response_deadline=response_deadline) as session:
        # Keep the session's PYTHONPATH: it already names /agent, the source and the
        # installed requirements (/agent/deps) for every process Codex starts.
        session.update(
            TYCHE_ISOLATED_RUN="1", TYCHE_REQUEST_FILE=str(request_file),
            TYCHE_RUN_STARTED_AT=datetime.now(timezone.utc).isoformat(),
            TYCHE_WIND_DOWN_SECONDS=str(runner.WIND_DOWN_SECONDS),
            TYCHE_UNPRICED_CALLS=runner.UNPRICED_CALLS, TYCHE_PARALLEL_WORKERS="1")
        config = Path(session["CODEX_HOME"]) / "config.toml"
        config.write_text("developer_instructions = " + json.dumps(instructions()) + "\n"
                          + compaction_limit(config.read_text())
                          + tool_configuration(run_file, research_deadline, response_deadline))
        prompt = ("Source companies for this saved Leadpoet request with the TYCHE native tools. "
                  "Use the supplied lead-sourcing skill, then call tyche_inspect(). If it reports not_started, call tyche_start "
                  "with the interpreted request below and omit request.original_text; the launcher binds it. "
                  "If inspection returns a saved request, continue that run without reconstructing its request; "
                  "otherwise follow the returned blocker or error guidance. Research once the run is ready.\n\n"
                  + request_file.read_text(encoding="utf-8"))
        host = ArenaHost(runtime, run_dir, session, research_deadline, response_deadline, publisher, guard)
        code = runner.supervise_worker([runtime.CODEX_BINARY, "exec", prompt], request_file, session,
                                       Path(session["CODEX_HOME"]), host=host)
        if code not in (0, 2):
            status = run_dir / "worker-status.json"
            reason = json.loads(status.read_text()).get("reason") if status.exists() else None
            print(json.dumps({"tyche_arena_supervisor_exit": code, "reason": reason}), file=sys.stderr, flush=True)
