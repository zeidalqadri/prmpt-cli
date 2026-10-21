# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-10-21

### Added

- **Model-tier ceiling.** `max_model_tier` (flag `--max-model-tier`, env
  `PRMPT_MAX_MODEL_TIER`, config key) defaults to `sonnet`. A model above the ceiling is
  refused before the call, with the fix in the message. Values: `haiku`, `sonnet`,
  `opus`, `any`.
- **Usage and cost reporting.** After a real run, stderr shows the counters the provider
  reported (`tokens: in … · cached … · out … · $…`). Claude CLI usage comes from its
  `result` event; HTTP usage from the response's `usage` object. `--quiet` hides it.
- Failing tests now name the behaviour they protect; the suite covers the ceiling,
  usage capture, and tier heuristics (97 → 114 tests).

### Changed

- `claude-cli` is invoked with an explicit `--model` pinned to the tier ceiling (`sonnet`
  by default) instead of inheriting the CLI's own default, which `prmpt` could not see or
  cap. Set `--max-model-tier` to change it.
- `prmpt` imports no HTTP stack for `--version`, `--help`, or `--show-config`; module-scope
  imports are limited to what those paths need. Measured import cost of `prmpt.cli`:
  370 ms → 17 ms.
- `--show-config` reports `max_model_tier` and the effective (pinned) model.

## [0.2.0] - 2026-10-21

Production hardening. No change to the dossier contract.

### Added

- Retry with jittered exponential backoff for transient failures: HTTP 408/425/429/5xx,
  connection errors, and rate-limited Claude CLI runs. `Retry-After` is honoured.
- `--retries` flag and `PRMPT_RETRIES` / `retries` config key (0–10, default 2).
- Per-attempt deadlines for HTTP streaming, so a slow-drip response cannot outlive
  `timeout`; the Claude CLI child is killed rather than abandoned.
- `--max-input-bytes` flag and `PRMPT_MAX_INPUT_BYTES` / `max_input_bytes` config key.
- Atomic `--out` writes via temporary file plus rename; `--out -` writes to stdout.
- `--debug` now prints a traceback for every error class, not only unexpected ones.
- Documented exit codes: `0` success, `1` provider/runtime failure, `2` config/usage,
  `130` interrupted.
- Metadata now carries `schema`, `prmpt_version`, and an input fingerprint (bytes,
  characters, SHA-256 prefix).
- `py.typed`, package classifiers, project URLs, and a `dev` extra.
- Test suite grown from 10 to 97 tests: config precedence/validation, metadata merging,
  prompt assembly, HTTP streaming/retry/timeout against a mock server, the Claude CLI
  adapter against a committed fake `claude`, and end-to-end CLI behaviour.
- `Makefile`, GitHub Actions CI (lint, Linux/macOS matrix, wheel build and smoke test),
  and a tagged release workflow.
- `SECURITY.md` and `CONTRIBUTING.md`.

### Changed

- `install.sh` installs **non-editable** by default and forces a rebuild; `--editable`
  is now an explicit development opt-in. Stale bytecode and build metadata are cleaned
  before install, and a cloud-synced source tree triggers a warning.
- Config file keys are validated strictly: unknown keys are rejected instead of ignored.
- Config file is written with mode `0600` in a `0700` directory; `stream` and `retries`
  are now part of the template.
- `--stream` help text matches reality for both provider families.
- HTTP requests send `Accept-Encoding: identity` so SSE is not buffered by proxies.
- Input decoding is explicit UTF-8 (BOM tolerated) with a clear error on failure, for
  both `--file` and stdin.
- `--show-config` reports `retries`, `max_input_bytes`, and correct provider notes.
- `provider: "none"` is a valid config value; it produces the "no provider available"
  guidance instead of an "unknown provider" error.
- Version is defined once in `prmpt/__init__.py` and read by the build.

### Fixed

- `claude-cli` no longer buffers the whole dossier: output streams via `stream-json`
  partial messages, with a text-mode fallback when the installed CLI rejects those flags.
- Partial output is never replayed by a retry.
- Subprocess pipes are closed deterministically (no `ResourceWarning`).
- Broken pipes (`prmpt ... | head`) exit `0` without "Exception ignored" noise.
- A non-UTF-8 file or a directory passed to `--file` yields a usage error, not an
  unexpected-error traceback.
- Prompt files are read as UTF-8 with BOM handling.

## [0.1.0] - 2026-10-21

Initial release.

- `prmpt` CLI: positional utterance, `--file`, `--stdin`, `--meta`, `--metadata`,
  `--system-prompt`.
- Providers: `claude-cli`, `openrouter`, `openai`, `deepseek`, `groq`, `custom`, `auto`.
- SSE streaming for HTTP providers, `--out`, `--copy`, `--quiet`, `--print-prompt`,
  `--show-config`, `--init`.
- Bundled PRS-1 operator prompt with input sandboxing and a terminal output contract.
