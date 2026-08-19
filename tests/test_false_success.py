"""Phase 3.5: the two checks that catch a run which succeeded on paper.

Everything here describes the same failure. The job finished, the marker is
there, the report exists and is not empty, every artifact is the right size --
and the result is still wrong. Existence and size checks pass all of it, which
is why these two read content instead.
"""

import os
import tempfile
import unittest

from arcx_auto.adapters.arcx_cfg import parse_arcx_cfg
from arcx_auto.config.settings import Settings
from arcx_auto.domain.enums import CaseState, Severity
from arcx_auto.domain.models import IndexSource
from arcx_auto.services.qa.summary_table import (
    find_bad_values,
    parse_summary_table,
)
from tests.fixtures.fake_run import (
    CaseSpec,
    make_arcx_cfg,
    make_index_run_folder,
    make_report_dirs,
    make_summary_table,
)


# ---------------------------------------------------------------------------
# The table parser, with no filesystem involved
# ---------------------------------------------------------------------------

CLEAN = """\
input report: /path/a
input report: /path/b

refReport = /path/a
rep item refReport cmpReport1 diffCmp1
cell_a total_cap 1.234 1.240 0.006
cell_b total_cap 2.000 2.010 0.010
########
"""


class ParseSummaryTableTest(unittest.TestCase):
    def test_parses_the_table_after_the_ref_line(self):
        table = parse_summary_table(CLEAN)
        self.assertTrue(table.found)
        self.assertEqual(table.ref_report, "/path/a")
        self.assertEqual(
            table.header,
            ("rep", "item", "refReport", "cmpReport1", "diffCmp1"))
        self.assertEqual(len(table.rows), 2)

    def test_the_input_report_preamble_is_not_part_of_the_table(self):
        table = parse_summary_table(CLEAN)
        self.assertNotIn("input", " ".join(table.header))

    def test_width_comes_from_the_header_not_from_an_expectation(self):
        """cmpReport2, cmpReport3 and their diffs may or may not be there."""
        text = CLEAN.replace(
            "rep item refReport cmpReport1 diffCmp1",
            "rep item refReport cmpReport1 diffCmp1 cmpReport2 diffCmp2",
        ).replace(
            "cell_a total_cap 1.234 1.240 0.006",
            "cell_a total_cap 1.234 1.240 0.006 1.250 0.016",
        ).replace(
            "cell_b total_cap 2.000 2.010 0.010",
            "cell_b total_cap 2.000 2.010 0.010 2.020 0.020",
        )
        table = parse_summary_table(text)
        self.assertEqual(table.value_columns(2),
                         ("refReport", "cmpReport1", "diffCmp1",
                          "cmpReport2", "diffCmp2"))
        self.assertEqual(find_bad_values(table), [])

    def test_the_end_marker_stops_the_table(self):
        table = parse_summary_table(CLEAN + "trailing junk here\n")
        self.assertEqual(len(table.rows), 2)

    def test_a_blank_line_stops_the_table(self):
        text = CLEAN.replace("########", "")
        table = parse_summary_table(text + "\nsomething else\n")
        self.assertEqual(len(table.rows), 2)

    def test_no_ref_line_is_reported_not_guessed_at(self):
        table = parse_summary_table("input report: /path/a\n########\n")
        self.assertFalse(table.found)
        self.assertIn("refReport", table.error)

    def test_a_header_with_no_rows_is_still_a_table(self):
        table = parse_summary_table(
            "refReport = /a\nrep item refReport\n########\n")
        self.assertTrue(table.found)
        self.assertEqual(table.rows, ())


