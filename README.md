# prmpt

Turn a rough, unedited request into a **PRS-1 refinement dossier** — intent inventory,
ambiguity heatmap, constraint register, a standalone production prompt, delta analysis,
and a validation plan — from your terminal.

`prmpt` sends your verbatim utterance plus metadata to an LLM using the bundled PRS-1
operator prompt. It is a single-purpose CLI: no server, no database, no dependencies
outside the Python standard library.

```console
$ prmpt "make the Q3 board update less defensive by Friday, keep the legal caveat"
prmpt: claude-cli · (provider default)
## INTENT INVENTORY
...
```

## Requirements

- Python 3.10 or newer
- One of: a signed-in `claude` CLI, an API key for a supported HTTP provider, or any
  OpenAI-compatible endpoint

## Install

From a checkout:

```bash
bash install.sh
```

The installer prefers `uv tool`, then `pipx`, then a private virtualenv, adds the bin
directory to your `PATH`, and verifies the command. The install is **non-editable**: the
code is copied into an isolated environment, so `prmpt` keeps working if you move or
delete the checkout.

For a development loop, install in place instead:

```bash
bash install.sh --editable
```

> Do not use `--editable` from a cloud-synced folder (iCloud Drive, Dropbox, OneDrive).
> Every import then goes through the sync daemon, and cold starts can stall for many
> seconds before anything prints. The installer warns when it detects this.

Uninstall:

```bash
bash install.sh --uninstall
```

## Providers

The default provider is `auto`.

| Provider | How it authenticates | Default model |
|---|---|---|
| `claude-cli` | Your existing `claude` login | Claude Code default |
| `openrouter` | `OPENROUTER_API_KEY` | `deepseek/deepseek-chat` |
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `groq` | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
| `custom` | `PRMPT_API_KEY` + base URL | required |

`auto` resolves in this order: explicit custom credentials (`PRMPT_API_KEY` plus a base
URL), a `claude` binary on `PATH`, then the API keys above (openai, deepseek, openrouter,
groq). Force a provider when you want a specific one:

```bash
prmpt "tighten this board update" --provider openrouter --model deepseek/deepseek-chat
```

Any other OpenAI-compatible endpoint:

```bash
PRMPT_API_KEY=... PRMPT_BASE_URL=https://your-host/v1 \
  prmpt "..." --provider custom --model your-model
```

## Usage

```bash
# Rough utterance, quoted so the shell preserves punctuation
prmpt "make the Q3 board update less defensive by Friday, keep the legal caveat"

# Add structured context
prmpt "write a launch email" \
  --meta user.role=founder \
  --meta stakes=high \
  --meta audience=existing_customers \
  --meta downstream.system=hubspot

# Read from a file, write the dossier to a file
prmpt --file utterance.txt --out dossier.md

# Read from stdin
cat utterance.txt | prmpt --stdin

# Inspect exactly what would be sent, without calling a model
prmpt "make this clearer" --print-prompt

# Save and copy the result
prmpt "turn these notes into a decision memo" --out memo.md --copy
```

`--out -` writes to stdout. `--out` is atomic: an interrupted run never leaves a
half-written dossier, and no temporary files are left behind.

### Metadata

`--meta key=value` can be repeated; dotted keys nest. Values are parsed as JSON where
possible, so `stakes=high`, `score=0.4`, and `flags=["a","b"]` all work.
`--metadata FILE` merges a JSON object (YAML with PyYAML installed) before `--meta`
values are applied.

Useful keys: `user.role`, `user.expertise_self_reported`, `domain`, `stakes`,
`decision_or_outcome`, `desired_artifact`, `audience`, `downstream.system`,
`downstream.format`, `privacy.class`, `deadline`.

Every request also carries a generated `request_id`, a timestamp, the provider/model, a
`prs1-metadata/1` schema tag, and an input fingerprint (byte count and SHA-256 prefix).

## Configuration

```bash
prmpt --init          # write ~/.config/prmpt/config.json (mode 0600)
prmpt --show-config   # show the resolved settings; never prints the key itself
```

CLI flags override environment variables, which override the config file.

| Key | Default | Meaning |
|---|---|---|
| `provider` | `auto` | Provider name |
| `model` | `null` | Model id; provider default when unset |
| `base_url` | `null` | OpenAI-compatible base URL |
| `temperature` | `0.3` | Sampling temperature (HTTP providers only) |
| `max_tokens` | `4096` | Output token cap (HTTP providers only) |
| `timeout` | `300` | Per-attempt timeout in seconds |
| `retries` | `2` | Retries for transient failures (0–10) |
| `stream` | `true` | Stream HTTP responses via SSE |
| `max_input_bytes` | `1048576` | Reject larger utterances |

