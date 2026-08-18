"""One complete monitoring scan -- the core flow shared by the CLI and daemon.

    collect  ->  transition  ->  QA  ->  resolve  ->  (caller persists)
    observe      structural       judge   final state

This logic started out in the CLI, but the L4 interface layer should hold no
business logic and the daemon needs exactly the same flow -- a second copy
would inevitably drift. Living in L2 means both share one path, and the flow
can be tested without a CLI at all.
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
    """Everything one scan produced."""

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
    """Wires Collector, StateEngine, QA and StateResolver together.

    Holds the previous snapshots itself, because stall detection compares
    across scans. The daemon keeps one long-lived instance; the CLI loads the
    previous result from the store on each invocation.
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
        """Load the previous verdicts, e.g. after a daemon restart."""
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
            lsf_note = "LSF queries disabled"
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
    """Fold QA results into case states (COMPLETED_MARKER -> DONE / FAILED)."""
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
