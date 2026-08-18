"""QA domain types -- pure data, zero I/O.

The core distinction (docs/architecture.md 7):

    State  unique and exclusive  -- "where this case is now", from StateEngine
    Issue  many can coexist      -- "what is wrong with it", from QA functions

A case can be in COMPLETED_MARKER state while carrying both an
ARTIFACT_MISSING and a MARKER_INCONSISTENT issue.

The final state is derived by StateResolver from (base_state, issues). The
direction is one way, so the judgement logic that keeps growing all lives in QA
functions and StateEngine stays small.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from arcx_auto.domain.enums import Completeness, IssueScope, IssueStage, Severity


@dataclass(frozen=True)
class Issue:
    """A problem found by one QA check.

    ``evidence`` is for humans: file paths, actual versus expected values. It is
    stored verbatim in qa/<case>.json and shown in the UI, because three days
    later "why did it decide this failed" has to still have an answer.
    """

    id: str
    severity: Severity
    message: str
    scope: IssueScope = IssueScope.CASE
    stage: IssueStage = IssueStage.POST
    title: str = ""
    index_key: Optional[str] = None
    case_id: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    doc: str = ""                 # the check's docstring, shown in the UI

    @property
    def is_fatal(self) -> bool:
        return self.severity == Severity.FATAL

    @property
    def blocks_success(self) -> bool:
        """Whether this is enough to deny "succeeded".

        UNKNOWN counts: "I could not check" must never be treated as a pass.
        """
        return self.severity in (Severity.FATAL, Severity.UNKNOWN)


@dataclass(frozen=True)
class QaResult:
    """Everything one case (or index) produced from a QA run."""

    target: str                                # case_id or index_key
    scope: IssueScope = IssueScope.CASE
    stage: IssueStage = IssueStage.POST
    issues: Tuple[Issue, ...] = ()
    checked_at: float = 0.0
    attempt: int = 1
    checks_run: Tuple[str, ...] = ()
    checks_failed: Tuple[str, ...] = ()        # QA functions that threw

    @property
    def fatal(self) -> Tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == Severity.FATAL)

    @property
    def unknown(self) -> Tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == Severity.UNKNOWN)

    @property
    def warnings(self) -> Tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == Severity.WARN)

    @property
    def passed(self) -> bool:
        return not any(i.blocks_success for i in self.issues)

    def completeness(self) -> Tuple[Completeness, str]:
        """The **issue-based** view of whether a rerun should delete this case.

        Asymmetric on purpose (architecture 6.1): deleting something complete
        wastes one run and the result stays correct, while keeping something
        incomplete ships a truncated result as a success. So "could not check"
        leans towards deleting, not towards keeping.

        This is only half the answer. A case that never reached a POST state
        has had only the LIVE checks run, so a clean result here means "nothing
        looks wrong right now", not "it finished". The rerun planner combines
        this with the state machine's verdict and takes whichever argues harder
        for deleting -- see services/rerun_planner.py.
        """
        if self.unknown:
            return (Completeness.UNKNOWN,
                    "%d check(s) could not decide" % len(self.unknown))
        if self.fatal:
            return (Completeness.INCOMPLETE,
                    "; ".join(i.id for i in self.fatal[:3]))
        return (Completeness.COMPLETE, "all required checks passed")

    def worst_severity(self) -> Optional[Severity]:
        order = [Severity.FATAL, Severity.UNKNOWN, Severity.WARN, Severity.INFO]
        for severity in order:
            if any(i.severity == severity for i in self.issues):
                return severity
        return None


@dataclass(frozen=True)
class ExpectedArtifact:
    """A file a case should produce, derived from arcx.cfg.

        <block_name>_<QC_FLOW>/work_<QC_FLOW>/<netlist>

    The path is relative to the case run dir.
    """

    block: str
    flow: str
    relpath: str
    min_bytes: int = 0

    @property
    def flow_dir(self) -> str:
        return "%s_%s" % (self.block, self.flow)
