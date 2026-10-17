"""Private, content-free counts of completed Arena native tool calls."""

from collections import Counter
import fcntl
import os
from pathlib import Path
import re
import stat

from research_tools import TOOLS

FILE_NAME = "native-call-counts.log"
MAX_BYTES = 128 * 1024
TRUNCATED = b"!truncated\n"
PHASES = frozenset({"research", "final_review"})
TOOLS = frozenset(TOOLS)
TOOL_NAMES = TOOLS | {"unknown_tool"}
MODES = frozenset({"other", "invalid", "overview", "company", "company_review",
                   "source", "literal_find", "tool", "capability_search", "recovery", "saved_field"})
# Only statuses returned by the existing tool and Arena adapter are printed.
STATUSES = frozenset({"arena_output_validated", "available_for_default_options", "config_error", "confirmed_leads_saved",
    "export_failed", "invalid_approval", "invalid_state", "missing_approval",
    "needs_repair", "needs_research", "no_documented_rate", "not_started", "operationally_blocked",
    "output_unpublishable", "partial_unavailable", "preflight_failed", "provider_error",
    "repair_needed", "review_handoff", "review_required", "stale_approval", "unavailable",
    "unconfirmed_snapshot", "validated", "worker_yield"})
OUTCOMES = STATUSES | {"started", "returned", "raised"}
MAX_RAISED_SITES = 64
EXCEPTION_FAMILIES = (KeyError, IndexError, AttributeError, TimeoutError,
                      PermissionError, FileNotFoundError, ValueError, TypeError,
                      AssertionError, RuntimeError, OSError)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = ((_ROOT / "tyche_arena").resolve(),
            (_ROOT / ".agents/skills/lead-sourcing/scripts").resolve())
_MODULES = frozenset(
    ("tyche_arena." if index == 0 else "") + source.stem
    for index, directory in enumerate(_SOURCES)
    for source in directory.glob("*.py") if _IDENTIFIER.fullmatch(source.stem)
)


def _exception_detail(exc):
    """Use only a fixed type family and the innermost trusted source location."""
    family = next((cls.__name__ for cls in EXCEPTION_FAMILIES
                   if issubclass(type(exc), cls)), "other")
    try:
        trace = exc.__traceback__
        if trace is None:
            return family, "unavailable"
        while trace.tb_next is not None:
            trace = trace.tb_next
        frame = trace.tb_frame
        filename = Path(frame.f_code.co_filename).resolve(strict=True)
        declared = Path(frame.f_globals["__file__"]).resolve(strict=True)
        if filename != declared or filename.suffix != ".py":
            return family, "unavailable"
        module = next((("tyche_arena." if i == 0 else "") + filename.stem
                       for i, directory in enumerate(_SOURCES)
                       if filename.parent == directory), None)
        function = frame.f_code.co_name
        if (module not in _MODULES or not _IDENTIFIER.fullmatch(function)
                or trace.tb_lineno < 1):
            return family, "unavailable"
        return family, f"{module}.{function}:{trace.tb_lineno}"
    except (OSError, KeyError, TypeError, ValueError):
        return family, "unavailable"


def _valid_site(site):
    if site == "unavailable":
        return True
    if len(site) > 128 or ":" not in site or "." not in site:
        return False
    location, line = site.rsplit(":", 1)
    module, function = location.rsplit(".", 1)
    return (module in _MODULES and bool(_IDENTIFIER.fullmatch(function))
            and line.isascii() and line.isdecimal() and 0 < int(line) <= 1000000)


def inspect_mode(name, arguments):
    if name != "tyche_inspect":
        return "other"
    if not isinstance(arguments, dict):
        return "invalid"
    selectors = [key for key in ("target", "ref", "tool", "query", "recover")
                 if arguments.get(key) is not None]
    if len(selectors) > 1:
        return "invalid"
    if arguments.get("term") is not None:
        return "literal_find" if selectors == ["ref"] and arguments.get("field") else "invalid"
    if selectors == ["target"]:
        return "company_review" if arguments.get("field") == "evidence_review" else "company"
    if selectors:
        return {"ref": "source", "tool": "tool", "query": "capability_search",
                "recover": "recovery"}[selectors[0]]
    return "saved_field" if arguments.get("field") else "overview"


