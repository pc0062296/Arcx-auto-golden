"""把 preflight → workspace → launch → gate 串成一次完整的提交。

這是 Phase 2b 的編排層。它是系統中**第一個會寫入磁碟的流程**, 所以
每一步都遵守同一套規則:

  * 有 FATAL 就完全不動手 —— 不留半成品
  * 每一個寫入型動作都寫進 audit
  * dry-run 走完全部決策但不碰磁碟, 讓人先看到會發生什麼
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from arcx_auto.adapters.arcx_cfg import ArcxConfig, parse_arcx_cfg
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import Settings
from arcx_auto.domain.models import WavePlan
from arcx_auto.domain.qa import QaResult
from arcx_auto.services.launcher import LaunchResult, Launcher
from arcx_auto.services.qa import QaRunner
from arcx_auto.services.submission import GateDecision, SubmissionController
from arcx_auto.services.workspace import WaveWorkspace, WorkspaceBuilder, WorkspaceError


@dataclass
class SubmitOutcome:
    """一次提交的結果。"""

    run_id: str
    run_dir: str
    plan: WavePlan
    cfg_check: Optional[QaResult] = None
    preflight: Optional[QaResult] = None
    workspaces: List[WaveWorkspace] = field(default_factory=list)
    launches: List[LaunchResult] = field(default_factory=list)
    pending_waves: List[str] = field(default_factory=list)
    blocked: bool = False
    dry_run: bool = False
    error: Optional[str] = None

    @property
    def submitted_count(self) -> int:
        return sum(1 for l in self.launches if l.ok and not l.dry_run)

    @property
    def ok(self) -> bool:
        return not self.blocked and self.error is None and all(
            l.ok for l in self.launches)


class Submitter:
    """執行一次提交。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        lsf: Optional[LsfAdapter] = None,
        launcher: Optional[Launcher] = None,
        qa: Optional[QaRunner] = None,
        builder: Optional[WorkspaceBuilder] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.lsf = lsf or LsfAdapter(self.settings.lsf)
        self.launcher = launcher or Launcher(self.settings, lsf=self.lsf)
        self.qa = qa or QaRunner(self.settings)
        self.builder = builder or WorkspaceBuilder(self.settings)
        self.sleep = sleep or time.sleep

    # ------------------------------------------------------------------

    def check(
        self,
        plan: WavePlan,
        run_dir: str,
        arcx_cfg: str,
        run_root: Optional[str] = None,
        now: Optional[float] = None,
    ) -> SubmitOutcome:
        """只跑檢查, 不碰磁碟。dry-run 與正式提交共用同一條檢查路徑 ——
        兩條路徑會走歪, 而走歪的那次就是出事的那次。
        """
        now = now if now is not None else time.time()
        config = parse_arcx_cfg(arcx_cfg) if arcx_cfg else None

        outcome = SubmitOutcome(
            run_id=os.path.basename(run_dir), run_dir=run_dir, plan=plan)
        outcome.cfg_check = self.qa.run_config(config, now=now)
        outcome.preflight = self.qa.run_preflight(
            plan, run_dir, arcx_config=config, lsf=self.lsf,
            run_root=run_root, now=now)
        outcome.blocked = bool(outcome.cfg_check.fatal or outcome.preflight.fatal)
        return outcome

    def submit(
        self,
        plan: WavePlan,
        run_id: str,
        run_root: str,
        arcx_cfg: str,
        dir_map: str,
        dry_run: bool = False,
        max_waves: Optional[int] = None,
        wait_for_gate: bool = True,
        on_progress: Optional[Callable[[str], None]] = None,
        now: Optional[float] = None,
    ) -> SubmitOutcome:
        """完整流程。"""
        now = now if now is not None else time.time()
        run_root = os.path.abspath(os.path.expanduser(run_root))
        run_dir = os.path.join(run_root, run_id)
        say = on_progress or (lambda _msg: None)

        outcome = self.check(plan, run_dir, arcx_cfg, run_root=run_root, now=now)
        outcome.run_id = run_id
        outcome.dry_run = dry_run
        if outcome.blocked:
            return outcome

        store = RunStore(self.settings.expanded_state_root(), run_id)
        store.ensure()

        # --- 建立 workspace ------------------------------------------
        if dry_run:
            outcome.workspaces = []
        else:
            try:
                outcome.workspaces = self.builder.build(
                    plan, run_dir, arcx_cfg, dir_map, run_id=run_id, now=now)
            except WorkspaceError as exc:
                outcome.error = str(exc)
                store.append_audit({
                    "action": "workspace_build_failed",
                    "run_id": run_id, "reason": str(exc)})
                return outcome
            store.append_audit({
                "action": "workspace_built",
                "run_id": run_id,
                "waves": [w.wave_name for w in outcome.workspaces],
                "reason": "使用者提交",
            })
            say("已建立 %d 個 wave 目錄於 %s" % (len(outcome.workspaces), run_dir))

        # --- 逐波提交 -------------------------------------------------
        controller = SubmissionController(
            [w.name for w in plan.waves], self.settings.gate)
        workspaces = {w.wave_name: w for w in outcome.workspaces}
        limit = max_waves if max_waves is not None else len(plan.waves)
        done = 0

        while done < limit:
            decision = controller.evaluate(njobs=self._njobs())
            if decision is None:
                break

            wave = controller.next_wave()
            assert wave is not None

            if not decision.allow:
                if dry_run:
                    # 預覽絕不真的等待 —— dry-run 的目的就是一次看完所有 wave
                    # 會執行什麼。照實印出閘門的判斷, 然後當作放行繼續往下走。
                    say("%s 實際執行時會等待: %s"
                        % (wave.wave_name, decision.reason))
                else:
                    say("%s 等待中: %s" % (wave.wave_name, decision.reason))
                    if not wait_for_gate:
                        break
                    self.sleep(max(1.0, decision.wait_hint_sec))
                    continue

            if decision.forced:
                say("%s %s: %s" % (wave.wave_name, decision.label, decision.reason))
                store.append_audit({
                    "action": "gate_forced_release",
                    "run_id": run_id, "wave": wave.wave_name,
                    "reason": decision.reason,
                })

            result = self._launch_one(
                wave.wave_name, workspaces.get(wave.wave_name), run_id,
                plan, dry_run, store, say)
            outcome.launches.append(result)

            if result.ok:
                controller.mark_submitted(wave.wave_name, result.job_id)
            else:
                controller.mark_failed(wave.wave_name, result.error or "提交失敗")
                outcome.error = result.error
                break
            done += 1

        outcome.pending_waves = [p.wave_name for p in controller.pending()]
        self._write_submission_state(store, run_id, run_dir, controller, outcome)
        return outcome

    # ------------------------------------------------------------------

    def _launch_one(self, wave_name: str, workspace: Optional[WaveWorkspace],
                    run_id: str, plan: WavePlan, dry_run: bool,
                    store: RunStore, say) -> LaunchResult:
        if workspace is None:
            if not dry_run:
                return LaunchResult(wave_name=wave_name, ok=False,
                                    error="找不到 workspace")
            # dry-run: 用假的 workspace 只為了組出指令給人看
            workspace = _preview_workspace(wave_name, plan, run_id, self.settings)

        result = self.launcher.launch(workspace, run_id, dry_run=dry_run)
        if dry_run:
            say("%s (dry-run): %s" % (wave_name, " ".join(result.command)))
            return result

        store.append_audit({
            "action": "wave_submitted" if result.ok else "wave_submit_failed",
            "run_id": run_id,
            "wave": wave_name,
            "job_id": result.job_id,
            "command": list(result.command),
            "reason": "使用者提交",
            "error": result.error,
        })
        say("%s 已提交, job id = %s" % (wave_name, result.job_id or "?")
            if result.ok else "%s 提交失敗: %s" % (wave_name, result.error))
        return result

    def _njobs(self) -> Optional[int]:
        value, _error = self.lsf.current_njobs()
        return value

    def _write_submission_state(self, store: RunStore, run_id: str, run_dir: str,
                                controller: SubmissionController,
                                outcome: SubmitOutcome) -> None:
        """把提交進度寫進 run store, 讓 Web UI 與 daemon 看得到。"""
        if outcome.dry_run:
            return
        state = store.read_state()
        state.setdefault("run_id", run_id)
        state["submission"] = {
            "run_dir": run_dir,
            "updated_at": time.time(),
            "waves": controller.to_json(),
            "pending": list(outcome.pending_waves),
        }
        store.write_state(state)


def _preview_workspace(wave_name: str, plan: WavePlan, run_id: str,
                       settings: Settings) -> WaveWorkspace:
    """dry-run 用的假 workspace —— 只是為了把指令組出來給人看。"""
    wave = next((w for w in plan.waves if w.name == wave_name), None)
    keys = wave.index_keys if wave else ()
    base = os.path.join(settings.expanded_run_root(), run_id, wave_name)
    return WaveWorkspace(
        wave_name=wave_name,
        path=base,
        arcx_cfg=os.path.join(base, "arcx.cfg"),
        dir_map=os.path.join(base, "dir_map"),
        meta_dir=os.path.join(base, ".arcx_auto"),
        index_keys=keys,
    )
