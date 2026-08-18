"""Registering and running QA checks.

A check looks like this:

    @qa_check(id="NETLIST_MISSING", title="netlist missing",
              severity=Severity.FATAL, scope=Scope.CASE, stage=Stage.POST)
    def netlist_missing(case: CaseContext):
        '''This docstring is what the UI shows.'''
        missing = case.missing_artifacts()
        if not missing:
            return None
        return case.fail("%d artifact(s) missing" % len(missing),
                         evidence={"missing": [a.relpath for a in missing]})

Returning None or [] means the check passed; an Issue or a list of Issues means
something was found.

Three properties that matter:

  1. **Error isolation.** Engineers are meant to add their own rules, so some of
     those functions will have bugs. Each check runs inside a try; one that
     throws becomes a QA_INTERNAL_ERROR issue (with a traceback) and the rest
     still run. One bad rule must never take down the monitoring.

  2. **The id is the interface.** policy.yaml refers to ids, not to
     implementations, so rewriting a check does not touch policy. Registering
     the same id twice raises immediately -- silent shadowing is the hardest
     kind of bug to find.

  3. **Checks can be disabled** from settings (disabled_checks) without
     deleting code.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from arcx_auto.domain.enums import IssueScope, IssueStage, Severity
from arcx_auto.domain.qa import Issue, QaResult
from arcx_auto.services.qa.context import CaseContext, IndexContext, _BaseContext

CheckReturn = Union[None, Issue, Sequence[Issue]]
CheckFunc = Callable[..., CheckReturn]

INTERNAL_ERROR_ID = "QA_INTERNAL_ERROR"


@dataclass(frozen=True)
class CheckSpec:
    """One registered check."""

    id: str
    func: CheckFunc
    title: str
    severity: Severity
    scope: IssueScope
    stage: IssueStage
    doc: str = ""

    def metadata(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "scope": self.scope,
            "stage": self.stage,
            "doc": self.doc,
            "severity": self.severity,
        }


class QaRegistry:
    """Where every QA check registers itself."""

    def __init__(self) -> None:
        self._checks: Dict[str, CheckSpec] = {}

    # -- Registration --------------------------------------------------

    def register(self, spec: CheckSpec) -> None:
        if spec.id in self._checks:
            raise ValueError(
                "duplicate QA check id: %s (already registered by %s)"
                % (spec.id, self._checks[spec.id].func.__name__)
            )
        self._checks[spec.id] = spec

    def check(
        self,
        id: str,
        title: str,
        severity: Severity = Severity.FATAL,
        scope: IssueScope = IssueScope.CASE,
        stage: IssueStage = IssueStage.POST,
    ) -> Callable[[CheckFunc], CheckFunc]:
        """The decorator."""

        def decorator(func: CheckFunc) -> CheckFunc:
            self.register(CheckSpec(
                id=id, func=func, title=title, severity=severity,
                scope=scope, stage=stage,
                doc=(func.__doc__ or "").strip(),
            ))
            return func

        return decorator

    # -- Queries -------------------------------------------------------

    def all(self) -> Tuple[CheckSpec, ...]:
        return tuple(self._checks.values())

    def select(
        self,
        scope: IssueScope,
        stage: IssueStage,
        disabled: Iterable[str] = (),
    ) -> Tuple[CheckSpec, ...]:
        blocked = set(disabled)
        return tuple(
            spec for spec in self._checks.values()
            if spec.scope == scope and spec.stage == stage
            and spec.id not in blocked
        )

    # -- Execution -----------------------------------------------------

    def run(
        self,
        context: _BaseContext,
        scope: IssueScope,
        stage: IssueStage,
        target: str,
        disabled: Iterable[str] = (),
        attempt: int = 1,
    ) -> QaResult:
        """Run every check matching scope and stage, collecting issues."""
        issues: List[Issue] = []
        ran: List[str] = []
        crashed: List[str] = []

        for spec in self.select(scope, stage, disabled):
            ran.append(spec.id)
            context._current = spec.metadata()
            try:
                outcome = spec.func(context)
            except Exception:  # noqa: BLE001 - intentional, see module docstring
                crashed.append(spec.id)
                issues.append(Issue(
                    id=INTERNAL_ERROR_ID,
                    severity=Severity.UNKNOWN,
                    message="check %s raised an exception" % spec.id,
                    scope=scope,
                    stage=stage,
                    title="a QA check itself failed",
                    index_key=getattr(context, "index_key", None),
                    case_id=getattr(context, "case_id", None),
                    evidence={
                        "check_id": spec.id,
                        "traceback": traceback.format_exc(limit=8),
                    },
                    doc="A check crashed. That does not mean the case is bad, "
                        "it means the rule has a bug -- but it cannot count as "
                        "a pass either, hence UNKNOWN.",
                ))
                continue
            finally:
                context._current = {}

            issues.extend(_normalize(outcome))

        return QaResult(
            target=target,
            scope=scope,
            stage=stage,
            issues=tuple(issues),
            checked_at=context.now,
            attempt=attempt,
            checks_run=tuple(ran),
            checks_failed=tuple(crashed),
        )


def _normalize(outcome: CheckReturn) -> List[Issue]:
    if outcome is None:
        return []
    if isinstance(outcome, Issue):
        return [outcome]
    return [i for i in outcome if isinstance(i, Issue)]


# The global registry. Importing checks_*.py registers into it.
REGISTRY = QaRegistry()
qa_check = REGISTRY.check
