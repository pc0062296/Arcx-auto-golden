"""L2 Service Layer - business logic.

Reaches the outside world only through L1 adapters. StateEngine and
WavePlanner are **completely pure functions**, testable on a machine with no
LSF, no NFS and no Arcx -- the most valuable design investment in the project,
because real jobs take days and validating logic by running them is hopeless.
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
