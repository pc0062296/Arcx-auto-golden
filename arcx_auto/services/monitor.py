"""一次完整的監控掃描 —— CLI 與 daemon 共用的核心流程。

    collect  →  transition  →  QA  →  resolve  →  (呼叫端負責持久化)
    觀測         結構性狀態     判斷    最終狀態

原本這段邏輯寫在 CLI 裡, 但 L4 介面層不該有業務邏輯 —— daemon 需要
一模一樣的流程, 抄一份就一定會走歪。抽到 L2 之後兩邊共用同一條路徑,
也讓整個流程可以在沒有 CLI 的情況下測試。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from arcx_auto.adapters.arcx_cfg import ArcxConfig
from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.config.settings import Settings
from arcx_auto.domain.models import (
    IndexRunObservation,
    IndexRunSnapshot,
    LsfJobView,
    StateEvent,
)
from arcx_auto.domain.qa import Issue
from arcx_auto.services.collector import Collector
from arcx_auto.services.qa import QaRunner
from arcx_auto.services.qa.runner import IndexQaReport
from arcx_auto.services.state_engine import TransitionContext, transition_index_run
from arcx_auto.services.state_resolver import resolve_case_state


@dataclass(frozen=True)
class ScanResult:
    """一次掃描的完整產物。"""

    scanned_at: float
    snapshots: Tuple[IndexRunSnapshot, ...] = ()
    observations: Tuple[IndexRunObservation, ...] = ()
    qa_reports: Tuple[IndexQaReport, ...] = ()
    events: Tuple[StateEvent, ...] = ()
    lsf_note: Optional[str] = None
    lsf_job_count: int = 0

    @property
    def lsf_available(self) -> bool:
        return self.lsf_note is None

    def all_issues(self) -> Tuple[Issue, ...]:
        issues: List[Issue] = []
        for report in self.qa_reports:
            issues.extend(report.all_issues())
        return tuple(issues)

    def snapshots_by_folder(self) -> Dict[str, IndexRunSnapshot]:
        return {s.run_folder: s for s in self.snapshots}


class MonitorService:
    """把 Collector / StateEngine / QA / StateResolver 串起來。

    自己持有「上一次的 snapshot」, 因為 stall 偵測需要跨次比較。
    daemon 長期持有一個實例; CLI 每次呼叫則從 store 載入前一次的結果。
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        collector: Optional[Collector] = None,
        qa_runner: Optional[QaRunner] = None,
        enable_qa: bool = True,
    ) -> None:
        self.settings = settings or Settings()
        self.collector = collector or Collector(self.settings)
        self.qa_runner = qa_runner or (QaRunner(self.settings) if enable_qa else None)
        self.previous: Dict[str, IndexRunSnapshot] = {}

    def prime(self, snapshots: Dict[str, IndexRunSnapshot]) -> None:
        """載入上一次的判定結果 (例如 daemon 重啟後從 store 讀回來)。"""
        self.previous = dict(snapshots)

    def scan(
        self,
        wave_dirs: Sequence[str] = (),
        run_folders: Sequence[str] = (),
        arcx_config: Optional[ArcxConfig] = None,
        use_lsf: bool = True,
        now: Optional[float] = None,
    ) -> ScanResult:
        now = now if now is not None else time.time()

        lsf_jobs: List[LsfJobView] = []
        lsf_note: Optional[str] = None
        if not use_lsf:
            lsf_note = "已停用 LSF 查詢"
        else:
            lsf_jobs, error = self.collector.fetch_lsf_jobs()
            if error:
                lsf_note = error

        observations: List[IndexRunObservation] = []
        for wave_dir in wave_dirs:
            observations.extend(
                self.collector.collect_wave(wave_dir, lsf_jobs=lsf_jobs, now=now))
        for folder in run_folders:
            observations.append(
                self.collector.collect_index_run(folder, lsf_jobs=lsf_jobs, now=now))

        ctx = TransitionContext.from_settings(
            self.settings.monitor,
            lsf_data_available=(lsf_note is None),
            now=now,
        )

        snapshots: List[IndexRunSnapshot] = []
        reports: List[IndexQaReport] = []
        events: List[StateEvent] = []
        updated: Dict[str, IndexRunSnapshot] = dict(self.previous)

        for observation in observations:
            key = observation.run_folder
            snapshot, new_events = transition_index_run(
                self.previous.get(key), observation, ctx)
            events.extend(new_events)

            if self.qa_runner is not None:
                report = self.qa_runner.run_index(
                    snapshot, observation, arcx_config, now=now)
                reports.append(report)
                snapshot = _apply_qa(snapshot, report)

            snapshots.append(snapshot)
            updated[key] = snapshot

        self.previous = updated

        return ScanResult(
            scanned_at=now,
            snapshots=tuple(snapshots),
            observations=tuple(observations),
            qa_reports=tuple(reports),
            events=tuple(events),
            lsf_note=lsf_note,
            lsf_job_count=len(lsf_jobs),
        )


def _apply_qa(snapshot: IndexRunSnapshot, report: IndexQaReport) -> IndexRunSnapshot:
    """把 QA 結果收斂進 case 狀態 (COMPLETED_MARKER -> DONE / FAILED)。"""
    cases = {}
    changed = False
    for case_id, case in snapshot.cases.items():
        base = case.base_state or case.state
        final, reason = resolve_case_state(base, report.issues_for(case_id))
        if final != case.state or (reason and reason != case.note):
            cases[case_id] = replace(case, state=final, note=reason or case.note)
            changed = True
        else:
            cases[case_id] = case
    return replace(snapshot, cases=cases) if changed else snapshot
