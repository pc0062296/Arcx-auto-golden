"""L0 Domain Model - 純資料 + 純函數, 零 I/O、零外部依賴。

這一層不 import 任何 arcx_auto 的其他模組, 也不做任何檔案/程序存取。
所有型別皆為 frozen dataclass, 可安全地共享與序列化。
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
