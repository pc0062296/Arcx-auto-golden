"""QA Registry —— 判斷「這個 case 能不能跑 / 現在健康嗎 / 真的成功了嗎」。

三個 stage, 同一套輸出 (Issue), 交給同一套 Policy 決定動作:

    PRE   提交前, 一次      這個 case 能不能跑?
    LIVE  執行中, 每個 tick  它現在健康嗎?    (卡住 / suspended / 監控不到)
    POST  .complete 後, 一次 它真的成功了嗎?  (產出物 / 完整性)

匯入這個 package 就會把所有內建檢查註冊進 REGISTRY。
"""

# 匯入即註冊 —— 順序不重要, id 重複會直接報錯
from arcx_auto.services.qa import (  # noqa: F401
    checks_case,
    checks_config,
    checks_index,
)
from arcx_auto.services.qa.context import (
    CaseContext,
    ConfigContext,
    IndexContext,
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
    "CheckSpec",
    "QaRegistry",
    "QaRunner",
    "REGISTRY",
    "qa_check",
    "INTERNAL_ERROR_ID",
    "expected_artifacts",
    "expected_flow_dirs",
]
