"""Execute a rerun: stop, drain, verify quiet, back up, clean, resubmit.

**This is the only component in the system that deletes files.** Every design
choice here trades speed for the ability to stop safely:

    STOPPING_PARENT   bkill <arcx_job_id>        kill the parent first, or it
                                                 simply submits replacements
    DRAINING          bjobs_manage.py -djp, wait, recount; up to N times,
                      because the deletion can lag behind the request
    VERIFY_QUIESCENT  [SAFETY GATE] K consecutive readings of zero jobs with
                      the marker set unchanged. Anything else aborts.
    DECIDE_CLEAN_SET  from the plan, which was computed before we touched
                      anything
    BACKUP            move the unfinished run dirs into .arcx_auto/attempts/N/
    CLEAN             (the move above is the deletion; nothing is unlinked)
    RESUBMIT          bsub "... -keep_dir --run"

Two rules that are never bent:

  * **An unreadable job count is unknown, never zero.** bjobs_manage.py -jp
    reports a count; if it cannot be parsed we abort. Treating "could not tell"
    as "nothing left" would delete files while jobs are still running, which is
    the most destructive mistake this system could make.

  * **Backup is a move, not a delete.** The unfinished run dirs are relocated
    into attempts/N/ rather than removed, so the failed state survives for
    debugging. It is also instant, because it stays on the same filesystem.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

from arcx_auto.adapters.lock import FileLock, LockBusy
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import Settings
from arcx_auto.domain.rerun import (
    DrainAttempt,
    QuiescentCheck,
    RerunOutcome,
    RerunPhase,
    RerunPlan,
)
from arcx_auto.services.launcher import Launcher
from arcx_auto.services.workspace import WaveWorkspace

Progress = Callable[[str], None]


class Remediator:
    """Runs a rerun plan."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        lsf: Optional[LsfAdapter] = None,
        launcher: Optional[Launcher] = None,
        store: Optional[RunStore] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.lsf = lsf or LsfAdapter(self.settings.lsf)
        self.launcher = launcher or Launcher(self.settings, lsf=self.lsf)
        self.store = store
        self.sleep = sleep or time.sleep

    # ------------------------------------------------------------------

    def run(
        self,
        plan: RerunPlan,
        run_id: str = "",
        dry_run: bool = False,
        on_progress: Optional[Progress] = None,
    ) -> RerunOutcome:
        """Execute the plan. With dry_run nothing is stopped, moved or sent."""
        say = on_progress or (lambda _msg: None)
        outcome = RerunOutcome(plan=plan, dry_run=dry_run)

        if plan.blockers:
            return replace(
                outcome, phase=RerunPhase.ABORTED,
                error="; ".join(plan.blockers))

        if not plan.to_delete:
            return replace(
                outcome, phase=RerunPhase.MONITORING,
                error=None)

        if dry_run:
            say("dry run: nothing will be stopped, moved or submitted")
            return replace(outcome, phase=RerunPhase.REQUESTED)

        # One wave directory, one writer. Without this two reruns started a
        # minute apart both pass the quiescent gate, both move the same run
        # dirs aside, and both resubmit -- leaving two Arcx parents writing
        # into one wave. The lock lives in the wave dir itself, so it covers
        # every machine that mounts the share.
        lock = FileLock(
            os.path.join(plan.wave_dir, ".arcx_auto", "lock"),
            purpose="arcx-auto rerun wave=%s attempt=%d"
                    % (plan.wave_name, plan.attempt),
        )
        try:
            lock.acquire()
        except LockBusy as exc:
            message = "this wave is already being worked on: %s" % exc
            say(message)
            self._audit(run_id, "rerun_lock_busy", plan, {"error": str(exc)})
            return replace(outcome, phase=RerunPhase.ABORTED, error=message)
        except OSError as exc:
            message = "could not take the wave lock: %s" % exc
            say(message)
            return replace(outcome, phase=RerunPhase.ABORTED, error=message)

        try:
            return self._run_locked(outcome, plan, run_id, say)
        finally:
            lock.release()

    def _run_locked(self, outcome: RerunOutcome, plan: RerunPlan,
                    run_id: str, say: Progress) -> RerunOutcome:
        """The destructive part, with the wave lock held."""
        self._audit(run_id, "rerun_started", plan, {
            "delete": [d.case_id for d in plan.to_delete],
            "uncertain": [d.case_id for d in plan.uncertain],
        })

        # -- 1. stop the parent ---------------------------------------
        outcome = replace(outcome, phase=RerunPhase.STOPPING_PARENT)
        say("stopping the parent Arcx job %s" % plan.arcx_job_id)
        killed, error = self.lsf.kill_job(str(plan.arcx_job_id))
        if not killed:
            # bkill failing on an already-finished job is normal, so this is
            # not fatal by itself; the quiescent gate is what actually decides.
            say("  bkill reported: %s" % error)
        outcome = replace(outcome, parent_killed=killed)
        self._audit(run_id, "rerun_parent_stopped", plan,
                    {"job_id": plan.arcx_job_id, "ok": killed, "error": error})

        # -- 2. drain the children ------------------------------------
        outcome = replace(outcome, phase=RerunPhase.DRAINING)
        outcome, drained = self._drain(outcome, plan, run_id, say)
        if not drained:
            return replace(outcome, phase=RerunPhase.ABORTED,
                           error=outcome.error or "could not drain the jobs")

        # -- 3. the safety gate ---------------------------------------
        outcome = replace(outcome, phase=RerunPhase.VERIFY_QUIESCENT)
        outcome, quiet = self._verify_quiescent(outcome, plan, run_id, say)
        if not quiet:
            return replace(outcome, phase=RerunPhase.ABORTED,
                           error=outcome.error or "the wave never went quiet")

        # -- 4. back up and clean -------------------------------------
        outcome = replace(outcome, phase=RerunPhase.BACKUP)
        outcome = self._backup_and_clean(outcome, plan, run_id, say)
        if outcome.error:
            return replace(outcome, phase=RerunPhase.ABORTED)

        # -- 5. resubmit ----------------------------------------------
        outcome = replace(outcome, phase=RerunPhase.RESUBMIT)
        return self._resubmit(outcome, plan, run_id, say)

    # ------------------------------------------------------------------
    # 2. Draining
    # ------------------------------------------------------------------

    def _drain(self, outcome: RerunOutcome, plan: RerunPlan, run_id: str,
               say: Progress) -> Tuple[RerunOutcome, bool]:
        """Delete the jobs under the wave directory, then confirm none are left.

        Reissued up to drain_attempts times because the deletion can lag: LSF
        acknowledges the request before every job has actually gone.
        """
        settings = self.settings.lsf
        attempts: List[DrainAttempt] = []

        for attempt in range(1, settings.drain_attempts + 1):
            say("draining jobs under %s (attempt %d/%d)"
                % (plan.wave_dir, attempt, settings.drain_attempts))
            deleted_ok, _raw, delete_error = self.lsf.kill_jobs_under_path(
                plan.wave_dir)

            self.sleep(settings.drain_retry_delay_sec)

            remaining, raw, count_error = self.lsf.count_jobs_under_path(
                plan.wave_dir)
            attempts.append(DrainAttempt(
                attempt=attempt,
                deleted_ok=deleted_ok,
                remaining=remaining,
                error=delete_error or count_error,
                raw_output=raw,
            ))
            outcome = replace(outcome, drain_attempts=tuple(attempts))

            if count_error or remaining is None:
                # Unknown, never zero. Stop here rather than guess.
                message = ("could not read the job count under the path: %s"
                           % (count_error or "unrecognised output"))
                say("  " + message)
                self._audit(run_id, "rerun_drain_unreadable", plan,
                            {"attempt": attempt, "raw": raw[:500]})
                return replace(outcome, error=message), False

            say("  %d job(s) remaining" % remaining)
            if remaining == 0:
                self._audit(run_id, "rerun_drained", plan, {"attempts": attempt})
                return outcome, True

        message = ("%d job(s) still present after %d drain attempts"
                   % (attempts[-1].remaining, settings.drain_attempts))
        say("  " + message)
        self._audit(run_id, "rerun_drain_failed", plan,
                    {"remaining": attempts[-1].remaining})
        return replace(outcome, error=message), False

    # ------------------------------------------------------------------
    # 3. The safety gate
    # ------------------------------------------------------------------

    def _verify_quiescent(self, outcome: RerunOutcome, plan: RerunPlan,
                          run_id: str, say: Progress) -> Tuple[RerunOutcome, bool]:
        """Require K consecutive readings of zero jobs with markers unchanged.

        Draining reported zero once; this makes sure it stays zero and that
        nothing is still writing into the directory. A changed marker set means
        something is alive, so the confirmation count restarts.

        There is no way past this gate. Everything after it deletes files.
        """
        settings = self.settings.lsf
        deadline = time.time() + settings.quiescent_timeout_sec
        checks: List[QuiescentCheck] = []
        marker_re = re.compile(self.settings.layout.marker_any_regex)
        previous_markers = _marker_fingerprint(plan.wave_dir, marker_re)
        confirmed = 0

        while confirmed < settings.quiescent_confirm_times:
            if time.time() > deadline:
                message = ("the wave did not stay quiet within %.0f seconds"
                           % settings.quiescent_timeout_sec)
                say("  " + message)
                self._audit(run_id, "rerun_quiescent_timeout", plan, {})
                return replace(outcome, error=message), False

            self.sleep(settings.quiescent_interval_sec)

            jobs, _raw, error = self.lsf.count_jobs_under_path(plan.wave_dir)
            markers = _marker_fingerprint(plan.wave_dir, marker_re)
            changed = markers != previous_markers
            previous_markers = markers

            checks.append(QuiescentCheck(
                index=len(checks) + 1, jobs=jobs,
                markers_changed=changed, error=error))
            outcome = replace(outcome, quiescent_checks=tuple(checks))

            if error or jobs is None:
                message = ("could not read the job count during verification: %s"
                           % (error or "unrecognised output"))
                say("  " + message)
                self._audit(run_id, "rerun_quiescent_unreadable", plan, {})
                return replace(outcome, error=message), False

            if jobs != 0:
                message = ("%d job(s) reappeared under the path; something is "
                           "still submitting" % jobs)
                say("  " + message)
                self._audit(run_id, "rerun_quiescent_jobs_returned", plan,
                            {"jobs": jobs})
                return replace(outcome, error=message), False

            if changed:
                say("  markers changed; restarting the confirmation count")
                confirmed = 0
                continue

            confirmed += 1
            say("  quiet confirmation %d/%d"
                % (confirmed, settings.quiescent_confirm_times))

        self._audit(run_id, "rerun_quiescent_confirmed", plan,
                    {"confirmations": confirmed})
        return outcome, True

    # ------------------------------------------------------------------
    # 4. Backup and clean
    # ------------------------------------------------------------------

    def _backup_and_clean(self, outcome: RerunOutcome, plan: RerunPlan,
                          run_id: str, say: Progress) -> RerunOutcome:
        """Move the unfinished run dirs into .arcx_auto/attempts/N/.

        Moving rather than copying then deleting: it preserves the failed state
        for debugging, it frees the name so the rerun can recreate it, and it is
        instant because attempts/ lives on the same filesystem.
        """
        attempts_dir = os.path.join(
            plan.wave_dir, ".arcx_auto", "attempts", str(plan.attempt))
        moved: List[str] = []

        for decision in plan.to_delete:
            if not decision.case_dir or not os.path.isdir(decision.case_dir):
                continue
            destination = os.path.join(
                attempts_dir, decision.index_key, decision.case_id)
            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.move(decision.case_dir, destination)
            except OSError as exc:
                message = ("could not move %s aside: %s"
                           % (decision.case_dir, exc))
                say("  " + message)
                self._audit(run_id, "rerun_backup_failed", plan,
                            {"case": decision.case_id, "error": str(exc)})
                return replace(outcome, error=message,
                               backed_up=tuple(moved), deleted=tuple(moved))
            moved.append(decision.case_dir)
            say("  moved %s/%s aside" % (decision.index_key, decision.case_id))

        self._audit(run_id, "rerun_cleaned", plan, {
            "moved": len(moved),
            "attempts_dir": attempts_dir,
            "cases": [d.case_id for d in plan.to_delete],
        })
        return replace(outcome, phase=RerunPhase.CLEAN,
                       backed_up=tuple(moved), deleted=tuple(moved))

    # ------------------------------------------------------------------
    # 5. Resubmit
    # ------------------------------------------------------------------

    def _resubmit(self, outcome: RerunOutcome, plan: RerunPlan, run_id: str,
                  say: Progress) -> RerunOutcome:
        """Resubmit with -keep_dir --run.

        Arcx skips the cases whose run dirs are still there and redoes the ones
        we moved aside.
        """
        workspace = WaveWorkspace(
            wave_name=plan.wave_name,
            path=plan.wave_dir,
            arcx_cfg=plan.arcx_cfg or os.path.join(plan.wave_dir, "arcx.cfg"),
            dir_map=os.path.join(plan.wave_dir, "dir_map"),
            meta_dir=os.path.join(plan.wave_dir, ".arcx_auto"),
            index_keys=plan.index_keys,
        )
        result = self.launcher.launch(workspace, run_id, rerun=True)

        self._audit(run_id,
                    "rerun_resubmitted" if result.ok else "rerun_resubmit_failed",
                    plan,
                    {"job_id": result.job_id, "command": list(result.command),
                     "error": result.error})

        if not result.ok:
            say("resubmission failed: %s" % result.error)
            return replace(outcome, phase=RerunPhase.ABORTED, error=result.error)

        say("resubmitted, job id = %s" % result.job_id)
        return replace(outcome, phase=RerunPhase.MONITORING,
                       resubmit_job_id=result.job_id)

    # ------------------------------------------------------------------

    def _audit(self, run_id: str, action: str, plan: RerunPlan,
               extra: Dict) -> None:
        if self.store is None:
            return
        record = {
            "action": action,
            "run_id": run_id,
            "wave": plan.wave_name,
            "wave_dir": plan.wave_dir,
            "attempt": plan.attempt,
            "reason": "user requested a rerun",
        }
        record.update(extra)
        self.store.append_audit(record)


