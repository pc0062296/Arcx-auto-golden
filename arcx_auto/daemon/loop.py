"""daemon 主迴圈。

    每個 tick:  collect → transition → QA → resolve → 持久化 → 匯出

三個性質:

  * **可隨時被 kill 並重啟。** 啟動時從 state.json 載回上一次的判定
    (讓 stall 計時延續), 但即使那份檔案不見了也能從 run folder 重建 ——
    檔案系統才是唯一真相 (architecture 決策 2)。

  * **唯一寫入者。** 用 flock 保證同一個 state root 只有一個 daemon。
    UI 與 CLI 全部唯讀。

  * **一次 tick 失敗不能讓 daemon 死掉。** NFS 抽風、LSF 逾時都是常態,
    錯誤記進 state.json 的 daemon.last_error 讓人看得到, 然後繼續下一個 tick。

不用 asyncio: scandir / bjobs / stat 全是 blocking I/O, 同步程式碼更好懂
也更好除錯, 而單人規模 (數百 case) 完全不需要並行。
"""

from __future__ import annotations

import signal
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from arcx_auto.adapters.arcx_cfg import ArcxConfig, parse_arcx_cfg
from arcx_auto.adapters.lock import FileLock, LockBusy
from arcx_auto.adapters.store import RunStore, _deserialize_index_run
from arcx_auto.config.settings import Settings
from arcx_auto.daemon.state import build_state_payload, daemon_info
from arcx_auto.domain.models import as_json_dict
from arcx_auto.services.monitor import MonitorService, ScanResult


@dataclass
class DaemonOptions:
    run_id: str
    wave_dirs: List[str] = field(default_factory=list)
    run_folders: List[str] = field(default_factory=list)
    arcx_cfg: Optional[str] = None
    interval_sec: Optional[float] = None      # None -> 用 settings 的分層間隔
    use_lsf: bool = True
    once: bool = False
    max_ticks: Optional[int] = None           # 測試用


