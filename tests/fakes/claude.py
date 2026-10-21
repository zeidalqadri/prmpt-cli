"""Stand-in for the Claude CLI, used by the test suite.

Behaviour is selected with FAKE_CLAUDE_MODE. The script only implements the
argument shapes prmpt actually sends, so any drift in flags shows up as a
test failure rather than a silent behaviour change.
"""

from __future__ import annotations

import json
import os
import sys
import time

STREAM_FLAGS = {"stream-json", "--include-partial-messages", "--verbose"}
STREAM_PARTS = ["Hel", "lo", " from", " fake"]


def emit_stream() -> None:
    for part in STREAM_PARTS:
        event = {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": part}},
        }
        print(json.dumps(event), flush=True)
        time.sleep(0.02)
    print(
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "".join(STREAM_PARTS),
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 0,
                },
                "total_cost_usd": 0.0012,
                "modelUsage": {"claude-sonnet-5-5": {}},
            }
        ),
        flush=True,
    )


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(code)


def main() -> None:
    mode = os.environ.get("FAKE_CLAUDE_MODE", "stream")
    args = sys.argv[1:]

    if mode == "reject_stream_flags" and STREAM_FLAGS & set(args):
        fail("error: unknown option '--include-partial-messages'", 2)

    sys.stdin.read()

    if mode == "slow":
        time.sleep(60)
        return
    if mode == "fail_transient":
        fail("API error: 429 rate limit exceeded, please retry")
    if mode == "fail_permanent":
        fail("Invalid API key. Please run /login")
    if mode == "empty":
        return
    if mode == "error_result":
        print(json.dumps({"type": "result", "is_error": True, "result": "boom"}))
        return
    if mode == "flaky":
        state = os.environ["FAKE_CLAUDE_STATE"]
        if not os.path.exists(state):
            open(state, "w", encoding="utf-8").close()
            fail("API error: 503 service unavailable, retry later")
        emit_stream()
        return
    if mode in {"text", "reject_stream_flags"}:
        print("PLAIN TEXT OK", flush=True)
        return
    emit_stream()


if __name__ == "__main__":
    main()
