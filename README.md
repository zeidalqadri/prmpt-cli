# prmpt

Turn a rough request into a PRS-1 refinement dossier from your terminal.

```bash
prmpt "make our onboarding email less corporate"
```

`prmpt` sends your verbatim utterance plus metadata to an LLM using the PRS-1 operator prompt. It returns Markdown with an intent inventory, ambiguity heatmap, constraint/conflict register, a standalone production prompt, a delta analysis, a validation plan, failure modes, and a peer-review checklist.

## Install

From this directory:

```bash
bash install.sh
```

The installer prefers `uv tool`, then `pipx`, then a private virtualenv. It makes sure the install bin directory (normally `~/.local/bin`) is on your `PATH` and verifies the command. The install is non-editable: the code is copied out of this directory, so `prmpt` keeps working even if you move or delete the checkout. `uv` is the most reliable path on macOS because it does not depend on `ensurepip`.

While developing, install in place instead:

```bash
bash install.sh --editable
```

Do not use `--editable` from a cloud-synced folder (iCloud Drive, Dropbox, OneDrive). Every import then goes through the sync daemon and `prmpt` can stall for many seconds before printing anything; the installer warns when it detects this.

```bash
prmpt --version
```

To uninstall:

```bash
bash install.sh --uninstall
```

## Providers

`prmpt` works with a local CLI or any OpenAI-compatible HTTP API. The default provider is `auto`.

| Provider | How it authenticates | Default model |
|---|---|---|
| `claude-cli` | Your existing `claude` login | Claude Code default |
| `openrouter` | `OPENROUTER_API_KEY` | `deepseek/deepseek-chat` |
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `groq` | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
| `custom` | `PRMPT_API_KEY` + `PRMPT_BASE_URL` | required |

`auto` prefers a working local `claude` CLI, then the API keys above. If the Claude CLI is installed and signed in, it is used by default; force an API provider if you prefer. You can also force a provider:

```bash
prmpt "tighten this board update" --provider openrouter --model deepseek/deepseek-chat
```

For any other OpenAI-compatible endpoint:

```bash
PRMPT_API_KEY=... PRMPT_BASE_URL=https://your-host/v1 \
  prmpt "..." --provider custom --model your-model
```

If your endpoint does not support streaming, add `--no-stream`.

### Environment variables

| Variable | Purpose |
|---|---|
| `PRMPT_PROVIDER` | `auto`, `claude-cli`, `openrouter`, `openai`, `deepseek`, `groq`, `custom` |
| `PRMPT_MODEL` | Model id |
| `PRMPT_BASE_URL` | OpenAI-compatible base URL |
| `PRMPT_API_KEY` | API key for `custom` or an explicit provider |
| `PRMPT_TEMPERATURE` | Default `0.3` |
| `PRMPT_MAX_TOKENS` | Default `4096` |
| `PRMPT_TIMEOUT` | Request timeout in seconds, default `300` |

## Examples

```bash
# Start from a rough utterance
prmpt "make the Q3 board update less defensive by Friday, keep the legal caveat"

# Add context
prmpt "write a launch email" \
  --meta user.role=founder \
  --meta stakes=high \
  --meta audience=existing_customers \
  --meta downstream.system=hubspot

# Read the utterance from a file
prmpt --file utterance.txt --out dossier.md

# Read from stdin
cat utterance.txt | prmpt --stdin

# See exactly what would be sent, without calling a model
prmpt "make this clearer" --print-prompt

# Save and copy the result
prmpt "turn these notes into a decision memo" --out memo.md --copy
```

## Metadata

`--meta key=value` can be repeated. Dotted keys become nested objects. Values are parsed as JSON when possible, so `stakes=high`, `score=0.4`, and `flags=["a","b"]` all work.

`--metadata FILE` accepts a JSON object. JSON is valid YAML, so the model can parse it as metadata.

Useful keys:

```text
user.role
user.expertise_self_reported
domain
stakes
decision_or_outcome
desired_artifact
audience
downstream.system
downstream.format
privacy.class
deadline
```

## Configuration

Create a config template:

```bash
prmpt --init
```

This writes `~/.config/prmpt/config.json`. CLI flags override environment variables, which override the config file.

```json
{
  "provider": "auto",
  "model": null,
  "base_url": null,
  "temperature": 0.3,
  "max_tokens": 4096,
  "timeout": 300
}
```

Inspect the resolved setup without printing secrets:

```bash
prmpt --show-config
```

## Notes

- The utterance is passed through verbatim; typos and hedges are intentional signal.
- If no provider is available, `prmpt` tells you how to configure one and suggests `--print-prompt`.
- Output is streamed for HTTP providers and printed at completion for `claude-cli`.
- API keys are read from the environment, not written to config or logs.
- The dossier is a specification, not proof of effectiveness. Use its validation plan and the PRS-1 scorecard before claiming a measured improvement.
