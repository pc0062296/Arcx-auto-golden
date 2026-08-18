"""FsAdapter: run folder 掃描。

對應使用者提供的真實 ls -a:
    .complete.case1 .complete.case2 .complete.case3
    QC_Cc/ QC_Ct/ QC_Spice/
    submit_bjob_cmd_file_1.log ...
    case1/ case2/ case3/
"""

import os
import tempfile
import unittest

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.domain.enums import MarkerKind
from tests.fixtures.fake_run import CaseSpec, make_index_run_folder


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FsAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def test_scans_real_completed_layout(self):
        folder = make_index_run_folder(self.tmp.name, "1000", [
            CaseSpec("case1"), CaseSpec("case2"), CaseSpec("case3"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1000")
        self.assertIsNone(obs.error)
        self.assertEqual(sorted(obs.cases), ["case1", "case2", "case3"])
        for case in obs.cases.values():
            self.assertTrue(case.has_complete_marker)
            self.assertTrue(case.case_dir_exists)
            self.assertIsNotNone(case.log_path)

    def test_qc_dirs_are_reports_not_cases(self):
        """QC_* 是 Arcx 的 report 目錄。誤當成 case 會讓統計整個錯掉。"""
        folder = make_index_run_folder(self.tmp.name, "1000", [CaseSpec("case1")])
        obs = self.fs.scan_index_run_folder(folder, "1000")
        self.assertEqual(obs.report_dirs, ("QC_Cc", "QC_Ct", "QC_Spice"))
        self.assertEqual(list(obs.cases), ["case1"])

    def test_log_filename_maps_to_case(self):
        """submit_bjob_cmd_file_2.log -> case2"""
        folder = make_index_run_folder(self.tmp.name, "1000", [
            CaseSpec("case1"), CaseSpec("case2"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1000")
        self.assertTrue(obs.cases["case2"].log_path.endswith(
            "submit_bjob_cmd_file_2.log"))

    def test_case_discovered_from_dir_alone(self):
        """只有 case dir、沒有 marker 也沒有 log 的 case 必須被看見。

        這代表「case 建立了但從未被提交」—— 目前流程最容易靜默漏掉的失敗。
        """
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("case1"), CaseSpec("case9", "orphan_dir"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertIn("case9", obs.cases)
        self.assertEqual(obs.cases["case9"].markers, frozenset())
        self.assertTrue(obs.cases["case9"].case_dir_exists)

    def test_case_discovered_from_marker_alone(self):
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("case5", "orphan_marker"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertIn("case5", obs.cases)
        self.assertFalse(obs.cases["case5"].case_dir_exists)

    def test_marker_inconsistency_detected(self):
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("case1", "inconsistent"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        case = obs.cases["case1"]
        self.assertTrue(case.has_complete_marker)
        self.assertTrue(case.has_run_marker)
        self.assertTrue(case.marker_inconsistent)

    def test_markers_parsed_by_kind(self):
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("case1", "queued"),
            CaseSpec("case2", "running"),
            CaseSpec("case3", "complete"),
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertEqual(obs.cases["case1"].markers, frozenset({MarkerKind.QUEUE}))
        self.assertEqual(obs.cases["case2"].markers, frozenset({MarkerKind.RUN}))
        self.assertEqual(obs.cases["case3"].markers, frozenset({MarkerKind.COMPLETE}))

    def test_cases_sorted_numerically(self):
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("case%d" % i) for i in (1, 2, 10, 11)
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertEqual(list(obs.cases), ["case1", "case2", "case10", "case11"])

    def test_unknown_entries_are_reported_not_silently_dropped(self):
        """慣例之外的檔案要浮出來 —— 這是發現 Arcx 行為變動的早期訊號。"""
        folder = make_index_run_folder(self.tmp.name, "1001", [CaseSpec("case1")])
        open(os.path.join(folder, "something_new.txt"), "w").close()
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertIn("something_new.txt", obs.unmatched_entries)

    def test_missing_folder_returns_error_not_exception(self):
        obs = self.fs.scan_index_run_folder(os.path.join(self.tmp.name, "nope"))
        self.assertIsNotNone(obs.error)
        self.assertEqual(obs.cases, {})

    def test_list_index_run_folders_skips_hidden(self):
        wave = os.path.join(self.tmp.name, "wave_001")
        make_index_run_folder(wave, "1000", [CaseSpec("case1")])
        make_index_run_folder(wave, "1001", [CaseSpec("case1")])
        os.makedirs(os.path.join(wave, ".arcx_auto"))
        found = self.fs.list_index_run_folders(wave)
        self.assertEqual([k for k, _ in found], ["1000", "1001"])


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
        count, _ = self.fs.count_gds(self.tmp.name)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
