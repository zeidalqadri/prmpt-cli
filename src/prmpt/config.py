"""Configuration loading, validation, and provider resolution."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigError

PROVIDERS = ("auto", "claude", "claude-cli", "openrouter", "openai", "deepseek", "groq", "custom")
RESOLVED_PROVIDERS = (*PROVIDERS, "none")

# Model tiers, cheapest first. `max_model_tier` caps what prmpt will spend on.
TIER_ORDER = {"haiku": 0, "sonnet": 1, "opus": 2}
TIER_CHOICES = (*TIER_ORDER, "any")

_CHEAP_HINTS = ("haiku", "mini", "nano", "flash", "lite", "small", "8b", "7b")
_HIGH_HINTS = ("opus", "o1-pro", "o3-pro", "gpt-4.5", "large")


def model_tier(model: str | None) -> str | None:
    """Best-effort tier for a model id. Returns None when it cannot be told."""
    if not model:
        return None
    name = str(model).lower()
    for tier in ("haiku", "sonnet", "opus"):
        if tier in name:
            return tier
    if any(hint in name for hint in _CHEAP_HINTS):
        return "haiku"
    if any(hint in name for hint in _HIGH_HINTS):
        return "opus"
    return None

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "auto",
    "model": None,
    "base_url": None,
    "temperature": 0.3,
    "max_tokens": 4096,
    "timeout": 300,
    "retries": 2,
    "stream": True,
    "max_input_bytes": 1 << 20,
    "max_model_tier": "sonnet",
}

CONFIG_KEYS = frozenset(DEFAULT_CONFIG)

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

MAX_RETRIES = 10


@dataclass(frozen=True)
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
    retries: int = 2
    max_input_bytes: int = 1 << 20
    max_model_tier: str = "sonnet"


def config_path() -> Path:
    """Location of the JSON config file (overridable for tests and sandboxes)."""
    override = os.environ.get("PRMPT_CONFIG_PATH")
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / ".config" / "prmpt" / "config.json"


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def load_config() -> dict[str, Any]:
    """Load the JSON config, rejecting unknown keys and unusable shapes."""
    path = config_path()
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Could not parse {path} as JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object.")

    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        raise ConfigError(
            f"Unknown config key(s) in {path}: {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(CONFIG_KEYS))}."
        )

    merged = dict(DEFAULT_CONFIG)
    merged.update(raw)
    return merged


def write_default_config(force: bool = False) -> Path:
    """Write a config template with owner-only permissions."""
    path = config_path()
    if path.exists() and not force:
        raise ConfigError(f"{path} already exists. Use --force to overwrite it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def _normalise_provider(provider: str) -> str:
    provider = provider.strip().lower()
    if provider == "claude":
        return "claude-cli"
    if provider in {"openai-compatible", "openai_compatible"}:
        return "custom"
    if provider not in RESOLVED_PROVIDERS:
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


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{name} must be true or false, got {value!r}.")


def _validated_base_url(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(
            f"base_url must be an http(s) URL with a host, got {text!r}."
        )
    return text.rstrip("/")


def _detect_provider(args: Any, config: dict[str, Any]) -> str:
    """Pick a provider for `auto` from the environment, then config."""
    if _env("PRMPT_API_KEY") and (
        getattr(args, "base_url", None) or _env("PRMPT_BASE_URL") or config.get("base_url")
    ):
        return "custom"
    if shutil.which("claude"):
        return "claude-cli"
    for env_name, provider in (
        ("OPENAI_API_KEY", "openai"),
        ("DEEPSEEK_API_KEY", "deepseek"),
        ("OPENROUTER_API_KEY", "openrouter"),
        ("GROQ_API_KEY", "groq"),
    ):
        if _env(env_name):
            return provider
    return "none"


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
        provider = _detect_provider(args, config)

    defaults = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["none"])
    key_env = defaults.get("key_env")
    config_provider = _normalise_provider(str(config.get("provider") or "auto"))
    trust_config = config_provider == provider or provider == "custom"

    model = _first(getattr(args, "model", None), _env("PRMPT_MODEL"))
    if model is None and trust_config:
        model = config.get("model")
    if model is None:
        model = defaults.get("model")

    max_model_tier = str(
        _first(
            getattr(args, "max_model_tier", None),
            _env("PRMPT_MAX_MODEL_TIER"),
            config.get("max_model_tier"),
            "sonnet",
        )
    ).strip().lower()
    if max_model_tier not in TIER_CHOICES:
        raise ConfigError(
            f"max_model_tier must be one of {', '.join(TIER_CHOICES)}, "
            f"got {max_model_tier!r}."
        )
    if provider == "claude-cli" and model is None and max_model_tier in TIER_ORDER:
        # Pin the CLI explicitly: with no --model it would use its own default,
        # which prmpt cannot see or cap.
        model = max_model_tier

    base_url = _validated_base_url(
        _first(
            getattr(args, "base_url", None),
            _env("PRMPT_BASE_URL"),
            config.get("base_url") if trust_config else None,
            defaults.get("base_url"),
        )
    )

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
    retries = _as_int(
        _first(
            getattr(args, "retries", None),
            _env("PRMPT_RETRIES"),
            config.get("retries"),
            2,
        ),
        "retries",
    )
    max_input_bytes = _as_int(
        _first(
            getattr(args, "max_input_bytes", None),
            _env("PRMPT_MAX_INPUT_BYTES"),
            config.get("max_input_bytes"),
            1 << 20,
        ),
        "max_input_bytes",
    )

    stream_arg = getattr(args, "stream", None)
    if stream_arg is None:
        stream = _as_bool(config.get("stream", True), "stream")
    else:
        stream = bool(stream_arg)

    if not 0 <= temperature <= 2:
        raise ConfigError("temperature must be between 0 and 2.")
    if max_tokens <= 0:
        raise ConfigError("max_tokens must be greater than zero.")
    if timeout <= 0:
        raise ConfigError("timeout must be greater than zero.")
    if not 0 <= retries <= MAX_RETRIES:
        raise ConfigError(f"retries must be between 0 and {MAX_RETRIES}.")
    if max_input_bytes <= 0:
        raise ConfigError("max_input_bytes must be greater than zero.")
    if model is not None:
        model = str(model).strip()
        if not model:
            raise ConfigError("model must not be empty.")

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
            raise ConfigError(f"Provider '{provider}' requires an API key.{env_hint}")
        if provider == "custom" and not base_url:
            raise ConfigError(
                "Provider 'custom' requires a base URL. Set PRMPT_BASE_URL or pass --base-url."
            )
        if provider not in {"claude-cli", "none"} and not model:
            raise ConfigError(
                f"Provider '{provider}' requires a model. Pass --model or set PRMPT_MODEL."
            )
        tier = model_tier(model)
        if (
            tier is not None
            and max_model_tier != "any"
            and TIER_ORDER[tier] > TIER_ORDER[max_model_tier]
        ):
            raise ConfigError(
                f"Model '{model}' is {tier}-tier, above the max_model_tier ceiling "
                f"'{max_model_tier}'. Use --model {max_model_tier} or cheaper, or "
                "raise the ceiling with --max-model-tier."
            )

    return Config(
        provider=provider,
        model=str(model) if model else None,
        base_url=base_url,
        api_key=api_key,
        key_source=key_source,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        stream=stream,
        retries=retries,
        max_input_bytes=max_input_bytes,
        max_model_tier=max_model_tier,
    )


def describe_config(config: Config) -> str:
    """Human-readable configuration with no secret values."""
    is_cli = config.provider == "claude-cli"
    cli_note = " (not applied by claude-cli)" if is_cli else ""
    stream_note = " (claude-cli always streams)" if is_cli else ""
    api_note = " (set)" if config.api_key and config.key_source not in {
        "claude CLI session",
        "not needed",
        "missing",
    } else ""
    lines = [
        f"provider:        {config.provider}",
        f"model:           {config.model or '(provider default)'}",
        f"base_url:        {config.base_url or '(not applicable)'}",
        f"api_key:         {config.key_source}{api_note}",
        f"temperature:     {config.temperature}{cli_note}",
        f"max_tokens:      {config.max_tokens if config.max_tokens is not None else '(default)'}"
        f"{cli_note}",
        f"timeout:         {config.timeout:g}s",
        f"retries:         {config.retries}",
        f"stream:          {config.stream}{stream_note}",
        f"max_input_bytes: {config.max_input_bytes}",
        f"max_model_tier:  {config.max_model_tier}",
    ]
    return "\n".join(lines)
