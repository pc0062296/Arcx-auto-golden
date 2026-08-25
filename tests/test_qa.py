"""QA registry, expectations, the checks themselves, and StateResolver."""

import os
import tempfile
import unittest

from arcx_auto.adapters.arcx_cfg import parse_arcx_cfg
from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.config.settings import FlowProfile, Settings
from arcx_auto.domain.enums import (
    CaseState,
    IssueScope,
    IssueStage,
    Completeness,
    Severity,
)
from arcx_auto.domain.models import IndexSource
from arcx_auto.domain.qa import Issue, QaResult
from arcx_auto.services.qa import QaRunner, REGISTRY
from arcx_auto.services.qa.expectations import expected_artifacts
from arcx_auto.services.qa.registry import INTERNAL_ERROR_ID, QaRegistry
from arcx_auto.services.state_engine import TransitionContext, transition_index_run
from arcx_auto.services.state_resolver import resolve_case_state
from tests.fixtures.fake_run import (
    CaseSpec,
    make_arcx_cfg,
    make_index_run_folder,
)


def build(tmp, cases, index_key="1000"):
    """Build a wave dir, cfg and run folder; return (snapshot, obs, cfg)."""
    wave = os.path.join(tmp, "wave_001")
    os.makedirs(wave, exist_ok=True)
    cfg_path = make_arcx_cfg(os.path.join(wave, "arcx.cfg"))
    folder = make_index_run_folder(wave, index_key, cases)

    fs = FsAdapter()
    observation = fs.scan_index_run_folder(folder, index_key)
    ctx = TransitionContext(now=observation.observed_at, lsf_data_available=False)
    snapshot, _events = transition_index_run(None, observation, ctx)
    return snapshot, observation, parse_arcx_cfg(cfg_path)


def issue_ids(report, case_id):
    return sorted({i.id for i in report.issues_for(case_id)})


# ---------------------------------------------------------------------------
# expectations
# ---------------------------------------------------------------------------

class ExpectationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = parse_arcx_cfg(
            make_arcx_cfg(os.path.join(self.tmp.name, "arcx.cfg")))
        self.qa = Settings().qa

    def tearDown(self):
        self.tmp.cleanup()

    def test_paths_match_real_structure(self):
        """<block>_<flow>/work_<flow>/<netlist>"""
        artifacts, problems = expected_artifacts(self.cfg, "NTN_1", self.qa)
        self.assertEqual(problems, ())
        self.assertEqual(
            sorted(a.relpath for a in artifacts),
            [
                "blocking_naming_qcap_calQCAP/work_calQCAP/CCI_DB.spice",
                "blocking_naming_qrcfs_calQRCFS/work_calQRCFS/NTN_1.spf",
            ],
        )

    def test_case_name_substituted_per_flow(self):
        """calQRCFS names its netlist after the case; calQCAP is fixed."""
        a1, _ = expected_artifacts(self.cfg, "NTN_1", self.qa)
        a2, _ = expected_artifacts(self.cfg, "PDIO_9", self.qa)
        self.assertIn("NTN_1.spf", " ".join(a.relpath for a in a1))
        self.assertIn("PDIO_9.spf", " ".join(a.relpath for a in a2))
        for group in (a1, a2):
            self.assertIn("CCI_DB.spice", " ".join(a.relpath for a in group))

    def test_unknown_flow_reported_not_silently_skipped(self):
        cfg = parse_arcx_cfg(make_arcx_cfg(
            os.path.join(self.tmp.name, "b.cfg"),
            blocks=(("blk", "calMYSTERY", "x.spf"),)))
        artifacts, problems = expected_artifacts(cfg, "NTN_1", self.qa)
        self.assertEqual(artifacts, ())
        self.assertTrue(any("calMYSTERY" in p for p in problems), problems)

    def test_new_flow_can_be_added_by_config_only(self):
        """Adding an EDA tool is adding a profile, not changing code."""
        qa = Settings().qa
        qa.flows["calMYSTERY"] = FlowProfile(netlists=["{case}_out.spf"])
        cfg = parse_arcx_cfg(make_arcx_cfg(
            os.path.join(self.tmp.name, "c.cfg"),
            blocks=(("blk", "calMYSTERY", "x"),)))
        artifacts, problems = expected_artifacts(cfg, "NTN_1", qa)
        self.assertEqual(problems, ())
        self.assertEqual(artifacts[0].relpath,
                         "blk_calMYSTERY/work_calMYSTERY/NTN_1_out.spf")


