"""Command-line interface for prmpt."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .client import stream_response
from .config import (
    CONFIG_PATH,
    PROVIDERS,
    Config,
    describe_config,
    resolve_config,
    write_default_config,
)
from .errors import ConfigError, PrmptError, ProviderError
from .metadata import build_metadata
from .prompt import build_system_prompt, build_user_message

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
    source.add_argument("--stdin", action="store_true", help="Read the utterance from standard input.")

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
    parser.add_argument("--temperature", type=float, help="Sampling temperature (0-2; ignored by claude-cli).")
    parser.add_argument("--max-tokens", type=int, help="Maximum output tokens (ignored by claude-cli).")
    parser.add_argument("--timeout", type=float, help="Request timeout in seconds.")
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

    parser.add_argument("--out", metavar="PATH", help="Save the dossier to a file.")
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
        help=f"Write a config template to {CONFIG_PATH}.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite an existing config with --init.")
    parser.add_argument("--debug", action="store_true", help="Show a traceback for unexpected errors.")
    parser.add_argument("--version", action="version", version=f"prmpt {__version__}")
    return parser


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

    if args.file:
        try:
            text = Path(args.file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Could not read {args.file}: {exc}") from exc
    elif args.stdin:
        text = sys.stdin.read()
    elif args.prompt is not None:
        text = args.prompt
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.error("provide a prompt, --file PATH, or --stdin.")

    if not text.strip():
        raise ConfigError("The utterance is empty.")
    return text


def _prompt_document(system_prompt: str, user_message: str) -> str:
    return f"=== SYSTEM ===\n{system_prompt}\n\n=== USER ===\n{user_message}"


def _write_output(path: str, text: str) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def _copy_to_clipboard(text: str) -> bool:
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


def _print_dry_run(
    args: argparse.Namespace,
    config: Config,
    system_prompt: str,
    user_message: str,
) -> None:
    document = _prompt_document(system_prompt, user_message)
    if args.out:
        _write_output(args.out, document)
        if not args.quiet:
            print(f"prmpt: wrote prompt to {args.out}", file=sys.stderr)
    else:
        print(document)


def _run(
    args: argparse.Namespace,
    config: Config,
    system_prompt: str,
    user_message: str,
) -> int:
    if not args.quiet:
        model = config.model or "(provider default)"
        print(f"prmpt: {config.provider} · {model}", file=sys.stderr)

    chunks: list[str] = []
    try:
        for chunk in stream_response(config, system_prompt, user_message):
            chunks.append(chunk)
            sys.stdout.write(chunk)
            sys.stdout.flush()
    except BrokenPipeError:
        return 0

    text = "".join(chunks)
    if not text.strip():
        raise ProviderError("The provider returned an empty response.")

    if not text.endswith("\n"):
        sys.stdout.write("\n")
        sys.stdout.flush()

    if args.out:
        _write_output(args.out, text)
        if not args.quiet:
            print(f"prmpt: wrote {args.out}", file=sys.stderr)

    if args.copy:
        if _copy_to_clipboard(text):
            if not args.quiet:
                print("prmpt: copied to clipboard", file=sys.stderr)
        else:
            print("prmpt: clipboard tool not found; output was not copied.", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty():
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    try:
        if args.init:
            path = write_default_config(force=args.force)
            print(path)
            return 0

        if args.show_config:
            config = resolve_config(args, dry_run=True)
            print(describe_config(config))
            return 0

        utterance = _read_utterance(args, parser)
        config = resolve_config(args, dry_run=args.print_prompt)
        metadata = build_metadata(
            meta_pairs=args.meta,
            metadata_file=args.metadata,
            provider=config.provider,
            model=config.model,
            temperature=config.temperature,
        )
        system_prompt = build_system_prompt(args.system_prompt)
        user_message = build_user_message(utterance, metadata)

        if args.print_prompt:
            _print_dry_run(args, config, system_prompt, user_message)
            return 0

        return _run(args, config, system_prompt, user_message)

    except KeyboardInterrupt:
        print("\nprmpt: cancelled", file=sys.stderr)
        return 130
    except (ConfigError, ProviderError) as exc:
        print(f"prmpt: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 1
    except PrmptError as exc:
        print(f"prmpt: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort CLI boundary
        if args.debug:
            raise
        print(f"prmpt: unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
