"""各頁面的內容產生。純函數: state.json 的內容 -> HTML。

刻意不 import 任何 service —— UI 只渲染 daemon 已經算好的資料。
這讓 UI 可以隨時關掉重開、崩潰、被換掉, daemon 照跑不受影響
(architecture 決策 1)。
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

from arcx_auto.web.html import (
    cards,
    duration,
    esc,
    page,
    progress_bar,
    severity_pill,
    size,
    state_pill,
    table,
    timestamp,
)

_SEVERITY_RANK = {"FATAL": 0, "UNKNOWN": 1, "WARN": 2, "INFO": 3}


def _q(*parts: str) -> str:
    return "/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)


# ---------------------------------------------------------------------------
# 首頁: 所有 run 的總覽
# ---------------------------------------------------------------------------

def render_home(states: Sequence[Dict[str, Any]], refresh: int) -> str:
    rows = []
    total_attention = 0
    for state in states:
        totals = state.get("totals") or {}
        attention = totals.get("attention", 0)
        total_attention += attention
        run_id = state.get("run_id", "?")
        daemon = state.get("daemon") or {}
        rows.append([
            "<a href='%s'>%s</a>" % (esc(_q("run", run_id)), esc(run_id)),
            progress_bar(totals.get("states") or {}),
            esc(totals.get("cases", 0)),
            esc(totals.get("indexes", 0)),
            ("<span class='bad'>%d</span>" % attention) if attention else "-",
            esc(totals.get("issues", 0)),
            esc(timestamp(state.get("updated_at"))),
            _daemon_health(daemon, state.get("updated_at")),
        ])

    body = cards([
        ("需要你決定", total_attention, "alert" if total_attention else ""),
        ("監控中的 run", len(states), ""),
    ])
    body += "<h2>run</h2>"
    body += table(
        ["run", "進度", "cases", "index", "需注意", "issues", "更新於", "daemon"],
        rows, numeric=[2, 3, 4, 5],
        empty="還沒有任何監控中的 run。用 arcx-auto daemon 啟動一個。",
    )
    return page("Arcx Auto Golden", body, refresh=refresh)


def _daemon_health(daemon: Dict[str, Any], updated_at: Optional[float]) -> str:
    """daemon 自己也要被監控 —— 它靜默死掉的話, 畫面會停在最後一刻
    看起來一切正常, 那是最危險的情況。
    """
    if daemon.get("last_error"):
        return "<span class='bad'>錯誤</span>"
    import time

    if updated_at and time.time() - updated_at > 900:
        return "<span class='warn'>逾 15 分鐘未更新</span>"
    if not daemon:
        return "<span class='muted'>-</span>"
    return "<span class='good'>pid %s</span>" % esc(daemon.get("pid"))


# ---------------------------------------------------------------------------
# run 詳情
# ---------------------------------------------------------------------------

def render_run(state: Dict[str, Any], refresh: int) -> str:
    run_id = state.get("run_id", "?")
    totals = state.get("totals") or {}
    severities = totals.get("severities") or {}

    body = cards([
        ("需要你決定", totals.get("attention", 0),
         "alert" if totals.get("attention") else ""),
        ("cases", totals.get("cases", 0), ""),
        ("FATAL", severities.get("FATAL", 0), "alert" if severities.get("FATAL") else ""),
        ("UNKNOWN", severities.get("UNKNOWN", 0), ""),
        ("WARN", severities.get("WARN", 0), ""),
    ])

    body += _lsf_banner(state)
    body += _daemon_banner(state)

    body += "<h2>issue 摘要</h2>" + _issue_summary(state, run_id)

    rows = []
    for index in state.get("indexes") or []:
        counts = index.get("counts") or {}
        attention = index.get("attention", 0)
        rows.append([
            "<a href='%s'>%s</a>" % (
                esc(_q("run", run_id, "index", index.get("index_key", "?"))),
                esc(index.get("index_key"))),
            progress_bar(counts),
            esc(sum(counts.values())),
            esc(counts.get("DONE", 0)),
            ("<span class='bad'>%d</span>" % attention) if attention else "-",
            _anomaly_cell(index),
            "<span class='muted'>%s</span>" % esc(index.get("run_folder")),
        ])
    body += "<h2>index</h2>" + table(
        ["index", "進度", "cases", "done", "需注意", "掃描異常", "run folder"],
        rows, numeric=[2, 3, 4])

    return page("run %s" % run_id, body, refresh=refresh,
                crumbs=[("/", "全部 run"), (_q("run", run_id), run_id)],
                meta="更新於 %s" % timestamp(state.get("updated_at")))


def _lsf_banner(state: Dict[str, Any]) -> str:
    lsf = state.get("lsf") or {}
    if lsf.get("available"):
        return ""
    return (
        "<div class='card' style='border-color:var(--warn);margin-bottom:8px'>"
        "<span class='warn'>LSF 資料不可用：%s</span>"
        "<div class='doc'>LOST / SUSPENDED 判定已停用，只依 marker 與 log 判斷。"
        "</div></div>" % esc(lsf.get("note") or "未知原因")
    )


def _daemon_banner(state: Dict[str, Any]) -> str:
    daemon = state.get("daemon") or {}
    if not daemon.get("last_error"):
        return ""
    return (
        "<div class='card' style='border-color:var(--bad);margin-bottom:8px'>"
        "<span class='bad'>上一次掃描失敗</span><pre>%s</pre></div>"
        % esc(daemon["last_error"])
    )


def _issue_summary(state: Dict[str, Any], run_id: str) -> str:
    """同 id 聚合。200 個 case 犯同一個錯時, 工程師要看的是
    「NETLIST_MISSING x 200」而不是 200 行一樣的訊息。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for issue in state.get("issues") or []:
        grouped.setdefault(issue["id"], []).append(issue)
    if not grouped:
        return "<div class='empty'>沒有發現任何問題</div>"

    rows = []
    for issue_id, group in sorted(
        grouped.items(),
        key=lambda kv: (_SEVERITY_RANK.get(kv[1][0]["severity"], 9),
                        -len(kv[1]), kv[0]),
    ):
        first = group[0]
        targets = sorted({(i.get("case_id") or i.get("index_key") or "-")
                          for i in group})
        shown = ", ".join(esc(t) for t in targets[:6])
        if len(targets) > 6:
            shown += " <span class='muted'>… (+%d)</span>" % (len(targets) - 6)
        rows.append([
            severity_pill(first["severity"]),
            esc(issue_id),
            esc(len(group)),
            esc(first.get("title") or ""),
            shown,
        ])
    return table(["嚴重度", "issue id", "數量", "說明", "對象"],
                 rows, numeric=[2])


