"""LsfAdapter: parsing bjobs / busers output, and degrading gracefully."""

import unittest

from arcx_auto.adapters.lsf import LsfAdapter, parse_jobs_in_path
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


class BjobsTruncationTest(unittest.TestCase):
    """A path bjobs cut short matches nothing, and looks like a missing job.

    This is a false alarm produced entirely by a display width, which is the
    worst kind: nothing is wrong, nothing reports an error, and the case says
    its job has disappeared.
    """

    def test_the_path_columns_are_given_a_width(self):
        spec = LsfAdapter()._bjobs_format()
        self.assertIn("exec_cwd:512", spec)
        self.assertIn("output_file:512", spec)
        self.assertIn("jobid", spec)
        self.assertNotIn("jobid:", spec)

    def test_the_width_is_configurable(self):
        from arcx_auto.config.settings import LsfSettings

        settings = LsfSettings()
        settings.bjobs_path_width = 64
        self.assertIn("sub_cwd:64", LsfAdapter(settings)._bjobs_format())

    def test_a_width_of_zero_asks_for_no_widths_at_all(self):
        from arcx_auto.config.settings import LsfSettings

        settings = LsfSettings()
        settings.bjobs_path_width = 0
        spec = LsfAdapter(settings)._bjobs_format()
        self.assertNotIn(":", spec)

    def test_a_truncated_path_is_marked_and_the_marker_removed(self):
        jobs = LsfAdapter._parse_bjobs(
            "104 RUN /work/run/wave_001/1000/ca* /work/sub - host01 j\n")
        self.assertEqual(jobs[0].exec_cwd, "/work/run/wave_001/1000/ca")
        self.assertTrue(jobs[0].truncated)

    def test_an_untruncated_line_is_not_marked(self):
        jobs = LsfAdapter._parse_bjobs(BJOBS_OUTPUT)
        self.assertFalse(any(j.truncated for j in jobs))

    def test_a_truncated_path_still_says_which_wave_it_belongs_to(self):
        """Shortened, it is still a prefix -- enough for ownership, never
        enough for equality.
        """
        job = LsfJobView(job_id="1", state=LsfState.RUN,
                         exec_cwd="/work/run/wave_001/1000/ca", truncated=True)
        self.assertTrue(job.belongs_to("/work/run/wave_001"))


class BusersParseTest(unittest.TestCase):
    def test_njobs_column_located_by_header(self):
        """Locate the column by name, not by position: the busers column
        order varies between versions.
        """
        adapter = LsfAdapter()
        lines = [ln for ln in BUSERS_OUTPUT.splitlines() if ln.strip()]
        header = lines[0].split()
        self.assertEqual(header.index("NJOBS"), 3)
        self.assertEqual(int(lines[1].split()[3]), 42)


class BelongsToTest(unittest.TestCase):
    """The wave directory is the ownership boundary for LSF jobs."""

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
        """/work/run/wave_0011 is not under /work/run/wave_001."""
        job = LsfJobView(job_id="1", state=LsfState.RUN,
                         exec_cwd="/work/run/wave_0011/1000")
        self.assertFalse(job.belongs_to("/work/run/wave_001"))


class StateSemanticsTest(unittest.TestCase):
    def test_suspended_states(self):
        for state in (LsfState.PSUSP, LsfState.USUSP, LsfState.SSUSP):
            self.assertTrue(state.is_suspended)
        self.assertFalse(LsfState.RUN.is_suspended)

    def test_active_states_include_suspended(self):
        """A suspended job still holds resources and is not finished."""
        self.assertTrue(LsfState.SSUSP.is_active)
        self.assertTrue(LsfState.PEND.is_active)
        self.assertFalse(LsfState.DONE.is_active)
        self.assertFalse(LsfState.EXIT.is_active)


class DegradationTest(unittest.TestCase):
    """When LSF is unavailable the adapter degrades instead of raising:
    monitoring must not stop because bjobs had a bad moment.
    """

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


class JobsInPathTest(unittest.TestCase):
    """bjobs_manage.py -jp reports a count, not a job list.

        grep all jobs...
        finished, total 304 jobs      <- every job
        total 299 jobs in path        <- scoped to the path, this one

    The distinction matters: this feeds the drain safety gate, and treating
    "could not tell" as "no jobs left" would delete files while jobs are still
    running -- the most destructive mistake this system could make.
    """

    REAL_OUTPUT = ("grep all jobs...\n"
                   "finished, total 304 jobs\n"
                   "total 299 jobs in path\n")

    def test_parses_the_in_path_count(self):
        self.assertEqual(parse_jobs_in_path(self.REAL_OUTPUT), 299)

    def test_does_not_pick_the_total_count(self):
        """304 is every job on the cluster; 299 is the one we need."""
        self.assertNotEqual(parse_jobs_in_path(self.REAL_OUTPUT), 304)

    def test_zero_is_a_real_answer(self):
        self.assertEqual(parse_jobs_in_path(
            "grep all jobs...\nfinished, total 4 jobs\ntotal 0 jobs in path\n"), 0)

    def test_unrecognised_output_is_none_not_zero(self):
        """None means unknown. Never confuse it with zero."""
        self.assertIsNone(parse_jobs_in_path("something unexpected"))
        self.assertIsNone(parse_jobs_in_path(""))

    def test_singular_wording_accepted(self):
        self.assertEqual(parse_jobs_in_path("total 1 job in path"), 1)


if __name__ == "__main__":
    unittest.main()
