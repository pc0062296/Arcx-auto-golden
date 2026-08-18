"""Derive what a case should produce from arcx.cfg.

This is QA's map. What belongs in a case run dir comes entirely from the cfg:

    arcx.cfg                              case run dir
    ---------------------------------------------------------------------
    BEGIN_SETTINGS: blocking_naming_qcap  NTN_1/
      QC_FLOW = calQCAP                     blocking_naming_qcap_calQCAP/
    END_SETTINGS                              work_calQCAP/
                                                CCI_DB.spice

    BEGIN_SETTINGS: blocking_naming_qrcfs   blocking_naming_qrcfs_calQRCFS/
      QC_FLOW = calQRCFS                        work_calQRCFS/
    END_SETTINGS                                  NTN_1.spf

What each flow produces is declared in settings.qa.flows, so adding a new EDA
tool is a settings change rather than a code change.
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
    """Work out which files a case should produce.

    Returns (artifacts, problems). ``problems`` describes faults in the cfg
    itself -- a block with no QC_FLOW, an unrecognised flow -- which the caller
    turns into issues. This layer is pure and raises no issues of its own.
    """
    artifacts: List[ExpectedArtifact] = []
    problems: List[str] = []

    for block in config.enabled_blocks:
        flow = block.flow
        if not flow:
            problems.append("block %s has no QC_FLOW" % block.name)
            continue

        profile = qa.flows.get(flow)
        if profile is None:
            problems.append(
                "block %s has QC_FLOW = %s, which is not a known flow (%s)"
                % (block.name, flow, ", ".join(sorted(qa.flows)) or "none")
            )
            continue

        if not profile.netlists:
            problems.append(
                "flow %s declares no artifacts, so nothing can be verified"
                % flow)
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
    """The <block>_<flow> directories that should exist in a case run dir."""
    names = []
    for block in config.enabled_blocks:
        name = block.output_dir_name
        if name:
            names.append(name)
    return tuple(names)


def _fill(template: str, block: CfgBlock, flow: str, case_id: str) -> str:
    """Apply a path template.

    An unknown {placeholder} is left as-is rather than raising: a mistake in
    settings should surface as a "file not found" issue somebody can see, not
    as a crash that takes the whole QA pass down.
    """
    try:
        return template.format(flow=flow, block=block.name, case=case_id)
    except (KeyError, IndexError):
        return template


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p)