def outcome(result):
    status = result.get("status") if isinstance(result, dict) else None
    return status if isinstance(status, str) and status in STATUSES else "returned"


def record(path, phase, name, mode, result, exception=None):
    """One locked append; diagnostic failure never changes a native tool result."""
    if phase not in PHASES or name not in TOOL_NAMES or mode not in MODES or result not in OUTCOMES:
        return
    detail = ""
    if result == "raised" and exception is not None:
        family, site = _exception_detail(exception)
        detail = f"|{family}|{site}"
    line = f"{phase}|{name}|{mode}|{result}{detail}\n".encode("ascii")
    try:
        fd = os.open(Path(path).parent / FILE_NAME,
                     os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            details = os.fstat(fd)
            if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                    or details.st_mode & 0o077):
                return
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size  # Another process may have appended before this lock.
            if size >= len(TRUNCATED) and os.pread(fd, len(TRUNCATED), size - len(TRUNCATED)) == TRUNCATED:
                return
            if size + len(line) <= MAX_BYTES - len(TRUNCATED):
                os.write(fd, line)
            elif size <= MAX_BYTES - len(TRUNCATED):
                os.write(fd, TRUNCATED)
        finally:
            os.close(fd)
    except OSError:
        pass


def summary(path):
    try:
        fd = os.open(Path(path).parent / FILE_NAME,
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            details = os.fstat(fd)
            if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                    or details.st_mode & 0o077):
                return {"status": "unavailable"}
            fcntl.flock(fd, fcntl.LOCK_SH)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_BYTES + 1)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return {"status": "unavailable"}
    if not data:
        return {"status": "unavailable"}
    starts, finished, outcomes, raised_sites = Counter(), Counter(), Counter(), Counter()
    raised_unattributed = 0
    invalid = 0
    truncated = len(data) > MAX_BYTES
    for line in data.splitlines(keepends=True):
        if line == TRUNCATED:
            truncated = True
            continue
        parts = line.decode("ascii", errors="replace").rstrip("\n").split("|")
        detailed = len(parts) == 6 and parts[3] == "raised"
        if (not line.endswith(b"\n") or len(parts) not in (4, 6) or parts[0] not in PHASES
                or parts[1] not in TOOL_NAMES or parts[2] not in MODES or parts[3] not in OUTCOMES):
            invalid += 1
            continue
        if len(parts) == 6 and (not detailed or parts[4] not in {cls.__name__ for cls in EXCEPTION_FAMILIES} | {"other"}
                                or not _valid_site(parts[5])):
            invalid += 1
            continue
        key = tuple(parts[:3])
        (starts if parts[3] == "started" else finished)[key] += 1
        if parts[3] != "started":
            outcomes[(*key, parts[3])] += 1
        if parts[3] == "raised":
            if detailed and parts[5] != "unavailable":
                raised_sites[(*key, parts[4], parts[5])] += 1
            else:
                raised_unattributed += 1
    incomplete = sum(max(0, n - finished[key]) for key, n in starts.items())
    if any(finished[key] > starts[key] for key in finished):
        invalid += 1
    counts = {}
    for (phase, name, mode, result), n in sorted(outcomes.items()):
        counts.setdefault(phase, {}).setdefault(name, {}).setdefault(mode, {})[result] = n
    ranked = sorted(raised_sites.items(), key=lambda item: (-item[1], item[0]))
    site_counts = [{"phase": phase, "tool": name, "mode": mode,
                    "family": family, "site": site, "count": n}
                   for (phase, name, mode, family, site), n in ranked[:MAX_RAISED_SITES]]
    return {"status": "partial" if truncated or invalid or incomplete else "available",
            "completed": counts, "incomplete_count": incomplete,
            "invalid_record_count": invalid, "truncated": truncated,
            "raised_sites": site_counts,
            "raised_site_unattributed_count": raised_unattributed,
            "raised_site_overflow_count": sum(n for _, n in ranked[MAX_RAISED_SITES:])}
