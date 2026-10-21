"""Configuration loading and provider resolution."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

CONFIG_PATH = Path.home() / ".config" / "prmpt" / "config.json"

PROVIDERS = ("auto", "claude", "claude-cli", "openrouter", "openai", "deepseek", "groq", "custom")

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "auto",
    "model": None,
    "base_url": None,
    "temperature": 0.3,
    "max_tokens": 4096,
    "timeout": 300,
}

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat",
        "key_env": "OPENROUTER_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_env": "GROQ_API_KEY",
    },
    "custom": {
        "base_url": None,
        "model": None,
        "key_env": "PRMPT_API_KEY",
    },
    "claude-cli": {
        "base_url": None,
        "model": None,
        "key_env": None,
    },
    "none": {
        "base_url": None,
        "model": None,
        "key_env": None,
    },
}


@dataclass
class Config:
    provider: str
    model: str | None
    base_url: str | None
    api_key: str | None
    key_source: str
    temperature: float
    max_tokens: int | None
    timeout: float
    stream: bool


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def load_config() -> dict[str, Any]:
    """Load the JSON config, returning defaults when it is absent or invalid."""
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read {CONFIG_PATH}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_PATH} must contain a JSON object.")
    merged = dict(DEFAULT_CONFIG)
    merged.update(raw)
    return merged


def write_default_config(force: bool = False) -> Path:
    """Write a config template. Refuse to overwrite unless forced."""
    if CONFIG_PATH.exists() and not force:
        raise ConfigError(
            f"{CONFIG_PATH} already exists. Use --force to overwrite it."
        )
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    return CONFIG_PATH


def _normalise_provider(provider: str) -> str:
    provider = provider.strip().lower()
    if provider == "claude":
        return "claude-cli"
    if provider in {"openai-compatible", "openai_compatible"}:
        return "custom"
    if provider not in PROVIDERS:
        raise ConfigError(
            f"Unknown provider '{provider}'. Choose one of: {', '.join(PROVIDERS)}."
        )
    return provider


def _as_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}.") from exc


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}.") from exc


def resolve_config(args: Any, dry_run: bool = False) -> Config:
    """Resolve CLI flags, environment variables, config, and provider defaults."""
    config = load_config()

    raw_provider = _first(
        getattr(args, "provider", None),
        _env("PRMPT_PROVIDER"),
        config.get("provider"),
        "auto",
    )
    provider = _normalise_provider(str(raw_provider))

    if provider == "auto":
        if _env("PRMPT_API_KEY") and (
            getattr(args, "base_url", None)
            or _env("PRMPT_BASE_URL")
            or config.get("base_url")
        ):
            provider = "custom"
        elif shutil.which("claude"):
            provider = "claude-cli"
        elif _env("OPENAI_API_KEY"):
            provider = "openai"
        elif _env("DEEPSEEK_API_KEY"):
            provider = "deepseek"
        elif _env("OPENROUTER_API_KEY"):
            provider = "openrouter"
        elif _env("GROQ_API_KEY"):
            provider = "groq"
        else:
            provider = "none"

    defaults = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["none"])
    key_env = defaults.get("key_env")

    config_provider = _normalise_provider(str(config.get("provider") or "auto"))

    model = _first(
        getattr(args, "model", None),
        _env("PRMPT_MODEL"),
    )
    if model is None and (config_provider == provider or provider == "custom"):
        model = config.get("model")
    if model is None:
        model = defaults.get("model")

    base_url = _first(
        getattr(args, "base_url", None),
        _env("PRMPT_BASE_URL"),
    )
    if base_url is None and (config_provider == provider or provider == "custom"):
        base_url = config.get("base_url")
    if base_url is None:
        base_url = defaults.get("base_url")

    api_key = None
    key_source = "not needed"
    if provider == "claude-cli":
        key_source = "claude CLI session"
    elif provider != "none":
        if key_env:
            api_key = _env(key_env) or _env("PRMPT_API_KEY")
            key_source = key_env if _env(key_env) else ("PRMPT_API_KEY" if api_key else "missing")
        else:
            api_key = _env("PRMPT_API_KEY")
            key_source = "PRMPT_API_KEY" if api_key else "missing"

    temperature = _as_float(
        _first(
            getattr(args, "temperature", None),
            _env("PRMPT_TEMPERATURE"),
            config.get("temperature"),
            0.3,
        ),
        "temperature",
    )
    max_tokens = _as_int(
        _first(
            getattr(args, "max_tokens", None),
            _env("PRMPT_MAX_TOKENS"),
            config.get("max_tokens"),
            4096,
        ),
        "max_tokens",
    )
    timeout = _as_float(
        _first(
            getattr(args, "timeout", None),
            _env("PRMPT_TIMEOUT"),
            config.get("timeout"),
            300,
        ),
        "timeout",
    )

    if not 0 <= temperature <= 2:
        raise ConfigError("temperature must be between 0 and 2.")
    if max_tokens <= 0:
        raise ConfigError("max_tokens must be greater than zero.")
    if timeout <= 0:
        raise ConfigError("timeout must be greater than zero.")

    stream_arg = getattr(args, "stream", None)
    if stream_arg is None:
        stream = bool(config.get("stream", True))
    else:
        stream = bool(stream_arg)

    if not dry_run:
        if provider == "none":
            raise ConfigError(
                "No model provider is available.\n"
                "Install and sign in to the Claude CLI, or set an API key:\n"
                "  OPENROUTER_API_KEY=...  prmpt \"...\" --provider openrouter\n"
                "  OPENAI_API_KEY=...      prmpt \"...\" --provider openai\n"
                "  DEEPSEEK_API_KEY=...    prmpt \"...\" --provider deepseek\n"
                "You can also run with --print-prompt to see the prompt without calling a model."
            )
        if provider == "claude-cli" and not shutil.which("claude"):
            raise ConfigError(
                "Provider 'claude-cli' was selected but the `claude` command was not found on PATH."
            )
        if provider != "claude-cli" and not api_key:
            env_hint = f" Set {key_env}." if key_env else ""
            raise ConfigError(
                f"Provider '{provider}' requires an API key.{env_hint}"
            )
        if provider == "custom" and not base_url:
            raise ConfigError(
                "Provider 'custom' requires a base URL. Set PRMPT_BASE_URL or pass --base-url."
            )
        if provider not in {"claude-cli", "none"} and not model:
            raise ConfigError(
                f"Provider '{provider}' requires a model. Pass --model or set PRMPT_MODEL."
            )

    return Config(
        provider=provider,
        model=str(model) if model else None,
        base_url=str(base_url) if base_url else None,
        api_key=api_key,
        key_source=key_source,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        stream=stream,
    )


def describe_config(config: Config) -> str:
    """Human-readable configuration with no secret values."""
    is_cli = config.provider == "claude-cli"
    cli_note = " (not applied by claude-cli)" if is_cli else ""
    stream_note = " (claude-cli always streams)" if is_cli else ""
    lines = [
        f"provider:    {config.provider}",
        f"model:       {config.model or '(provider default)'}",
        f"base_url:    {config.base_url or '(not applicable)'}",
        f"api_key:     {config.key_source}"
        + (" (set)" if config.api_key and config.key_source not in {"claude CLI session", "not needed"} else ""),
        f"temperature: {config.temperature}{cli_note}",
        f"max_tokens:  {config.max_tokens if config.max_tokens is not None else '(provider default)'}{cli_note}",
        f"timeout:     {config.timeout:g}s",
        f"stream:      {config.stream}{stream_note}",
    ]
    return "\n".join(lines)