def _anomaly_cell(index: Dict[str, Any]) -> str:
    anomalies = index.get("anomalies") or {}
    parts = []
    if anomalies.get("unknown_markers"):
        parts.append("<span class='bad'>未知 marker %d</span>"
                     % len(anomalies["unknown_markers"]))
    if anomalies.get("unresolved_logs"):
        parts.append("<span class='warn'>孤兒 log %d</span>"
                     % len(anomalies["unresolved_logs"]))
    if anomalies.get("unmatched_entries"):
        parts.append("<span class='muted'>未歸類 %d</span>"
                     % len(anomalies["unmatched_entries"]))
    return " ".join(parts) or "-"


# ---------------------------------------------------------------------------
# index 詳情
# ---------------------------------------------------------------------------

def render_index(state: Dict[str, Any], index: Dict[str, Any],
                 refresh: int) -> str:
    run_id = state.get("run_id", "?")
    index_key = index.get("index_key", "?")
    counts = index.get("counts") or {}

    body = cards([(state_name, count,
                   "alert" if state_name in ("FAILED", "LOST", "STALLED") else "")
                  for state_name, count in sorted(counts.items())])

    rows = []
    for case in index.get("cases") or []:
        issue_ids = sorted({i["id"] for i in case.get("issues") or []})
        rows.append([
            "<a href='%s'>%s</a>" % (
                esc(_q("run", run_id, "index", index_key,
                       "case", case["case_id"])),
                esc(case["case_id"])),
            state_pill(case["state"]),
            esc(case.get("lsf_state") or "-"),
            esc(duration(case.get("in_state_sec"))),
            _silent_cell(case),
            esc(size(case.get("log_size"))),
            ", ".join(esc(i) for i in issue_ids) or "-",
            "<span class='muted'>%s</span>" % esc(case.get("note") or ""),
        ])

    body += "<h2>cases</h2>" + table(
        ["case", "狀態", "LSF", "停留", "log 靜止", "log 大小", "issues", "說明"],
        rows, numeric=[3, 4, 5])

    body += _anomaly_section(index)

    return page("%s / %s" % (run_id, index_key), body, refresh=refresh,
                crumbs=[("/", "全部 run"), (_q("run", run_id), run_id),
                        (_q("run", run_id, "index", index_key), index_key)])


