"""Configuration resolution and validation."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prmpt.config import (  # noqa: E402
    DEFAULT_CONFIG,
    config_path,
    describe_config,
    load_config,
    resolve_config,
    write_default_config,
)
from prmpt.errors import ConfigError  # noqa: E402

ARGS = (
    "max_model_tier",
    "provider",
    "model",
    "base_url",
    "temperature",
    "max_tokens",
    "timeout",
    "retries",
    "max_input_bytes",
    "stream",
)


class Args:
    def __init__(self, **kwargs: object) -> None:
        for name in ARGS:
            setattr(self, name, kwargs.get(name))


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config_file = self.tmp / "config.json"
        self.env = mock.patch.dict(os.environ, {"PRMPT_CONFIG_PATH": str(self.config_file)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        which = mock.patch("prmpt.config.shutil.which", return_value=None)
        self.which = which.start()
        self.addCleanup(which.stop)

    def write_config(self, **values: object) -> Path:
        payload = dict(DEFAULT_CONFIG)
        payload.update(values)
        self.config_file.write_text(json.dumps(payload), encoding="utf-8")
        return self.config_file


class PathTests(ConfigTestCase):
    def test_default_path_is_under_home_config(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config_path(), Path.home() / ".config" / "prmpt" / "config.json")

    def test_env_override(self) -> None:
        self.assertEqual(config_path(), self.config_file)

    def test_init_writes_template_with_owner_only_permissions(self) -> None:
        path = write_default_config()
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text()), DEFAULT_CONFIG)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_init_refuses_to_overwrite_without_force(self) -> None:
        write_default_config()
        with self.assertRaises(ConfigError):
            write_default_config()
        write_default_config(force=True)

    def test_missing_config_uses_defaults(self) -> None:
        self.assertEqual(load_config(), DEFAULT_CONFIG)

    def test_unknown_config_key_is_rejected(self) -> None:
        self.write_config(providr="openai")
        with self.assertRaises(ConfigError) as ctx:
            load_config()
        self.assertIn("providr", str(ctx.exception))

    def test_invalid_json_is_rejected(self) -> None:
        self.config_file.write_text("{nope", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config()


class ProviderTests(ConfigTestCase):
    def test_auto_falls_back_to_none(self) -> None:
        config = resolve_config(Args(), dry_run=True)
        self.assertEqual(config.provider, "none")

    def test_auto_detects_claude_cli(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        config = resolve_config(Args(), dry_run=True)
        self.assertEqual(config.provider, "claude-cli")
        self.assertEqual(config.key_source, "claude CLI session")

    def test_auto_prefers_explicit_custom_credentials(self) -> None:
        with mock.patch.dict(os.environ, {"PRMPT_API_KEY": "k", "PRMPT_BASE_URL": "https://x.test/v1"}):
            self.which.return_value = "/usr/local/bin/claude"
            config = resolve_config(Args(), dry_run=True)
        self.assertEqual(config.provider, "custom")
        self.assertEqual(config.api_key, "k")

    def test_auto_detects_provider_keys_in_priority_order(self) -> None:
        with mock.patch.dict(
            os.environ, {"GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o", "OPENAI_API_KEY": "p"}
        ):
            self.assertEqual(resolve_config(Args(), dry_run=True).provider, "openai")

    def test_aliases(self) -> None:
        self.assertEqual(resolve_config(Args(provider="claude"), dry_run=True).provider, "claude-cli")
        custom = resolve_config(Args(provider="openai-compatible"), dry_run=True)
        self.assertEqual(custom.provider, "custom")

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config(Args(provider="hal9000"), dry_run=True)

    def test_none_provider_is_valid_for_dry_runs(self) -> None:
        self.assertEqual(resolve_config(Args(provider="none"), dry_run=True).provider, "none")

    def test_none_provider_fails_a_real_run(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            resolve_config(Args(provider="none"), dry_run=False)
        self.assertIn("No model provider", str(ctx.exception))

    def test_missing_api_key_fails_a_real_run(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            resolve_config(Args(provider="openai"), dry_run=False)
        self.assertIn("requires an API key", str(ctx.exception))

    def test_custom_requires_base_url(self) -> None:
        with mock.patch.dict(os.environ, {"PRMPT_API_KEY": "k"}), self.assertRaises(
            ConfigError
        ) as ctx:
            resolve_config(Args(provider="custom"), dry_run=False)
        self.assertIn("base URL", str(ctx.exception))

    def test_invalid_base_url_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config(Args(provider="custom", base_url="api.example.com"), dry_run=True)

    def test_base_url_trailing_slash_is_removed(self) -> None:
        config = resolve_config(Args(provider="custom", base_url="https://x.test/v1/"), dry_run=True)
        self.assertEqual(config.base_url, "https://x.test/v1")


class PrecedenceTests(ConfigTestCase):
    def test_flags_beat_environment_beat_config(self) -> None:
        self.write_config(temperature=0.9, timeout=99)
        with mock.patch.dict(os.environ, {"PRMPT_TEMPERATURE": "0.1"}):
            config = resolve_config(Args(), dry_run=True)
            self.assertEqual(config.temperature, 0.1)
            self.assertEqual(config.timeout, 99)
            explicit = resolve_config(Args(temperature=0.5), dry_run=True)
            self.assertEqual(explicit.temperature, 0.5)

    def test_config_model_only_applies_to_matching_provider(self) -> None:
        self.write_config(provider="openai", model="gpt-4o")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            # The openai model in config must not leak to an explicitly chosen groq.
            config = resolve_config(Args(provider="groq"), dry_run=True)
        self.assertEqual(config.provider, "groq")
        self.assertEqual(config.model, "llama-3.3-70b-versatile")

    def test_config_model_applies_when_provider_matches(self) -> None:
        self.write_config(provider="openai", model="gpt-4o")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            config = resolve_config(Args(), dry_run=True)
        self.assertEqual(config.provider, "openai")
        self.assertEqual(config.model, "gpt-4o")

    def test_stream_can_be_disabled_from_config(self) -> None:
        self.write_config(stream=False)
        self.assertFalse(resolve_config(Args(), dry_run=True).stream)

    def test_invalid_stream_value_is_rejected(self) -> None:
        self.write_config(stream="maybe")
        with self.assertRaises(ConfigError):
            resolve_config(Args(), dry_run=True)


class ModelTierTests(ConfigTestCase):
    def test_default_ceiling_is_sonnet(self) -> None:
        self.assertEqual(resolve_config(Args(), dry_run=True).max_model_tier, "sonnet")

    def test_claude_cli_is_pinned_to_the_ceiling(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        config = resolve_config(Args(), dry_run=True)
        self.assertEqual(config.provider, "claude-cli")
        self.assertEqual(config.model, "sonnet")

    def test_haiku_ceiling_pins_haiku(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        config = resolve_config(Args(max_model_tier="haiku"), dry_run=True)
        self.assertEqual(config.model, "haiku")

    def test_opus_is_rejected_under_the_default_ceiling(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        with self.assertRaises(ConfigError) as ctx:
            resolve_config(Args(model="opus"), dry_run=False)
        self.assertIn("above the max_model_tier ceiling", str(ctx.exception))

    def test_sonnet_is_rejected_under_a_haiku_ceiling(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        with self.assertRaises(ConfigError):
            resolve_config(Args(model="sonnet", max_model_tier="haiku"), dry_run=False)

    def test_haiku_is_allowed(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        config = resolve_config(Args(model="haiku"), dry_run=False)
        self.assertEqual(config.model, "haiku")

    def test_ceiling_any_allows_opus(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        config = resolve_config(Args(model="opus", max_model_tier="any"), dry_run=False)
        self.assertEqual(config.model, "opus")

    def test_unknown_tiers_are_not_blocked(self) -> None:
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k"}):
            config = resolve_config(Args(provider="deepseek"), dry_run=False)
        self.assertEqual(config.model, "deepseek-chat")

    def test_http_model_tiers_are_checked_by_heuristic(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            with self.assertRaises(ConfigError):
                resolve_config(Args(provider="openai", model="gpt-4.5"), dry_run=False)
            config = resolve_config(Args(provider="openai", model="gpt-4o-mini"), dry_run=False)
        self.assertEqual(config.model, "gpt-4o-mini")

    def test_invalid_ceiling_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config(Args(max_model_tier="platinum"), dry_run=True)

    def test_ceiling_can_come_from_config(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        self.write_config(max_model_tier="haiku")
        self.assertEqual(resolve_config(Args(), dry_run=True).model, "haiku")


class ValidationTests(ConfigTestCase):
    def test_temperature_range(self) -> None:
        for value in (-0.1, 2.1):
            with self.assertRaises(ConfigError):
                resolve_config(Args(temperature=value), dry_run=True)

    def test_retries_range(self) -> None:
        for value in (-1, 11):
            with self.assertRaises(ConfigError):
                resolve_config(Args(retries=value), dry_run=True)

    def test_non_numeric_values(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_config(Args(timeout="soon"), dry_run=True)

    def test_limits_defaults(self) -> None:
        config = resolve_config(Args(), dry_run=True)
        self.assertEqual(config.retries, 2)
        self.assertEqual(config.max_input_bytes, 1 << 20)
        self.assertEqual(config.max_model_tier, "sonnet")


class DescribeTests(ConfigTestCase):
    def test_secrets_are_never_printed(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-super-secret-value"}):
            config = resolve_config(Args(provider="openai"), dry_run=True)
        rendered = describe_config(config)
        self.assertIn("OPENAI_API_KEY", rendered)
        self.assertNotIn("sk-super-secret-value", rendered)

    def test_cli_provider_notes(self) -> None:
        self.which.return_value = "/usr/local/bin/claude"
        rendered = describe_config(resolve_config(Args(), dry_run=True))
        self.assertIn("(not applied by claude-cli)", rendered)
        self.assertIn("(claude-cli always streams)", rendered)
        self.assertIn("max_model_tier:", rendered)
        self.assertIn("model:           sonnet", rendered)


if __name__ == "__main__":
    unittest.main()
