"""Keep every outside call TYCHE makes itself, by ARETE's provider-call capture contract v1.

A host turns capture on with PROVIDER_CAPTURE_DIR=<folder>. Unset or empty, nothing here changes a call and nothing
is written. Each attempt is one JSON line (schema arete.provider_call/1) appended to
<folder>/provider-calls-<pid>.jsonl: the exact request, the raw reply up to 16 MiB, status, headers and timing, with
every credential redacted. Capture never raises into the stage and never changes what it sends, receives or decides;
a line it cannot write is lost, and the stage's own ledger still shows the call.

The lowest send points carry the hooks: an HTTP function decorated with http() marks its urllib request with sent()
and reads its reply through read(); the Deepline CLI runs through run(). route() and dispatch() say what those sends
are for: the ledger call a paid dispatch sends, or reads (catalog, billing, recovery) that send none. A billing read's
reply lists other runs' records, so it is stored only as the stage keeps it (keeping()).

A call only notes plain facts while it runs. Its line is rendered and written once the stage is done with it: when its
outermost route ends (after the stage's reads, deadline checks and resends), or as it ends outside any route.
"""

from contextlib import contextmanager, nullcontext
import contextvars
from datetime import datetime, timezone
import functools
import hashlib
from http.client import HTTPException, IncompleteRead
import json
import locale
import mmap
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, unquote_plus, urlsplit

ENV = "PROVIDER_CAPTURE_DIR"
SCHEMA = "arete.provider_call/1"
BOUND = 16 * 1024 * 1024  # A longer reply keeps its first BOUND bytes; its sha256 and size stay of the whole.
SHORTEST_SECRET = 8  # A shorter value is a placeholder, not a credential.
REDACTED = "[REDACTED]"
CREDENTIAL_GAP = "a credential reached this call and was redacted"
_HIDDEN_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
_KEY_HEADER_WORDS = ("api-key", "api_key", "apikey", "token", "secret", "password")  # never an idempotency key
_CREDENTIAL_PARAMS = {"api_key", "apikey", "api-key", "key", "access_token", "auth_token", "token", "secret",
                      "client_secret", "password"}
_KEY_FILES = (".env.deepline",)  # TYCHE's project credentials, read from the working folder up
_CLI_KEYS = Path(".local/deepline")  # The Deepline CLI's host credentials: ~/.local/deepline/<host>/.env
_ROUTE = contextvars.ContextVar("provider_capture_route", default=None)
_CALL = contextvars.ContextVar("provider_capture_call", default=None)
_WRITE = threading.Lock()  # Capture's own; the stage never holds it.
_WRITE_WAIT = 5  # Seconds; a writer never blocks the stage for longer, and loses its line instead.
_HELD_LOCK = threading.Lock()
_HELD = set()  # Every credential this process has sent, so any later line (a CLI's output too) redacts it.
_ROUTE_ENV = "CODEX_PROVIDER_OVERRIDES"  # The host's model route; its Authorization header holds a per-run token.
# A ledger call's attempts continue across processes (a resumed run): this process's sends of each (folder, call id)
# are counted as they go, and added at write time to the highest attempt the folder's files already held for that
# call when this process first wrote a send there. Route ids are random, so one ARETE run's TYCHE runs never share one.
_SENDS_LOCK = threading.Lock()  # Held only to count one send, never while a file is read.
_SENDS = {}  # (folder, call id) -> this process's sends so far
_SEED_LOCK = threading.Lock()  # Held while a folder's files are read, at write time only.
_SEEDS = {}  # folder -> {call id: highest attempt in the folder's files before this process wrote a send there}
_SEND_HEAD = re.compile(rb'\{"schema": "arete\.provider_call/1", "stage": "tyche", "call_id": ("(?:[^"\\\n]|\\.)*"), '
                        rb'"attempt": (\d+)')  # Matched at each line's start only.


def _folder():
    return os.environ.get(ENV, "").strip()


