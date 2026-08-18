"""從 arcx.cfg 推導出「這個 case 應該產出什麼」。

這是 QA 的地圖。case run dir 內該有哪些檔案完全由 cfg 決定:

    arcx.cfg                              case run dir
    ─────────────────────────────────────────────────────────────────
    BEGIN_SETTINGS: blocking_naming_qcap  NTN_1/
      QC_FLOW = calQCAP                     blocking_naming_qcap_calQCAP/
    END_SETTINGS                              work_calQCAP/
                                                CCI_DB.spice

    BEGIN_SETTINGS: blocking_naming_qrcfs   blocking_naming_qrcfs_calQRCFS/
      QC_FLOW = calQRCFS                        work_calQRCFS/
    END_SETTINGS                                  NTN_1.spf

每個 flow 產出什麼由 settings.qa.flows 定義 —— 新增一種 EDA tool
只需要在設定裡加一個 profile, 不需要改程式。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from arcx_auto.adapters.arcx_cfg import ArcxConfig, CfgBlock
from arcx_auto.config.settings import FlowProfile, QaSettings
from arcx_auto.domain.qa import ExpectedArtifact


def expected_artifacts(
    config: ArcxConfig,
    case_id: str,
    qa: QaSettings,
) -> Tuple[Tuple[ExpectedArtifact, ...], Tuple[str, ...]]:
    """算出某個 case 應該產出哪些檔案。

    回傳 (artifacts, problems)。``problems`` 是 cfg 本身的問題
    (block 沒寫 QC_FLOW、flow 不認得), 由呼叫端轉成 issue ——
    這一層是純函數, 不產生 issue。
    """
    artifacts: List[ExpectedArtifact] = []
    problems: List[str] = []

    for block in config.enabled_blocks:
        flow = block.flow
        if not flow:
            problems.append("block %s 沒有設定 QC_FLOW" % block.name)
            continue

        profile = qa.flows.get(flow)
        if profile is None:
            problems.append(
                "block %s 的 QC_FLOW = %s 不在已知 flow 清單中 (%s)"
                % (block.name, flow, ", ".join(sorted(qa.flows)) or "空")
            )
            continue

        if not profile.netlists:
            problems.append("flow %s 沒有定義任何產出物, 無法驗證" % flow)
            continue

        for template in profile.netlists:
            artifacts.append(ExpectedArtifact(
                block=block.name,
                flow=flow,
                relpath=_join(
                    "%s_%s" % (block.name, flow),
                    _fill(profile.work_dir, block, flow, case_id),
                    _fill(template, block, flow, case_id),
                ),
                min_bytes=max(profile.min_bytes, qa.min_netlist_bytes),
            ))

    return tuple(artifacts), tuple(problems)


def expected_flow_dirs(config: ArcxConfig) -> Tuple[str, ...]:
    """case run dir 底下應該存在的 <block>_<flow> 目錄。"""
    names = []
    for block in config.enabled_blocks:
        name = block.output_dir_name
        if name:
            names.append(name)
    return tuple(names)


def _fill(template: str, block: CfgBlock, flow: str, case_id: str) -> str:
    """套用路徑樣板。未知的 {變數} 保持原樣而不是丟例外 ——
    設定寫錯時應該產生一個「找不到檔案」的 issue 讓人看到, 而不是讓整個
    QA 流程崩掉。
    """
    try:
        return template.format(flow=flow, block=block.name, case=case_id)
    except (KeyError, IndexError):
        return template


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p)
