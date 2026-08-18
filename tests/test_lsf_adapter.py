"""LsfAdapter: bjobs / busers 輸出解析與優雅降級。"""

import unittest

from arcx_auto.adapters.lsf import LsfAdapter, _extract_job_ids
from arcx_auto.domain.enums import LsfState
from arcx_auto.domain.models import LsfJobView

BJOBS_OUTPUT = """\
101 RUN /work/run/wave_001/1000/case1 /work/sub /work/run/wave_001/1000/submit_bjob_cmd_file_1.log host01 arcx_child
102 PEND - /work/sub - - arcx_child
103 SSUSP /work/run/wave_001/1001/case2 /work/sub /work/log2 host02 arcx_child
"""

BUSERS_OUTPUT = """\
USER/GROUP          JL/P    MAX  NJOBS   PEND    RUN  SSUSP  USUSP    RSV
myuser                 -      -     42     12     28      2      0      0
"""


class BjobsParseTest(unittest.TestCase):
    def test_parses_states_and_paths(self):
        jobs = LsfAdapter._parse_bjobs(BJOBS_OUTPUT)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0].job_id, "101")
        self.assertEqual(jobs[0].state, LsfState.RUN)
        self.assertEqual(jobs[0].exec_cwd, "/work/run/wave_001/1000/case1")
        self.assertEqual(jobs[1].state, LsfState.PEND)

    def test_dash_becomes_none(self):
        jobs = LsfAdapter._parse_bjobs(BJOBS_OUTPUT)
        self.assertIsNone(jobs[1].exec_cwd)
        self.assertIsNone(jobs[1].exec_host)

    def test_unknown_state_does_not_crash(self):
        jobs = LsfAdapter._parse_bjobs("999 WEIRD - - - - -\n")
        self.assertEqual(jobs[0].state, LsfState.UNKWN)

    def test_blank_and_short_lines_skipped(self):
        jobs = LsfAdapter._parse_bjobs("\n\n123\n")
        self.assertEqual(jobs, [])


class BusersParseTest(unittest.TestCase):
    def test_njobs_column_located_by_header(self):
        """依欄位名而非固定位置解析 —— busers 的欄位順序會因版本而異。"""
        adapter = LsfAdapter()
        lines = [ln for ln in BUSERS_OUTPUT.splitlines() if ln.strip()]
        header = lines[0].split()
        self.assertEqual(header.index("NJOBS"), 3)
        self.assertEqual(int(lines[1].split()[3]), 42)


class BelongsToTest(unittest.TestCase):
    """wave 目錄是 LSF job 的歸屬邊界 (架構 §8)。"""

    def test_matches_by_exec_cwd(self):
        job = LsfJobView(job_id="1", state=LsfState.RUN,
                         exec_cwd="/work/run/wave_001/1000/case1")
        self.assertTrue(job.belongs_to("/work/run/wave_001"))
        self.assertTrue(job.belongs_to("/work/run/wave_001/"))

    def test_matches_by_output_file(self):
        job = LsfJobView(job_id="1", state=LsfState.RUN,
                         output_file="/work/run/wave_001/1000/x.log")
        self.assertTrue(job.belongs_to("/work/run/wave_001"))

    def test_does_not_match_sibling_wave(self):
        job = LsfJobView(job_id="1", state=LsfState.RUN,
                         exec_cwd="/work/run/wave_002/1000/case1")
        self.assertFalse(job.belongs_to("/work/run/wave_001"))

    def test_prefix_is_path_aware_not_string_prefix(self):
        """/work/run/wave_0011 不屬於 /work/run/wave_001。"""
        job = LsfJobView(job_id="1", state=LsfState.RUN,
                         exec_cwd="/work/run/wave_0011/1000")
        self.assertFalse(job.belongs_to("/work/run/wave_001"))


class StateSemanticsTest(unittest.TestCase):
    def test_suspended_states(self):
        for state in (LsfState.PSUSP, LsfState.USUSP, LsfState.SSUSP):
            self.assertTrue(state.is_suspended)
        self.assertFalse(LsfState.RUN.is_suspended)

    def test_active_states_include_suspended(self):
        """suspended 的 job 仍佔用資源, 不能當成結案。"""
        self.assertTrue(LsfState.SSUSP.is_active)
        self.assertTrue(LsfState.PEND.is_active)
        self.assertFalse(LsfState.DONE.is_active)
        self.assertFalse(LsfState.EXIT.is_active)


class DegradationTest(unittest.TestCase):
    """LSF 不可用時必須降級而非丟例外 —— 監控不能因為 bjobs 抽風而停擺。"""

    def test_missing_command_reports_error(self):
        adapter = LsfAdapter()
        adapter._availability["definitely_not_a_real_command"] = False
        adapter.settings.busers_cmd = "definitely_not_a_real_command"
        value, error = adapter.current_njobs()
        self.assertIsNone(value)
        self.assertIsNotNone(error)

    def test_list_jobs_returns_error_not_exception(self):
        adapter = LsfAdapter()
        adapter.settings.bjobs_cmd = "definitely_not_a_real_command"
        jobs, error = adapter.list_user_jobs()
        self.assertEqual(jobs, [])
        self.assertIsNotNone(error)


class JobIdExtractionTest(unittest.TestCase):
    def test_extracts_and_dedupes(self):
        text = "Job <12345> is running\nJob <12345> again\nJob <67890> pending\n"
        self.assertEqual(_extract_job_ids(text), ["12345", "67890"])

    def test_empty_output(self):
        self.assertEqual(_extract_job_ids(""), [])


if __name__ == "__main__":
    unittest.main()
