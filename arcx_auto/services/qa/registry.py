"""QA 檢查的註冊與執行。

一條檢查長這樣:

    @qa_check(id="NETLIST_MISSING", title="netlist 缺失",
              severity=Severity.FATAL, scope=Scope.CASE, stage=Stage.POST)
    def netlist_missing(case: CaseContext):
        '''這段 docstring 會直接顯示在 UI 上。'''
        missing = case.missing_artifacts()
        if not missing:
            return None
        return case.fail("缺少 %d 個產出物" % len(missing),
                         evidence={"missing": [a.relpath for a in missing]})

回傳 None / [] 代表通過; 回傳 Issue 或 List[Issue] 代表發現問題。

三個關鍵性質:

  1. **錯誤隔離。** 既然要讓工程師自己加規則, 他們寫的 function 一定會有 bug。
     每條檢查都在 try 裡跑, 爆炸的轉成 QA_INTERNAL_ERROR issue (附 traceback)
     並繼續跑其他檢查。一條壞規則絕不能讓整個監控停擺。

  2. **id 是介面。** policy.yaml 只認 id 不認實作, 所以改檢查的實作不需要動
     policy。重複註冊同一個 id 會直接報錯 —— 靜默覆蓋是最難查的那種問題。

  3. **可停用。** 設定裡可以關掉個別檢查 (disabled_checks), 不需要刪程式。
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from arcx_auto.domain.enums import IssueScope, IssueStage, Severity
from arcx_auto.domain.qa import Issue, QaResult
from arcx_auto.services.qa.context import CaseContext, IndexContext, _BaseContext

CheckReturn = Union[None, Issue, Sequence[Issue]]
CheckFunc = Callable[..., CheckReturn]

INTERNAL_ERROR_ID = "QA_INTERNAL_ERROR"


@dataclass(frozen=True)
class CheckSpec:
    """一條註冊好的檢查。"""

    id: str
    func: CheckFunc
    title: str
    severity: Severity
    scope: IssueScope
    stage: IssueStage
    doc: str = ""

    def metadata(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "scope": self.scope,
            "stage": self.stage,
            "doc": self.doc,
            "severity": self.severity,
        }


class QaRegistry:
    """所有 QA 檢查的登記處。"""

    def __init__(self) -> None:
        self._checks: Dict[str, CheckSpec] = {}

    # -- 註冊 ---------------------------------------------------------

    def register(self, spec: CheckSpec) -> None:
        if spec.id in self._checks:
            raise ValueError(
                "QA 檢查 id 重複: %s (已由 %s 註冊)"
                % (spec.id, self._checks[spec.id].func.__name__)
            )
        self._checks[spec.id] = spec

    def check(
        self,
        id: str,
        title: str,
        severity: Severity = Severity.FATAL,
        scope: IssueScope = IssueScope.CASE,
        stage: IssueStage = IssueStage.POST,
    ) -> Callable[[CheckFunc], CheckFunc]:
        """裝飾器。"""

        def decorator(func: CheckFunc) -> CheckFunc:
            self.register(CheckSpec(
                id=id, func=func, title=title, severity=severity,
                scope=scope, stage=stage,
                doc=(func.__doc__ or "").strip(),
            ))
            return func

        return decorator

    # -- 查詢 ---------------------------------------------------------

    def all(self) -> Tuple[CheckSpec, ...]:
        return tuple(self._checks.values())

    def select(
        self,
        scope: IssueScope,
        stage: IssueStage,
        disabled: Iterable[str] = (),
    ) -> Tuple[CheckSpec, ...]:
        blocked = set(disabled)
        return tuple(
            spec for spec in self._checks.values()
            if spec.scope == scope and spec.stage == stage
            and spec.id not in blocked
        )

    # -- 執行 ---------------------------------------------------------

    def run(
        self,
        context: _BaseContext,
        scope: IssueScope,
        stage: IssueStage,
        target: str,
        disabled: Iterable[str] = (),
        attempt: int = 1,
    ) -> QaResult:
        """跑符合 scope/stage 的所有檢查, 收集 issue。"""
        issues: List[Issue] = []
        ran: List[str] = []
        crashed: List[str] = []

        for spec in self.select(scope, stage, disabled):
            ran.append(spec.id)
            context._current = spec.metadata()
            try:
                outcome = spec.func(context)
            except Exception:  # noqa: BLE001 - 刻意攔截所有例外, 見模組 docstring
                crashed.append(spec.id)
                issues.append(Issue(
                    id=INTERNAL_ERROR_ID,
                    severity=Severity.UNKNOWN,
                    message="檢查 %s 執行時發生例外" % spec.id,
                    scope=scope,
                    stage=stage,
                    title="QA 檢查本身出錯",
                    index_key=getattr(context, "index_key", None),
                    case_id=getattr(context, "case_id", None),
                    evidence={
                        "check_id": spec.id,
                        "traceback": traceback.format_exc(limit=8),
                    },
                    doc="一條檢查自己爆炸了。這不代表 case 有問題, 而是規則有 bug, "
                        "但也不能當成通過 —— 所以嚴重度是 UNKNOWN。",
                ))
                continue
            finally:
                context._current = {}

            issues.extend(_normalize(outcome))

        return QaResult(
            target=target,
            scope=scope,
            stage=stage,
            issues=tuple(issues),
            checked_at=context.now,
            attempt=attempt,
            checks_run=tuple(ran),
            checks_failed=tuple(crashed),
        )


def _normalize(outcome: CheckReturn) -> List[Issue]:
    if outcome is None:
        return []
    if isinstance(outcome, Issue):
        return [outcome]
    return [i for i in outcome if isinstance(i, Issue)]


# 全域 registry。checks_*.py 匯入時會把自己註冊進來。
REGISTRY = QaRegistry()
qa_check = REGISTRY.check