class Daemon:
    """監控 daemon。"""

    def __init__(
        self,
        options: DaemonOptions,
        settings: Optional[Settings] = None,
        monitor: Optional[MonitorService] = None,
        on_tick: Optional[Callable[[ScanResult], None]] = None,
    ) -> None:
        self.options = options
        self.settings = settings or Settings()
        self.monitor = monitor or MonitorService(self.settings)
        self.store = RunStore(self.settings.expanded_state_root(), options.run_id)
        self.on_tick = on_tick

        self._stop = threading.Event()
        self._started_at = 0.0
        self._tick = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------

    def run(self) -> int:
        """啟動。回傳 exit code。"""
        lock = FileLock(
            self.store.dir + "/daemon.lock",
            purpose="arcx-auto daemon run_id=%s" % self.options.run_id,
        )
        self.store.ensure()
        try:
            lock.acquire()
        except LockBusy as exc:
            print("已經有另一個 daemon 在監控這個 run: %s" % exc)
            return 1

        try:
            return self._run_locked()
        finally:
            lock.release()

    def _run_locked(self) -> int:
        self._started_at = time.time()
        self._install_signal_handlers()
        self._write_manifest()
        self._restore_previous()
        self.store.append_audit({
            "action": "daemon_start",
            "run_id": self.options.run_id,
            "wave_dirs": list(self.options.wave_dirs),
            "run_folders": list(self.options.run_folders),
            "reason": "使用者啟動監控",
        })

        arcx_config = self._load_cfg()

        while not self._stop.is_set():
            self._tick += 1
            result = self._safe_tick(arcx_config)

            if self.options.once:
                break
            if self.options.max_ticks and self._tick >= self.options.max_ticks:
                break
            self._stop.wait(self._next_interval(result))

        self.store.append_audit({
            "action": "daemon_stop",
            "run_id": self.options.run_id,
            "ticks": self._tick,
            "reason": "收到停止訊號" if self._stop.is_set() else "達到結束條件",
        })
        return 0

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            # 只設旗標, 讓當前 tick 走完再退出 —— 半途中斷會留下寫到一半的狀態
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - 非主執行緒
                pass

    # ------------------------------------------------------------------
    # 每個 tick
    # ------------------------------------------------------------------

    def _safe_tick(self, arcx_config: Optional[ArcxConfig]) -> Optional[ScanResult]:
        """跑一次掃描。任何例外都記下來並繼續 —— NFS 抽風、LSF 逾時是常態,
        不該讓監控整個死掉。
        """
        try:
            result = self.monitor.scan(
                wave_dirs=self.options.wave_dirs,
                run_folders=self.options.run_folders,
                arcx_config=arcx_config,
                use_lsf=self.options.use_lsf,
            )
        except Exception:  # noqa: BLE001 - 見 docstring
            self._last_error = traceback.format_exc(limit=6)
            self._write_error_state()
            return None

        self._last_error = None
        self._persist(result)
        if self.on_tick is not None:
            self.on_tick(result)
        return result

    def _persist(self, result: ScanResult) -> None:
        payload = build_state_payload(
            self.options.run_id, result,
            daemon_info(self._started_at, self._tick, self._last_error),
        )
        self.store.write_state(payload)
        if result.events:
            self.store.append_events(as_json_dict(e) for e in result.events)

    def _write_error_state(self) -> None:
        """掃描失敗時也要更新 state.json —— 否則 UI 會顯示過期資料
        卻看起來一切正常。
        """
        state = self.store.read_state()
        state.setdefault("run_id", self.options.run_id)
        state["daemon"] = daemon_info(self._started_at, self._tick, self._last_error)
        state["updated_at"] = time.time()
        self.store.write_state(state)

    def _next_interval(self, result: Optional[ScanResult]) -> float:
        """分層 polling: 有 case 在跑就掃勤一點, 全部結束就放慢。"""
        if self.options.interval_sec:
            return self.options.interval_sec
        monitor = self.settings.monitor
        if result is None:
            return monitor.poll_active_sec
        for snapshot in result.snapshots:
            for case in snapshot.cases.values():
                if case.state.is_in_flight:
                    return monitor.poll_active_sec
        return monitor.poll_idle_sec

    # ------------------------------------------------------------------
    # 啟動時的還原
    # ------------------------------------------------------------------

    def _write_manifest(self) -> None:
        self.store.write_manifest({
            "run_id": self.options.run_id,
            "created_at": time.time(),
            "wave_dirs": list(self.options.wave_dirs),
            "run_folders": list(self.options.run_folders),
            "arcx_cfg": self.options.arcx_cfg,
        })

    def _restore_previous(self) -> None:
        """從 state.json 載回上一次的判定, 讓 stall 計時跨重啟延續。

        載不回來也沒關係 —— 下一次掃描會從 log mtime 重新推算
        (見 StateEngine 的首次觀測種子邏輯)。
        """
        state = self.store.read_state()
        restored = {}
        for index in state.get("indexes") or []:
            snapshot = _index_from_state(index)
            if snapshot is not None:
                restored[snapshot.run_folder] = snapshot
        if restored:
            self.monitor.prime(restored)

    def _load_cfg(self) -> Optional[ArcxConfig]:
        path = self.options.arcx_cfg
        if not path:
            import os

            for wave_dir in self.options.wave_dirs:
                candidate = os.path.join(wave_dir, "arcx.cfg")
                if os.path.isfile(candidate):
                    path = candidate
                    break
        if not path:
            return None
        return parse_arcx_cfg(path)


def _index_from_state(payload: Dict[str, Any]):
    """把 state.json 裡的一個 index 還原成 IndexRunSnapshot。

    只還原狀態機需要的欄位 —— 其餘都會在下一次掃描重新算出來。
    """
    cases = {}
    for case in payload.get("cases") or []:
        cases[case["case_id"]] = {
            "case_id": case["case_id"],
            "state": case.get("base_state") or case["state"],
            "entered_state_at": case.get("entered_state_at", 0.0),
            "last_progress_at": (
                case.get("entered_state_at", 0.0)
                if case.get("silent_sec") is None
                else payload.get("updated_at", 0.0) - case.get("silent_sec", 0.0)
            ),
            "last_progress_size": case.get("log_size") or 0,
            "lsf_job_id": case.get("lsf_job_id"),
            "lsf_state": case.get("lsf_state"),
            "case_dir": case.get("case_dir"),
            "log_path": case.get("log_path"),
            "exec_path": case.get("exec_path"),
            "marker_inconsistent": case.get("marker_inconsistent", False),
            "base_state": case.get("base_state"),
        }
    return _deserialize_index_run({
        "index_key": payload.get("index_key", "?"),
        "run_folder": payload.get("run_folder", ""),
        "updated_at": payload.get("updated_at", 0.0),
        "cases": cases,
    })
