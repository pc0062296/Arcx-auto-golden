"""StateEngine: deciding states.

Every test is pure -- no filesystem, no LSF. That is the payoff of keeping the
decision logic pure: a scenario like "the job ran for three days and then got
stuck" can be enumerated in milliseconds instead of waiting three days.
"""

import unittest

from arcx_auto.domain.enums import CaseState, Completeness, LsfState, MarkerKind
from arcx_auto.domain.models import CaseObservation, LsfJobView
from arcx_auto.services.state_engine import (
    TransitionContext,
    classify_completeness,
    transition_case,
)

T0 = 1_000_000.0


def obs(case_id="case1", markers=(), log_size=None, log_mtime=None,
        case_dir=None, lsf=None, log_path=None):
    return CaseObservation(
        case_id=case_id,
        markers=frozenset(markers),
        case_dir=case_dir,
        case_dir_exists=case_dir is not None,
        log_path=log_path or ("/tmp/l.log" if log_size is not None else None),
        log_size=log_size,
        log_mtime=log_mtime,
        lsf=lsf,
    )


def ctx(now=T0, stall=3600.0, grace=300.0, lsf_available=True):
    return TransitionContext(
        now=now, stall_threshold_sec=stall, lost_grace_sec=grace,
        lsf_data_available=lsf_available,
    )


class BasicTransitionTest(unittest.TestCase):
    def test_no_marker_is_pending(self):
        snap, _ = transition_case(None, obs(), ctx())
        self.assertEqual(snap.state, CaseState.PENDING)

    def test_queue_marker(self):
        snap, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), ctx())
        self.assertEqual(snap.state, CaseState.QUEUED)

    def test_run_marker(self):
        snap, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100), ctx())
        self.assertEqual(snap.state, CaseState.RUNNING)

    def test_complete_marker(self):
        snap, _ = transition_case(None, obs(markers=[MarkerKind.COMPLETE]), ctx())
        self.assertEqual(snap.state, CaseState.COMPLETED_MARKER)

    def test_complete_wins_over_run(self):
        """Marker precedence is complete > run > queue.

        A .run that was never cleared does not change the fact that Arcx
        considers the case finished, but the inconsistency is recorded.
        """
        snap, _ = transition_case(
            None, obs(markers=[MarkerKind.COMPLETE, MarkerKind.RUN]), ctx())
        self.assertEqual(snap.state, CaseState.COMPLETED_MARKER)
        self.assertTrue(snap.marker_inconsistent)

    def test_complete_marker_is_not_done(self):
        """.complete only means Arcx thinks it finished, not that the result
        is correct.

        DONE requires QA to pass; that separation is what catches false
        success.
        """
        snap, _ = transition_case(None, obs(markers=[MarkerKind.COMPLETE]), ctx())
        self.assertNotEqual(snap.state, CaseState.DONE)

    def test_orphan_dir_is_pending_with_reason(self):
        snap, _ = transition_case(None, obs(case_dir="/x/case9"), ctx())
        self.assertEqual(snap.state, CaseState.PENDING)
        self.assertIn("marker", snap.note or "")


class ProgressTest(unittest.TestCase):
    def test_log_growth_resets_silence(self):
        c = ctx(now=T0)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        later = ctx(now=T0 + 1800)
        second, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=200), later)
        self.assertEqual(second.last_progress_at, T0 + 1800)
        self.assertEqual(second.silent_for(T0 + 1800), 0.0)

    def test_log_not_growing_accumulates_silence(self):
        c = ctx(now=T0)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        later = ctx(now=T0 + 1800)
        second, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100), later)
        self.assertEqual(second.silent_for(T0 + 1800), 1800.0)

    def test_stall_detected_past_threshold(self):
        """STALLED means "alive but not progressing", so a live LSF job is
        required.
        """
        alive = LsfJobView(job_id="7", state=LsfState.RUN)
        c = ctx(now=T0)
        first, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0, lsf=alive),
            c,
        )
        later = ctx(now=T0 + 3700, stall=3600.0)
        second, _ = transition_case(
            first,
            obs(markers=[MarkerKind.RUN], log_size=100, lsf=alive),
            later,
        )
        self.assertEqual(second.state, CaseState.STALLED)

    def test_lost_takes_priority_over_stalled(self):
        """A vanished job plus a still log means LOST, not STALLED.

        When both hold, LOST is the more precise diagnosis (the job died).
        STALLED would send an engineer looking for why it is slow when it is
        not running at all.
        """
        c = ctx(now=T0, grace=300.0, stall=3600.0)
        job = LsfJobView(job_id="7", state=LsfState.RUN)
        first, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0, lsf=job),
            c)
        # The job is gone and the log is still. The grace period has only just
        # started, so this reads STALLED for now.
        second, _ = transition_case(
            first,
            obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 3700, grace=300.0, stall=3600.0),
        )
        self.assertEqual(second.state, CaseState.STALLED)
        # Once the job has been gone long enough, LOST wins: it is the more
        # precise diagnosis, and STALLED would send somebody looking for why
        # it is slow when it is not running at all.
        third, _ = transition_case(
            second,
            obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 4100, grace=300.0, stall=3600.0),
        )
        self.assertEqual(third.state, CaseState.LOST)

    def test_first_observation_seeds_silence_from_mtime(self):
        """Key: the first observation (and every daemon restart) must seed
        from mtime.

        Always starting from now would make a case stuck for three days look
        healthy after every restart, while the design requires rebuilding all
        state from the filesystem.
        """
        snap, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0 - 7200),
            ctx(now=T0, stall=3600.0),
        )
        self.assertEqual(snap.state, CaseState.STALLED)

    def test_future_mtime_is_clamped(self):
        """Clock skew must not produce a negative quiet time."""
        snap, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0 + 5000),
            ctx(now=T0),
        )
        self.assertEqual(snap.last_progress_at, T0)
        self.assertEqual(snap.silent_for(T0), 0.0)


