"""把 base_state 與 QA issue 收斂成最終狀態。**純函數**。

為什麼要有這一層 (見 docs/architecture.md §7):

    StateEngine  只做結構性判定 (marker + LSF), 幾乎不需要改
    QA Registry  做所有會一直增加的判斷, 產出 Issue
    StateResolver 把兩者合併成唯一的 final state

好處: 「卡住的門檻怎麼算」這種會反覆調整的邏輯全部留在 QA function 裡,
不會讓 StateEngine 長成大雜燴; 而 state 仍然是唯一且互斥的。

方向永遠是單向的: issue -> state, 不會反過來。
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

from arcx_auto.domain.enums import CaseState, Severity
from arcx_auto.domain.qa import Issue

#: 出現這些 issue 時, RUNNING 會被升級成 STALLED
STALL_ISSUE_IDS = ("CASE_QUIET",)


def resolve_case_state(
    base_state: CaseState,
    issues: Sequence[Issue],
) -> Tuple[CaseState, str]:
    """回傳 (final_state, reason)。

    規則刻意只有三條, 而且互不重疊:

      1. COMPLETED_MARKER + 任何足以否定成功的 issue  -> FAILED
      2. COMPLETED_MARKER + 全部通過                  -> DONE
      3. RUNNING + FATAL 等級的 CASE_QUIET            -> STALLED

    其餘狀態原樣保留 —— QA 不該去改寫 LOST / SUSPENDED 這類由 LSF
    直接證實的事實。
    """
    blocking = [i for i in issues if i.blocks_success]

    if base_state == CaseState.COMPLETED_MARKER:
        if blocking:
            worst = _worst(blocking)
            return (
                CaseState.FAILED,
                "QA 未通過: %s" % ", ".join(sorted({i.id for i in blocking})[:3]),
            ) if worst == Severity.FATAL else (
                CaseState.FAILED,
                "QA 無法確認成功: %s" % ", ".join(
                    sorted({i.id for i in blocking})[:3]),
            )
        return (CaseState.DONE, "QA 全部通過")

    if base_state == CaseState.RUNNING:
        for issue in issues:
            if issue.id in STALL_ISSUE_IDS and issue.severity == Severity.FATAL:
                return (CaseState.STALLED, issue.message)

    return (base_state, "")


def _worst(issues: Iterable[Issue]) -> Optional[Severity]:
    for severity in (Severity.FATAL, Severity.UNKNOWN, Severity.WARN,
                     Severity.INFO):
        if any(i.severity == severity for i in issues):
            return severity
    return None
