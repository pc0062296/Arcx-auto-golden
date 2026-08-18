"""列舉型別。

刻意使用 str 作為基底, 讓 JSON 序列化 / CLI 輸出 / 設定檔比對都能直接用字串,
不需要額外的轉換層。
"""

from __future__ import annotations

from enum import Enum


class MarkerKind(str, Enum):
    """Arcx 在 index run folder 內建立的隱藏 marker 檔種類。

    檔名形式為 ``.<kind>.<case_id>``, 例如 ``.complete.case1``。
    """

    QUEUE = "queue"
    RUN = "run"
    COMPLETE = "complete"


class LsfState(str, Enum):
    """LSF job 狀態 (取自 bjobs 的 STAT 欄位)。"""

    PEND = "PEND"
    RUN = "RUN"
    PSUSP = "PSUSP"
    USUSP = "USUSP"
    SSUSP = "SSUSP"
    DONE = "DONE"
    EXIT = "EXIT"
    UNKWN = "UNKWN"
    ZOMBI = "ZOMBI"

    @property
    def is_suspended(self) -> bool:
        return self in (LsfState.PSUSP, LsfState.USUSP, LsfState.SSUSP)

    @property
    def is_active(self) -> bool:
        """job 仍佔用或等待資源 (尚未結案)。"""
        return self in (
            LsfState.PEND,
            LsfState.RUN,
            LsfState.PSUSP,
            LsfState.USUSP,
            LsfState.SSUSP,
            LsfState.UNKWN,
        )


class CaseState(str, Enum):
    """單一 case 的狀態。

    比使用者原本提的 queue/run/fail/done 更細, 因為實務上最麻煩的幾類
    (suspended / stalled / lost / 假成功) 都藏在那四態之間。

    重點: ``COMPLETED_MARKER`` 與 ``DONE`` 刻意分開 —— ``.complete.caseN``
    只代表 Arcx 認為它跑完了, 不代表結果正確。中間必須經過 QA 驗證 (Phase 1)。
    """

    PENDING = "PENDING"                    # 已知有這個 case, 但還沒有任何 marker
    QUEUED = "QUEUED"                      # .queue marker / LSF PEND
    RUNNING = "RUNNING"                    # .run marker / LSF RUN
    SUSPENDED = "SUSPENDED"                # LSF 回報 *SUSP
    STALLED = "STALLED"                    # 看似在跑, 但 log 長時間無成長
    COMPLETED_MARKER = "COMPLETED_MARKER"  # .complete 出現, 尚未通過 QA
    DONE = "DONE"                          # 通過 QA           [Phase 1]
    FAILED = "FAILED"                      # QA 判定失敗        [Phase 1]
    LOST = "LOST"                          # marker 停在中途, LSF job 已不存在
    UNKNOWN = "UNKNOWN"                    # 觀測資料不足以判定

    @property
    def is_terminal(self) -> bool:
        return self in (CaseState.DONE, CaseState.FAILED)

    @property
    def is_in_flight(self) -> bool:
        """仍在 LSF 手上 (佔用或等待資源)。"""
        return self in (
            CaseState.QUEUED,
            CaseState.RUNNING,
            CaseState.SUSPENDED,
            CaseState.STALLED,
        )

    @property
    def needs_attention(self) -> bool:
        """需要人看一眼的狀態。"""
        return self in (
            CaseState.SUSPENDED,
            CaseState.STALLED,
            CaseState.LOST,
            CaseState.FAILED,
            CaseState.UNKNOWN,
        )


class Completeness(str, Enum):
    """rerun 時判定「這個 case 的 run dir 要不要刪掉重跑」。

    刻意保留 UNKNOWN 三態 (而非 bool): 見 docs/architecture.md §6.1 ——
    在這個決策上, 「不確定 → 傾向刪掉重跑」是安全方向, 因為誤刪的代價
    (浪費一次運算) 可回收, 漏刪的代價 (殘缺結果被當成功) 不可回收。
    """

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"


class Severity(str, Enum):
    """QA issue 的嚴重度。

    UNKNOWN 是刻意存在的一等公民: 「我檢查不了」絕不能被當成「通過」。
    讀不到 case run dir、格式不認得、QA function 自己爆炸 —— 這些都是
    UNKNOWN, 而 UNKNOWN 在 rerun 判定上偏向「刪掉重跑」(architecture §6.1)。
    """

    INFO = "INFO"
    WARN = "WARN"
    UNKNOWN = "UNKNOWN"
    FATAL = "FATAL"


class IssueScope(str, Enum):
    """QA 檢查的作用範圍。"""

    CASE = "CASE"
    INDEX = "INDEX"
    WAVE = "WAVE"
    GLOBAL = "GLOBAL"


class IssueStage(str, Enum):
    """QA 檢查的執行時機。"""

    PRE = "PRE"    # 提交前
    LIVE = "LIVE"  # 執行中
    POST = "POST"  # 完成後


class WaveState(str, Enum):
    """一個 wave (一條 Arcx 指令 + 一個隔離目錄) 的生命週期。"""

    PLANNED = "PLANNED"
    WAITING_GATE = "WAITING_GATE"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    MONITORING = "MONITORING"
    DONE = "DONE"
    ABORTED = "ABORTED"


class PlanMode(str, Enum):
    """分波模式。

    三者共用同一個 WavePlan 結構 —— OFF 只是「只有一個 wave」的特例,
    MANUAL 只是「分組由人指定」。下游完全不需要分支處理。
    """

    AUTO = "AUTO"
    MANUAL = "MANUAL"
    OFF = "OFF"
