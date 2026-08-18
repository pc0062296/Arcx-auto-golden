"""Phase 2b: Preflight / WorkspaceBuilder / Launcher / 閘門 / Submitter。

這是系統中第一個會寫入磁碟的流程, 所以測試的重點是**它在什麼情況下不動手**:
有 FATAL 就完全不建立目錄、dry-run 絕不碰磁碟、目標目錄非空就拒絕。
"""

import json
import os
import tempfile
import unittest

from arcx_auto.adapters.arcx_cfg import parse_arcx_cfg
from arcx_auto.config.settings import GateSettings, Settings
from arcx_auto.domain.enums import PlanMode, Severity, WaveState
from arcx_auto.domain.models import IndexSpec
from arcx_auto.services.launcher import LaunchResult, Launcher, read_launch
from arcx_auto.services.qa import QaRunner
from arcx_auto.services.submission import (
    GateState,
    SubmissionController,
    evaluate_gate,
)
from arcx_auto.services.submitter import Submitter
from arcx_auto.services.wave_planner import plan_waves
from arcx_auto.services.workspace import (
    WorkspaceBuilder,
    WorkspaceError,
    sha256,
)
from tests.fixtures.fake_run import make_dir_map, make_index_source

CFG_TEMPLATE = """\
g:QCA = Yes

1 BEGIN_SETTING : blocking_nameing_1
1 QC_FLOW = calQCAP
1 RCX_TECH_QTF = {qtf}
END_SETTINGS

1 BEGIN_SETTING : blocking_nameing_2
1 QC_FLOW = calQRCFS
1 RCX_TECH_QTF = {qtf}
END_SETTINGS
"""


class FakeLsf:
    """假的 LSF。讓提交流程可以在沒有 LSF 的機器上完整測試。"""

    def __init__(self, njobs=10, available=True, job_id="12345", ok=True):
        self.njobs = njobs
        self.available = available
        self.job_id = job_id
        self.ok = ok
        self.commands = []

    def is_available(self, command=None):
        return self.available

    def current_njobs(self):
        return (self.njobs, None) if self.available else (None, "沒有 busers")

    def _run(self, argv):
        from arcx_auto.adapters.lsf import CommandResult

        self.commands.append(list(argv))
        if not self.ok:
            return CommandResult(False, "", "queue 不存在", 1, None)
        return CommandResult(
            True, "Job <%s> is submitted to queue <normal>.\n" % self.job_id,
            "", 0, None)


def build_fixture(root, counts=((("1000", 3, 4, "sram_core")),
                                (("1001", 30, 4, "logic")),
                                (("1002", 2, 16, "ro_ring")))):
    """造 dir_map + arcx.cfg + index 來源目錄。"""
    src = os.path.join(root, "sources")
    entries = {}
    for key, gds, cpu, hint in counts:
        entries[key] = make_index_source(src, key, gds_count=gds,
                                         cpu_per_case=cpu, name_hint=hint)
    dir_map = make_dir_map(os.path.join(root, "dir_map"), entries)

    qtf = os.path.join(root, "tech.qtf")
    open(qtf, "w").close()
    cfg = os.path.join(root, "arcx.cfg")
    with open(cfg, "w", encoding="utf-8") as handle:
        handle.write(CFG_TEMPLATE.format(qtf=qtf))
    return dir_map, cfg, entries


def make_plan(entries, settings, max_slots=100, mode=PlanMode.AUTO):
    from arcx_auto.adapters.arcx import ArcxAdapter

    arcx = ArcxAdapter(settings.layout, settings.plan)
    specs = [arcx.build_index_spec(k, p) for k, p in sorted(entries.items())]
    return plan_waves(specs, max_slots_per_wave=max_slots, mode=mode)


# ---------------------------------------------------------------------------
# 閘門 —— 純函數, 可以窮舉
# ---------------------------------------------------------------------------

