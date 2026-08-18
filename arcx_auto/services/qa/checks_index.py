"""index 層級的 QA 檢查。

這一層抓的是「單看每個 case 都正常, 但整體對不上」的問題 ——
例如某個 case 從頭到尾沒出現過, 或 report 少了幾筆。
這類失敗不會產生任何錯誤訊息, 只看 case 是抓不到的。

report 目錄的結構:

    QC_Cc/
      Report_QC_Cc                    <- 主報告
      Report_QC_Cc_Summary_SCCB3      <- 摘要, 後綴會變, 所以用 glob
    QC_Ct/  (同上)
    QC_Spice/ (同上, 但不一定存在)
"""

from __future__ import annotations

from typing import List, Optional

from arcx_auto.domain.enums import CaseState, IssueScope, IssueStage, Severity
from arcx_auto.domain.qa import Issue
from arcx_auto.services.qa.context import IndexContext
from arcx_auto.services.qa.registry import qa_check

INDEX = IssueScope.INDEX
POST = IssueStage.POST
LIVE = IssueStage.LIVE


@qa_check(id="REPORT_DIR_MISSING", title="必要的 QC report 目錄缺失",
          severity=Severity.FATAL, scope=INDEX, stage=POST)
def report_dir_missing(index: IndexContext) -> Optional[Issue]:
    """QC_Cc / QC_Ct 一定會存在。缺了代表 Arcx 的收尾階段沒跑完。"""
    missing = [d for d in index.qa.reports.required_dirs if not index.is_dir(d)]
    if not missing:
        return None
    return index.fail(
        "缺少必要的 report 目錄: %s" % ", ".join(missing),
        evidence={"missing": missing, "found": index.listdir()},
    )


@qa_check(id="REPORT_FILE_MISSING", title="QC report 檔案異常",
          severity=Severity.FATAL, scope=INDEX, stage=POST)
def report_file_missing(index: IndexContext) -> Optional[Issue]:
    """report 目錄在, 但裡面的主報告或摘要不對。

    只檢查存在的目錄 —— 目錄本身缺失由 REPORT_DIR_MISSING 負責, 不重複報。

    Summary 的後綴 (例如 _SCCB3) 只是命名, 所以用 glob 比對; 但每個 QC_*
    底下**恰好**只會有一個 Summary, 因此多於一個也算異常 (通常是前一輪殘留)。
    """
    reports = index.qa.reports
    problems: List[dict] = []

    for name in list(reports.required_dirs) + list(reports.optional_dirs):
        if not index.is_dir(name):
            continue
        main = reports.main_file_template.format(dir=name)
        if not index.exists("%s/%s" % (name, main)):
            problems.append({"dir": name, "missing": main})

        # 每個 QC_* 底下**恰好**一個 Summary。
        # 多於一個通常是前一輪殘留沒清乾淨, 那會讓下游拿到錯的報告 ——
        # 所以「太多」跟「缺少」一樣要報。
        summary_glob = reports.summary_glob_template.format(dir=name)
        found = index.glob(summary_glob, rel_dir=name)
        if not found:
            problems.append({"dir": name, "missing_glob": summary_glob})
        elif len(found) > 1:
            problems.append({
                "dir": name,
                "expected_one_summary": summary_glob,
                "found": found,
            })

    if not problems:
        return None
    return index.fail(
        "%d 個 report 檔案有問題" % len(problems), evidence={"problems": problems})


@qa_check(id="INDEX_HAS_UNKNOWN_MARKER", title="出現未知的 marker",
          severity=Severity.UNKNOWN, scope=INDEX, stage=LIVE)
def index_has_unknown_marker(index: IndexContext) -> Optional[Issue]:
    """.queue / .run / .complete 以外的 marker。

    使用者確認這不正常。我們不知道它代表什麼, 所以是 UNKNOWN 而不是
    FATAL 或忽略 —— 需要人來判斷。
    """
    obs = index.observation
    if obs is None or not obs.unknown_markers:
        return None
    return index.unknown(
        "出現 %d 個未知 marker" % len(obs.unknown_markers),
        evidence={
            "markers": [{"file": n, "case_id": c} for n, c in obs.unknown_markers],
        },
    )


@qa_check(id="LOG_UNRESOLVED", title="log 無法對應到 case",
          severity=Severity.UNKNOWN, scope=INDEX, stage=LIVE)
def log_unresolved(index: IndexContext) -> Optional[Issue]:
    """有 log 但找不到或無法解析對應的 cmd_file。

    代表有一個 case 我們**監控不到**。靜默忽略的話, 系統會在自己已經瞎掉的
    情況下回報一切正常。
    """
    obs = index.observation
    if obs is None or not obs.unresolved_logs:
        return None
    return index.unknown(
        "%d 個 log 無法對應到 case" % len(obs.unresolved_logs),
        evidence={"logs": list(obs.unresolved_logs)},
    )


@qa_check(id="INDEX_INCOMPLETE", title="仍有 case 未完成",
          severity=Severity.WARN, scope=INDEX, stage=POST)
def index_incomplete(index: IndexContext) -> Optional[Issue]:
    """整個 index 收尾了, 但還有 case 沒有走到終點。

    這些就是 rerun 時要刪掉 run dir 重跑的對象。
    """
    pending = [
        case_id for case_id, snap in index.cases.items()
        if snap.state not in (CaseState.DONE, CaseState.COMPLETED_MARKER)
    ]
    if not pending:
        return None
    return index.warn(
        "%d / %d 個 case 尚未完成" % (len(pending), len(index.cases)),
        evidence={"pending": sorted(pending)[:50], "total": len(index.cases)},
    )
