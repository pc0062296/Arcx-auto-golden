"""QA Registry、expectations、實際檢查與 StateResolver。"""

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
    """造 wave 目錄 + cfg + run folder, 回傳 (snapshot, observation, cfg)。"""
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
        """calQRCFS 的 netlist 名稱依 case 而變, calQCAP 是固定檔名。"""
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
        """新增一種 EDA tool = 加一個 profile, 不需要改程式。"""
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
# 假成功偵測 —— 這是整個系統最重要的能力
# ---------------------------------------------------------------------------

class FakeSuccessTest(unittest.TestCase):
    """四個 case 都有 .complete marker, 人工看全部像成功。"""

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
        """marker 說完成, 但 netlist 根本沒產出來。"""
        self.assertIn("NETLIST_MISSING", issue_ids(self.report, "NDIO_1"))

    def test_empty_netlist_detected(self):
        """檔案在但是 0 byte —— 只看「存不存在」會漏掉。"""
        self.assertIn("NETLIST_EMPTY", issue_ids(self.report, "PDIO_1"))

    def test_missing_flow_dir_detected(self):
        """整個 flow 沒跑 —— 比產出物寫失敗更嚴重。"""
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
        """FAILED 的 case run dir 要被刪掉重跑, 成功的保留。"""
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
        """沒有 arcx.cfg 時, 絕不能把 case 當成通過。"""
        snapshot, observation, _cfg = build(
            self.tmp.name, [CaseSpec("NTN_1", "complete", artifacts="none")])
        report = QaRunner(Settings()).run_index(snapshot, observation, None)
        self.assertIn("CFG_EXPECTATION_UNAVAILABLE", issue_ids(report, "NTN_1"))
        result = report.merged_case_result("NTN_1")
        self.assertFalse(result.passed)
        self.assertEqual(result.completeness()[0], Completeness.UNKNOWN)


# ---------------------------------------------------------------------------
# index 層級
# ---------------------------------------------------------------------------

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
        """目錄在但裡面的 Report_QC_Cc / Summary 不見了。"""
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
# 卡住的分級判定
# ---------------------------------------------------------------------------

class QuietTest(unittest.TestCase):
    """分級而非二元 —— runtime 從 10 分鐘到 3 天都有, 硬判會誤報。"""

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
        """不寫 log 但產出物都齊了, 通常只是在收尾 —— 不該當成卡住。"""
        issues = self._run(9 * 3600, artifacts="full")
        self.assertEqual(issues[0].severity, Severity.WARN)
        self.assertTrue(issues[0].evidence["artifacts_ready"])

    def test_evidence_carries_duration(self):
        issues = self._run(5 * 3600)
        self.assertGreater(issues[0].evidence["silent_sec"], 4 * 3600)


# ---------------------------------------------------------------------------
# Registry 行為
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
        """一條壞規則絕不能讓整個監控停擺。"""
        registry = QaRegistry()

        @registry.check(id="BOOM", title="會爆的檢查")
        def boom(ctx):
            raise RuntimeError("這是故意的")

        @registry.check(id="FINE", title="正常的檢查")
        def fine(ctx):
            return ctx.fail("我有跑到")

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

        @registry.check(id="BOOM", title="會爆的檢查")
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
        """docstring 就是 UI 上的說明 —— 缺了使用者看不懂這條在檢查什麼。"""
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
        """檢查不了絕不能當成成功。"""
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
        """LOST / SUSPENDED 是 LSF 直接證實的事實, QA 不該改寫。"""
        for base in (CaseState.LOST, CaseState.SUSPENDED, CaseState.QUEUED):
            state, _ = resolve_case_state(
                base, [self._issue("CASE_QUIET", Severity.FATAL)])
            self.assertEqual(state, base)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# PRE —— arcx.cfg 檢查
# ---------------------------------------------------------------------------

class ConfigCheckTest(unittest.TestCase):
    """提交前的 cfg 驗證。投資報酬率最高的一層 ——
    設定錯誤造成的失敗大多在提交前就看得出來, 而送出去要等好幾小時才會發現。
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
        """行首 0 的設定不生效 —— 檢查它的路徑只會製造假警報。"""
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
        """未列在 cfg_path_keys 的設定即使長得像路徑也不檢查。

        「看起來像路徑就檢查」會對輸出路徑、樣板字串產生大量假警報,
        假警報多了就沒人看了。
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
        """含 $ 的值無法在這裡解析, 列為 skipped 而不是誤報成缺失。"""
        result, ids = self._check(
            "1 BEGIN_SETTINGS: blk\n1 QC_FLOW = calQCAP\n"
            "1 RCX_TECH_QTF = $TECH_ROOT/a.qtf\nEND_SETTINGS\n")
        self.assertNotIn("CFG_PATH_NOT_FOUND", ids)

    def test_duplicate_block_name_detected(self):
        """同名 block 的產出目錄會互相覆蓋, 而且不會有任何錯誤訊息。"""
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
        """會安靜地跑完卻什麼都不產出 —— 最浪費 TAT 的錯誤。"""
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
    """每個 QC_* 底下恰好一個 Summary —— 多於一個通常是前一輪殘留。"""

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