@contextmanager
def route(call_id, operation=None, paid=None):
    """Say what the calls sent inside are for: a ledger call's own sends (its id), or reads that send none (None).

    The lines of every call inside are written when the outermost route ends, once the stage is done with them.
    """
    if not _folder():
        yield
        return
    parent = _ROUTE.get()
    pending = {"calls": [], "open": True} if parent is None else parent["pending"]
    token = _ROUTE.set({"call_id": call_id, "for_call": None, "operation": operation, "paid": paid, "keep": None,
                        "pending": pending})
    try:
        yield
    finally:
        try:
            _ROUTE.reset(token)
        except Exception:
            pass
        if parent is None:
            pending["open"] = False  # A call still ending elsewhere writes its own line.
            _flush(pending["calls"])


def dispatch(request):
    """The route of one dispatched request. Only a paid request, which carries its spend reservation, sends a ledger
    call (spend.route_id); a free catalog read sends none."""
    if not _folder():
        return nullcontext()
    try:
        spend = request.get("spend") or {}
        operation = request.get("tool") if request.get("operation") == "execute" else request.get("operation")
        return route(spend.get("route_id") if spend else None, operation, bool(spend))
    except Exception:
        return nullcontext()


def reading_for(call_id):
    """Name the ledger call that the billing or recovery reads from here on read for; None once they stop."""
    current = _ROUTE.get()
    if current is not None and current["call_id"] is None:
        current["for_call"] = call_id


def keeping(keep):
    """Store each reply of the reads from here on only as keep(raw bytes, url) returns what the stage keeps of it
    (response_kept "as_kept"; None keeps nothing): a billing page lists other runs' records."""
    current = _ROUTE.get()
    if current is not None and current["call_id"] is None:
        current["keep"] = keep


def http(provider, *, kept=False):
    """Keep one line per call of the decorated function, which sends one urllib request.

    It marks the request with sent() as it opens it and reads its reply through read(); an exception it handles itself
    goes to failed(). With kept=True its reply is never stored raw, only as the route's keeping() function returns it.
    Without the switch the function runs unchanged.
    """
    def decorate(function):
        @functools.wraps(function)
        def captured(*args, **kwargs):
            folder = _folder()
            if not folder:
                return function(*args, **kwargs)
            call = _Http(provider, folder, kept)
            token = _CALL.set(call)
            try:
                return function(*args, **kwargs)
            except BaseException as exc:
                call.failed(exc)
                raise
            finally:
                _CALL.reset(token)
                call.end()
        return captured
    return decorate


def sent(request):
    """Mark the urllib request about to be opened; it is returned unchanged."""
    call = _CALL.get()
    if call is not None:
        call.sent(request)
    return request


def read(response, *amount):
    """response.read(*amount), noting the reply's status, headers and bytes. Capture never reads on for the stage: a
    bounded read that stops before the reply's end keeps what the stage read."""
    call = _CALL.get()
    if call is None:
        return response.read(*amount)
    call.reply(response)
    try:
        data = response.read(*amount)
    except BaseException:
        call.cut_short = True  # The read itself failed: the reply could not be read to its end.
        raise
    call.reply(response, data, complete=_read_to_end(response, data, amount))
    return data


def _read_to_end(response, data, amount):
    """Whether a read reached the reply's end, judged from what http.client already knows."""
    try:
        if not amount or amount[0] is None or amount[0] < 0:
            return True  # An unbounded read returns only at the end; a reply that breaks off raises.
        remaining = getattr(response, "length", None)
        return remaining == 0 if type(remaining) is int else len(data) < amount[0]
    except Exception:
        return False


def failed(error):
    """Note an exception the sending function handles itself, such as a timeout it reports in its result."""
    call = _CALL.get()
    if call is not None:
        call.failed(error)


