"""狀態快取的讀寫 (Phase 0 最小版)。

重要前提 (architecture 決策 2): **檔案系統是唯一真相, 這裡只是快取。**
快取損毀、遺失、格式不符時一律當作「沒有前一次狀態」重新開始, 絕不丟例外。
唯一會因此損失的是跨次呼叫的進度記憶 (stall 偵測的計時), 這是可接受的降級。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from arcx_auto.domain.enums import CaseState, LsfState
from arcx_auto.domain.models import CaseSnapshot, IndexRunSnapshot
from arcx_auto.util.atomic import atomic_write_json, read_json

SCHEMA_VERSION = 1


class SnapshotStore:
    """把 IndexRunSnapshot 存成 JSON, 讓 stall 偵測能跨次呼叫累積。"""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))

    # -- 讀 ------------------------------------------------------------

    def load(self) -> Dict[str, IndexRunSnapshot]:
        raw = read_json(self.path, default=None)
        if not isinstance(raw, dict):
            return {}
        if raw.get("schema_version") != SCHEMA_VERSION:
            # 版本不符就整份丟掉重來 —— 快取而已, 不需要遷移邏輯
            return {}
        result: Dict[str, IndexRunSnapshot] = {}
        for key, payload in (raw.get("index_runs") or {}).items():
            snapshot = _deserialize_index_run(payload)
            if snapshot is not None:
                result[key] = snapshot
        return result

    # -- 寫 ------------------------------------------------------------

    def save(self, snapshots: Dict[str, IndexRunSnapshot]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "index_runs": {
                key: _serialize_index_run(snap) for key, snap in snapshots.items()
            },
        }
        atomic_write_json(self.path, payload)


# --------------------------------------------------------------------------
# 序列化
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
