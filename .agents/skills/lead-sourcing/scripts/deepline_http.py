"""One Deepline execution, retaining error bodies and request IDs for billing."""

import io
import json
import os
import re
import socket
import time
import uuid
from pathlib import Path
from functools import partial
from http.client import HTTPException, HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from provider_output import load_json

API_HOST = "https://code.deepline.com"
# Deepline does not deduplicate a Firecrawl batch submission by key. Its charge is
# read by the submission's own ID after the batch finishes.
UNKEYED_TOOLS = frozenset({"firecrawl_batch_scrape"})
_EXECUTION_KEY = re.compile(r"[A-Za-z0-9._:-]{1,200}")


class BillingUnavailable(ValueError):
    """A billing feed transport failed; another read-only feed may still work."""


def new_execution_key():
    """A fresh key, saved before dispatch, that lets Deepline name this one execution later."""
    return "tyche:" + uuid.uuid4().hex


def _transport_diagnostic(error, stage, started):
    # Exception messages may contain credentials. Types, numeric errno and our
    # own stage labels explain the failure without exposing their contents.
    cause = error.reason if isinstance(error, URLError) else error
    result = {"stage": stage, "error_type": type(error).__name__,
              "elapsed_seconds": round(time.monotonic() - started, 3)}
    if isinstance(cause, BaseException):
        result["cause_type"] = type(cause).__name__
        if type(getattr(cause, "errno", None)) is int:
            result["errno"] = cause.errno
    return result


def _auth_file(path):
    """Read only the SDK's two auth fields; never evaluate shell syntax."""
    try:
        with path.open(encoding="utf-8") as stream:
            raw = stream.read(65537)
    except FileNotFoundError:
        return {}
    if len(raw) > 65536:
        raise ValueError("Deepline auth configuration is too large")
    fields = {}
    for line in raw.splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key.strip() in {"DEEPLINE_HOST_URL", "DEEPLINE_API_KEY"}:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                fields[key.strip()] = value
    return fields


def api_key():
    """Use existing production CLI authentication without printing/copying it.

    Follow the SDK's env, nearest project, then host-scoped credential order.
    Custom CLI hosts/binaries retain their own authentication path.
    """
    explicit = os.environ.get("DEEPLINE_API_KEY", "").strip()
    if os.environ.get("DEEPLINE_BIN", "").strip() and not explicit:
        return None
    project = {}
    for directory in (Path.cwd(), *Path.cwd().parents):
        path = directory / ".env.deepline"
        if path.is_file():
            project = _auth_file(path)
            break
    scoped = _auth_file(Path.home() / ".local/deepline/code-deepline-com/.env")
    host = (os.environ.get("DEEPLINE_HOST_URL") or project.get("DEEPLINE_HOST_URL")
            or scoped.get("DEEPLINE_HOST_URL") or API_HOST).strip().rstrip("/")
    if host != API_HOST:
        return None  # Never forward another host's credentials to production.
    project_key = (project.get("DEEPLINE_API_KEY", "")
                   if project.get("DEEPLINE_HOST_URL", "").rstrip("/") == API_HOST else "")
    if explicit or project_key.strip():
        return explicit or project_key.strip()
    scoped_key = scoped.get("DEEPLINE_API_KEY", "").strip()
    if scoped_key and scoped.get("DEEPLINE_HOST_URL", API_HOST).strip().rstrip("/") != API_HOST:
        # Falling back to the CLI here could select the same mismatched key.
        raise ValueError("Deepline scoped credential belongs to another host")
    return scoped_key or None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None  # Never replay a POST or forward credentials to a redirect.


class _DeadlineReader(io.RawIOBase):
    """Apply the remaining response budget to every underlying socket read."""

    def __init__(self, raw, sock, deadline):
        self.raw, self.sock, self.deadline = raw, sock, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Deepline response deadline reached")
        self.sock.settimeout(remaining)
        return self.raw.readinto(buffer)

    def fileno(self):
        return self.raw.fileno()

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineResponse(HTTPResponse):
    def __init__(self, sock, *args, deadline, **kwargs):
        super().__init__(sock, *args, **kwargs)
        # Wrap before begin() reads headers. Buffering and chunk parsing still
        # belong to http.client; slow headers/chunks cannot reset the deadline.
        self.fp = io.BufferedReader(_DeadlineReader(self.fp.detach(), sock, deadline))


