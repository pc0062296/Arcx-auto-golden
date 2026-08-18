"""FsAdapter: run folder 掃描。

對應使用者提供的真實 ls -a:
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
    """直接對應使用者給的真實 ls -a。"""

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
        """rerun 時要刪的就是這些目錄。"""
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
        """我們自己的 .arcx_auto/ 不能被當成 case。"""
        os.makedirs(os.path.join(self.folder, ".arcx_auto"))
        obs = self.fs.scan_index_run_folder(self.folder, "1000")
        self.assertNotIn(".arcx_auto", obs.cases)


class CmdFileMappingTest(unittest.TestCase):
    """log -> cmd_file -> case 的對應。

    log 檔名只有流水號, 與 case 沒有任何關係, 所以這條鏈是唯一可靠的對應方式。
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
        """編號順序與 case 名稱排序無關 —— 系統不能偷偷用編號猜。

        這裡 cmd_file_1 指向 ZZZ_LAST, cmd_file_2 指向 AAA_FIRST。
        若實作用了任何「第 N 個 log 對第 N 個 case (排序後)」的捷徑,
        這個測試就會失敗。
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
        """有 log 但 cmd_file 缺失 -> 有一個 case 我們監控不到, 必須讓人看到。"""
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
        """相對路徑的 cd 無法判定 case, 不能拿來猜。"""
        path = os.path.join(self.tmp.name, "cmd_rel")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/csh -f\ncd ../somewhere\n")
        self.assertIsNone(self.fs.read_cmd_exec_path(path))

    def test_cd_inside_run_folder_preferred(self):
        """script 有多個 cd 時, 採用位於 run folder 底下的那一個。"""
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
    """case 來源取 marker / run dir / log 三者的聯集。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FsAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    def test_case_discovered_from_dir_alone(self):
        """只有 run dir、沒有 marker 也沒有 log。

        代表「case 建立了但從未被提交」—— 目前流程最容易靜默漏掉的失敗。
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
        """NDIO_2 必須排在 NDIO_10 前面。"""
        folder = make_index_run_folder(self.tmp.name, "1001", [
            CaseSpec("NDIO_%d" % i, "complete") for i in (1, 2, 10, 11)
        ])
        obs = self.fs.scan_index_run_folder(folder, "1001")
        self.assertEqual(list(obs.cases),
                         ["NDIO_1", "NDIO_2", "NDIO_10", "NDIO_11"])

    def test_unknown_files_are_reported(self):
        """慣例之外的檔案要浮出來 —— 發現 Arcx 行為變動的早期訊號。"""
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
    """慣例全部可設定 —— Arcx 或環境有變動時只改設定, 不動程式。"""

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
