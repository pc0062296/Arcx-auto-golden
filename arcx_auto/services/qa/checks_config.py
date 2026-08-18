"""PRE 檢查 —— arcx.cfg 的驗證。提交前跑, 不需要任何 run folder。

這一層的投資報酬率最高: 設定錯誤造成的失敗, 大部分在提交前就看得出來,
而且提交出去之後要等好幾小時才會發現同一件事。

行首旗標的語意 (使用者確認):
    1  啟用
    0  停用  ─┬─ 這一行不生效, 所以**不檢查**它引用的路徑
    #  註解  ─┘   (那些路徑根本不會被用到, 檢查只會製造假警報)
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from arcx_auto.domain.enums import IssueScope, IssueStage, Severity
from arcx_auto.domain.qa import Issue
from arcx_auto.services.qa.context import ConfigContext
from arcx_auto.services.qa.registry import qa_check

GLOBAL = IssueScope.GLOBAL
PRE = IssueStage.PRE


@qa_check(id="CFG_UNREADABLE", title="arcx.cfg 讀不到",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_unreadable(cfg: ConfigContext) -> Optional[Issue]:
    """檔案不存在或無法讀取。"""
    if cfg.config is None:
        return cfg.fail("沒有提供 arcx.cfg")
    if cfg.config.error:
        return cfg.fail(cfg.config.error, evidence={"path": cfg.config.source_path})
    return None


@qa_check(id="CFG_NO_BLOCKS", title="arcx.cfg 沒有任何 settings block",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_no_blocks(cfg: ConfigContext) -> Optional[Issue]:
    """整個檔案解析不出任何 block —— 送出去也不會做任何事。"""
    if cfg.config is None or cfg.config.error:
        return None                      # 交給 CFG_UNREADABLE
    if cfg.config.blocks:
        return None
    return cfg.fail("解析不到任何 BEGIN_SETTINGS block",
                    evidence={"path": cfg.config.source_path})


@qa_check(id="CFG_ALL_BLOCKS_DISABLED", title="所有 block 都被停用",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_all_blocks_disabled(cfg: ConfigContext) -> Optional[Issue]:
    """有 block, 但全部行首旗標是 0。

    這種 cfg 會安靜地跑完卻什麼都不產出 —— 是最浪費 TAT 的一種錯誤。
    """
    if cfg.config is None or not cfg.config.blocks:
        return None
    if cfg.config.enabled_blocks:
        return None
    return cfg.fail(
        "全部 %d 個 block 都被停用 (行首旗標 0)" % len(cfg.config.blocks),
        evidence={"blocks": [b.name for b in cfg.config.blocks]},
    )


@qa_check(id="CFG_DUPLICATE_BLOCK", title="block 名稱重複",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_duplicate_block(cfg: ConfigContext) -> Optional[Issue]:
    """兩個 block 同名。

    產出目錄是 <block>_<QC_FLOW>, 同名 block 會寫進同一個目錄互相覆蓋,
    結果是「跑完了但只剩最後一份」——而且不會有任何錯誤訊息。
    """
    if cfg.config is None:
        return None
    seen: Dict[str, int] = {}
    for block in cfg.config.blocks:
        seen[block.name] = seen.get(block.name, 0) + 1
    duplicates = {name: count for name, count in seen.items() if count > 1}
    if not duplicates:
        return None
    return cfg.fail(
        "有 %d 個重複的 block 名稱" % len(duplicates),
        evidence={"duplicates": duplicates},
    )


@qa_check(id="CFG_BLOCK_NO_FLOW", title="block 沒有設定 QC_FLOW",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_block_no_flow(cfg: ConfigContext) -> Optional[Issue]:
    """啟用中的 block 缺少 QC_FLOW —— 不知道要跑哪個 EDA tool。"""
    if cfg.config is None:
        return None
    offenders = [b.name for b in cfg.config.enabled_blocks if not b.flow]
    if not offenders:
        return None
    return cfg.fail("%d 個 block 沒有 QC_FLOW" % len(offenders),
                    evidence={"blocks": offenders})


@qa_check(id="CFG_UNKNOWN_FLOW", title="QC_FLOW 不在已知清單中",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_unknown_flow(cfg: ConfigContext) -> Optional[Issue]:
    """QC_FLOW 的值系統不認得。

    可能是 cfg 打錯字, 也可能是新增了 EDA tool 但還沒在 qa.flows 裡定義。
    無論哪種, 系統都無法驗證它的產出物 —— 所以要在提交前就講清楚。
    """
    if cfg.config is None:
        return None
    known = set(cfg.qa.flows)
    offenders = [
        {"block": b.name, "flow": b.flow}
        for b in cfg.config.enabled_blocks
        if b.flow and b.flow not in known
    ]
    if not offenders:
        return None
    return cfg.fail(
        "%d 個 block 的 QC_FLOW 不認得" % len(offenders),
        evidence={"blocks": offenders, "known_flows": sorted(known)},
    )


@qa_check(id="CFG_PATH_NOT_FOUND", title="設定引用的檔案不存在",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_path_not_found(cfg: ConfigContext) -> Optional[Issue]:
    """啟用中的設定所引用的檔案 / 目錄找不到。

    只檢查 qa.cfg_path_keys 列出的 key (RCX_TECH_QTF、LVS_DECK 等), 而且
    只檢查**啟用中**的行 —— 旗標 0 或註解掉的行不會生效, 檢查它們只會製造
    假警報, 而假警報多了就沒人看了。

    含 $ 的值 (環境變數) 無法在這裡解析, 會列為 skipped 而不是誤報。
    """
    if cfg.config is None or not cfg.qa.verify_cfg_paths:
        return None

    keys = [k for k in cfg.qa.cfg_path_keys
            if k not in set(cfg.qa.cfg_path_check_skip_keys)]
    missing: List[dict] = []
    skipped: List[dict] = []

    for block in cfg.config.enabled_blocks:
        for key in keys:
            if key in block.disabled_keys:
                continue                  # 行首旗標 0 -> 不生效, 不檢查
            value = block.settings.get(key)
            if not value:
                continue
            if "$" in value:
                skipped.append({"block": block.name, "key": key, "value": value})
                continue
            if not os.path.exists(os.path.expanduser(value)):
                missing.append({"block": block.name, "key": key, "path": value})

    if not missing:
        return None
    return cfg.fail(
        "%d 個設定引用的路徑不存在" % len(missing),
        evidence={"missing": missing, "skipped_env_vars": skipped},
    )


@qa_check(id="CFG_PARSE_WARNING", title="arcx.cfg 解析時有疑點",
          severity=Severity.WARN, scope=GLOBAL, stage=PRE)
def cfg_parse_warning(cfg: ConfigContext) -> Optional[Issue]:
    """解析器發現的疑點: 關鍵字拼錯、缺 END_SETTINGS、無法解析的行等。"""
    if cfg.config is None:
        return None
    all_warnings = list(cfg.config.warnings)
    for block in cfg.config.blocks:
        all_warnings.extend("[%s] %s" % (block.name, w) for w in block.warnings)
    if not all_warnings:
        return None
    return cfg.warn("解析時有 %d 個疑點" % len(all_warnings),
                    evidence={"warnings": all_warnings})
