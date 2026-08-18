"""LSF adapter。

設計要點:
  * **批次查詢**。bjobs 一次拿回帳號所有 job, 在記憶體裡過濾。
    絕不 per-job 呼叫 —— 幾百次 bjobs 會打爆 LSF master。
  * **優雅降級**。開發機或 LSF 暫時不可用時, 所有查詢回傳 None/空集合
    並記錄原因, 而不是丟例外。監控系統不能因為 bjobs 抽風就整個停擺。
  * 所有外部指令都有 timeout。

TODO(待確認): bjobs_manage.py -jp 的實際輸出格式尚未取得,
目前用「抓出所有看起來像 job id 的數字」的寬鬆解析, 並保留 raw 輸出。
拿到真實輸出後改成精確解析。
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from arcx_auto.config.settings import LsfSettings
from arcx_auto.domain.enums import LsfState
from arcx_auto.domain.models import LsfJobView

_JOB_ID_RE = re.compile(r"\b(\d{3,})\b")

# bjobs -o 的欄位順序, 與 _parse_bjobs 一一對應
_BJOBS_FIELDS = ["jobid", "stat", "exec_cwd", "sub_cwd", "output_file",
                 "exec_host", "job_name"]


class LsfUnavailable(Exception):
    """LSF 指令不存在或無法執行。"""


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    error: Optional[str] = None


class LsfAdapter:
    """封裝所有 LSF 存取。"""

    def __init__(
        self,
        settings: Optional[LsfSettings] = None,
        user: Optional[str] = None,
    ) -> None:
        self.settings = settings or LsfSettings()
        self.user = user or _current_user()
        self._availability: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------

    def is_available(self, command: Optional[str] = None) -> bool:
        """該指令是否存在於 PATH。結果會快取, 避免每個 tick 重查。"""
        cmd = command or self.settings.bjobs_cmd
        if cmd not in self._availability:
            self._availability[cmd] = shutil.which(cmd) is not None
        return self._availability[cmd]

    def _run(self, argv: List[str]) -> CommandResult:
        if not self.is_available(argv[0]):
            return CommandResult(
                ok=False, stdout="", stderr="", returncode=127,
                error="找不到指令: %s" % argv[0],
            )
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.settings.command_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                ok=False, stdout="", stderr="", returncode=-1,
                error="指令逾時 (%.0fs): %s"
                      % (self.settings.command_timeout_sec, " ".join(argv)),
            )
        except OSError as exc:
            return CommandResult(
                ok=False, stdout="", stderr="", returncode=-1,
                error="指令執行失敗: %s" % exc,
            )
        return CommandResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
            returncode=proc.returncode,
        )

    # ------------------------------------------------------------------
    # bjobs
    # ------------------------------------------------------------------

    def list_user_jobs(self) -> Tuple[List[LsfJobView], Optional[str]]:
        """一次取回本帳號所有 job。回傳 (jobs, error)。

        exec_cwd / sub_cwd / output_file 是把 job 對回 wave 目錄的依據 ——
        Arcx 送出的子 job 沒有可辨識的 job name (使用者已確認),
        因此只能靠路徑歸屬。
        """
        argv = [
            self.settings.bjobs_cmd,
            "-u", self.user,
            "-o", " ".join(_BJOBS_FIELDS),
            "-noheader",
        ]
        result = self._run(argv)
        if not result.ok:
            # 沒有 job 時 bjobs 會以非 0 結束並在 stderr 印 "No unfinished job found"
            if "No unfinished job found" in (result.stderr + result.stdout):
                return ([], None)
            return ([], result.error or result.stderr.strip() or "bjobs 執行失敗")
        return (self._parse_bjobs(result.stdout), None)

    @staticmethod
    def _parse_bjobs(text: str) -> List[LsfJobView]:
        jobs: List[LsfJobView] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            padded = parts + ["-"] * (len(_BJOBS_FIELDS) - len(parts))
            values = dict(zip(_BJOBS_FIELDS, padded))
            try:
                state = LsfState(values["stat"].upper())
            except ValueError:
                state = LsfState.UNKWN
            jobs.append(
                LsfJobView(
                    job_id=values["jobid"],
                    state=state,
                    exec_cwd=_none_if_dash(values["exec_cwd"]),
                    sub_cwd=_none_if_dash(values["sub_cwd"]),
                    output_file=_none_if_dash(values["output_file"]),
                    exec_host=_none_if_dash(values["exec_host"]),
                    job_name=_none_if_dash(values["job_name"]),
                )
            )
        return jobs

    def jobs_under_path(self, path: str) -> Tuple[List[LsfJobView], Optional[str]]:
        """本帳號 job 中屬於某路徑前綴的部分 (由 bjobs 結果過濾)。"""
        jobs, error = self.list_user_jobs()
        if error:
            return ([], error)
        prefix = os.path.abspath(os.path.expanduser(path))
        return ([j for j in jobs if j.belongs_to(prefix)], None)

    # ------------------------------------------------------------------
    # busers  (提交閘門的 quota 來源)
    # ------------------------------------------------------------------

    def current_njobs(self) -> Tuple[Optional[int], Optional[str]]:
        """讀 busers 的 NJOBS 欄位 —— 提交閘門的判斷依據。

        busers 輸出範例:
            USER/GROUP   JL/P  MAX  NJOBS  PEND  RUN  SSUSP  USUSP  RSV
            myuser          -    -     12     3    9      0      0    0
        """
        result = self._run([self.settings.busers_cmd, self.user])
        if not result.ok:
            return (None, result.error or result.stderr.strip() or "busers 執行失敗")

        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if len(lines) < 2:
            return (None, "busers 輸出無法解析")

        header = lines[0].split()
        try:
            njobs_idx = header.index("NJOBS")
        except ValueError:
            return (None, "busers 輸出中找不到 NJOBS 欄位")

        for line in lines[1:]:
            parts = line.split()
            if len(parts) <= njobs_idx:
                continue
            try:
                return (int(parts[njobs_idx]), None)
            except ValueError:
                continue
        return (None, "busers 輸出中找不到本帳號的資料列")

    # ------------------------------------------------------------------
    # bjobs_manage.py  (依路徑列出 / 刪除 job)
    # ------------------------------------------------------------------

    def list_jobs_under_path_native(
        self, path: str
    ) -> Tuple[Optional[List[str]], str, Optional[str]]:
        """用 bjobs_manage.py -jp 列出目標路徑底下的 job id。

        回傳 (job_ids, raw_output, error)。這是 drain 安全門
        (VERIFY_QUIESCENT) 的主要判斷依據。
        """
        argv = [
            self.settings.bjobs_manage_cmd,
            self.settings.bjobs_manage_list_flag,
            _trailing_slash(path),
        ]
        result = self._run(argv)
        if not result.ok:
            return (None, result.stdout,
                    result.error or result.stderr.strip() or "bjobs_manage.py 執行失敗")
        return (_extract_job_ids(result.stdout), result.stdout, None)

    def kill_jobs_under_path(
        self, path: str
    ) -> Tuple[bool, str, Optional[str]]:
        """用 bjobs_manage.py -djp 刪除目標路徑底下所有 job。

        呼叫前必須**先** bkill parent Arcx job —— 否則 parent 會補送新 job
        (architecture §9.4)。這個順序由 Remediator 保證, adapter 不隱含它。
        """
        argv = [
            self.settings.bjobs_manage_cmd,
            self.settings.bjobs_manage_delete_flag,
            _trailing_slash(path),
        ]
        result = self._run(argv)
        if not result.ok:
            return (False, result.stdout,
                    result.error or result.stderr.strip() or "bjobs_manage.py 執行失敗")
        return (True, result.stdout, None)

    def kill_job(self, job_id: str) -> Tuple[bool, Optional[str]]:
        """bkill 單一 job (用來殺 parent Arcx job)。"""
        result = self._run([self.settings.bkill_cmd, str(job_id)])
        if not result.ok:
            return (False, result.error or result.stderr.strip() or "bkill 執行失敗")
        return (True, None)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - 極少數無 pwd entry 的環境
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _none_if_dash(value: str) -> Optional[str]:
    value = (value or "").strip()
    return None if value in ("", "-") else value


def _trailing_slash(path: str) -> str:
    resolved = os.path.abspath(os.path.expanduser(path))
    return resolved.rstrip("/") + "/"


def _extract_job_ids(text: str) -> List[str]:
    """從自製工具的輸出中抓出 job id。

    寬鬆解析: 抓所有 3 位數以上的整數並去重。等拿到 bjobs_manage.py 的
    真實輸出格式後改成精確解析 (見模組 docstring 的 TODO)。
    """
    seen: List[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        for match in _JOB_ID_RE.finditer(line):
            job_id = match.group(1)
            if job_id not in seen:
                seen.append(job_id)
    return seen