def _marker_fingerprint(wave_dir: str, marker_re) -> Tuple[str, ...]:
    """A stable fingerprint of the markers under the wave directory.

    A change between readings means something is still writing, which is
    exactly what the gate must not miss.

    Two things this deliberately does **not** do:

      * It does not recurse. Markers live one level down, in the index run
        folders. Walking the whole tree would stat every netlist and every
        QC_* report on NFS, three times, inside a 15 minute deadline.

      * It does not treat every dot-file as a marker. NFS renames a file that
        is deleted while still open to `.nfs0000...`, and those appear exactly
        when jobs are being killed -- which is the moment this gate runs. A
        fingerprint that counted them would churn on every reading, the
        confirmation count would never reach K, and every rerun would abort at
        the safety gate for a reason that has nothing to do with safety.
    """
    found: List[str] = []
    try:
        index_dirs = [e.path for e in os.scandir(wave_dir)
                      if e.is_dir() and e.name != ".arcx_auto"]
    except OSError:
        return ()
    for index_dir in index_dirs:
        try:
            entries = list(os.scandir(index_dir))
        except OSError:
            # An unreadable directory is not proof of quiet. Feed a value that
            # differs from any real reading so the gate keeps waiting instead
            # of concluding "nothing changed".
            found.append("%s\0unreadable\0%f" % (index_dir, time.time()))
            continue
        for entry in entries:
            if marker_re.match(entry.name):
                found.append(entry.path)
    return tuple(sorted(found))