# ---------------------------------------------------------------------------
# False success detection -- the most important capability in the system
# ---------------------------------------------------------------------------

class FakeSuccessTest(unittest.TestCase):
    """All four cases carry a .complete marker and look successful by eye."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.snapshot, self.observation, self.cfg = build(self.tmp.name, [
            CaseSpec("NTN_1", "complete", artifacts="full"),
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist"),
            CaseSpec("PDIO_1", "complete", artifacts="empty_netlist"),
            CaseSpec("RES_HI", "complete", artifacts="missing_flow"),
        ])
        settings = Settings()
        settings.qa.min_netlist_bytes = 16
        self.report = QaRunner(settings).run_index(
            self.snapshot, self.observation, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_success_passes(self):
        self.assertEqual(issue_ids(self.report, "NTN_1"), [])
        self.assertTrue(self.report.merged_case_result("NTN_1").passed)

    def test_missing_netlist_detected(self):
        """The marker says finished, but the netlist was never produced."""
        self.assertIn("NETLIST_MISSING", issue_ids(self.report, "NDIO_1"))

    def test_empty_netlist_detected(self):
        """The file exists but is 0 bytes; checking existence alone misses it."""
        self.assertIn("NETLIST_EMPTY", issue_ids(self.report, "PDIO_1"))

    def test_missing_flow_dir_detected(self):
        """The whole flow never ran, which is worse than a failed write."""
        ids = issue_ids(self.report, "RES_HI")
        self.assertIn("FLOW_DIR_MISSING", ids)

    def test_states_resolve_to_done_or_failed(self):
        resolved = {}
        for case_id, case in self.snapshot.cases.items():
            state, _reason = resolve_case_state(
                case.base_state or case.state, self.report.issues_for(case_id))
            resolved[case_id] = state
        self.assertEqual(resolved["NTN_1"], CaseState.DONE)
        for case_id in ("NDIO_1", "PDIO_1", "RES_HI"):
            self.assertEqual(resolved[case_id], CaseState.FAILED, case_id)

    def test_failed_cases_marked_for_rerun(self):
        """FAILED run dirs get deleted and rerun; successful ones are kept."""
        self.assertEqual(
            self.report.merged_case_result("NTN_1").completeness()[0],
            Completeness.COMPLETE)
        self.assertEqual(
            self.report.merged_case_result("NDIO_1").completeness()[0],
            Completeness.INCOMPLETE)


class NoConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_cfg_yields_unknown_not_pass(self):
        """Without arcx.cfg a case must never be treated as passing."""
        snapshot, observation, _cfg = build(
            self.tmp.name, [CaseSpec("NTN_1", "complete", artifacts="none")])
        report = QaRunner(Settings()).run_index(snapshot, observation, None)
        self.assertIn("CFG_EXPECTATION_UNAVAILABLE", issue_ids(report, "NTN_1"))
        result = report.merged_case_result("NTN_1")
        self.assertFalse(result.passed)
        self.assertEqual(result.completeness()[0], Completeness.UNKNOWN)


# ---------------------------------------------------------------------------
# Index level
# ---------------------------------------------------------------------------

T0 = 2_000_000.0


def queue_snapshot(tmp, states, entered_at=T0):
    """An index run made of states, without touching a filesystem.

    ``states`` is {case_id: CaseState}, or {case_id: (CaseState, entered_at)}
    when a case has to have moved at a different time from the rest.
    """
    from arcx_auto.domain.models import CaseSnapshot, IndexRunSnapshot

    cases = {}
    for case_id, value in states.items():
        state, moved_at = value if isinstance(value, tuple) else (value,
                                                                 entered_at)
        cases[case_id] = CaseSnapshot(
            case_id=case_id, state=state, entered_state_at=moved_at,
            last_progress_at=moved_at, base_state=state)
    return IndexRunSnapshot(index_key="1000", run_folder=tmp, cases=cases,
                            updated_at=entered_at)


class QueueNotMovingTest(unittest.TestCase):
    """A queued case is normal. A queue that has stopped moving is not.

    Arcx runs only so many cases at a time within one index, so on a large
    index most cases are queued most of the time. What that hides is an index
    that has simply stopped: nothing running, work still waiting, and no error
    anywhere to say so.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings()

    def ids(self, states, now=T0 + 7200, entered_at=T0):
        report = QaRunner(self.settings).run_index(
            queue_snapshot(self.tmp.name, states, entered_at), None, None,
            now=now)
        found = set()
        for result in report.index_results:
            found.update(i.id for i in result.issues)
        return found

    def test_queued_while_something_runs_is_normal(self):
        """The case this check must never fire on: Arcx holding cases back
        while it works through the index.
        """
        self.assertNotIn("INDEX_QUEUE_NOT_MOVING", self.ids({
            "a": CaseState.RUNNING,
            "b": CaseState.QUEUED,
            "c": CaseState.QUEUED,
        }))

    def test_queued_with_nothing_running_is_reported(self):
        self.assertIn("INDEX_QUEUE_NOT_MOVING", self.ids({
            "a": CaseState.DONE,
            "b": CaseState.QUEUED,
            "c": CaseState.QUEUED,
        }))

    def test_nothing_is_said_before_the_threshold(self):
        """Arcx goes quiet between cases while it assembles reports or sets
        the next one up, and that is not a fault.
        """
        self.assertNotIn("INDEX_QUEUE_NOT_MOVING", self.ids(
            {"a": CaseState.DONE, "b": CaseState.QUEUED},
            now=T0 + 60))

    def test_the_clock_starts_at_the_last_thing_that_happened(self):
        """Idle time, not queued time: "queued for six hours" is a fact about
        the size of the index, "six hours with nothing running" is a fact
        about the run.
        """
        states = {"a": (CaseState.DONE, T0), "b": (CaseState.QUEUED, T0)}
        self.assertIn("INDEX_QUEUE_NOT_MOVING", self.ids(states, now=T0 + 7200))
        # the same long-queued case, but something moved a minute ago
        states["c"] = (CaseState.DONE, T0 + 7140)
        self.assertNotIn("INDEX_QUEUE_NOT_MOVING",
                         self.ids(states, now=T0 + 7200))

    def test_a_suspended_job_still_counts_as_running(self):
        """It holds its slot, so the queue is waiting for a reason."""
        self.assertNotIn("INDEX_QUEUE_NOT_MOVING", self.ids({
            "a": CaseState.SUSPENDED, "b": CaseState.QUEUED}))

    def test_a_stalled_case_still_counts_as_running(self):
        self.assertNotIn("INDEX_QUEUE_NOT_MOVING", self.ids({
            "a": CaseState.STALLED, "b": CaseState.QUEUED}))

    def test_a_finished_case_does_not_count_as_running(self):
        """.complete is permanent. Counting it would silence this check for
        good on any index that completed one case and then died.
        """
        self.assertIn("INDEX_QUEUE_NOT_MOVING", self.ids({
            "a": CaseState.COMPLETED_MARKER, "b": CaseState.QUEUED}))

    def test_an_index_with_nothing_queued_says_nothing(self):
        self.assertNotIn("INDEX_QUEUE_NOT_MOVING", self.ids({
            "a": CaseState.DONE, "b": CaseState.FAILED}))

    def test_an_index_that_never_started_anything_is_reported(self):
        """Submitted, and Arcx never ran a single case."""
        self.assertIn("INDEX_QUEUE_NOT_MOVING", self.ids({
            "a": CaseState.QUEUED, "b": CaseState.QUEUED}))

    def test_the_threshold_is_configurable(self):
        self.settings.qa.queue.idle_after_sec = 100000.0
        self.assertNotIn("INDEX_QUEUE_NOT_MOVING", self.ids({
            "a": CaseState.DONE, "b": CaseState.QUEUED}))

    def test_the_evidence_names_the_waiting_cases(self):
        report = QaRunner(self.settings).run_index(
            queue_snapshot(self.tmp.name,
                           {"a": CaseState.DONE, "b": CaseState.QUEUED}),
            None, None, now=T0 + 7200)
        issue = [i for result in report.index_results for i in result.issues
                 if i.id == "INDEX_QUEUE_NOT_MOVING"][0]
        self.assertEqual(issue.severity.value, "WARN")
        self.assertEqual(issue.evidence["queued"], ["b"])
        self.assertEqual(issue.evidence["idle_sec"], 7200)


class ReportCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _index_ids(self, report):
        ids = set()
        for result in report.index_results:
            ids.update(i.id for i in result.issues)
        return ids

    def test_reports_present_passes(self):
        snapshot, observation, cfg = build(
            self.tmp.name, [CaseSpec("NTN_1", "complete")])
        report = QaRunner(Settings()).run_index(snapshot, observation, cfg)
        self.assertNotIn("REPORT_DIR_MISSING", self._index_ids(report))
        self.assertNotIn("REPORT_FILE_MISSING", self._index_ids(report))

    def test_missing_required_report_dir_detected(self):
        wave = os.path.join(self.tmp.name, "wave_001")
        folder = make_index_run_folder(
            wave, "1000", [CaseSpec("NTN_1", "complete")], report_dirs=("QC_Cc",))
        fs = FsAdapter()
        observation = fs.scan_index_run_folder(folder, "1000")
        ctx = TransitionContext(now=observation.observed_at)
        snapshot, _ = transition_index_run(None, observation, ctx)
        report = QaRunner(Settings()).run_index(snapshot, observation, None)
        self.assertIn("REPORT_DIR_MISSING", self._index_ids(report))

    def test_missing_report_file_detected(self):
        """The directory exists but Report_QC_Cc or the Summary is missing."""
        from tests.fixtures.fake_run import make_report_dirs

        wave = os.path.join(self.tmp.name, "wave_001")
        folder = make_index_run_folder(
            wave, "1000", [CaseSpec("NTN_1", "complete")], report_dirs=())
        make_report_dirs(folder, ("QC_Cc", "QC_Ct"), skip_files=("QC_Ct",))
        fs = FsAdapter()
        observation = fs.scan_index_run_folder(folder, "1000")
        ctx = TransitionContext(now=observation.observed_at)
        snapshot, _ = transition_index_run(None, observation, ctx)
        report = QaRunner(Settings()).run_index(snapshot, observation, None)
        self.assertIn("REPORT_FILE_MISSING", self._index_ids(report))

    def test_unknown_marker_surfaces_as_unknown_issue(self):
        wave = os.path.join(self.tmp.name, "wave_001")
        folder = make_index_run_folder(wave, "1000", [CaseSpec("NTN_1", "complete")])
        open(os.path.join(folder, ".fail.NTN_1"), "w").close()
        fs = FsAdapter()
        observation = fs.scan_index_run_folder(folder, "1000")
        ctx = TransitionContext(now=observation.observed_at)
        snapshot, _ = transition_index_run(None, observation, ctx)
        report = QaRunner(Settings()).run_index(snapshot, observation, None)
        self.assertIn("INDEX_HAS_UNKNOWN_MARKER", self._index_ids(report))