class GateTest(unittest.TestCase):
    def setUp(self):
        self.settings = GateSettings(min_interval_sec=600, quota_threshold=100,
                                     max_wait_sec=7200)
        self.state = GateState(wave_name="w", entered_at=0.0)

    def _eval(self, now, njobs, prev=None):
        return evaluate_gate(self.state, now, njobs, self.settings,
                             previous_submit_at=prev)

    def test_low_quota_allows(self):
        self.assertTrue(self._eval(0, 50).allow)

    def test_high_quota_blocks(self):
        self.assertFalse(self._eval(0, 150).allow)

    def test_min_interval_blocks_even_when_quota_is_low(self):
        """純 OR 的漏洞: 時間到了但 quota 滿的照送會塞爆 queue。

        反過來也一樣 —— quota 低但剛送完, 還是要等最小間隔 (防抖)。
        """
        self.assertFalse(self._eval(0, 10, prev=0.0).allow)
        self.assertTrue(self._eval(700, 10, prev=0.0).allow)

    def test_max_wait_forces_release(self):
        """quota 永遠不降時不能無限期卡死。"""
        decision = self._eval(7300, 999)
        self.assertTrue(decision.allow)
        self.assertTrue(decision.forced)

    def test_forced_release_is_labelled(self):
        """強制放行必須看得出來 —— 之後排隊很久不是系統壞掉。"""
        self.assertEqual(self._eval(7300, 999).label, "強制放行")

    def test_unknown_quota_does_not_allow(self):
        """查不到 quota 時**不能**當成「quota 很低」——
        那會在 LSF 有問題時反而狂送。
        """
        decision = self._eval(0, None)
        self.assertFalse(decision.allow)
        self.assertIn("查不到", decision.reason)

    def test_unknown_quota_still_honours_max_wait(self):
        self.assertTrue(self._eval(7300, None).allow)

    def test_wait_hint_is_bounded(self):
        self.assertLessEqual(self._eval(0, 999).wait_hint_sec, 300.0)


class ControllerTest(unittest.TestCase):
    def test_submits_in_plan_order(self):
        controller = SubmissionController(["wave_001", "wave_002"],
                                          GateSettings(min_interval_sec=0))
        self.assertEqual(controller.next_wave().wave_name, "wave_001")
        controller.mark_submitted("wave_001", "1", now=100.0)
        self.assertEqual(controller.next_wave().wave_name, "wave_002")

    def test_only_one_wave_advances_at_a_time(self):
        controller = SubmissionController(["a", "b", "c"], GateSettings())
        controller.evaluate(now=0.0, njobs=1)
        self.assertEqual(len(controller.pending()), 3)

    def test_failed_wave_is_aborted(self):
        controller = SubmissionController(["a"], GateSettings())
        controller.mark_failed("a", "bsub 失敗")
        self.assertEqual(controller.progress[0].state, WaveState.ABORTED)
        self.assertEqual(controller.pending(), [])

    def test_gate_state_persists_entered_at(self):
        controller = SubmissionController(["a"], GateSettings())
        controller.evaluate(now=100.0, njobs=999)
        controller.evaluate(now=200.0, njobs=999)
        self.assertEqual(controller.progress[0].gate.entered_at, 100.0)


# ---------------------------------------------------------------------------
# WorkspaceBuilder
# ---------------------------------------------------------------------------