def run(provider, argv, **options):
    """subprocess.run(argv, **options), kept as one line when the switch is on.

    The line keeps the exact bytes, so the child runs in binary mode and its output is decoded here as subprocess's
    own text mode decodes it on POSIX (with universal newlines); the caller receives the same text.
    """
    folder = _folder()
    try:
        call = folder and _Process(provider, folder, argv)
    except Exception:
        call = None
    if not call:
        return subprocess.run(argv, **options)
    text = any(options.get(name) for name in ("text", "universal_newlines", "encoding", "errors"))
    encoding = options.pop("encoding", None) or locale.getpreferredencoding(False)
    errors = options.pop("errors", None) or "strict"
    check = options.pop("check", False)
    options.pop("text", None)
    options.pop("universal_newlines", None)
    if text and isinstance(options.get("input"), str):
        options["input"] = options["input"].encode(encoding, errors)
    try:
        call.started_now()
        completed = subprocess.run(argv, **options)
        call.exited(completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired as exc:
        call.exited(None, exc.stdout, exc.stderr)
        call.failed(exc)
        raise
    except BaseException as exc:
        call.failed(exc)
        raise
    finally:
        call.end()
    if text:
        completed.stdout, completed.stderr = (
            data.decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n") if isinstance(data, bytes)
            else data for data in (completed.stdout, completed.stderr))
    if check and completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, completed.args, completed.stdout, completed.stderr)
    return completed


def _flush(calls):
    """Render and write the lines of a route that has ended, in the order their calls ended."""
    while calls:
        try:
            calls.pop(0).write()
        except Exception:
            pass


