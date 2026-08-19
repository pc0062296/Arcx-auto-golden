"""Wire preflight -> workspace -> launch -> gate into one submission.

This is the orchestration layer and the **first flow in the system that
writes to disk**, so every step follows the same rules:

  * any FATAL means nothing is touched at all -- no half-built state
  * every write action is recorded in the audit log
  * a dry run makes every decision but touches no disk, so what would
    happen can be seen first
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
    """The result of one submission."""

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
    """Runs one submission."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        lsf: Optional[LsfAdapter] = None,
        launcher: Optional[Launcher] = None,
        qa: Optional[QaRunner] = None,
        builder: Optional[WorkspaceBuilder] = None,
        sleep: Optional[Callable[[float], None]] = None,
        stop=None,
    ) -> None:
        self.settings = settings or Settings()
        self.lsf = lsf or LsfAdapter(self.settings.lsf)
        self.launcher = launcher or Launcher(self.settings, lsf=self.lsf)
        self.qa = qa or QaRunner(self.settings)
        self.builder = builder or WorkspaceBuilder(self.settings)
        # A threading.Event that means "give up waiting". The gate can hold a
        # submission for hours, and a plain time.sleep through that makes a
        # stop request look ignored.
        self.stop = stop
        self.sleep = sleep or self._interruptible_sleep

    def _interruptible_sleep(self, seconds: float) -> None:
        if self.stop is not None:
            self.stop.wait(seconds)
            return
        time.sleep(seconds)

    def _stopping(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    # ------------------------------------------------------------------

    def check(
        self,
        plan: WavePlan,
        run_dir: str,
        arcx_cfg: str,
        run_root: Optional[str] = None,
        now: Optional[float] = None,
    ) -> SubmitOutcome:
        """Checks only, no disk access. A dry run and a real submission
        share one checking path -- two paths would drift, and the drifted
        one that bites.
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
        """The full flow."""
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

        # --- Build the workspaces ------------------------------------
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
                "reason": "user submission",
            })
            say("created %d wave director(ies) under %s"
                % (len(outcome.workspaces), run_dir))

        # --- Submit wave by wave --------------------------------------
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
                    # A preview never actually waits: the point of a dry run
                    # is to see every wave in one go. Print the gate decision
                    # and carry on as if released.
                    say("%s would wait here: %s"
                        % (wave.wave_name, decision.reason))
                else:
                    say("%s waiting: %s" % (wave.wave_name, decision.reason))
                    if not wait_for_gate:
                        break
                    self.sleep(max(1.0, decision.wait_hint_sec))
                    if self._stopping():
                        outcome.error = ("stopped while waiting at the gate; "
                                         "nothing further was submitted")
                        break
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
                controller.mark_failed(
                    wave.wave_name, result.error or "submission failed")
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
                                    error="no workspace found")
            # dry run: a stand-in workspace, only to assemble the command
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
            "reason": "user submission",
            "error": result.error,
        })
        say("%s submitted, job id = %s" % (wave_name, result.job_id or "?")
            if result.ok else "%s submission failed: %s"
            % (wave_name, result.error))
        return result

    def _njobs(self) -> Optional[int]:
        value, _error = self.lsf.current_njobs()
        return value

    def _write_submission_state(self, store: RunStore, run_id: str, run_dir: str,
                                controller: SubmissionController,
                                outcome: SubmitOutcome) -> None:
        """Write submission progress into the run store for UI and daemon."""
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
    """A stand-in workspace for a dry run, only to assemble the command."""
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