def _silent_cell(case: Dict[str, Any]) -> str:
    """安靜時間依長短升級醒目程度 —— 系統把時間講清楚, 判斷交給人。"""
    silent = case.get("silent_sec") or 0
    text = esc(duration(silent))
    if silent >= 28800:
        return "<span class='bad'>%s</span>" % text
    if silent >= 14400:
        return "<span class='warn'>%s</span>" % text
    return text


def _anomaly_section(index: Dict[str, Any]) -> str:
    anomalies = index.get("anomalies") or {}
    rows = []
    for item in anomalies.get("unknown_markers") or []:
        rows.append(["<span class='bad'>未知 marker</span>",
                     esc(item.get("file")),
                     esc("已知只有 queue/run/complete；case=%s"
                         % item.get("case_id"))])
    for name in anomalies.get("unresolved_logs") or []:
        rows.append(["<span class='warn'>log 無法對應</span>", esc(name),
                     "找不到或無法解析對應的 cmd_file"])
    for name in anomalies.get("unmatched_entries") or []:
        rows.append(["<span class='muted'>未歸類</span>", esc(name),
                     "不符合任何已知慣例"])
    if not rows:
        return ""
    return "<h2>掃描異常</h2>" + table(["類型", "名稱", "說明"], rows)


# ---------------------------------------------------------------------------
# case 詳情
# ---------------------------------------------------------------------------

def render_case(state: Dict[str, Any], index: Dict[str, Any],
                case: Dict[str, Any], refresh: int) -> str:
    run_id = state.get("run_id", "?")
    index_key = index.get("index_key", "?")
    case_id = case["case_id"]

    body = cards([
        ("狀態", case["state"],
         "alert" if case["state"] in ("FAILED", "LOST", "STALLED") else ""),
        ("停留", duration(case.get("in_state_sec")), ""),
        ("log 靜止", duration(case.get("silent_sec")), ""),
        ("log 大小", size(case.get("log_size")), ""),
    ])

    facts = [
        ("結構性狀態", case.get("base_state") or "-"),
        ("判定說明", case.get("note") or "-"),
        ("LSF", "%s (job %s)" % (case.get("lsf_state") or "-",
                                 case.get("lsf_job_id") or "-")),
        ("case run dir", case.get("case_dir") or "-"),
        ("cmd_file 執行路徑", case.get("exec_path") or "-"),
        ("log", case.get("log_path") or "-"),
        ("marker 一致", "否" if case.get("marker_inconsistent") else "是"),
    ]
    body += "<h2>基本資訊</h2>" + table(
        ["項目", "值"],
        [[esc(k), "<span class='muted'>%s</span>" % esc(v)] for k, v in facts])

    body += "<h2>QA 問題</h2>" + _case_issues(case)

    return page("%s / %s" % (index_key, case_id), body, refresh=refresh,
                crumbs=[("/", "全部 run"), (_q("run", run_id), run_id),
                        (_q("run", run_id, "index", index_key), index_key),
                        (_q("run", run_id, "index", index_key,
                            "case", case_id), case_id)])


def _case_issues(case: Dict[str, Any]) -> str:
    issues = sorted(case.get("issues") or [],
                    key=lambda i: _SEVERITY_RANK.get(i["severity"], 9))
    if not issues:
        return "<div class='empty'>沒有發現任何問題</div>"

    blocks = []
    for issue in issues:
        evidence = ""
        if issue.get("evidence"):
            import json

            evidence = "<pre>%s</pre>" % esc(
                json.dumps(issue["evidence"], ensure_ascii=False, indent=2))
        blocks.append(
            "<div class='card' style='margin-bottom:10px'>"
            "<div>%s <strong>%s</strong> — %s</div>"
            "<div class='doc'>%s</div>%s</div>"
            % (severity_pill(issue["severity"]), esc(issue["id"]),
               esc(issue.get("message") or ""),
               esc(issue.get("doc") or ""), evidence)
        )
    return "".join(blocks)
