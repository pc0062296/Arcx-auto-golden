"""Phase 3: the rerun plan and the drain state machine.

This is the only code path in the system that removes files, so the tests
concentrate on **when it refuses to act**: an unreadable job count, jobs coming
back, markers still moving, a missing parent job id. Each of those must abort
before anything is touched.
"""

import json
import os
import tempfile
import unittest

from arcx_auto.config.settings import Settings
from arcx_auto.domain.enums import CaseState, Completeness
from arcx_auto.domain.rerun import RerunPhase
from arcx_auto.services.qa import QaRunner
from arcx_auto.services.remediator import Remediator
from arcx_auto.services.rerun_planner import build_rerun_plan
from arcx_auto.services.state_engine import TransitionContext, transition_index_run
from arcx_auto.adapters.arcx_cfg import parse_arcx_cfg
from arcx_auto.adapters.fs import FsAdapter
from tests.fixtures.fake_run import CaseSpec, make_arcx_cfg, make_index_run_folder


class FakeLsf:
    """A fake LSF whose job count follows a script.

    ``counts`` is the sequence returned by successive -jp calls, so a test can
    describe exactly how the cluster behaves: draining slowly, going quiet, or
    handing back something unreadable (None).
    """

    def __init__(self, counts, kill_ok=True, submit_ok=True, job_id="55555"):
        self.counts = list(counts)
        self.kill_ok = kill_ok
        self.submit_ok = submit_ok
        self.job_id = job_id
        self.killed_jobs = []
        self.deleted_paths = []
        self.count_calls = 0

    def is_available(self, command=None):
        return True

    def current_njobs(self):
        return (1, None)

    def kill_job(self, job_id):
        self.killed_jobs.append(job_id)
        return (self.kill_ok, None if self.kill_ok else "bkill failed")

    def kill_jobs_under_path(self, path):
        self.deleted_paths.append(path)
        return (True, "", None)

    def count_jobs_under_path(self, path):
        self.count_calls += 1
        value = self.counts.pop(0) if self.counts else 0
        if value is None:
            return (None, "unrecognised output", None)
        return (value, "total %d jobs in path" % value, None)

    def _run(self, argv, cwd=None):
        from arcx_auto.adapters.lsf import CommandResult

        if not self.submit_ok:
            return CommandResult(False, "", "queue is closed", 1, None)
        return CommandResult(
            True, "Job <%s> is submitted to queue <q>.\n" % self.job_id,
            "", 0, None)


def build_wave(tmp, cases, launch=True):
    """Build a wave directory that already looks like a finished Arcx run."""
    wave = os.path.join(tmp, "runs", "demo", "wave_001")
    os.makedirs(wave, exist_ok=True)
    cfg = make_arcx_cfg(os.path.join(wave, "arcx.cfg"))
    open(os.path.join(wave, "dir_map"), "w").close()
    make_index_run_folder(wave, "1000", cases)

    meta = os.path.join(wave, ".arcx_auto")
    os.makedirs(meta, exist_ok=True)
    with open(os.path.join(meta, "manifest.json"), "w", encoding="utf-8") as h:
        json.dump({"wave": "wave_001", "index_keys": ["1000"],
                   "snapshots": {"arcx_cfg": cfg}}, h)
    if launch:
        with open(os.path.join(meta, "launch.json"), "w", encoding="utf-8") as h:
            json.dump({"run_id": "demo", "wave": "wave_001",
                       "index_keys": ["1000"], "arcx_job_id": "68905",
                       "attempts": [{"attempt": 1, "job_id": "68905"}]}, h)
    return wave, cfg


def observe(wave, cfg, settings=None):
    """Scan the wave the way the monitor does, and return (snapshots, reports)."""
    settings = settings or Settings()
    fs = FsAdapter(settings.layout)
    folder = os.path.join(wave, "1000_run")
    observation = fs.scan_index_run_folder(folder, "1000")
    ctx = TransitionContext(now=observation.observed_at)
    snapshot, _ = transition_index_run(None, observation, ctx)
    report = QaRunner(settings).run_index(
        snapshot, observation, parse_arcx_cfg(cfg))
    return [snapshot], [report]


