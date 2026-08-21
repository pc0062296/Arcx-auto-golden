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
        # --no-keep-folders: the demo's index sources share one parent
        # directory, so with folder grouping on there is one block and the
        # order inside it is the selection order. That behaviour has its own
        # tests; this one is about the keyword priority.
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--max-slots", "1000", "--no-keep-folders", "--json"])
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


class RootsTest(unittest.TestCase):
    """state_root and run_root are different kinds of thing.

    run_root is the work. One per project directory is the normal way to use
    this tool, and a relative ./arcx_runs is how somebody says so -- which
    makes "where the command was typed" meaningful rather than a hazard.

    state_root is the tool's own memory: the command queue, the drafts, the
    daemon locks. There has to be exactly one per person, or a request queued
    in one directory is invisible to the daemon started in another.
    """

    def test_state_root_does_not_move_with_the_cwd(self):
        from arcx_auto.config.settings import Settings

        here = os.getcwd()
        try:
            first = Settings().expanded_state_root()
            os.chdir(tempfile.gettempdir())
            self.assertEqual(Settings().expanded_state_root(), first)
        finally:
            os.chdir(here)

    def test_run_root_is_meant_to_follow_the_working_directory(self):
        from arcx_auto.config.settings import Settings

        here = os.getcwd()
        try:
            first = Settings().expanded_run_root()
            os.chdir(tempfile.gettempdir())
            self.assertNotEqual(Settings().expanded_run_root(), first)
        finally:
            os.chdir(here)

    def test_a_relative_state_root_is_obeyed_but_warned_about(self):
        from arcx_auto.config.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"state_root": "./somewhere"}, handle)
            settings, warnings = load_settings(path)

        self.assertEqual(settings.state_root, "./somewhere")
        self.assertTrue(any("relative path" in w for w in warnings), warnings)

    def test_a_relative_run_root_warns_about_nothing(self):
        """It is the supported workspace model, not a mistake."""
        from arcx_auto.config.settings import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"run_root": "./arcx_runs"}, handle)
            _settings, warnings = load_settings(path)
        self.assertEqual([w for w in warnings if "relative" in w], [])


class ShippedConfigTest(unittest.TestCase):
    """config/default.yaml must say exactly what the code already does.

    It is copied into ~/.arcx-auto/config/ as the starting point, so every
    value in it silently overrides a code default from then on. When the two
    drift, changing a default in the code changes nothing for anybody who
    followed the setup instructions -- which is how default_cpu_per_case
    stayed at 0 for a week after it was raised to 4, quietly excluding every
    index whose special.cfg could not be read.
    """

    def test_the_shipped_config_matches_the_code_defaults(self):
        from dataclasses import fields, is_dataclass

        from arcx_auto.config.settings import Settings, load_settings

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, "config", "default.yaml")
        self.assertTrue(os.path.isfile(path), path)

        loaded, warnings = load_settings(path)
        self.assertEqual([w for w in warnings if "unknown" in w], [])

        differences = []

        def compare(left, right, prefix=""):
            for spec in fields(left):
                if spec.name == "source_path":
                    continue
                where = "%s.%s" % (prefix, spec.name) if prefix else spec.name
                mine = getattr(left, spec.name)
                theirs = getattr(right, spec.name)
                if is_dataclass(mine) and not isinstance(mine, type):
                    compare(mine, theirs, where)
                elif mine != theirs:
                    differences.append("%s: file=%r code=%r"
                                       % (where, mine, theirs))

        compare(loaded, Settings())
        self.assertEqual(differences, [],
                         "config/default.yaml has drifted from the code "
                         "defaults:\n  " + "\n  ".join(differences))


if __name__ == "__main__":
    unittest.main()
