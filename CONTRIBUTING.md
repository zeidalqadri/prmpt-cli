# Contributing

## Setup

```bash
git clone https://github.com/zeidalqadri/prmpt-cli
cd prmpt-cli
bash install.sh --editable   # only from a local, non-synced folder
make check
```

Run the CLI from the checkout without installing:

```bash
PYTHONPATH=src python3 -m prmpt "make this clearer" --print-prompt
```

## Layout

```text
src/prmpt/cli.py        argument parsing, input decoding, output writing, exit codes
src/prmpt/client.py     OpenAI-compatible SSE + Claude CLI providers, retries, deadlines
src/prmpt/config.py     flag/env/file precedence, provider resolution, validation
src/prmpt/prompt.py     PRS-1 system prompt assembly and input sandboxing
src/prmpt/metadata.py   request metadata, input fingerprint, --meta merging
src/prmpt/prompts/      bundled PRS-1 operator prompt
tests/                  stdlib unittest; tests/fakes/claude is a scripted CLI stand-in
```

## Conventions

- Standard library only at runtime. Do not add dependencies to `dependencies`.
- Keep the CLI boundary honest: expected failures raise `ConfigError` (exit 2),
  `ProviderError` (exit 1), or `OutputError` (exit 1). Never let a raw exception reach the
  user without `--debug`.
- Never print, log, or persist an API key.
- New flags need: `--help` text, config/env support where it makes sense, a test, and a
  README row.
- Provider behaviour changes need a test in `tests/test_client.py`; keep
  `tests/fakes/claude.py` in sync with any flag you start passing.
- Line length 100, ruff clean (`make lint`), no formatter reformatting.

## Tests

```bash
make test        # or: python3 -m unittest discover -s tests -v
```

The suite is offline and hermetic: the HTTP provider is a local mock server, the Claude
provider is `tests/fakes/claude.py` driven by `FAKE_CLAUDE_MODE`, and config paths are
redirected with `PRMPT_CONFIG_PATH`. Do not add tests that call a real model.

## Commits and releases

- Commit messages: imperative subject, then a body explaining why.
- Update `CHANGELOG.md` under `Unreleased` (or the upcoming version) in the same commit as
  the change.
- Version lives in `src/prmpt/__init__.py`; the build reads it from there.
- Release: bump the version, move the changelog section, commit, then
  `git tag vX.Y.Z && git push origin main --tags`. The release workflow builds the wheel
  and publishes it with generated notes.
