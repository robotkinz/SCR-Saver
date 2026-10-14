#!/usr/bin/env python3
"""Rewrite SOCIAL.md from the latest section of CHANGELOG.md."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHANGELOG = ROOT / "CHANGELOG.md"
SOCIAL = ROOT / "SOCIAL.md"
GITHUB = "https://github.com/robotkinz/SCR-Saver"


def latest_section(text: str) -> tuple[str, list[str]]:
    version = ""
    bullets: list[str] = []
    in_section = False
    for line in text.splitlines():
        heading = re.match(r"^##\s+(\S+)", line)
        if heading:
            if in_section:
                break
            version = heading.group(1)
            in_section = True
            continue
        if in_section and line.startswith("- "):
            bullets.append(line)
    if not version:
        raise SystemExit("CHANGELOG.md has no ## version heading")
    return version, bullets


def main() -> int:
    version, bullets = latest_section(CHANGELOG.read_text(encoding="utf-8"))
    lines = [
        f"SCR Saver {version}",
        "",
        GITHUB,
        "",
        "What's new:",
        "",
        *bullets,
        "",
    ]
    SOCIAL.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {SOCIAL} for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
