"""檔案系統 adapter。

真實的 index run folder 長這樣:

    .queue.NDIO_1                  <- marker, case id 是 cell 名稱
    .run.PDIO_1
    .complete.NTN_1
    NDIO_1/  PDIO_1/  NTN_1/       <- 每個 case 的 run dir (rerun 時刪這些)
    QC_Cc/  QC_Ct/  QC_Spice/      <- Arcx 整理的 report, 不是 case
    submit_bjob_cmd_file_1.log     <- log, 檔名只有流水號
    cmd_folder/cmd_file_1          <- 送進 LSF 的 script, 內含 `cd <case run dir>`

兩個關鍵慣例:

  1. **case id 是 cell 名稱, 沒有共同樣式。**
     所以 case run dir 用排除法辨識 (不是 QC_* / cmd_folder / 隱藏目錄),
     而不是用 include pattern。

  2. **log 檔名與 case 沒有直接關係。**
     submit_bjob_cmd_file_1.log 依編號配對 cmd_folder/cmd_file_1,
     再從該 script 的 `cd <path>` 得知它屬於哪個 case。
     這是唯一可靠的對應方式 —— 編號順序不保證等於任何排序。

效能設計 (NFS 上動輒數萬個 entry):
  * 一次 os.scandir 掃完整個目錄, 在記憶體裡分類, 不做 per-case 的 exists()
  * cmd_file 只讀開頭幾 KB, 且結果快取 (script 送出後就不會再變)
  * log 只 stat (size/mtime), 不讀內容
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Set, Tuple

from arcx_auto.config.settings import LayoutSettings
from arcx_auto.domain.enums import MarkerKind
from arcx_auto.domain.models import CaseObservation, IndexRunObservation


class FsAdapter:
    """封裝所有對 run folder 的唯讀存取。"""

    def __init__(self, layout: Optional[LayoutSettings] = None) -> None:
        self.layout = layout or LayoutSettings()
        self._marker_re = re.compile(self.layout.marker_regex)
        self._log_re = re.compile(self.layout.log_regex)
        self._report_re = re.compile(self.layout.report_dir_regex)
        self._cd_re = re.compile(self.layout.cmd_cd_regex)
        self._non_case_res = [
            re.compile(pattern) for pattern in self.layout.non_case_dir_regexes
        ]
        # cmd_file 路徑 -> 解析出的執行路徑。script 送出後不會變, 只需讀一次。
        self._cmd_cache: Dict[str, Optional[str]] = {}

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

        case 的來源刻意取三者的**聯集**: marker / case run dir / cmd_file 解析出的 log。
        三者缺一都代表某種異常, 只看其中一個會漏掉:
          - 只有 dir 沒有 marker  -> case 建立了但從未被提交
          - 只有 marker 沒有 dir  -> 目錄被誤刪或尚未建立
          - 只有 log 沒有 marker  -> marker 寫入失敗
        """
        import time

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

        try:
            entries = list(os.scandir(run_folder))
        except OSError as exc:
            return IndexRunObservation(
                index_key=key,
                run_folder=run_folder,
                observed_at=now,
                error="無法讀取 run folder: %s" % exc,
            )

        markers: Dict[str, Set[MarkerKind]] = {}
        case_dirs: Dict[str, str] = {}
        logs_by_num: Dict[str, str] = {}
        report_dirs: List[str] = []
        unmatched: List[str] = []

        for entry in entries:
            name = entry.name

            marker_match = self._marker_re.match(name)
            if marker_match:
                try:
                    kind = MarkerKind(marker_match.group("kind"))
                except ValueError:
                    unmatched.append(name)
                    continue
                markers.setdefault(marker_match.group("case"), set()).add(kind)
                continue

            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False

            if is_dir:
                if self._report_re.match(name):
                    report_dirs.append(name)
                elif self._is_case_dir_name(name):
                    # case run dir 的名字就是 cell 名稱, 用排除法辨識
                    case_dirs[name] = entry.path
                # 其餘 (cmd_folder / 隱藏目錄) 是已知的非 case 目錄, 不算 unmatched
                continue

            log_match = self._log_re.match(name)
            if log_match:
                logs_by_num[log_match.group("num")] = entry.path
                continue

            unmatched.append(name)

        # log -> cmd_file -> case
        logs_by_case, cmd_by_case, exec_by_case, unresolved = self._resolve_logs(
            run_folder, logs_by_num
        )

        all_case_ids = sorted(
            set(markers) | set(case_dirs) | set(logs_by_case), key=natural_key
        )

        cases: Dict[str, CaseObservation] = {}
        for case_id in all_case_ids:
            log_path = logs_by_case.get(case_id)
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
                cmd_file=cmd_by_case.get(case_id),
                exec_path=exec_by_case.get(case_id),
            )

        return IndexRunObservation(
            index_key=key,
            run_folder=run_folder,
            observed_at=now,
            cases=cases,
            report_dirs=tuple(sorted(report_dirs)),
            unmatched_entries=tuple(sorted(unmatched)),
            unresolved_logs=tuple(sorted(unresolved)),
        )

    def _is_case_dir_name(self, name: str) -> bool:
        """排除法: 不是 report / cmd_folder / 隱藏目錄的, 就當作 case run dir。

        用排除法而非 include pattern, 是因為 case id 是 cell 名稱
        (NDIO_1 / PDIO_1 / NTN_1 ...), 沒有共同樣式可比對。
        代價是新增的非 case 目錄會被誤認 —— 所以排除清單放在設定裡可擴充。
        """
        return not any(pattern.match(name) for pattern in self._non_case_res)

    # ------------------------------------------------------------------
    # log -> cmd_file -> case
    # ------------------------------------------------------------------

    def _resolve_logs(
        self, run_folder: str, logs_by_num: Dict[str, str]
    ) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str], List[str]]:
        """把每個 log 依編號配對 cmd_file, 再解析出它屬於哪個 case。

        回傳 (logs_by_case, cmd_by_case, exec_by_case, unresolved_logs)。
        """
        logs_by_case: Dict[str, str] = {}
        cmd_by_case: Dict[str, str] = {}
        exec_by_case: Dict[str, str] = {}
        unresolved: List[str] = []

        cmd_dir = os.path.join(run_folder, self.layout.cmd_dir_name)

        for num, log_path in logs_by_num.items():
            cmd_name = self.layout.cmd_file_template.format(num=num)
            cmd_path = os.path.join(cmd_dir, cmd_name)

            exec_path = self.read_cmd_exec_path(cmd_path, run_folder)
            if not exec_path:
                unresolved.append(os.path.basename(log_path))
                continue

            case_id = os.path.basename(exec_path.rstrip("/"))
            if not case_id:
                unresolved.append(os.path.basename(log_path))
                continue

            logs_by_case[case_id] = log_path
            cmd_by_case[case_id] = cmd_path
            exec_by_case[case_id] = exec_path

        return logs_by_case, cmd_by_case, exec_by_case, unresolved

    def read_cmd_exec_path(
        self, cmd_path: str, run_folder: Optional[str] = None
    ) -> Optional[str]:
        """從 cmd_file script 讀出它 cd 進去的執行路徑。

            #!/bin/csh -f
            source xxxx
            cd /path/to/index/NDIO_1
            ...

        script 裡可能有多個 cd。優先採用**位於 run folder 底下**的那一個
        (那才是 case run dir); 找不到時退回最後一個絕對路徑的 cd。
        結果快取 —— script 送出後就不會再變。
        """
        cmd_path = os.path.abspath(cmd_path)
        cache_key = "%s|%s" % (cmd_path, run_folder or "")
        if cache_key in self._cmd_cache:
            return self._cmd_cache[cache_key]

        text = self.read_head(cmd_path, self.layout.cmd_file_head_bytes)
        best: Optional[str] = None
        last_absolute: Optional[str] = None

        prefix = None
        if run_folder:
            prefix = os.path.abspath(run_folder).rstrip("/") + "/"

        for line in text.splitlines():
            match = self._cd_re.match(line)
            if not match:
                continue
            path = match.group("path").rstrip("/")
            if not path.startswith("/"):
                continue
            last_absolute = path
            if prefix and (path + "/").startswith(prefix):
                best = path

        result = best or last_absolute
        self._cmd_cache[cache_key] = result
        return result

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
        """讀檔案開頭。

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


_NATURAL_RE = re.compile(r"(\d+)")


def natural_key(name: str) -> Tuple:
    """自然排序: NDIO_1 < NDIO_2 < NDIO_10。

    case id 是 cell 名稱, 純字串排序會把 NDIO_10 排在 NDIO_2 前面,
    在 UI 上看起來像資料錯亂。
    """
    parts = _NATURAL_RE.split(name)
    key: List[Tuple[int, object]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        elif part:
            key.append((1, part))
    return tuple(key)
