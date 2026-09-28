"""Versioning: VERSION is X.Y.Z with a matching CHANGELOG section, and the bump script cuts releases."""
import importlib.util
import re
import unittest
from pathlib import Path

import pipeline

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
bump_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_version)

CHANGELOG = """# Changelog

## [Unreleased]

### Added
- Discord threads.

## [1.0.0] - 2026-09-28

First public release.

[Unreleased]: https://github.com/hanzong111/Personal-News-Agent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/hanzong111/Personal-News-Agent/releases/tag/v1.0.0
"""


class RepoVersionTest(unittest.TestCase):
    def test_version_is_semver_and_documented(self):
        v = (ROOT / "VERSION").read_text().strip()
        self.assertRegex(v, r"^\d+\.\d+\.\d+$")
        self.assertEqual(v, pipeline.__version__)
        self.assertIn(f"## [{v}]", (ROOT / "CHANGELOG.md").read_text())


class BumpTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual("1.0.1", bump_version.bump("1.0.0", "patch"))
        self.assertEqual("1.1.0", bump_version.bump("1.0.3", "minor"))
        self.assertEqual("2.0.0", bump_version.bump("1.4.3", "major"))
        self.assertEqual("1.2.0", bump_version.bump("1.0.0", "1.2.0"))

    def test_rejects_lower_or_malformed(self):
        for bad in ("1.0.0", "0.9.9", "v1.1", "banana"):
            with self.assertRaises(SystemExit):
                bump_version.bump("1.0.0", bad)

    def test_changelog_moves_unreleased_notes_under_new_version(self):
        out = bump_version.changelog(CHANGELOG, "1.0.0", "1.1.0", "2026-10-15")
        self.assertIn("## [Unreleased]\n\n## [1.1.0] - 2026-10-15\n\n### Added\n- Discord threads.", out)
        self.assertIn("[Unreleased]: https://github.com/hanzong111/Personal-News-Agent/compare/v1.1.0...HEAD", out)
        self.assertIn("[1.1.0]: https://github.com/hanzong111/Personal-News-Agent/compare/v1.0.0...v1.1.0", out)
        self.assertEqual(1, len(re.findall(r"^\[Unreleased\]: ", out, re.M)))

    def test_refuses_an_empty_release(self):
        empty = CHANGELOG.replace("### Added\n- Discord threads.\n\n", "")
        with self.assertRaises(SystemExit):
            bump_version.changelog(empty, "1.0.0", "1.0.1", "2026-10-15")


if __name__ == "__main__":
    unittest.main()
