"""Enumerations.

Deliberately str-based so that JSON serialisation, CLI output and config
comparison can all use the plain string without a conversion layer.
"""

from __future__ import annotations

from enum import Enum


class MarkerKind(str, Enum):
    """Hidden marker files Arcx creates inside an index run folder.

    Named ``.<kind>.<case_id>``, for example ``.complete.NTN_1``.
    """

    QUEUE = "queue"
    RUN = "run"
    COMPLETE = "complete"


class LsfState(str, Enum):
    """LSF job state, from the STAT column of bjobs."""

    PEND = "PEND"
    RUN = "RUN"
    PSUSP = "PSUSP"
    USUSP = "USUSP"
    SSUSP = "SSUSP"
    DONE = "DONE"
    EXIT = "EXIT"
    UNKWN = "UNKWN"
    ZOMBI = "ZOMBI"

    @property
    def is_suspended(self) -> bool:
        return self in (LsfState.PSUSP, LsfState.USUSP, LsfState.SSUSP)

    @property
    def is_active(self) -> bool:
        """The job still holds or waits for resources; it has not finished."""
        return self in (
            LsfState.PEND,
            LsfState.RUN,
            LsfState.PSUSP,
            LsfState.USUSP,
            LsfState.SSUSP,
            LsfState.UNKWN,
        )


class CaseState(str, Enum):
    """State of a single case.

    Finer grained than the queue/run/fail/done the user first described,
    because in practice the troublesome categories -- suspended, stalled, lost,
    and false success -- all hide between those four.

    Note that ``COMPLETED_MARKER`` and ``DONE`` are deliberately separate: a
    ``.complete.<case>`` marker only means Arcx believes it finished, not that
    the result is correct. QA has to pass in between.
    """

    PENDING = "PENDING"                    # known case, no marker yet
    QUEUED = "QUEUED"                      # .queue marker / LSF PEND
    RUNNING = "RUNNING"                    # .run marker / LSF RUN
    SUSPENDED = "SUSPENDED"                # LSF reports *SUSP
    STALLED = "STALLED"                    # looks alive, log is not growing
    COMPLETED_MARKER = "COMPLETED_MARKER"  # .complete present, QA not run yet
    DONE = "DONE"                          # QA passed
    FAILED = "FAILED"                      # QA failed
    LOST = "LOST"                          # marker mid-flight, LSF job gone
    UNKNOWN = "UNKNOWN"                    # not enough observation to decide

    @property
    def is_terminal(self) -> bool:
        return self in (CaseState.DONE, CaseState.FAILED)

    @property
    def is_in_flight(self) -> bool:
        """Still in LSF's hands, holding or waiting for resources."""
        return self in (
            CaseState.QUEUED,
            CaseState.RUNNING,
            CaseState.SUSPENDED,
            CaseState.STALLED,
        )

    @property
    def needs_attention(self) -> bool:
        """States a human should look at."""
        return self in (
            CaseState.SUSPENDED,
            CaseState.STALLED,
            CaseState.LOST,
            CaseState.FAILED,
            CaseState.UNKNOWN,
        )


class Completeness(str, Enum):
    """Whether a rerun should delete this case's run dir and redo it.

    Three states rather than a bool on purpose (see docs/architecture.md 6.1):
    here "not sure" leans towards deleting, because the costs are asymmetric.
    Deleting something that was complete wastes one run; keeping something that
    was incomplete ships a truncated result as if it had succeeded.
    """

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"


class Severity(str, Enum):
    """Severity of a QA issue.

    UNKNOWN is a deliberate first class value meaning "I could not check".
    An unreadable run dir, an unrecognised format, or a QA function that threw
    must never be recorded as a pass, and in rerun decisions UNKNOWN leans
    towards deleting and rerunning (architecture 6.1).
    """

    INFO = "INFO"
    WARN = "WARN"
    UNKNOWN = "UNKNOWN"
    FATAL = "FATAL"


class IssueScope(str, Enum):
    """What a QA check looks at."""

    CASE = "CASE"
    INDEX = "INDEX"
    WAVE = "WAVE"
    GLOBAL = "GLOBAL"


class IssueStage(str, Enum):
    """When a QA check runs."""

    PRE = "PRE"    # before submission
    LIVE = "LIVE"  # while running
    POST = "POST"  # after completion


class WaveState(str, Enum):
    """Lifecycle of one wave: one Arcx command in one isolated directory."""

    PLANNED = "PLANNED"
    WAITING_GATE = "WAITING_GATE"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    MONITORING = "MONITORING"
    DONE = "DONE"
    ABORTED = "ABORTED"


class PlanMode(str, Enum):
    """Wave planning mode.

    All three share one WavePlan shape: OFF is just "a single wave" and MANUAL
    is just "the grouping came from a human", so nothing downstream needs to
    branch on the mode.
    """

    AUTO = "AUTO"
    MANUAL = "MANUAL"
    OFF = "OFF"
