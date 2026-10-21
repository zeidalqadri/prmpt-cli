"""Model provider clients: OpenAI-compatible HTTP and the Claude CLI."""

from __future__ import annotations

import codecs
import contextlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

from . import __version__
from .config import Config
from .errors import ProviderError

Notify = Callable[[str], None]

# Gateway and rate-limit statuses worth repeating; everything else is the
# request's fault and would fail the same way again.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 30.0
BACKOFF_BASE = 0.5
BACKOFF_CAP = 8.0

_TRANSIENT_CLI_RE = re.compile(
    r"(rate.?limit|overloaded|temporarily unavailable|too many requests|"
    r"connection (reset|refused|error)|econnreset|etimedout|epipe|"
    r"socket hang up|\b(502|503|504|529)\b)",
    re.IGNORECASE,
)


def _default_notify(message: str) -> None:
    with contextlib.suppress(OSError):
        print(f"prmpt: {message}", file=sys.stderr, flush=True)


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _headers(config: Config) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        # Keep SSE unbuffered through proxies that would otherwise gzip it.
        "Accept-Encoding": "identity",
        "Accept": "text/event-stream" if config.stream else "application/json",
        "User-Agent": f"prmpt/{__version__}",
    }
    if config.base_url and "openrouter.ai" in config.base_url:
        headers["HTTP-Referer"] = "https://github.com/zeidalqadri/prmpt-cli"
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


def _backoff_delay(attempt: int) -> float:
    delay = min(BACKOFF_BASE * (2**attempt), BACKOFF_CAP)
    return delay + random.uniform(0, delay / 4)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER))
    except (TypeError, ValueError):
        # HTTP-date form; fall back to exponential backoff.
        return None


def _timeout_error(config: Config) -> ProviderError:
    return ProviderError(
        f"Provider timed out after {config.timeout:g}s.", retryable=True
    )


def _http_once(
    config: Config, system_prompt: str, user_message: str
) -> Iterator[str]:
    """One HTTP attempt. Raises ProviderError annotated with retry metadata."""
    if not config.base_url:
        raise ProviderError("No base URL configured for the HTTP provider.")
    body = json.dumps(_payload(config, system_prompt, user_message)).encode("utf-8")
    request = urllib.request.Request(
        _endpoint(config.base_url),
        data=body,
        headers=_headers(config),
        method="POST",
    )
    deadline = time.monotonic() + config.timeout

    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            if not config.stream:
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        f"Provider returned invalid JSON: {_error_message(raw)}",
                        retryable=True,
                    ) from exc
                if isinstance(data, dict) and "error" in data:
                    raise ProviderError(_error_message(raw))
                choices = data.get("choices") if isinstance(data, dict) else None
                if not choices:
                    raise ProviderError("Provider returned no choices.", retryable=True)
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                text = _text_from_content(content)
                if not text:
                    raise ProviderError("Provider returned an empty response.", retryable=True)
                yield text
                return

            saw_data = False
            for raw_line in response:
                if time.monotonic() > deadline:
                    raise _timeout_error(config)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or line.startswith("event:"):
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
                raise ProviderError("Provider returned an empty stream.", retryable=True)
    except urllib.error.HTTPError as exc:
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body_text = ""
        retryable = exc.code in RETRYABLE_STATUS
        raise ProviderError(
            f"HTTP {exc.code}: {_error_message(body_text)}",
            status=exc.code,
            retryable=retryable,
            retry_after=_retry_after(exc),
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ProviderError(
            f"Could not reach the provider: {reason}", retryable=True
        ) from exc
    except TimeoutError as exc:
        raise _timeout_error(config) from exc


def _stream_http(
    config: Config,
    system_prompt: str,
    user_message: str,
    notify: Notify | None = None,
) -> Iterator[str]:
    note = notify or _default_notify
    attempts = max(1, config.retries + 1)
    for attempt in range(attempts):
        produced = False
        try:
            for chunk in _http_once(config, system_prompt, user_message):
                produced = True
                yield chunk
            return
        except ProviderError as exc:
            if produced or not exc.retryable or attempt + 1 >= attempts:
                raise
            delay = exc.retry_after if exc.retry_after is not None else _backoff_delay(attempt)
            note(
                f"{exc} Retrying in {delay:.1f}s "
                f"(attempt {attempt + 2}/{attempts})."
            )
            time.sleep(delay)


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
            with contextlib.suppress(OSError):
                process.kill()

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
                            detail = (
                                event.get("result")
                                or event.get("api_error_status")
                                or "unknown error"
                            )
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
                with contextlib.suppress(OSError):
                    process.kill()
            returncode = process.wait()
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()

        stderr_file.seek(0)
        stderr_text = stderr_file.read().decode("utf-8", errors="replace")

    if expired.is_set():
        raise ProviderError(f"Claude CLI timed out after {config.timeout:g}s.")
    if returncode != 0:
        if _UNKNOWN_FLAG_RE.search(stderr_text):
            raise _UnsupportedCliFlags(_clean_cli_error(stderr_text))
        raise ProviderError(
            f"Claude CLI failed (exit {returncode}):\n{_clean_cli_error(stderr_text)}",
            retryable=bool(_TRANSIENT_CLI_RE.search(stderr_text)),
        )
    if not emitted:
        raise ProviderError("Claude CLI returned an empty response.", retryable=True)


def _claude_attempt(
    executable: str,
    config: Config,
    system_prompt: str,
    user_message: str,
    env: dict[str, str],
    streaming: bool,
) -> Iterator[str]:
    command = _claude_command(executable, config, system_prompt, streaming)
    yield from _claude_output(
        config, command, user_message, env, parse_json=streaming
    )


def _stream_claude_cli(
    config: Config,
    system_prompt: str,
    user_message: str,
    notify: Notify | None = None,
) -> Iterator[str]:
    executable = shutil.which("claude")
    if not executable:
        raise ProviderError(
            "The `claude` command was not found on PATH. "
            "Install Claude Code or choose another provider."
        )

    note = notify or _default_notify
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    attempts = max(1, config.retries + 1)

    for attempt in range(attempts):
        produced = False
        streaming = True
        while True:
            try:
                for chunk in _claude_attempt(
                    executable, config, system_prompt, user_message, env, streaming=streaming
                ):
                    produced = True
                    yield chunk
                return
            except _UnsupportedCliFlags:
                # Only safe to downgrade while nothing has been printed;
                # otherwise the retry would duplicate the dossier's opening.
                if produced or not streaming:
                    raise
                streaming = False
            except ProviderError as exc:
                if produced or not exc.retryable or attempt + 1 >= attempts:
                    raise
                delay = _backoff_delay(attempt)
                note(f"{exc} Retrying in {delay:.1f}s (attempt {attempt + 2}/{attempts}).")
                time.sleep(delay)
                break


def stream_response(
    config: Config,
    system_prompt: str,
    user_message: str,
    notify: Notify | None = None,
) -> Iterator[str]:
    if config.provider == "claude-cli":
        yield from _stream_claude_cli(config, system_prompt, user_message, notify)
    else:
        yield from _stream_http(config, system_prompt, user_message, notify)
