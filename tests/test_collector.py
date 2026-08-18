"""Collector: LSF job 對回 case。

Arcx 送出的子 job 沒有可辨識的 job name, 但 cmd_folder/cmd_file_N 裡的
`cd <path>` 直接給出執行路徑, 是確定性的依據。
"""

import os
import tempfile
import unittest

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.config.settings import Settings
from arcx_auto.domain.enums import LsfState
from arcx_auto.domain.models import LsfJobView
from arcx_auto.services.collector import Collector
from tests.fixtures.fake_run import CaseSpec, make_index_run_folder


class AttachLsfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.collector = Collector(
            settings=self.settings,
            fs=FsAdapter(self.settings.layout),
            lsf=LsfAdapter(self.settings.lsf),
        )
        self.folder = make_index_run_folder(self.tmp.name, "1000", [
            CaseSpec("NDIO_1", "running"),
            CaseSpec("PDIO_1", "running"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_match_by_output_file(self):
        """方式 1: output_file 直接等於該 case 的 log —— 最可靠。"""
        log = os.path.join(self.folder, "submit_bjob_cmd_file_1.log")
        job = LsfJobView(job_id="777", state=LsfState.RUN, output_file=log,
                         exec_cwd=self.folder)
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertEqual(obs.cases["NDIO_1"].lsf.job_id, "777")
        self.assertIsNone(obs.cases["PDIO_1"].lsf)

    def test_match_by_cmd_file_exec_path(self):
        """方式 2: job 的 cwd 落在 cmd_file 指出的執行路徑底下。"""
        job = LsfJobView(job_id="888", state=LsfState.SSUSP,
                         exec_cwd=os.path.join(self.folder, "PDIO_1"))
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertEqual(obs.cases["PDIO_1"].lsf.job_id, "888")
        self.assertEqual(obs.cases["PDIO_1"].lsf.state, LsfState.SSUSP)
        self.assertIsNone(obs.cases["NDIO_1"].lsf)

    def test_match_by_nested_subdirectory(self):
        """job 實際跑在 case run dir 的子目錄裡也算。"""
        job = LsfJobView(job_id="666", state=LsfState.RUN,
                         exec_cwd=os.path.join(self.folder, "NDIO_1", "deep", "x"))
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertEqual(obs.cases["NDIO_1"].lsf.job_id, "666")

    def test_parent_arcx_job_not_attached_to_cases(self):
        """在 index run folder 執行的 parent Arcx job 不能被掛到任何 case 上。

        它的 cwd 是所有 case 目錄的上層; 若用雙向前綴比對就會被掛到每一個 case,
        讓整張狀態表顯示同一個 job —— 比對不到還糟。
        """
        parent = LsfJobView(job_id="555", state=LsfState.RUN,
                            exec_cwd=self.folder)
        obs = self.collector.collect_index_run(self.folder, "1000",
                                               lsf_jobs=[parent])
        self.assertIsNone(obs.cases["NDIO_1"].lsf)
        self.assertIsNone(obs.cases["PDIO_1"].lsf)

    def test_job_outside_run_folder_ignored(self):
        job = LsfJobView(job_id="999", state=LsfState.RUN,
                         exec_cwd="/somewhere/else/NDIO_1")
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertIsNone(obs.cases["NDIO_1"].lsf)

    def test_sibling_wave_job_not_matched(self):
        """/work/wave_0011 不屬於 /work/wave_001 —— 前綴比對必須是路徑感知的。"""
        job = LsfJobView(job_id="123", state=LsfState.RUN,
                         exec_cwd=self.folder + "1/NDIO_1")
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertIsNone(obs.cases["NDIO_1"].lsf)

    def test_no_jobs_is_harmless(self):
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[])
        self.assertEqual(len(obs.cases), 2)
        self.assertIsNone(obs.cases["NDIO_1"].lsf)


class CollectWaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.collector = Collector(settings=Settings())

    def tearDown(self):
        self.tmp.cleanup()

    def test_collects_all_index_run_folders(self):
        wave = os.path.join(self.tmp.name, "wave_001")
        make_index_run_folder(wave, "1000", [CaseSpec("NDIO_1")])
        make_index_run_folder(wave, "1001", [CaseSpec("NDIO_1"),
                                             CaseSpec("PDIO_1")])
        os.makedirs(os.path.join(wave, ".arcx_auto"))

        results = self.collector.collect_wave(wave)
        self.assertEqual([r.index_key for r in results], ["1000", "1001"])
        self.assertEqual(sum(r.case_count for r in results), 3)

    def test_empty_wave_dir(self):
        wave = os.path.join(self.tmp.name, "empty")
        os.makedirs(wave)
        self.assertEqual(self.collector.collect_wave(wave), [])


if __name__ == "__main__":
    unittest.main()
