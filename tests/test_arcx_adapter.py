"""ArcxAdapter: parsing dir_map and special.cfg.

These tests mirror the real file formats exactly. Misreading a format makes
everything downstream wrong, so the quirks (a missing comma and so on) are
deliberately covered here.
"""

import os
import tempfile
import unittest

from arcx_auto.adapters.arcx import ArcxAdapter
from arcx_auto.config.settings import LsfSettings, PlanSettings
from tests.fixtures.fake_run import make_index_source

REAL_DIR_MAP = '''%dir_map =(
"1000" => "/path/to/index1000/"  ,
"1001" => "/path/to/index1001/" ,
"min" => "1000"
"max" => "1014"
);
return 1 ;
'''


class DirMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = ArcxAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text, name="dir_map"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_parses_real_format(self):
        result = self.adapter.parse_dir_map(self._write(REAL_DIR_MAP))
        self.assertEqual(result.entries, {
            "1000": "/path/to/index1000/",
            "1001": "/path/to/index1001/",
        })

    def test_min_max_are_meta_not_index(self):
        """min and max are reserved keys; treating them as indices would
        hand Arcx an index that does not exist.
        """
        result = self.adapter.parse_dir_map(self._write(REAL_DIR_MAP))
        self.assertNotIn("min", result.entries)
        self.assertNotIn("max", result.entries)
        self.assertEqual(result.meta["min"], "1000")
        self.assertEqual(result.meta["max"], "1014")

    def test_missing_trailing_comma_is_tolerated(self):
        """The real sample has no trailing comma on the "min" line, and
        parsing must not fail because of it.
        """
        result = self.adapter.parse_dir_map(self._write(REAL_DIR_MAP))
        self.assertEqual(len(result.entries), 2)

    def test_warns_when_range_has_gaps(self):
        """min/max declare 1000-1014 but only 2 entries exist -> warn.

        A gap like this is invisible to the eye but makes some indices
        silently never run.
        """
        result = self.adapter.parse_dir_map(self._write(REAL_DIR_MAP))
        self.assertTrue(any("missing" in w for w in result.warnings), result.warnings)

    def test_comments_are_ignored(self):
        text = REAL_DIR_MAP.replace(
            '"1001" => "/path/to/index1001/" ,',
            '# "1001" => "/should/not/appear/" ,',
        )
        result = self.adapter.parse_dir_map(self._write(text))
        self.assertNotIn("1001", result.entries)

    def test_single_quotes(self):
        text = "%dir_map =(\n'2000' => '/a/b/',\n);\n"
        result = self.adapter.parse_dir_map(self._write(text))
        self.assertEqual(result.entries, {"2000": "/a/b/"})

    def test_missing_file_yields_warning_not_exception(self):
        result = self.adapter.parse_dir_map(os.path.join(self.tmp.name, "nope"))
        self.assertEqual(result.entries, {})
        self.assertTrue(result.warnings)

    def test_keys_sorted_numerically(self):
        text = '%dir_map =(\n"9" => "/a",\n"10" => "/b",\n"2" => "/c",\n);\n'
        result = self.adapter.parse_dir_map(self._write(text))
        self.assertEqual(result.keys_sorted(), ["2", "9", "10"])


class SpecialCfgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = ArcxAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_cpu_from_special_cfg(self):
        path = make_index_source(self.tmp.name, "1000", gds_count=5, cpu_per_case=7)
        cpu, warnings, _estimated = self.adapter.read_cpu_per_case(path)
        self.assertEqual(cpu, 7)
        self.assertEqual(warnings, [])

    def test_slots_is_cpu_times_gds_count(self):
        """slots = O_QCAP_LSF_NUM * GDS count -- the core planning formula."""
        path = make_index_source(self.tmp.name, "1000", gds_count=5, cpu_per_case=7)
        spec = self.adapter.build_index_spec("1000", path)
        self.assertEqual(spec.gds_count, 5)
        self.assertEqual(spec.cpu_per_case, 7)
        self.assertEqual(spec.slots, 35)

    def test_missing_special_cfg_marks_index_unusable(self):
        """A missing special.cfg must not be guessed at: exclude and explain."""
        path = os.path.join(self.tmp.name, "broken")
        os.makedirs(path)
        open(os.path.join(path, "a.gds"), "w").close()
        spec = self.adapter.build_index_spec("1004", path)
        self.assertFalse(spec.usable)
        self.assertIsNotNone(spec.error)

    def test_no_gds_marks_index_unusable(self):
        path = make_index_source(self.tmp.name, "1005", gds_count=0, cpu_per_case=4)
        spec = self.adapter.build_index_spec("1005", path)
        self.assertFalse(spec.usable)

    def test_nonexistent_path(self):
        spec = self.adapter.build_index_spec("9999", "/definitely/not/here")
        self.assertFalse(spec.usable)
        self.assertIn("does not exist", spec.error or "")

    def test_priority_keyword_from_path(self):
        adapter = ArcxAdapter(plan=PlanSettings(priority_keywords=["sram", "ro"]))
        path = make_index_source(self.tmp.name, "1000", 2, 4, name_hint="sram_core")
        spec = adapter.build_index_spec("1000", path)
        self.assertIn("sram", spec.keywords)
        self.assertEqual(spec.priority, 1)

    def test_no_keyword_means_normal_priority(self):
        path = make_index_source(self.tmp.name, "1001", 2, 4, name_hint="logic")
        spec = self.adapter.build_index_spec("1001", path)
        self.assertEqual(spec.keywords, ())
        self.assertEqual(spec.priority, 0)


class CommandTest(unittest.TestCase):
    def test_run_command(self):
        adapter = ArcxAdapter()
        argv = adapter.build_run_command("arcx.cfg", ["1000", "1001"],
                                         lsf_settings=LsfSettings())
        self.assertEqual(
            argv,
            ["Arcx", "-p", "arcx.cfg", "-d", "1000", "1001",
             "-lsf0", "-nt", "50", "--run"],
        )

    def test_rerun_command_adds_keep_dir(self):
        """A rerun must carry -keep_dir or finished cases get wiped."""
        adapter = ArcxAdapter()
        argv = adapter.build_run_command("arcx.cfg", ["1000"], rerun=True,
                                         lsf_settings=LsfSettings())
        self.assertIn("-keep_dir", argv)
        self.assertEqual(argv[-1], "--run")


if __name__ == "__main__":
    unittest.main()