class _Call:
    """One attempt. While it runs it only notes plain facts; its line is rendered once the stage is done with it."""
    transport = "http"

    def __init__(self, provider, folder, kept=False):
        self.provider, self.folder, self.kept, self.route = provider, folder, kept, _ROUTE.get()
        self.pending = None if self.route is None else self.route["pending"]
        self.outer = sys.exc_info()[1]  # An exception the caller was already handling is not this call's.
        self.started = self.ended = self.error = self.keep = None
        self.data, self.complete, self.cut_short = None, False, False

    def _sending(self):
        try:
            self._note_send()
        except Exception:
            self.started = None  # Capture never raises into the stage; this call's line is lost.

    def _note_send(self):
        self.started = _clock()
        route = self.route or {}
        self.operation, self.paid = route.get("operation"), route.get("paid")
        if route.get("call_id") is not None:
            self.call_id, self.attempt, self.for_call = route["call_id"], None, None
            with _SENDS_LOCK:
                key = (self.folder, self.call_id)
                self.send = _SENDS[key] = _SENDS.get(key, 0) + 1  # Its attempt is numbered when its line is written.
        else:
            self.call_id, self.attempt, self.for_call, self.keep = None, 1, route.get("for_call"), route.get("keep")

    def failed(self, error):
        if self.error is None:
            self.error, self.ended = error, self.ended or _clock()

    def end(self):
        """The call is over: keep the facts its line needs, let go of every object it saw, and write its line now
        outside any route, or when the outermost route ends."""
        try:
            if self.started is not None:
                self._settle()
        except Exception:
            self.started = None
        finally:
            # Hold nothing past the call: a kept exception or reply would keep its connection open.
            self.error = self.outer = self.route = None
            self.__dict__.pop("request", None)
            self.__dict__.pop("response", None)
        if self.started is None:
            return
        if self.pending is not None and self.pending["open"]:
            self.pending["calls"].append(self)
        else:
            self.write()

    def write(self):
        try:
            if self.call_id is not None:
                seed = _seed(self.folder)
                if seed is None:
                    return  # Its attempt cannot be numbered, so its line is lost; the ledger still shows the call.
                self.attempt = seed.get(self.call_id, 0) + self.send
            _write(self.folder, self.line())
        except Exception:
            pass  # Capture never raises into the stage; the ledger still shows the call.
        finally:
            self.__dict__.clear()

    def _ending(self, chain, replied, broke=False):
        """(outcome, the text of the exception that ended the call, how its reply is kept)."""
        outcome, cause = "answered", self.error
        if self.error is not None and not self.complete:
            for name, test in (("timeout", _timed_out), ("transport_error", _transport_failed)):
                found = next((exc for exc in chain if test(exc)), None)
                if found is not None:
                    outcome, cause = name, found
                    break
            else:
                if not replied or self.cut_short:
                    outcome = "interrupted"  # The stage's own exception while the call ran.
        # After the reply came, an exception is the stage's own decision about it, and is named in `error`.
        if not replied and self.data is None:
            kept = "none"
        elif self.complete:
            kept = "whole"
        elif broke or self.cut_short or outcome in {"timeout", "transport_error"}:
            kept = "broke"  # The reply broke off, or could not be read to its end.
        else:
            kept = "as_read"  # The stage stopped reading (or never read) before the reply's end.
        return outcome, None if cause is None else f"{type(cause).__name__}: {cause}", kept

    def _reply(self, url, secrets, gaps):
        """The reply's fields. A read route's keeping() function (or kept=True) stores only what the stage keeps of
        the reply it read to its end, with the sha256 and size of the raw reply; of a reply it did not read to its end
        (one it stopped reading, never read, or that broke off) it keeps nothing, and the sha256 and size are null."""
        data, kept = self.data, self.kept_as
        if kept == "none" or (self.keep is None and not self.kept):
            return _reply(data, kept, secrets, gaps)
        if not self.complete or data is None:
            return {"response": None, "response_sha256": None, "response_bytes": None, "response_kept": "as_kept"}
        raw = data
        fields = {"response": None, "response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw),
                  "response_kept": "as_kept"}
        try:
            value = None if self.keep is None else self.keep(raw, url)
            if value is not None and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            gaps.append("what the stage keeps of this reply could not be made, so none of it is stored")
            return fields
        if value is not None:
            text = value.encode("utf-8", errors="replace")
            fields["response"] = _text(text, secrets, gaps, "the kept reply")
            fields["response_kept"] = "cut" if len(text) > BOUND else "as_kept"
        return fields

    def _head(self, operation, secrets):
        line = {"schema": SCHEMA, "stage": "tyche", "call_id": self.call_id}
        if self.for_call is not None:
            line["for_call"] = self.for_call
        line.update(attempt=self.attempt, provider=self.provider,
                    operation=_scrub(str(self.operation or operation), secrets)[0], paid=self.paid,
                    transport=self.transport)
        return line

    def _times(self):
        ended = self.ended or self.settled
        return {"started_at": _iso(self.started[0]), "ended_at": _iso(ended[0]),
                "latency_ms": max(0, round((ended[1] - self.started[1]) * 1000))}


class _Http(_Call):
    def __init__(self, provider, folder, kept=False):
        super().__init__(provider, folder, kept)
        self.request = self.response = None

    def sent(self, request):
        if self.request is None:
            self.request = request
            self._sending()

    def reply(self, response, data=None, complete=True):
        self.response = response
        if data is not None:
            self.data, self.complete, self.ended = data, complete, self.ended or _clock()

    def _settle(self):
        self.settled = _clock()
        request, response = self.request, self.response
        chain = _chain(self.error, self.outer)
        if response is None:  # An HTTP error status is a reply, even one the stage never read.
            response = next((exc for exc in chain if isinstance(exc, HTTPError)), None)
        broke = next((exc.partial for exc in chain if isinstance(exc, IncompleteRead)), None)
        if self.data is None:  # http.client keeps what arrived of a reply that broke off.
            self.data = broke
        self.outcome, self.cause, self.kept_as = self._ending(chain, response is not None, broke is not None)
        self.method, self.url, self.sent_headers = request.get_method(), request.full_url, request.header_items()
        self.body = request.data if isinstance(request.data, bytes) else None
        self.status = _status(response)
        self.got_headers = None if response is None else list(response.headers.items())
        _hold(self.url, self.sent_headers)

    def line(self):
        secrets, gaps = _secrets(), []
        line = self._head(urlsplit(self.url).path, secrets)
        line.update(method=self.method, url=_url(self.url, secrets), request_headers=_headers(self.sent_headers, secrets),
                    **_request(self.body, secrets, gaps), **self._times(), http_status=self.status,
                    response_headers=None if self.got_headers is None else _headers(self.got_headers, secrets),
                    **self._reply(self.url, secrets, gaps), outcome=self.outcome,
                    error=_error(self.cause, secrets, gaps), gaps=gaps)
        return line


