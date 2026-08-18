"""Domain 資料模型。

全部是 frozen dataclass —— 觀測結果與計畫都是不可變快照, 傳給純函數處理,
不會有「誰偷偷改了誰」的問題。

三組模型:
  * Observation  觀測到的「事實」    (由 Collector 產生)
  * Snapshot     推導出的「解釋」    (由 StateEngine 產生)
  * Plan         分波的「計畫」      (由 WavePlanner 產生)
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from arcx_auto.domain.enums import (
    CaseState,
    Completeness,
    LsfState,
    MarkerKind,
    PlanMode,
    WaveState,
)


# --------------------------------------------------------------------------
# 序列化 helper
# --------------------------------------------------------------------------

def _to_jsonable(value: Any) -> Any:
    """把 dataclass / enum / set / tuple 轉成可 json.dumps 的結構。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_to_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    # str-based Enum 在 asdict 之後已經是 str, 這裡處理直接傳入 enum 的情況
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return value.value
    return value


def as_json_dict(obj: Any) -> Dict[str, Any]:
    """公開的序列化入口。"""
    result = _to_jsonable(obj)
    if not isinstance(result, dict):
        raise TypeError("as_json_dict 只接受 dataclass 物件")
    return result


# --------------------------------------------------------------------------
# Observation —— 觀測到的事實
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LsfJobView:
    """從 bjobs 觀測到的單一 LSF job。

    ``exec_cwd`` 是把 job 對回 wave 目錄的關鍵 —— wave 目錄同時是
    Arcx 的隔離邊界與 LSF job 的歸屬邊界 (見 architecture §8)。
    """

    job_id: str
    state: LsfState
    exec_cwd: Optional[str] = None
    sub_cwd: Optional[str] = None
    output_file: Optional[str] = None
    exec_host: Optional[str] = None
    job_name: Optional[str] = None

    def belongs_to(self, path_prefix: str) -> bool:
        """這個 job 是否屬於某個路徑前綴底下。"""
        prefix = path_prefix.rstrip("/") + "/"
        for candidate in (self.exec_cwd, self.sub_cwd, self.output_file):
            if candidate and (candidate.rstrip("/") + "/").startswith(prefix):
                return True
        return False


@dataclass(frozen=True)
class CaseObservation:
    """單一 case 在某個時間點的觀測快照。

    這是「事實」而非「解釋」—— 只記錄看到什麼, 不做任何判斷。
    判斷交給 StateEngine (純函數) 與 QA Registry。
    """

    case_id: str
    markers: FrozenSet[MarkerKind] = frozenset()
    case_dir: Optional[str] = None        # case run dir 的絕對路徑 (rerun 時刪這個)
    case_dir_exists: bool = False
    log_path: Optional[str] = None
    log_size: Optional[int] = None
    log_mtime: Optional[float] = None
    artifacts: Tuple[str, ...] = ()       # Phase 1 由 QA 使用
    lsf: Optional[LsfJobView] = None

    @property
    def has_complete_marker(self) -> bool:
        return MarkerKind.COMPLETE in self.markers

    @property
    def has_run_marker(self) -> bool:
        return MarkerKind.RUN in self.markers

    @property
    def has_queue_marker(self) -> bool:
        return MarkerKind.QUEUE in self.markers

    @property
    def marker_inconsistent(self) -> bool:
        """.complete 已出現, 但 .run / .queue 沒被清掉。

        代表 Arcx 收尾不完整, 或是有殘留的舊 marker —— 值得人看一眼。
        """
        return self.has_complete_marker and (
            self.has_run_marker or self.has_queue_marker
        )


@dataclass(frozen=True)
class IndexRunObservation:
    """一個 index run folder 的完整觀測快照。"""

    index_key: str
    run_folder: str
    observed_at: float
    cases: Dict[str, CaseObservation] = field(default_factory=dict)
    report_dirs: Tuple[str, ...] = ()     # QC_Cc / QC_Ct / QC_Spice ...
    unmatched_entries: Tuple[str, ...] = ()  # 無法歸類的檔案, 用來發現慣例變動
    error: Optional[str] = None           # 掃描失敗時的原因 (例如路徑不存在)

    @property
    def case_count(self) -> int:
        return len(self.cases)


