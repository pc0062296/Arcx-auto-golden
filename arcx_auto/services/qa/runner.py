"""Run the QA checks across one whole index run folder.

Deciding which group runs when:

    LIVE  every case, every scan (cheap; uses observations we already have)
    POST  once, after a case reaches COMPLETED_MARKER (reads the run dir)

POST results are cached by (case_id, attempt) because they cannot change --
except through a rerun, which increments the attempt.
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
from arcx_auto.services.qa.context import (
    CaseContext,
    ConfigContext,
    IndexContext,
    PreflightContext,
    _FsCache,
)
from arcx_auto.services.qa.registry import REGISTRY, QaRegistry


@dataclass(frozen=True)
class IndexQaReport:
    """The complete QA result for one index run folder."""

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
        """Merge a case's LIVE and POST results for the completeness decision."""
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
    """Runs the QA checks."""

    #: States meaning "Arcx considers this case finished", worth a POST pass
    POST_STATES = (CaseState.COMPLETED_MARKER, CaseState.DONE, CaseState.FAILED)

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[QaRegistry] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.registry = registry or REGISTRY
        self.disabled = tuple(getattr(self.settings.qa, "disabled_checks", ()) or ())
        # (run_folder, case_id, attempt) -> POST result; stable until a rerun
        self._post_cache: Dict[Tuple[str, str, int], QaResult] = {}

    def run_config(
        self,
        arcx_config: Optional[ArcxConfig],
        now: Optional[float] = None,
    ) -> QaResult:
        """PRE: check arcx.cfg only, with no run folder involved."""
        now = now if now is not None else time.time()
        context = ConfigContext(
            config=arcx_config, settings=self.settings,
            cache=_FsCache(), now=now,
        )
        target = arcx_config.source_path if arcx_config else "(no arcx.cfg)"
        return self.registry.run(
            context, IssueScope.GLOBAL, IssueStage.PRE, target,
            disabled=self.disabled,
        )

    def run_preflight(
        self,
        plan,
        run_dir: str,
        arcx_config=None,
        lsf=None,
        run_root: Optional[str] = None,
        now: Optional[float] = None,
    ) -> QaResult:
        """PRE: can this batch go out right now?"""
        now = now if now is not None else time.time()
        context = PreflightContext(
            plan=plan, run_dir=run_dir, settings=self.settings,
            arcx_config=arcx_config, cache=_FsCache(), now=now,
            lsf=lsf, run_root=run_root,
        )
        return self.registry.run(
            context, IssueScope.WAVE, IssueStage.PRE, run_dir,
            disabled=self.disabled,
        )

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
        # A new attempt means the run dir was rebuilt, so every earlier verdict
        # for this case describes files that have been moved aside. Drop them
        # rather than let a long-lived daemon accumulate them for the life of
        # the process.
        for stale in [k for k in self._post_cache
                      if k[0] == run_folder and k[1] == case_id]:
            del self._post_cache[stale]
        result = self.registry.run(
            context, IssueScope.CASE, IssueStage.POST, case_id,
            disabled=self.disabled, attempt=attempt,
        )
        self._post_cache[key] = result
        return result