def plan_for(wave, cfg, settings=None):
    snapshots, reports = observe(wave, cfg, settings)
    meta = os.path.join(wave, ".arcx_auto")
    launch = {}
    launch_path = os.path.join(meta, "launch.json")
    if os.path.isfile(launch_path):
        with open(launch_path, encoding="utf-8") as handle:
            launch = json.load(handle)
    with open(os.path.join(meta, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    return build_rerun_plan(wave, "wave_001", snapshots, reports,
                            launch=launch, manifest=manifest)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_keeps_a_real_success(self):
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NTN_1", "complete", artifacts="full")])
        plan = plan_for(wave, cfg)
        self.assertEqual([d.case_id for d in plan.to_keep], ["NTN_1"])
        self.assertEqual(plan.to_delete, ())

    def test_deletes_a_false_success(self):
        """A .complete marker with no netlist behind it."""
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist")])
        plan = plan_for(wave, cfg)
        self.assertEqual([d.case_id for d in plan.to_delete], ["NDIO_1"])

    def test_deletes_a_running_case_despite_a_clean_qa_result(self):
        """QA alone must not be able to call a case complete.

        A RUNNING case has only had the LIVE checks run against it, so finding
        no issues means "nothing looks wrong right now", not "it finished".
        """
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("PDIO_1", "running", artifacts="none")])
        plan = plan_for(wave, cfg)
        self.assertEqual([d.case_id for d in plan.to_delete], ["PDIO_1"])
        self.assertIn("RUNNING", plan.to_delete[0].reason)

    def test_deletes_a_queued_case(self):
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NDIO_1", "queued", artifacts="none")])
        plan = plan_for(wave, cfg)
        self.assertEqual([d.case_id for d in plan.to_delete], ["NDIO_1"])

    def test_uncertain_cases_are_deleted_and_flagged(self):
        """Inconsistent markers cannot be judged, so they are deleted and rerun,
        but they are marked so a human can override.
        """
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NMOS_1", "inconsistent", artifacts="full")])
        plan = plan_for(wave, cfg)
        self.assertEqual([d.case_id for d in plan.uncertain], ["NMOS_1"])
        self.assertEqual([d.case_id for d in plan.to_delete], ["NMOS_1"])
        self.assertTrue(any("could not be judged" in w for w in plan.warnings))

    def test_mixed_wave(self):
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NTN_1", "complete", artifacts="full"),
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist"),
            CaseSpec("PDIO_1", "running", artifacts="none"),
        ])
        plan = plan_for(wave, cfg)
        self.assertEqual(sorted(d.case_id for d in plan.to_delete),
                         ["NDIO_1", "PDIO_1"])
        self.assertEqual([d.case_id for d in plan.to_keep], ["NTN_1"])

    def test_missing_parent_job_id_blocks(self):
        """Without the parent's id it cannot be stopped, and a live parent
        simply resubmits everything we drain.
        """
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist")],
            launch=False)
        plan = plan_for(wave, cfg)
        self.assertTrue(plan.blockers)
        self.assertFalse(plan.executable)
        self.assertTrue(any("arcx_job_id" in b for b in plan.blockers))

    def test_attempt_number_follows_launch_history(self):
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist")])
        self.assertEqual(plan_for(wave, cfg).attempt, 2)

    def test_nothing_to_do_is_not_an_error(self):
        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NTN_1", "complete", artifacts="full")])
        plan = plan_for(wave, cfg)
        self.assertFalse(plan.executable)
        self.assertEqual(plan.blockers, ())
        self.assertTrue(any("nothing needs rerunning" in w
                            for w in plan.warnings))


# ---------------------------------------------------------------------------
# Execution and the safety gate
# ---------------------------------------------------------------------------

class RemediatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.lsf.drain_retry_delay_sec = 0
        self.settings.lsf.quiescent_interval_sec = 0
        self.wave, self.cfg = build_wave(self.tmp.name, [
            CaseSpec("NTN_1", "complete", artifacts="full"),
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist"),
        ])
        self.plan = plan_for(self.wave, self.cfg, self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, lsf, **kwargs):
        from arcx_auto.services.launcher import Launcher

        remediator = Remediator(
            self.settings, lsf=lsf,
            launcher=Launcher(self.settings, lsf=lsf),
            sleep=lambda _s: None)
        return remediator.run(self.plan, run_id="demo", **kwargs)

    def _case_dirs(self):
        return sorted(n for n in os.listdir(os.path.join(self.wave, "1000_run"))
                      if not n.startswith(("QC_", "cmd_", "submit_", ".")))

    # -- the happy path ------------------------------------------------

    def test_full_sequence(self):
        lsf = FakeLsf(counts=[0, 0, 0, 0])
        outcome = self._run(lsf)
        self.assertEqual(outcome.phase, RerunPhase.MONITORING)
        self.assertTrue(outcome.ok)
        self.assertEqual(lsf.killed_jobs, ["68905"])
        self.assertEqual(outcome.resubmit_job_id, "55555")

    def test_parent_is_stopped_before_the_children(self):
        """The other order lets the parent submit replacements."""
        order = []

        class Recording(FakeLsf):
            def kill_job(self, job_id):
                order.append("parent")
                return super().kill_job(job_id)

            def kill_jobs_under_path(self, path):
                order.append("children")
                return super().kill_jobs_under_path(path)

        self._run(Recording(counts=[0, 0, 0, 0]))
        self.assertEqual(order[:2], ["parent", "children"])

    def test_unfinished_case_is_moved_aside_and_the_finished_one_stays(self):
        self._run(FakeLsf(counts=[0, 0, 0, 0]))
        self.assertEqual(self._case_dirs(), ["NTN_1"])
        backup = os.path.join(self.wave, ".arcx_auto", "attempts", "2",
                              "1000", "NDIO_1")
        self.assertTrue(os.path.isdir(backup))

    def test_backup_preserves_the_failed_state(self):
        """The move keeps the evidence rather than unlinking it."""
        self._run(FakeLsf(counts=[0, 0, 0, 0]))
        backup = os.path.join(self.wave, ".arcx_auto", "attempts", "2",
                              "1000", "NDIO_1")
        self.assertTrue(os.listdir(backup))

    def test_resubmit_carries_keep_dir(self):
        from arcx_auto.services.launcher import read_launch

        self._run(FakeLsf(counts=[0, 0, 0, 0]))
        attempts = read_launch(self.wave)["attempts"]
        self.assertTrue(attempts[-1]["rerun"])
        self.assertIn("-keep_dir", " ".join(attempts[-1]["command"]))

    # -- draining ------------------------------------------------------

    def test_drain_retries_because_deletion_lags(self):
        """LSF acknowledges the delete before every job has actually gone."""
        lsf = FakeLsf(counts=[3, 0, 0, 0, 0])
        outcome = self._run(lsf)
        self.assertEqual([a.remaining for a in outcome.drain_attempts], [3, 0])
        self.assertEqual(len(lsf.deleted_paths), 2)
        self.assertTrue(outcome.ok)

    def test_drain_gives_up_after_the_attempt_limit(self):
        outcome = self._run(FakeLsf(counts=[5, 4, 3]))
        self.assertEqual(outcome.phase, RerunPhase.ABORTED)
        self.assertIn("still present", outcome.error)
        self.assertEqual(self._case_dirs(), ["NDIO_1", "NTN_1"])

    def test_unreadable_count_aborts_and_never_reads_as_zero(self):
        """The single most destructive mistake would be deleting files while
        jobs are still running, so an unparseable count stops everything.
        """
        outcome = self._run(FakeLsf(counts=[None]))
        self.assertEqual(outcome.phase, RerunPhase.ABORTED)
        self.assertIn("could not read the job count", outcome.error)
        self.assertEqual(self._case_dirs(), ["NDIO_1", "NTN_1"])

    # -- the quiescent gate --------------------------------------------

    def test_jobs_reappearing_aborts(self):
        """Draining said zero, then something submitted again."""
        outcome = self._run(FakeLsf(counts=[0, 0, 2]))
        self.assertEqual(outcome.phase, RerunPhase.ABORTED)
        self.assertIn("reappeared", outcome.error)
        self.assertEqual(self._case_dirs(), ["NDIO_1", "NTN_1"])

    def test_unreadable_count_during_verification_aborts(self):
        outcome = self._run(FakeLsf(counts=[0, 0, None]))
        self.assertEqual(outcome.phase, RerunPhase.ABORTED)
        self.assertEqual(self._case_dirs(), ["NDIO_1", "NTN_1"])

    def test_confirmations_restart_when_markers_move(self):
        """A changing marker set means something is still writing."""
        wave = self.wave
        state = {"n": 0}

        class Moving(FakeLsf):
            def count_jobs_under_path(self, path):
                state["n"] += 1
                if state["n"] == 2:
                    open(os.path.join(wave, "1000_run", ".run.LATE_1"), "w").close()
                return super().count_jobs_under_path(path)

        outcome = self._run(Moving(counts=[0] * 12))
        self.assertTrue(outcome.ok)
        # a restart means more readings than the bare confirmation count
        self.assertGreater(len(outcome.quiescent_checks),
                           self.settings.lsf.quiescent_confirm_times)

    def test_timeout_aborts(self):
        self.settings.lsf.quiescent_timeout_sec = -1
        outcome = self._run(FakeLsf(counts=[0] * 8))
        self.assertEqual(outcome.phase, RerunPhase.ABORTED)
        self.assertIn("quiet", outcome.error)
        self.assertEqual(self._case_dirs(), ["NDIO_1", "NTN_1"])

    # -- other refusals ------------------------------------------------

    def test_dry_run_changes_nothing(self):
        lsf = FakeLsf(counts=[0, 0, 0, 0])
        outcome = self._run(lsf, dry_run=True)
        self.assertTrue(outcome.dry_run)
        self.assertEqual(lsf.killed_jobs, [])
        self.assertEqual(lsf.deleted_paths, [])
        self.assertEqual(self._case_dirs(), ["NDIO_1", "NTN_1"])

    def test_blocked_plan_is_refused(self):
        from dataclasses import replace

        blocked = replace(self.plan, blockers=("no parent job id",))
        lsf = FakeLsf(counts=[0, 0, 0, 0])
        from arcx_auto.services.launcher import Launcher

        outcome = Remediator(self.settings, lsf=lsf,
                             launcher=Launcher(self.settings, lsf=lsf),
                             sleep=lambda _s: None).run(blocked)
        self.assertEqual(outcome.phase, RerunPhase.ABORTED)
        self.assertEqual(lsf.killed_jobs, [])
        self.assertEqual(self._case_dirs(), ["NDIO_1", "NTN_1"])

    def test_failed_resubmit_is_reported(self):
        outcome = self._run(FakeLsf(counts=[0, 0, 0, 0], submit_ok=False))
        self.assertEqual(outcome.phase, RerunPhase.ABORTED)
        # the clean already happened, and the backup is what makes that safe
        self.assertEqual(self._case_dirs(), ["NTN_1"])
        self.assertTrue(os.path.isdir(os.path.join(
            self.wave, ".arcx_auto", "attempts", "2", "1000", "NDIO_1")))

    def test_bkill_failure_is_not_fatal(self):
        """bkill on an already-finished parent fails normally; the quiescent
        gate is what actually decides whether it is safe to proceed.
        """
        outcome = self._run(FakeLsf(counts=[0, 0, 0, 0], kill_ok=False))
        self.assertTrue(outcome.ok)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Concurrency and the safety gate's inputs
