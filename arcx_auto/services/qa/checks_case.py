"""case 層級的 QA 檢查。

**這個檔案是給人讀和改的。** 每條檢查應該短到一眼看完, 而且 docstring
就是它在 UI 上的說明 —— 文件與程式不會走歪。

要新增一條檢查: 複製一個既有的, 改 id / title / severity, 寫邏輯。
要停用一條: 設定裡的 qa.disabled_checks 加上它的 id, 不用刪程式。

期望產出物的結構 (由 arcx.cfg 推導, 見 expectations.py):

    <case_run_dir>/<block>_<QC_FLOW>/work_<QC_FLOW>/<netlist>
"""

from __future__ import annotations

from typing import List, Optional

from arcx_auto.domain.enums import CaseState, IssueScope, IssueStage, Severity
from arcx_auto.domain.qa import Issue
from arcx_auto.services.qa.context import CaseContext
from arcx_auto.services.qa.registry import qa_check

CASE = IssueScope.CASE
POST = IssueStage.POST
LIVE = IssueStage.LIVE


# ===========================================================================
# POST —— .complete 出現後跑一次, 回答「它真的成功了嗎」
# ===========================================================================

@qa_check(id="CASE_DIR_MISSING", title="case run dir 不存在",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def case_dir_missing(case: CaseContext) -> Optional[Issue]:
    """marker 說跑完了, 但 case 的 run dir 根本不在。

    通常是目錄被誤刪, 或 marker 是上一輪殘留的。
    """
    if case.case_dir_exists:
        return None
    return case.fail("找不到 case run dir", evidence={"path": case.root})


@qa_check(id="CFG_EXPECTATION_UNAVAILABLE", title="無法得知該檢查什麼",
          severity=Severity.UNKNOWN, scope=CASE, stage=POST)
def cfg_expectation_unavailable(case: CaseContext) -> Optional[Issue]:
    """沒有 arcx.cfg, 或 cfg 沒能推導出任何期望產出物。

    這種情況**不能當成通過** —— 我們只是不知道該檢查什麼而已。
    提交時應該要有 cfg 快照; 沒有的話代表 workspace 不完整。
    """
    if case.expected_artifacts:
        return None
    if case.arcx_config is None:
        return case.unknown(
            "沒有 arcx.cfg 快照, 無法推導期望產出物",
            evidence={"hint": "提交時應把 arcx.cfg 快照進 wave 目錄"},
        )
    return case.unknown(
        "arcx.cfg 沒有推導出任何期望產出物",
        evidence={"problems": list(case.expectation_problems)},
    )


@qa_check(id="CFG_EXPECTATION_PROBLEM", title="arcx.cfg 有問題",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def cfg_expectation_problem(case: CaseContext) -> Optional[Issue]:
    """cfg 的 block 缺 QC_FLOW, 或 QC_FLOW 是系統不認得的 flow。

    不認得的 flow 代表我們無法驗證它的產出 —— 要嘛 cfg 寫錯,
    要嘛 settings.qa.flows 需要補上這個 flow 的定義。
    """
    problems = case.expectation_problems
    if not problems:
        return None
    return case.fail(
        "arcx.cfg 有 %d 個問題, 部分產出物無法驗證" % len(problems),
        evidence={"problems": list(problems)},
    )


@qa_check(id="FLOW_DIR_MISSING", title="flow 目錄缺失",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def flow_dir_missing(case: CaseContext) -> Optional[Issue]:
    """arcx.cfg 裡有這個 block, 但 case run dir 底下對應的目錄不存在。

    代表這個 flow 根本沒被執行, 而不只是產出物寫失敗。
    """
    expected = case.expected_flow_dirs
    if not expected:
        return None
    missing = [name for name in expected if not case.is_dir(name)]
    if not missing:
        return None
    return case.fail(
        "缺少 %d 個 flow 目錄" % len(missing),
        evidence={"missing": missing, "found": case.listdir()},
    )


@qa_check(id="NETLIST_MISSING", title="netlist 缺失",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def netlist_missing(case: CaseContext) -> Optional[Issue]:
    """期望的 netlist 檔案不存在。

    這是抓「假成功」的主力 —— Arcx 寫了 .complete marker, 但檔案根本沒產出來。
    """
    missing = [a for a in case.expected_artifacts if not case.exists(a.relpath)]
    if not missing:
        return None
    return case.fail(
        "缺少 %d 個 netlist" % len(missing),
        evidence={
            "missing": [
                {"block": a.block, "flow": a.flow, "path": a.relpath}
                for a in missing
            ],
        },
    )


@qa_check(id="NETLIST_EMPTY", title="netlist 過小或為空",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def netlist_empty(case: CaseContext) -> Optional[Issue]:
    """netlist 存在, 但小到不可能是有效結果。

    檔案被建立、然後 job 立刻死掉時就會長這樣 —— 只看「存不存在」會漏掉。
    """
    offenders = []
    for artifact in case.expected_artifacts:
        size = case.size(artifact.relpath)
        if size is None:
            continue                       # 交給 NETLIST_MISSING, 不重複報
        if size < artifact.min_bytes:
            offenders.append({
                "path": artifact.relpath,
                "size": size,
                "min_bytes": artifact.min_bytes,
            })
    if not offenders:
        return None
    return case.fail(
        "%d 個 netlist 小於門檻" % len(offenders), evidence={"files": offenders})


@qa_check(id="MARKER_INCONSISTENT", title="marker 收尾不完整",
          severity=Severity.WARN, scope=CASE, stage=POST)
def marker_inconsistent(case: CaseContext) -> Optional[Issue]:
    """.complete 已經出現, 但 .run / .queue 沒被清掉。

    產出物可能是好的, 但 Arcx 的收尾沒做完 —— 值得人看一眼,
    而且會讓 rerun 的完整性判定變成「存疑」。
    """
    if not case.case.marker_inconsistent:
        return None
    return case.warn(
        "有 .complete 但 .run/.queue 未清除",
        evidence={"case_id": case.case_id},
    )


# ===========================================================================
# LIVE —— 執行中每個 tick 跑, 回答「它現在健康嗎」
# ===========================================================================

@qa_check(id="CASE_QUIET", title="log 長時間沒有成長",
          severity=Severity.WARN, scope=CASE, stage=LIVE)
def case_quiet(case: CaseContext) -> Optional[Issue]:
    """log 安靜太久。

    刻意做成**分級**而不是二元判定: 單一 case 的 runtime 從 10 分鐘到 3 天
    都有, 而且確實存在「不寫 log 但產出物已經齊了」的正常情況。
    系統負責把「安靜多久」講清楚並隨時間升級醒目程度, 判斷交給人。

        < 4h        不報
        4h ~ 8h     WARN   —— 少見, 值得看一眼
        > 8h        FATAL  —— 基本上可判定卡住

    產出物都已經齊了的話降一級 —— 那通常只是在收尾。
    """
    if case.state not in (CaseState.RUNNING, CaseState.SUSPENDED,
                          CaseState.STALLED):
        return None

    quiet = case.qa.quiet
    silent = case.silent_for
    if silent < quiet.warn_after_sec:
        return None

    severity = (Severity.FATAL if silent >= quiet.stalled_after_sec
                else Severity.WARN)

    ready = case.artifacts_ready()
    if ready and quiet.downgrade_when_artifacts_ready:
        severity = Severity.WARN if severity == Severity.FATAL else Severity.INFO

    return case.at(
        severity,
        "log 已 %.1f 小時沒有成長%s" % (
            silent / 3600.0, "（但期望產出物都已存在）" if ready else ""),
        evidence={
            "silent_sec": round(silent, 1),
            "warn_after_sec": quiet.warn_after_sec,
            "stalled_after_sec": quiet.stalled_after_sec,
            "artifacts_ready": ready,
            "log_path": case.case.log_path,
        },
    )


@qa_check(id="LSF_SUSPENDED", title="LSF job 被 suspend",
          severity=Severity.WARN, scope=CASE, stage=LIVE)
def lsf_suspended(case: CaseContext) -> Optional[Issue]:
    """LSF 回報這個 job 處於 *SUSP 狀態。

    它佔著 slot 卻沒有進度, 通常是資源搶占或被人手動停住。
    """
    lsf_state = case.case.lsf_state
    if lsf_state is None or not lsf_state.is_suspended:
        return None
    return case.warn(
        "LSF 狀態為 %s" % lsf_state.value,
        evidence={"lsf_state": lsf_state.value, "job_id": case.case.lsf_job_id},
    )


@qa_check(id="CASE_NEVER_STARTED", title="case 從未被提交",
          severity=Severity.FATAL, scope=CASE, stage=LIVE)
def case_never_started(case: CaseContext) -> Optional[Issue]:
    """有 case run dir, 但從頭到尾沒有出現任何 marker 也沒有 log。

    這是目前流程最容易靜默漏掉的失敗 —— 它不會失敗, 它只是不存在,
    所以沒有任何錯誤訊息會提到它。
    """
    if case.state != CaseState.PENDING:
        return None
    if not case.case_dir_exists:
        return None
    if case.case.log_path:
        return None
    return case.fail(
        "有 run dir 但沒有任何 marker 或 log, 可能從未被提交",
        evidence={"case_dir": case.root},
    )
