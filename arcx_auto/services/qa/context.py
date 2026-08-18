"""QA 檢查的執行環境。

設計目的: 讓一條 QA function 通常只有 3~10 行。
QA 作者不需要碰 os.path、不需要處理例外、不需要擔心重複 I/O ——
全部由 context 負責。

三個性質:
  * **有快取**: 同一個 case 被十條檢查掃過, 每個目錄只真的 scandir 一次
  * **不丟例外**: 讀不到就回 None / "" / [], 不是 crash
  * **路徑相對化**: 所有 rel path 都相對 case run dir, 檢查裡不會出現絕對路徑
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from arcx_auto.adapters.arcx_cfg import ArcxConfig
from arcx_auto.config.settings import QaSettings, Settings
from arcx_auto.domain.enums import CaseState, IssueScope, IssueStage, Severity
from arcx_auto.domain.models import CaseSnapshot, IndexRunObservation, IndexRunSnapshot
from arcx_auto.domain.qa import ExpectedArtifact, Issue
from arcx_auto.services.qa.expectations import expected_artifacts, expected_flow_dirs


class _FsCache:
    """目錄內容與 stat 的快取。一個 index 掃描週期共用一份。"""

    def __init__(self) -> None:
        self._listing: Dict[str, List[str]] = {}
        self._stat: Dict[str, Optional[os.stat_result]] = {}

    def listdir(self, path: str) -> List[str]:
        if path not in self._listing:
            try:
                self._listing[path] = sorted(os.listdir(path))
            except OSError:
                self._listing[path] = []
        return self._listing[path]

    def stat(self, path: str) -> Optional[os.stat_result]:
        if path not in self._stat:
            try:
                self._stat[path] = os.stat(path)
            except OSError:
                self._stat[path] = None
        return self._stat[path]


class _BaseContext:
    """CaseContext / IndexContext 的共用部分。"""

    def __init__(self, root: str, settings: Settings, cache: _FsCache,
                 now: float) -> None:
        self.root = os.path.abspath(root) if root else ""
        self.settings = settings
        self.qa: QaSettings = settings.qa
        self.now = now
        self._cache = cache
        # 由 registry 在呼叫每條檢查前填入, 讓 fail()/warn() 能自動帶上 id
        self._current: Dict[str, Any] = {}

    # -- 檔案存取 (全部相對 root) -------------------------------------

    def abspath(self, rel: str = "") -> str:
        return os.path.join(self.root, rel) if rel else self.root

    def exists(self, rel: str) -> bool:
        return self._cache.stat(self.abspath(rel)) is not None

    def is_dir(self, rel: str) -> bool:
        st = self._cache.stat(self.abspath(rel))
        return st is not None and os.path.isdir(self.abspath(rel))

    def size(self, rel: str) -> Optional[int]:
        st = self._cache.stat(self.abspath(rel))
        return st.st_size if st else None

    def mtime(self, rel: str) -> Optional[float]:
        st = self._cache.stat(self.abspath(rel))
        return st.st_mtime if st else None

    def listdir(self, rel: str = "") -> List[str]:
        return self._cache.listdir(self.abspath(rel))

    def glob(self, pattern: str, rel_dir: str = "") -> List[str]:
        """在某個目錄下比對檔名。回傳相對 root 的路徑。"""
        names = self._cache.listdir(self.abspath(rel_dir))
        hits = [n for n in names if fnmatch.fnmatch(n, pattern)]
        return [os.path.join(rel_dir, n) if rel_dir else n for n in sorted(hits)]

    def first_match(self, pattern: str, rel_dir: str = "") -> Optional[str]:
        hits = self.glob(pattern, rel_dir)
        return hits[0] if hits else None

    def read_text(self, rel: str, max_bytes: int = 65536) -> str:
        try:
            with open(self.abspath(rel), "rb") as handle:
                return handle.read(max_bytes).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def read_tail(self, rel: str, nbytes: int = 4096) -> str:
        path = self.abspath(rel)
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                start = max(0, handle.tell() - nbytes)
                handle.seek(start)
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def count_lines(self, rel: str, prefix: Optional[str] = None,
                    max_bytes: int = 1 << 22) -> int:
        text = self.read_text(rel, max_bytes)
        if prefix is None:
            return text.count("\n")
        return sum(1 for line in text.splitlines() if line.startswith(prefix))

    # -- 產出 Issue ---------------------------------------------------

    def _issue(self, severity: Severity, message: str,
               evidence: Optional[Dict[str, Any]] = None) -> Issue:
        meta = self._current
        return Issue(
            id=meta.get("id", "UNSPECIFIED"),
            severity=severity,
            message=message,
            scope=meta.get("scope", IssueScope.CASE),
            stage=meta.get("stage", IssueStage.POST),
            title=meta.get("title", ""),
            doc=meta.get("doc", ""),
            index_key=getattr(self, "index_key", None),
            case_id=getattr(self, "case_id", None),
            evidence=evidence or {},
        )

    def fail(self, message: str, evidence: Optional[Dict[str, Any]] = None) -> Issue:
        return self._issue(Severity.FATAL, message, evidence)

    def warn(self, message: str, evidence: Optional[Dict[str, Any]] = None) -> Issue:
        return self._issue(Severity.WARN, message, evidence)

    def info(self, message: str, evidence: Optional[Dict[str, Any]] = None) -> Issue:
        return self._issue(Severity.INFO, message, evidence)

    def unknown(self, message: str,
                evidence: Optional[Dict[str, Any]] = None) -> Issue:
        """「我檢查不了」。絕不能用 pass 代替 —— 見 Severity.UNKNOWN 的說明。"""
        return self._issue(Severity.UNKNOWN, message, evidence)

    def at(self, severity: Severity, message: str,
           evidence: Optional[Dict[str, Any]] = None) -> Issue:
        """嚴重度由檢查自己算出來時使用 (例如依安靜時間分級)。"""
        return self._issue(severity, message, evidence)


class CaseContext(_BaseContext):
    """單一 case 的檢查環境。root = case run dir。"""

    def __init__(
        self,
        case: CaseSnapshot,
        index_key: str,
        run_folder: str,
        settings: Settings,
        config: Optional[ArcxConfig],
        cache: _FsCache,
        now: float,
        attempt: int = 1,
    ) -> None:
        root = case.case_dir or os.path.join(run_folder, case.case_id)
        super().__init__(root, settings, cache, now)
        self.case = case
        self.case_id = case.case_id
        self.index_key = index_key
        self.run_folder = os.path.abspath(run_folder)
        self.attempt = attempt
        self.arcx_config = config
        self._expected: Optional[Tuple[Tuple[ExpectedArtifact, ...],
                                       Tuple[str, ...]]] = None

    # -- 便捷屬性 -----------------------------------------------------

    @property
    def state(self) -> CaseState:
        return self.case.state

    @property
    def case_dir_exists(self) -> bool:
        return self.is_dir("")

    @property
    def silent_for(self) -> float:
        """log 已經多久沒有成長 (秒)。"""
        return self.case.silent_for(self.now)

    @property
    def expected_artifacts(self) -> Tuple[ExpectedArtifact, ...]:
        """從 arcx.cfg 推導出的期望產出物。cfg 不可用時回空 tuple。"""
        return self._expectations()[0]

    @property
    def expectation_problems(self) -> Tuple[str, ...]:
        """cfg 本身的問題 (block 沒 QC_FLOW、flow 不認得)。"""
        return self._expectations()[1]

    @property
    def expected_flow_dirs(self) -> Tuple[str, ...]:
        if self.arcx_config is None:
            return ()
        return expected_flow_dirs(self.arcx_config)

    def missing_artifacts(self) -> List[ExpectedArtifact]:
        return [a for a in self.expected_artifacts if not self.exists(a.relpath)]

    def artifacts_ready(self) -> bool:
        """所有期望產出物都在且大小足夠。

        用來判斷「安靜但其實已經跑完了」—— 這種情況不該被當成卡住。
        cfg 不可用時回 False (不知道就不要說它好了)。
        """
        if not self.expected_artifacts:
            return False
        for artifact in self.expected_artifacts:
            size = self.size(artifact.relpath)
            if size is None or size < artifact.min_bytes:
                return False
        return True

    def _expectations(self):
        if self._expected is None:
            if self.arcx_config is None:
                self._expected = ((), ())
            else:
                self._expected = expected_artifacts(
                    self.arcx_config, self.case_id, self.qa)
        return self._expected


class ConfigContext(_BaseContext):
    """PRE 檢查的環境。root = arcx.cfg 所在目錄。

    不需要 run folder —— PRE 在提交前跑, 那時候什麼都還沒建立。
    """

    def __init__(
        self,
        config: Optional[ArcxConfig],
        settings: Settings,
        cache: _FsCache,
        now: float,
    ) -> None:
        root = os.path.dirname(config.source_path) if config else ""
        super().__init__(root, settings, cache, now)
        self.config = config
        self.index_key = None
        self.case_id = None

    @property
    def source_path(self) -> Optional[str]:
        return self.config.source_path if self.config else None


class IndexContext(_BaseContext):
    """一個 index run folder 的檢查環境。root = run folder。"""

    def __init__(
        self,
        snapshot: IndexRunSnapshot,
        observation: Optional[IndexRunObservation],
        settings: Settings,
        config: Optional[ArcxConfig],
        cache: _FsCache,
        now: float,
    ) -> None:
        super().__init__(snapshot.run_folder, settings, cache, now)
        self.snapshot = snapshot
        self.observation = observation
        self.index_key = snapshot.index_key
        self.case_id = None
        self.arcx_config = config

    @property
    def cases(self) -> Dict[str, CaseSnapshot]:
        return self.snapshot.cases
