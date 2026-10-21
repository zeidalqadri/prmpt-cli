#!/usr/bin/env python3
"""Verify a built wheel contains everything the installed CLI needs at runtime."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REQUIRED = (
    "prmpt/__init__.py",
    "prmpt/__main__.py",
    "prmpt/cli.py",
    "prmpt/client.py",
    "prmpt/config.py",
    "prmpt/errors.py",
    "prmpt/metadata.py",
    "prmpt/prompt.py",
    "prmpt/prompts/prs1.md",
    "prmpt/py.typed",
)
FORBIDDEN = ("tests/", "build/", "dist/", "__pycache__", ".pyc")
ENTRY_POINT = "prmpt = prmpt.cli:main"


def check(path: Path) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for required in REQUIRED:
            if required not in names:
                problems.append(f"missing from wheel: {required}")
        for name in names:
            if any(token in name for token in FORBIDDEN):
                problems.append(f"unexpected file in wheel: {name}")
        entry_points = [name for name in names if name.endswith("entry_points.txt")]
        if not entry_points:
            problems.append("missing entry_points.txt")
        elif ENTRY_POINT not in archive.read(entry_points[0]).decode("utf-8"):
            problems.append(f"entry point not registered: {ENTRY_POINT}")
        prompt = archive.read("prmpt/prompts/prs1.md") if "prmpt/prompts/prs1.md" in names else b""
        if b"## OUTPUT CONTRACT" not in prompt:
            problems.append("bundled PRS-1 prompt looks truncated")
    return problems


def main(argv: list[str]) -> int:
    wheels = [Path(arg) for arg in argv[1:]]
    if not wheels:
        print("usage: check_wheel.py WHEEL [WHEEL...]", file=sys.stderr)
        return 2
    failed = False
    for wheel in wheels:
        problems = check(wheel)
        if problems:
            failed = True
            print(f"{wheel.name}: FAILED", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        else:
            print(f"{wheel.name}: ok ({len(REQUIRED)} required entries present)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
