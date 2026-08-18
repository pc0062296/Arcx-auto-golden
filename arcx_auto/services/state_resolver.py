"""Narrow (base_state, issues) into one final state. **Pure.**

Why this layer exists (docs/architecture.md 7):

    StateEngine    structural only (markers + LSF); barely ever changes
    QA Registry    every judgement that keeps growing, emitted as Issues
    StateResolver  combines them into the one final state

The payoff is that logic like "how long counts as stuck", which gets tuned
repeatedly, stays inside QA functions instead of growing into StateEngine --
while the state itself remains single and exclusive.

The direction is always one way: issue -> state, never the reverse.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

from arcx_auto.domain.enums import CaseState, Severity
from arcx_auto.domain.qa import Issue

#: Issues that promote RUNNING to STALLED
STALL_ISSUE_IDS = ("CASE_QUIET",)


def resolve_case_state(
    base_state: CaseState,
    issues: Sequence[Issue],
) -> Tuple[CaseState, str]:
    """Return (final_state, reason).

    Only three rules, deliberately non-overlapping:

      1. COMPLETED_MARKER + anything that denies success -> FAILED
      2. COMPLETED_MARKER + everything passed            -> DONE
      3. RUNNING + a FATAL CASE_QUIET                    -> STALLED

    Every other state is left alone: QA has no business rewriting LOST or
    SUSPENDED, which LSF confirmed directly.
    """
    blocking = [i for i in issues if i.blocks_success]

    if base_state == CaseState.COMPLETED_MARKER:
        if blocking:
            worst = _worst(blocking)
            names = ", ".join(sorted({i.id for i in blocking})[:3])
            if worst == Severity.FATAL:
                return (CaseState.FAILED, "QA failed: %s" % names)
            return (CaseState.FAILED,
                    "QA could not confirm success: %s" % names)
        return (CaseState.DONE, "all QA checks passed")

    if base_state == CaseState.RUNNING:
        for issue in issues:
            if issue.id in STALL_ISSUE_IDS and issue.severity == Severity.FATAL:
                return (CaseState.STALLED, issue.message)

    return (base_state, "")


def _worst(issues: Iterable[Issue]) -> Optional[Severity]:
    for severity in (Severity.FATAL, Severity.UNKNOWN, Severity.WARN,
                     Severity.INFO):
        if any(i.severity == severity for i in issues):
            return severity
    return None
