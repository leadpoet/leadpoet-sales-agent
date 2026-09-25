"""PydanticAI harness for live lead sourcing."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext, Tool, ToolOutput, messages
from pydantic_ai.capabilities import PrepareTools, ProcessHistory
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage, UsageLimits

from experiments.harness_bakeoff import evidence, reverify
from experiments.harness_bakeoff.models import (
    CompaniesResult,
    _canonical_company_stage,
    companies_result_model,
    _canonical_employee_band,
    validate_companies,
)
from experiments.harness_bakeoff.contacts import (
    add_investor_relations_hints,
    bind_homepage_pages,
    cite_company_records,
    enrich_contacts,
    publish_homepage_linkedin,
)
from experiments.harness_bakeoff.prompt import build_prompt, system_prompt
from experiments.harness_bakeoff.tool_client import ToolClient
from experiments.harness_bakeoff.tool_contract import (
    TOOL_DESCRIPTIONS,
    tool_input_schema,
)


DEFAULT_MODEL = "openai/gpt-5.6-sol"
LAST_USAGE: dict[str, Any] = {}
_RESEARCH_TOOL_NAMES = frozenset(
    {
        "search_companies",
        "get_company_profile",
        "get_company_events",
        "search_web",
        "fetch_page",
    }
)
_COMPACTABLE_TOOL_NAMES = _RESEARCH_TOOL_NAMES - {"fetch_page"}
_MAX_PRIOR_TOOL_RESULT_BYTES = 1_200
_FINALIZE_INPUT_TOKENS = 170_000
_FINALIZE_REQUESTS = 22
_FINALIZE_TOOL_CALLS = 26
_PROVIDER_CALL_QUOTA = 30  # the Arena's per-ICP Deepline quota
# get_company_profile alone can cost three Deepline calls, so the reserve and
# the hard stop are measured in provider calls, not tool calls.
_FINALIZE_DEEPLINE_CALLS = 26
_RESEARCH_DEEPLINE_CALLS = 28
# Under contacts_v1 a company without a verified contact scores zero, so a run
# that requires contacts keeps Deepline calls back for the contact pass: one
# role-matched search plus up to three profile lookups per company.
_BASE_FINALIZE_DEEPLINE_CALLS = _FINALIZE_DEEPLINE_CALLS
_BASE_RESEARCH_DEEPLINE_CALLS = _RESEARCH_DEEPLINE_CALLS
# Per company the contact pass spends one homepage fetch, one company record,
# one people search and up to four profile lookups; the reserve covers the
# first two or three companies and the rest use what research left unused.
_CONTACT_DEEPLINE_RESERVE = 14
_CONTACT_SUBMIT_RESERVE_SECONDS = 2.0
# Profile lookups take 10-20 seconds, so a call needs this much of the run
# window left; the client timeout is capped for the whole pass instead of per
# call because the lookups run on several threads.
_CONTACT_MIN_CALL_SECONDS = 25.0
_CONTACT_CALL_TIMEOUT_SECONDS = 45.0
# Title-filtered people searches at smaller companies returned nobody in every
# measured case, so the pass searches only companies of at least this size when
# the ICP band allows larger ones.
_CONTACT_MIN_EMPLOYEES = 50
# The fit re-check runs before the contact pass in contact rounds, so it gets
# a shorter window there to leave the profile lookups their time.
_CONTACT_REVERIFY_TIMEOUT_SECONDS = 30.0
# Under the contact rules a company earns credit only with a verified employee
# holding a target title. Measured on the arena-2026-09-14 ICPs, Seed and
# Series A companies (11-50 staff) never yielded one, so the run returns
# nothing for those stages. Series B is researched again: the organizer
# confirmed the cost rule will not change and no entry has been cost-eligible
# under it, so the round is decided by score, and Series B ICPs do yield
# contacts. BAKEOFF_CONTACT_SKIP_STAGES overrides the list.
_CONTACT_SKIP_STAGES = frozenset({"pre-seed", "preseed", "seed", "series a"})
_ACTIVE_BUDGET: "_ToolBudget | None" = None
# The Arena sandbox kills a run at 300 seconds. The final structured output
# request can itself take a minute, so research must stop well before that:
# a run that is killed mid-research scores zero for the whole ICP.
# The earlier 800 s finalize point never fired: in Arena mode agent.run is still
# cut off by the 285 s run_timeout, so a model that had not stopped by itself lost
# the whole ICP (arena 09-24 ICP06). Finalize at 220 s, leaving time for the answer.
# The 880 s hard deadline below only bounds post-run evidence checks.
_ARENA_FINALIZE_ELAPSED_SECONDS = float(os.environ.get("BAKEOFF_ARENA_FINALIZE_SECONDS") or 220.0)
_EVIDENCE_MIN_SECONDS = 25.0
_HOMEPAGE_LINKEDIN_MIN_SECONDS = 30.0
_HOMEPAGE_LINKEDIN_FETCH_SECONDS = 20.0
_REVERIFY_MIN_SECONDS = 45.0
_ARENA_HARD_DEADLINE_SECONDS = float(os.environ.get("BAKEOFF_ARENA_HARD_DEADLINE_SECONDS") or 880.0)
_LOCAL_FINALIZE_ELAPSED_SECONDS = 540.0
# Per-ICP model spend. The round budget is shared by all twenty ICPs, and a
# challenger that exhausts it mid-round returns nothing for every later ICP.
_RUN_COST_LIMIT_USD = Decimal("1.75")
_RUN_STARTED_AT: float | None = None
_FINALIZE_ELAPSED_SECONDS = _LOCAL_FINALIZE_ELAPSED_SECONDS
_ARENA_REQUEST_OUTPUT_TOKENS = 4_096
_RUN_OUTPUT_TOKENS_LIMIT = 15_000
_FINALIZE_MARKER = "[research-budget-reserve]"
_STATUS_MARKER = "[research-status]"
_MIN_COMPANIES_BEFORE_EARLY_STOP = 3
_FINALIZE_PROMPT = (
    f"{_FINALIZE_MARKER} Research is complete because the run must reserve capacity "
    "for its final structured output. Do not request more research tools. Call "
    "submit_companies now. Include every company for which you have already read, in tool "
    "results: a fetched article, a fetched job page, or a job listing returned by "
    "get_company_events naming the required index-0 event with its date, "
    "the company's own website, an employee band from the profile or an article, its HQ "
    "country, and evidence consistent with the required stage. A missing bonus signal is "
    "never a reason to omit a company. Omit only a company missing one of those facts. "
    "Preserve exact evidence dates, URLs, and quotes; do not invent missing facts. An empty "
    "submission scores zero, so submit what you proved."
)
_KNOWN_COMPANY_STAGES = frozenset(
    {
        "Seed",
        "Bootstrapped",
        "Series A",
        "Series B",
        "Series C+",
        "Private Equity",
        "Public",
    }
)


def _filter_explicit_stage_conflicts(
    icp: dict[str, Any], companies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop only returned canonical stages that contradict a canonical ICP stage."""

    requested = _canonical_company_stage(icp.get("company_stage"))
    if not isinstance(requested, str) or requested not in _KNOWN_COMPANY_STAGES:
        return companies

    return [
        company
        for company in companies
        if not (
            (returned := _canonical_company_stage(company.get("company_stage")))
            in _KNOWN_COMPANY_STAGES
            and returned != requested
        )
    ]


