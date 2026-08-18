"""終端輸出渲染。純函數: 資料 -> 字串。"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from arcx_auto.domain.enums import CaseState, Completeness, PlanMode, Severity
from arcx_auto.domain.models import (
    DirMap,
    IndexRunObservation,
    IndexRunSnapshot,
    IndexSpec,
    WavePlan,
)
from arcx_auto.domain.qa import Issue
from arcx_auto.services.state_engine import classify_completeness
from arcx_auto.util.textfmt import (
    format_duration,
    format_size,
    format_timestamp,
    render_table,
)

# 狀態顯示順序: 需要注意的排前面, 讓人一眼看到問題
_STATE_ORDER = [
    CaseState.FAILED,
    CaseState.LOST,
    CaseState.STALLED,
    CaseState.SUSPENDED,
    CaseState.UNKNOWN,
    CaseState.RUNNING,
    CaseState.QUEUED,
    CaseState.PENDING,
    CaseState.COMPLETED_MARKER,
    CaseState.DONE,
]


def render_status(
    snapshots: Sequence[IndexRunSnapshot],
    now: float,
    detail: bool = False,
    lsf_note: Optional[str] = None,
    observations: Optional[Sequence["IndexRunObservation"]] = None,
    qa_reports: Optional[Sequence[object]] = None,
    show_issues: bool = False,
) -> str:
    """狀態總覽。"""
    lines: List[str] = []

    if lsf_note:
        lines.append("  ! LSF 資料不可用: %s" % lsf_note)
        lines.append("    -> LOST / SUSPENDED 判定已停用, 只依 marker 與 log 判斷。")
        lines.append("")

    totals: Dict[str, int] = {}
    for snapshot in snapshots:
        for state, count in snapshot.count_by_state().items():
            totals[state] = totals.get(state, 0) + count

    lines.append("== 總覽 ==")
    lines.append(_render_state_summary(totals))
    lines.append("")

    lines.append("== 各 index run folder ==")
    rows = []
    for snapshot in snapshots:
        counts = snapshot.count_by_state()
        attention = len(snapshot.cases_needing_attention())
        rows.append([
            snapshot.index_key,
            str(len(snapshot.cases)),
            str(counts.get(CaseState.COMPLETED_MARKER.value, 0)
                + counts.get(CaseState.DONE.value, 0)),
            str(counts.get(CaseState.RUNNING.value, 0)),
            str(counts.get(CaseState.QUEUED.value, 0)),
            str(attention) if attention else "-",
            snapshot.error or "",
        ])
    lines.append(render_table(
        ["index", "cases", "完成", "執行中", "排隊", "需注意", "備註"],
        rows,
        aligns=["left", "right", "right", "right", "right", "right", "left"],
    ))

    attention_rows = []
    for snapshot in snapshots:
        for case in sorted(snapshot.cases_needing_attention(),
                           key=lambda c: c.case_id):
            attention_rows.append([
                snapshot.index_key,
                case.case_id,
                case.state.value,
                format_duration(now - case.entered_state_at),
                format_duration(case.silent_for(now)),
                case.note or "",
            ])
    if attention_rows:
        lines.append("")
        lines.append("== 需要注意的 case (%d) ==" % len(attention_rows))
        lines.append(render_table(
            ["index", "case", "狀態", "停留", "log 靜止", "原因"],
            attention_rows,
        ))

    qa_block = _render_qa_issues(qa_reports or (), show_all=show_issues)
    if qa_block:
        lines.append("")
        lines.append(qa_block)

    scan_issues = _render_scan_issues(observations or ())
    if scan_issues:
        lines.append("")
        lines.append(scan_issues)

    if detail:
        for snapshot in snapshots:
            lines.append("")
            lines.append("== %s (%s) ==" % (snapshot.index_key, snapshot.run_folder))
            rows = []
            for case_id in sorted(snapshot.cases, key=_case_sort):
                case = snapshot.cases[case_id]
                completeness, _why = classify_completeness(case)
                rows.append([
                    case.case_id,
                    case.state.value,
                    case.lsf_state.value if case.lsf_state else "-",
                    case.lsf_job_id or "-",
                    format_size(case.last_progress_size),
                    format_duration(case.silent_for(now)),
                    _completeness_label(completeness),
                ])
            lines.append(render_table(
                ["case", "狀態", "LSF", "job id", "log 大小", "靜止", "rerun 判定"],
                rows,
                aligns=["left", "left", "left", "right", "right", "right", "left"],
            ))
    return "\n".join(lines)


#: 嚴重度顯示順序與標記。UNKNOWN 刻意排在 WARN 之上 ——
#: 「檢查不了」比「有小問題」更需要人來看。
_SEVERITY_ORDER = [Severity.FATAL, Severity.UNKNOWN, Severity.WARN, Severity.INFO]
_SEVERITY_MARK = {
    Severity.FATAL: "!!",
    Severity.UNKNOWN: "??",
    Severity.WARN: "! ",
    Severity.INFO: "  ",
}


def _render_qa_issues(reports: Sequence[object], show_all: bool = False) -> str:
    """QA 發現的問題。

    預設只列 FATAL / UNKNOWN (需要人處理的); --issues 才列全部。
    同 id 的 issue 會被聚合 —— 200 個 case 犯同一個錯時, 工程師需要看到的是
    「NETLIST_MISSING x 200」而不是 200 行一樣的訊息。
    """
    issues: List[Issue] = []
    for report in reports:
        issues.extend(getattr(report, "all_issues")())
    if not issues:
        return ""

    if not show_all:
        issues = [i for i in issues
                  if i.severity in (Severity.FATAL, Severity.UNKNOWN)]
        if not issues:
            return ""

    grouped: Dict[str, List[Issue]] = {}
    for issue in issues:
        grouped.setdefault(issue.id, []).append(issue)

    def group_key(item):
        first = item[1][0]
        rank = (_SEVERITY_ORDER.index(first.severity)
                if first.severity in _SEVERITY_ORDER else 99)
        return (rank, -len(item[1]), item[0])

    rows = []
    for issue_id, group in sorted(grouped.items(), key=group_key):
        first = group[0]
        targets = sorted({i.case_id or i.index_key or "-" for i in group})
        shown = ", ".join(targets[:4])
        if len(targets) > 4:
            shown += " ... (+%d)" % (len(targets) - 4)
        rows.append([
            _SEVERITY_MARK.get(first.severity, "  ") + " " + first.severity.value,
            issue_id,
            str(len(group)),
            first.title or "",
            shown,
        ])

    total = sum(len(g) for g in grouped.values())
    header = "== QA 問題 (%d) ==" % total
    if not show_all:
        header += "   (只顯示 FATAL/UNKNOWN, 加 --issues 看全部)"
    return header + "\n" + render_table(
        ["嚴重度", "issue id", "數量", "說明", "對象"],
        rows,
        aligns=["left", "left", "right", "left", "left"],
        max_col_width=44,
    )


def _render_scan_issues(observations: Sequence["IndexRunObservation"]) -> str:
    """顯示掃描時無法歸類的東西。

    這兩類是「Arcx 的檔案慣例變了」或「有 case 我們監控不到」的早期訊號,
    靜默忽略的話, 系統會在自己已經瞎掉的情況下回報一切正常。
    """
    rows = []
    for obs in observations:
        for name, case_id in obs.unknown_markers:
            rows.append([
                obs.index_key, "!! 未知 marker", name,
                "已知只有 queue/run/complete; case=%s 需人工確認" % case_id,
            ])
        for name in obs.unresolved_logs:
            rows.append([
                obs.index_key, "log 無法對應到 case", name,
                "找不到或無法解析對應的 cmd_file",
            ])
        for name in obs.unmatched_entries:
            rows.append([obs.index_key, "無法歸類的檔案", name, "不符合任何已知慣例"])
    if not rows:
        return ""
    return "== 掃描異常 (%d) ==\n" % len(rows) + render_table(
        ["index", "類型", "名稱", "說明"], rows, max_col_width=50
    )


def _render_state_summary(totals: Dict[str, int]) -> str:
    if not totals:
        return "  (沒有觀測到任何 case)"
    rows = []
    for state in _STATE_ORDER:
        count = totals.get(state.value, 0)
        if count:
            rows.append([state.value, str(count),
                         "需注意" if state.needs_attention else ""])
    for state_name, count in sorted(totals.items()):
        if state_name not in {s.value for s in _STATE_ORDER}:
            rows.append([state_name, str(count), ""])
    return render_table(["狀態", "數量", ""], rows, aligns=["left", "right", "left"])


def _completeness_label(completeness: Completeness) -> str:
    return {
        Completeness.COMPLETE: "保留",
        Completeness.INCOMPLETE: "刪除重跑",
        Completeness.UNKNOWN: "刪除重跑 (存疑)",
    }[completeness]


def _case_sort(case_id: str):
    """case id 是 cell 名稱, 用自然排序避免 NDIO_10 排在 NDIO_2 前面。"""
    from arcx_auto.adapters.fs import natural_key
    return natural_key(case_id)


# --------------------------------------------------------------------------
# 分波計畫
# --------------------------------------------------------------------------

def render_plan(plan: WavePlan, show_command: bool = False,
                commands: Optional[Dict[str, str]] = None) -> str:
    lines: List[str] = []
    lines.append("== 分波計畫 ==")
    lines.append("  模式          : %s" % plan.mode.value)
    lines.append("  單波 slot 上限: %d" % plan.max_slots_per_wave)
    lines.append("  wave 數       : %d" % len(plan.waves))
    lines.append("  index 總數    : %d" % sum(len(w.indices) for w in plan.waves))
    lines.append("  case 總數     : %d" % plan.total_cases)
    lines.append("  slot 總數     : %d" % plan.total_slots)
    lines.append("")

    rows = []
    for wave in plan.waves:
        over = " ⚠ 超量" if wave.total_slots > plan.max_slots_per_wave else ""
        rows.append([
            wave.name,
            str(len(wave.indices)),
            str(wave.total_cases),
            str(wave.total_slots) + over,
            ", ".join(wave.index_keys),
        ])
    lines.append(render_table(
        ["wave", "index 數", "case 數", "slots", "index"],
        rows,
        aligns=["left", "right", "right", "right", "left"],
    ))

    lines.append("")
    lines.append("== 各 index 明細 ==")
    detail_rows = []
    for wave in plan.waves:
        for spec in wave.indices:
            detail_rows.append([
                wave.name,
                spec.index_key,
                str(spec.gds_count),
                str(spec.cpu_per_case),
                str(spec.slots),
                ",".join(spec.keywords) or "-",
                spec.path,
            ])
    lines.append(render_table(
        ["wave", "index", "GDS", "cpu/case", "slots", "關鍵字", "path"],
        detail_rows,
        aligns=["left", "left", "right", "right", "right", "left", "left"],
        max_col_width=70,
    ))

    if plan.excluded:
        lines.append("")
        lines.append("== 被排除的 index (%d) ==" % len(plan.excluded))
        lines.append(render_table(
            ["index", "原因", "path"],
            [[s.index_key, s.error or "資料不完整", s.path] for s in plan.excluded],
            max_col_width=70,
        ))

    if plan.warnings:
        lines.append("")
        lines.append("== 警告 (%d) ==" % len(plan.warnings))
        for warning in plan.warnings:
            lines.append("  ! %s" % warning)

    if show_command and commands:
        lines.append("")
        lines.append("== 各 wave 會執行的指令 (dry-run, 未實際執行) ==")
        for wave in plan.waves:
            lines.append("  %s:" % wave.name)
            lines.append("    cwd: <run_root>/<run_id>/%s" % wave.name)
            lines.append("    cmd: %s" % commands.get(wave.name, ""))

    return "\n".join(lines)


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def render_dir_map(dir_map: DirMap, verify: bool = False) -> str:
    import os

    lines: List[str] = []
    lines.append("== dir_map ==")
    lines.append("  來源  : %s" % dir_map.source_path)
    lines.append("  index : %d 筆" % len(dir_map.entries))
    if dir_map.meta:
        lines.append("  meta  : %s" % ", ".join(
            "%s=%s" % (k, v) for k, v in sorted(dir_map.meta.items())
        ))
    lines.append("")

    rows = []
    for key in dir_map.keys_sorted():
        path = dir_map.entries[key]
        status = ""
        if verify:
            status = "ok" if os.path.isdir(os.path.expanduser(path)) else "路徑不存在"
        rows.append([key, path, status])
    headers = ["index", "path"] + (["檢查"] if verify else [""])
    lines.append(render_table(headers, rows, max_col_width=80))

    if dir_map.warnings:
        lines.append("")
        lines.append("== 警告 (%d) ==" % len(dir_map.warnings))
        for warning in dir_map.warnings:
            lines.append("  ! %s" % warning)
    return "\n".join(lines)


def render_index_specs(specs: Sequence[IndexSpec]) -> str:
    lines: List[str] = []
    lines.append("== index 資源需求 ==")
    rows = []
    for spec in specs:
        rows.append([
            spec.index_key,
            str(spec.gds_count),
            str(spec.cpu_per_case),
            str(spec.slots),
            ",".join(spec.keywords) or "-",
            spec.error or ("; ".join(spec.warnings) if spec.warnings else "ok"),
        ])
    lines.append(render_table(
        ["index", "GDS", "cpu/case", "slots", "關鍵字", "狀態"],
        rows,
        aligns=["left", "right", "right", "right", "left", "left"],
        max_col_width=70,
    ))
    return "\n".join(lines)
