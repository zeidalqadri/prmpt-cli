"""Model provider clients: OpenAI-compatible HTTP and the Claude CLI."""

from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from . import __version__
from .config import Config
from .errors import ProviderError


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _headers(config: Config) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if config.stream else "application/json",
        "User-Agent": f"prmpt/{__version__}",
    }
    if config.base_url and "openrouter.ai" in config.base_url:
        headers["HTTP-Referer"] = "https://github.com/example/prmpt-cli"
        headers["X-Title"] = "prmpt"
    return headers


def _payload(config: Config, system_prompt: str, user_message: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": config.temperature,
        "stream": config.stream,
    }
    if config.max_tokens is not None:
        payload["max_tokens"] = config.max_tokens
    return payload


def _error_message(body: str) -> str:
    body = body.strip()
    if not body:
        return "no error details"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:600]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or json.dumps(error)
            return str(message)[:600]
        if isinstance(error, str):
            return error[:600]
        if isinstance(data.get("message"), str):
            return data["message"][:600]
    return body[:600]


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(part, str):
                chunks.append(part)
        return "".join(chunks)
    return ""


def _extract_delta(event: dict[str, Any]) -> str:
    choices = event.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    delta = choice.get("delta")
    if isinstance(delta, dict):
        text = _text_from_content(delta.get("content"))
        if text:
            return text
    # Some compatible providers emit the older completion shape.
    return _text_from_content(choice.get("text"))


def _stream_http(config: Config, system_prompt: str, user_message: str) -> Iterator[str]:
    if not config.base_url:
        raise ProviderError("No base URL configured for the HTTP provider.")
    body = json.dumps(_payload(config, system_prompt, user_message)).encode("utf-8")
    request = urllib.request.Request(
        _endpoint(config.base_url),
        data=body,
        headers=_headers(config),
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            if not config.stream:
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        f"Provider returned invalid JSON: {_error_message(raw)}"
                    ) from exc
                if isinstance(data, dict) and "error" in data:
                    raise ProviderError(_error_message(raw))
                choices = data.get("choices") if isinstance(data, dict) else None
                if not choices:
                    raise ProviderError("Provider returned no choices.")
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                text = _text_from_content(content)
                if not text:
                    raise ProviderError("Provider returned an empty response.")
                yield text
                return

            saw_data = False
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and "error" in event:
                    raise ProviderError(_error_message(data))
                text = _extract_delta(event)
                if text:
                    saw_data = True
                    yield text
            if not saw_data:
                raise ProviderError("Provider returned an empty stream.")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(
            f"HTTP {exc.code}: {_error_message(body_text)}", status=exc.code
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ProviderError(f"Could not reach the provider: {reason}") from exc
    except TimeoutError as exc:
        raise ProviderError(f"Provider timed out after {config.timeout:g}s.") from exc


def _clean_cli_error(stderr: str) -> str:
    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.startswith("WARNING:")
    ]
    return "\n".join(lines[-8:]) or "the CLI exited without an error message"


class _UnsupportedCliFlags(ProviderError):
    """The installed Claude CLI rejected a streaming flag."""


# Older Claude CLI builds do not know the stream-json flags. Their argument
# parser fails before any model call, so retrying in text mode costs nothing.
_UNKNOWN_FLAG_RE = re.compile(r"unknown (option|argument|flag)", re.IGNORECASE)


def _claude_command(
    executable: str, config: Config, system_prompt: str, streaming: bool
) -> list[str]:
    command = [
        executable,
        "-p",
        "--restricted",
        "--no-session-persistence",
        "--system-prompt",
        system_prompt,
    ]
    if streaming:
        command.extend(
            [
                "--output-format",
                "stream-json",
                # stream-json in --print mode requires --verbose, and partial
                # messages are what turn it into token-by-token output.
                "--verbose",
                "--include-partial-messages",
            ]
        )
    else:
        command.extend(["--output-format", "text"])
    if config.model:
        command.extend(["--model", config.model])
    return command


