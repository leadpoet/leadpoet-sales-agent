from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import unittest
from unittest.mock import patch

import production_runner


PROVIDERS = {
    "OPENROUTER_API_KEY": "openrouter-secret",
    "DEEPLINE_API_KEY": "deepline-secret",
    "SCRAPINGDOG_API_KEY": "scrapingdog-secret",
}


def _payload(output: StringIO) -> dict:
    lines = output.getvalue().splitlines()
    sentinel_lines = [
        line for line in lines if line.startswith(production_runner.SENTINEL)
    ]
    if len(sentinel_lines) != 1:
        raise AssertionError(f"expected one sentinel line, got {sentinel_lines!r}")
    return json.loads(sentinel_lines[0].removeprefix(production_runner.SENTINEL))


class ProductionRunnerTests(unittest.TestCase):
    @patch("production_runner.select_model")
    @patch("production_runner.deepline_preflight")
    @patch("production_runner.load_provider_secrets")
    def test_preflight_emits_reusable_model_and_safe_provider_status(
        self, load_secrets, deepline_check, model_check
    ) -> None:
        load_secrets.return_value = dict(PROVIDERS)
        deepline_check.return_value = {"connected": True, "health": "ok"}
        model_check.return_value = {
            "selected": "openai/test-model",
            "pricing": {"prompt": "0.000001"},
            "probe": {"tool_call": True},
            "errors": [],
        }
        output = StringIO()

        with redirect_stdout(output):
            returncode = production_runner.main(
                ["preflight", "--deepline-bin", "/opt/deepline"]
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(
            _payload(output),
            {
                "ok": True,
                "action": "preflight",
                "deepline": {"connected": True, "health": "ok"},
                "selected_model": "openai/test-model",
                "model_pricing": {"prompt": "0.000001"},
                "model_probe": {"tool_call": True},
                "model_errors": [],
            },
        )
        deepline_check.assert_called_once_with(
            deepline_api_key="deepline-secret", deepline_bin="/opt/deepline"
        )
        model_check.assert_called_once_with("openrouter-secret")

    @patch("production_runner.select_model")
    @patch("production_runner.run_attempt")
    @patch("production_runner.load_provider_secrets")
    def test_run_uses_raw_icp_and_a_fresh_worker_attempt(
        self, load_secrets, run_attempt, select_model
    ) -> None:
        load_secrets.return_value = dict(PROVIDERS)
        run_attempt.return_value = {
            "ok": True,
            "companies": [],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        icp = {"icp_id": "icp_today", "employee_count": ["11-50"]}
        output = StringIO()

        with patch("sys.stdin", StringIO(json.dumps(icp))):
            with redirect_stdout(output):
                returncode = production_runner.main(
                    [
                        "run",
                        "--model",
                        "openai/test-model",
                        "--model-pricing-json",
                        '{"prompt":"0.000001","completion":"0.000002"}',
                        "--evaluation-date",
                        "2026-09-03",
                        "--max-companies",
                        "3",
                        "--deepline-bin",
                        "/opt/deepline",
                    ]
                )

        self.assertEqual(returncode, 0)
        self.assertEqual(_payload(output)["action"], "run")
        run_attempt.assert_called_once_with(
            arm="pydantic_ai",
            icp=icp,
            provider_secrets=PROVIDERS,
            model="openai/test-model",
            model_pricing={"prompt": "0.000001", "completion": "0.000002"},
            max_companies=3,
            python=sys.executable,
            deepline_bin="/opt/deepline",
            evaluation_date="2026-09-03",
            timeout_seconds=production_runner.ATTEMPT_SECONDS,
        )
        select_model.assert_not_called()

    @patch("production_runner.run_attempt")
    @patch("production_runner.load_provider_secrets")
    def test_run_failure_is_redacted_and_has_one_sentinel_line(
        self, load_secrets, run_attempt
    ) -> None:
        load_secrets.return_value = dict(PROVIDERS)
        run_attempt.side_effect = RuntimeError(
            "request failed bearer openrouter-secret token=deepline-secret"
        )
        output = StringIO()

        with patch("sys.stdin", StringIO('{"icp_id":"icp_today"}')):
            with redirect_stdout(output):
                returncode = production_runner.main(
                    [
                        "run",
                        "--model",
                        "openai/test-model",
                        "--model-pricing-json",
                        "{}",
                    ]
                )

        payload = _payload(output)
        self.assertEqual(returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertNotIn("openrouter-secret", output.getvalue())
        self.assertNotIn("deepline-secret", output.getvalue())
        self.assertIn("[redacted]", payload["error"])


if __name__ == "__main__":
    unittest.main()
