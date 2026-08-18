"""QA 的 domain 型別 —— 純資料, 零 I/O。

核心區分 (見 docs/architecture.md §7):

    State  唯一、互斥   —— 「這個 case 現在在哪」, 由 StateEngine 判定
    Issue  可多個並存   —— 「這個 case 有什麼問題」, 由 QA function 產出

一個 case 可以同時是 COMPLETED_MARKER 狀態, 又帶著
ARTIFACT_MISSING + MARKER_INCONSISTENT 兩個 issue。

最終狀態由 StateResolver 從 (base_state, issues) 推導, 方向單向,
所以會一直增加的判斷邏輯全部集中在 QA function 裡, StateEngine 保持精簡。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from arcx_auto.domain.enums import Completeness, IssueScope, IssueStage, Severity


@dataclass(frozen=True)
class Issue:
    """一個 QA 檢查發現的問題。

    ``evidence`` 是給人看的證據 (檔案路徑、實際值 vs 期望值), 會原樣存進
    qa/<case>.json 並顯示在 UI 上 —— 三天後回頭問「當初為什麼判它失敗」時,
    答案必須還在。
    """

    id: str
    severity: Severity
    message: str
    scope: IssueScope = IssueScope.CASE
    stage: IssueStage = IssueStage.POST
    title: str = ""
    index_key: Optional[str] = None
    case_id: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    doc: str = ""                 # 檢查的 docstring, 直接顯示在 UI 上

    @property
    def is_fatal(self) -> bool:
        return self.severity == Severity.FATAL

    @property
    def blocks_success(self) -> bool:
        """這個問題是否足以否定「成功」。

        UNKNOWN 嚴重度的檢查 (「我檢查不了」) 也算 —— 檢查不了絕不能當成通過。
        """
        return self.severity in (Severity.FATAL, Severity.UNKNOWN)


@dataclass(frozen=True)
class QaResult:
    """一個 case (或 index) 跑完所有 QA 檢查後的結果。"""

    target: str                                # case_id 或 index_key
    scope: IssueScope = IssueScope.CASE
    stage: IssueStage = IssueStage.POST
    issues: Tuple[Issue, ...] = ()
    checked_at: float = 0.0
    attempt: int = 1
    checks_run: Tuple[str, ...] = ()
    checks_failed: Tuple[str, ...] = ()        # QA function 自己爆炸的

    @property
    def fatal(self) -> Tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == Severity.FATAL)

    @property
    def unknown(self) -> Tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == Severity.UNKNOWN)

    @property
    def warnings(self) -> Tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == Severity.WARN)

    @property
    def passed(self) -> bool:
        return not any(i.blocks_success for i in self.issues)

    def completeness(self) -> Tuple[Completeness, str]:
        """rerun 時要不要刪掉這個 case 的 run dir。

        不對稱原則 (architecture §6.1): 誤刪已完成只是浪費一次運算 (可回收),
        漏刪未完成會讓殘缺結果被當成功交付 (不可回收)。
        所以「檢查不了」一律偏向刪除, 不偏向保留。
        """
        if self.unknown:
            return (Completeness.UNKNOWN,
                    "有 %d 項檢查無法判定" % len(self.unknown))
        if self.fatal:
            return (Completeness.INCOMPLETE,
                    "; ".join(i.id for i in self.fatal[:3]))
        return (Completeness.COMPLETE, "所有必要檢查通過")

    def worst_severity(self) -> Optional[Severity]:
        order = [Severity.FATAL, Severity.UNKNOWN, Severity.WARN, Severity.INFO]
        for severity in order:
            if any(i.severity == severity for i in self.issues):
                return severity
        return None


@dataclass(frozen=True)
class ExpectedArtifact:
    """從 arcx.cfg 推導出來的、某個 case 應該產出的檔案。

        <block_name>_<QC_FLOW>/work_<QC_FLOW>/<netlist>

    路徑是相對 case run dir 的。
    """

    block: str
    flow: str
    relpath: str
    min_bytes: int = 0

    @property
    def flow_dir(self) -> str:
        return "%s_%s" % (self.block, self.flow)
