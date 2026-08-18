"""WavePlanner -- **completely pure**, zero I/O.

    in:  a sequence of IndexSpec (resource footprints already read by
         ArcxAdapter)
    out: a WavePlan -- pure data, previewable, editable, replayable

No bin-packing optimisation, on purpose (architecture 5.2): the selection order
and the keyword priority are explicit intent, and reordering makes the result
unpredictable. When the sizing itself is approximate, a clever algorithm
chasing a few percent is pointless, whereas "an engineer can see why this index
landed in this wave" genuinely matters.
"""

from __future__ import annotations

import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from arcx_auto.domain.enums import PlanMode
from arcx_auto.domain.models import IndexSpec, Wave, WavePlan


def plan_waves(
    specs: Sequence[IndexSpec],
    max_slots_per_wave: int,
    mode: PlanMode = PlanMode.AUTO,
    now: Optional[float] = None,
) -> WavePlan:
    """Split indices into waves.

    Steps:
      1. drop indices with incomplete data into `excluded` rather than
         silently discarding them
      2. stable sort by priority (keyword hits first, original order preserved
         within a priority)
      3. fill waves in order up to the slot cap

    ``mode`` only affects how waves are cut, never the output shape: OFF is
    just "a single wave", so nothing downstream (WorkspaceBuilder, Launcher,
    monitoring) needs to branch on it.
    """
    now = now if now is not None else time.time()
    warnings: List[str] = []

    usable = [s for s in specs if s.usable]
    excluded = tuple(s for s in specs if not s.usable)
    for spec in excluded:
        warnings.append(
            "index %s excluded: %s" % (spec.index_key, spec.error or "incomplete data")
        )

    if not usable:
        return WavePlan(
            mode=mode,
            max_slots_per_wave=max_slots_per_wave,
            waves=(),
            excluded=excluded,
            warnings=tuple(warnings),
            created_at=now,
        )

    if max_slots_per_wave <= 0:
        warnings.append(
            "max_slots_per_wave = %d is invalid; wave splitting disabled"
            % max_slots_per_wave
        )
        mode = PlanMode.OFF

    if mode == PlanMode.OFF:
        waves = (Wave(seq=1, indices=tuple(usable)),)
    else:
        ordered = _stable_priority_sort(usable)
        waves = _fill_waves(ordered, max_slots_per_wave)

    plan = WavePlan(
        mode=mode,
        max_slots_per_wave=max_slots_per_wave,
        waves=waves,
        excluded=excluded,
        warnings=tuple(warnings),
        created_at=now,
    )
    return _with_oversize_warnings(plan)


def _stable_priority_sort(specs: Iterable[IndexSpec]) -> List[IndexSpec]:
    """Higher priority first; original selection order kept within a priority.

    Python's sorted is stable, so sorting on priority alone preserves order.
    """
    return sorted(specs, key=lambda s: -s.priority)


def _fill_waves(specs: Sequence[IndexSpec], max_slots: int) -> Tuple[Wave, ...]:
    """Fill waves in order, starting a new one when the cap would be exceeded.

    An index that exceeds the cap on its own gets a wave to itself rather than
    being refused or crammed in: refusing means it can never run, and cramming
    drags the other indices down with it. A dedicated wave plus an OVERSIZED
    marker in the plan is the only option that both runs and stays visible.
    """
    waves: List[Wave] = []
    current: List[IndexSpec] = []
    current_slots = 0

    for spec in specs:
        if current and current_slots + spec.slots > max_slots:
            waves.append(Wave(seq=len(waves) + 1, indices=tuple(current)))
            current = []
            current_slots = 0
        current.append(spec)
        current_slots += spec.slots

    if current:
        waves.append(Wave(seq=len(waves) + 1, indices=tuple(current)))
    return tuple(waves)


def _with_oversize_warnings(plan: WavePlan) -> WavePlan:
    """Turn oversized waves into explicit warnings, visible while planning."""
    oversized = plan.oversized_waves
    if not oversized:
        return plan
    warnings = list(plan.warnings)
    for wave in oversized:
        warnings.append(
            "%s needs %d slots, over the cap of %d (indices: %s)"
            % (
                wave.name,
                wave.total_slots,
                plan.max_slots_per_wave,
                ", ".join(wave.index_keys),
            )
        )
    return WavePlan(
        mode=plan.mode,
        max_slots_per_wave=plan.max_slots_per_wave,
        waves=plan.waves,
        excluded=plan.excluded,
        warnings=tuple(warnings),
        created_at=plan.created_at,
    )


def regroup_manual(
    specs: Sequence[IndexSpec],
    groups: Sequence[Sequence[str]],
    max_slots_per_wave: int,
    now: Optional[float] = None,
) -> WavePlan:
    """Manual batching: the user says which indices go in which wave.

    The same oversize check still applies -- a hand-made grouping can be over
    the cap too, and that still deserves a warning. Indices not placed in any
    group are reported rather than silently dropped.
    """
    now = now if now is not None else time.time()
    by_key: Dict[str, IndexSpec] = {s.index_key: s for s in specs}
    warnings: List[str] = []
    used: set = set()
    waves: List[Wave] = []

    for group in groups:
        members: List[IndexSpec] = []
        for key in group:
            spec = by_key.get(key)
            if spec is None:
                warnings.append("manual grouping names an unknown index: %s" % key)
                continue
            if key in used:
                # An index may belong to only one wave, or Arcx would process
                # the same GDS files in two isolated directories at once with
                # unpredictable results.
                warnings.append(
                    "index %s appears in more than one wave; duplicate ignored"
                    % key)
                continue
            if not spec.usable:
                warnings.append(
                    "index %s excluded: %s" % (key, spec.error or "incomplete data")
                )
                continue
            used.add(key)
            members.append(spec)
        if members:
            waves.append(Wave(seq=len(waves) + 1, indices=tuple(members)))

    leftover = [s for s in specs if s.index_key not in used and s.usable]
    for spec in leftover:
        warnings.append("index %s was not assigned to any wave" % spec.index_key)

    excluded = tuple(s for s in specs if not s.usable)

    plan = WavePlan(
        mode=PlanMode.MANUAL,
        max_slots_per_wave=max_slots_per_wave,
        waves=tuple(waves),
        excluded=excluded,
        warnings=tuple(warnings),
        created_at=now,
    )
    return _with_oversize_warnings(plan)