class LsfTest(unittest.TestCase):
    def test_suspended_from_lsf(self):
        job = LsfJobView(job_id="1", state=LsfState.SSUSP)
        snap, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=1, lsf=job), ctx())
        self.assertEqual(snap.state, CaseState.SUSPENDED)

    def test_lost_requires_grace_period(self):
        """A vanished LSF job is not immediately LOST: markers and LSF have
        different visibility lag. An LSF job leaves bjobs the moment it
        finishes, and Arcx writes .complete some time afterwards.
        """
        c = ctx(now=T0, grace=300.0)
        job = LsfJobView(job_id="7", state=LsfState.RUN)
        first, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0, lsf=job),
            c)
        # the job is gone, but still inside the grace period
        mid, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 100, grace=300.0))
        self.assertEqual(mid.state, CaseState.RUNNING)
        # past the grace period
        late, _ = transition_case(
            mid, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 400, grace=300.0))
        self.assertEqual(late.state, CaseState.LOST)

    def test_never_lost_when_lsf_data_unavailable(self):
        """A bjobs hiccup must never mark every case LOST.

        Better to leave them RUNNING and visible than to raise a false alarm
        across the board.
        """
        c = ctx(now=T0, lsf_available=False)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        late, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 100000, lsf_available=False, stall=1e9))
        self.assertEqual(late.state, CaseState.RUNNING)

    def test_lost_clears_when_job_reappears(self):
        c = ctx(now=T0, grace=300.0)
        seen = LsfJobView(job_id="7", state=LsfState.RUN)
        first, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0, lsf=seen),
            c)
        gone, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 100, grace=300.0))
        self.assertIsNotNone(gone.lsf_missing_since)
        job = LsfJobView(job_id="7", state=LsfState.RUN)
        back, _ = transition_case(
            gone, obs(markers=[MarkerKind.RUN], log_size=100, lsf=job),
            ctx(now=T0 + 200, grace=300.0))
        self.assertIsNone(back.lsf_missing_since)
        self.assertEqual(back.state, CaseState.RUNNING)

    def test_completed_case_never_judged_lost(self):
        """A finished case has no LSF job, and that is normal, not LOST."""
        snap, _ = transition_case(
            None, obs(markers=[MarkerKind.COMPLETE]), ctx(now=T0))
        self.assertEqual(snap.state, CaseState.COMPLETED_MARKER)


    def test_a_queued_case_is_never_lost(self):
        """A .queue marker is Arcx's own queue, not LSF's.

        Arcx runs only so many cases at a time within one index, so a queued
        case has no LSF job yet **by design**. This used to be reported as
        LOST, which said something false about the most normal situation
        there is -- and said it about every case waiting its turn.
        """
        c = ctx(now=T0, grace=300.0)
        first, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), c)
        self.assertEqual(first.state, CaseState.QUEUED)
        later, _ = transition_case(
            first, obs(markers=[MarkerKind.QUEUE]),
            ctx(now=T0 + 100000, grace=300.0))
        self.assertEqual(later.state, CaseState.QUEUED)

    def test_a_running_case_whose_job_was_never_matched_is_not_lost(self):
        """Never having found a job is not evidence that one is gone.

        It is the absence of evidence either way, and LOST is far too
        definite a word for that. The case says so instead.
        """
        c = ctx(now=T0, grace=300.0, stall=1e9)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        later, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 100000, grace=300.0, stall=1e9))
        self.assertEqual(later.state, CaseState.RUNNING)
        self.assertIn("no LSF job", later.note)
        self.assertIsNone(later.lsf_missing_since)

    def test_a_job_seen_once_is_remembered(self):
        """The id stays on the page after the job leaves bjobs, but the page
        has to be able to say it is no longer matched.
        """
        c = ctx(now=T0, grace=300.0)
        job = LsfJobView(job_id="7", state=LsfState.RUN)
        first, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0, lsf=job),
            c)
        self.assertTrue(first.lsf_job_matched)
        gone, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 60, grace=300.0))
        self.assertEqual(gone.lsf_job_id, "7")
        self.assertFalse(gone.lsf_job_matched)


