"""Stateless stdin/stdout adapter for one public baseline attempt.

The host runs ``preflight`` once before a daily batch. Each ``run`` command
starts the existing fresh worker process through ``run_attempt``.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
import re
import sys
from typing import Any

from experiments.harness_bakeoff.model_preflight import select_model
from experiments.harness_bakeoff.providers import deepline_preflight
from experiments.harness_bakeoff.runner import ATTEMPT_SECONDS, run_attempt
from experiments.harness_bakeoff.secrets import load_provider_secrets


SENTINEL = "PYDANTIC_HARNESS_RESULT_JSON="


def _emit(payload: dict[str, Any]) -> None:
    print(
        SENTINEL
        + json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def _public_error(exc: BaseException, secrets: dict[str, str]) -> str:
    message = f"{type(exc).__name__}: {str(exc)}"
    for secret in secrets.values():
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|token)=([^&\s]+)",
        r"\1=[redacted]",
        message,
    )
    return re.sub(
        r"(?i)(\bbearer\s+)([^\s,;\"'}]+)", r"\1[redacted]", message
    )[:2_000]


def _pricing(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("--model-pricing-json must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--model-pricing-json must be a JSON object")
    return parsed


def _evaluation_date(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("--evaluation-date must use YYYY-MM-DD") from exc
    if normalized != value:
        raise ValueError("--evaluation-date must use normalized YYYY-MM-DD")
    return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument(
        "--deepline-bin",
        default=os.environ.get("BAKEOFF_DEEPLINE_BIN", "deepline"),
    )
    run = commands.add_parser("run")
    run.add_argument("--model", required=True)
    run.add_argument("--model-pricing-json", required=True)
    run.add_argument("--evaluation-date")
    run.add_argument("--max-companies", type=int, default=5)
    run.add_argument(
        "--deepline-bin",
        default=os.environ.get("BAKEOFF_DEEPLINE_BIN", "deepline"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    secrets: dict[str, str] = {}
    try:
        secrets = load_provider_secrets()
        if args.command == "preflight":
            deepline = deepline_preflight(
                deepline_api_key=secrets["DEEPLINE_API_KEY"],
                deepline_bin=args.deepline_bin,
            )
            model = select_model(secrets["OPENROUTER_API_KEY"])
            _emit(
                {
                    "ok": True,
                    "action": "preflight",
                    "deepline": deepline,
                    "selected_model": str(model["selected"]),
                    "model_pricing": (
                        model.get("pricing")
                        if isinstance(model.get("pricing"), dict)
                        else {}
                    ),
                    "model_probe": (
                        model.get("probe")
                        if isinstance(model.get("probe"), dict)
                        else {}
                    ),
                    "model_errors": (
                        model.get("errors")
                        if isinstance(model.get("errors"), list)
                        else []
                    ),
                }
            )
            return 0

        model = str(args.model).strip()
        if not model:
            raise ValueError("--model must not be empty")
        if not 1 <= int(args.max_companies) <= 5:
            raise ValueError("--max-companies must be between 1 and 5")
        icp = json.load(sys.stdin)
        if not isinstance(icp, dict):
            raise ValueError("stdin must contain one ICP JSON object")
        attempt = run_attempt(
            arm="pydantic_ai",
            icp=icp,
            provider_secrets=dict(secrets),
            model=model,
            model_pricing=_pricing(args.model_pricing_json),
            max_companies=int(args.max_companies),
            python=sys.executable,
            deepline_bin=args.deepline_bin,
            evaluation_date=_evaluation_date(args.evaluation_date),
            timeout_seconds=ATTEMPT_SECONDS,
        )
        _emit({"action": "run", **attempt})
        return 0 if attempt.get("ok") is True else 1
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "action": str(getattr(args, "command", "") or "unknown"),
                "error": _public_error(exc, secrets),
            }
        )
        return 1
    finally:
        secrets.clear()


if __name__ == "__main__":
    raise SystemExit(main())
