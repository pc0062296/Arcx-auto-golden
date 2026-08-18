"""SubmissionController —— 逐波提交的閘門。

閘門條件 (architecture §5.4):

    放行 = 已過 min_interval  AND  ( NJOBS < quota_threshold  OR  已過 max_wait )

為什麼不是單純的「等固定時間 **或** quota 降下來」: 純 OR 有個漏洞 ——
時間到了但 quota 還是滿的, 照送一樣會塞爆 queue。上面的組合同時涵蓋
三件事: 不會太密集、不會塞爆、也不會因為 quota 永遠不降而無限期卡住。

`max_wait` 觸發強制放行時必須在 UI 與 audit 留下明確記錄 —— 那代表
「等不到 quota 硬送的」, 之後排隊很久不是系統壞掉。

閘門判定是**純函數**, 狀態是純資料 —— 這樣「等了多久、為什麼放行」
可以在毫秒內窮舉測試, 而不用真的等兩小時。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from arcx_auto.config.settings import GateSettings
from arcx_auto.domain.enums import WaveState


@dataclass(frozen=True)
class GateState:
    """閘門的持久化狀態。存進 state.json, daemon 重啟後接續, 不重新計時。"""

    wave_name: str
    entered_at: float
    last_submit_at: Optional[float] = None
    last_njobs: Optional[int] = None
    checked_at: Optional[float] = None

    def waited_for(self, now: float) -> float:
        return max(0.0, now - self.entered_at)


@dataclass(frozen=True)
class GateDecision:
    """放行與否, 以及理由。理由會直接顯示給人看。"""

    allow: bool
    reason: str
    forced: bool = False              # 因逾時而強制放行
    wait_hint_sec: float = 0.0        # 建議多久後再問一次

    @property
    def label(self) -> str:
        return ("強制放行" if self.forced else "放行") if self.allow else "等待中"


def evaluate_gate(
    state: GateState,
    now: float,
    njobs: Optional[int],
    settings: GateSettings,
    previous_submit_at: Optional[float] = None,
) -> GateDecision:
    """純函數: 現在可以送下一波嗎。

    ``njobs`` 為 None 代表查不到 quota (busers 不可用)。這時**不能**當作
    「quota 很低」放行 —— 那會在 LSF 有問題時反而狂送。改為只靠
    min_interval 與 max_wait 決定, 並在理由中說明資料不可用。
    """
    waited = state.waited_for(now)

    # 1. 硬性最小間隔 (防抖) —— 從上一次實際提交起算
    since_submit = None
    if previous_submit_at is not None:
        since_submit = now - previous_submit_at
        if since_submit < settings.min_interval_sec:
            remaining = settings.min_interval_sec - since_submit
            return GateDecision(
                allow=False,
                reason="距離上次提交只有 %.0f 分鐘, 最小間隔為 %.0f 分鐘"
                       % (since_submit / 60.0, settings.min_interval_sec / 60.0),
                wait_hint_sec=remaining,
            )

    # 2. quota 夠低就放行
    if njobs is not None and njobs < settings.quota_threshold:
        return GateDecision(
            allow=True,
            reason="NJOBS = %d, 低於門檻 %d" % (njobs, settings.quota_threshold),
        )

    # 3. 等太久了就強制放行, 避免 quota 永遠不降而卡死
    if waited >= settings.max_wait_sec:
        return GateDecision(
            allow=True,
            forced=True,
            reason="已等待 %.1f 小時, 超過上限 %.1f 小時, 強制放行"
                   % (waited / 3600.0, settings.max_wait_sec / 3600.0),
        )

    if njobs is None:
        return GateDecision(
            allow=False,
            reason="查不到 NJOBS, 只能等到 %.1f 小時的上限"
                   % (settings.max_wait_sec / 3600.0),
            wait_hint_sec=min(300.0, settings.max_wait_sec - waited),
        )

    return GateDecision(
        allow=False,
        reason="NJOBS = %d, 尚未低於門檻 %d (已等 %.0f 分鐘)"
               % (njobs, settings.quota_threshold, waited / 60.0),
        wait_hint_sec=min(300.0, settings.max_wait_sec - waited),
    )


@dataclass
class WaveProgress:
    """一個 wave 在提交流程中的位置。"""

    wave_name: str
    state: WaveState = WaveState.PLANNED
    gate: Optional[GateState] = None
    job_id: Optional[str] = None
    submitted_at: Optional[float] = None
    last_decision: Optional[GateDecision] = None
    error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "wave": self.wave_name,
            "state": self.state.value,
            "job_id": self.job_id,
            "submitted_at": self.submitted_at,
            "gate_entered_at": self.gate.entered_at if self.gate else None,
            "waited_sec": (
                None if self.gate is None
                else max(0.0, time.time() - self.gate.entered_at)),
            "decision": (
                None if self.last_decision is None
                else {"allow": self.last_decision.allow,
                      "forced": self.last_decision.forced,
                      "reason": self.last_decision.reason}),
            "error": self.error,
        }


class SubmissionController:
    """持有整批 wave 的提交進度, 每個 tick 決定要不要送下一波。

    刻意做成「一次只推進一波」: 同時放行多波會讓閘門失去意義,
    而且 LSF 的負載尖峰正是我們要避免的東西。
    """

    def __init__(
        self,
        wave_names: Sequence[str],
        settings: Optional[GateSettings] = None,
    ) -> None:
        self.settings = settings or GateSettings()
        self.progress: List[WaveProgress] = [
            WaveProgress(wave_name=name) for name in wave_names]
        self.last_submit_at: Optional[float] = None

    # ------------------------------------------------------------------

    def pending(self) -> List[WaveProgress]:
        return [p for p in self.progress
                if p.state in (WaveState.PLANNED, WaveState.WAITING_GATE)]

    def next_wave(self) -> Optional[WaveProgress]:
        """下一個要送的 wave。順序即計畫順序, 不重排。"""
        pending = self.pending()
        return pending[0] if pending else None

    def evaluate(self, now: Optional[float] = None,
                 njobs: Optional[int] = None) -> Optional[GateDecision]:
        """問閘門: 現在可以送下一波嗎。回傳 None 代表沒有待送的 wave。"""
        now = now if now is not None else time.time()
        wave = self.next_wave()
        if wave is None:
            return None

        if wave.gate is None:
            wave.gate = GateState(wave_name=wave.wave_name, entered_at=now)
            wave.state = WaveState.WAITING_GATE

        decision = evaluate_gate(
            wave.gate, now, njobs, self.settings,
            previous_submit_at=self.last_submit_at,
        )
        wave.gate = replace(wave.gate, last_njobs=njobs, checked_at=now)
        wave.last_decision = decision
        return decision

    def mark_submitted(self, wave_name: str, job_id: Optional[str],
                       now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self.last_submit_at = now
        for wave in self.progress:
            if wave.wave_name == wave_name:
                wave.state = WaveState.SUBMITTED
                wave.job_id = job_id
                wave.submitted_at = now
                return

    def mark_failed(self, wave_name: str, error: str) -> None:
        for wave in self.progress:
            if wave.wave_name == wave_name:
                wave.state = WaveState.ABORTED
                wave.error = error
                return

    def to_json(self) -> List[Dict[str, Any]]:
        return [p.to_json() for p in self.progress]
