"""Provider clients: HTTP streaming/retry and the Claude CLI adapter."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prmpt.client import _stream_http, stream_response  # noqa: E402
from prmpt.config import Config  # noqa: E402
from prmpt.errors import ProviderError  # noqa: E402

FAKE_CLAUDE_DIR = ROOT / "tests" / "fakes"


def make_config(**overrides: object) -> Config:
    values: dict[str, object] = {
        "provider": "custom",
        "model": "test-model",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": "test-key",
        "key_source": "test",
        "temperature": 0.3,
        "max_tokens": 100,
        "timeout": 5.0,
        "stream": True,
        "retries": 0,
        "max_input_bytes": 1 << 20,
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


class _Provider(BaseHTTPRequestHandler):
    """Programmable OpenAI-compatible provider; one plan step per request."""

    plan: list[dict] = []
    calls: int = 0

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        cls = type(self)
        step = cls.plan[min(cls.calls, len(cls.plan) - 1)]
        cls.calls += 1
        kind = step["kind"]

        if kind == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for part in step["parts"]:
                payload = {"choices": [{"delta": {"content": part}}]}
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        if kind == "json":
            body = json.dumps(step["body"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps(step.get("body", {"error": {"message": "failed"}})).encode()
        self.send_response(step["code"])
        for name, value in step.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _Provider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _Provider.calls = 0
        _Provider.plan = []
        self.notes: list[str] = []
        backoff = mock.patch("prmpt.client._backoff_delay", return_value=0.0)
        backoff.start()
        self.addCleanup(backoff.stop)

    def run_http(self, **overrides: object) -> list[str]:
        config = make_config(base_url=self.base_url, **overrides)
        return list(_stream_http(config, "system", "user", notify=self.notes.append))

    def test_streaming_yields_chunks_as_they_arrive(self) -> None:
        _Provider.plan = [{"kind": "sse", "parts": ["hello", " world"]}]
        self.assertEqual(self.run_http(), ["hello", " world"])
        self.assertEqual(_Provider.calls, 1)

    def test_non_streaming_returns_single_chunk(self) -> None:
        _Provider.plan = [{"kind": "json", "body": {"choices": [{"message": {"content": "hi"}}]}}]
        self.assertEqual(self.run_http(stream=False), ["hi"])

    def test_retries_rate_limit_and_honours_retry_after(self) -> None:
        _Provider.plan = [
            {"kind": "status", "code": 429, "headers": {"Retry-After": "0"}},
            {"kind": "sse", "parts": ["ok"]},
        ]
        self.assertEqual(self.run_http(retries=2), ["ok"])
        self.assertEqual(_Provider.calls, 2)
        self.assertTrue(any("Retrying" in note for note in self.notes))

    def test_retries_server_errors(self) -> None:
        _Provider.plan = [
            {"kind": "status", "code": 503},
            {"kind": "sse", "parts": ["ok"]},
        ]
        self.assertEqual(self.run_http(retries=1), ["ok"])
        self.assertEqual(_Provider.calls, 2)

    def test_does_not_retry_client_errors(self) -> None:
        _Provider.plan = [{"kind": "status", "code": 401}]
        with self.assertRaises(ProviderError) as ctx:
            self.run_http(retries=3)
        self.assertEqual(_Provider.calls, 1)
        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(ctx.exception.retryable)

    def test_gives_up_after_configured_attempts(self) -> None:
        _Provider.plan = [{"kind": "status", "code": 502}]
        with self.assertRaises(ProviderError) as ctx:
            self.run_http(retries=1)
        self.assertEqual(_Provider.calls, 2)
        self.assertIn("HTTP 502", str(ctx.exception))

    def test_error_message_is_extracted_from_body(self) -> None:
        _Provider.plan = [
            {"kind": "status", "code": 401, "body": {"error": {"message": "invalid api key"}}}
        ]
        with self.assertRaises(ProviderError) as ctx:
            self.run_http()
        self.assertIn("invalid api key", str(ctx.exception))

    def test_empty_stream_is_an_error(self) -> None:
        _Provider.plan = [{"kind": "sse", "parts": []}]
        with self.assertRaises(ProviderError) as ctx:
            self.run_http()
        self.assertIn("empty stream", str(ctx.exception))

    def test_invalid_json_in_non_streaming_mode_is_an_error(self) -> None:
        _Provider.plan = [{"kind": "json", "body": {"unexpected": True}}]
        with self.assertRaises(ProviderError) as ctx:
            self.run_http(stream=False)
        self.assertIn("no choices", str(ctx.exception))

    def test_unreachable_provider_is_a_provider_error(self) -> None:
        config = make_config(base_url="http://127.0.0.1:1/v1", retries=0)
        with self.assertRaises(ProviderError) as ctx:
            list(_stream_http(config, "system", "user", notify=self.notes.append))
        self.assertIn("Could not reach the provider", str(ctx.exception))

    def test_partial_output_is_never_replayed(self) -> None:
        # A mid-stream failure must not replay content the user already saw.
        calls = {"count": 0}

        def fake_once(config: Config, system_prompt: str, user_message: str) -> object:
            calls["count"] += 1
            yield "partial"
            raise ProviderError("connection dropped", retryable=True)

        with mock.patch("prmpt.client._http_once", fake_once):
            config = make_config(base_url=self.base_url, retries=3)
            with self.assertRaises(ProviderError):
                list(_stream_http(config, "system", "user", notify=self.notes.append))
        self.assertEqual(calls["count"], 1)
        self.assertEqual(self.notes, [])


class ClaudeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = str(Path(self.tmp.name) / "state")
        path = f"{FAKE_CLAUDE_DIR}:{os.environ.get('PATH', '')}"
        patcher = mock.patch.dict(os.environ, {"PATH": path})
        patcher.start()
        self.addCleanup(patcher.stop)
        backoff = mock.patch("prmpt.client._backoff_delay", return_value=0.0)
        backoff.start()
        self.addCleanup(backoff.stop)
        self.notes: list[str] = []

    def claude(self, mode: str, **overrides: object) -> list[str]:
        env = {"FAKE_CLAUDE_MODE": mode, "FAKE_CLAUDE_STATE": self.state}
        with mock.patch.dict(os.environ, env):
            config = make_config(provider="claude-cli", base_url=None, api_key=None, **overrides)
            return list(
                stream_response(config, "system", "user", notify=self.notes.append)
            )

    def test_streams_partial_messages(self) -> None:
        self.assertEqual(self.claude("stream"), ["Hel", "lo", " from", " fake"])

    def test_falls_back_when_stream_flags_are_rejected(self) -> None:
        self.assertEqual(self.claude("reject_stream_flags"), ["PLAIN TEXT OK\n"])

    def test_timeout_kills_the_child(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ProviderError) as ctx:
            self.claude("slow", timeout=0.5, retries=1)
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(time.monotonic() - started, 5)

    def test_permanent_failure_is_not_retried(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            self.claude("fail_permanent", retries=3)
        self.assertIn("Invalid API key", str(ctx.exception))
        self.assertEqual(self.notes, [])

    def test_transient_failure_is_retried_then_reported(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            self.claude("fail_transient", retries=1)
        self.assertIn("rate limit", str(ctx.exception))
        self.assertEqual(len(self.notes), 1)

    def test_transient_failure_recovers(self) -> None:
        self.assertEqual(self.claude("flaky", retries=1), ["Hel", "lo", " from", " fake"])
        self.assertEqual(len(self.notes), 1)

    def test_empty_output_is_an_error(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            self.claude("empty", retries=0)
        self.assertIn("empty response", str(ctx.exception))

    def test_error_result_event_is_surfaced(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            self.claude("error_result", retries=0)
        self.assertIn("boom", str(ctx.exception))

    def test_missing_cli_is_reported_clearly(self) -> None:
        with mock.patch("prmpt.client.shutil.which", return_value=None):
            config = make_config(provider="claude-cli")
            with self.assertRaises(ProviderError) as ctx:
                list(stream_response(config, "system", "user"))
        self.assertIn("not found on PATH", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