class FindBadValuesTest(unittest.TestCase):
    def _bad(self, row_line, **kwargs):
        text = CLEAN.replace("cell_b total_cap 2.000 2.010 0.010", row_line)
        return find_bad_values(parse_summary_table(text), **kwargs)

    def test_a_clean_table_has_nothing_to_report(self):
        self.assertEqual(find_bad_values(parse_summary_table(CLEAN)), [])

    def test_the_word_fail_is_not_a_number(self):
        bad = self._bad("cell_b total_cap 2.000 fail 0.010")
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].column, "cmpReport1")
        self.assertIn("fail", bad[0].reason)

    def test_the_default_failure_sentinel_is_caught(self):
        """1e+15 is the tool saying "no answer" in a way that parses as a
        number, so a plain float() check would wave it straight through.
        """
        bad = self._bad("cell_b total_cap 2.000 1e+15 0.010")
        self.assertEqual(len(bad), 1)
        self.assertIn("default failure value", bad[0].reason)

    def test_a_negative_sentinel_is_caught_too(self):
        bad = self._bad("cell_b total_cap 2.000 -1e+15 0.010")
        self.assertEqual(len(bad), 1)

    def test_a_value_below_the_threshold_is_fine(self):
        self.assertEqual(self._bad("cell_b total_cap 2.000 9.9e+14 0.010"), [])

    def test_a_row_that_stops_early_is_missing_values(self):
        bad = self._bad("cell_b total_cap 2.000")
        self.assertEqual([b.column for b in bad], ["cmpReport1", "diffCmp1"])

    def test_a_row_with_only_names_is_reported(self):
        bad = self._bad("cell_b total_cap")
        self.assertEqual(len(bad), 1)
        self.assertIn("no values at all", bad[0].reason)

    def test_text_where_a_number_belongs_is_caught(self):
        bad = self._bad("cell_b total_cap 2.000 n/a 0.010")
        self.assertEqual(bad[0].reason, "not a number")

    def test_nan_is_caught(self):
        bad = self._bad("cell_b total_cap 2.000 nan 0.010")
        self.assertIn("nan", bad[0].reason)

    def test_the_ref_column_is_checked_as_well(self):
        bad = self._bad("cell_b total_cap fail 2.010 0.010")
        self.assertEqual(bad[0].column, "refReport")

    def test_the_fail_word_list_is_configurable(self):
        bad = self._bad("cell_b total_cap 2.000 SKIPPED 0.010",
                        fail_words=["skipped"])
        self.assertIn("SKIPPED", bad[0].reason)


# ---------------------------------------------------------------------------
# The checks, against real directories
# ---------------------------------------------------------------------------

