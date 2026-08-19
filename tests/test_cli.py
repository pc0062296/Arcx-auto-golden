"""CLI end-to-end tests.

They also assert a hard requirement: the read-only commands **must not write
to any run folder**.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from arcx_auto.cli.main import main
from tests.fixtures.fake_run import build_demo


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def snapshot_tree(root):
    """Record (path, size) for a whole tree, to prove nothing changed."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            result[os.path.join(dirpath, name)] = None
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                result[path] = os.path.getsize(path)
            except OSError:
                result[path] = -1
    return result


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.demo = build_demo(os.path.join(cls.tmp.name, "demo"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- inspect -------------------------------------------------------

    def test_inspect_dir_map(self):
        code, out, _ = run_cli(["inspect", "dir-map", self.demo["dir_map"]])
        self.assertEqual(code, 0)
        self.assertIn("1000", out)
        self.assertIn("max=1004", out)

    def test_inspect_dir_map_json(self):
        code, out, _ = run_cli(
            ["inspect", "dir-map", self.demo["dir_map"], "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("1000", data["entries"])
        self.assertNotIn("min", data["entries"])

    def test_inspect_index(self):
        path = os.path.join(self.demo["sources"], "1000_sram_core")
        code, out, _ = run_cli(["inspect", "index", path, "--json"])
        self.assertEqual(code, 0)
        spec = json.loads(out)[0]
        self.assertEqual(spec["gds_count"], 3)
        self.assertEqual(spec["cpu_per_case"], 4)
        self.assertIn("sram", spec["keywords"])

    # -- status --------------------------------------------------------

    def test_status_wave_dir(self):
        code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf"])
        self.assertEqual(code, 0)
        self.assertIn("overview", out)
        self.assertIn("DONE", out)
        self.assertIn("STALLED", out)

    def test_status_detects_fake_success(self):
        """The demo has three cases with a .complete marker; only one really
        succeeded.

        By eye all three look successful, which is exactly what the system
        exists to catch.
        """
        code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf"])
        self.assertEqual(code, 0)
        self.assertIn("FAILED", out)
        self.assertIn("NETLIST_MISSING", out)
        self.assertIn("NETLIST_EMPTY", out)

    def test_status_no_qa_keeps_raw_marker_state(self):
        """With --no-qa it observes only and does not narrow COMPLETED_MARKER
        into DONE or FAILED.
        """
        code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf", "--no-qa"])
        self.assertEqual(code, 0)
        self.assertIn("COMPLETED_MARKER", out)
        self.assertNotIn("QA issues", out)

    def test_status_json_carries_qa_issues(self):
        code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        ids = {i["id"] for i in data["qa_issues"]}
        self.assertIn("NETLIST_MISSING", ids)

    def test_status_issues_flag_shows_warnings_too(self):
        _c, brief, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf"])
        _c, full, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf", "--issues"])
        self.assertGreater(len(full), len(brief))
        self.assertIn("CASE_QUIET", full)

    def test_status_json(self):
        code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["index_runs"]), 2)
        keys = {r["index_key"] for r in data["index_runs"]}
        self.assertEqual(keys, {"1000", "1001"})

    def test_status_reports_lsf_unavailable(self):
        """When LSF is unavailable it must say so, or the reader assumes the
        picture is complete.
        """
        _code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf"])
        self.assertIn("LSF data unavailable", out)
        self.assertIn("detection is disabled", out)

    def test_status_missing_run_folder_is_reported(self):
        code, out, _ = run_cli(
            ["status", "--run-folder", "/definitely/not/here", "--no-lsf"])
        self.assertEqual(code, 0)
        self.assertIn("does not exist", out)

    def test_status_state_file_persists_across_calls(self):
        state = os.path.join(self.tmp.name, "state.json")
        run_cli(["status", "--wave-dir", self.demo["wave_dir"],
                 "--no-lsf", "--state-file", state])
        self.assertTrue(os.path.exists(state))
        code, out, _ = run_cli(["status", "--wave-dir", self.demo["wave_dir"],
                                "--no-lsf", "--state-file", state])
        self.assertEqual(code, 0)
        self.assertIn("overview", out)

    def test_status_does_not_modify_run_folder(self):
        """Hard requirement: status is read only."""
        before = snapshot_tree(self.demo["wave_dir"])
        run_cli(["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf",
                 "--detail"])
        self.assertEqual(snapshot_tree(self.demo["wave_dir"]), before)

    # -- plan ----------------------------------------------------------

    def test_plan_all(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--max-slots", "100"])
        self.assertEqual(code, 0)
        self.assertIn("wave plan", out)
        self.assertIn("wave_001", out)

    def test_plan_json_structure(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--max-slots", "100", "--json"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["mode"], "AUTO")
        self.assertTrue(plan["waves"])
        # 1004 has no GDS and no special.cfg -> excluded, with a reason
        self.assertEqual([s["index_key"] for s in plan["excluded"]], ["1004"])

    def test_plan_priority_keyword_first(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--max-slots", "1000", "--json"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        first_wave_keys = [i["index_key"] for i in plan["waves"][0]["indices"]]
        # 1000 (sram) and 1002 (ro) hit keywords, so they come first
        self.assertEqual(first_wave_keys[:2], ["1000", "1002"])

    def test_plan_off_mode_single_wave(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--mode", "off", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["waves"]), 1)

    def test_plan_show_command(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--index", "1000",
             "--arcx-cfg", "arcx.cfg", "--show-command"])
        self.assertEqual(code, 0)
        self.assertIn("Arcx -p arcx.cfg -d 1000 -lsf0 -nt 50 --run", out)

    def test_plan_unknown_index_is_excluded_with_reason(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--index", "9999",
             "--json"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["waves"], [])
        self.assertIn("not found", plan["excluded"][0]["error"])

    def test_plan_requires_index_selection(self):
        code, _out, err = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"]])
        self.assertEqual(code, 2)
        self.assertIn("--index", err)

    def test_plan_creates_no_directories(self):
        """Hard requirement: plan only computes; it creates nothing and
        submits nothing.
        """
        before = snapshot_tree(self.demo["root"])
        run_cli(["plan", "--dir-map", self.demo["dir_map"], "--all",
                 "--show-command"])
        self.assertEqual(snapshot_tree(self.demo["root"]), before)


class ConfigTest(unittest.TestCase):
    def test_missing_config_file_is_an_error(self):
        code, _out, err = run_cli(
            ["-c", "/no/such/config.json", "inspect", "dir-map", "/x"])
        self.assertEqual(code, 2)
        self.assertIn("failed to load settings", err)

    def test_json_config_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "c.json")
            with open(cfg, "w") as handle:
                json.dump({"plan": {"max_slots_per_wave": 7}}, handle)
            demo = build_demo(os.path.join(tmp, "demo"))
            code, out, _ = run_cli(
                ["-c", cfg, "plan", "--dir-map", demo["dir_map"],
                 "--all", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["max_slots_per_wave"], 7)

    def test_shipped_default_yaml_has_no_unknown_fields(self):
        """config/default.yaml and the Settings dataclass drift apart easily.

        Any "unknown settings field" warning means the template contains a
        field that does not take effect -- changing it and seeing no result is
        the hardest kind of problem to track down.
        """
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is not installed")
        from arcx_auto.config.settings import load_settings

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "default.yaml",
        )
        _settings, warnings = load_settings(path)
        self.assertEqual(warnings, [])

    def test_unknown_config_field_warns_but_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "c.json")
            with open(cfg, "w") as handle:
                json.dump({"nonsense_field": 1}, handle)
            demo = build_demo(os.path.join(tmp, "demo"))
            code, _out, err = run_cli(
                ["-c", cfg, "inspect", "dir-map", demo["dir_map"]])
            self.assertEqual(code, 0)
            self.assertIn("unknown settings field", err)


if __name__ == "__main__":
    unittest.main()


class RootsAreNotTiedToTheWorkingDirectoryTest(unittest.TestCase):
    """Where results land must not depend on where the command was typed.

    A relative run_root means waves created from one directory and a daemon
    started from another never see each other -- and the daemon finds waves by
    scanning run_root, so the symptom is that it silently monitors nothing.
    """

    def test_the_defaults_are_absolute(self):
        from arcx_auto.config.settings import Settings

        settings = Settings()
        for value in (settings.state_root, settings.run_root):
            self.assertTrue(value.startswith("~") or os.path.isabs(value),
                            "%r follows the shell's cwd" % value)

    def test_the_default_run_root_does_not_move_with_the_cwd(self):
        from arcx_auto.config.settings import Settings

        here = os.getcwd()
        try:
            first = Settings().expanded_run_root()
            os.chdir(tempfile.gettempdir())
            self.assertEqual(Settings().expanded_run_root(), first)
        finally:
            os.chdir(here)

    def test_a_relative_root_is_obeyed_but_warned_about(self):
        from arcx_auto.config.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"run_root": "./somewhere"}, handle)
            settings, warnings = load_settings(path)

        self.assertEqual(settings.run_root, "./somewhere")
        self.assertTrue(any("relative path" in w for w in warnings), warnings)

    def test_an_absolute_root_warns_about_nothing(self):
        from arcx_auto.config.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"run_root": "/proj/runs",
                           "state_root": "~/.arcx-auto"}, handle)
            _settings, warnings = load_settings(path)
        self.assertEqual([w for w in warnings if "relative" in w], [])


class LauncherTest(unittest.TestCase):
    """bin/arcx-auto is the whole installation on a machine with no pip."""

    def _launcher(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bin", "arcx-auto")

    def test_it_exists_and_is_executable(self):
        path = self._launcher()
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.access(path, os.X_OK), "not executable")

    def test_it_runs_from_an_unrelated_directory(self):
        import subprocess

        result = subprocess.run(
            [self._launcher(), "--version"],
            cwd=tempfile.gettempdir(), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".", result.stdout.strip())

    def test_it_works_through_a_symlink(self):
        """The link may live on a PATH directory far from the source tree, so
        the script has to resolve itself rather than trust its own path.
        """
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "arcx-auto")
            os.symlink(self._launcher(), link)
            result = subprocess.run(
                [link, "--version"], cwd=tempfile.gettempdir(),
                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