def _claude_event_text(event: Any) -> str:
    """Extract incremental text from one stream-json event."""
    if not isinstance(event, dict):
        return ""
    if event.get("type") != "stream_event":
        return ""
    inner = event.get("event")
    if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
        return ""
    delta = inner.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def _claude_output(
    executable: str,
    config: Config,
    command: list[str],
    user_message: str,
    env: dict[str, str],
    parse_json: bool,
) -> Iterator[str]:
    """Run one Claude CLI call, yielding text as it arrives."""
    # stdin and stderr go through temp files: a pipe would deadlock if either
    # side ever filled its buffer while we were reading the other.
    with tempfile.TemporaryFile() as stdin_file, tempfile.TemporaryFile() as stderr_file:
        stdin_file.write(user_message.encode("utf-8"))
        stdin_file.seek(0)
        try:
            process = subprocess.Popen(
                command,
                stdin=stdin_file,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                env=env,
            )
        except OSError as exc:
            raise ProviderError(f"Could not run the Claude CLI: {exc}") from exc

        expired = threading.Event()

        def _expire() -> None:
            expired.set()
            try:
                process.kill()
            except OSError:
                pass

        timer = threading.Timer(config.timeout, _expire)
        timer.daemon = True
        timer.start()

        emitted = False
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            assert process.stdout is not None
            if parse_json:
                for raw_line in process.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "result":
                        if event.get("is_error"):
                            detail = event.get("result") or event.get("api_error_status") or "unknown error"
                            raise ProviderError(f"Claude CLI reported an error: {detail}")
                        if not emitted:
                            # Fallback for builds that ignore partial messages.
                            fallback = event.get("result")
                            if isinstance(fallback, str) and fallback.strip():
                                emitted = True
                                yield fallback
                        continue
                    text = _claude_event_text(event)
                    if text:
                        if text.strip():
                            emitted = True
                        yield text
            else:
                fd = process.stdout.fileno()
                while True:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    if text:
                        if text.strip():
                            emitted = True
                        yield text
        finally:
            timer.cancel()
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            returncode = process.wait()

        stderr_file.seek(0)
        stderr_text = stderr_file.read().decode("utf-8", errors="replace")

    if expired.is_set():
        raise ProviderError(f"Claude CLI timed out after {config.timeout:g}s.")
    if returncode != 0:
        if _UNKNOWN_FLAG_RE.search(stderr_text):
            raise _UnsupportedCliFlags(_clean_cli_error(stderr_text))
        raise ProviderError(
            f"Claude CLI failed (exit {returncode}):\n{_clean_cli_error(stderr_text)}"
        )
    if not emitted:
        raise ProviderError("Claude CLI returned an empty response.")


def _stream_claude_cli(config: Config, system_prompt: str, user_message: str) -> Iterator[str]:
    executable = shutil.which("claude")
    if not executable:
        raise ProviderError(
            "The `claude` command was not found on PATH. Install Claude Code or choose another provider."
        )

    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")

    produced = False
    try:
        for chunk in _claude_output(
            executable,
            config,
            _claude_command(executable, config, system_prompt, streaming=True),
            user_message,
            env,
            parse_json=True,
        ):
            produced = True
            yield chunk
        return
    except _UnsupportedCliFlags:
        # Only safe to retry while nothing has been printed; otherwise the
        # retry would duplicate the beginning of the dossier.
        if produced:
            raise

    yield from _claude_output(
        executable,
        config,
        _claude_command(executable, config, system_prompt, streaming=False),
        user_message,
        env,
        parse_json=False,
    )


def stream_response(
    config: Config, system_prompt: str, user_message: str
) -> Iterator[str]:
    if config.provider == "claude-cli":
        yield from _stream_claude_cli(config, system_prompt, user_message)
    else:
        yield from _stream_http(config, system_prompt, user_message)