Unknown keys are rejected, so a typo fails loudly instead of being ignored.

### Environment variables

| Variable | Purpose |
|---|---|
| `PRMPT_PROVIDER` | Provider name |
| `PRMPT_MODEL` | Model id |
| `PRMPT_BASE_URL` | OpenAI-compatible base URL |
| `PRMPT_API_KEY` | Key for `custom` or an explicit provider |
| `PRMPT_TEMPERATURE` | Sampling temperature |
| `PRMPT_MAX_TOKENS` | Output token cap |
| `PRMPT_TIMEOUT` | Per-attempt timeout |
| `PRMPT_RETRIES` | Retry count |
| `PRMPT_MAX_INPUT_BYTES` | Input size limit |
| `PRMPT_CONFIG_PATH` | Alternative config file location |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Provider or runtime failure (model, network, output write) |
| `2` | Configuration or usage error (bad flags, missing credentials, oversized input) |
| `130` | Interrupted (Ctrl-C) |

## Behaviour worth knowing

- **Verbatim input.** The utterance is passed through unchanged; typos and hedges are
  signal. Only closing XML tags inside user-controlled blocks are escaped.
- **Fail fast.** Missing credentials, unknown config keys, invalid UTF-8 input, and
  oversized input are reported before any model call.
- **Streaming.** HTTP providers stream SSE; `claude-cli` streams stream-json partial
  messages, with a text-mode fallback for older CLI builds. `--no-stream` affects HTTP
  providers only.
- **Retries.** Transient failures (HTTP 408/425/429/5xx, connection errors, rate-limited
  CLI runs) are retried with jittered exponential backoff, honouring `Retry-After`.
  Content already printed is never replayed, so a mid-stream failure fails cleanly
  instead of duplicating the opening of the dossier.
- **Timeouts.** `timeout` is per attempt. The Claude CLI child process is killed on
  timeout rather than abandoned.
- **No silent downgrade.** `--debug` prints a traceback for any error; without it you get
  a one-line message and a specific exit code.

## Troubleshooting

**`prmpt` takes seconds before printing anything.** The source tree is on a synced
volume or a slow filesystem. Install non-editable (`bash install.sh`) and keep the
checkout on local disk.

**`No model provider is available`.** No `claude` binary and no API key. Install and sign
in to Claude Code, or export one of the supported keys, or use `--print-prompt`.

**`claude-cli` fails with an authentication message.** Run `claude` once and sign in.
`prmpt` uses your existing CLI session; it never handles your Claude credentials.

**HTTP 429 / 503.** The request is retried automatically (default 2 retries). Lower
`--temperature`/`--max-tokens`, or raise `--retries` for a flaky gateway.

**Empty response.** The provider returned nothing. Rerun with `--debug` to see the
underlying error; check `--show-config` to confirm which provider answered.

**Input rejected as non-UTF-8.** Re-save the file as UTF-8. UTF-8 with BOM is accepted.

## Security

API keys are read from the environment only. They are never written to the config file,
never included in the dossier, and never printed by `--show-config` (it reports the
variable name and whether it is set). The config file is created with mode `0600` inside
a `0700` directory.

Utterances are data, not instructions: the PRS-1 prompt instructs the model to ignore
instructions embedded in the utterance, metadata, exemplars, or prior iterations, and
user-controlled closing tags are escaped. See [SECURITY.md](SECURITY.md) for reporting.

## Development

```bash
make test     # 97 tests, stdlib unittest, no network
make lint     # ruff
make check    # lint + test
make build    # wheel + packaging checks
make smoke    # installed command, no model call
```

Tests cover config resolution and precedence, prompt assembly, metadata merging, HTTP
streaming/retry/timeout behaviour against a local mock server, the Claude CLI adapter
against a committed fake `claude`, and end-to-end CLI behaviour including exit codes,
stdin/file input, and atomic output.

CI runs lint plus the test suite on Linux and macOS across Python 3.10–3.13, builds a
wheel, and smoke-tests the installed artifact. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Notes

- The dossier is a specification, not proof of effectiveness. Use its validation plan and
  the PRS-1 scorecard before claiming a measured improvement.
- `prmpt` is intentionally single-shot: no sessions, no history, no telemetry.

## License

MIT — see [LICENSE](LICENSE).