#
# Three defects found while reviewing the finished flow. Each one is silent:
# nothing crashes, the wrong answer just looks like the right one.
# ---------------------------------------------------------------------------

class WaveLockTest(unittest.TestCase):
    """One wave directory, one writer.

    Two reruns started a minute apart would both pass the quiescent gate (each
    sees zero jobs, because the other has not submitted yet), both move the
    same run dirs aside, and both resubmit -- two Arcx parents writing into one
    wave, which is the thing the whole design forbids.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.lsf.drain_retry_delay_sec = 0
        self.settings.lsf.quiescent_interval_sec = 0
        self.wave, self.cfg = build_wave(self.tmp.name, [
            CaseSpec("NTN_1", "complete", artifacts="full"),
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist"),
        ])
        self.plan = plan_for(self.wave, self.cfg, self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def _remediator(self, lsf):
        from arcx_auto.services.launcher import Launcher

        return Remediator(self.settings, lsf=lsf,
                          launcher=Launcher(self.settings, lsf=lsf),
                          sleep=lambda _s: None)

    def test_a_held_wave_lock_refuses_the_rerun(self):
        from arcx_auto.adapters.lock import FileLock

        held = FileLock(os.path.join(self.wave, ".arcx_auto", "lock"),
                        purpose="pretend another rerun")
        held.acquire()
        try:
            lsf = FakeLsf(counts=[0, 0, 0, 0])
            outcome = self._remediator(lsf).run(self.plan, run_id="demo")
        finally:
            held.release()

        self.assertEqual(outcome.phase, RerunPhase.ABORTED)
        self.assertIn("already being worked on", outcome.error)
        # Refused before anything was touched -- no bkill, no move.
        self.assertEqual(lsf.killed_jobs, [])
        self.assertEqual(outcome.backed_up, ())
        self.assertTrue(os.path.isdir(os.path.join(self.wave, "1000_run", "NDIO_1")))

    def test_the_lock_is_released_afterwards(self):
        from arcx_auto.adapters.lock import FileLock

        outcome = self._remediator(FakeLsf(counts=[0, 0, 0, 0])).run(
            self.plan, run_id="demo")
        self.assertEqual(outcome.phase, RerunPhase.MONITORING)
        # A second rerun must be able to take the lock again.
        again = FileLock(os.path.join(self.wave, ".arcx_auto", "lock"))
        again.acquire()
        again.release()

    def test_a_dry_run_takes_no_lock(self):
        from arcx_auto.adapters.lock import FileLock

        held = FileLock(os.path.join(self.wave, ".arcx_auto", "lock"))
        held.acquire()
        try:
            outcome = self._remediator(FakeLsf(counts=[0])).run(
                self.plan, run_id="demo", dry_run=True)
        finally:
            held.release()
        self.assertEqual(outcome.phase, RerunPhase.REQUESTED)


class MarkerFingerprintTest(unittest.TestCase):
    """What the quiescent gate is allowed to call "something changed".

    The gate restarts its confirmation count whenever the marker set moves. If
    the fingerprint is too broad it never settles and every rerun aborts at the
    safety gate for a reason that has nothing to do with safety.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.wave, self.cfg = build_wave(self.tmp.name, [
            CaseSpec("NTN_1", "complete", artifacts="full")])

    def tearDown(self):
        self.tmp.cleanup()

    def _fingerprint(self):
        import re

        from arcx_auto.services.remediator import _marker_fingerprint

        return _marker_fingerprint(
            self.wave, re.compile(self.settings.layout.marker_any_regex))

    def test_real_markers_are_seen(self):
        self.assertTrue(any(".complete.NTN_1" in p for p in self._fingerprint()))

    def test_nfs_silly_rename_files_are_ignored(self):
        """NFS renames a file deleted while still open to .nfs0000...

        Those appear exactly when jobs are being killed, which is the moment
        this gate runs. Counting them would churn the fingerprint on every
        reading and the confirmation count would never reach K.
        """
        before = self._fingerprint()
        open(os.path.join(self.wave, "1000_run", ".nfs00000000000a1b2c3d"), "w").close()
        self.assertEqual(self._fingerprint(), before)

    def test_artifacts_deep_in_the_tree_are_ignored(self):
        """Markers live one level down. Walking the whole tree would stat every
        netlist and QC_* report on NFS, three times, inside a 15 minute deadline.
        """
        before = self._fingerprint()
        deep = os.path.join(self.wave, "1000_run", "NTN_1", "nested", "deeper")
        os.makedirs(deep, exist_ok=True)
        open(os.path.join(deep, ".complete.SOMETHING"), "w").close()
        self.assertEqual(self._fingerprint(), before)

    def test_a_marker_appearing_changes_the_fingerprint(self):
        before = self._fingerprint()
        open(os.path.join(self.wave, "1000_run", ".run.LATE_1"), "w").close()
        self.assertNotEqual(self._fingerprint(), before)

    def test_an_unreadable_index_dir_never_reads_as_quiet(self):
        """A directory that cannot be listed is not proof that nothing moved."""
        import unittest.mock as mock

        with mock.patch("os.scandir", side_effect=_scandir_failing_on_index):
            first = self._fingerprint()
            second = self._fingerprint()
        self.assertNotEqual(first, second)


