"""L0 Domain Model - pure data and pure functions, zero I/O.

This layer imports nothing else from arcx_auto and performs no file or process
access. Every type is a frozen dataclass, safe to share and to serialise.
"""

from arcx_auto.domain.enums import (
    CaseState,
    MarkerKind,
    Severity,
    IssueScope,
    IssueStage,
    Completeness,
    WaveState,
    PlanMode,
    LsfState,
)
from arcx_auto.domain.models import (
    LsfJobView,
    CaseObservation,
    IndexRunObservation,
    CaseSnapshot,
    IndexRunSnapshot,
    StateEvent,
    IndexSpec,
    Wave,
    WavePlan,
    DirMap,
)

__all__ = [
    "CaseState",
    "MarkerKind",
    "Severity",
    "IssueScope",
    "IssueStage",
    "Completeness",
    "WaveState",
    "PlanMode",
    "LsfState",
    "LsfJobView",
    "CaseObservation",
    "IndexRunObservation",
    "CaseSnapshot",
    "IndexRunSnapshot",
    "StateEvent",
    "IndexSpec",
    "Wave",
    "WavePlan",
    "DirMap",
]
