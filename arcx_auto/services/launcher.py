"""Launcher —— 把 Arcx 本身 bsub 出去。

    bsub -J arcx_<run>_<wave> -o .arcx_auto/arcx_bsub.log \\
         Arcx -p arcx.cfg -d 1000 1001 -lsf0 -nt 50 --run

**為什麼 Arcx 本身也要 bsub** (架構決策 A1 的 (c) 方案): 這樣我們的 daemon
隨時可以重啟、崩潰、被 kill, 都不會影響正在跑的工作。如果 daemon 直接
spawn Arcx, daemon 一死整批就毀了 —— 而這些工作要跑好幾天。

job id 寫進 wave 目錄的 launch.json。rerun 時必須先 bkill 它, 再用
bjobs_manage.py -djp 清掉子 job (順序反過來的話, parent 會補送新的)。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from arcx_auto.adapters.arcx import ArcxAdapter
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.config.settings import Settings
from arcx_auto.services.workspace import WaveWorkspace
from arcx_auto.util.atomic import atomic_write_json, read_json

# bsub 的標準輸出: Job <12345> is submitted to queue <normal>.
_JOB_ID_RE = re.compile(r"Job\s+<(?P<job_id>\d+)>")


@dataclass(frozen=True)
class LaunchResult:
    wave_name: str
    ok: bool
    job_id: Optional[str] = None
    command: tuple = ()
    stdout: str = ""
    error: Optional[str] = None
    dry_run: bool = False


class Launcher:
    """提交一個 wave。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        lsf: Optional[LsfAdapter] = None,
        arcx: Optional[ArcxAdapter] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.lsf = lsf or LsfAdapter(self.settings.lsf)
        self.arcx = arcx or ArcxAdapter(self.settings.layout, self.settings.plan)

    # ------------------------------------------------------------------

    def build_command(self, workspace: WaveWorkspace, run_id: str,
                      rerun: bool = False) -> List[str]:
        """組出完整的 bsub 指令。

        cfg 用 wave 目錄內的**快照**而不是原檔 —— 這樣原檔之後被改過,
        也不影響已經送出去的工作, 而且事後 QA 用的是同一份。
        """
        launch = self.settings.launch
        job_name = launch.job_name_template.format(
            run_id=run_id or "run", wave=workspace.wave_name)

        argv: List[str] = [self.settings.lsf.bsub_cmd, "-J", job_name]
        if launch.output_file:
            argv.extend(["-o", os.path.join(workspace.path, launch.output_file)])
        argv.extend(launch.bsub_args)
        argv.extend(self.arcx.build_run_command(
            workspace.arcx_cfg, list(workspace.index_keys),
            rerun=rerun, lsf_settings=self.settings.lsf,
        ))
        return argv

    def launch(
        self,
        workspace: WaveWorkspace,
        run_id: str,
        rerun: bool = False,
        dry_run: bool = False,
        now: Optional[float] = None,
    ) -> LaunchResult:
        """提交。dry_run 時只組指令不執行。"""
        now = now if now is not None else time.time()
        argv = self.build_command(workspace, run_id, rerun=rerun)

        if dry_run:
            return LaunchResult(wave_name=workspace.wave_name, ok=True,
                                command=tuple(argv), dry_run=True)

        os.makedirs(os.path.dirname(
            os.path.join(workspace.path, self.settings.launch.output_file)),
            exist_ok=True)

        result = self.lsf._run(argv)  # noqa: SLF001 - adapter 內部的統一執行入口
        if not result.ok:
            error = result.error or result.stderr.strip() or "bsub 執行失敗"
            self._record(workspace, run_id, argv, None, error, now, rerun)
            return LaunchResult(wave_name=workspace.wave_name, ok=False,
                                command=tuple(argv), stdout=result.stdout,
                                error=error)

        match = _JOB_ID_RE.search(result.stdout)
        job_id = match.group("job_id") if match else None
        error = None if job_id else "bsub 成功但輸出中找不到 job id"

        self._record(workspace, run_id, argv, job_id, error, now, rerun)
        return LaunchResult(
            wave_name=workspace.wave_name, ok=job_id is not None, job_id=job_id,
            command=tuple(argv), stdout=result.stdout, error=error,
        )

    # ------------------------------------------------------------------

    def _record(self, workspace: WaveWorkspace, run_id: str, argv: List[str],
                job_id: Optional[str], error: Optional[str], now: float,
                rerun: bool) -> None:
        """把提交紀錄寫進 wave 目錄。

        用 append 的形式保留每一次 attempt —— rerun 之後仍然看得到
        第一次是什麼時候用什麼指令送的。
        """
        existing = read_json(workspace.launch_json, default=None) or {}
        attempts = list(existing.get("attempts") or [])
        attempts.append({
            "attempt": len(attempts) + 1,
            "submitted_at": now,
            "job_id": job_id,
            "command": argv,
            "cwd": workspace.path,
            "rerun": rerun,
            "error": error,
        })
        atomic_write_json(workspace.launch_json, {
            "run_id": run_id,
            "wave": workspace.wave_name,
            "index_keys": list(workspace.index_keys),
            "arcx_job_id": job_id or existing.get("arcx_job_id"),
            "attempts": attempts,
        })


def read_launch(workspace_path: str) -> Dict[str, Any]:
    """讀某個 wave 目錄的提交紀錄。rerun 需要從這裡拿 parent job id。"""
    return read_json(
        os.path.join(workspace_path, ".arcx_auto", "launch.json"),
        default={}) or {}
