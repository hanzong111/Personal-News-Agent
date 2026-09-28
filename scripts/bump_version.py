#!/usr/bin/env python3
"""Start a release: bump VERSION and turn the CHANGELOG's Unreleased notes into a version section.

    python scripts/bump_version.py patch|minor|major     # 1.0.0 -> 1.0.1 / 1.1.0 / 2.0.0
    python scripts/bump_version.py 1.4.0                 # or an exact version

Run it on a release branch cut from develop (release/X.Y.Z), merge that into develop, then open
the develop -> main pull request. Merging into main tags vX.Y.Z and publishes the GitHub Release.
"""
from __future__ import annotations
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/hanzong111/TickerPigeon"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def bump(current: str, how: str) -> str:
    m = SEMVER.match(current)
    if not m:
        raise SystemExit(f"VERSION is not X.Y.Z: {current!r}")
    major, minor, patch = map(int, m.groups())
    if how == "major":
        return f"{major + 1}.0.0"
    if how == "minor":
        return f"{major}.{minor + 1}.0"
    if how == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if not SEMVER.match(how):
        raise SystemExit(f"expected patch, minor, major or X.Y.Z, got {how!r}")
    if tuple(map(int, how.split("."))) <= (major, minor, patch):
        raise SystemExit(f"{how} is not higher than the current version {current}")
    return how


def changelog(text: str, old: str, new: str, today: str) -> str:
    head = "## [Unreleased]"
    if head not in text:
        raise SystemExit("CHANGELOG.md has no '## [Unreleased]' section")
    start = text.index(head) + len(head)
    nxt = text.find("\n## [", start)
    notes = text[start:nxt if nxt != -1 else len(text)].strip()
    if not notes:
        raise SystemExit("Nothing under '## [Unreleased]' — add the changes for this release first")
    if f"## [{new}]" in text:
        raise SystemExit(f"CHANGELOG.md already has a {new} section")
    text = text.replace(head, f"{head}\n\n## [{new}] - {today}", 1)   # the notes now sit under the new heading
    link = f"[Unreleased]: {REPO}/compare/v{old}...HEAD"
    new_links = f"[Unreleased]: {REPO}/compare/v{new}...HEAD\n[{new}]: {REPO}/compare/v{old}...v{new}"
    if link in text:
        text = text.replace(link, new_links)
    else:
        text = re.sub(r"^\[Unreleased\]: .*$", new_links, text, flags=re.M)
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip())
        return 2
    vfile, cfile = ROOT / "VERSION", ROOT / "CHANGELOG.md"
    old = vfile.read_text().strip()
    new = bump(old, argv[0])
    cfile.write_text(changelog(cfile.read_text(), old, new, date.today().isoformat()))
    vfile.write_text(new + "\n")
    print(f"{old} -> {new}. Review CHANGELOG.md, then commit: git commit -am 'chore: release {new}'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
