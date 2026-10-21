# Security policy

## Supported versions

The latest release on `main` receives security fixes. Older tags are not patched.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private reporting:

<https://github.com/zeidalqadri/prmpt-cli/security/advisories/new>

Include the version (`prmpt --version`), your OS, a reproduction, and the impact you
believe it has. Expect an acknowledgement within a few days.

## What prmpt does with secrets

- **API keys come from the environment only.** `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
  `OPENROUTER_API_KEY`, `GROQ_API_KEY`, or `PRMPT_API_KEY`. They are never written to the
  config file, never written to `--out`, and never included in request metadata.
- **`--show-config` reports the variable name and whether it is set**, never the value.
- **The Claude path uses your existing CLI session.** `prmpt` invokes `claude` and does
  not read, store, or forward your Claude credentials.
- **The config file is created with mode `0600`** inside a `0700` directory. It contains
  no credentials by design.
- **Dossiers are written with default umask permissions.** They can contain sensitive
  business context; place `--out` targets accordingly.

## Prompt-injection posture

The utterance, metadata, exemplars, and prior iterations are treated as data:

- The PRS-1 prompt instructs the model to ignore instructions embedded in those blocks.
- Closing XML tags in user-controlled values are escaped so a payload cannot break out of
  its block.
- The Claude CLI is invoked with `--restricted` and `--no-session-persistence`, so the
  run cannot use tools and leaves no session behind.

This reduces, but does not eliminate, prompt-injection risk. A model can still follow
instructions inside data. Treat a dossier as untrusted text when it is fed to another
agent or tool.

## Out of scope

- Content of model output, including whether the dossier is accurate or safe to execute.
- Availability or terms of third-party providers.
- Local attackers who can already read your environment or your home directory.
