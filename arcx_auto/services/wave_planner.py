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
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from arcx_auto.domain.enums import PlanMode
from arcx_auto.domain.models import IndexSpec, SubmitGroup, Wave, WavePlan


def plan_waves(
    specs: Sequence[IndexSpec],
    max_slots_per_wave: int,
    mode: PlanMode = PlanMode.AUTO,
    now: Optional[float] = None,
    keep_folders_together: bool = True,
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

    ``keep_folders_together`` makes the **parent directory** the unit instead
    of the index. The directory structure is already how the work is
    classified, so a wave boundary through the middle of one scatters related
    cases across batches that start hours apart, and somebody debugging has to
    reassemble them by hand. With it on:

      * the priority sort works on folders (a folder is as urgent as its most
        urgent index), because sorting individual indices is itself one of the
        things that tears a folder apart;
      * a folder is never split, **even when it exceeds the cap on its own**.
        That is a deliberate choice to go over the cap rather than lose the
        grouping, and it is reported -- as a plan warning and as a PRE check --
        rather than done quietly.
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
    elif keep_folders_together:
        blocks = _folder_blocks(usable)
        waves = _fill_waves_by_folder(blocks, max_slots_per_wave)
        warnings.extend(_folder_warnings(blocks, max_slots_per_wave))
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


def plan_groups(
    groups: Sequence[Tuple[SubmitGroup, Sequence[IndexSpec]]],
    max_slots_per_wave: int,
    mode: PlanMode = PlanMode.AUTO,
    now: Optional[float] = None,
) -> WavePlan:
    """Plan several groups into one ordered list of waves.

    Each group is a person's selection: its own dir_map, its own arcx.cfg, and
    the indices they ticked. Groups are planned **separately**, because a wave
    is one Arcx command against one cfg and mixing two cfgs into one wave is
    not a thing Arcx can do. They are then concatenated in the order the person
    built them.

    Wave numbering is global across groups, so wave_001 is the first thing that
    goes out no matter which group it came from -- the gate releases one wave at
    a time and the number is the order it releases in.
    """
    now = now if now is not None else time.time()
    waves: List[Wave] = []
    excluded: List[IndexSpec] = []
    warnings: List[str] = []

    for group, specs in groups:
        # Per group, because only the person who made the selection knows
        # whether its directory structure means anything.
        part = plan_waves(
            specs, max_slots_per_wave, mode=mode, now=now,
            keep_folders_together=getattr(group, "keep_folders_together", True))
        excluded.extend(part.excluded)
        for warning in part.warnings:
            warnings.append("%s: %s" % (group.label, warning))
        for wave in part.waves:
            waves.append(replace(
                wave,
                seq=len(waves) + 1,
                group=group.name,
                dir_map=group.dir_map,
                arcx_cfg=group.arcx_cfg,
            ))

    plan = WavePlan(
        mode=mode,
        max_slots_per_wave=max_slots_per_wave,
        waves=tuple(waves),
        excluded=tuple(excluded),
        warnings=tuple(warnings),
        created_at=now,
    )
    return _with_oversize_warnings(plan)


def _stable_priority_sort(specs: Iterable[IndexSpec]) -> List[IndexSpec]:
    """Higher priority first; original selection order kept within a priority.

    Python's sorted is stable, so sorting on priority alone preserves order.
    """
    return sorted(specs, key=lambda s: -s.priority)


def _folder_blocks(
    specs: Sequence[IndexSpec],
) -> List[Tuple[str, List[IndexSpec]]]:
    """Group indices by folder, folders in priority then first-seen order.

    A folder is as urgent as its most urgent index. Sorting the indices
    themselves -- which is what happens without this -- pulls every keyword hit
    to the front of the batch and out of the folder it came from, which is
    exactly the scattering this exists to stop.
    """
    order: List[str] = []
    members: Dict[str, List[IndexSpec]] = {}
    for spec in specs:
        folder = spec.folder
        if folder not in members:
            members[folder] = []
            order.append(folder)
        members[folder].append(spec)

    def priority_of(folder: str) -> int:
        return max(s.priority for s in members[folder])

    ordered = sorted(order, key=lambda f: -priority_of(f))
    return [(folder, members[folder]) for folder in ordered]


def _fill_waves_by_folder(
    blocks: Sequence[Tuple[str, List[IndexSpec]]], max_slots: int,
) -> Tuple[Wave, ...]:
    """Fill waves a whole folder at a time.

    A folder that does not fit in what is left starts the next wave; a folder
    that does not fit in an empty wave still goes in whole. Small folders
    therefore still share a wave, which matters more than it looks: the gate
    releases one wave at a time with a minimum interval between them, so one
    wave per folder would turn twenty small folders into hours of waiting for
    work that would fit in a single batch.

    No reordering to fill the gaps. First fit in the order the person chose
    keeps "why is this index in this wave" answerable, which is worth more
    than the few percent of slot utilisation a cleverer packing would win
    (architecture 5.2).
    """
    waves: List[Wave] = []
    current: List[IndexSpec] = []
    current_slots = 0

    for _folder, members in blocks:
        block_slots = sum(s.slots for s in members)
        if current and current_slots + block_slots > max_slots:
            waves.append(Wave(seq=len(waves) + 1, indices=tuple(current)))
            current = []
            current_slots = 0
        current.extend(members)
        current_slots += block_slots

    if current:
        waves.append(Wave(seq=len(waves) + 1, indices=tuple(current)))
    return tuple(waves)


def _folder_warnings(blocks: Sequence[Tuple[str, List[IndexSpec]]],
                     max_slots: int) -> List[str]:
    """Name the folders that put a wave over the cap, and why they did.

    The oversize warning alone says a wave is too big, which reads like a bug.
    This says which folder caused it and that keeping it whole was the
    instruction -- so the reader can decide between raising the cap and
    turning the grouping off for that group.
    """
    out: List[str] = []
    for folder, members in blocks:
        slots = sum(s.slots for s in members)
        if slots > max_slots:
            out.append(
                "folder %s needs %d slots on its own, over the cap of %d; it "
                "is kept in one wave because this group keeps folders "
                "together (indices: %s)"
                % (folder, slots, max_slots,
                   ", ".join(s.index_key for s in members)))
    return out


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
