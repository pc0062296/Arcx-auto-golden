"""Domain types for the rerun flow -- pure data, zero I/O.

A rerun is the only operation in the system that deletes files, so the types
here are shaped around making the decision inspectable **before** anything is
touched: a RerunPlan says exactly which directories would go and why, and can
be printed, reviewed and stored without executing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from arcx_auto.domain.enums import CaseState, Completeness


class RerunPhase(str, Enum):
    """Where a rerun is in its sequence.

    Persisted so a daemon killed mid-flight resumes from the right step rather
    than restarting a destructive sequence from the beginning.
    """

    REQUESTED = "REQUESTED"
    STOPPING_PARENT = "STOPPING_PARENT"
    DRAINING = "DRAINING"
    VERIFY_QUIESCENT = "VERIFY_QUIESCENT"
    DECIDE_CLEAN_SET = "DECIDE_CLEAN_SET"
    BACKUP = "BACKUP"
    CLEAN = "CLEAN"
    RESUBMIT = "RESUBMIT"
    MONITORING = "MONITORING"
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class CaseDecision:
    """What a rerun would do with one case, and why."""

    index_key: str
    case_id: str
    case_dir: Optional[str]
    state: CaseState
    completeness: Completeness
    reason: str

    @property
    def delete(self) -> bool:
        """Whether this case's run dir would be deleted.

        UNKNOWN deletes (architecture 6.1): the costs are asymmetric, and
        deleting something complete only wastes a run while keeping something
        incomplete ships a truncated result as a success.
        """
        return self.completeness in (Completeness.INCOMPLETE,
                                     Completeness.UNKNOWN)

    @property
    def uncertain(self) -> bool:
        return self.completeness == Completeness.UNKNOWN


@dataclass(frozen=True)
class RerunPlan:
    """Everything a rerun would do, computed before anything is touched."""

    wave_dir: str
    wave_name: str
    index_keys: Tuple[str, ...] = ()
    arcx_job_id: Optional[str] = None
    arcx_cfg: Optional[str] = None
    attempt: int = 1
    decisions: Tuple[CaseDecision, ...] = ()
    # Reasons the plan must not be executed. Non-empty means refuse.
    blockers: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def to_delete(self) -> Tuple[CaseDecision, ...]:
        return tuple(d for d in self.decisions if d.delete)

    @property
    def to_keep(self) -> Tuple[CaseDecision, ...]:
        return tuple(d for d in self.decisions if not d.delete)

    @property
    def uncertain(self) -> Tuple[CaseDecision, ...]:
        return tuple(d for d in self.decisions if d.uncertain)

    @property
    def executable(self) -> bool:
        return not self.blockers and bool(self.to_delete)


@dataclass(frozen=True)
class DrainAttempt:
    """One pass of "delete the jobs under this path, then count what is left"."""

    attempt: int
    deleted_ok: bool
    remaining: Optional[int]        # None means unknown, never zero
    error: Optional[str] = None
    raw_output: str = ""


@dataclass(frozen=True)
class QuiescentCheck:
    """One confirmation reading of the safety gate."""

    index: int
    jobs: Optional[int]
    markers_changed: bool
    error: Optional[str] = None


@dataclass(frozen=True)
class RerunOutcome:
    """The result of executing (or previewing) a rerun."""

    plan: RerunPlan
    phase: RerunPhase = RerunPhase.REQUESTED
    dry_run: bool = False
    parent_killed: bool = False
    drain_attempts: Tuple[DrainAttempt, ...] = ()
    quiescent_checks: Tuple[QuiescentCheck, ...] = ()
    backed_up: Tuple[str, ...] = ()
    deleted: Tuple[str, ...] = ()
    resubmit_job_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def aborted(self) -> bool:
        return self.phase == RerunPhase.ABORTED

    @property
    def ok(self) -> bool:
        return self.error is None and not self.aborted
