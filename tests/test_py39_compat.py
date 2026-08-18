"""目標執行環境是 Python 3.9.10。

開發機可能是較新的版本, 所以用 AST 的 feature_version 明確擋住
3.10+ 才有的語法 (match / PEP 604 的 X | Y 等) 進到程式碼裡。
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
        self.assertEqual(failures, [], "以下檔案用到 3.10+ 語法:\n" + "\n".join(failures))

    def test_no_third_party_imports_in_core(self):
        """核心必須零第三方相依 —— 內網環境安裝套件是摩擦。

        PyYAML 只在讀 .yaml 設定檔時才 import (lazy), 不算核心相依。
        """
        allowed_stdlib_prefixes = (
            "arcx_auto", "os", "re", "sys", "time", "json", "typing",
            "dataclasses", "enum", "argparse", "subprocess", "shutil",
            "tempfile", "getpass", "unicodedata", "fnmatch", "pathlib",
            "collections", "itertools", "functools", "contextlib", "io",
            "ast", "unittest", "threading", "hashlib", "uuid", "errno",
            "math", "textwrap", "traceback", "logging",
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
                    # yaml 是 lazy import (只在讀 .yaml 時), 允許
                    if root == "yaml":
                        continue
                    offenders.append("%s: %s" % (path.relative_to(ROOT), name))
        self.assertEqual(offenders, [], "核心出現第三方相依:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
