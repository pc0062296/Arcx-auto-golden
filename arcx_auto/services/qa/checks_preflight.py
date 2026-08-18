"""提交前的整體檢查 (scope=WAVE, stage=PRE)。

arcx.cfg 本身的檢查在 checks_config.py; 這裡檢查的是「這批工作現在送得出去嗎」——
目標目錄、磁碟、LSF、quota、與既有工作的衝突。

原則: **有 FATAL 就不讓提交。** 送出去要等好幾小時才會發現同一件事,
而且中途失敗還會留下半成品要清理。
"""

from __future__ import annotations

import os
from typing import List, Optional

from arcx_auto.domain.enums import IssueScope, IssueStage, Severity
from arcx_auto.domain.qa import Issue
from arcx_auto.services.qa.context import PreflightContext
from arcx_auto.services.qa.registry import qa_check

WAVE = IssueScope.WAVE
PRE = IssueStage.PRE


@qa_check(id="PREFLIGHT_NO_WAVES", title="沒有可以提交的 wave",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def no_waves(ctx: PreflightContext) -> Optional[Issue]:
    """分波之後一個 wave 都不剩 —— 選到的 index 全部不可用。"""
    if ctx.plan.waves:
        return None
    return ctx.fail(
        "沒有任何可提交的 wave",
        evidence={
            "excluded": [
                {"index": s.index_key, "reason": s.error}
                for s in ctx.plan.excluded
            ],
        },
    )


@qa_check(id="PREFLIGHT_INDEX_EXCLUDED", title="部分 index 被排除",
          severity=Severity.WARN, scope=WAVE, stage=PRE)
def index_excluded(ctx: PreflightContext) -> Optional[Issue]:
    """有 index 因為資料不完整而排不進 wave。

    不擋提交 —— 其他 index 照跑比較有價值 —— 但一定要讓人知道少了什麼,
    否則收工時會以為全部都跑過了。
    """
    if not ctx.plan.excluded:
        return None
    return ctx.warn(
        "%d 個 index 不會被執行" % len(ctx.plan.excluded),
        evidence={
            "excluded": [
                {"index": s.index_key, "path": s.path, "reason": s.error}
                for s in ctx.plan.excluded
            ],
        },
    )


@qa_check(id="PREFLIGHT_TARGET_EXISTS", title="目標目錄已存在且非空",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def target_exists(ctx: PreflightContext) -> Optional[Issue]:
    """要建立的 wave 目錄已經有東西了。

    不可妥協的原則之一是絕不覆蓋既有結果。要重跑請走 rerun 流程 ——
    那條路會先備份現場再清理, 而直接覆寫會讓失敗證據永遠消失。
    """
    occupied = []
    for wave in ctx.plan.waves:
        path = os.path.join(ctx.run_dir, wave.name)
        if os.path.isdir(path) and os.listdir(path):
            occupied.append(path)
    if not occupied:
        return None
    return ctx.fail(
        "%d 個目標目錄已存在且非空" % len(occupied),
        evidence={"paths": occupied,
                  "hint": "換一個 --run-id, 或走 rerun 流程"},
    )


@qa_check(id="PREFLIGHT_DISK_LOW", title="磁碟空間不足",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def disk_low(ctx: PreflightContext) -> Optional[Issue]:
    """目標檔案系統快滿了。

    磁碟爆掉是 RC extraction 的頭號隱形殺手 —— job 會在跑到一半時死掉,
    而且是以看不出原因的方式死。提交前擋下來比事後除錯便宜太多。
    """
    ratio = ctx.disk_free_ratio()
    if ratio is None:
        return None
    settings = ctx.settings.preflight
    if ratio < settings.min_disk_free_ratio:
        return ctx.fail(
            "剩餘空間只有 %.1f%%, 低於門檻 %.1f%%"
            % (ratio * 100, settings.min_disk_free_ratio * 100),
            evidence={"path": ctx.run_dir, "free_ratio": round(ratio, 4)},
        )
    if ratio < settings.warn_disk_free_ratio:
        return ctx.warn(
            "剩餘空間 %.1f%%, 接近門檻" % (ratio * 100),
            evidence={"path": ctx.run_dir, "free_ratio": round(ratio, 4)},
        )
    return None


@qa_check(id="PREFLIGHT_LSF_UNAVAILABLE", title="LSF 指令不可用",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def lsf_unavailable(ctx: PreflightContext) -> Optional[Issue]:
    """bsub 找不到。送出去也不會有任何事發生。"""
    if ctx.lsf is None:
        return None
    missing = [
        name for name in (ctx.settings.lsf.bsub_cmd, ctx.settings.lsf.bjobs_cmd)
        if not ctx.lsf.is_available(name)
    ]
    if not missing:
        return None
    return ctx.fail("找不到 LSF 指令: %s" % ", ".join(missing),
                    evidence={"missing": missing})


@qa_check(id="PREFLIGHT_QUOTA_HIGH", title="帳號 job 數已接近上限",
          severity=Severity.WARN, scope=WAVE, stage=PRE)
def quota_high(ctx: PreflightContext) -> Optional[Issue]:
    """現在送出去只會全部卡在 PEND。

    不擋提交 (分波閘門本來就會等 quota 降下來), 但先講清楚, 免得使用者
    以為系統卡住了。
    """
    njobs = ctx.current_njobs()
    if njobs is None:
        return None
    threshold = ctx.settings.gate.quota_threshold
    if njobs < threshold * ctx.settings.preflight.quota_warn_ratio:
        return None
    return ctx.warn(
        "目前 NJOBS = %d, 閘門門檻是 %d" % (njobs, threshold),
        evidence={"njobs": njobs, "quota_threshold": threshold},
    )


@qa_check(id="PREFLIGHT_CFG_RELATIVE_PATH", title="arcx.cfg 內有相對路徑",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def cfg_relative_path(ctx: PreflightContext) -> Optional[Issue]:
    """cfg 會被複製一份到 wave 目錄, 相對路徑在那裡會解析成不同的東西。

    我們刻意用快照而不是原檔執行 —— 這樣事後做 QA 時, 用的是當時那份 cfg。
    代價就是路徑必須是絕對的。
    """
    config = ctx.arcx_config
    if config is None:
        return None
    keys = set(ctx.settings.qa.cfg_path_keys)
    offenders = []
    for block in config.enabled_blocks:
        for key, value in block.settings.items():
            if key not in keys or key in block.disabled_keys:
                continue
            if not value or value.startswith("/") or "$" in value:
                continue
            offenders.append({"block": block.name, "key": key, "value": value})
    if not offenders:
        return None
    return ctx.fail(
        "%d 個設定使用相對路徑" % len(offenders),
        evidence={"paths": offenders,
                  "hint": "cfg 會被複製到 wave 目錄, 請改用絕對路徑"},
    )


@qa_check(id="PREFLIGHT_INDEX_IN_USE", title="index 已在其他 wave 中使用",
          severity=Severity.WARN, scope=WAVE, stage=PRE)
def index_in_use(ctx: PreflightContext) -> Optional[Issue]:
    """同一個 index 出現在既有的 wave 裡。

    同時對同一個 index 跑兩份 Arcx 會互相干擾。這裡只警告不阻擋 ——
    既有的那份可能早就跑完了, 系統無法確定, 所以把證據交給人判斷。
    """
    conflicts = ctx.existing_index_usage()
    if not conflicts:
        return None
    return ctx.warn(
        "%d 個 index 曾在其他 wave 中提交過" % len(conflicts),
        evidence={"conflicts": conflicts},
    )