class _Process(_Call):
    transport = "subprocess"

    def __init__(self, provider, folder, argv):
        super().__init__(provider, folder)
        self.argv, self.exit_code, self.stderr = list(argv), None, None

    def started_now(self):
        self._sending()

    def exited(self, code, stdout, stderr):
        self.exit_code, self.data, self.stderr, self.complete = code, stdout, stderr, code is not None
        self.ended = _clock()

    def _settle(self):
        self.settled = _clock()
        # A child killed at its timeout has a partial reply: it broke off.
        self.outcome, self.cause, self.kept_as = self._ending(_chain(self.error, self.outer),
                                                              self.exit_code is not None, self.data is not None)
        self.argv = [os.fsdecode(part) for part in self.argv]
        self.body = self.unstored = None
        try:  # The Deepline CLI reads its payload from `--input @file`: that file is the body it sent.
            path = next((value[1:] for flag, value in zip(self.argv, self.argv[1:])
                         if flag == "--input" and value.startswith("@")), None)
            self.body = None if path is None else Path(path).read_bytes()
        except Exception:
            self.unstored = "the request body (the --input file) could not be read"

    def line(self):
        secrets = _secrets()
        gaps = [self.unstored] if self.unstored else []
        line = self._head(" ".join(self.argv[1:3]), secrets)
        line.update(argv=[_scrub(part, secrets)[0] for part in self.argv], request_headers={},
                    **_request(self.body, secrets, gaps), **self._times(), exit_code=self.exit_code,
                    response_headers={}, **self._reply(None, secrets, gaps),
                    stderr=None if self.stderr is None else _text(self.stderr, secrets, gaps, "stderr"),
                    outcome=self.outcome, error=_error(self.cause, secrets, gaps), gaps=gaps)
        return line


def _clock():
    return time.time(), time.monotonic()


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _chain(error, outer=None):
    """The exception and its causes, stopping at the one the caller was already handling."""
    seen, pending = [], [error]
    while pending:
        exc = pending.pop(0)
        if exc is not None and exc is not outer and all(exc is not other for other in seen):
            seen.append(exc)
            pending += [exc.__cause__, exc.__context__]
    return seen


def _timed_out(exc):
    if isinstance(exc, (TimeoutError, socket.timeout, subprocess.TimeoutExpired)):
        return True
    reason = getattr(exc, "reason", None)
    return (isinstance(exc, URLError) and not isinstance(exc, HTTPError)
            and (isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()))


def _transport_failed(exc):
    return isinstance(exc, (OSError, HTTPException)) and not isinstance(exc, HTTPError)


def _status(response):
    status = getattr(response, "code", None)
    return getattr(response, "status", None) if status is None else status


def _hold(url, headers):
    """Remember every credential a request sent in an authorization header or a credential query parameter."""
    sent = set()
    for name, value in headers:
        if str(name).lower() in {"authorization", "proxy-authorization"}:
            sent |= {str(value), str(value).strip().partition(" ")[2]}
    for part in url.partition("?")[2].partition("#")[0].split("&"):
        name, separator, value = part.partition("=")
        if separator and unquote_plus(name).lower() in _CREDENTIAL_PARAMS:
            sent.add(unquote_plus(value))
    sent = {value for value in sent if len(value.strip()) >= SHORTEST_SECRET}
    with _HELD_LOCK:
        _HELD.update(sent)