class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.dir_map, self.cfg, self.entries = build_fixture(self.tmp.name)
        self.plan = make_plan(self.entries, self.settings)
        self.run_dir = os.path.join(self.tmp.name, "runs", "r1")

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self):
        return WorkspaceBuilder(self.settings).build(
            self.plan, self.run_dir, self.cfg, self.dir_map, run_id="r1")

    def test_creates_one_dir_per_wave(self):
        workspaces = self._build()
        self.assertEqual(len(workspaces), len(self.plan.waves))
        for workspace in workspaces:
            self.assertTrue(os.path.isdir(workspace.path))

    def test_snapshots_cfg_and_dir_map(self):
        """用快照而不是原檔執行 —— 三天後做 QA 用的必須是提交當下那份。"""
        workspace = self._build()[0]
        self.assertTrue(os.path.isfile(workspace.arcx_cfg))
        self.assertTrue(os.path.isfile(workspace.dir_map))
        self.assertEqual(sha256(workspace.arcx_cfg), sha256(self.cfg))

    def test_snapshot_survives_source_change(self):
        workspace = self._build()[0]
        before = sha256(workspace.arcx_cfg)
        with open(self.cfg, "a", encoding="utf-8") as handle:
            handle.write("\n# 有人改了原檔\n")
        self.assertEqual(sha256(workspace.arcx_cfg), before)
        self.assertNotEqual(sha256(self.cfg), before)

    def test_snapshots_special_cfg_per_index(self):
        """「當初 O_QCAP_LSF_NUM 設多少」是事後檢討分波的關鍵資訊。"""
        workspace = self._build()[0]
        with open(os.path.join(workspace.meta_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        specials = manifest["snapshots"]["special_cfg"]
        self.assertEqual(set(specials), set(workspace.index_keys))
        for info in specials.values():
            self.assertTrue(os.path.isfile(info["path"]))

    def test_manifest_records_intent(self):
        workspace = self._build()[0]
        with open(os.path.join(workspace.meta_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["index_keys"], list(workspace.index_keys))
        self.assertGreater(manifest["total_slots"], 0)

    def test_refuses_non_empty_target(self):
        """絕不覆蓋既有結果 —— 這是最後一道防線 (Preflight 是第一道)。"""
        self._build()
        with self.assertRaises(WorkspaceError):
            self._build()

    def test_missing_cfg_raises(self):
        with self.assertRaises(WorkspaceError):
            WorkspaceBuilder(self.settings).build(
                self.plan, self.run_dir, "/no/such/arcx.cfg", self.dir_map)

    def test_attempts_dir_created(self):
        """rerun 前備份失敗現場的地方, 提交時就先建好。"""
        workspace = self._build()[0]
        self.assertTrue(os.path.isdir(workspace.attempts_dir))


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------

class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.dir_map, self.cfg, self.entries = build_fixture(self.tmp.name)
        self.plan = make_plan(self.entries, self.settings)
        self.workspace = WorkspaceBuilder(self.settings).build(
            self.plan, os.path.join(self.tmp.name, "runs", "r1"),
            self.cfg, self.dir_map, run_id="r1")[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_command_uses_snapshot_cfg(self):
        lsf = FakeLsf()
        argv = Launcher(self.settings, lsf=lsf).build_command(
            self.workspace, "r1")
        self.assertIn(self.workspace.arcx_cfg, argv)
        self.assertNotIn(self.cfg, argv)

    def test_command_shape(self):
        argv = Launcher(self.settings, lsf=FakeLsf()).build_command(
            self.workspace, "r1")
        self.assertEqual(argv[0], "bsub")
        self.assertIn("-J", argv)
        self.assertIn("Arcx", argv)
        self.assertIn("--run", argv)
        self.assertEqual(argv[-1], "--run")

    def test_rerun_adds_keep_dir(self):
        argv = Launcher(self.settings, lsf=FakeLsf()).build_command(
            self.workspace, "r1", rerun=True)
        self.assertIn("-keep_dir", argv)

    def test_job_id_parsed_and_recorded(self):
        lsf = FakeLsf(job_id="98765")
        result = Launcher(self.settings, lsf=lsf).launch(self.workspace, "r1")
        self.assertTrue(result.ok)
        self.assertEqual(result.job_id, "98765")
        self.assertEqual(read_launch(self.workspace.path)["arcx_job_id"], "98765")

    def test_dry_run_does_not_execute(self):
        lsf = FakeLsf()
        result = Launcher(self.settings, lsf=lsf).launch(
            self.workspace, "r1", dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(lsf.commands, [])
        self.assertFalse(os.path.exists(self.workspace.launch_json))

    def test_failure_is_recorded_too(self):
        """失敗也要留紀錄 —— 否則事後看不出「有沒有送過」。"""
        lsf = FakeLsf(ok=False)
        result = Launcher(self.settings, lsf=lsf).launch(self.workspace, "r1")
        self.assertFalse(result.ok)
        record = read_launch(self.workspace.path)
        self.assertEqual(len(record["attempts"]), 1)
        self.assertIsNotNone(record["attempts"][0]["error"])

    def test_attempts_accumulate(self):
        launcher = Launcher(self.settings, lsf=FakeLsf())
        launcher.launch(self.workspace, "r1")
        launcher.launch(self.workspace, "r1", rerun=True)
        record = read_launch(self.workspace.path)
        self.assertEqual(len(record["attempts"]), 2)
        self.assertTrue(record["attempts"][1]["rerun"])


# ---------------------------------------------------------------------------
# Submitter 端到端
# ---------------------------------------------------------------------------

class SubmitterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.state_root = os.path.join(self.tmp.name, "state")
        self.settings.run_root = os.path.join(self.tmp.name, "runs")
        self.settings.gate.min_interval_sec = 0
        self.settings.preflight.min_disk_free_ratio = 0.0
        self.settings.preflight.warn_disk_free_ratio = 0.0
        self.dir_map, self.cfg, self.entries = build_fixture(self.tmp.name)
        self.plan = make_plan(self.entries, self.settings)
        self.lsf = FakeLsf()

    def tearDown(self):
        self.tmp.cleanup()

    def _submit(self, **kwargs):
        submitter = Submitter(
            self.settings, lsf=self.lsf,
            launcher=Launcher(self.settings, lsf=self.lsf),
            sleep=lambda _s: None)
        kwargs.setdefault("run_id", "r1")
        kwargs.setdefault("run_root", self.settings.run_root)
        return submitter.submit(
            plan=self.plan, arcx_cfg=self.cfg, dir_map=self.dir_map, **kwargs)

    def test_dry_run_writes_nothing(self):
        """dry-run 的意義就在於完全不碰磁碟。"""
        outcome = self._submit(dry_run=True)
        self.assertFalse(os.path.exists(self.settings.run_root))
        self.assertEqual(self.lsf.commands, [])
        self.assertTrue(all(l.dry_run for l in outcome.launches))

    def test_dry_run_shows_every_wave(self):
        """預覽不該在閘門前停下來 —— 目的就是一次看完全部。"""
        outcome = self._submit(dry_run=True)
        self.assertEqual(len(outcome.launches), len(self.plan.waves))

    def test_real_submit_creates_workspaces_and_submits(self):
        outcome = self._submit(dry_run=False)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(outcome.workspaces), len(self.plan.waves))
        self.assertEqual(len(self.lsf.commands), len(self.plan.waves))

    def test_max_waves_limits_submission(self):
        outcome = self._submit(dry_run=False, max_waves=1)
        self.assertEqual(len(self.lsf.commands), 1)
        self.assertEqual(len(outcome.pending_waves), len(self.plan.waves) - 1)

    def test_blocked_by_preflight_writes_nothing(self):
        """有 FATAL 就完全不動手 —— 不留半成品。"""
        self.lsf.available = False
        outcome = self._submit(dry_run=False)
        self.assertTrue(outcome.blocked)
        self.assertFalse(os.path.exists(self.settings.run_root))
        self.assertEqual(self.lsf.commands, [])

    def test_blocked_by_bad_cfg_writes_nothing(self):
        with open(self.cfg, "w", encoding="utf-8") as handle:
            handle.write("1 BEGIN_SETTING : x\n1 QC_FLOW = calMYSTERY\n"
                         "END_SETTINGS\n")
        outcome = self._submit(dry_run=False)
        self.assertTrue(outcome.blocked)
        self.assertIn("CFG_UNKNOWN_FLOW",
                      {i.id for i in outcome.cfg_check.issues})
        self.assertFalse(os.path.exists(self.settings.run_root))

    def test_existing_target_blocks_second_submit(self):
        self._submit(dry_run=False)
        second = self._submit(dry_run=False)
        self.assertTrue(second.blocked)
        self.assertIn("PREFLIGHT_TARGET_EXISTS",
                      {i.id for i in second.preflight.issues})

    def test_gate_blocks_without_wait(self):
        self.settings.gate.quota_threshold = 1
        self.settings.gate.max_wait_sec = 10 ** 9
        self.lsf.njobs = 500
        outcome = self._submit(dry_run=False, wait_for_gate=False)
        self.assertEqual(self.lsf.commands, [])
        self.assertEqual(len(outcome.pending_waves), len(self.plan.waves))

    def test_audit_records_every_write(self):
        from arcx_auto.adapters.store import RunStore

        self._submit(dry_run=False)
        store = RunStore(self.settings.expanded_state_root(), "r1")
        with open(store.audit_path, encoding="utf-8") as handle:
            actions = [json.loads(line)["action"] for line in handle]
        self.assertIn("workspace_built", actions)
        self.assertEqual(actions.count("wave_submitted"), len(self.plan.waves))

    def test_submission_state_visible_to_ui(self):
        from arcx_auto.adapters.store import RunStore

        self._submit(dry_run=False)
        state = RunStore(self.settings.expanded_state_root(), "r1").read_state()
        self.assertIn("submission", state)
        self.assertEqual(len(state["submission"]["waves"]),
                         len(self.plan.waves))

    def test_check_only_does_not_write(self):
        submitter = Submitter(self.settings, lsf=self.lsf)
        outcome = submitter.check(
            self.plan, os.path.join(self.settings.run_root, "r1"), self.cfg)
        self.assertFalse(outcome.blocked)
        self.assertFalse(os.path.exists(self.settings.run_root))


class PreflightCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.preflight.min_disk_free_ratio = 0.0
        self.settings.preflight.warn_disk_free_ratio = 0.0
        self.dir_map, self.cfg, self.entries = build_fixture(self.tmp.name)
        self.plan = make_plan(self.entries, self.settings)
        self.run_dir = os.path.join(self.tmp.name, "runs", "r1")

    def tearDown(self):
        self.tmp.cleanup()

    def _ids(self, lsf=None, plan=None):
        result = QaRunner(self.settings).run_preflight(
            plan or self.plan, self.run_dir,
            arcx_config=parse_arcx_cfg(self.cfg),
            lsf=lsf or FakeLsf(),
            run_root=os.path.dirname(self.run_dir))
        return {i.id for i in result.issues}

    def test_clean_plan_passes(self):
        self.assertEqual(self._ids(), set())

    def test_lsf_missing_is_fatal(self):
        self.assertIn("PREFLIGHT_LSF_UNAVAILABLE",
                      self._ids(lsf=FakeLsf(available=False)))

    def test_high_quota_warns(self):
        self.assertIn("PREFLIGHT_QUOTA_HIGH", self._ids(lsf=FakeLsf(njobs=95)))

    def test_excluded_index_warns(self):
        broken = IndexSpec(index_key="9999", path="/nope", gds_count=0,
                           cpu_per_case=0, error="沒有 GDS")
        from arcx_auto.adapters.arcx import ArcxAdapter

        arcx = ArcxAdapter(self.settings.layout, self.settings.plan)
        specs = [arcx.build_index_spec(k, p)
                 for k, p in sorted(self.entries.items())] + [broken]
        plan = plan_waves(specs, max_slots_per_wave=100)
        self.assertIn("PREFLIGHT_INDEX_EXCLUDED", self._ids(plan=plan))

    def test_no_waves_is_fatal(self):
        plan = plan_waves([], max_slots_per_wave=100)
        self.assertIn("PREFLIGHT_NO_WAVES", self._ids(plan=plan))

    def test_relative_path_in_cfg_is_fatal(self):
        """cfg 會被複製到 wave 目錄, 相對路徑在那裡會解析成不同的東西。"""
        with open(self.cfg, "w", encoding="utf-8") as handle:
            handle.write("1 BEGIN_SETTING : x\n1 QC_FLOW = calQCAP\n"
                         "1 RCX_TECH_QTF = ./relative.qtf\nEND_SETTINGS\n")
        self.assertIn("PREFLIGHT_CFG_RELATIVE_PATH", self._ids())

    def test_disabled_relative_path_ignored(self):
        with open(self.cfg, "w", encoding="utf-8") as handle:
            handle.write("1 BEGIN_SETTING : x\n1 QC_FLOW = calQCAP\n"
                         "0 RCX_TECH_QTF = ./relative.qtf\nEND_SETTINGS\n")
        self.assertNotIn("PREFLIGHT_CFG_RELATIVE_PATH", self._ids())

    def test_non_empty_target_is_fatal(self):
        os.makedirs(os.path.join(self.run_dir, "wave_001"))
        open(os.path.join(self.run_dir, "wave_001", "x"), "w").close()
        self.assertIn("PREFLIGHT_TARGET_EXISTS", self._ids())

    def test_index_reuse_warns(self):
        """同一 index 出現在既有的 wave 中 —— 警告但不阻擋, 證據交給人判斷。"""
        other = os.path.join(os.path.dirname(self.run_dir), "older", "wave_001",
                             ".arcx_auto")
        os.makedirs(other)
        with open(os.path.join(other, "manifest.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"index_keys": ["1000"], "created_at": 1.0}, handle)
        self.assertIn("PREFLIGHT_INDEX_IN_USE", self._ids())


if __name__ == "__main__":
    unittest.main()