# --------------------------------------------------------------------------
# Snapshot —— 推導出的解釋
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseSnapshot:
    """StateEngine 對單一 case 的判定結果 + 為了下次判定而保留的記憶。

    ``last_progress_at`` / ``last_progress_size`` 是 stall 偵測的核心:
    只有在 log 實際長大時才更新, 因此 ``now - last_progress_at`` 就是
    「這個 case 安靜了多久」。用 size 而非 mtime, 是因為 NFS 上的 mtime
    不可靠, 而且有些 tool 會 touch 檔案卻沒有實質輸出。
    """

    case_id: str
    state: CaseState
    entered_state_at: float
    last_progress_at: float
    last_progress_size: int = 0
    last_seen_at: float = 0.0
    lsf_job_id: Optional[str] = None
    lsf_state: Optional[LsfState] = None
    # 第一次觀測到「應該有 LSF job 卻找不到」的時間。
    # LOST 判定需要 grace period, 否則 marker 與 LSF 的可見性落差會造成誤判。
    lsf_missing_since: Optional[float] = None
    case_dir: Optional[str] = None
    log_path: Optional[str] = None
    marker_inconsistent: bool = False
    note: Optional[str] = None

    def silent_for(self, now: float) -> float:
        """log 已經多久沒有成長 (秒)。"""
        return max(0.0, now - self.last_progress_at)


@dataclass(frozen=True)
class IndexRunSnapshot:
    """一個 index run folder 的判定結果。"""

    index_key: str
    run_folder: str
    updated_at: float
    cases: Dict[str, CaseSnapshot] = field(default_factory=dict)
    error: Optional[str] = None

    def count_by_state(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for snap in self.cases.values():
            counts[snap.state.value] = counts.get(snap.state.value, 0) + 1
        return counts

    def cases_needing_attention(self) -> List[CaseSnapshot]:
        return [s for s in self.cases.values() if s.state.needs_attention]


@dataclass(frozen=True)
class StateEvent:
    """一次狀態轉移。append-only 寫進 events.jsonl, 永不改寫。"""

    ts: float
    index_key: str
    case_id: str
    from_state: Optional[CaseState]
    to_state: CaseState
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Plan —— 分波計畫
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DirMap:
    """解析後的 dir_map。

    ``meta`` 存 min/max 這類保留 key —— 它們不是真實 index, 不可拿去跑。
    """

    source_path: str
    entries: Dict[str, str] = field(default_factory=dict)
    meta: Dict[str, str] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()

    def resolve(self, index_key: str) -> Optional[str]:
        return self.entries.get(index_key)

    def keys_sorted(self) -> List[str]:
        """數字 index 依數值排序, 非數字的排在後面依字母序。"""

        def sort_key(k: str) -> Tuple[int, float, str]:
            try:
                return (0, float(k), "")
            except ValueError:
                return (1, 0.0, k)

        return sorted(self.entries.keys(), key=sort_key)


@dataclass(frozen=True)
class IndexSpec:
    """一個 index 的資源需求描述, WavePlanner 的輸入單位。

    ``slots = cpu_per_case × gds_count``
    其中 cpu_per_case 來自 <index_path>/special.cfg 的 O_QCAP_LSF_NUM。
    """

    index_key: str
    path: str
    gds_count: int
    cpu_per_case: int
    keywords: Tuple[str, ...] = ()
    priority: int = 0
    warnings: Tuple[str, ...] = ()
    error: Optional[str] = None

    @property
    def slots(self) -> int:
        return self.cpu_per_case * self.gds_count

    @property
    def usable(self) -> bool:
        """資料完整到可以排進 wave。"""
        return self.error is None and self.gds_count > 0 and self.cpu_per_case > 0


@dataclass(frozen=True)
class Wave:
    """一批一起送出的 index —— 對應一條 Arcx 指令 + 一個隔離目錄。"""

    seq: int
    indices: Tuple[IndexSpec, ...]
    state: WaveState = WaveState.PLANNED

    @property
    def name(self) -> str:
        return "wave_%03d" % self.seq

    @property
    def total_slots(self) -> int:
        return sum(i.slots for i in self.indices)

    @property
    def total_cases(self) -> int:
        return sum(i.gds_count for i in self.indices)

    @property
    def index_keys(self) -> Tuple[str, ...]:
        return tuple(i.index_key for i in self.indices)


@dataclass(frozen=True)
class WavePlan:
    """完整的分波計畫 —— 純資料, 可預覽 / 可編輯 / 可存檔重放。

    刻意不在這裡執行任何動作 (見 architecture 決策 5)。
    """

    mode: PlanMode
    max_slots_per_wave: int
    waves: Tuple[Wave, ...] = ()
    excluded: Tuple[IndexSpec, ...] = ()   # 資料不完整, 無法排入
    warnings: Tuple[str, ...] = ()
    created_at: float = 0.0

    @property
    def total_slots(self) -> int:
        return sum(w.total_slots for w in self.waves)

    @property
    def total_cases(self) -> int:
        return sum(w.total_cases for w in self.waves)

    @property
    def oversized_waves(self) -> Tuple[Wave, ...]:
        """單一 wave 的 slot 需求就超過上限 —— 通常代表某個 index 特別大,
        或 max_slots_per_wave 設得太低。計畫階段就該讓人知道。
        """
        return tuple(
            w for w in self.waves if w.total_slots > self.max_slots_per_wave
        )