def _file_keys():
    """The keys TYCHE and the Deepline CLI load from files: every .env.deepline from the working folder up, and the
    CLI's host credentials (~/.local/deepline/<host>/.env)."""
    paths = []
    try:
        here = Path.cwd()
        paths += [folder / name for folder in (here, *here.parents) for name in _KEY_FILES]
    except Exception:
        pass
    try:
        paths += sorted((Path.home() / _CLI_KEYS).glob("*/.env"))
    except Exception:
        pass
    keys = set()
    for path in paths:
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                text = stream.read(65537)
        except Exception:
            continue
        for row in text.splitlines():
            name, separator, value = row.strip().partition("=")
            if separator and name.strip().upper().endswith(("_API_KEY", "_TOKEN", "_KEY", "_SECRET")):
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                keys.add(value)
    return keys


def _route_tokens():
    """The model route's token: the value after `Bearer ` in each http_headers.Authorization of the JSON settings in
    CODEX_PROVIDER_OVERRIDES (flat keys such as model_providers.<id>.http_headers.Authorization, or nested)."""
    tokens, pending = set(), []
    try:
        pending.append(("", json.loads(os.environ.get(_ROUTE_ENV) or "null")))
    except Exception:
        return tokens
    while pending:
        path, value = pending.pop()
        if isinstance(value, dict):
            pending += [((path + "." if path else "") + str(key), item) for key, item in value.items()]
        elif isinstance(value, str) and path.lower().endswith("http_headers.authorization"):
            tokens |= {value, value.strip().partition(" ")[2]}
    return tokens


def _secrets():
    """Every credential value a line could carry, longest first: each *_API_KEY and *_TOKEN in the environment,
    every key TYCHE or the Deepline CLI loads from a file, the model route's token, and every value this process has
    sent as one; as raw and stripped values and their JSON-escaped, repr and URL-encoded forms."""
    values = {value for name, value in os.environ.items() if name.upper().endswith(("_API_KEY", "_TOKEN"))}
    values |= _file_keys() | _route_tokens()
    with _HELD_LOCK:
        values |= _HELD
    forms = set()
    for value in values:
        if len(value.strip()) < SHORTEST_SECRET:
            continue
        for base in {value, value.strip()}:
            forms |= {base, json.dumps(base)[1:-1], repr(base)[1:-1], quote(base), quote(base, safe=""),
                      quote_plus(base)}
    forms.discard("")
    return sorted(forms, key=len, reverse=True)


def _scrub(text, secrets):
    found = False
    for secret in secrets:
        if secret in text:
            text, found = text.replace(secret, REDACTED), True
    return text, found


def _scrubbed(text, secrets, gaps):
    """Redact a body, reply, stderr or error text; a credential found there is a gap."""
    text, found = _scrub(text, secrets)
    if found and CREDENTIAL_GAP not in gaps:
        gaps.append(CREDENTIAL_GAP)
    return text


def _url(url, secrets):
    base, mark, rest = url.partition("?")
    query, hash_mark, fragment = rest.partition("#")
    parts = []
    for part in query.split("&") if mark else ():
        name, separator, value = part.partition("=")
        parts.append(name + "=" + REDACTED if separator and unquote_plus(name).lower() in _CREDENTIAL_PARAMS else part)
    return _scrub(base + mark + "&".join(parts) + hash_mark + fragment, secrets)[0]


def _headers(items, secrets):
    kept = {}
    for name, value in items:
        name = str(name).lower()
        if name in _HIDDEN_HEADERS or any(word in name for word in _KEY_HEADER_WORDS):
            continue
        value = _scrub(str(value), secrets)[0]
        kept[name] = kept[name] + ", " + value if name in kept else value
    return kept


