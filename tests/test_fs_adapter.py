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
        """A directory alone does not make a case.

        Case ids are cell names with no shared pattern, so a directory can only
        be recognised by matching a roster built from markers and cmd_files.
        Treating any unrecognised directory as a case is what turned a real
        five case run into seven. It is reported, not counted.
        """
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("NDIO_1", "running"),
            CaseSpec("RES_HI", "orphan_dir"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertNotIn("RES_HI", obs.cases)
        self.assertIn("RES_HI", obs.unexpected_dirs)

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

    def test_an_unknown_dir_needs_no_configuration_to_be_excluded(self):
        """This used to need an exclusion pattern per surprising directory.

        An exclusion list only contains the cases somebody thought of, and the
        ones nobody thought of became phantom cases. Now the roster decides, so
        a directory nobody has ever heard of costs nothing.
        """
        folder = make_index_run_folder(self.tmp.name, "1000",
                                       [CaseSpec("NDIO_1")])
        for name in ("scratch", "QC_Cc", "svdb", "work_calQCAP"):
            os.makedirs(os.path.join(folder, name), exist_ok=True)

        obs = FsAdapter().scan_index_run_folder(folder, "1000")
        self.assertEqual(sorted(obs.cases), ["NDIO_1"])
        for name in ("scratch", "svdb", "work_calQCAP"):
            self.assertIn(name, obs.unexpected_dirs)
        # QC_* is a report directory, which we do know about.
        self.assertIn("QC_Cc", obs.report_dirs)
        self.assertNotIn("QC_Cc", obs.unexpected_dirs)


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


class RealRunFolderTest(unittest.TestCase):
    """The scenario reported from a real run folder.

    Five genuinely complete cases were shown as seven: five FAILED and two
    UNKNOWN. Two separate defects combined to produce that, and each is pinned
    here separately so a fix to one cannot quietly undo the other.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cases = ["NDIO_1", "PDIO_1", "NTN_1", "PTN_1", "CAP_MIM"]
        self.folder = make_index_run_folder(
            self.tmp.name, "1000",
            [CaseSpec(n, "complete", artifacts="full") for n in self.cases])
        # Directories Arcx leaves behind that nobody told the scanner about.
        for name in ("svdb", "work_calQCAP"):
            os.makedirs(os.path.join(self.folder, name), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_five_cases_not_seven(self):
        obs = FsAdapter().scan_index_run_folder(self.folder, "1000")
        self.assertEqual(sorted(obs.cases), sorted(self.cases))
        self.assertEqual(obs.case_count, 5)

    def test_the_two_extra_dirs_are_reported_not_counted(self):
        """Skipped, but never silently: they show in the anomalies table."""
        obs = FsAdapter().scan_index_run_folder(self.folder, "1000")
        self.assertEqual(sorted(obs.unexpected_dirs), ["svdb", "work_calQCAP"])

    def test_the_cfg_snapshot_is_not_an_anomaly(self):
        """zmwu.cfg is an input we read, so reporting it as unclassified would
        be the tool flagging its own source of truth.
        """
        open(os.path.join(self.folder, "zmwu.cfg"), "w").close()
        obs = FsAdapter().scan_index_run_folder(self.folder, "1000")
        self.assertIn("zmwu.cfg", obs.cfg_files)
        self.assertNotIn("zmwu.cfg", obs.unmatched_entries)


class CmdFileRosterTest(unittest.TestCase):
    """cmd_folder/cmd_file_N is the roster: one file, one submitted case."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_cmd(self, folder, num, target):
        cmd_dir = os.path.join(folder, "cmd_folder")
        os.makedirs(cmd_dir, exist_ok=True)
        with open(os.path.join(cmd_dir, "cmd_file_%d" % num), "w",
                  encoding="utf-8") as handle:
            handle.write("#!/bin/csh -f\nsource setup\ncd %s\nArcx ...\n" % target)

    def test_a_submitted_case_with_no_marker_and_no_log_still_appears(self):
        """The cmd_file is read directly rather than reached through the log.

        A case submitted seconds ago has neither a marker nor a log yet. Going
        via the logs would make it invisible for exactly as long as it is most
        worth seeing.
        """
        folder = make_index_run_folder(self.tmp.name, "1002",
                                       [CaseSpec("NDIO_1", "running")])
        self._write_cmd(folder, 9, os.path.join(folder, "FRESH_1"))
        obs = FsAdapter().scan_index_run_folder(folder, "1002")
        self.assertIn("FRESH_1", obs.cases)
        self.assertEqual(obs.cases["FRESH_1"].markers, frozenset())
        self.assertIsNone(obs.cases["FRESH_1"].log_path)
        self.assertIsNotNone(obs.cases["FRESH_1"].cmd_file)

    def test_an_unreadable_cmd_file_is_surfaced(self):
        """A submitted case we cannot name is a case we cannot monitor."""
        folder = make_index_run_folder(self.tmp.name, "1003",
                                       [CaseSpec("NDIO_1", "running")])
        cmd_dir = os.path.join(folder, "cmd_folder")
        with open(os.path.join(cmd_dir, "cmd_file_8"), "w",
                  encoding="utf-8") as handle:
            handle.write("#!/bin/csh -f\n# no cd line at all\n")
        obs = FsAdapter().scan_index_run_folder(folder, "1003")
        self.assertTrue(any("cmd_file_8" in name for name in obs.unresolved_logs))
