"""End-to-end CLI behaviour, exercised through a subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FAKE_CLAUDE_DIR = ROOT / "tests" / "fakes"
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config = self.tmp / "config.json"

    def fake_claude_path(self) -> str:
        """PATH entry that resolves `claude` to the fixture on this interpreter."""
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "claude"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE_DIR / "claude.py"}" "$@"\n'
        )
        shim.chmod(0o755)
        return f"{bin_dir}:{BASE_PATH}"

    def run_cli(
        self,
        *args: str,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        full_env = {
            "PYTHONPATH": str(SRC),
            "PATH": BASE_PATH,
            "PRMPT_CONFIG_PATH": str(self.config),
            "HOME": str(self.tmp),
            "NO_COLOR": "1",
        }
        full_env.update(env or {})
        return subprocess.run(
            [sys.executable, "-m", "prmpt", *args],
            cwd=ROOT,
            env=full_env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=60,
        )


class BasicsTests(CliTestCase):
    def test_version(self) -> None:
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^prmpt \d+\.\d+\.\d+$")

    def test_help_documents_exit_codes_and_environment(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("exit codes:", result.stdout)
        self.assertIn("130", result.stdout)
        self.assertIn("PRMPT_API_KEY", result.stdout)

    def test_unknown_provider_is_a_usage_error(self) -> None:
        result = self.run_cli("hello", "--provider", "hal9000")
        self.assertEqual(result.returncode, 2)


class InputTests(CliTestCase):
    def test_positional_prompt(self) -> None:
        result = self.run_cli("make it clearer", "--print-prompt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("make it clearer", result.stdout)
        self.assertIn("=== SYSTEM ===", result.stdout)
        self.assertIn("=== USER ===", result.stdout)

    def test_stdin(self) -> None:
        result = self.run_cli("--stdin", "--print-prompt", stdin="piped utterance")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("piped utterance", result.stdout)

    def test_file(self) -> None:
        path = self.tmp / "utterance.txt"
        path.write_text("from a file", encoding="utf-8")
        result = self.run_cli("--file", str(path), "--print-prompt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("from a file", result.stdout)

    def test_conflicting_sources_are_rejected(self) -> None:
        result = self.run_cli("prompt", "--stdin", "--print-prompt", stdin="x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("one input source", result.stderr)

    def test_empty_input_is_rejected(self) -> None:
        result = self.run_cli("   ", "--print-prompt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("empty", result.stderr)

    def test_invalid_utf8_file_is_a_clear_error(self) -> None:
        path = self.tmp / "bad.txt"
        path.write_bytes(b"hello \xff world")
        result = self.run_cli("--file", str(path), "--print-prompt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not valid UTF-8", result.stderr)

    def test_oversized_input_is_rejected(self) -> None:
        result = self.run_cli("x" * 200, "--print-prompt", "--max-input-bytes", "50")
        self.assertEqual(result.returncode, 2)
        self.assertIn("byte limit", result.stderr)

    def test_input_limit_can_be_raised(self) -> None:
        result = self.run_cli("x" * 200, "--print-prompt", "--max-input-bytes", "5000")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_file_is_a_config_error(self) -> None:
        result = self.run_cli("--file", "/nonexistent/nope.txt", "--print-prompt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Could not read", result.stderr)


class OutputTests(CliTestCase):
    def test_out_writes_the_prompt(self) -> None:
        out = self.tmp / "nested" / "dossier.md"
        result = self.run_cli("hello", "--print-prompt", "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("hello", out.read_text(encoding="utf-8"))
        leftovers = [p.name for p in out.parent.iterdir() if p.name != out.name]
        self.assertEqual(leftovers, [], "atomic write left temp files behind")

    def test_out_dash_writes_to_stdout(self) -> None:
        result = self.run_cli("hello", "--print-prompt", "--out", "-")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hello", result.stdout)

    def test_quiet_suppresses_status_messages(self) -> None:
        out = self.tmp / "dossier.md"
        result = self.run_cli("hello", "--print-prompt", "--out", str(out), "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_metadata_pairs_reach_the_prompt(self) -> None:
        result = self.run_cli(
            "hello", "--print-prompt", "--meta", "stakes=high", "--meta", "user.role=CFO"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"stakes": "high"', result.stdout)
        self.assertIn('"role": "CFO"', result.stdout)

    def test_metadata_file_reaches_the_prompt(self) -> None:
        path = self.tmp / "meta.json"
        path.write_text(json.dumps({"audience": ["board"]}), encoding="utf-8")
        result = self.run_cli("hello", "--print-prompt", "--metadata", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"audience"', result.stdout)


class ConfigTests(CliTestCase):
    def test_show_config_without_credentials(self) -> None:
        result = self.run_cli("--show-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("provider:", result.stdout)
        self.assertIn("retries:", result.stdout)
        self.assertIn("max_input_bytes:", result.stdout)
        self.assertIn("max_model_tier:", result.stdout)

    def test_claude_cli_is_pinned_to_the_tier_ceiling(self) -> None:
        result = self.run_cli("--show-config", "--provider", "claude-cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("model:           sonnet", result.stdout)

    def test_show_config_never_prints_the_key(self) -> None:
        secret = "sk-do-not-print-this"
        result = self.run_cli(
            "--show-config", env={"OPENAI_API_KEY": secret, "PRMPT_PROVIDER": "openai"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPENAI_API_KEY", result.stdout)
        self.assertNotIn(secret, result.stdout)

    def test_init_writes_a_template_and_refuses_to_clobber(self) -> None:
        first = self.run_cli("--init")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(self.config.exists())
        self.assertEqual(json.loads(self.config.read_text())["provider"], "auto")

        second = self.run_cli("--init")
        self.assertEqual(second.returncode, 2)
        self.assertIn("already exists", second.stderr)

        forced = self.run_cli("--init", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_unknown_config_key_is_reported(self) -> None:
        self.config.write_text(json.dumps({"providr": "openai"}), encoding="utf-8")
        result = self.run_cli("hello", "--print-prompt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("providr", result.stderr)

    def test_no_provider_is_a_config_error(self) -> None:
        result = self.run_cli("hello")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No model provider", result.stderr)

    def test_provider_failure_exits_one(self) -> None:
        prompt = self.tmp / "system.md"
        prompt.write_text("Reply with ok", encoding="utf-8")
        result = self.run_cli(
            "hello",
            "--provider",
            "claude-cli",
            "--system-prompt",
            str(prompt),
            "--retries",
            "0",
            env={"PATH": self.fake_claude_path(), "FAKE_CLAUDE_MODE": "fail_permanent"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Invalid API key", result.stderr)

    def test_opus_is_blocked_by_the_tier_ceiling(self) -> None:
        prompt = self.tmp / "system.md"
        prompt.write_text("Reply with ok", encoding="utf-8")
        result = self.run_cli(
            "hello",
            "--provider",
            "claude-cli",
            "--model",
            "opus",
            "--system-prompt",
            str(prompt),
            env={"PATH": self.fake_claude_path()},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("above the max_model_tier ceiling", result.stderr)

    def test_ceiling_can_be_raised_explicitly(self) -> None:
        result = self.run_cli(
            "--show-config", "--provider", "claude-cli", "--max-model-tier", "any", "--model", "opus"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("model:           opus", result.stdout)

    def test_usage_and_cost_are_reported(self) -> None:
        prompt = self.tmp / "system.md"
        prompt.write_text("Reply with ok", encoding="utf-8")
        result = self.run_cli(
            "hello",
            "--provider",
            "claude-cli",
            "--system-prompt",
            str(prompt),
            "--retries",
            "0",
            env={"PATH": self.fake_claude_path(), "FAKE_CLAUDE_MODE": "stream"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tokens: in 11", result.stderr)
        self.assertIn("cached 100", result.stderr)
        self.assertIn("out 7", result.stderr)
        self.assertIn("$0.0012", result.stderr)

    def test_debug_prints_a_traceback(self) -> None:
        result = self.run_cli("hello", "--provider", "claude-cli", "--debug")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Traceback", result.stderr)

    def test_provider_streams_a_dossier(self) -> None:
        prompt = self.tmp / "system.md"
        prompt.write_text("Reply with ok", encoding="utf-8")
        result = self.run_cli(
            "hello",
            "--provider",
            "claude-cli",
            "--system-prompt",
            str(prompt),
            "--retries",
            "0",
            env={"PATH": self.fake_claude_path(), "FAKE_CLAUDE_MODE": "stream"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Hello from fake\n")
        self.assertIn("prmpt: claude-cli", result.stderr)


if __name__ == "__main__":
    unittest.main()
