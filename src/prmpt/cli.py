"""Command-line interface for prmpt."""

from __future__ import annotations

# Module-scope imports stay lean: --version, --help and --show-config must not
# pay for urllib, subprocess or traceback. Heavy imports live in the function
# that needs them.
import argparse
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    PROVIDERS,
    TIER_CHOICES,
    Config,
    config_path,
    describe_config,
    resolve_config,
    write_default_config,
)
from .errors import ConfigError, OutputError, PrmptError, ProviderError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130

DESCRIPTION = """\
Turn a rough request into a PRS-1 refinement dossier.

The utterance is passed verbatim. The output includes an intent inventory,
ambiguity heatmap, constraint register, a standalone production prompt,
a delta analysis, and a validation plan.
"""

EPILOG = """\
examples:
  prmpt "make the Q3 board update less defensive, keep the legal caveat"
  prmpt "write a launch email" --meta user.role=founder --meta stakes=high
  prmpt --file utterance.txt --out dossier.md
  cat utterance.txt | prmpt --stdin
  prmpt "make this clearer" --print-prompt
  prmpt --show-config

exit codes:
  0  success
  1  provider or runtime failure
  2  configuration or usage error
  130  interrupted

environment:
  PRMPT_PROVIDER PRMPT_MODEL PRMPT_BASE_URL PRMPT_API_KEY
  PRMPT_TEMPERATURE PRMPT_MAX_TOKENS PRMPT_TIMEOUT PRMPT_RETRIES
  PRMPT_MAX_INPUT_BYTES PRMPT_MAX_MODEL_TIER PRMPT_CONFIG_PATH
  OPENAI_API_KEY DEEPSEEK_API_KEY OPENROUTER_API_KEY GROQ_API_KEY

API keys are read from the environment only; prmpt never writes them to disk.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prmpt",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Raw, unedited utterance. Quote it so the shell preserves punctuation.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", metavar="PATH", help="Read the utterance from a UTF-8 file.")
    source.add_argument(
        "--stdin", action="store_true", help="Read the utterance from standard input."
    )

    parser.add_argument(
        "--meta",
        action="append",
        metavar="KEY=VALUE",
        help="Add metadata; repeatable. Dotted keys nest, e.g. user.role=CFO.",
    )
    parser.add_argument(
        "--metadata",
        metavar="PATH",
        help="JSON (or YAML with PyYAML) metadata object to merge.",
    )
    parser.add_argument(
        "--system-prompt",
        metavar="PATH",
        help="Override the bundled PRS-1 system prompt with a file.",
    )

    parser.add_argument("--provider", choices=PROVIDERS, help="Model provider (default: auto).")
    parser.add_argument("--model", help="Model id or alias.")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL.")
    parser.add_argument(
        "--temperature",
        type=float,
        help="Sampling temperature (0-2; ignored by claude-cli).",
    )
    parser.add_argument(
        "--max-tokens", type=int, help="Maximum output tokens (ignored by claude-cli)."
    )
    parser.add_argument("--timeout", type=float, help="Per-attempt request timeout in seconds.")
    parser.add_argument(
        "--retries",
        type=int,
        metavar="N",
        help="Retry transient failures up to N times (0-10; default: 2).",
    )
    parser.add_argument(
        "--max-model-tier",
        choices=TIER_CHOICES,
        metavar="TIER",
        help=(
            "Highest model tier prmpt will call: haiku, sonnet, opus, or any. "
            "Default: sonnet."
        ),
    )
    parser.add_argument(
        "--max-input-bytes",
        type=int,
        metavar="N",
        help="Reject utterances larger than N UTF-8 bytes (default: 1048576).",
    )
    parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Stream output as it arrives (default: on). HTTP providers use SSE; "
            "claude-cli uses stream-json partial messages and falls back to "
            "text output on older CLI builds."
        ),
    )

    parser.add_argument(
        "--out", metavar="PATH", help="Save the dossier to a file ('-' for stdout)."
    )
    parser.add_argument("--copy", action="store_true", help="Copy the dossier to the clipboard.")
    parser.add_argument("--quiet", action="store_true", help="Suppress status messages on stderr.")

    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="Print the assembled system and user prompt without calling a model.",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Show the resolved provider and settings without printing secrets.",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help=f"Write a config template to {config_path()}.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing config with --init."
    )
    parser.add_argument("--debug", action="store_true", help="Show a traceback for any error.")
    parser.add_argument("--version", action="version", version=f"prmpt {__version__}")
    return parser


def _note(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(f"prmpt: {message}", file=sys.stderr)


def _decode(data: bytes, source: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{source} is not valid UTF-8 (first bad byte at offset {exc.start}). "
            "Re-save the input as UTF-8."
        ) from exc


def _read_utterance(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    selected = sum(
        [
            args.prompt is not None,
            args.file is not None,
            bool(args.stdin),
        ]
    )
    if selected > 1:
        parser.error("choose one input source: a positional prompt, --file, or --stdin.")

    source = "input"
    if args.file:
        path = Path(args.file).expanduser()
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"Could not read {path}: {exc}") from exc
        text = _decode(data, str(path))
        source = str(path)
    elif args.stdin:
        text = _decode(sys.stdin.buffer.read(), "standard input")
        source = "standard input"
    elif args.prompt is not None:
        text = args.prompt
        source = "prompt"
    elif not sys.stdin.isatty():
        text = _decode(sys.stdin.buffer.read(), "standard input")
        source = "standard input"
    else:
        parser.error("provide a prompt, --file PATH, or --stdin.")

    if not text.strip():
        raise ConfigError(f"The utterance from {source} is empty.")
    return text


def _check_size(args: argparse.Namespace, config: Config, text: str) -> None:
    size = len(text.encode("utf-8"))
    if size > config.max_input_bytes:
        raise ConfigError(
            f"The utterance is {size} bytes, over the {config.max_input_bytes}-byte limit. "
            "Raise it with --max-input-bytes or PRMPT_MAX_INPUT_BYTES if this is intentional."
        )


def _prompt_document(system_prompt: str, user_message: str) -> str:
    return f"=== SYSTEM ===\n{system_prompt}\n\n=== USER ===\n{user_message}"


def _write_output(path: str, text: str) -> None:
    """Write atomically so an interrupted run never leaves a partial dossier."""
    import tempfile

    if path == "-":
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        sys.stdout.flush()
        return

    target = Path(path).expanduser()
    temp_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text if text.endswith("\n") else text + "\n")
            temp_name = handle.name
        os.replace(temp_name, target)
    except OSError as exc:
        if temp_name:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
        raise OutputError(f"Could not write {target}: {exc}") from exc


def _copy_to_clipboard(text: str) -> bool:
    import shutil
    import subprocess

    commands: list[list[str]] = []
    if shutil.which("pbcopy"):
        commands.append(["pbcopy"])
    if shutil.which("wl-copy"):
        commands.append(["wl-copy"])
    if shutil.which("xclip"):
        commands.append(["xclip", "-selection", "clipboard"])
    for command in commands:
        try:
            process = subprocess.run(command, input=text, text=True, capture_output=True)
        except OSError:
            continue
        if process.returncode == 0:
            return True
    return False


def _copy(args: argparse.Namespace, text: str) -> None:
    if not args.copy:
        return
    if _copy_to_clipboard(text):
        _note(args, "copied to clipboard")
    else:
        _note(args, "clipboard tool not found; output was not copied.")


def _report_usage(args: argparse.Namespace, stats: dict[str, Any]) -> None:
    parts: list[str] = []
    if stats.get("input_tokens") is not None:
        parts.append(f"in {stats['input_tokens']}")
    if stats.get("cache_read_tokens"):
        parts.append(f"cached {stats['cache_read_tokens']}")
    if stats.get("output_tokens") is not None:
        parts.append(f"out {stats['output_tokens']}")
    if stats.get("cost_usd") is not None:
        parts.append(f"${float(stats['cost_usd']):.4f}")
    if parts:
        _note(args, "tokens: " + " · ".join(parts))


def _print_dry_run(
    args: argparse.Namespace,
    config: Config,
    system_prompt: str,
    user_message: str,
) -> None:
    document = _prompt_document(system_prompt, user_message)
    if args.out:
        _write_output(args.out, document)
        _note(args, f"wrote prompt to {args.out}")
    else:
        _write_output("-", document)
    _copy(args, document)


def _run(
    args: argparse.Namespace,
    config: Config,
    system_prompt: str,
    user_message: str,
) -> int:
    _note(args, f"{config.provider} · {config.model or '(provider default)'}")

    from .client import stream_response

    chunks: list[str] = []
    stats: dict[str, Any] = {}
    try:
        for chunk in stream_response(
            config,
            system_prompt,
            user_message,
            notify=lambda message: _note(args, message),
            stats=stats,
        ):
            chunks.append(chunk)
            sys.stdout.write(chunk)
            sys.stdout.flush()
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_OK

    text = "".join(chunks)
    sys.stdout.write("" if text.endswith("\n") else "\n")
    sys.stdout.flush()

    if args.out:
        _write_output(args.out, text)
        _note(args, f"wrote {args.out}")

    _copy(args, text)
    _report_usage(args, stats)
    return EXIT_OK


def _silence_stdout() -> None:
    """Avoid 'Exception ignored' noise when the reader closed the pipe."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty():
        parser.print_help()
        return EXIT_OK
    args = parser.parse_args(argv)

    try:
        if args.init:
            print(write_default_config(force=args.force))
            return EXIT_OK

        if args.show_config:
            config = resolve_config(args, dry_run=True)
            print(describe_config(config))
            return EXIT_OK

        from .metadata import build_metadata
        from .prompt import build_system_prompt, build_user_message

        config = resolve_config(args, dry_run=args.print_prompt)
        utterance = _read_utterance(args, parser)
        _check_size(args, config, utterance)
        metadata = build_metadata(
            meta_pairs=args.meta,
            metadata_file=args.metadata,
            provider=config.provider,
            model=config.model,
            temperature=config.temperature,
            utterance=utterance,
        )
        system_prompt = build_system_prompt(args.system_prompt)
        user_message = build_user_message(utterance, metadata)

        if args.print_prompt:
            _print_dry_run(args, config, system_prompt, user_message)
            return EXIT_OK

        return _run(args, config, system_prompt, user_message)

    except KeyboardInterrupt:
        print("\nprmpt: cancelled", file=sys.stderr)
        return EXIT_INTERRUPT
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_OK
    except ConfigError as exc:
        if args.debug:
            import traceback

            traceback.print_exc()
        print(f"prmpt: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ProviderError as exc:
        if args.debug:
            import traceback

            traceback.print_exc()
        print(f"prmpt: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except (PrmptError, OutputError) as exc:
        if args.debug:
            import traceback

            traceback.print_exc()
        print(f"prmpt: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - last-resort CLI boundary
        if args.debug:
            import traceback

            traceback.print_exc()
        print(f"prmpt: unexpected error: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