def _request(body, secrets, gaps):
    if body is None:
        return {"request": None, "request_sha256": None, "request_bytes": 0}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
        gaps.append("the request body is not UTF-8, so its stored text replaces undecodable bytes")
    return {"request": _scrubbed(text, secrets, gaps), "request_sha256": hashlib.sha256(body).hexdigest(),
            "request_bytes": len(body)}


def _text(raw, secrets, gaps, what):
    """Bytes as stored text: every credential redacted first, so no part of one survives the cut to BOUND bytes."""
    over = len(raw) > BOUND
    if over:
        gaps.append(f"{what} was over {BOUND} bytes; only its first {BOUND} bytes are kept")
    found = False
    for secret in secrets:
        try:
            value = secret.encode("utf-8")
        except UnicodeError:
            continue
        if value in raw:
            raw, found = raw.replace(value, REDACTED.encode()), True
    if found and CREDENTIAL_GAP not in gaps:
        gaps.append(CREDENTIAL_GAP)
    return (raw[:BOUND] if over else raw).decode("utf-8", errors="replace")


def _broke(raw):
    return f"the reply did not arrive whole; only the {len(raw)} bytes that arrived are kept"


def _reply(data, kept, secrets, gaps):
    """The raw reply: whole; as_read when the stage stopped reading early (what it read, no gap); cut when it was
    over BOUND or broke off (always with a gap); none when no reply arrived. The sha256 and size are of every byte
    that arrived or that the stage read."""
    if kept == "none":
        return {"response": None, "response_sha256": None, "response_bytes": 0, "response_kept": "none"}
    data = data or b""
    if kept == "broke":
        gaps.append(_broke(data))
        kept = "cut"
    return {"response": _text(data, secrets, gaps, "the reply"), "response_sha256": hashlib.sha256(data).hexdigest(),
            "response_bytes": len(data), "response_kept": "cut" if len(data) > BOUND else kept}


def _error(cause, secrets, gaps):
    return None if cause is None else _scrubbed(cause, secrets, gaps)[:300]


def _seed(folder):
    """The highest attempt of each call id in the folder's files, read once, when this process first writes a send
    there; None when capture's lock is not free in time. A file that cannot be read adds nothing."""
    if not _SEED_LOCK.acquire(timeout=_WRITE_WAIT):
        return None
    try:
        if folder not in _SEEDS:
            highest = {}
            for path in sorted(Path(folder).glob("provider-calls-*.jsonl")):
                try:
                    _highest(path, highest)
                except Exception:
                    continue
            _SEEDS[folder] = highest
        return _SEEDS[folder]
    finally:
        _SEED_LOCK.release()


def _highest(path, highest):
    """Note the highest attempt of each send line's call id in one file, read from its line heads alone."""
    with open(path, "rb") as stream:
        if not os.fstat(stream.fileno()).st_size:
            return
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            start = 0
            while start >= 0:
                match = _SEND_HEAD.match(data, start)
                if match:
                    call_id, attempt = json.loads(match.group(1)), int(match.group(2))
                    highest[call_id] = max(attempt, highest.get(call_id, 0))
                start = data.find(b"\n", start)
                start = start + 1 if start >= 0 else -1


def _last_byte(fd, size):
    try:
        return os.pread(fd, 1, size - 1)
    except (AttributeError, OSError):
        return b"\n"


def _write(folder, line):
    """Append one whole line with one write; a line that cannot be written is lost.

    A file whose last byte is not a newline (a line cut short by a crash or a full disk) gets one first, so the two
    never fuse. The lock is capture's own, and a writer waits for it only so long.
    """
    data = (json.dumps(line, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
    if not _WRITE.acquire(timeout=_WRITE_WAIT):
        return
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, f"provider-calls-{os.getpid()}.jsonl")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            size = os.fstat(fd).st_size
            if size and _last_byte(fd, size) != b"\n":
                data = b"\n" + data
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    finally:
        _WRITE.release()
