"""QA Registry - can this case run, is it healthy now, did it really succeed.

Three stages, one output type (Issue), one policy engine downstream:

    PRE   before submission, once   can this run at all?
    LIVE  during execution, per tick  is it healthy right now?
    POST  after .complete, once     did it really succeed?

Importing this package registers every built in check into REGISTRY.
"""

from arcx_auto.services.qa import (  # noqa: F401
    checks_case,
    checks_config,
    checks_index,
    checks_preflight,
)
from arcx_auto.services.qa.context import (
    CaseContext,
    ConfigContext,
    IndexContext,
    PreflightContext,
    _FsCache,
)
from arcx_auto.services.qa.expectations import (
    expected_artifacts,
    expected_flow_dirs,
)
from arcx_auto.services.qa.registry import (
    INTERNAL_ERROR_ID,
    CheckSpec,
    QaRegistry,
    REGISTRY,
    qa_check,
)
from arcx_auto.services.qa.runner import QaRunner

__all__ = [
    "CaseContext",
    "ConfigContext",
    "IndexContext",
    "PreflightContext",
    "CheckSpec",
    "QaRegistry",
    "QaRunner",
    "REGISTRY",
    "qa_check",
    "INTERNAL_ERROR_ID",
    "expected_artifacts",
    "expected_flow_dirs",
]
