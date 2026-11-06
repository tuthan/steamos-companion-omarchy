"""Repository-wide gate: the product was renamed, so no tracked file may use the old name.

The rename was a clean break (docs/decisions/clean-rename.md): every active
identifier moved to the SteamOS Companion namespace, with no alias or migration
path.  This test fails on any tracked file, or file name, that still carries a
former identifier, so a stale copy-paste cannot quietly recreate a split
namespace.  The two files below name the old identifiers on purpose and are the
only ones allowed to.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Compared case-insensitively, so ``SteamOSRemote`` and ``X-SteamOS-Remote-...``
# are covered by the lower-case spellings.  The human-readable two-word form of
# the old product name is deliberately not gated: the README upgrade note and
# the decision record have to say it.
FORMER_NAMES = (
    "steamos-remote",
    "steamosremote",
    "steam-remote",
    "decky-steam-remote",
    "omarchy-steam-remote",
)

# Files that must spell the former identifiers out: this gate, and the dated
# decision record that lists what was renamed.
EXEMPT = frozenset({
    "tests/test_naming.py",
    "docs/decisions/clean-rename.md",
})


def former_names_in(data: bytes) -> list[str]:
    lowered = data.lower()
    return [name for name in FORMER_NAMES if name.encode("ascii") in lowered]


def tracked_files() -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [os.fsdecode(entry) for entry in result.stdout.split(b"\0") if entry]


class FormerNameGateTests(unittest.TestCase):
    def test_the_matcher_finds_every_former_spelling_and_ignores_the_new_name(self):
        for name in FORMER_NAMES + ("SteamOSRemote", "X-SteamOS-Remote-TLS-Binding", "io.github.tuthan.SteamOSRemote"):
            with self.subTest(name=name):
                self.assertTrue(former_names_in(f"prefix {name} suffix".encode()), name)
        for name in ("steamos-companion", "SteamOSCompanion", "io.github.tuthan.steamoscompanion", "SteamOS Remote"):
            with self.subTest(name=name):
                self.assertEqual(former_names_in(name.encode()), [], name)

    def test_no_tracked_file_or_file_name_uses_a_former_name(self):
        files = tracked_files()
        if files is None:
            self.skipTest("not a git checkout")
        # An empty or truncated listing would make this gate pass vacuously.
        self.assertIn("README.md", files)
        self.assertGreater(len(files), 30)

        offenders: list[str] = []
        for relative in files:
            if relative in EXEMPT:
                continue
            for name in former_names_in(relative.encode()):
                offenders.append(f"{relative}: file name contains {name!r}")
            path = ROOT / relative
            # A tracked file deleted from the working tree, or a symlink, has no
            # content of its own to scan.
            if path.is_symlink() or not path.is_file():
                continue
            for number, line in enumerate(path.read_bytes().split(b"\n"), start=1):
                for name in former_names_in(line):
                    offenders.append(f"{relative}:{number}: {name!r}")
        self.assertEqual(offenders, [], "former product name found:\n" + "\n".join(offenders[:40]))

    def test_the_readme_carries_the_upgrade_note(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Upgrading from SteamOS Remote", readme)
        self.assertIn("pair again", readme.lower())


if __name__ == "__main__":
    unittest.main()