# ---------------------------------------------------------------------------
# Graded stall detection
# ---------------------------------------------------------------------------

class QuietTest(unittest.TestCase):
    """Graded, not binary: runtimes span ten minutes to three days, so a hard
    rule would raise false alarms.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, age_sec, artifacts="none"):
        snapshot, observation, cfg = build(self.tmp.name, [
            CaseSpec("NTN_1", "running", age_sec=age_sec, artifacts=artifacts),
        ])
        settings = Settings()
        settings.qa.min_netlist_bytes = 1
        report = QaRunner(settings).run_index(
            snapshot, observation, cfg, now=observation.observed_at)
        return [i for i in report.issues_for("NTN_1") if i.id == "CASE_QUIET"]

    def test_quiet_under_threshold_not_reported(self):
        self.assertEqual(self._run(600), [])

    def test_quiet_over_4h_is_warn(self):
        issues = self._run(5 * 3600)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, Severity.WARN)

    def test_quiet_over_8h_is_fatal(self):
        issues = self._run(9 * 3600)
        self.assertEqual(issues[0].severity, Severity.FATAL)

    def test_downgraded_when_artifacts_already_present(self):
        """Quiet with every artifact present is usually tidy-up, not a stall."""
        issues = self._run(9 * 3600, artifacts="full")
        self.assertEqual(issues[0].severity, Severity.WARN)
        self.assertTrue(issues[0].evidence["artifacts_ready"])

    def test_evidence_carries_duration(self):
        issues = self._run(5 * 3600)
        self.assertGreater(issues[0].evidence["silent_sec"], 4 * 3600)


# ---------------------------------------------------------------------------
# Registry behaviour
# ---------------------------------------------------------------------------

class RegistryTest(unittest.TestCase):
    def test_duplicate_id_rejected(self):
        registry = QaRegistry()

        @registry.check(id="X", title="t")
        def first(ctx):
            return None

        with self.assertRaises(ValueError):
            @registry.check(id="X", title="t2")
            def second(ctx):
                return None

    def test_crashing_check_is_isolated(self):
        """One bad rule must never stop the whole monitoring."""
        registry = QaRegistry()

        @registry.check(id="BOOM", title="a check that explodes")
        def boom(ctx):
            raise RuntimeError("deliberate")

        @registry.check(id="FINE", title="a working check")
        def fine(ctx):
            return ctx.fail("I ran")

        tmp = tempfile.TemporaryDirectory()
        try:
            snapshot, observation, cfg = build(
                tmp.name, [CaseSpec("NTN_1", "complete")])
            report = QaRunner(Settings(), registry=registry).run_index(
                snapshot, observation, cfg)
            ids = issue_ids(report, "NTN_1")
            self.assertIn(INTERNAL_ERROR_ID, ids)
            self.assertIn("FINE", ids)
        finally:
            tmp.cleanup()

    def test_crash_is_unknown_not_pass(self):
        registry = QaRegistry()

        @registry.check(id="BOOM", title="a check that explodes")
        def boom(ctx):
            raise RuntimeError("x")

        tmp = tempfile.TemporaryDirectory()
        try:
            snapshot, observation, cfg = build(
                tmp.name, [CaseSpec("NTN_1", "complete")])
            report = QaRunner(Settings(), registry=registry).run_index(
                snapshot, observation, cfg)
            result = report.merged_case_result("NTN_1")
            self.assertFalse(result.passed)
            self.assertEqual(result.completeness()[0], Completeness.UNKNOWN)
        finally:
            tmp.cleanup()

    def test_disabled_check_is_skipped(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            snapshot, observation, cfg = build(
                tmp.name,
                [CaseSpec("NTN_1", "complete", artifacts="missing_netlist")])
            settings = Settings()
            settings.qa.disabled_checks = ["NETLIST_MISSING"]
            report = QaRunner(settings).run_index(snapshot, observation, cfg)
            self.assertNotIn("NETLIST_MISSING", issue_ids(report, "NTN_1"))
        finally:
            tmp.cleanup()

    def test_every_builtin_check_has_a_docstring(self):
        """The docstring is the UI description; without it nobody can tell
        what the check does.
        """
        missing = [s.id for s in REGISTRY.all() if not s.doc]
        self.assertEqual(missing, [])


# ---------------------------------------------------------------------------
# StateResolver
# ---------------------------------------------------------------------------

class ResolverTest(unittest.TestCase):
    def _issue(self, id_, severity):
        return Issue(id=id_, severity=severity, message="m")

    def test_completed_with_no_issue_is_done(self):
        state, _ = resolve_case_state(CaseState.COMPLETED_MARKER, [])
        self.assertEqual(state, CaseState.DONE)

    def test_completed_with_fatal_is_failed(self):
        state, reason = resolve_case_state(
            CaseState.COMPLETED_MARKER,
            [self._issue("NETLIST_MISSING", Severity.FATAL)])
        self.assertEqual(state, CaseState.FAILED)
        self.assertIn("NETLIST_MISSING", reason)

    def test_completed_with_unknown_is_failed_not_done(self):
        """Could not check must never count as success."""
        state, _ = resolve_case_state(
            CaseState.COMPLETED_MARKER,
            [self._issue("CFG_EXPECTATION_UNAVAILABLE", Severity.UNKNOWN)])
        self.assertEqual(state, CaseState.FAILED)

    def test_completed_with_warn_only_is_done(self):
        state, _ = resolve_case_state(
            CaseState.COMPLETED_MARKER,
            [self._issue("MARKER_INCONSISTENT", Severity.WARN)])
        self.assertEqual(state, CaseState.DONE)

    def test_running_with_fatal_quiet_becomes_stalled(self):
        state, _ = resolve_case_state(
            CaseState.RUNNING, [self._issue("CASE_QUIET", Severity.FATAL)])
        self.assertEqual(state, CaseState.STALLED)

    def test_running_with_warn_quiet_stays_running(self):
        state, _ = resolve_case_state(
            CaseState.RUNNING, [self._issue("CASE_QUIET", Severity.WARN)])
        self.assertEqual(state, CaseState.RUNNING)

    def test_lsf_confirmed_states_not_rewritten(self):
        """LOST and SUSPENDED are facts LSF confirmed; QA must not rewrite them."""
        for base in (CaseState.LOST, CaseState.SUSPENDED, CaseState.QUEUED):
            state, _ = resolve_case_state(
                base, [self._issue("CASE_QUIET", Severity.FATAL)])
            self.assertEqual(state, base)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# PRE -- arcx.cfg validation
# ---------------------------------------------------------------------------

class ConfigCheckTest(unittest.TestCase):
    """Pre-submission cfg validation, the layer with the best return: most
    configuration mistakes are visible before submitting, and finding them
    afterwards costs hours of waiting.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = QaRunner(Settings())

    def tearDown(self):
        self.tmp.cleanup()

    def _check(self, text):
        path = os.path.join(self.tmp.name, "arcx.cfg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        result = self.runner.run_config(parse_arcx_cfg(path))
        return result, {i.id for i in result.issues}

    def _existing_file(self, name="real.qtf"):
        path = os.path.join(self.tmp.name, name)
        open(path, "w").close()
        return path

    def test_good_config_passes(self):
        qtf = self._existing_file()
        result, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
            "1 RCX_TECH_QTF = %s\nEND_SETTINGS\n" % qtf)
        self.assertEqual(ids, set())
        self.assertTrue(result.passed)

    def test_missing_path_detected(self):
        _r, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
            "1 RCX_TECH_QTF = /definitely/not/here\nEND_SETTINGS\n")
        self.assertIn("CFG_PATH_NOT_FOUND", ids)

    def test_disabled_line_path_not_checked(self):
        """A leading 0 makes the setting inactive; checking its path would
        only manufacture a false alarm.
        """
        _r, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
            "0 RCX_TECH_QTF = /definitely/not/here\nEND_SETTINGS\n")
        self.assertNotIn("CFG_PATH_NOT_FOUND", ids)

    def test_commented_line_path_not_checked(self):
        _r, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
            "# 1 RCX_TECH_QTF = /definitely/not/here\nEND_SETTINGS\n")
        self.assertNotIn("CFG_PATH_NOT_FOUND", ids)

    def test_disabled_block_paths_not_checked(self):
        _r, ids = self._check(
            "0 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
            "1 RCX_TECH_QTF = /definitely/not/here\nEND_SETTINGS\n")
        self.assertNotIn("CFG_PATH_NOT_FOUND", ids)

    def test_only_listed_keys_are_path_checked(self):
        """Settings outside cfg_path_keys are not checked even if they look
        like paths.

        Checking anything path-shaped would produce false alarms on output
        paths and templates, and once there are false alarms nobody reads the
        warnings.
        """
        _r, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
            "1 SOME_OUTPUT_DIR = /will/be/created/later\nEND_SETTINGS\n")
        self.assertNotIn("CFG_PATH_NOT_FOUND", ids)

    def test_all_six_path_keys_are_checked(self):
        keys = ["RCX_TECH_QTF", "RCX_LAYER_NAME_MAP", "LVS_DFM_DIR",
                "LVS_DECK", "LVS_QUERY_CMD", "RCX_STAR_CMD"]
        for key in keys:
            _r, ids = self._check(
                "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
                "1 %s = /definitely/not/here\nEND_SETTINGS\n" % key)
            self.assertIn("CFG_PATH_NOT_FOUND", ids, key)

    def test_env_var_paths_skipped_not_reported_missing(self):
        """Values containing $ cannot be resolved here; they are skipped
        rather than reported as missing.
        """
        result, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
            "1 RCX_TECH_QTF = $TECH_ROOT/a.qtf\nEND_SETTINGS\n")
        self.assertNotIn("CFG_PATH_NOT_FOUND", ids)

    def test_duplicate_block_name_detected(self):
        """Same-named blocks overwrite each other with no error message."""
        _r, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n"
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQRCFS\nEND_SETTINGS\n")
        self.assertIn("CFG_DUPLICATE_BLOCK", ids)

    def test_missing_qc_flow_detected(self):
        _r, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 RCX_TECH_QTF = /x\nEND_SETTINGS\n")
        self.assertIn("CFG_BLOCK_NO_FLOW", ids)

    def test_unknown_flow_detected(self):
        _r, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calMYSTERY\nEND_SETTINGS\n")
        self.assertIn("CFG_UNKNOWN_FLOW", ids)

    def test_all_blocks_disabled_detected(self):
        """It runs to completion quietly and produces nothing, the most
        wasteful failure there is.
        """
        _r, ids = self._check(
            "0 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n")
        self.assertIn("CFG_ALL_BLOCKS_DISABLED", ids)

    def test_empty_config_detected(self):
        _r, ids = self._check("# nothing here\n")
        self.assertIn("CFG_NO_BLOCKS", ids)

    def test_missing_file_detected(self):
        result = self.runner.run_config(
            parse_arcx_cfg(os.path.join(self.tmp.name, "nope.cfg")))
        self.assertIn("CFG_UNREADABLE", {i.id for i in result.issues})

    def test_no_config_at_all_is_fatal(self):
        result = self.runner.run_config(None)
        self.assertIn("CFG_UNREADABLE", {i.id for i in result.issues})

    def test_typo_keyword_warns(self):
        _r, ids = self._check(
            "1 BEGIN_SETTIMGS: blk\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n")
        self.assertIn("CFG_PARSE_WARNING", ids)


