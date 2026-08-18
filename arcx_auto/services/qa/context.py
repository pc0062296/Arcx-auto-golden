"""The execution environment for a QA check.

The goal is that a QA function is usually three to ten lines. Its author
never touches os.path, never handles exceptions, and never worries about
repeating I/O -- the context takes care of all of it.

Three properties:
  * **cached**: ten checks over one case still scandir each directory once
  * **never raises**: unreadable means None / "" / [], not a crash
  * **relative paths**: every rel path is relative to the case run dir, so
    absolute paths never appear inside a check
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from arcx_auto.adapters.arcx_cfg import ArcxConfig
from arcx_auto.config.settings import QaSettings, Settings
from arcx_auto.domain.enums import CaseState, IssueScope, IssueStage, Severity
from arcx_auto.domain.models import (
    CaseSnapshot,
    IndexRunObservation,
    IndexRunSnapshot,
    WavePlan,
)
from arcx_auto.domain.qa import ExpectedArtifact, Issue
from arcx_auto.services.qa.expectations import expected_artifacts, expected_flow_dirs


class _FsCache:
    """Directory listing and stat cache, shared across one scan of an index."""

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
    """What CaseContext and IndexContext have in common."""

    def __init__(self, root: str, settings: Settings, cache: _FsCache,
                 now: float) -> None:
        self.root = os.path.abspath(root) if root else ""
        self.settings = settings
        self.qa: QaSettings = settings.qa
        self.now = now
        self._cache = cache
        # Filled in by the registry before each check so that fail()/warn()
        # can carry the right id automatically
        self._current: Dict[str, Any] = {}

    # -- File access (everything relative to root) ---------------------

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
        """Match filenames in a directory. Returns paths relative to root."""
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

    # -- Producing issues ----------------------------------------------

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
        """"I could not check". Never substitute a pass -- see Severity.UNKNOWN."""
        return self._issue(Severity.UNKNOWN, message, evidence)

    def at(self, severity: Severity, message: str,
           evidence: Optional[Dict[str, Any]] = None) -> Issue:
        """Used when the check computes its own severity, e.g. by quiet time."""
        return self._issue(severity, message, evidence)


class CaseContext(_BaseContext):
    """Check environment for one case. root is the case run dir."""

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

    # -- Convenience ----------------------------------------------------

    @property
    def state(self) -> CaseState:
        return self.case.state

    @property
    def case_dir_exists(self) -> bool:
        return self.is_dir("")

    @property
    def silent_for(self) -> float:
        """How long the log has not grown, in seconds."""
        return self.case.silent_for(self.now)

    @property
    def expected_artifacts(self) -> Tuple[ExpectedArtifact, ...]:
        """Artifacts derived from arcx.cfg; empty when the cfg is unavailable."""
        return self._expectations()[0]

    @property
    def expectation_problems(self) -> Tuple[str, ...]:
        """Faults in the cfg itself (no QC_FLOW, unknown flow)."""
        return self._expectations()[1]

    @property
    def expected_flow_dirs(self) -> Tuple[str, ...]:
        if self.arcx_config is None:
            return ()
        return expected_flow_dirs(self.arcx_config)

    def missing_artifacts(self) -> List[ExpectedArtifact]:
        return [a for a in self.expected_artifacts if not self.exists(a.relpath)]

    def artifacts_ready(self) -> bool:
        """Whether every expected artifact exists and is large enough.

        Used to recognise "quiet but actually finished", which should not be
        treated as stuck. Returns False when the cfg is unavailable: not
        knowing is not the same as being fine.
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
    """Environment for PRE checks. root is the directory holding arcx.cfg.

    No run folder is needed: PRE runs before submission, when nothing exists.
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


class PreflightContext(_BaseContext):
    """Environment for the pre-submission checks.

    root is this run's target directory, which does not exist yet.
    It does not look at run folders (there are none yet) but at where things
    are about to be created, and at external resources: disk, LSF, existing
    waves.
    """

    def __init__(
        self,
        plan: "WavePlan",
        run_dir: str,
        settings: Settings,
        arcx_config: Optional[ArcxConfig],
        cache: _FsCache,
        now: float,
        lsf: Optional[object] = None,
        run_root: Optional[str] = None,
    ) -> None:
        super().__init__(run_dir, settings, cache, now)
        self.plan = plan
        self.run_dir = os.path.abspath(run_dir)
        self.run_root = os.path.abspath(run_root or os.path.dirname(self.run_dir))
        self.arcx_config = arcx_config
        self.lsf = lsf
        self.index_key = None
        self.case_id = None
        self._njobs_cached = False
        self._njobs: Optional[int] = None

    # -- External resources ---------------------------------------------

    def disk_free_ratio(self) -> Optional[float]:
        """Free space ratio on the target filesystem.

        Walks up to the first existing ancestor when the directory does not
        exist yet: statvfs wants a mount point, not the final path.
        """
        path = self.run_dir
        while path and not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent
        try:
            st = os.statvfs(path)
        except OSError:
            return None
        if st.f_blocks == 0:
            return None
        return float(st.f_bavail) / float(st.f_blocks)

    def current_njobs(self) -> Optional[int]:
        """The account's current NJOBS. Queried once and cached."""
        if self._njobs_cached:
            return self._njobs
        self._njobs_cached = True
        if self.lsf is None:
            return None
        value, _error = self.lsf.current_njobs()
        self._njobs = value
        return value

    def existing_index_usage(self) -> List[Dict[str, Any]]:
        """Scan existing wave manifests for indices this run wants to use."""
        wanted = {
            spec.index_key
            for wave in self.plan.waves
            for spec in wave.indices
        }
        if not wanted or not os.path.isdir(self.run_root):
            return []

        import json

        conflicts: List[Dict[str, Any]] = []
        for run_name in sorted(os.listdir(self.run_root)):
            run_path = os.path.join(self.run_root, run_name)
            if not os.path.isdir(run_path) or run_path == self.run_dir:
                continue
            for wave_name in sorted(os.listdir(run_path)):
                manifest = os.path.join(
                    run_path, wave_name, ".arcx_auto", "manifest.json")
                try:
                    with open(manifest, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except (OSError, ValueError):
                    continue
                shared = wanted & set(data.get("index_keys") or [])
                for index_key in sorted(shared):
                    conflicts.append({
                        "index": index_key,
                        "wave_dir": os.path.join(run_path, wave_name),
                        "submitted_at": data.get("created_at"),
                    })
        return conflicts


class IndexContext(_BaseContext):
    """Check environment for one index run folder. root is the run folder."""

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
