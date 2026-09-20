"""Bound one ICP, including synchronous transports and executor shutdown.

The worker is an ordinary child inside the same sandbox, with the same API
boundary. The parent only retains validated output and enforces wall time;
it performs no sourcing. A process boundary is necessary because cancelling
an await does not stop the synchronous HTTP thread behind it.
"""
from __future__ import annotations

import json
import os
import select
import signal
import time
from contextvars import ContextVar
from copy import deepcopy

FINALIZE_SECONDS = 230.0
KILL_SECONDS = 249.0
TOTAL_SECONDS = 250.0
if os.environ.get('AGENT_RULE_V15_FULL_EXTRACT', '1').lower() not in ('0', 'false', 'off', 'no'):
    FINALIZE_SECONDS, KILL_SECONDS, TOTAL_SECONDS = 140.0, 146.0, 147.0
CALL_SECONDS = 30.0
LLM_SECONDS = 120.0
CALL_WINDOW = ContextVar('provider_call_window', default=None)
IN_PROCESS = ContextVar('deadline_inprocess', default=False)


class BudgetExhausted(RuntimeError):
    """No new request is permitted in this budget window."""


def request_timeout(configured: float) -> float:
    window = CALL_WINDOW.get()
    remaining = window() if window is not None else configured
    if remaining <= 0:
        raise BudgetExhausted('request deadline exhausted')
    return min(configured, CALL_SECONDS, remaining)


def _run_inprocess(runner, icp, started, reason):
    """Keep the normal call/cancellation deadlines without a process guard.

    A thread that ignores cancellation cannot be forcibly reaped on this path;
    usage explicitly records that the hard wall guarantee is unavailable.
    """
    companies, usage = [], {}
    def publish(rows, metrics, *, done=False, error=None):
        nonlocal companies, usage
        if not error:
            companies, usage = deepcopy(rows), dict(metrics)
    token = IN_PROCESS.set(True)
    try:
        companies, usage = runner(icp, started, publish)
    except (BudgetExhausted, TimeoutError) as exc:
        usage = {**usage, 'deadline.stop': type(exc).__name__}
    finally:
        IN_PROCESS.reset(token)
    usage = {**usage, 'deadline.fallback': 'inprocess',
             'deadline.fallback_reason': type(reason).__name__,
             'deadline.hard_wall': False,
             'wall_seconds': round(time.monotonic() - started, 3),
             'worker_killed': False}
    return companies, usage


def policy_limits(icp):
    from agent.v27_policies import default_contacts
    if default_contacts(icp):return 230.0,249.0,250.0
    if icp.get('contact_policy')=='contacts_v1' or icp.get('company_quality_policy')=='company_quality_v1':
        return 230.0,249.0,250.0
    return FINALIZE_SECONDS,KILL_SECONDS,TOTAL_SECONDS

def run_isolated(runner, icp):
    """Return the last completed snapshot even if worker cleanup is stuck.

    No forked worker or its executor threads survive this function. Ordinary
    completion transmits the exact final result. A one-second kill/reap margin
    is reserved below the total wall bound. Bounds assume normal OS scheduling.
    Pre-fork setup failure uses the same runner in-process with a soft deadline.
    """
    finalize_seconds,kill_seconds,total_seconds=policy_limits(icp)
    started = time.monotonic()
    read_fd = write_fd = None
    try:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        pid = os.fork()
    except BaseException as exc:
        for fd in (read_fd, write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if isinstance(exc, Exception):
            return _run_inprocess(runner, icp, started, exc)
        raise
    if pid == 0:
        os.close(read_fd)
        def publish(companies, usage, *, done=False, error=None):
            wire = (json.dumps({'companies': companies, 'usage': usage,
                                'done': done, 'error': error}, separators=(',', ':')) + '\n').encode()
            # Only this worker writes; the parent drains continuously.
            while wire:
                wire = wire[os.write(write_fd, wire):]
        try:
            companies, usage = runner(icp, started, publish)
            publish(companies, usage, done=True)
        except BaseException as exc:
            publish([], {}, done=True, error=f'{type(exc).__name__}: {str(exc)[:300]}')
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    companies, usage, buffer = [], {}, b''
    stopped = killed = reaped = done = False
    error = None
    try:
        while True:
            elapsed = time.monotonic() - started
            if not stopped and elapsed >= finalize_seconds:
                stopped = True
                os.kill(pid, signal.SIGTERM)  # worker cancels the root task
            if not killed and elapsed >= kill_seconds:
                killed = True
                os.kill(pid, signal.SIGKILL)
            if select.select([read_fd], [], [], min(.1, max(0, total_seconds - elapsed)))[0]:
                chunk = os.read(read_fd, 65536)
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    record = json.loads(line)
                    if not record['error']:
                        companies, usage = record['companies'], record['usage']
                    error = record['error']
                    done = record['done']
            if os.waitpid(pid, os.WNOHANG)[0]:
                reaped = True
                # The pipe may hold a final frame larger than one read.
                while True:
                    chunk = os.read(read_fd, 65536)
                    if not chunk:
                        break
                    buffer += chunk
                for line in buffer.splitlines():
                    record = json.loads(line)
                    if not record['error']:
                        companies, usage = record['companies'], record['usage']
                    error, done = record['error'], record['done']
                break
            if elapsed >= total_seconds:
                break
        if error and not stopped:
            raise RuntimeError(error)
        if not done and not stopped:
            raise RuntimeError('ICP worker exited without a final result')
        usage['wall_seconds'] = round(time.monotonic() - started, 3)
        usage['wall_cancelled'] = stopped
        usage['worker_killed'] = killed
        return companies, usage
    finally:
        os.close(read_fd)
        if not reaped:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