_REAL_SCANDIR = os.scandir


def _scandir_failing_on_index(path):
    if os.path.basename(str(path)) == "1000_run":
        raise OSError("stale NFS file handle")
    return _REAL_SCANDIR(path)


class PostCacheAttemptTest(unittest.TestCase):
    """POST results must not outlive the run dir they describe.

    POST checks are expensive and their answer cannot change -- except through
    a rerun, which rebuilds the run dir from scratch. The cache key therefore
    carries the attempt number. The monitor used to pass a constant 1, so a
    long-lived daemon kept serving the verdict computed *before* the rerun,
    against files that had since been moved into .arcx_auto/attempts/.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self, monitor, wave, cfg):
        return monitor.scan(run_folders=[os.path.join(wave, "1000_run")],
                            arcx_config=parse_arcx_cfg(cfg), use_lsf=False)

    @staticmethod
    def _fatal_ids(result):
        return sorted(i.id for i in result.all_issues() if i.severity.name == "FATAL")

    @staticmethod
    def _rebuild(wave, spec):
        """What a rerun leaves behind: the index run folder built again from
        nothing, not the old one written over."""
        import shutil

        shutil.rmtree(os.path.join(wave, "1000_run"))
        make_index_run_folder(wave, "1000", [spec])

    def _record_rerun(self, wave):
        """What Launcher._record does on a resubmission: append an attempt."""
        path = os.path.join(wave, ".arcx_auto", "launch.json")
        with open(path, encoding="utf-8") as handle:
            launch = json.load(handle)
        launch["attempts"].append({"attempt": 2, "job_id": "99999", "rerun": True})
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(launch, handle)

    def test_post_is_recomputed_after_a_rerun(self):
        from arcx_auto.services.monitor import MonitorService

        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist")])
        monitor = MonitorService(self.settings)

        broken = self._scan(monitor, wave, cfg)
        self.assertTrue(self._fatal_ids(broken),
                        "the truncated case should fail POST on attempt 1")

        # The rerun: the run dir is rebuilt complete, and launch.json gains an
        # attempt. Same MonitorService instance, as a running daemon would be.
        self._rebuild(wave, CaseSpec("NDIO_1", "complete", artifacts="full"))
        self._record_rerun(wave)

        fixed = self._scan(monitor, wave, cfg)
        self.assertEqual(
            self._fatal_ids(fixed), [],
            "a successful rerun still reported the pre-rerun POST verdict")

    def test_a_rerun_that_made_it_worse_is_also_seen(self):
        """The mirror image, and the dangerous one: attempt 1 passed, the rerun
        broke it, and a stale cache would keep reporting success.
        """
        from arcx_auto.services.monitor import MonitorService

        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NDIO_1", "complete", artifacts="full")])
        monitor = MonitorService(self.settings)

        self.assertEqual(self._fatal_ids(self._scan(monitor, wave, cfg)), [])

        self._rebuild(
            wave, CaseSpec("NDIO_1", "complete", artifacts="missing_netlist"))
        self._record_rerun(wave)

        self.assertTrue(self._fatal_ids(self._scan(monitor, wave, cfg)),
                        "a rerun that broke the case still reported success")

    def test_without_a_rerun_post_is_only_computed_once(self):
        """The cache still has to work, or every tick re-reads the run dir."""
        from arcx_auto.services.monitor import MonitorService

        wave, cfg = build_wave(self.tmp.name, [
            CaseSpec("NDIO_1", "complete", artifacts="missing_netlist")])
        monitor = MonitorService(self.settings)
        self._scan(monitor, wave, cfg)
        keys_after_first = set(monitor.qa_runner._post_cache)
        self._scan(monitor, wave, cfg)
        self.assertEqual(set(monitor.qa_runner._post_cache), keys_after_first)
        self.assertEqual(len(keys_after_first), 1)
