"""Turn one scan result into the contents of state.json.

state.json is the only interface between the daemon and the UI, and it is
deliberately **self-contained plain data**: the UI imports no service and
recomputes nothing, it only renders.

Again (architecture decision 2): this is a cache, not the truth. Delete it and
the next daemon scan rebuilds it.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any, Dict, List, Optional, Sequence

from arcx_auto.domain.enums import CaseState, Severity
from arcx_auto.domain.models import IndexRunObservation, IndexRunSnapshot
from arcx_auto.domain.qa import Issue
from arcx_auto.services.monitor import ScanResult

SCHEMA_VERSION = 1

#: Display order in the UI: things needing attention come first
STATE_DISPLAY_ORDER = [
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


def build_state_payload(
    run_id: str,
    result: ScanResult,
    daemon_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the state.json payload."""
    issues_by_case: Dict[str, List[Issue]] = {}
    for report in result.qa_reports:
        for case_id in report.case_results:
            issues_by_case.setdefault(
                _case_key(report.index_key, case_id), []
            ).extend(report.issues_for(case_id))

    observations = {o.run_folder: o for o in result.observations}

    indexes = [
        _index_payload(snapshot, observations.get(snapshot.run_folder),
                       issues_by_case, result.scanned_at)
        for snapshot in result.snapshots
    ]

    all_issues = [_issue_payload(i) for i in result.all_issues()]

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "updated_at": result.scanned_at,
        "lsf": {
            "available": result.lsf_available,
            "note": result.lsf_note,
            "job_count": result.lsf_job_count,
        },
        "daemon": daemon_info or {},
        "totals": _totals(indexes, all_issues),
        "indexes": indexes,
        "issues": all_issues,
    }


def _case_key(index_key: str, case_id: str) -> str:
    return "%s\x00%s" % (index_key, case_id)


def _index_payload(
    snapshot: IndexRunSnapshot,
    observation: Optional[IndexRunObservation],
    issues_by_case: Dict[str, List[Issue]],
    now: float,
) -> Dict[str, Any]:
    cases = []
    for case_id, case in snapshot.cases.items():
        issues = issues_by_case.get(_case_key(snapshot.index_key, case_id), [])
        cases.append({
            "case_id": case_id,
            "state": case.state.value,
            "base_state": case.base_state.value if case.base_state else None,
            "note": case.note,
            "entered_state_at": case.entered_state_at,
            "in_state_sec": max(0.0, now - case.entered_state_at),
            "silent_sec": case.silent_for(now),
            "log_size": case.last_progress_size,
            "log_path": case.log_path,
            "case_dir": case.case_dir,
            "exec_path": case.exec_path,
            "lsf_job_id": case.lsf_job_id,
            "lsf_state": case.lsf_state.value if case.lsf_state else None,
            "marker_inconsistent": case.marker_inconsistent,
            "issues": [_issue_payload(i) for i in issues],
            "worst_severity": _worst(issues),
        })
    cases.sort(key=lambda c: _state_rank(c["state"]))

    anomalies: Dict[str, Any] = {}
    if observation is not None:
        anomalies = {
            "unknown_markers": [
                {"file": n, "case_id": c} for n, c in observation.unknown_markers
            ],
            "unresolved_logs": list(observation.unresolved_logs),
            "unmatched_entries": list(observation.unmatched_entries),
        }

    return {
        "index_key": snapshot.index_key,
        "run_folder": snapshot.run_folder,
        "updated_at": snapshot.updated_at,
        "error": snapshot.error,
        "counts": _count_states(snapshot),
        "attention": sum(
            1 for c in snapshot.cases.values() if c.state.needs_attention),
        "cases": cases,
        "anomalies": anomalies,
    }


def _issue_payload(issue: Issue) -> Dict[str, Any]:
    return {
        "id": issue.id,
        "severity": issue.severity.value,
        "title": issue.title,
        "message": issue.message,
        "scope": issue.scope.value,
        "stage": issue.stage.value,
        "index_key": issue.index_key,
        "case_id": issue.case_id,
        "evidence": issue.evidence,
        "doc": issue.doc,
    }


def _count_states(snapshot: IndexRunSnapshot) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for case in snapshot.cases.values():
        counts[case.state.value] = counts.get(case.state.value, 0) + 1
    return counts


def _totals(indexes: Sequence[Dict[str, Any]],
            issues: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    states: Dict[str, int] = {}
    for index in indexes:
        for state, count in index["counts"].items():
            states[state] = states.get(state, 0) + count

    severities: Dict[str, int] = {}
    for issue in issues:
        severities[issue["severity"]] = severities.get(issue["severity"], 0) + 1

    return {
        "indexes": len(indexes),
        "cases": sum(states.values()),
        "states": states,
        "attention": sum(index["attention"] for index in indexes),
        "issues": len(issues),
        "severities": severities,
    }


def _state_rank(state: str) -> int:
    for rank, candidate in enumerate(STATE_DISPLAY_ORDER):
        if candidate.value == state:
            return rank
    return len(STATE_DISPLAY_ORDER)


def _worst(issues: Sequence[Issue]) -> Optional[str]:
    for severity in (Severity.FATAL, Severity.UNKNOWN, Severity.WARN,
                     Severity.INFO):
        if any(i.severity == severity for i in issues):
            return severity.value
    return None


def daemon_info(started_at: float, tick: int,
                last_error: Optional[str] = None) -> Dict[str, Any]:
    return {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "user": os.environ.get("USER", "?"),
        "started_at": started_at,
        "uptime_sec": max(0.0, time.time() - started_at),
        "tick": tick,
        "last_error": last_error,
    }
