"""Build the metadata block sent with the utterance."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import ConfigError


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def set_nested(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = [part for part in dotted_key.split(".") if part]
    if not parts:
        raise ConfigError("Metadata keys cannot be empty.")
    cursor = data
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def parse_scalar(raw: str) -> Any:
    """Parse a CLI metadata value as JSON when possible, otherwise keep it as text."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_meta_pairs(pairs: list[str] | None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ConfigError(
                f"Invalid --meta value {pair!r}. Use key=value, for example: --meta stakes=high"
            )
        key, raw = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError("Invalid --meta value with an empty key.")
        set_nested(data, key, parse_scalar(raw.strip()))
    return data


def load_metadata_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    file_path = Path(path).expanduser()
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read metadata file {file_path}: {exc}") from exc

    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ConfigError(
                "YAML metadata requires PyYAML. Use a .json file instead, or install PyYAML."
            ) from exc
        try:
            data = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - surface the parser error
            raise ConfigError(f"Could not parse {file_path}: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Could not parse {file_path} as JSON: {exc}. "
                "For YAML, use a .yaml extension and install PyYAML."
            ) from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{file_path} must contain an object at the top level.")
    return data


def _local_timezone_name(now: datetime) -> str:
    tzinfo = now.tzinfo
    if tzinfo is None:
        return "local"
    name = tzinfo.tzname(now)
    if name:
        return name
    return str(tzinfo)


def _locale() -> str:
    raw = os.environ.get("LC_ALL") or os.environ.get("LANG") or "en-US"
    return raw.split(".", 1)[0].replace("_", "-") or "en-US"


def build_metadata(
    meta_pairs: list[str] | None,
    metadata_file: str | None,
    provider: str,
    model: str | None,
    temperature: float,
) -> dict[str, Any]:
    now = datetime.now().astimezone()
    request_id = f"prmpt-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"

    metadata: dict[str, Any] = {
        "request_id": request_id,
        "captured_at": now.isoformat(timespec="seconds"),
        "timezone": _local_timezone_name(now),
        "channel": "terminal",
        "locale": _locale(),
        "stakes": "unknown",
        "user": {
            "expertise_self_reported": "unknown",
        },
        "model": {
            "provider": provider,
            "name": model or "(provider default)",
            "temperature": (
                "provider default (claude-cli does not expose temperature)"
                if provider == "claude-cli"
                else temperature
            ),
        },
        "privacy": {
            "class": "unknown",
            "redaction_log": "none",
        },
        "input_handling": "utterance preserved verbatim; closing XML tags escaped only",
    }

    _deep_merge(metadata, load_metadata_file(metadata_file))
    _deep_merge(metadata, parse_meta_pairs(meta_pairs))
    return metadata
