"""PRE checks -- validating arcx.cfg. Runs before submission, needs no run
folder.

This layer has the best return on effort: most configuration mistakes are
visible before anything is submitted, and finding the same mistake afterwards
costs hours of waiting.

Leading flag semantics (confirmed with the user):
    1  enabled
    0  disabled  -+- the line does not take effect, so its paths are **not**
    #  comment   -+  checked; those paths are never used and checking them
                    would only manufacture false alarms
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


@qa_check(id="CFG_UNREADABLE", title="arcx.cfg cannot be read",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_unreadable(cfg: ConfigContext) -> Optional[Issue]:
    """The file does not exist or cannot be read."""
    if cfg.config is None:
        return cfg.fail("no arcx.cfg was provided")
    if cfg.config.error:
        return cfg.fail(cfg.config.error, evidence={"path": cfg.config.source_path})
    return None


@qa_check(id="CFG_NO_BLOCKS", title="arcx.cfg has no settings block",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_no_blocks(cfg: ConfigContext) -> Optional[Issue]:
    """No block parsed at all -- submitting it would do nothing."""
    if cfg.config is None or cfg.config.error:
        return None                      # CFG_UNREADABLE covers this
    if cfg.config.blocks:
        return None
    return cfg.fail("no BEGIN_SETTINGS block could be parsed",
                    evidence={"path": cfg.config.source_path})


@qa_check(id="CFG_ALL_BLOCKS_DISABLED", title="every block is disabled",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_all_blocks_disabled(cfg: ConfigContext) -> Optional[Issue]:
    """Blocks exist but every one has a leading 0.

    Such a cfg runs to completion quietly and produces nothing, which is the
    most wasteful failure mode there is.
    """
    if cfg.config is None or not cfg.config.blocks:
        return None
    if cfg.config.enabled_blocks:
        return None
    return cfg.fail(
        "all %d block(s) are disabled by a leading 0" % len(cfg.config.blocks),
        evidence={"blocks": [b.name for b in cfg.config.blocks]},
    )


@qa_check(id="CFG_DUPLICATE_BLOCK", title="duplicate block name",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_duplicate_block(cfg: ConfigContext) -> Optional[Issue]:
    """Two blocks share a name.

    Output directories are <block>_<QC_FLOW>, so same-named blocks write into
    the same directory and overwrite each other. The run "succeeds" with only
    the last one left, and nothing reports an error.
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
        "%d duplicated block name(s)" % len(duplicates),
        evidence={"duplicates": duplicates},
    )


@qa_check(id="CFG_BLOCK_NO_FLOW", title="block has no QC_FLOW",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_block_no_flow(cfg: ConfigContext) -> Optional[Issue]:
    """An enabled block without QC_FLOW: no EDA tool flow is named."""
    if cfg.config is None:
        return None
    offenders = [b.name for b in cfg.config.enabled_blocks if not b.flow]
    if not offenders:
        return None
    return cfg.fail("%d block(s) have no QC_FLOW" % len(offenders),
                    evidence={"blocks": offenders})


@qa_check(id="CFG_UNKNOWN_FLOW", title="QC_FLOW is not a known flow",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_unknown_flow(cfg: ConfigContext) -> Optional[Issue]:
    """The QC_FLOW value is not one the system knows.

    Either the cfg has a typo, or a new EDA tool was added without a definition
    in qa.flows. Either way its artifacts cannot be verified, so it has to be
    said before submission rather than after.
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
        "%d block(s) name an unknown QC_FLOW" % len(offenders),
        evidence={"blocks": offenders, "known_flows": sorted(known)},
    )


@qa_check(id="CFG_PATH_NOT_FOUND", title="a referenced file does not exist",
          severity=Severity.FATAL, scope=GLOBAL, stage=PRE)
def cfg_path_not_found(cfg: ConfigContext) -> Optional[Issue]:
    """A file or directory referenced by an active setting is missing.

    Only the keys listed in qa.cfg_path_keys (RCX_TECH_QTF, LVS_DECK and so on)
    are checked, and only on **enabled** lines. A line with a leading 0, or
    commented out, never takes effect, so checking it would only manufacture
    false alarms -- and once there are false alarms nobody reads the warnings.

    Values containing $ cannot be resolved here, so they are listed as skipped
    rather than reported as missing.
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
                continue                  # leading 0 -> inactive, not checked
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
        "%d referenced path(s) do not exist" % len(missing),
        evidence={"missing": missing, "skipped_env_vars": skipped},
    )


@qa_check(id="CFG_PARSE_WARNING", title="arcx.cfg parsed with doubts",
          severity=Severity.WARN, scope=GLOBAL, stage=PRE)
def cfg_parse_warning(cfg: ConfigContext) -> Optional[Issue]:
    """Doubts the parser raised: misspelt keywords, a missing END_SETTINGS,
    unparseable lines.
    """
    if cfg.config is None:
        return None
    all_warnings = list(cfg.config.warnings)
    for block in cfg.config.blocks:
        all_warnings.extend("[%s] %s" % (block.name, w) for w in block.warnings)
    if not all_warnings:
        return None
    return cfg.warn("%d parsing doubt(s)" % len(all_warnings),
                    evidence={"warnings": all_warnings})
