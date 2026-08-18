"""FsAdapter: scanning a run folder.

Mirrors the real ls -a:
    .queue.NDIO_1  .run.PDIO_1  .complete.NTN_1
    NDIO_1/  PDIO_1/  NTN_1/
    QC_Cc/  QC_Ct/  QC_Spice/
    submit_bjob_cmd_file_1.log ...
    cmd_folder/cmd_file_1 ...
"""

import os
import tempfile
import unittest

from arcx_auto.adapters.fs import FsAdapter, natural_key
from arcx_auto.config.settings import LayoutSettings
from arcx_auto.domain.enums import MarkerKind
from tests.fixtures.fake_run import CaseSpec, make_index_run_folder


class RealLayoutTest(unittest.TestCase):
    """A direct mirror of the real ls -a."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FsAdapter()
        self.folder = make_index_run_folder(self.tmp.name, "1000", [
            CaseSpec("NDIO_1", "queued"),
            CaseSpec("PDIO_1", "running"),
            CaseSpec("NTN_1", "complete"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_all_three_cases_by_cell_name(self):
        obs = self.fs.scan_index_run_folder(self.folder, "1000")
        self.assertIsNone(obs.error)
        self.assertEqual(sorted(obs.cases), ["NDIO_1", "NTN_1", "PDIO_1"])

    def test_markers_parsed_per_case(self):
        obs = self.fs.scan_index_run_folder(self.folder, "1000")
        self.assertEqual(obs.cases["NDIO_1"].markers, frozenset({MarkerKind.QUEUE}))
        self.assertEqual(obs.cases["PDIO_1"].markers, frozenset({MarkerKind.RUN}))
        self.assertEqual(obs.cases["NTN_1"].markers, frozenset({MarkerKind.COMPLETE}))

    def test_case_run_dir_located(self):
        """These are exactly the directories a rerun deletes."""
        obs = self.fs.scan_index_run_folder(self.folder, "1000")
        for case_id in ("NDIO_1", "PDIO_1", "NTN_1"):
            case = obs.cases[case_id]
            self.assertTrue(case.case_dir_exists, case_id)
            self.assertEqual(os.path.basename(case.case_dir), case_id)

    def test_qc_dirs_are_reports_not_cases(self):
        obs = self.fs.scan_index_run_folder(self.folder, "1000")
        self.assertEqual(obs.report_dirs, ("QC_Cc", "QC_Ct", "QC_Spice"))
        self.assertNotIn("QC_Cc", obs.cases)

    def test_cmd_folder_is_not_a_case(self):
        obs = self.fs.scan_index_run_folder(self.folder, "1000")
        self.assertNotIn("cmd_folder", obs.cases)
        self.assertNotIn("cmd_folder", obs.unmatched_entries)

    def test_hidden_dirs_are_not_cases(self):
        """Our own .arcx_auto/ must not be mistaken for a case."""
        os.makedirs(os.path.join(self.folder, ".arcx_auto"))
        obs = self.fs.scan_index_run_folder(self.folder, "1000")
        self.assertNotIn(".arcx_auto", obs.cases)


class CmdFileMappingTest(unittest.TestCase):
    """The log -> cmd_file -> case mapping.

    A log filename carries only a sequence number and says nothing about its
    case, so this chain is the only reliable mapping.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FsAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_mapped_to_case_via_cmd_file(self):
        folder = make_index_run_folder(self.tmp.name, "1000", [
            CaseSpec("NDIO_1", "running"),
            CaseSpec("PDIO_1", "running"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1000")
        self.assertTrue(obs.cases["NDIO_1"].log_path.endswith(
            "submit_bjob_cmd_file_1.log"))
        self.assertTrue(obs.cases["PDIO_1"].log_path.endswith(
            "submit_bjob_cmd_file_2.log"))

    def test_numbering_order_is_not_assumed(self):
        """Sequence order is unrelated to case name order: nothing may guess
        the case from the number.

        Here cmd_file_1 points at ZZZ_LAST and cmd_file_2 at AAA_FIRST. Any
        shortcut of the form "the Nth log belongs to the Nth case once sorted"
        fails this test.
        """
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("ZZZ_LAST", "running"),
            CaseSpec("AAA_FIRST", "running"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertTrue(obs.cases["ZZZ_LAST"].log_path.endswith("_1.log"))
        self.assertTrue(obs.cases["AAA_FIRST"].log_path.endswith("_2.log"))

    def test_exec_path_recorded(self):
        folder = make_index_run_folder(self.tmp.name, "1000", [
            CaseSpec("NDIO_1", "running"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1000")
        case = obs.cases["NDIO_1"]
        self.assertEqual(case.exec_path, os.path.join(folder, "NDIO_1"))
        self.assertTrue(case.cmd_file.endswith("cmd_folder/cmd_file_1"))

    def test_unresolvable_log_is_reported(self):
        """A log with no cmd_file means a case we cannot monitor, which has
        to be visible.
        """
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("DIODE_X", "no_cmd_file"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertIn("submit_bjob_cmd_file_1.log", obs.unresolved_logs)

    def test_cd_parsing_variants(self):
        cases = [
            ("cd /a/b/CELL_1\n", "/a/b/CELL_1"),
            ("   cd    /a/b/CELL_1   \n", "/a/b/CELL_1"),
            ('cd "/a/b/CELL_1"\n', "/a/b/CELL_1"),
            ("cd /a/b/CELL_1/\n", "/a/b/CELL_1"),
        ]
        for i, (line, expected) in enumerate(cases):
            path = os.path.join(self.tmp.name, "cmd_%d" % i)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/csh -f\nsource x\n" + line + "tool run\n")
            self.assertEqual(self.fs.read_cmd_exec_path(path), expected, line)

    def test_relative_cd_ignored(self):
        """A relative cd cannot identify the case and must not be guessed at."""
        path = os.path.join(self.tmp.name, "cmd_rel")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/csh -f\ncd ../somewhere\n")
        self.assertIsNone(self.fs.read_cmd_exec_path(path))

    def test_cd_inside_run_folder_preferred(self):
        """With several cd lines, take the one under the run folder."""
        run_folder = os.path.join(self.tmp.name, "run")
        os.makedirs(run_folder)
        path = os.path.join(self.tmp.name, "cmd_multi")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/csh -f\n"
                "cd /opt/tools/setup\n"
                "cd %s/CELL_A\n"
                "cd /var/tmp/scratch\n" % run_folder
            )
        self.assertEqual(
            self.fs.read_cmd_exec_path(path, run_folder),
            os.path.join(run_folder, "CELL_A"),
        )

    def test_missing_cmd_file_returns_none(self):
        self.assertIsNone(
            self.fs.read_cmd_exec_path(os.path.join(self.tmp.name, "nope")))


class DiscoveryUnionTest(unittest.TestCase):
    """Cases come from the union of markers, run dirs and logs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FsAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def test_case_discovered_from_dir_alone(self):
        """A run dir with no marker and no log.

        It means the case exists but was never submitted, the failure the
        current process misses most easily.
        """
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("NDIO_1", "running"),
            CaseSpec("RES_HI", "orphan_dir"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertIn("RES_HI", obs.cases)
        self.assertEqual(obs.cases["RES_HI"].markers, frozenset())
        self.assertTrue(obs.cases["RES_HI"].case_dir_exists)
        self.assertIsNone(obs.cases["RES_HI"].log_path)

    def test_case_discovered_from_marker_alone(self):
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("CAP_MIM", "orphan_marker"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertIn("CAP_MIM", obs.cases)
        self.assertFalse(obs.cases["CAP_MIM"].case_dir_exists)

    def test_marker_inconsistency_detected(self):
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("NMOS_1", "inconsistent"),
        ])
        case = self.fs.scan_index_run_folder(folder, "1001").cases["NMOS_1"]
        self.assertTrue(case.has_complete_marker)
        self.assertTrue(case.has_run_marker)
        self.assertTrue(case.marker_inconsistent)

    def test_cases_sorted_naturally(self):
        """NDIO_2 has to sort before NDIO_10."""
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("NDIO_%d" % i, "complete") for i in (1, 2, 10, 11)
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertEqual(list(obs.cases),
                         ["NDIO_1", "NDIO_2", "NDIO_10", "NDIO_11"])

    def test_unknown_marker_is_flagged_separately(self):
        """An unknown marker such as .fail.X is abnormal and must be flagged
        separately rather than mixed into the noise.
        """
        folder = make_index_run_folder(self.tmp.name, "1001",
                                       [CaseSpec("NDIO_1", "running")])
        open(os.path.join(folder, ".fail.NDIO_9"), "w").close()
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertEqual(obs.unknown_markers, ((".fail.NDIO_9", "NDIO_9"),))
        self.assertNotIn(".fail.NDIO_9", obs.unmatched_entries)

    def test_unknown_marker_still_reveals_the_case(self):
        """An unknown marker still proves the case exists; it must not vanish
        from the case list.
        """
        folder = make_index_run_folder(self.tmp.name, "1001",
                                       [CaseSpec("NDIO_1", "running")])
        open(os.path.join(folder, ".fail.NDIO_9"), "w").close()
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertIn("NDIO_9", obs.cases)
        self.assertEqual(obs.cases["NDIO_9"].markers, frozenset())

    def test_known_markers_not_reported_as_unknown(self):
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("NDIO_1", "queued"),
            CaseSpec("PDIO_1", "running"),
            CaseSpec("NTN_1", "complete"),
        ])
        self.assertEqual(
            self.fs.scan_index_run_folder(folder, "1001").unknown_markers, ())

    def test_unknown_files_are_reported(self):
        """Files outside the conventions must surface: they are the early
        signal that Arcx behaviour changed.
        """
        folder = make_index_run_folder(self.tmp.name, "1001",
                                       [CaseSpec("NDIO_1")])
        open(os.path.join(folder, "something_new.txt"), "w").close()
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertIn("something_new.txt", obs.unmatched_entries)

    def test_missing_folder_returns_error_not_exception(self):
        obs = self.fs.scan_index_run_folder(os.path.join(self.tmp.name, "nope"))
        self.assertIsNotNone(obs.error)
        self.assertEqual(obs.cases, {})

    def test_list_index_run_folders_skips_hidden(self):
        wave = os.path.join(self.tmp.name, "wave_001")
        make_index_run_folder(wave, "1000", [CaseSpec("NDIO_1")])
        make_index_run_folder(wave, "1001", [CaseSpec("NDIO_1")])
        os.makedirs(os.path.join(wave, ".arcx_auto"))
        found = self.fs.list_index_run_folders(wave)
        self.assertEqual([k for k, _ in found], ["1000", "1001"])


class ConfigurableLayoutTest(unittest.TestCase):
    """Every convention is configurable, so a change in Arcx or in the
    environment is a settings change rather than a code change.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_extra_non_case_dir_can_be_excluded(self):
        layout = LayoutSettings()
        layout.non_case_dir_regexes = layout.non_case_dir_regexes + [r"^scratch$"]
        folder = make_index_run_folder(self.tmp.name, "1000",
                                       [CaseSpec("NDIO_1")])
        os.makedirs(os.path.join(folder, "scratch"))

        default_obs = FsAdapter().scan_index_run_folder(folder, "1000")
        self.assertIn("scratch", default_obs.cases)

        tuned_obs = FsAdapter(layout).scan_index_run_folder(folder, "1000")
        self.assertNotIn("scratch", tuned_obs.cases)


class GdsCountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FsAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_gds_variants_once_each(self):
        for name in ("a.gds", "b.gds.gz", "c.GDS", "notes.txt", "d.oas"):
            open(os.path.join(self.tmp.name, name), "w").close()
        count, names = self.fs.count_gds(self.tmp.name)
        self.assertEqual(count, 3)
        self.assertEqual(names, ["a.gds", "b.gds.gz", "c.GDS"])

    def test_directories_are_not_counted(self):
        os.makedirs(os.path.join(self.tmp.name, "sub.gds"))
        self.assertEqual(self.fs.count_gds(self.tmp.name)[0], 0)


class NaturalKeyTest(unittest.TestCase):
    def test_numeric_chunks_compare_numerically(self):
        names = ["NDIO_10", "NDIO_2", "NDIO_1", "PDIO_1", "NTN_1"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["NDIO_1", "NDIO_2", "NDIO_10", "NTN_1", "PDIO_1"],
        )

    def test_pure_text_names(self):
        self.assertEqual(sorted(["b", "a"], key=natural_key), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
