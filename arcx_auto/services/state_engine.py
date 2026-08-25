"""The state machine -- **completely pure**, zero I/O.

    in:  the previous verdict (CaseSnapshot) + this observation
         (CaseObservation) + thresholds
    out: a new verdict + the state transition events

Separating interpretation from observation buys two things:
  * every situation can be enumerated with fake data in milliseconds instead of
    by actually running jobs
  * changing a judgement never touches I/O code
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from arcx_auto.domain.enums import CaseState, Completeness, LsfState
from arcx_auto.domain.models import (
    CaseObservation,
    CaseSnapshot,
    IndexRunObservation,
    IndexRunSnapshot,
    StateEvent,
)


@dataclass(frozen=True)
class TransitionContext:
    """Decision thresholds.

    ``lsf_data_available`` is a deliberate safety switch: when LSF cannot be
    queried (a dev box, or bjobs having a bad day) a case must never be called
    LOST just because no job was found. Better to leave it RUNNING and visible
    than to raise a false alarm across the board.

    ``lost_grace_sec`` covers the other lag, the one inside a normal case: an
    LSF job leaves bjobs the moment it finishes, and Arcx writes the .complete
    marker some time afterwards. In that window the markers say running and
    LSF has nothing, and the case is neither finished nor lost.
    """

    now: float
    stall_threshold_sec: float = 3600.0
    lost_grace_sec: float = 300.0
    lsf_data_available: bool = False

    @classmethod
    def from_settings(
        cls,
        monitor: "object",
        lsf_data_available: bool,
        now: Optional[float] = None,
    ) -> "TransitionContext":
        from arcx_auto.config.settings import MonitorSettings

        assert isinstance(monitor, MonitorSettings)
        return cls(
            now=now if now is not None else time.time(),
            stall_threshold_sec=monitor.stall_threshold_sec,
            lost_grace_sec=monitor.lost_grace_sec,
            lsf_data_available=lsf_data_available,
        )


# --------------------------------------------------------------------------
# One case
# --------------------------------------------------------------------------

def transition_case(
    prev: Optional[CaseSnapshot],
    obs: CaseObservation,
    ctx: TransitionContext,
) -> Tuple[CaseSnapshot, List[StateEvent]]:
    """Derive the new state of one case. Pure: same input, same output."""
    now = ctx.now

    # --- 1. Progress tracking --------------------------------------------
    # Progress is measured by log size, not mtime: NFS mtimes are unreliable
    # and some tools touch a file without writing anything. Only real growth
    # counts as progress.
    #
    # The **first** observation has no history to compare against, so it seeds
    # from mtime rather than from now. That matters more than it looks: if it
    # always started from now, a case that has been stuck for three days would
    # look healthy after every daemon restart -- and would look healthy again
    # after the next one. Seeding from mtime restores the true quiet time
    # immediately (architecture decision 2: rebuild all state from the
    # filesystem).
    size = obs.log_size or 0
    if prev is None:
        last_progress_at = obs.log_mtime if obs.log_mtime else now
        # A timestamp in the future (clock skew) would give negative quiet time
        if last_progress_at > now:
            last_progress_at = now
        last_progress_size = size
    elif size > prev.last_progress_size:
        last_progress_at = now
        last_progress_size = size
    else:
        last_progress_at = prev.last_progress_at
        last_progress_size = prev.last_progress_size

    # --- 2. Tracking a missing LSF job -----------------------------------
    lsf_state = obs.lsf.state if obs.lsf else None
    lsf_job_id = obs.lsf.job_id if obs.lsf else (prev.lsf_job_id if prev else None)

    # A .queue marker is Arcx's own queue, not LSF's. Arcx submits only so
    # many cases at a time within an index, so a queued case has no LSF job
    # yet **by design** -- and calling that LOST says something false about
    # the most normal situation there is.
    expects_job = obs.has_run_marker
    # Sticky: once a job has been matched, its id survives ticks where nothing
    # matched. That is also the evidence that a job ever existed.
    ever_seen = bool(lsf_job_id)
    job_absent = ctx.lsf_data_available and expects_job and (
        obs.lsf is None or not lsf_state.is_active  # type: ignore[union-attr]
    )
    # Only start the clock for a case whose job we have actually seen. Never
    # having found one is not evidence that one is gone: it is the absence of
    # evidence either way, and LOST is far too definite a word for that.
    if job_absent and ever_seen:
        lsf_missing_since = (
            prev.lsf_missing_since if prev and prev.lsf_missing_since else now
        )
    else:
        lsf_missing_since = None
    never_matched = job_absent and not ever_seen

    # --- 3. Decide the state ---------------------------------------------
    state, reason = _decide_state(
        obs=obs,
        ctx=ctx,
        lsf_state=lsf_state,
        lsf_missing_since=lsf_missing_since,
        last_progress_at=last_progress_at,
        never_matched=never_matched,
    )

    # Compare base against base, not against the resolved state: `state` may
    # already have been narrowed to DONE / FAILED / STALLED by StateResolver,
    # and comparing against that would reset entered_state_at every tick.
    prev_base = (prev.base_state or prev.state) if prev else None
    entered_state_at = (
        prev.entered_state_at if prev and prev_base == state else now
    )

    snapshot = CaseSnapshot(
        case_id=obs.case_id,
        state=state,
        entered_state_at=entered_state_at,
        last_progress_at=last_progress_at,
        last_progress_size=last_progress_size,
        last_seen_at=now,
        lsf_job_id=lsf_job_id,
        lsf_state=lsf_state,
        lsf_job_matched=obs.lsf is not None,
        lsf_missing_since=lsf_missing_since,
        case_dir=obs.case_dir or (prev.case_dir if prev else None),
        log_path=obs.log_path or (prev.log_path if prev else None),
        exec_path=obs.exec_path or (prev.exec_path if prev else None),
        marker_inconsistent=obs.marker_inconsistent,
        note=reason,
        base_state=state,
    )

    events: List[StateEvent] = []
    if prev is None or prev_base != state:
        events.append(
            StateEvent(
                ts=now,
                index_key="",  # filled in by transition_index_run
                case_id=obs.case_id,
                from_state=prev_base,
                to_state=state,
                reason=reason,
                evidence={
                    "markers": sorted(m.value for m in obs.markers),
                    "log_size": obs.log_size,
                    "lsf_state": lsf_state.value if lsf_state else None,
                    "silent_sec": round(now - last_progress_at, 1),
                },
            )
        )
    return snapshot, events


def _decide_state(
    obs: CaseObservation,
    ctx: TransitionContext,
    lsf_state: Optional[LsfState],
    lsf_missing_since: Optional[float],
    last_progress_at: float,
    never_matched: bool = False,
) -> Tuple[CaseState, str]:
    """State precedence.

    Markers rank complete > run > queue: even if .run was never cleared, a
    .complete means Arcx considers the case finished. The inconsistency is
    recorded separately.
    """
    now = ctx.now

    # 3.1 A complete marker wins. Note this only means Arcx believes it
    #     finished; DONE requires QA to pass.
    if obs.has_complete_marker:
        if obs.marker_inconsistent:
            return (CaseState.COMPLETED_MARKER,
                    ".complete present but .run/.queue were not cleared")
        return (CaseState.COMPLETED_MARKER,
                ".complete marker present, awaiting QA")

    # 3.2 LSF explicitly reports a suspension
    if lsf_state is not None and lsf_state.is_suspended:
        return (CaseState.SUSPENDED, "LSF reports %s" % lsf_state.value)

    # 3.3 A job we have seen is gone -- and only LOST past the grace period.
    #     lsf_missing_since is None unless a job was matched at some point, so
    #     "we never found one" cannot reach here.
    if lsf_missing_since is not None:
        missing_for = now - lsf_missing_since
        if missing_for >= ctx.lost_grace_sec:
            return (
                CaseState.LOST,
                "marker says in flight but the LSF job has been gone for %.0fs"
                % missing_for,
            )
        # still inside the grace period: keep reading the markers as they are

    # 3.4 Running
    if obs.has_run_marker:
        silent = now - last_progress_at
        if silent >= ctx.stall_threshold_sec:
            return (
                CaseState.STALLED,
                ".run marker present but the log has not grown for %.0f minutes"
                % (silent / 60.0),
            )
        if never_matched:
            # Running as far as the markers are concerned, and no LSF job has
            # ever been matched to it. Said plainly rather than dressed up as
            # LOST: the case may simply be waiting its turn inside Arcx, which
            # runs only so many at a time within one index.
            return (CaseState.RUNNING,
                    ".run marker present; no LSF job has been matched to it")
        return (CaseState.RUNNING, ".run marker present")

    # 3.5 Queued
    if obs.has_queue_marker:
        return (CaseState.QUEUED, ".queue marker present")

    # 3.6 No markers at all
    if obs.case_dir_exists:
        return (
            CaseState.PENDING,
            "case run dir exists but no marker (not submitted, or marker lost)",
        )
    if obs.log_path:
        return (CaseState.UNKNOWN, "a log with no marker and no run dir")
    return (CaseState.PENDING, "no marker has appeared yet")


# --------------------------------------------------------------------------
# A whole index run folder
# --------------------------------------------------------------------------

def transition_index_run(
    prev: Optional[IndexRunSnapshot],
    obs: IndexRunObservation,
    ctx: TransitionContext,
) -> Tuple[IndexRunSnapshot, List[StateEvent]]:
    """Run the transition for every case in one index run folder."""
    prev_cases: Dict[str, CaseSnapshot] = dict(prev.cases) if prev else {}
    new_cases: Dict[str, CaseSnapshot] = {}
    events: List[StateEvent] = []

    for case_id, case_obs in obs.cases.items():
        snapshot, case_events = transition_case(
            prev_cases.get(case_id), case_obs, ctx
        )
        new_cases[case_id] = snapshot
        for event in case_events:
            events.append(replace(event, index_key=obs.index_key))

    # Cases seen before but missing now keep their previous verdict rather than
    # vanishing from the display. A deleted directory or a failed scan both
    # cause this, and both are worth noticing.
    for case_id, old in prev_cases.items():
        if case_id not in new_cases:
            new_cases[case_id] = replace(
                old, note="not observed in this scan (may have been deleted)"
            )

    snapshot = IndexRunSnapshot(
        index_key=obs.index_key,
        run_folder=obs.run_folder,
        updated_at=ctx.now,
        cases=new_cases,
        error=obs.error,
    )
    return snapshot, events


# --------------------------------------------------------------------------
# The rerun delete list
# --------------------------------------------------------------------------

def classify_completeness(snapshot: CaseSnapshot) -> Tuple[Completeness, str]:
    """Decide whether a rerun should delete this case's run dir.

    A deliberate exception (architecture 6.1): here "not sure" leans towards
    deleting and rerunning, the opposite of the rest of the system. The costs
    are asymmetric:
      mistakenly deleting a complete case -> one wasted run, result still right
      mistakenly keeping an incomplete one -> a truncated result shipped as good

    UNKNOWN therefore lands in the delete list by default, but the UI marks it
    differently and it can be unticked, and everything is backed up into
    .arcx_auto/attempts/N/ before deletion.
    """
    state = snapshot.state

    if state == CaseState.DONE:
        return (Completeness.COMPLETE, "QA verified")
    if state == CaseState.COMPLETED_MARKER:
        if snapshot.marker_inconsistent:
            return (
                Completeness.UNKNOWN,
                ".complete present but .run/.queue were not cleared; "
                "the tidy-up state is doubtful",
            )
        return (Completeness.COMPLETE, ".complete marker present")
    if state in (CaseState.PENDING, CaseState.QUEUED, CaseState.RUNNING,
                 CaseState.SUSPENDED, CaseState.STALLED, CaseState.LOST,
                 CaseState.FAILED):
        return (Completeness.INCOMPLETE, "state is %s, not finished" % state.value)
    return (Completeness.UNKNOWN, "state is %s, cannot decide" % state.value)
