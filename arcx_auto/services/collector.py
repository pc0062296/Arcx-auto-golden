"""Collector —— 把檔案系統與 LSF 的觀測組合成一份不可變快照。

職責邊界: Collector 只負責「看到什麼」, **不做任何判斷**。
判斷全部交給 StateEngine (純函數) 與 QA Registry。

LSF job 對回 case 的難處: Arcx 送出的子 job 沒有可辨識的 job name。
但 cmd_folder/cmd_file_N 這個 script 裡的 `cd <path>` 直接給出該 job 的
執行路徑, 是**確定性**的依據 —— 不需要去猜 log 的格式。依成本由低到高:

  1. output_file 直接等於該 case 的 log 路徑
  2. exec_cwd / sub_cwd 落在 cmd_file 指出的執行路徑底下
  3. exec_cwd / sub_cwd 落在 case run dir 底下 (exec_path 解析失敗時的退路)
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.config.settings import Settings
from arcx_auto.domain.models import (
    CaseObservation,
    IndexRunObservation,
    LsfJobView,
)


class Collector:
    """組合 FsAdapter + LsfAdapter 的觀測。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        fs: Optional[FsAdapter] = None,
        lsf: Optional[LsfAdapter] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.fs = fs or FsAdapter(self.settings.layout)
        self.lsf = lsf or LsfAdapter(self.settings.lsf)

    # ------------------------------------------------------------------
    # 公開入口
    # ------------------------------------------------------------------

    def collect_index_run(
        self,
        run_folder: str,
        index_key: Optional[str] = None,
        lsf_jobs: Optional[List[LsfJobView]] = None,
        now: Optional[float] = None,
    ) -> IndexRunObservation:
        """觀測單一 index run folder。

        ``lsf_jobs`` 由呼叫端一次查好後傳入 —— 監控多個 run folder 時
        絕不能每個都各查一次 bjobs (見 LsfAdapter 的批次查詢設計)。
        """
        now = now if now is not None else time.time()
        observation = self.fs.scan_index_run_folder(run_folder, index_key, now=now)
        if lsf_jobs:
            observation = self.attach_lsf(observation, lsf_jobs)
        return observation

    def collect_wave(
        self,
        wave_dir: str,
        lsf_jobs: Optional[List[LsfJobView]] = None,
        now: Optional[float] = None,
    ) -> List[IndexRunObservation]:
        """觀測一個 wave 目錄底下所有 index run folder。"""
        now = now if now is not None else time.time()
        results: List[IndexRunObservation] = []
        for index_key, path in self.fs.list_index_run_folders(wave_dir):
            results.append(
                self.collect_index_run(path, index_key, lsf_jobs=lsf_jobs, now=now)
            )
        return results

    def fetch_lsf_jobs(self) -> Tuple[List[LsfJobView], Optional[str]]:
        """一次取回本帳號所有 job。回傳 (jobs, error)。

        error 非 None 時代表 LSF 資料不可用 —— 呼叫端必須把
        ``TransitionContext.lsf_data_available`` 設為 False, 否則會誤判 LOST。
        """
        return self.lsf.list_user_jobs()

    # ------------------------------------------------------------------
    # LSF job 對應
    # ------------------------------------------------------------------

    def attach_lsf(
        self,
        observation: IndexRunObservation,
        jobs: List[LsfJobView],
    ) -> IndexRunObservation:
        """把 LSF job 掛到對應的 case 上。"""
        if not observation.cases:
            return observation

        # 先縮小候選範圍: 只留下屬於這個 run folder 的 job
        candidates = [j for j in jobs if j.belongs_to(observation.run_folder)]
        if not candidates:
            return observation

        by_log: Dict[str, LsfJobView] = {}
        by_dir: List[Tuple[str, LsfJobView]] = []
        for job in candidates:
            if job.output_file:
                by_log[os.path.abspath(job.output_file)] = job
            for cwd in (job.exec_cwd, job.sub_cwd):
                if cwd:
                    by_dir.append((os.path.abspath(cwd).rstrip("/") + "/", job))

        updated: Dict[str, CaseObservation] = {}
        for case_id, case in observation.cases.items():
            match = self._match_job(case, by_log, by_dir)
            updated[case_id] = replace(case, lsf=match) if match else case

        return replace(observation, cases=updated)

    @staticmethod
    def _match_job(
        case: CaseObservation,
        by_log: Dict[str, LsfJobView],
        by_dir: List[Tuple[str, LsfJobView]],
    ) -> Optional[LsfJobView]:
        # 方式 1: output_file 就是這個 case 的 log —— 最可靠
        if case.log_path:
            direct = by_log.get(os.path.abspath(case.log_path))
            if direct is not None:
                return direct

        # 方式 2/3: job 的 cwd 落在 cmd_file 指出的執行路徑, 或 case run dir 底下。
        #
        # 只接受「job 的 cwd 在 case 路徑之內」這個方向。反向比對 (job 的 cwd 是
        # case 目錄的**上層**) 會把在 index run folder 執行的 parent Arcx job
        # 掛到底下每一個 case 上, 讓所有 case 顯示同一個 job —— 比對不到還糟。
        for base in (case.exec_path, case.case_dir):
            if not base:
                continue
            prefix = os.path.abspath(base).rstrip("/") + "/"
            for cwd, job in by_dir:
                if cwd.startswith(prefix):
                    return job
        return None