def _domain_key(value: Any) -> str:
    """Registrable-ish host for exclusion and duplicate checks."""

    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    host = (urlsplit(text).hostname or "").strip(".")
    return host[4:] if host.startswith("www.") else host


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _evaluation_date() -> date:
    raw = (
        os.environ.get("BAKEOFF_EVALUATION_DATE")
        or os.environ.get("LAB_ARENA_EVALUATION_DATE")
        or ""
    ).strip()
    parsed = _parse_date(raw) if raw else None
    return parsed or date.today()


def _harden_output(
    icp: dict[str, Any], companies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Enforce, in code, the output rules that decide the score.

    The judge averages every attached intent signal, subtracts ten points for
    a company whose primary intent it cannot confirm, skips a company whose
    employee band is outside the ICP, and rejects excluded companies. None of
    those outcomes depends on research quality, so none of them should be
    left to the model's discretion at the end of a long run.
    """

    evaluation = _evaluation_date()
    try:
        max_age_days = int(icp.get("intent_max_age_days") or 365)
    except (TypeError, ValueError):
        max_age_days = 365
    oldest_allowed = evaluation - timedelta(days=max(1, max_age_days))

    excluded = {
        _domain_key(item)
        for item in (icp.get("excluded_companies") or [])
        if _domain_key(item)
    }
    example = str(icp.get("verified_example_company") or "").strip().lower()
    raw_bands = icp.get("employee_count")
    # Compare bands in canonical form so "1,001-5,000" and "1001-5000" match.
    allowed_bands = {
        _canonical_employee_band(str(band).strip())
        for band in (raw_bands if isinstance(raw_bands, list) else [raw_bands])
        if str(band or "").strip()
    }
    has_bonus = bool(icp.get("bonus_intents"))

    kept: list[tuple[date, dict[str, Any]]] = []
    seen_domains: set[str] = set()
    for company in companies:
        domain = _domain_key(company.get("company_website"))
        if not domain or domain in seen_domains or domain in excluded:
            continue
        if example and str(company.get("company_name") or "").strip().lower() == example:
            continue
        band = _canonical_employee_band(str(company.get("employee_count") or "").strip())
        if allowed_bands and band not in allowed_bands:
            continue

        signals = list(company.get("intent_signals") or [])
        primaries = [
            (parsed, signal)
            for signal in signals
            if int(signal.get("matched_icp_signal", -1)) == 0
            and (parsed := _parse_date(signal.get("date"))) is not None
            and oldest_allowed <= parsed <= evaluation
        ]
        if not primaries:
            continue
        # The judge sums verified signals under a cap that rises with the
        # number of *distinct-domain* signals (60 -> 80 -> 88), so keep up to
        # three primary signals on different domains, newest first.
        primaries.sort(key=lambda item: item[0], reverse=True)
        best_date = primaries[0][0]
        chosen: list[dict[str, Any]] = []
        used_domains: set[str] = set()
        for _, signal in primaries:
            signal_domain = evidence.evidence_domain(str(signal.get("url") or ""))
            if not signal_domain or signal_domain in used_domains:
                continue
            used_domains.add(signal_domain)
            chosen.append(signal)
            if len(chosen) >= evidence.MAX_SIGNALS_PER_INDEX:
                break
        if has_bonus:
            bonus_by_index: dict[int, tuple[date, dict[str, Any]]] = {}
            for signal in signals:
                index = int(signal.get("matched_icp_signal", -1))
                parsed = _parse_date(signal.get("date"))
                if index <= 0 or parsed is None or parsed > evaluation:
                    continue
                if evidence.evidence_domain(str(signal.get("url") or "")) in used_domains:
                    continue
                current = bonus_by_index.get(index)
                if current is None or parsed > current[0]:
                    bonus_by_index[index] = (parsed, signal)
            chosen.extend(signal for _, signal in sorted(bonus_by_index.values(), key=lambda item: -item[0].toordinal()))

        hardened = dict(company)
        hardened["intent_signals"] = chosen
        hardened["company_linkedin"] = ""
        seen_domains.add(domain)
        kept.append((best_date, hardened))

    # Most recent primary event first: the judge scores in output order and
    # recency is the largest single multiplier on the intent score.
    kept.sort(key=lambda item: item[0], reverse=True)
    return [company for _, company in kept]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")


def _key_priority(key: Any) -> tuple[int, str]:
    normalized = str(key).lower()
    if any(token in normalized for token in ("url", "link", "domain", "website")):
        return (0, normalized)
    if any(
        token in normalized
        for token in (
            "date",
            "time",
            "_at",
            "financing",
            "quote",
            "snippet",
            "title",
            "description",
        )
    ):
        return (1, normalized)
    if any(
        token in normalized
        for token in (
            "company",
            "name",
            "industry",
            "employee",
            "stage",
            "country",
            "state",
            "location",
        )
    ):
        return (2, normalized)
    return (3, normalized)


def _compact_tool_value(
    value: Any,
    *,
    string_chars: int,
    list_items: int,
    dict_items: int,
    depth: int = 0,
    field_name: str = "",
) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        if any(
            token in field_name.lower()
            for token in ("url", "link", "domain", "website", "date", "time")
        ):
            return value
        return value if len(value) <= string_chars else value[:string_chars] + "..."
    if isinstance(value, list):
        return [
            _compact_tool_value(
                item,
                string_chars=string_chars,
                list_items=list_items,
                dict_items=dict_items,
                depth=depth + 1,
                field_name=field_name,
            )
            for item in value[:list_items]
        ]
    if isinstance(value, dict):
        prioritized = sorted(value.items(), key=lambda item: _key_priority(item[0]))
        return {
            str(key): _compact_tool_value(
                item,
                string_chars=string_chars,
                list_items=list_items,
                dict_items=dict_items,
                depth=depth + 1,
                field_name=str(key),
            )
            for key, item in prioritized[:dict_items]
        }
    return value


def _bounded_history_tool_result(value: Any) -> Any:
    """Keep prior evidence useful without replaying full provider payloads forever."""

    if len(_json_bytes(value)) <= _MAX_PRIOR_TOOL_RESULT_BYTES:
        return value
    for string_chars, list_items, dict_items in (
        (320, 5, 40),
        (180, 5, 30),
        (120, 4, 24),
        (80, 3, 18),
        (60, 2, 14),
        (60, 1, 10),
        (50, 1, 10),
    ):
        compacted = _compact_tool_value(
            value,
            string_chars=string_chars,
            list_items=list_items,
            dict_items=dict_items,
        )
        if isinstance(compacted, dict):
            compacted["prior_result_truncated"] = True
        else:
            compacted = {
                "prior_result_truncated": True,
                "result": compacted,
            }
        if len(_json_bytes(compacted)) <= _MAX_PRIOR_TOOL_RESULT_BYTES:
            return compacted

    raw = _json_bytes(value).decode("utf-8", errors="replace")
    preview = raw[:800]
    fallback = {"prior_result_truncated": True, "json_preview": preview}
    while len(_json_bytes(fallback)) > _MAX_PRIOR_TOOL_RESULT_BYTES:
        preview = preview[: max(1, len(preview) // 2)]
        fallback["json_preview"] = preview
    return fallback


def _homepage_binding_enabled() -> bool:
    # The judge proves identity and reads LinkedIn's employee band only when
    # its homepage parser finds the company's LinkedIn link, and its partial
    # page read finds an early link reliably. One fetch per finalist orders
    # the contact spend by that reliability.
    return str(os.environ.get("BAKEOFF_HOMEPAGE_BINDING") or "1").strip() not in {"0", "false", "no"}


def _contact_round_skip_reason(icp: dict[str, Any]) -> str:
    """Why a contact-scored ICP is answered with no companies, or ""."""

    if str(os.environ.get("BAKEOFF_CONTACT_SKIP_EARLY_STAGES") or "1").strip() in {"0", "false", "no"}:
        return ""
    stage = str(icp.get("company_stage") or "").strip().casefold()
    configured = str(os.environ.get("BAKEOFF_CONTACT_SKIP_STAGES") or "").strip()
    skip_stages = (
        {item.strip().casefold() for item in configured.split(",") if item.strip()}
        if configured
        else _CONTACT_SKIP_STAGES
    )
    if stage in skip_stages:
        return (
            f"skipped: {stage} companies rarely employ the target titles, so research "
            "spend here could not earn cost allowance"
        )
    return ""


def _elapsed_seconds() -> float:
    return 0.0 if _RUN_STARTED_AT is None else max(0.0, time.monotonic() - _RUN_STARTED_AT)


def _deepline_calls() -> int:
    budget = _ACTIVE_BUDGET
    return 0 if budget is None else budget.deepline_calls()


def _finalization_due(usage: RunUsage) -> bool:
    return (
        usage.input_tokens >= _FINALIZE_INPUT_TOKENS
        or usage.requests >= _FINALIZE_REQUESTS
        or usage.tool_calls >= _FINALIZE_TOOL_CALLS
        or _deepline_calls() >= _FINALIZE_DEEPLINE_CALLS
        or _elapsed_seconds() >= _FINALIZE_ELAPSED_SECONDS
    )


def _status_line(usage: RunUsage) -> str:
    """One line the model can plan against: real elapsed time and calls left."""

    elapsed = int(_elapsed_seconds())
    remaining = max(0, int(_FINALIZE_ELAPSED_SECONDS - elapsed))
    calls_left = max(0, min(
        _FINALIZE_TOOL_CALLS - int(getattr(usage, "tool_calls", 0) or 0),
        _FINALIZE_DEEPLINE_CALLS - _deepline_calls(),
    ))
    return (
        f"{_STATUS_MARKER} elapsed {elapsed}s; research stops in {remaining}s; "
        f"{calls_left} provider calls left (a profile costs 2, 3 with financing). Do not call submit_companies with fewer than "
        f"{_MIN_COMPANIES_BEFORE_EARLY_STOP} verified companies while more than 60s and 4 "
        "calls remain: run another news search with different wording, then verify its hits."
    )


def _without_status_parts(message: messages.ModelRequest) -> messages.ModelRequest:
    parts = [
        part
        for part in message.parts
        if not (
            isinstance(part, messages.UserPromptPart)
            and isinstance(part.content, str)
            and part.content.startswith(_STATUS_MARKER)
        )
    ]
    return message if len(parts) == len(message.parts) else dataclasses.replace(message, parts=parts)


def _process_history(
    context: RunContext[Any], history: list[messages.ModelMessage]
) -> list[messages.ModelMessage]:
    """Project old tool payloads, refresh the status line, and warn once at the reserve."""

    tool_returns = [
        (message_index, part_index)
        for message_index, message in enumerate(history)
        if isinstance(message, messages.ModelRequest)
        for part_index, part in enumerate(message.parts)
        if isinstance(part, messages.ToolReturnPart)
        and part.tool_name in _COMPACTABLE_TOOL_NAMES
    ]
    prior_returns = set(tool_returns[:-1])
    processed: list[messages.ModelMessage] = []
    for message_index, message in enumerate(history):
        if not isinstance(message, messages.ModelRequest):
            processed.append(message)
            continue
        parts = [
            dataclasses.replace(
                part, content=_bounded_history_tool_result(part.content)
            )
            if (message_index, part_index) in prior_returns
            else part
            for part_index, part in enumerate(message.parts)
        ]
        processed.append(_without_status_parts(dataclasses.replace(message, parts=parts)))

    # Refresh the status on the latest tool return so the model always sees the
    # real clock; the first user prompt carries no status.
    if _RUN_STARTED_AT is not None and len(processed) > 1:
        last = processed[-1]
        if isinstance(last, messages.ModelRequest) and any(
            isinstance(part, messages.ToolReturnPart) for part in last.parts
        ):
            processed[-1] = dataclasses.replace(
                last, parts=[*last.parts, messages.UserPromptPart(_status_line(context.usage))]
            )

    if _finalization_due(context.usage):
        already_warned = any(
            isinstance(part, messages.UserPromptPart)
            and isinstance(part.content, str)
            and _FINALIZE_MARKER in part.content
            for message in processed
            if isinstance(message, messages.ModelRequest)
            for part in message.parts
        )
        if not already_warned:
            last = processed[-1]
            if not isinstance(last, messages.ModelRequest):
                raise RuntimeError(
                    "processed PydanticAI history must end in a model request"
                )
            processed[-1] = dataclasses.replace(
                last, parts=[*last.parts, messages.UserPromptPart(_FINALIZE_PROMPT)]
            )
    return processed


def _prepare_research_tools(
    context: RunContext[Any], tool_definitions: list[ToolDefinition]
) -> list[ToolDefinition]:
    """Leave only the output tool available once the final-output reserve starts."""

    return [] if _finalization_due(context.usage) else tool_definitions


def _run_usage_limits() -> UsageLimits:
    """Keep cumulative run limits separate from the per-request model cap."""

    return UsageLimits(
        cost_limit=_RUN_COST_LIMIT_USD,
        request_limit=30,
        tool_calls_limit=30,
        input_tokens_limit=260_000,
        output_tokens_limit=_RUN_OUTPUT_TOKENS_LIMIT,
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _positive_integer(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be from 1 through {maximum}")
    return value


def _positive_float(name: str, default: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0 or value > maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum:g}")
    return value


class _DeadlineProviderCall:
    """Bound synchronous provider calls to their remaining run window."""

    def __init__(
        self,
        call: Any,
        client: Any,
        deadline: float,
        *,
        reserve_seconds: float = _CONTACT_SUBMIT_RESERVE_SECONDS,
        deadline_error: str = "contact provider deadline reached",
        clock: Any = time.monotonic,
    ) -> None:
        self._call = call
        self._client = client
        self._deadline = deadline
        self._reserve_seconds = reserve_seconds
        self._deadline_error = deadline_error
        self._clock = clock

    def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        available = self._deadline - self._clock() - self._reserve_seconds
        if available < _CONTACT_MIN_CALL_SECONDS:
            raise RuntimeError(self._deadline_error)
        return self._call(name, arguments)


class _ToolBudget:
    def __init__(self, client: ToolClient, maximum: int) -> None:
        self.client = client
        self.maximum = maximum
        self.calls = 0
        self._lock = threading.Lock()
        self._profile_lock = threading.Lock()
        self.pages: dict[str, str] = {}

    def deepline_calls(self) -> int:
        return int(getattr(self.client, "deepline_calls", 0) or 0)

    def page_text(self, url: str) -> str | None:
        """Page text for the evidence pass: cached, else one bounded fetch."""

        if url in self.pages:
            return self.pages[url]
        with self._lock:
            if self.calls >= self.maximum or self.deepline_calls() >= _PROVIDER_CALL_QUOTA - 1:
                return None
            self.calls += 1
        try:
            result = self.client.call("fetch_page", {"url": url, "max_chars": 4000})
        except Exception:  # noqa: BLE001 - unavailable pages are reported by the caller
            return None
        text = str((result or {}).get("text") or "") if isinstance(result, dict) else ""
        self.pages[url] = text
        return text

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name != "submit_companies":
            with self._lock:
                if self.calls >= self.maximum:
                    raise RuntimeError(f"provider-call limit of {self.maximum} exceeded")
                if self.deepline_calls() >= _RESEARCH_DEEPLINE_CALLS:
                    return {"ok": False, "error": "provider budget exhausted: call submit_companies now"}
                self.calls += 1
        try:
            if name == "get_company_profile":
                with self._profile_lock:
                    if self.deepline_calls() >= _RESEARCH_DEEPLINE_CALLS - 2:
                        return {"ok": False, "error": "provider budget exhausted: call submit_companies now"}
                    result = self.client.call(name, arguments)
            else:
                result = self.client.call(name, arguments)
        except Exception as exc:
            if name == "submit_companies":
                raise
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        if name == "fetch_page" and isinstance(result, dict) and isinstance(result.get("text"), str):
            self.pages[str(arguments.get("url") or "")] = result["text"]
        return result

    def contact_call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Contact lookups spend the reserved calls the research gate holds back."""

        with self._lock:
            if self.calls >= self.maximum:
                raise RuntimeError(f"provider-call limit of {self.maximum} exceeded")
            if self.deepline_calls() >= _PROVIDER_CALL_QUOTA:
                raise RuntimeError("Deepline quota exhausted before the contact pass")
            self.calls += 1
        return self.client.call(name, arguments)


async def _run(icp: dict[str, Any]) -> list[dict[str, Any]]:
    global _RUN_STARTED_AT, _FINALIZE_ELAPSED_SECONDS, _RESEARCH_DEEPLINE_CALLS, _FINALIZE_DEEPLINE_CALLS
    _RUN_STARTED_AT = time.monotonic()
    arena_mode = bool(str(os.environ.get("LAB_ARENA_WORKER_SOCKET") or "").strip())
    _FINALIZE_ELAPSED_SECONDS = (
        _ARENA_FINALIZE_ELAPSED_SECONDS if arena_mode else _LOCAL_FINALIZE_ELAPSED_SECONDS
    )
    contact_enabled = (
        icp.get("contact_policy") == "contacts_v1"
        and isinstance(icp.get("target_roles"), list)
        and bool(icp["target_roles"])
    )
    contact_reserve_calls = _CONTACT_DEEPLINE_RESERVE if contact_enabled else 0
    _RESEARCH_DEEPLINE_CALLS = _BASE_RESEARCH_DEEPLINE_CALLS - contact_reserve_calls
    _FINALIZE_DEEPLINE_CALLS = _BASE_FINALIZE_DEEPLINE_CALLS - contact_reserve_calls
    api_key = "arena-host" if arena_mode else _required_environment("OPENROUTER_API_KEY")
    model_name = os.environ.get("BAKEOFF_OPENROUTER_MODEL", DEFAULT_MODEL).strip()
    if not model_name:
        raise RuntimeError("BAKEOFF_OPENROUTER_MODEL cannot be empty")

    max_companies = _positive_integer(
        "LAB_ARENA_COMPANY_LIMIT" if arena_mode else "BAKEOFF_MAX_COMPANIES",
        5,
        5,
    )
    max_provider_calls = _positive_integer("BAKEOFF_MAX_PROVIDER_CALLS", 30, 100)
    run_timeout = _positive_float(
        "BAKEOFF_RUN_TIMEOUT_SECONDS",
        285.0 if arena_mode else 720.0,
        3600.0,
    )
    tool_timeout = _positive_float("BAKEOFF_TOOL_TIMEOUT_SECONDS", 90.0, 600.0)
    tool_client: Any = None
    arena_http_client: httpx.AsyncClient | None = None

    async def close_resources() -> None:
        close = getattr(tool_client, "close", None)
        if callable(close):
            close()
        if arena_http_client is not None:
            await arena_http_client.aclose()

    try:
        if arena_mode:
            from arena_transport import ArenaToolClient, arena_openrouter_http_client

            tool_client = ArenaToolClient(timeout=tool_timeout)
            arena_http_client = arena_openrouter_http_client(timeout=120.0)
            openai_client = AsyncOpenAI(
                api_key=api_key,
                base_url="http://openrouter.ai/api/v1",
                http_client=arena_http_client,
            )
            provider = OpenRouterProvider(openai_client=openai_client)
        else:
            tool_client = ToolClient(timeout=tool_timeout)
            provider = OpenRouterProvider(api_key=api_key)
    except Exception:
        await close_resources()
        raise
    budget = _ToolBudget(tool_client, max_provider_calls)
    tool_client.allow_contacts = contact_enabled
    tool_client.structured_profile = True
    tool_client.intent_details_policy = icp.get("intent_details_policy")
    global _ACTIVE_BUDGET
    _ACTIVE_BUDGET = budget

    def search_companies(
        query: str,
        industry: str = "",
        geography: str = "",
        employee_count: list[str] = [],
        limit: int = 5,
    ) -> Any:
        """Discover candidate companies with Deepline and the supplied ICP filters."""

        return budget.call(
            "search_companies",
            {
                "query": query,
                "industry": industry,
                "geography": geography,
                "employee_count": employee_count,
                "limit": limit,
            },
        )

    def get_company_profile(domain: str, include_financing: bool = False) -> Any:
        """Get Deepline firmographics, LinkedIn band evidence, and optionally financing."""

        return budget.call(
            "get_company_profile",
            {"domain": domain, "include_financing": bool(include_financing)},
        )

    def get_company_events(
        domain: str,
        categories: list[str] = [],
        job_category: str = "",
        limit: int = 5,
    ) -> Any:
        """Find events, optionally filtering jobs by one coarse provider category."""

        return budget.call(
            "get_company_events",
            {
                "domain": domain,
                "categories": categories,
                "job_category": job_category,
                "limit": limit,
            },
        )

    def search_web(
        query: str,
        mode: str = "search",
        limit: int = 5,
        recency_days: int | None = None,
    ) -> Any:
        """Search the public web, news, or jobs for evidence."""

        return budget.call(
            "search_web",
            {
                "query": query,
                "mode": mode,
                "limit": limit,
                "recency_days": recency_days,
            },
        )

    def fetch_page(url: str, max_chars: int = 4000) -> Any:
        """Fetch one public evidence page and return its extracted text."""

        return budget.call("fetch_page", {"url": url, "max_chars": max_chars})

    max_output_tokens = (
        _ARENA_REQUEST_OUTPUT_TOKENS if arena_mode else _RUN_OUTPUT_TOKENS_LIMIT
    )
    model_settings: OpenRouterModelSettings = {
        "max_tokens": max_output_tokens,
        "parallel_tool_calls": os.environ.get("BAKEOFF_PARALLEL_TOOL_CALLS", "1").strip().lower() in {"1", "true", "yes"},
        "timeout": 120,
        "openrouter_reasoning": {
            "effort": os.environ.get("BAKEOFF_REASONING_EFFORT", "medium").strip() or "medium",
            "exclude": arena_mode,
        },
        "openrouter_usage": {"include": True},
    }
    try:
        model = OpenRouterModel(
            model_name,
            provider=provider,
            settings=model_settings,
        )
        agent = Agent(
            model,
            instructions=system_prompt(icp),
            tools=[
                Tool.from_schema(
                    search_companies,
                    "search_companies",
                    TOOL_DESCRIPTIONS["search_companies"],
                    tool_input_schema("search_companies"),
                ),
                Tool.from_schema(
                    get_company_profile,
                    "get_company_profile",
                    TOOL_DESCRIPTIONS["get_company_profile"],
                    tool_input_schema("get_company_profile"),
                ),
                Tool.from_schema(
                    get_company_events,
                    "get_company_events",
                    TOOL_DESCRIPTIONS["get_company_events"],
                    tool_input_schema("get_company_events"),
                ),
                Tool.from_schema(
                    search_web,
                    "search_web",
                    TOOL_DESCRIPTIONS["search_web"],
                    tool_input_schema("search_web"),
                ),
                Tool.from_schema(
                    fetch_page,
                    "fetch_page",
                    TOOL_DESCRIPTIONS["fetch_page"],
                    tool_input_schema("fetch_page"),
                ),
            ],
            output_type=ToolOutput(
                companies_result_model(icp.get("intent_details_policy")),
                name="submit_companies",
                description=TOOL_DESCRIPTIONS["submit_companies"],
                strict=True,
            ),
            capabilities=[
                ProcessHistory(_process_history),
                PrepareTools(_prepare_research_tools),
            ],
            model_settings=model_settings,
            tool_timeout=tool_timeout,
        )
    except Exception:
        await close_resources()
        raise
    run_usage = RunUsage()
    try:
        skip_reason = _contact_round_skip_reason(icp) if contact_enabled else ""
        if skip_reason:
            LAST_USAGE["evidence_report"] = [skip_reason]
            budget.call("submit_companies", {"companies": []})
            return []
        result = await asyncio.wait_for(
            agent.run(
                build_prompt(icp, max_companies=max_companies),
                usage_limits=_run_usage_limits(),
                usage=run_usage,
            ),
            timeout=run_timeout,
        )
        companies = validate_companies(
            result.output.model_dump(mode="json"),
            max_companies,
            intent_details_policy=icp.get("intent_details_policy"),
        )
        companies = _filter_explicit_stage_conflicts(icp, companies)
        companies = _harden_output(icp, companies)
        report: list[str] = []
        companies = evidence.verify_companies(
            icp,
            companies,
            budget.page_text,
            seconds_left=lambda: (
                (_ARENA_HARD_DEADLINE_SECONDS if arena_mode else run_timeout) - _elapsed_seconds()
            ),
            min_seconds=_EVIDENCE_MIN_SECONDS,
            report=report,
        )
        LAST_USAGE["evidence_report"] = report
        deadline = _ARENA_HARD_DEADLINE_SECONDS if arena_mode else run_timeout
        if companies and deadline - _elapsed_seconds() >= _REVERIFY_MIN_SECONDS:
            base_url = "http://openrouter.ai/api/v1" if arena_mode else "https://openrouter.ai/api/v1"
            http = arena_http_client if arena_mode else httpx.AsyncClient(timeout=httpx.Timeout(60.0))
            headers = {} if arena_mode else {"Authorization": f"Bearer {api_key}"}

            async def post_json(body: dict[str, Any]) -> dict[str, Any] | None:
                response = await http.post(base_url + "/chat/completions", json=body, headers=headers)
                return response.json() if response.status_code == 200 else None

            try:
                companies = await asyncio.wait_for(
                    reverify.rerank(companies, icp, post_json=post_json, report=report),
                    timeout=max(5.0, min(
                        _CONTACT_REVERIFY_TIMEOUT_SECONDS if contact_enabled else reverify.TIMEOUT_SECONDS + 5.0,
                        deadline - _elapsed_seconds() - 10.0,
                    )),
                )
            except Exception:  # noqa: BLE001 - the re-check is best effort
                report.append("fit re-check skipped: timeout or transport error")
            finally:
                if not arena_mode:
                    await http.aclose()
        if contact_enabled and companies:
            # The contact pass runs after the fit re-check so sourced
            # contradictions are dropped before their people lookups are paid
            # for; it is bounded by the run deadline, not the finalize point.
            contact_deadline = _RUN_STARTED_AT + (
                _ARENA_HARD_DEADLINE_SECONDS if arena_mode else run_timeout
            )
            contact_call = _DeadlineProviderCall(
                budget.contact_call, tool_client, contact_deadline, clock=time.monotonic,
            )
            original_timeout = getattr(tool_client, "timeout", None)
            if isinstance(original_timeout, (int, float)):
                tool_client.timeout = min(float(original_timeout), _CONTACT_CALL_TIMEOUT_SECONDS)
            try:
                # The judge binds identity from the homepage's LinkedIn link;
                # check it first so no contact lookup is paid for a company
                # the judge cannot prove, and search through that same page.
                homepage_hints: dict[str, str] = {}
                resolved_records: dict[str, dict[str, Any]] = {}
                if _homepage_binding_enabled() and callable(getattr(tool_client, "fetch_homepage_html", None)):
                    companies, homepage_hints = bind_homepage_pages(
                        companies,
                        lambda url: contact_call("fetch_homepage_html", {"url": url}),
                        report=report,
                    )
                companies = enrich_contacts(
                    icp,
                    companies,
                    contact_call,
                    linkedin_hints={
                        **dict(getattr(tool_client, "linkedin_hints", None) or {}),
                        **homepage_hints,
                    },
                    report=report,
                    fallback_search=False,
                    require_page=True,
                    min_employees=_CONTACT_MIN_EMPLOYEES,
                    lookup_page_by_name=True,
                    resolved=resolved_records,
                    validate_email=True,
                    known_records=getattr(tool_client, "company_records", None),
                )
                companies = cite_company_records(companies, resolved_records, report=report)
                companies = add_investor_relations_hints(
                    [company for company in companies if company.get("contact")],
                    lambda query: contact_call("search_web", dict(query)),
                    report=report,
                ) + [company for company in companies if not company.get("contact")]
            finally:
                if isinstance(original_timeout, (int, float)):
                    tool_client.timeout = original_timeout
            found = sum(1 for company in companies if company.get("contact"))
            report.append(f"contacts: {found} of {len(companies)} companies")
            # A company without a verified contact scores zero and earns no
            # cost allowance, while its spend still counts; keep only rows
            # that can score.
            companies = [company for company in companies if company.get("contact")]
        elif (
            companies
            and _homepage_binding_enabled()
            and callable(getattr(tool_client, "fetch_homepage_html", None))
            and deadline - _elapsed_seconds() >= _HOMEPAGE_LINKEDIN_MIN_SECONDS
        ):
            # The judge reuses an exactly bound LinkedIn company profile as fit
            # evidence; publish only the page the company's own homepage links.
            original_timeout = getattr(tool_client, "timeout", None)
            if isinstance(original_timeout, (int, float)):
                tool_client.timeout = min(float(original_timeout), _HOMEPAGE_LINKEDIN_FETCH_SECONDS)
            try:
                companies = publish_homepage_linkedin(
                    companies,
                    lambda url: tool_client.fetch_homepage_html({"url": url}),
                    report=report,
                )
            finally:
                if isinstance(original_timeout, (int, float)):
                    tool_client.timeout = original_timeout
        companies = validate_companies(
            companies,
            max_companies,
            allow_contacts=contact_enabled,
            intent_details_policy=icp.get("intent_details_policy"),
        )
        budget.call("submit_companies", {"companies": companies})
        return companies
    finally:
        await close_resources()
        usage = dataclasses.asdict(run_usage)
        report = LAST_USAGE.get("evidence_report")
        LAST_USAGE.clear()
        LAST_USAGE.update(json.loads(json.dumps(usage, default=str)))
        LAST_USAGE["provider_calls"] = budget.calls
        if report is not None:
            LAST_USAGE["evidence_report"] = report


def run_icp(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one ICP through a fresh PydanticAI agent."""

    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run(icp))
    raise RuntimeError("run_icp must be called outside an active asyncio event loop")


def get_last_usage() -> dict[str, Any]:
    """Return an isolated copy of usage data from the last completed run."""

    return dict(LAST_USAGE)


__all__ = ["LAST_USAGE", "get_last_usage", "run_icp"]
