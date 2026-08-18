"""狀態機 —— **完全純函數**, 零 I/O。

輸入: 前一次的判定 (CaseSnapshot) + 這次的觀測 (CaseObservation) + 門檻
輸出: 新的判定 + 狀態轉移事件

把「解釋」與「觀測」分開的價值:
  * 可以用假資料在毫秒內窮舉所有情境, 不需要真的跑 job
  * 判定邏輯要調整時, 完全不會碰到 I/O 程式碼
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from arcx_auto.domain.enums import CaseState, Completeness, LsfState
from arcx_auto.domain.models import (
    CaseObservation,
    CaseSnapshot,
    IndexRunObservation,
    IndexRunSnapshot,
    StateEvent,
)


@dataclass(frozen=True)
class TransitionContext:
    """判定門檻。

    ``lsf_data_available`` 是刻意存在的安全開關: LSF 查不到時
    (開發機、bjobs 暫時抽風), 絕不能因為「找不到 job」就把 case 判成 LOST。
    寧可停在 RUNNING 讓人看到, 也不要誤報。
    """

    now: float
    stall_threshold_sec: float = 3600.0
    lost_grace_sec: float = 300.0
    lsf_data_available: bool = False

    @classmethod
    def from_settings(
        cls,
        monitor: "object",
        lsf_data_available: bool,
        now: Optional[float] = None,
    ) -> "TransitionContext":
        from arcx_auto.config.settings import MonitorSettings

        assert isinstance(monitor, MonitorSettings)
        return cls(
            now=now if now is not None else time.time(),
            stall_threshold_sec=monitor.stall_threshold_sec,
            lost_grace_sec=monitor.lost_grace_sec,
            lsf_data_available=lsf_data_available,
        )


# --------------------------------------------------------------------------
# 單一 case
# --------------------------------------------------------------------------

def transition_case(
    prev: Optional[CaseSnapshot],
    obs: CaseObservation,
    ctx: TransitionContext,
) -> Tuple[CaseSnapshot, List[StateEvent]]:
    """推導單一 case 的新狀態。純函數, 相同輸入永遠得到相同輸出。"""
    now = ctx.now

    # --- 1. 進度追蹤 ------------------------------------------------------
    # 用 log 的 size 而非 mtime 判斷「有沒有進度」: NFS 的 mtime 不可靠,
    # 而且有些 tool 會 touch 檔案卻沒有實質輸出。只有 size 真的變大才算有進度。
    #
    # 但**第一次觀測**沒有歷史可比, 這時改用 mtime 當作起算點而不是 now。
    # 這件事比看起來重要: daemon 重啟 / CLI 重跑時, 若一律從 now 起算,
    # 一個真的卡住三天的 case 會看起來很健康, 而且每次重啟都再健康一次。
    # 用 mtime 當種子, 重啟後立刻就能還原正確的靜止時間 (架構決策 2:
    # 系統必須能從檔案系統重建全部狀態)。
    size = obs.log_size or 0
    if prev is None:
        last_progress_at = obs.log_mtime if obs.log_mtime else now
        # 觀測到未來時間 (時鐘不同步) 時退回 now, 避免出現負的靜止時間
        if last_progress_at > now:
            last_progress_at = now
        last_progress_size = size
    elif size > prev.last_progress_size:
        last_progress_at = now
        last_progress_size = size
    else:
        last_progress_at = prev.last_progress_at
        last_progress_size = prev.last_progress_size

    # --- 2. LSF 缺席追蹤 --------------------------------------------------
    lsf_state = obs.lsf.state if obs.lsf else None
    lsf_job_id = obs.lsf.job_id if obs.lsf else (prev.lsf_job_id if prev else None)

    expects_job = obs.has_run_marker or obs.has_queue_marker
    job_absent = ctx.lsf_data_available and expects_job and (
        obs.lsf is None or not lsf_state.is_active  # type: ignore[union-attr]
    )
    if job_absent:
        lsf_missing_since = (
            prev.lsf_missing_since if prev and prev.lsf_missing_since else now
        )
    else:
        lsf_missing_since = None

    # --- 3. 狀態判定 ------------------------------------------------------
    state, reason = _decide_state(
        obs=obs,
        ctx=ctx,
        lsf_state=lsf_state,
        lsf_missing_since=lsf_missing_since,
        last_progress_at=last_progress_at,
    )

    entered_state_at = (
        prev.entered_state_at if prev and prev.state == state else now
    )

    snapshot = CaseSnapshot(
        case_id=obs.case_id,
        state=state,
        entered_state_at=entered_state_at,
        last_progress_at=last_progress_at,
        last_progress_size=last_progress_size,
        last_seen_at=now,
        lsf_job_id=lsf_job_id,
        lsf_state=lsf_state,
        lsf_missing_since=lsf_missing_since,
        case_dir=obs.case_dir or (prev.case_dir if prev else None),
        log_path=obs.log_path or (prev.log_path if prev else None),
        marker_inconsistent=obs.marker_inconsistent,
        note=reason,
    )

    events: List[StateEvent] = []
    if prev is None or prev.state != state:
        events.append(
            StateEvent(
                ts=now,
                index_key="",  # 由 transition_index_run 補上
                case_id=obs.case_id,
                from_state=prev.state if prev else None,
                to_state=state,
                reason=reason,
                evidence={
                    "markers": sorted(m.value for m in obs.markers),
                    "log_size": obs.log_size,
                    "lsf_state": lsf_state.value if lsf_state else None,
                    "silent_sec": round(now - last_progress_at, 1),
                },
            )
        )
    return snapshot, events


def _decide_state(
    obs: CaseObservation,
    ctx: TransitionContext,
    lsf_state: Optional[LsfState],
    lsf_missing_since: Optional[float],
    last_progress_at: float,
) -> Tuple[CaseState, str]:
    """狀態判定的優先順序。

    marker 的優先序是 complete > run > queue —— 即使 .run 沒被清掉,
    只要 .complete 出現就視為 Arcx 認定跑完 (不一致另外由 QA 記錄)。
    """
    now = ctx.now

    # 3.1 完成 marker 優先。注意: 這只代表 Arcx 認為跑完了,
    #     不代表結果正確 —— DONE 要等 QA 驗證通過 (Phase 1)。
    if obs.has_complete_marker:
        if obs.marker_inconsistent:
            return (CaseState.COMPLETED_MARKER, "有 .complete, 但 .run/.queue 未清除")
        return (CaseState.COMPLETED_MARKER, "有 .complete marker, 待 QA 驗證")

    # 3.2 LSF 明確回報 suspended
    if lsf_state is not None and lsf_state.is_suspended:
        return (CaseState.SUSPENDED, "LSF 回報 %s" % lsf_state.value)

    # 3.3 job 應該在但 LSF 找不到 -> 過了 grace period 才敢判 LOST
    if lsf_missing_since is not None:
        missing_for = now - lsf_missing_since
        if missing_for >= ctx.lost_grace_sec:
            return (
                CaseState.LOST,
                "marker 停在執行中, 但 LSF job 已消失 %.0fs" % missing_for,
            )
        # 還在 grace 期內: 不改判, 沿用 marker 的解讀

    # 3.4 執行中
    if obs.has_run_marker:
        silent = now - last_progress_at
        if silent >= ctx.stall_threshold_sec:
            return (
                CaseState.STALLED,
                "有 .run marker, 但 log 已 %.0f 分鐘沒有成長" % (silent / 60.0),
            )
        return (CaseState.RUNNING, "有 .run marker")

    # 3.5 排隊中
    if obs.has_queue_marker:
        return (CaseState.QUEUED, "有 .queue marker")

    # 3.6 沒有任何 marker
    if obs.case_dir_exists:
        return (
            CaseState.PENDING,
            "有 case run dir 但沒有任何 marker (尚未提交, 或 marker 遺失)",
        )
    if obs.log_path:
        return (CaseState.UNKNOWN, "只有 log 沒有 marker 也沒有 run dir")
    return (CaseState.PENDING, "尚未出現任何 marker")


# --------------------------------------------------------------------------
# 整個 index run folder
# --------------------------------------------------------------------------

def transition_index_run(
    prev: Optional[IndexRunSnapshot],
    obs: IndexRunObservation,
    ctx: TransitionContext,
) -> Tuple[IndexRunSnapshot, List[StateEvent]]:
    """對一個 index run folder 的所有 case 做狀態轉移。"""
    prev_cases: Dict[str, CaseSnapshot] = dict(prev.cases) if prev else {}
    new_cases: Dict[str, CaseSnapshot] = {}
    events: List[StateEvent] = []

    for case_id, case_obs in obs.cases.items():
        snapshot, case_events = transition_case(
            prev_cases.get(case_id), case_obs, ctx
        )
        new_cases[case_id] = snapshot
        for event in case_events:
            events.append(replace(event, index_key=obs.index_key))

    # 曾經看過但這次消失的 case: 保留上次的判定, 不要讓它從畫面上憑空不見。
    # 目錄被刪或掃描失敗都可能造成這種情況, 值得留著讓人發現。
    for case_id, old in prev_cases.items():
        if case_id not in new_cases:
            new_cases[case_id] = replace(
                old, note="本次掃描未觀測到此 case (可能已被刪除)"
            )

    snapshot = IndexRunSnapshot(
        index_key=obs.index_key,
        run_folder=obs.run_folder,
        updated_at=ctx.now,
        cases=new_cases,
        error=obs.error,
    )
    return snapshot, events


# --------------------------------------------------------------------------
# rerun 的刪除清單判定
# --------------------------------------------------------------------------

def classify_completeness(snapshot: CaseSnapshot) -> Tuple[Completeness, str]:
    """判定 rerun 時「這個 case 的 run dir 要不要刪掉重跑」。

    刻意的例外 (architecture §6.1): 在這個決策上「不確定 -> 傾向刪掉重跑」,
    與系統其他地方的「不確定就停手」相反。理由是後果不對稱 ——
      誤刪已完成  -> 浪費一次運算, 結果仍正確            (可回收)
      漏刪未完成  -> 殘缺結果被當成功交付                 (不可回收)

    UNKNOWN 預設會進刪除清單, 但在 UI 以不同顏色標示、可取消勾選,
    且刪除前一律先備份到 .arcx_auto/attempts/N/。
    """
    state = snapshot.state

    if state == CaseState.DONE:
        return (Completeness.COMPLETE, "QA 已驗證通過")
    if state == CaseState.COMPLETED_MARKER:
        if snapshot.marker_inconsistent:
            return (
                Completeness.UNKNOWN,
                "有 .complete 但 .run/.queue 未清除, 收尾狀態存疑",
            )
        return (Completeness.COMPLETE, "有 .complete marker")
    if state in (CaseState.PENDING, CaseState.QUEUED, CaseState.RUNNING,
                 CaseState.SUSPENDED, CaseState.STALLED, CaseState.LOST,
                 CaseState.FAILED):
        return (Completeness.INCOMPLETE, "狀態為 %s, 未完成" % state.value)
    return (Completeness.UNKNOWN, "狀態為 %s, 無法判定" % state.value)
