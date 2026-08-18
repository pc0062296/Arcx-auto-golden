"""Reading and writing the state cache.

The premise (architecture decision 2): **the filesystem is the only truth and
this is just a cache.** A damaged, missing or version-mismatched cache is
treated as "no previous state" and never raises. The only thing lost is the
progress memory across calls (the stall timer), which is an acceptable
degradation.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional

from arcx_auto.domain.enums import CaseState, LsfState
from arcx_auto.domain.models import CaseSnapshot, IndexRunSnapshot
from arcx_auto.util.atomic import append_jsonl, atomic_write_json, read_json

SCHEMA_VERSION = 1


class SnapshotStore:
    """Persist IndexRunSnapshots so stall detection accumulates across calls."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))

    # -- read ----------------------------------------------------------

    def load(self) -> Dict[str, IndexRunSnapshot]:
        raw = read_json(self.path, default=None)
        if not isinstance(raw, dict):
            return {}
        if raw.get("schema_version") != SCHEMA_VERSION:
            # A version mismatch throws the whole file away: it is only a
            # cache, so migration logic would be dead weight.
            return {}
        result: Dict[str, IndexRunSnapshot] = {}
        for key, payload in (raw.get("index_runs") or {}).items():
            snapshot = _deserialize_index_run(payload)
            if snapshot is not None:
                result[key] = snapshot
        return result

    # -- write ---------------------------------------------------------

    def save(self, snapshots: Dict[str, IndexRunSnapshot]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "index_runs": {
                key: _serialize_index_run(snap) for key, snap in snapshots.items()
            },
        }
        atomic_write_json(self.path, payload)


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------

def _serialize_case(snap: CaseSnapshot) -> Dict[str, Any]:
    return {
        "case_id": snap.case_id,
        "state": snap.state.value,
        "base_state": snap.base_state.value if snap.base_state else None,
        "entered_state_at": snap.entered_state_at,
        "last_progress_at": snap.last_progress_at,
        "last_progress_size": snap.last_progress_size,
        "last_seen_at": snap.last_seen_at,
        "lsf_job_id": snap.lsf_job_id,
        "lsf_state": snap.lsf_state.value if snap.lsf_state else None,
        "lsf_missing_since": snap.lsf_missing_since,
        "case_dir": snap.case_dir,
        "log_path": snap.log_path,
        "exec_path": snap.exec_path,
        "marker_inconsistent": snap.marker_inconsistent,
        "note": snap.note,
    }


def _deserialize_case(data: Dict[str, Any]) -> Optional[CaseSnapshot]:
    try:
        return CaseSnapshot(
            case_id=data["case_id"],
            state=CaseState(data["state"]),
            base_state=(CaseState(data["base_state"])
                        if data.get("base_state") else None),
            entered_state_at=float(data["entered_state_at"]),
            last_progress_at=float(data["last_progress_at"]),
            last_progress_size=int(data.get("last_progress_size") or 0),
            last_seen_at=float(data.get("last_seen_at") or 0.0),
            lsf_job_id=data.get("lsf_job_id"),
            lsf_state=LsfState(data["lsf_state"]) if data.get("lsf_state") else None,
            lsf_missing_since=data.get("lsf_missing_since"),
            case_dir=data.get("case_dir"),
            log_path=data.get("log_path"),
            exec_path=data.get("exec_path"),
            marker_inconsistent=bool(data.get("marker_inconsistent")),
            note=data.get("note"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _serialize_index_run(snap: IndexRunSnapshot) -> Dict[str, Any]:
    return {
        "index_key": snap.index_key,
        "run_folder": snap.run_folder,
        "updated_at": snap.updated_at,
        "error": snap.error,
        "cases": {cid: _serialize_case(c) for cid, c in snap.cases.items()},
    }


def _deserialize_index_run(data: Any) -> Optional[IndexRunSnapshot]:
    if not isinstance(data, dict):
        return None
    try:
        cases = {}
        for cid, payload in (data.get("cases") or {}).items():
            case = _deserialize_case(payload)
            if case is not None:
                cases[cid] = case
        return IndexRunSnapshot(
            index_key=data["index_key"],
            run_folder=data["run_folder"],
            updated_at=float(data.get("updated_at") or 0.0),
            cases=cases,
            error=data.get("error"),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ==========================================================================
# RunStore -- what the daemon persists
# ==========================================================================

class RunStore:
    """Everything one monitoring session persists.

        ~/.arcx-auto/runs/<run_id>/
          manifest.json   immutable: creation time, targets, cfg path
          state.json      mutable snapshot, written atomically; the UI reads it
          events.jsonl    append-only state transitions
          audit.jsonl     append-only record of every write action
          commands/       action intents posted by the UI/CLI, consumed by the
                          daemon and then deleted

    Again (architecture decision 2): **the filesystem is the only truth and
    this is a cache.** Delete the whole directory and a rescan rebuilds it;
    only the history is lost.
    """

    def __init__(self, root: str, run_id: str) -> None:
        self.run_id = run_id
        self.dir = os.path.join(
            os.path.abspath(os.path.expanduser(root)), "runs", run_id)
        self.manifest_path = os.path.join(self.dir, "manifest.json")
        self.state_path = os.path.join(self.dir, "state.json")
        self.events_path = os.path.join(self.dir, "events.jsonl")
        self.audit_path = os.path.join(self.dir, "audit.jsonl")
        self.commands_dir = os.path.join(self.dir, "commands")

    def ensure(self) -> None:
        os.makedirs(self.commands_dir, exist_ok=True)

    # -- manifest (immutable) ------------------------------------------

    def write_manifest(self, payload: Dict[str, Any]) -> None:
        """Written once, at creation. The manifest records the original
        intent, and nothing that happens later should rewrite it.
        """
        self.ensure()
        if os.path.exists(self.manifest_path):
            return
        atomic_write_json(self.manifest_path, payload)

    def read_manifest(self) -> Dict[str, Any]:
        return read_json(self.manifest_path, default={}) or {}

    # -- state (mutable snapshot) --------------------------------------

    def write_state(self, payload: Dict[str, Any]) -> None:
        atomic_write_json(self.state_path, payload)

    def read_state(self) -> Dict[str, Any]:
        return read_json(self.state_path, default={}) or {}

    # -- append-only records -------------------------------------------

    def append_events(self, records: Iterable[Dict[str, Any]]) -> None:
        for record in records:
            append_jsonl(self.events_path, record)

    def append_audit(self, record: Dict[str, Any]) -> None:
        """Every write action records who, when, on what, and why."""
        enriched = dict(record)
        enriched.setdefault("ts", time.time())
        enriched.setdefault("pid", os.getpid())
        append_jsonl(self.audit_path, enriched)

    # -- discovery -----------------------------------------------------

    @staticmethod
    def list_runs(root: str) -> List[str]:
        """List existing run ids, most recently updated first."""
        runs_dir = os.path.join(
            os.path.abspath(os.path.expanduser(root)), "runs")
        try:
            names = [n for n in os.listdir(runs_dir)
                     if os.path.isdir(os.path.join(runs_dir, n))]
        except OSError:
            return []

        def sort_key(name: str) -> float:
            try:
                return -os.path.getmtime(os.path.join(runs_dir, name, "state.json"))
            except OSError:
                return 0.0

        return sorted(names, key=sort_key)
