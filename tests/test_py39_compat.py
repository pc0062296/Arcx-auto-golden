"""The target runtime is Python 3.9.10.

The development machine may be newer, so the AST feature_version is used to
block 3.10+ only syntax (match, PEP 604 X | Y and so on) from entering the
code base.
"""

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = (3, 9)


class Py39CompatTest(unittest.TestCase):
    def test_all_sources_parse_under_python_39(self):
        failures = []
        for package in ("arcx_auto", "tests"):
            for path in sorted((ROOT / package).rglob("*.py")):
                source = path.read_text(encoding="utf-8")
                try:
                    ast.parse(source, filename=str(path), feature_version=TARGET)
                except SyntaxError as exc:
                    failures.append("%s: %s" % (path.relative_to(ROOT), exc))
        self.assertEqual(
            failures, [],
            "these files use 3.10+ syntax:\n" + "\n".join(failures))

    def test_no_third_party_imports_in_core(self):
        """The core must have zero third-party dependencies: installing

        PyYAML is imported lazily and only when reading a .yaml settings
        file, so it does not count as a core dependency.
        """
        allowed_stdlib_prefixes = (
            # Every addition here is a conscious decision: this list is the
            # dependency gate
            "arcx_auto", "os", "re", "sys", "time", "json", "typing",
            "dataclasses", "enum", "argparse", "subprocess", "shutil",
            "tempfile", "getpass", "unicodedata", "fnmatch", "pathlib",
            "collections", "itertools", "functools", "contextlib", "io",
            "ast", "unittest", "threading", "hashlib", "uuid", "errno",
            "math", "textwrap", "traceback", "logging",
            # used by the daemon and the web UI
            "socket", "fcntl", "signal", "http", "urllib", "html", "pwd",
            "webbrowser",
        )
        offenders = []
        for path in sorted((ROOT / "arcx_auto").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    root = name.split(".")[0]
                    if root == "__future__" or root in allowed_stdlib_prefixes:
                        continue
                    # yaml is a lazy import, only when reading .yaml
                    if root == "yaml":
                        continue
                    offenders.append("%s: %s" % (path.relative_to(ROOT), name))
        self.assertEqual(
            offenders, [],
            "third-party imports in the core:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()


class NoRealSharedDiskInTestsTest(unittest.TestCase):
    """The suite must never write to the configured shared disk.

    export.shared_root is a real path outside the test tree, and the daemon
    publishes to it on a timer. A test that forgets to redirect it writes into
    whatever is mounted there on the machine running the suite -- which on a
    developer's box is somebody else's status page.
    """

    def test_the_default_share_is_untouched(self):
        import os

        from arcx_auto.config.settings import Settings

        shared = os.path.abspath(
            os.path.expanduser(Settings().export.shared_root))
        self.assertFalse(
            os.path.exists(shared),
            "the test suite wrote to the real shared disk at %s; a test built "
            "a daemon or an Exporter without redirecting export.shared_root"
            % shared)

    def test_the_default_run_root_is_untouched(self):
        """run_root defaults to ./arcx_runs, which is the repository itself
        while the suite runs. A test that builds a workspace without
        redirecting it leaves wave directories in the source tree.
        """
        import os

        from arcx_auto.config.settings import Settings

        run_root = Settings().expanded_run_root()
        self.assertFalse(
            os.path.exists(run_root),
            "the test suite created %s; a test built a workspace, a daemon or "
            "a submission without redirecting run_root" % run_root)