class _ResponseDeadline:
    def __init__(self, deadline, *args, **kwargs):
        self.deadline = deadline
        super().__init__(*args, **kwargs)

    def do_open(self, connection_class, request, **kwargs):
        def connection(*args, **options):
            result = connection_class(*args, **options)
            result.response_class = partial(_DeadlineResponse, deadline=self.deadline)
            return result
        # Preserve urllib's connection, proxy and TLS configuration unchanged.
        return super().do_open(connection, request, **kwargs)


class _DeadlineHTTPHandler(_ResponseDeadline, HTTPHandler):
    pass


class _DeadlineHTTPSHandler(_ResponseDeadline, HTTPSHandler):
    pass


def _opener(deadline):
    return build_opener(NoRedirect(), _DeadlineHTTPHandler(deadline), _DeadlineHTTPSHandler(deadline))


def billing_page(source, *, key, cursor=None, timeout=30):
    """Read billing with the same credentials/host as execution, never a POST.

    The usage feed groups charges. The credit ledger keeps individual posted
    debits; usage is still needed for free/failed attempts with no ledger debit.
    """
    if source not in {"ledger", "usage"}:
        raise ValueError("Unknown Deepline billing source")
    params = {"limit" if source == "ledger" else "recent_limit": "100"}
    if cursor:
        if source == "usage" and (not isinstance(cursor, str) or not cursor.isdecimal()):
            raise ValueError("Invalid Deepline usage offset")
        # The documented usage cursor repeated the same page in production.
        # Offsets advance correctly, including across grouped usage records.
        params["cursor" if source == "ledger" else "recent_offset"] = cursor
    wire = Request(API_HOST + "/api/v2/billing/" + source + "?" + urlencode(params),
                   headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with _opener(time.monotonic() + timeout).open(wire, timeout=timeout) as response:
            return load_json(response.read().decode("utf-8"), billing_feed=True)
    except (OSError, HTTPException):
        raise BillingUnavailable("Read-only Deepline billing feed unavailable; charges remain pending") from None
    except ValueError:
        # A malformed response is not an outage: fail closed, without its text.
        raise ValueError("Invalid Deepline billing response; charges remain pending") from None


def _read(path, *, key, timeout):
    """One read-only GET. A 404 is an answer (no such record), not an outage."""
    wire = Request(API_HOST + path, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        stream = _opener(time.monotonic() + timeout).open(wire, timeout=timeout)
    except HTTPError as exc:
        if exc.code != 404:
            exc.close()
            raise BillingUnavailable("Read-only Deepline lookup unavailable; charges remain pending") from None
        stream = exc
    except (OSError, HTTPException):
        raise BillingUnavailable("Read-only Deepline lookup unavailable; charges remain pending") from None
    try:
        return stream.code, stream.headers, stream.read().decode("utf-8")
    except (OSError, HTTPException):
        raise BillingUnavailable("Read-only Deepline lookup unavailable; charges remain pending") from None
    finally:
        stream.close()


def charge_by_request(request_id, *, key, timeout=30):
    """One call's own billing record, by the ID Deepline allocated before dispatch.

    None means Deepline holds no billing record for the ID. That is not proof of a
    free call: a missing or pending charge is never zero.
    """
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 200 or request_id != request_id.strip():
        raise ValueError("Invalid Deepline request ID; charges remain pending")
    status, _, text = _read("/api/v2/billing/usage?" + urlencode({"request_id": request_id}), key=key, timeout=timeout)
    if status == 404:
        return None
    try:
        return load_json(text, billing_feed=True)
    except ValueError:
        raise ValueError("Invalid Deepline billing response; charges remain pending") from None


def execution_by_key(execution_key, *, key, timeout=30):
    """Deepline's durable record of one keyed execution; never executes a tool.

    None means no execution has the key. The key is useless unless Deepline confirms
    that it honors execution keys, so an unconfirmed answer is an error.
    """
    if not isinstance(execution_key, str) or not _EXECUTION_KEY.fullmatch(execution_key):
        raise ValueError("Invalid Deepline execution key; the charge stays held")
    status, headers, text = _read("/api/v2/executions/by-key/" + quote(execution_key, safe=""), key=key, timeout=timeout)
    if headers.get("X-Deepline-Idempotency-Supported") != "true":
        raise ValueError("Deepline did not confirm keyed executions; the charge stays held")
    if status == 404:
        return None
    try:
        return load_json(text)
    except ValueError:
        raise ValueError("Invalid Deepline execution record; the charge stays held") from None


KEY_REFUSED = "IDEMPOTENCY_NOT_SUPPORTED"
# Fields a reply that ran could carry: output, a charge, a billing ID or other errors. A key refusal
# carries none of them (Deepline's reply, captured live on 2026-10-08).
_RAN_FIELDS = ("data", "result", "results", "toolResponse", "output", "response", "items", "rows", "records",
               "job_id", "request_id", "requestId", "errors")


def stated_codes(body):
    """The error codes a Deepline reply states, at its top level or under error or tool_error.

    A list, so an odd code (a dict) cannot raise.
    """
    if not isinstance(body, dict):
        return []
    codes = [body.get("code"), body.get("error_code"),
             *(body[name].get("code") for name in ("error", "tool_error") if isinstance(body.get(name), dict))]
    return [code for code in codes if code is not None]


def key_refused(response):
    """Deepline refused the execution key itself: HTTP 422 IDEMPOTENCY_NOT_SUPPORTED.

    Every code the reply states must be that one, and it must show no sign of having run: no
    charge, billing ID, output or other errors. Deepline's recovery docs: the tool or mode cannot
    honor a key, a keyed launch-only request is rejected before provider dispatch, and the reply
    names the alternative to follow before submitting work. Without a key, a call is sent as
    every call was before keys.
    """
    if not isinstance(response, dict) or response.get("http_status") != 422:
        return False
    body = response.get("body")
    if not isinstance(body, dict):
        return False
    stated, billing = stated_codes(body), body.get("billing")
    charged = billing is not None and (not isinstance(billing, dict) or any(
        billing.get(field) not in (None, 0) for field in ("credits_charged", "cost_usd")))
    return (bool(stated) and all(code == KEY_REFUSED for code in stated) and not charged
            and not any(body.get(field) for field in _RAN_FIELDS))


def execute(request, key=None, execution_key=None):
    """No automatic retry: transport failures can have an unknown charge.

    An execution key, saved in the ledger before this call, lets Deepline name the
    execution and its charge later if the response is lost. Not every tool takes
    one: a call whose key Deepline refuses is sent once more, unkeyed, within the
    call's own time, and its receipt keeps the refusal.
    """
    if execution_key is not None and (not isinstance(execution_key, str)
                                      or not _EXECUTION_KEY.fullmatch(execution_key)):
        raise ValueError("Invalid Deepline execution key; request was not sent")
    started = time.monotonic()
    response = _execute(request, key, execution_key)
    if execution_key is not None and key_refused(response):
        left = max(1, request["timeout_seconds"] - (time.monotonic() - started))
        refusal = {name: response[name] for name in ("http_status", "headers", "body") if name in response}
        response = dict(_execute(dict(request, timeout_seconds=left), key, None), execution_key_refused=refusal)
    return response


def _execute(request, key, execution_key):
    headers = {"Authorization": "Bearer " + (key or os.environ["DEEPLINE_API_KEY"].strip()),
               "Content-Type": "application/json", "Accept": "application/json",
               "X-Deepline-Tool-Error-Schema": "1",
               "X-Deepline-Execute-Response-Contract": "raw-v2",
               "X-Deepline-Execute-Response-Intent": "raw"}
    if execution_key is not None:
        headers["Idempotency-Key"] = execution_key
    wire = Request(
        API_HOST + "/api/v2/integrations/" + quote(request["tool"], safe="") + "/execute",
        data=json.dumps({"payload": request["payload"]}, allow_nan=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    response = {"body": "", "stderr": "", "headers": {}}
    stream = None
    started, stage = time.monotonic(), "opening_response"
    try:
        try:
            stream = _opener(started + request["timeout_seconds"]).open(wire, timeout=request["timeout_seconds"])
        except HTTPError as exc:
            stream = exc  # An error response still carries authoritative IDs/billing.
        response["http_status"] = stream.code
        response["headers"] = {key: stream.headers[key] for key in
            ("x-deepline-request-id", "x-request-id", "x-vercel-id") if stream.headers.get(key)}
        stage = "reading_response"
        raw = stream.read().decode("utf-8", errors="replace")
        try:
            response["body"] = load_json(raw)
        except ValueError:
            response["body"] = raw
        response["exit_code"] = 0 if 200 <= stream.code < 300 else 1
    except (TimeoutError, socket.timeout) as exc:
        response.update(timed_out=True, transport=_transport_diagnostic(exc, stage, started))
    except (URLError, OSError, HTTPException) as exc:
        # Do not copy network exception text, which may contain credentials.
        response.update(exit_code=1, stderr="Deepline transport failed; execution and billing remain unknown",
                        transport=_transport_diagnostic(exc, stage, started))
        if isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError):
            response["timed_out"] = True  # urllib wraps timeouts while reading headers.
    finally:
        if stream is not None:
            stream.close()
    return response
