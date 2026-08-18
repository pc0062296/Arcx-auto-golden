"""檔案系統 adapter。

效能設計 (針對 NFS 上動輒數萬個 entry 的 run folder):
  * **一次 os.scandir 掃完整個目錄**, 在記憶體裡分類。
    不做 per-case 的 os.path.exists —— 那會變成 N 次 NFS round trip。
  * scandir 的 DirEntry 自帶 is_dir/is_file 快取, 不額外 stat。
  * log 只 stat (size/mtime), 不讀內容; 需要讀時只讀開頭幾 KB。
"""

from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from arcx_auto.config.settings import LayoutSettings
from arcx_auto.domain.enums import MarkerKind
from arcx_auto.domain.models import CaseObservation, IndexRunObservation


class FsAdapter:
    """封裝所有對 run folder 的唯讀存取。"""

    def __init__(self, layout: Optional[LayoutSettings] = None) -> None:
        self.layout = layout or LayoutSettings()
        self._marker_re = re.compile(self.layout.marker_regex)
        self._case_dir_re = re.compile(self.layout.case_dir_regex)
        self._log_re = re.compile(self.layout.log_regex)
        self._report_re = re.compile(self.layout.report_dir_regex)

    # ------------------------------------------------------------------
    # 掃描 index run folder
    # ------------------------------------------------------------------

    def scan_index_run_folder(
        self,
        run_folder: str,
        index_key: Optional[str] = None,
        now: Optional[float] = None,
    ) -> IndexRunObservation:
        """掃描一個 index run folder, 產出觀測快照。

        case 的來源刻意取三者的**聯集**:
            marker (.complete.caseN) / case run dir (caseN/) / log 檔
        因為三者缺一都代表某種異常, 只看其中一個會漏掉:
          - 只有 dir 沒有 marker  -> case 建立了但從未被提交
          - 只有 marker 沒有 dir  -> 目錄被誤刪或尚未建立
          - 只有 log 沒有 marker  -> marker 寫入失敗
        """
        now = now if now is not None else time.time()
        run_folder = os.path.abspath(os.path.expanduser(run_folder))
        key = index_key if index_key is not None else os.path.basename(run_folder)

        if not os.path.isdir(run_folder):
            return IndexRunObservation(
                index_key=key,
                run_folder=run_folder,
                observed_at=now,
                error="run folder 不存在或不是目錄",
            )

        markers: Dict[str, Set[MarkerKind]] = {}
        case_dirs: Dict[str, str] = {}
        logs: Dict[str, str] = {}
        report_dirs: List[str] = []
        unmatched: List[str] = []

        try:
            entries = list(os.scandir(run_folder))
        except OSError as exc:
            return IndexRunObservation(
                index_key=key,
                run_folder=run_folder,
                observed_at=now,
                error="無法讀取 run folder: %s" % exc,
            )

        for entry in entries:
            name = entry.name
            classified = False

            marker_match = self._marker_re.match(name)
            if marker_match:
                kind_raw = marker_match.group("kind")
                case_id = marker_match.group("case")
                try:
                    kind = MarkerKind(kind_raw)
                except ValueError:
                    unmatched.append(name)
                    continue
                markers.setdefault(case_id, set()).add(kind)
                continue

            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False

            if is_dir:
                if self._report_re.match(name):
                    report_dirs.append(name)
                    classified = True
                else:
                    dir_match = self._case_dir_re.match(name)
                    if dir_match:
                        case_dirs[dir_match.group("case")] = entry.path
                        classified = True
            else:
                log_match = self._log_re.match(name)
                if log_match:
                    case_id = self._case_id_from_log(log_match)
                    if case_id:
                        logs[case_id] = entry.path
                        classified = True

            if not classified:
                unmatched.append(name)

        all_case_ids = sorted(
            set(markers) | set(case_dirs) | set(logs), key=_case_sort_key
        )

        cases: Dict[str, CaseObservation] = {}
        for case_id in all_case_ids:
            log_path = logs.get(case_id)
            size, mtime = self.stat_file(log_path) if log_path else (None, None)
            case_dir = case_dirs.get(case_id)
            cases[case_id] = CaseObservation(
                case_id=case_id,
                markers=frozenset(markers.get(case_id, set())),
                case_dir=case_dir,
                case_dir_exists=case_dir is not None,
                log_path=log_path,
                log_size=size,
                log_mtime=mtime,
            )

        return IndexRunObservation(
            index_key=key,
            run_folder=run_folder,
            observed_at=now,
            cases=cases,
            report_dirs=tuple(sorted(report_dirs)),
            unmatched_entries=tuple(sorted(unmatched)),
        )

    def _case_id_from_log(self, match: "re.Match") -> Optional[str]:
        """把 log 檔名的擷取結果轉成 case id。

        預設 submit_bjob_cmd_file_<num>.log -> case<num>。
        若 regex 直接提供 'case' group 就優先採用。
        """
        groups = match.groupdict()
        if groups.get("case"):
            return groups["case"]
        if groups.get("num") is not None:
            try:
                return self.layout.log_case_template.format(**groups)
            except (KeyError, IndexError):
                return None
        return None

    # ------------------------------------------------------------------
    # 尋找 index run folder
    # ------------------------------------------------------------------

    def list_index_run_folders(self, wave_dir: str) -> List[Tuple[str, str]]:
        """列出 wave 目錄底下由 Arcx 建立的 index run folder。

        回傳 [(index_key, abs_path), ...]。跳過我們自己的 .arcx_auto/
        與其他隱藏目錄。
        """
        wave_dir = os.path.abspath(os.path.expanduser(wave_dir))
        result: List[Tuple[str, str]] = []
        if not os.path.isdir(wave_dir):
            return result
        try:
            entries = list(os.scandir(wave_dir))
        except OSError:
            return result
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            result.append((entry.name, entry.path))
        return sorted(result)

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    def stat_file(self, path: Optional[str]) -> Tuple[Optional[int], Optional[float]]:
        """回傳 (size, mtime)。取不到就回 (None, None), 不丟例外。"""
        if not path:
            return (None, None)
        try:
            st = os.stat(path)
        except OSError:
            return (None, None)
        return (st.st_size, st.st_mtime)

    def read_head(self, path: str, nbytes: int) -> str:
        """讀檔案開頭。用來從 log 前幾行推斷 case 的執行路徑。

        errors='replace' —— EDA tool 的 log 常混雜非 UTF-8 位元組,
        絕不能因為編碼問題讓整個監控掛掉。
        """
        try:
            with open(path, "rb") as handle:
                raw = handle.read(nbytes)
        except OSError:
            return ""
        return raw.decode("utf-8", errors="replace")

    def count_gds(self, index_path: str) -> Tuple[int, List[str]]:
        """數 index path 底下的 GDS 檔數 (= case 數)。

        回傳 (count, sample_names)。同樣用單次 scandir, 不用 glob ——
        glob 會對每個 pattern 各掃一次目錄。
        """
        index_path = os.path.abspath(os.path.expanduser(index_path))
        if not os.path.isdir(index_path):
            return (0, [])
        import fnmatch

        names: List[str] = []
        try:
            entries = list(os.scandir(index_path))
        except OSError:
            return (0, [])
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            for pattern in self.layout.gds_globs:
                if fnmatch.fnmatch(entry.name, pattern):
                    names.append(entry.name)
                    break
        names.sort()
        return (len(names), names)

    def disk_free_ratio(self, path: str) -> Optional[float]:
        """回傳剩餘空間比例 (0.0 ~ 1.0)。取不到回 None。"""
        try:
            st = os.statvfs(os.path.expanduser(path))
        except OSError:
            return None
        if st.f_blocks == 0:
            return None
        return float(st.f_bavail) / float(st.f_blocks)


def _case_sort_key(case_id: str) -> Tuple[int, str]:
    """case1, case2, ..., case10 依數值排序而非字串排序。"""
    match = re.search(r"(\d+)$", case_id)
    if match:
        return (int(match.group(1)), case_id)
    return (10 ** 9, case_id)
