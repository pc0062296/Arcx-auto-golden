"""SubmissionController -- the gate that releases one wave at a time.

The gate condition (architecture 5.4):

    release = min_interval elapsed
              AND ( NJOBS < quota_threshold OR max_wait elapsed )

Why not simply "wait a fixed time **or** until the quota drops": a plain OR has
a hole -- once the timer expires, submitting while the quota is still full
floods the queue anyway. The combination above covers all three of not too
dense, not flooding, and never stuck forever because the quota never drops.

When `max_wait` forces a release it must be recorded plainly in the UI and the
audit log: that means "submitted without waiting for quota", so a long PEND
afterwards is expected rather than a fault.

The gate decision is a **pure function** over plain data, so "how long it
waited and why it released" can be enumerated in milliseconds instead of by
actually waiting two hours.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from arcx_auto.config.settings import GateSettings
from arcx_auto.domain.enums import WaveState


@dataclass(frozen=True)
class GateState:
    """Persisted gate state. Stored in state.json so a daemon restart resumes
    rather than restarting the clock.
    """

    wave_name: str
    entered_at: float
    last_submit_at: Optional[float] = None
    last_njobs: Optional[int] = None
    checked_at: Optional[float] = None

    def waited_for(self, now: float) -> float:
        return max(0.0, now - self.entered_at)


@dataclass(frozen=True)
class GateDecision:
    """Whether to release, and why. The reason is shown to humans verbatim."""

    allow: bool
    reason: str
    forced: bool = False              # released because max_wait expired
    wait_hint_sec: float = 0.0        # suggested delay before asking again

    @property
    def label(self) -> str:
        return ("forced" if self.forced else "release") if self.allow else "waiting"


def evaluate_gate(
    state: GateState,
    now: float,
    njobs: Optional[int],
    settings: GateSettings,
    previous_submit_at: Optional[float] = None,
) -> GateDecision:
    """Pure: may the next wave go out now?

    ``njobs`` of None means the quota could not be read (busers unavailable).
    That must **not** be treated as "the quota is low" -- it would submit
    hardest exactly when LSF is in trouble. Only min_interval and max_wait
    decide in that case, and the reason says the data was unavailable.
    """
    waited = state.waited_for(now)

    # 1. Hard minimum interval (debounce), measured from the last real submit
    since_submit = None
    if previous_submit_at is not None:
        since_submit = now - previous_submit_at
        if since_submit < settings.min_interval_sec:
            remaining = settings.min_interval_sec - since_submit
            return GateDecision(
                allow=False,
                reason="only %.0f minutes since the last submission; "
                       "the minimum interval is %.0f minutes"
                       % (since_submit / 60.0, settings.min_interval_sec / 60.0),
                wait_hint_sec=remaining,
            )

    # 2. Low enough quota releases
    if njobs is not None and njobs < settings.quota_threshold:
        return GateDecision(
            allow=True,
            reason="NJOBS = %d, below the threshold of %d"
                   % (njobs, settings.quota_threshold),
        )

    # 3. Waited too long -- force a release so a quota that never drops
    #    cannot stall everything indefinitely
    if waited >= settings.max_wait_sec:
        return GateDecision(
            allow=True,
            forced=True,
            reason="waited %.1f hours, over the %.1f hour limit; forcing release"
                   % (waited / 3600.0, settings.max_wait_sec / 3600.0),
        )

    if njobs is None:
        return GateDecision(
            allow=False,
            reason="NJOBS unavailable; can only wait out the %.1f hour limit"
                   % (settings.max_wait_sec / 3600.0),
            wait_hint_sec=min(300.0, settings.max_wait_sec - waited),
        )

    return GateDecision(
        allow=False,
        reason="NJOBS = %d, not yet below the threshold of %d "
               "(waited %.0f minutes)"
               % (njobs, settings.quota_threshold, waited / 60.0),
        wait_hint_sec=min(300.0, settings.max_wait_sec - waited),
    )


@dataclass
class WaveProgress:
    """Where one wave sits in the submission flow."""

    wave_name: str
    state: WaveState = WaveState.PLANNED
    gate: Optional[GateState] = None
    job_id: Optional[str] = None
    submitted_at: Optional[float] = None
    last_decision: Optional[GateDecision] = None
    error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "wave": self.wave_name,
            "state": self.state.value,
            "job_id": self.job_id,
            "submitted_at": self.submitted_at,
            "gate_entered_at": self.gate.entered_at if self.gate else None,
            "waited_sec": (
                None if self.gate is None
                else max(0.0, time.time() - self.gate.entered_at)),
            "decision": (
                None if self.last_decision is None
                else {"allow": self.last_decision.allow,
                      "forced": self.last_decision.forced,
                      "reason": self.last_decision.reason}),
            "error": self.error,
        }


class SubmissionController:
    """Holds submission progress for the whole batch and decides, each tick,
    whether the next wave may go out.

    Deliberately advances one wave at a time: releasing several at once defeats
    the gate, and the LSF load spike is exactly what it exists to prevent.
    """

    def __init__(
        self,
        wave_names: Sequence[str],
        settings: Optional[GateSettings] = None,
    ) -> None:
        self.settings = settings or GateSettings()
        self.progress: List[WaveProgress] = [
            WaveProgress(wave_name=name) for name in wave_names]
        self.last_submit_at: Optional[float] = None

    # ------------------------------------------------------------------

    def pending(self) -> List[WaveProgress]:
        return [p for p in self.progress
                if p.state in (WaveState.PLANNED, WaveState.WAITING_GATE)]

    def next_wave(self) -> Optional[WaveProgress]:
        """The next wave to submit. Plan order, never reordered."""
        pending = self.pending()
        return pending[0] if pending else None

    def evaluate(self, now: Optional[float] = None,
                 njobs: Optional[int] = None) -> Optional[GateDecision]:
        """Ask the gate whether the next wave may go out.

        Returns None when there is no wave left to submit.
        """
        now = now if now is not None else time.time()
        wave = self.next_wave()
        if wave is None:
            return None

        if wave.gate is None:
            wave.gate = GateState(wave_name=wave.wave_name, entered_at=now)
            wave.state = WaveState.WAITING_GATE

        decision = evaluate_gate(
            wave.gate, now, njobs, self.settings,
            previous_submit_at=self.last_submit_at,
        )
        wave.gate = replace(wave.gate, last_njobs=njobs, checked_at=now)
        wave.last_decision = decision
        return decision

    def mark_submitted(self, wave_name: str, job_id: Optional[str],
                       now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self.last_submit_at = now
        for wave in self.progress:
            if wave.wave_name == wave_name:
                wave.state = WaveState.SUBMITTED
                wave.job_id = job_id
                wave.submitted_at = now
                return

    def mark_failed(self, wave_name: str, error: str) -> None:
        for wave in self.progress:
            if wave.wave_name == wave_name:
                wave.state = WaveState.ABORTED
                wave.error = error
                return

    def to_json(self) -> List[Dict[str, Any]]:
        return [p.to_json() for p in self.progress]
