#!/usr/bin/env python3
"""Print one version's CHANGELOG.md section, for its GitHub Release notes.

Usage: scripts/release_notes.py VERSION [CHANGELOG]

Exits non-zero when the section is missing or empty, so a version is never
released without notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def release_notes(changelog: str, version: str) -> str:
    """The body of the `## [version]` section, without its heading.

    Raises ValueError if the section is missing or empty.
    """
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    following = re.search(r"^## \[", changelog[match.end() :], re.MULTILINE)
    end = match.end() + following.start() if following else len(changelog)
    body = changelog[match.end() : end].strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section for {version} is empty")
    return body


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[2]) if len(argv) == 3 else _CHANGELOG
    try:
        print(release_notes(path.read_text(encoding="utf-8"), argv[1]))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
