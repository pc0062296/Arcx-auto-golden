"""WavePlanner —— **完全純函數**, 零 I/O。

輸入: 一組 IndexSpec (資源需求已由 ArcxAdapter 讀好)
輸出: WavePlan (純資料, 可預覽 / 可編輯 / 可存檔重放)

刻意不做 bin-packing 最佳化 (architecture §5.2):
使用者的選取順序與關鍵字優先權是明確意圖, 重排會讓結果不可預期。
在估算本身就有誤差的情況下, 用複雜演算法去優化幾個百分點沒有意義,
而「工程師看得懂為什麼這個 index 被放在這一波」才是真正重要的。
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
    """把 index 切成數個 wave。

    步驟:
      1. 剔除資料不完整的 index (放進 excluded, 不靜默丟掉)
      2. 依 priority 穩定排序 (關鍵字命中者優先, 同 priority 保持原順序)
      3. 依 slot 上限依序填波

    ``mode`` 只影響切波方式, 不影響輸出結構 —— OFF 就是「只有一個 wave」的特例,
    因此下游 (WorkspaceBuilder / Launcher / 監控) 完全不需要分支處理。
    """
    now = now if now is not None else time.time()
    warnings: List[str] = []

    usable = [s for s in specs if s.usable]
    excluded = tuple(s for s in specs if not s.usable)
    for spec in excluded:
        warnings.append(
            "index %s 被排除: %s" % (spec.index_key, spec.error or "資料不完整")
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
            "max_slots_per_wave = %d 無效, 視同關閉分批" % max_slots_per_wave
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
    """priority 高的排前面; 同 priority 保持使用者原本的選取順序。

    Python 的 sorted 是穩定排序, 所以只需要對 priority 排序即可保住原順序。
    """
    return sorted(specs, key=lambda s: -s.priority)


def _fill_waves(specs: Sequence[IndexSpec], max_slots: int) -> Tuple[Wave, ...]:
    """依序填波: 加進去會超過上限就開新的一波。

    單一 index 本身就超過上限時, 讓它自己獨佔一波 (而不是硬塞或拒絕) ——
    拒絕會讓使用者完全跑不了, 硬塞會讓其他 index 跟著被拖累。
    獨佔一波並在計畫上標記 OVERSIZED, 是唯一既能跑又能讓人知道的作法。
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
    """把超量的 wave 寫成明確警告, 讓人在計畫階段就看到。"""
    oversized = plan.oversized_waves
    if not oversized:
        return plan
    warnings = list(plan.warnings)
    for wave in oversized:
        warnings.append(
            "%s 需要 %d slots, 超過上限 %d (index: %s)"
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
    """手動分批: 由使用者指定每個 wave 包含哪些 index。

    仍然套用同一套 oversize 檢查 —— 手動分的也可能超量, 該提醒還是要提醒。
    未被任何 group 收錄的 index 會進 warnings, 不會被靜默丟掉。
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
                warnings.append("手動分批指定了不存在的 index: %s" % key)
                continue
            if key in used:
                # 同一 index 只能屬於一個 wave —— 否則 Arcx 會在兩個
                # 隔離目錄中同時處理同一批 GDS, 結果無法預期。
                warnings.append("index %s 出現在多個 wave, 已忽略重複" % key)
                continue
            if not spec.usable:
                warnings.append(
                    "index %s 被排除: %s" % (key, spec.error or "資料不完整")
                )
                continue
            used.add(key)
            members.append(spec)
        if members:
            waves.append(Wave(seq=len(waves) + 1, indices=tuple(members)))

    leftover = [s for s in specs if s.index_key not in used and s.usable]
    for spec in leftover:
        warnings.append("index %s 未被指派到任何 wave" % spec.index_key)

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
