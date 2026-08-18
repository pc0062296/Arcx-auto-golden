"""The whole project must be pure ASCII.

The target environment rejects any byte outside the ASCII range, so a single
stray non-ASCII character anywhere in the tree can make a file unusable there.
Guarding it with a test is the only way to keep it true as the project grows:
comments, docstrings, user-facing strings, config templates and docs all count.
"""

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist",
             ".eggs"}
# Binary artefacts would fail an ASCII decode for legitimate reasons.
SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".png", ".jpg", ".gz", ".bundle",
                 ".whl", ".gds"}


def _iter_files():
    """Every tracked text file, not just a known list of suffixes.

    Checking by exclusion rather than inclusion means a new kind of file
    cannot slip past the guard by having an unfamiliar extension.
    """
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        yield path


class AsciiOnlyTest(unittest.TestCase):
    def test_no_non_ascii_bytes(self):
        offenders = []
        for path in _iter_files():
            raw = path.read_bytes()
            for lineno, line in enumerate(raw.split(b"\n"), start=1):
                try:
                    line.decode("ascii")
                except UnicodeDecodeError:
                    text = line.decode("utf-8", errors="replace")
                    bad = sorted({c for c in text if ord(c) > 127})
                    offenders.append(
                        "%s:%d contains %s"
                        % (path.relative_to(ROOT), lineno,
                           " ".join("U+%04X" % ord(c) for c in bad[:8]))
                    )
        self.assertEqual(
            offenders[:40], [],
            "non-ASCII found in %d place(s):\n%s"
            % (len(offenders), "\n".join(offenders[:40])),
        )


if __name__ == "__main__":
    unittest.main()
