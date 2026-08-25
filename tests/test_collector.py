"""Collector: mapping LSF jobs back to cases.

The child jobs Arcx submits carry no identifiable job name, but the
`cd <path>` line in cmd_folder/cmd_file_N states the execution path
directly, which is deterministic.
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
        """Method 1: output_file is exactly this case's log -- most reliable."""
        log = os.path.join(self.folder, "submit_bjob_cmd_file_1.log")
        job = LsfJobView(job_id="777", state=LsfState.RUN, output_file=log,
                         exec_cwd=self.folder)
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertEqual(obs.cases["NDIO_1"].lsf.job_id, "777")
        self.assertIsNone(obs.cases["PDIO_1"].lsf)

    def test_match_by_cmd_file_exec_path(self):
        """Method 2: the job cwd sits under the path from the cmd_file."""
        job = LsfJobView(job_id="888", state=LsfState.SSUSP,
                         exec_cwd=os.path.join(self.folder, "PDIO_1"))
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertEqual(obs.cases["PDIO_1"].lsf.job_id, "888")
        self.assertEqual(obs.cases["PDIO_1"].lsf.state, LsfState.SSUSP)
        self.assertIsNone(obs.cases["NDIO_1"].lsf)

    def test_match_by_nested_subdirectory(self):
        """A job running in a subdirectory of the case run dir still counts."""
        job = LsfJobView(job_id="666", state=LsfState.RUN,
                         exec_cwd=os.path.join(self.folder, "NDIO_1", "deep", "x"))
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertEqual(obs.cases["NDIO_1"].lsf.job_id, "666")

    def test_parent_arcx_job_not_attached_to_cases(self):
        """The parent Arcx job, which runs in the index run folder, must not

        Its cwd is an ancestor of every case dir, so a bidirectional prefix
        match would attach it to all of them and the whole table would show
        one job -- worse than matching nothing.
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
        """/work/wave_0011 is not under /work/wave_001: prefix matching has
        to be path aware.
        """
        job = LsfJobView(job_id="123", state=LsfState.RUN,
                         exec_cwd=self.folder + "1/NDIO_1")
        obs = self.collector.collect_index_run(self.folder, "1000", lsf_jobs=[job])
        self.assertIsNone(obs.cases["NDIO_1"].lsf)

    def test_a_truncated_output_file_is_not_matched_by_equality(self):
        """bjobs cut the path short, so it can only ever be a prefix.

        Comparing it for equality fails; matching on the prefix would attach
        the job to whichever case happened to sort first. Not matching is the
        honest answer, and it no longer produces a false LOST.
        """
        log = os.path.join(self.folder, "submit_bjob_cmd_file_1.log")
        job = LsfJobView(job_id="777", state=LsfState.RUN,
                         output_file=log[:-6], truncated=True)
        obs = self.collector.collect_index_run(self.folder, "1000",
                                               lsf_jobs=[job])
        self.assertIsNone(obs.cases["NDIO_1"].lsf)

    def test_a_truncated_cwd_still_matches_its_case(self):
        """The directory match is a prefix comparison already, so a path cut
        short after the case directory still lands on the right case.
        """
        job = LsfJobView(job_id="888", state=LsfState.RUN,
                         exec_cwd=os.path.join(self.folder, "PDIO_1", "wo"),
                         truncated=True)
        obs = self.collector.collect_index_run(self.folder, "1000",
                                               lsf_jobs=[job])
        self.assertEqual(obs.cases["PDIO_1"].lsf.job_id, "888")

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
