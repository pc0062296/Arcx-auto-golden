"""The daemon main loop.

    each tick:  collect -> transition -> QA -> resolve -> persist -> export

Three properties:

  * **Killable and restartable at any time.** On startup it reloads the last
    verdicts from state.json so stall timers continue, but even without that
    file it rebuilds everything from the run folders -- the filesystem is the
    only truth (architecture decision 2).

  * **The single writer.** flock guarantees one daemon per state root. The UI
    and CLI are read only.

  * **One failed tick must not kill the daemon.** NFS hiccups and LSF timeouts
    are routine; the error is recorded in daemon.last_error where people can
    see it, and the next tick runs.

No asyncio: scandir, bjobs and stat are all blocking I/O, synchronous code is
easier to follow and to debug, and a single-user scale of a few hundred cases
needs no concurrency at all.
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
from arcx_auto.services.exporter import Exporter
from arcx_auto.services.monitor import MonitorService, ScanResult


@dataclass
class DaemonOptions:
    run_id: str
    wave_dirs: List[str] = field(default_factory=list)
    run_folders: List[str] = field(default_factory=list)
    arcx_cfg: Optional[str] = None
    interval_sec: Optional[float] = None      # None -> tiered interval
    use_lsf: bool = True
    once: bool = False
    max_ticks: Optional[int] = None           # for tests
    export: Optional[bool] = None             # None -> follow settings


class Daemon:
    """The monitoring daemon."""

    def __init__(
        self,
        options: DaemonOptions,
        settings: Optional[Settings] = None,
        monitor: Optional[MonitorService] = None,
        on_tick: Optional[Callable[[ScanResult], None]] = None,
        exporter: Optional[Exporter] = None,
    ) -> None:
        self.options = options
        self.settings = settings or Settings()
        self.monitor = monitor or MonitorService(self.settings)
        self.exporter = exporter or Exporter(self.settings)
        self.store = RunStore(self.settings.expanded_state_root(), options.run_id)
        self.on_tick = on_tick

        self._stop = threading.Event()
        self._started_at = 0.0
        self._tick = 0
        self._last_error: Optional[str] = None
        self._export_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Start. Returns an exit code."""
        lock = FileLock(
            self.store.dir + "/daemon.lock",
            purpose="arcx-auto daemon run_id=%s" % self.options.run_id,
        )
        self.store.ensure()
        try:
            lock.acquire()
        except LockBusy as exc:
            print("another daemon is already monitoring this run: %s" % exc)
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
            "reason": "user started monitoring",
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
            "reason": ("stop signal received" if self._stop.is_set()
                       else "finish condition reached"),
        })
        return 0

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            # Only set a flag so the current tick finishes: interrupting it
            # mid-way would leave half-written state.
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass

    # ------------------------------------------------------------------
    # One tick
    # ------------------------------------------------------------------

    def _safe_tick(self, arcx_config: Optional[ArcxConfig]) -> Optional[ScanResult]:
        """Run one scan. Any exception is recorded and the loop continues:
        NFS hiccups and LSF timeouts are routine and must not kill monitoring.
        """
        try:
            result = self.monitor.scan(
                wave_dirs=self.options.wave_dirs,
                run_folders=self.options.run_folders,
                arcx_config=arcx_config,
                use_lsf=self.options.use_lsf,
            )
        except Exception:  # noqa: BLE001 - see the docstring
            self._last_error = traceback.format_exc(limit=6)
            self._write_error_state()
            return None

        self._last_error = None
        self._persist(result)
        self._publish()
        if self.on_tick is not None:
            self.on_tick(result)
        return result

    def _publish(self) -> None:
        """Push the shared-disk view, on its own slower schedule.

        Everything here is best effort. The share can be unmounted, full or
        read only, and none of that is a reason to stop monitoring -- so a
        failure is recorded where people can see it and the loop carries on.
        """
        if self.options.export is False:
            return
        if self.options.export is None and not self.settings.export.enabled:
            return
        try:
            result = self.exporter.export_now()
        except Exception:  # noqa: BLE001 - publishing must never kill the loop
            self._export_error = traceback.format_exc(limit=4)
            return
        self._export_error = (
            "; ".join(result.errors) if result.errors else None)

    def _persist(self, result: ScanResult) -> None:
        payload = build_state_payload(
            self.options.run_id, result,
            daemon_info(self._started_at, self._tick, self._last_error,
                        self._export_error),
        )
        self.store.write_state(payload)
        if result.events:
            self.store.append_events(as_json_dict(e) for e in result.events)

    def _write_error_state(self) -> None:
        """Update state.json even when the scan failed, or the UI shows stale
        data while looking perfectly healthy.
        """
        state = self.store.read_state()
        state.setdefault("run_id", self.options.run_id)
        state["daemon"] = daemon_info(
            self._started_at, self._tick, self._last_error, self._export_error)
        state["updated_at"] = time.time()
        self.store.write_state(state)

    def _next_interval(self, result: Optional[ScanResult]) -> float:
        """Tiered polling: scan often while cases run, slow down when idle."""
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
    # Restoring state on startup
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
        """Reload the previous verdicts so stall timers survive a restart.

        Failing to reload is fine: the next scan reseeds from log mtimes (see
        the first-observation seeding in StateEngine).
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
    """Rebuild one IndexRunSnapshot from state.json.

    Only the fields the state machine needs are restored; everything else is
    recomputed by the next scan.
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
