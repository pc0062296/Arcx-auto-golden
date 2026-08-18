"""把 QA 檢查跑在一整個 index run folder 上。

決定「什麼時候跑哪一組檢查」:

    LIVE  每個 case 每次掃描都跑 (成本低, 只用已有的觀測)
    POST  只在 case 走到 COMPLETED_MARKER 之後跑一次 (要進 run dir 讀檔)

POST 的結果會被快取 (key = case_id + attempt), 因為它不會再變 ——
除非 rerun, 而 rerun 會讓 attempt 加一。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from arcx_auto.adapters.arcx_cfg import ArcxConfig
from arcx_auto.config.settings import Settings
from arcx_auto.domain.enums import CaseState, IssueScope, IssueStage
from arcx_auto.domain.models import IndexRunObservation, IndexRunSnapshot
from arcx_auto.domain.qa import Issue, QaResult
from arcx_auto.services.qa.context import CaseContext, IndexContext, _FsCache
from arcx_auto.services.qa.registry import REGISTRY, QaRegistry


@dataclass(frozen=True)
class IndexQaReport:
    """一個 index run folder 的完整 QA 結果。"""

    index_key: str
    run_folder: str
    checked_at: float
    case_results: Dict[str, Tuple[QaResult, ...]] = field(default_factory=dict)
    index_results: Tuple[QaResult, ...] = ()

    def issues_for(self, case_id: str) -> Tuple[Issue, ...]:
        issues: List[Issue] = []
        for result in self.case_results.get(case_id, ()):
            issues.extend(result.issues)
        return tuple(issues)

    def all_issues(self) -> Tuple[Issue, ...]:
        issues: List[Issue] = []
        for results in self.case_results.values():
            for result in results:
                issues.extend(result.issues)
        for result in self.index_results:
            issues.extend(result.issues)
        return tuple(issues)

    def merged_case_result(self, case_id: str) -> QaResult:
        """把一個 case 的 LIVE + POST 結果合併成一份, 方便判定完整性。"""
        results = self.case_results.get(case_id, ())
        issues: List[Issue] = []
        ran: List[str] = []
        crashed: List[str] = []
        attempt = 1
        for result in results:
            issues.extend(result.issues)
            ran.extend(result.checks_run)
            crashed.extend(result.checks_failed)
            attempt = max(attempt, result.attempt)
        return QaResult(
            target=case_id,
            scope=IssueScope.CASE,
            issues=tuple(issues),
            checked_at=self.checked_at,
            attempt=attempt,
            checks_run=tuple(ran),
            checks_failed=tuple(crashed),
        )


class QaRunner:
    """執行 QA 檢查。"""

    #: 哪些狀態代表「Arcx 認為這個 case 收工了」, 值得跑 POST 檢查
    POST_STATES = (CaseState.COMPLETED_MARKER, CaseState.DONE, CaseState.FAILED)

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[QaRegistry] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.registry = registry or REGISTRY
        self.disabled = tuple(getattr(self.settings.qa, "disabled_checks", ()) or ())
        # (case_id, attempt) -> POST 結果。POST 不會變, 除非 rerun。
        self._post_cache: Dict[Tuple[str, str, int], QaResult] = {}

    def run_index(
        self,
        snapshot: IndexRunSnapshot,
        observation: Optional[IndexRunObservation] = None,
        arcx_config: Optional[ArcxConfig] = None,
        now: Optional[float] = None,
        attempts: Optional[Dict[str, int]] = None,
    ) -> IndexQaReport:
        now = now if now is not None else time.time()
        attempts = attempts or {}
        cache = _FsCache()

        case_results: Dict[str, Tuple[QaResult, ...]] = {}
        for case_id, case_snapshot in snapshot.cases.items():
            attempt = attempts.get(case_id, 1)
            context = CaseContext(
                case=case_snapshot,
                index_key=snapshot.index_key,
                run_folder=snapshot.run_folder,
                settings=self.settings,
                config=arcx_config,
                cache=cache,
                now=now,
                attempt=attempt,
            )
            results: List[QaResult] = [self.registry.run(
                context, IssueScope.CASE, IssueStage.LIVE, case_id,
                disabled=self.disabled, attempt=attempt,
            )]
            if case_snapshot.state in self.POST_STATES:
                results.append(self._run_post(context, case_id, attempt,
                                              snapshot.run_folder))
            case_results[case_id] = tuple(results)

        index_context = IndexContext(
            snapshot=snapshot,
            observation=observation,
            settings=self.settings,
            config=arcx_config,
            cache=cache,
            now=now,
        )
        index_results = (
            self.registry.run(index_context, IssueScope.INDEX, IssueStage.LIVE,
                              snapshot.index_key, disabled=self.disabled),
            self.registry.run(index_context, IssueScope.INDEX, IssueStage.POST,
                              snapshot.index_key, disabled=self.disabled),
        )

        return IndexQaReport(
            index_key=snapshot.index_key,
            run_folder=snapshot.run_folder,
            checked_at=now,
            case_results=case_results,
            index_results=index_results,
        )

    def _run_post(self, context: CaseContext, case_id: str, attempt: int,
                  run_folder: str) -> QaResult:
        key = (run_folder, case_id, attempt)
        cached = self._post_cache.get(key)
        if cached is not None:
            return cached
        result = self.registry.run(
            context, IssueScope.CASE, IssueStage.POST, case_id,
            disabled=self.disabled, attempt=attempt,
        )
        self._post_cache[key] = result
        return result
