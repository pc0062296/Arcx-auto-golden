"""Collector —— 把檔案系統與 LSF 的觀測組合成一份不可變快照。

職責邊界: Collector 只負責「看到什麼」, **不做任何判斷**。
判斷全部交給 StateEngine (純函數) 與 QA Registry。

LSF job 對回 case 的難處: Arcx 送出的子 job 沒有可辨識的 job name,
因此依序嘗試三種對應方式 (成本由低到高):
  1. output_file 直接等於該 case 的 log 路徑        —— 最可靠, 零額外 I/O
  2. exec_cwd / sub_cwd 落在該 case 的 run dir 底下  —— 可靠, 零額外 I/O
  3. 讀 log 開頭幾 KB, 從中找出執行路徑再比對        —— 有 I/O, 結果會快取
"""

from __future__ import annotations

import os
import re
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

_ABS_PATH_RE = re.compile(r"(/[^\s'\"<>|;:,()\[\]]+)")


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
        # log 開頭的路徑解析結果快取: log_path -> 解析出的路徑
        # log 開頭永遠不會變, 所以只需要讀一次。
        self._log_head_cache: Dict[str, Optional[str]] = {}

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
        unmatched = list(candidates)

        for case_id, case in observation.cases.items():
            match = self._match_job(case, by_log, by_dir)
            if match is not None:
                updated[case_id] = replace(case, lsf=match)
                if match in unmatched:
                    unmatched.remove(match)
            else:
                updated[case_id] = case

        return replace(observation, cases=updated)

    def _match_job(
        self,
        case: CaseObservation,
        by_log: Dict[str, LsfJobView],
        by_dir: List[Tuple[str, LsfJobView]],
    ) -> Optional[LsfJobView]:
        # 方式 1: output_file 就是這個 case 的 log
        if case.log_path:
            direct = by_log.get(os.path.abspath(case.log_path))
            if direct is not None:
                return direct

        # 方式 2: job 的 cwd 落在這個 case 的 run dir 底下
        if case.case_dir:
            case_prefix = os.path.abspath(case.case_dir).rstrip("/") + "/"
            for cwd, job in by_dir:
                if cwd.startswith(case_prefix):
                    return job

        # 方式 3: 從 log 開頭解析出執行路徑, 再跟 job 的 cwd 比對。
        #
        # 只接受「job 的 cwd 在解析出的路徑之內」這個方向。反向比對
        # (job 的 cwd 是 case 目錄的**上層**) 看似寬鬆一點, 實際上會把在
        # index run folder 執行的 parent Arcx job 掛到底下每一個 case 上,
        # 讓所有 case 都顯示同一個 job 狀態 —— 這比對不到還糟。
        head_path = self._path_from_log_head(case.log_path)
        if head_path:
            head_prefix = head_path.rstrip("/") + "/"
            for cwd, job in by_dir:
                if cwd.startswith(head_prefix):
                    return job
        return None

    def _path_from_log_head(self, log_path: Optional[str]) -> Optional[str]:
        """從 log 開頭幾 KB 找出這個 job 的執行路徑。

        Arcx 的 log 開頭會印出執行路徑, 但格式不固定, 因此採用
        「抓出所有絕對路徑, 取最長且真的存在的那個目錄」這種與格式無關的作法。
        結果會快取 —— log 開頭永遠不會變。
        """
        if not log_path:
            return None
        if log_path in self._log_head_cache:
            return self._log_head_cache[log_path]

        head = self.fs.read_head(log_path, self.settings.monitor.log_head_bytes)
        best: Optional[str] = None
        for match in _ABS_PATH_RE.finditer(head):
            candidate = match.group(1).rstrip("/")
            if len(candidate) < 2:
                continue
            directory = candidate if os.path.isdir(candidate) else os.path.dirname(candidate)
            if not directory or not os.path.isdir(directory):
                continue
            if best is None or len(directory) > len(best):
                best = directory

        self._log_head_cache[log_path] = best
        return best