class _CheckTest(unittest.TestCase):
    """Scan one index run folder and collect the issues it produced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self, folder, **kwargs):
        from arcx_auto.services.monitor import MonitorService

        return MonitorService(self.settings).scan(
            run_folders=[folder], use_lsf=False, **kwargs)

    def _issues(self, folder, **kwargs):
        return {i.id: i for i in self._scan(folder, **kwargs).all_issues()}

    def _states(self, folder, **kwargs):
        snapshot = self._scan(folder, **kwargs).snapshots[0]
        return {cid: c.state for cid, c in snapshot.cases.items()}


class NetlistSignatureTest(_CheckTest):
    """A netlist can be present and full sized and still be truncated.

    The extraction engine stamps its name on the first line, so that line is
    the cheapest evidence that it ran at all.
    """

    def _build(self, artifacts, special=None, wave=False):
        root = os.path.join(self.tmp.name, "wave_001") if wave else self.tmp.name
        folder = make_index_run_folder(
            root, "1000", [CaseSpec("NTN_1", "complete", artifacts=artifacts)])
        make_arcx_cfg(os.path.join(folder, "zmwu.cfg"))
        if special is not None:
            # Where WorkspaceBuilder puts the snapshot it took at submit time
            snap_dir = os.path.join(root, ".arcx_auto", "special_cfg")
            os.makedirs(snap_dir, exist_ok=True)
            with open(os.path.join(snap_dir, "1000.cfg"), "w",
                      encoding="utf-8") as handle:
                handle.write(special)
        return folder

    def test_a_complete_netlist_says_nothing(self):
        folder = self._build("full", special="g:O_EXTARCTION = PARA", wave=True)
        self.assertNotIn("NETLIST_NO_SIGNATURE", self._issues(folder))
        self.assertEqual(list(self._states(folder).values()), [CaseState.DONE])

    def test_a_missing_banner_with_para_mode_is_fatal(self):
        """The reported false success: full sized netlist, no QuickCap line."""
        folder = self._build("no_signature", special="g:O_EXTARCTION = PARA",
                             wave=True)
        issue = self._issues(folder).get("NETLIST_NO_SIGNATURE")
        self.assertIsNotNone(issue)
        self.assertEqual(issue.severity, Severity.FATAL)
        self.assertEqual(list(self._states(folder).values()), [CaseState.FAILED])

    def test_resistance_only_mode_never_expects_the_banner(self):
        """With O_EXTARCTION = R the QuickCap engine does not run, so its
        absence is correct and flagging it would be crying wolf.
        """
        folder = self._build("no_signature", special="g:O_EXTARCTION = R",
                             wave=True)
        self.assertNotIn("NETLIST_NO_SIGNATURE", self._issues(folder))
        self.assertEqual(list(self._states(folder).values()), [CaseState.DONE])

    def test_an_unreadable_special_cfg_is_unknown_not_a_pass(self):
        """Without special.cfg we cannot tell whether the banner was expected.

        UNKNOWN blocks success and says exactly that, rather than guessing in
        either direction.
        """
        folder = self._build("no_signature")
        issue = self._issues(folder).get("NETLIST_NO_SIGNATURE")
        self.assertIsNotNone(issue)
        self.assertEqual(issue.severity, Severity.UNKNOWN)

    def test_a_healthy_netlist_never_needs_special_cfg_at_all(self):
        """This is what keeps the check quiet in practice: special.cfg is only
        consulted once something already looks wrong.
        """
        folder = self._build("full")          # no special.cfg anywhere
        self.assertNotIn("NETLIST_NO_SIGNATURE", self._issues(folder))
        self.assertEqual(list(self._states(folder).values()), [CaseState.DONE])

    def test_special_cfg_can_come_from_the_index_path(self):
        """For a run this tool did not submit there is no snapshot, so dir_map
        supplies the index path instead.
        """
        index_path = os.path.join(self.tmp.name, "src1000")
        os.makedirs(index_path)
        with open(os.path.join(index_path, "special.cfg"), "w",
                  encoding="utf-8") as handle:
            handle.write("g:O_EXTARCTION = PARA\nO_QCAP_LSF_NUM = 4\n")
        folder = self._build("no_signature")
        issue = self._issues(folder, index_sources={
            "1000": IndexSource(index_key="1000", path=index_path),
        }).get("NETLIST_NO_SIGNATURE")
        self.assertEqual(issue.severity, Severity.FATAL)

    def test_only_flows_with_a_configured_signature_are_checked(self):
        """calQRCFS has no banner configured, so its netlist is left alone."""
        self.settings.qa.netlist_signature.flow_signatures = {}
        folder = self._build("no_signature", special="g:O_EXTARCTION = PARA",
                             wave=True)
        self.assertNotIn("NETLIST_NO_SIGNATURE", self._issues(folder))


class SummaryTableCheckTest(_CheckTest):
    def _build(self, summary_text=None, dir_names=("QC_Cc", "QC_Ct", "QC_Spice")):
        folder = make_index_run_folder(
            self.tmp.name, "1000",
            [CaseSpec("NTN_1", "complete", artifacts="full")],
            report_dirs=dir_names)
        make_arcx_cfg(os.path.join(folder, "zmwu.cfg"))
        if summary_text is not None:
            path = os.path.join(folder, "QC_Spice",
                                "Report_QC_Spice_Summary_SCCB3")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(summary_text)
        return folder

    def test_a_clean_summary_says_nothing(self):
        folder = self._build()
        issues = self._issues(folder)
        self.assertNotIn("SUMMARY_TABLE_BAD_VALUE", issues)
        self.assertNotIn("SUMMARY_TABLE_UNREADABLE", issues)

    def test_a_failed_comparison_is_fatal(self):
        folder = self._build(make_summary_table(bad=["cell_b"]))
        issue = self._issues(folder).get("SUMMARY_TABLE_BAD_VALUE")
        self.assertIsNotNone(issue)
        self.assertEqual(issue.severity, Severity.FATAL)
        self.assertEqual(issue.evidence["total"], 2)   # the value and its diff

    def test_the_evidence_names_the_row_and_the_column(self):
        folder = self._build(make_summary_table(bad=["cell_b"]))
        problems = self._issues(folder)["SUMMARY_TABLE_BAD_VALUE"] \
            .evidence["problems"]
        self.assertTrue(any(p["row"].startswith("cell_b") for p in problems))
        self.assertTrue(any("Summary" in p["file"] for p in problems))

    def test_an_unrecognised_summary_is_unknown_not_fatal(self):
        """Not finding the table says nothing about the run. Calling it a
        failure would cry wolf on every report of an unfamiliar shape.
        """
        folder = self._build("some other report format entirely\n")
        issues = self._issues(folder)
        self.assertNotIn("SUMMARY_TABLE_BAD_VALUE", issues)
        self.assertEqual(issues["SUMMARY_TABLE_UNREADABLE"].severity,
                         Severity.UNKNOWN)

    def test_a_lowercase_filename_is_still_found(self):
        """The directory is QC_Spice but the file is Report_QC_spice_Summary.

        A check that silently found nothing because of one letter would be
        worse than no check at all.
        """
        folder = self._build()
        spice = os.path.join(folder, "QC_Spice")
        for name in os.listdir(spice):
            if "Summary" in name:
                os.remove(os.path.join(spice, name))
        with open(os.path.join(spice, "Report_QC_spice_Summary"), "w",
                  encoding="utf-8") as handle:
            handle.write(make_summary_table(bad=["cell_a"]))
        self.assertIn("SUMMARY_TABLE_BAD_VALUE", self._issues(folder))

    def test_only_the_configured_directories_are_read(self):
        """The rule applies to QC_Spice for now. QC_Cc has a table of the same
        shape here, and must be left alone until someone says otherwise.
        """
        folder = self._build()
        with open(os.path.join(folder, "QC_Cc", "Report_QC_Cc_Summary_SCCB3"),
                  "w", encoding="utf-8") as handle:
            handle.write(make_summary_table(bad=["cell_a"]))
        self.assertNotIn("SUMMARY_TABLE_BAD_VALUE", self._issues(folder))

    def test_a_missing_qc_spice_is_not_this_check_s_problem(self):
        folder = self._build(dir_names=("QC_Cc", "QC_Ct"))
        issues = self._issues(folder)
        self.assertNotIn("SUMMARY_TABLE_BAD_VALUE", issues)
        self.assertNotIn("SUMMARY_TABLE_UNREADABLE", issues)


class BadSummaryReachesTheRerunPlanTest(_CheckTest):
    """The point of detecting a false success is being able to act on it.

    An index level failure has to reach the rerun decision, or the check is
    just a nicer way of finding out too late.
    """

    def test_a_bad_summary_shows_up_as_an_index_issue(self):
        folder = make_index_run_folder(
            self.tmp.name, "1000",
            [CaseSpec("NTN_1", "complete", artifacts="full")])
        make_arcx_cfg(os.path.join(folder, "zmwu.cfg"))
        with open(os.path.join(folder, "QC_Spice",
                               "Report_QC_Spice_Summary_SCCB3"),
                  "w", encoding="utf-8") as handle:
            handle.write(make_summary_table(bad=["cell_a", "cell_b"]))

        result = self._scan(folder)
        blocking = [i for i in result.all_issues() if i.blocks_success]
        self.assertTrue(any(i.id == "SUMMARY_TABLE_BAD_VALUE" for i in blocking))

    def test_the_rerun_plan_says_a_rerun_will_not_clear_it(self):
        """No case owns the failure, so nothing lands on the delete list -- and
        a plan that then said "nothing needs rerunning" would be the false
        success arriving by a different route.
        """
        from arcx_auto.services.rerun_planner import build_rerun_plan

        folder = make_index_run_folder(
            self.tmp.name, "1000",
            [CaseSpec("NTN_1", "complete", artifacts="full")])
        make_arcx_cfg(os.path.join(folder, "zmwu.cfg"))
        with open(os.path.join(folder, "QC_Spice",
                               "Report_QC_Spice_Summary_SCCB3"),
                  "w", encoding="utf-8") as handle:
            handle.write(make_summary_table(bad=["cell_a"]))

        result = self._scan(folder)
        plan = build_rerun_plan(
            self.tmp.name, "wave_001", result.snapshots, result.qa_reports,
            launch={"arcx_job_id": "1", "index_keys": ["1000"]},
            manifest={"snapshots": {"arcx_cfg": os.path.join(folder, "zmwu.cfg")}})

        self.assertEqual(plan.to_delete, ())
        self.assertTrue(
            any("SUMMARY_TABLE_BAD_VALUE" in w for w in plan.warnings),
            "the index level failure disappeared from the plan: %s"
            % (plan.warnings,))


if __name__ == "__main__":
    unittest.main()
