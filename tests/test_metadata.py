"""Request metadata capture."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prmpt import __version__  # noqa: E402
from prmpt.errors import ConfigError  # noqa: E402
from prmpt.metadata import build_metadata, parse_meta_pairs, set_nested  # noqa: E402


def build(**kwargs: object) -> dict:
    params = {
        "meta_pairs": None,
        "metadata_file": None,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "temperature": 0.3,
        "utterance": "make this clearer",
    }
    params.update(kwargs)
    return build_metadata(**params)  # type: ignore[arg-type]


class MetaPairTests(unittest.TestCase):
    def test_dotted_keys_nest(self) -> None:
        data = parse_meta_pairs(["user.role=CFO", "stakes=high", "score=0.4"])
        self.assertEqual(data["user"]["role"], "CFO")
        self.assertEqual(data["stakes"], "high")
        self.assertEqual(data["score"], 0.4)

    def test_scalars_are_parsed_as_json_where_possible(self) -> None:
        data = parse_meta_pairs(["a=true", "b=null", "c=plain text", "d=[1,2]"])
        self.assertIs(data["a"], True)
        self.assertIsNone(data["b"])
        self.assertEqual(data["c"], "plain text")
        self.assertEqual(data["d"], [1, 2])

    def test_set_nested_does_not_overwrite_scalar(self) -> None:
        data = {"user": "someone"}
        set_nested(data, "user.role", "CFO")
        self.assertEqual(data["user"]["role"], "CFO")

    def test_invalid_pairs_are_rejected(self) -> None:
        for pair in ("novalue", "=value", "  =value"):
            with self.assertRaises(ConfigError):
                parse_meta_pairs([pair])


class BuildTests(unittest.TestCase):
    def test_defaults(self) -> None:
        data = build()
        self.assertEqual(data["schema"], "prs1-metadata/1")
        self.assertEqual(data["prmpt_version"], __version__)
        self.assertEqual(data["channel"], "terminal")
        self.assertEqual(data["stakes"], "unknown")
        self.assertEqual(data["model"]["provider"], "openai")
        self.assertEqual(data["model"]["name"], "gpt-4o-mini")
        self.assertEqual(data["privacy"]["redaction_log"], "none")
        self.assertTrue(data["request_id"].startswith("prmpt-"))

    def test_input_fingerprint_is_recorded(self) -> None:
        data = build(utterance="hello")
        self.assertEqual(data["input"]["bytes"], 5)
        self.assertEqual(data["input"]["characters"], 5)
        self.assertEqual(len(data["input"]["sha256_prefix"]), 12)

    def test_input_block_when_utterance_is_absent(self) -> None:
        data = build(utterance=None)
        self.assertEqual(data["input"]["size"], "unknown")

    def test_claude_cli_temperature_note(self) -> None:
        data = build(provider="claude-cli", model=None)
        self.assertEqual(data["model"]["name"], "(provider default)")
        self.assertIn("provider default", data["model"]["temperature"])

    def test_metadata_file_merges_and_pairs_win(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            path.write_text(json.dumps({"audience": ["board"], "user": {"role": "VP"}}))
            data = build(metadata_file=str(path), meta_pairs=["stakes=high", "user.role=CFO"])
        self.assertEqual(data["stakes"], "high")
        self.assertEqual(data["audience"], ["board"])
        self.assertEqual(data["user"]["role"], "CFO")


class MetadataFileTests(unittest.TestCase):
    def test_missing_file_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            build(metadata_file="/nonexistent/prmpt-meta.json")

    def test_non_object_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            path.write_text("[1, 2]")
            with self.assertRaises(ConfigError):
                build(metadata_file=str(path))

    def test_invalid_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            path.write_text("{oops")
            with self.assertRaises(ConfigError):
                build(metadata_file=str(path))

    def test_yaml_without_pyyaml_is_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.yaml"
            path.write_text("stakes: high\n")
            try:
                build(metadata_file=str(path))
            except ConfigError as exc:
                self.assertIn("PyYAML", str(exc))
            else:
                self.skipTest("PyYAML is installed; YAML parsed instead")


if __name__ == "__main__":
    unittest.main()