class ReportSummaryCountTest(unittest.TestCase):
    """Exactly one Summary per QC_* directory; more is usually a leftover."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, extra_summaries=()):
        from tests.fixtures.fake_run import make_report_dirs

        wave = os.path.join(self.tmp.name, "wave_001")
        folder = make_index_run_folder(
            wave, "1000", [CaseSpec("NTN_1", "complete")], report_dirs=())
        make_report_dirs(folder, ("QC_Cc", "QC_Ct"))
        for name in extra_summaries:
            open(os.path.join(folder, "QC_Cc", name), "w").close()

        fs = FsAdapter()
        observation = fs.scan_index_run_folder(folder, "1000")
        ctx = TransitionContext(now=observation.observed_at)
        snapshot, _ = transition_index_run(None, observation, ctx)
        report = QaRunner(Settings()).run_index(snapshot, observation, None)
        ids = set()
        for result in report.index_results:
            ids.update(i.id for i in result.issues)
        return ids

    def test_exactly_one_summary_passes(self):
        self.assertNotIn("REPORT_FILE_MISSING", self._run())

    def test_two_summaries_flagged(self):
        ids = self._run(extra_summaries=("Report_QC_Cc_Summary_OLD",))
        self.assertIn("REPORT_FILE_MISSING", ids)


class StatusOnARealRunFolderTest(unittest.TestCase):
    """End to end on the shape of a real finished run folder.

    Reported symptom: five complete cases displayed as seven -- five FAILED and
    two UNKNOWN. Everything below is one assertion about that one run.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.names = ["NDIO_1", "PDIO_1", "NTN_1", "PTN_1", "CAP_MIM"]
        self.folder = make_index_run_folder(
            self.tmp.name, "1000",
            [CaseSpec(n, "complete", artifacts="full") for n in self.names])
        for name in ("svdb", "work_calQCAP"):
            os.makedirs(os.path.join(self.folder, name), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _states(self, **kwargs):
        from arcx_auto.services.monitor import MonitorService

        result = MonitorService(self.settings).scan(
            run_folders=[self.folder], use_lsf=False, **kwargs)
        snapshot = result.snapshots[0]
        return {cid: case.state for cid, case in snapshot.cases.items()}

    def test_five_cases_all_done_with_no_cfg_argument(self):
        """The whole reported bug, in one assertion.

        Arcx's own cfg snapshot sits in the run folder, so QA knows what to
        expect without being told, and the phantom directories never become
        cases.
        """
        make_arcx_cfg(os.path.join(self.folder, "zmwu.cfg"))
        states = self._states()
        self.assertEqual(sorted(states), sorted(self.names))
        self.assertEqual(set(states.values()), {CaseState.DONE})

    def test_without_any_cfg_it_says_it_does_not_know(self):
        """The counterpart: with no cfg anywhere, the checks must not silently
        pass. UNKNOWN blocks success, so the cases read FAILED -- correct, and
        the reason names the missing cfg rather than inventing a defect.
        """
        states = self._states()
        self.assertEqual(sorted(states), sorted(self.names))
        self.assertEqual(set(states.values()), {CaseState.FAILED})

    def test_an_explicit_cfg_still_wins(self):
        """A cfg passed on the command line is a deliberate choice and must
        override whatever happens to be lying in the folder.
        """
        outside = make_arcx_cfg(os.path.join(self.tmp.name, "explicit.cfg"))
        make_arcx_cfg(os.path.join(self.folder, "zmwu.cfg"))
        states = self._states(arcx_config=parse_arcx_cfg(outside))
        self.assertEqual(set(states.values()), {CaseState.DONE})

    def test_the_gds_cross_check_is_silent_without_a_dir_map(self):
        from arcx_auto.services.monitor import MonitorService

        make_arcx_cfg(os.path.join(self.folder, "zmwu.cfg"))
        result = MonitorService(self.settings).scan(
            run_folders=[self.folder], use_lsf=False)
        ids = {i.id for i in result.all_issues()}
        self.assertNotIn("INDEX_CASE_COUNT_MISMATCH", ids)

    def test_the_gds_cross_check_warns_when_the_totals_disagree(self):
        from arcx_auto.domain.enums import Severity as Sev
        from arcx_auto.services.monitor import MonitorService

        make_arcx_cfg(os.path.join(self.folder, "zmwu.cfg"))
        result = MonitorService(self.settings).scan(
            run_folders=[self.folder], use_lsf=False, index_sources={"1000": IndexSource(index_key="1000", gds_count=7)})
        hits = [i for i in result.all_issues()
                if i.id == "INDEX_CASE_COUNT_MISMATCH"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].severity, Sev.WARN)
        # A warning only: the run still reads as five successful cases, because
        # GDS filenames and top cell names need not correspond.
        states = self._states(index_sources={"1000": IndexSource(index_key="1000", gds_count=7)})
        self.assertEqual(set(states.values()), {CaseState.DONE})
