"""L2 Service Layer —— 業務邏輯。

只透過 L1 adapter 碰外界。其中 StateEngine 與 WavePlanner 是**完全純函數**,
可以在沒有 LSF / NFS / Arcx 的機器上完整測試 —— 這是本專案最重要的一筆設計投資,
因為真實 job 要跑好幾天, 靠實跑來驗證邏輯的迭代速度無法接受。
"""

from arcx_auto.services.state_engine import (
    TransitionContext,
    transition_case,
    transition_index_run,
    classify_completeness,
)
from arcx_auto.services.collector import Collector
from arcx_auto.services.wave_planner import plan_waves, regroup_manual

__all__ = [
    "TransitionContext",
    "transition_case",
    "transition_index_run",
    "classify_completeness",
    "Collector",
    "plan_waves",
    "regroup_manual",
]