class EventTest(unittest.TestCase):
    def test_event_emitted_on_first_observation(self):
        _snap, events = transition_case(None, obs(markers=[MarkerKind.QUEUE]), ctx())
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].from_state)
        self.assertEqual(events[0].to_state, CaseState.QUEUED)

    def test_no_event_when_state_unchanged(self):
        c = ctx(now=T0, lsf_available=False)
        first, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), c)
        _second, events = transition_case(
            first, obs(markers=[MarkerKind.QUEUE]),
            ctx(now=T0 + 60, lsf_available=False))
        self.assertEqual(events, [])

    def test_event_on_transition_carries_evidence(self):
        c = ctx(now=T0)
        first, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), c)
        _second, events = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=10), ctx(now=T0 + 60))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].from_state, CaseState.QUEUED)
        self.assertEqual(events[0].to_state, CaseState.RUNNING)
        self.assertIn("markers", events[0].evidence)

    def test_entered_state_at_preserved_while_state_unchanged(self):
        c = ctx(now=T0, lsf_available=False)
        first, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), c)
        second, _ = transition_case(
            first, obs(markers=[MarkerKind.QUEUE]),
            ctx(now=T0 + 999, lsf_available=False))
        self.assertEqual(second.entered_state_at, T0)


class CompletenessTest(unittest.TestCase):
    """Deciding the rerun delete list (architecture 6.1)."""

    def _snap(self, state, inconsistent=False):
        from arcx_auto.domain.models import CaseSnapshot
        return CaseSnapshot(
            case_id="case1", state=state, entered_state_at=T0,
            last_progress_at=T0, marker_inconsistent=inconsistent,
        )

    def test_completed_marker_is_kept(self):
        result, _ = classify_completeness(self._snap(CaseState.COMPLETED_MARKER))
        self.assertEqual(result, Completeness.COMPLETE)

    def test_inconsistent_complete_is_unknown(self):
        result, _ = classify_completeness(
            self._snap(CaseState.COMPLETED_MARKER, inconsistent=True))
        self.assertEqual(result, Completeness.UNKNOWN)

    def test_in_flight_states_are_incomplete(self):
        for state in (CaseState.PENDING, CaseState.QUEUED, CaseState.RUNNING,
                      CaseState.SUSPENDED, CaseState.STALLED, CaseState.LOST):
            result, _ = classify_completeness(self._snap(state))
            self.assertEqual(result, Completeness.INCOMPLETE, state)

    def test_unknown_state_is_unknown_not_complete(self):
        """A key safety property: undecidable must never lean towards keeping.

        Failing to delete an incomplete case ships a truncated result as a
        success, which cannot be undone. Deleting a complete one only wastes a
        run, which can.
        """
        result, _ = classify_completeness(self._snap(CaseState.UNKNOWN))
        self.assertEqual(result, Completeness.UNKNOWN)
        self.assertNotEqual(result, Completeness.COMPLETE)


if __name__ == "__main__":
    unittest.main()


class StallThresholdAgreementTest(unittest.TestCase):
    """Two settings mean "stalled" and both are live. They have to agree.

    StateEngine runs first and sets base_state, and StateResolver only promotes
    RUNNING -- so if MonitorSettings has the shorter clock it wins outright and
    the graded 4h/8h escalation never gets a chance to apply. A case quiet for
    seventy minutes, which is routine for this workload, would be displayed as
    STALLED.
    """

    def test_the_state_machine_does_not_undercut_the_graded_grading(self):
        from arcx_auto.config.settings import Settings

        settings = Settings()
        self.assertEqual(settings.monitor.stall_threshold_sec,
                         settings.qa.quiet.stalled_after_sec)

    def test_warn_comes_before_stalled(self):
        from arcx_auto.config.settings import Settings

        quiet = Settings().qa.quiet
        self.assertLess(quiet.warn_after_sec, quiet.stalled_after_sec)
