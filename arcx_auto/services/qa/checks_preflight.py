"""Whole-batch pre-submission checks (scope=WAVE, stage=PRE).

arcx.cfg itself is checked in checks_config.py; this file asks whether this
batch can go out right now: target directories, disk, LSF, quota, and clashes
with existing work.

The rule: **any FATAL blocks the submission.** Discovering the same thing after
submitting costs hours of waiting, and a failure part way through also leaves
half-built state to clean up.
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


@qa_check(id="PREFLIGHT_NO_WAVES", title="no wave can be submitted",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def no_waves(ctx: PreflightContext) -> Optional[Issue]:
    """Planning produced no waves: every selected index is unusable."""
    if ctx.plan.waves:
        return None
    return ctx.fail(
        "there is no wave to submit",
        evidence={
            "excluded": [
                {"index": s.index_key, "reason": s.error}
                for s in ctx.plan.excluded
            ],
        },
    )


@qa_check(id="PREFLIGHT_INDEX_EXCLUDED", title="some indices were excluded",
          severity=Severity.WARN, scope=WAVE, stage=PRE)
def index_excluded(ctx: PreflightContext) -> Optional[Issue]:
    """Some indices could not be planned because their data is incomplete.

    This does not block submission -- running the rest is still worth more --
    but what is missing has to be visible, or the batch will look complete when
    it finishes.
    """
    if not ctx.plan.excluded:
        return None
    return ctx.warn(
        "%d index/indices will not run" % len(ctx.plan.excluded),
        evidence={
            "excluded": [
                {"index": s.index_key, "path": s.path, "reason": s.error}
                for s in ctx.plan.excluded
            ],
        },
    )


@qa_check(id="PREFLIGHT_TARGET_EXISTS", title="target directory exists and is not empty",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def target_exists(ctx: PreflightContext) -> Optional[Issue]:
    """The wave directory we would create already has content.

    Never overwriting existing results is one of the non-negotiable rules. To
    redo work, use the rerun flow: it backs up the failed state before cleaning
    up, whereas overwriting destroys the evidence permanently.
    """
    occupied = []
    for wave in ctx.plan.waves:
        path = os.path.join(ctx.run_dir, wave.name)
        if os.path.isdir(path) and os.listdir(path):
            occupied.append(path)
    if not occupied:
        return None
    return ctx.fail(
        "%d target director(ies) already exist and are not empty" % len(occupied),
        evidence={"paths": occupied,
                  "hint": "use a different --run-id, or the rerun flow"},
    )


@qa_check(id="PREFLIGHT_DISK_LOW", title="not enough disk space",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def disk_low(ctx: PreflightContext) -> Optional[Issue]:
    """The target filesystem is nearly full.

    A full disk is the number one silent killer of RC extraction: jobs die part
    way through, in a way that does not say why. Blocking before submission is
    far cheaper than debugging afterwards.
    """
    ratio = ctx.disk_free_ratio()
    if ratio is None:
        return None
    settings = ctx.settings.preflight
    if ratio < settings.min_disk_free_ratio:
        return ctx.fail(
            "only %.1f%% free, below the %.1f%% threshold"
            % (ratio * 100, settings.min_disk_free_ratio * 100),
            evidence={"path": ctx.run_dir, "free_ratio": round(ratio, 4)},
        )
    if ratio < settings.warn_disk_free_ratio:
        return ctx.warn(
            "%.1f%% free, close to the threshold" % (ratio * 100),
            evidence={"path": ctx.run_dir, "free_ratio": round(ratio, 4)},
        )
    return None


@qa_check(id="PREFLIGHT_LSF_UNAVAILABLE", title="LSF commands unavailable",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def lsf_unavailable(ctx: PreflightContext) -> Optional[Issue]:
    """bsub is not on PATH. Submitting would achieve nothing."""
    if ctx.lsf is None:
        return None
    missing = [
        name for name in (ctx.settings.lsf.bsub_cmd, ctx.settings.lsf.bjobs_cmd)
        if not ctx.lsf.is_available(name)
    ]
    if not missing:
        return None
    return ctx.fail("LSF command(s) not found: %s" % ", ".join(missing),
                    evidence={"missing": missing})


@qa_check(id="PREFLIGHT_QUOTA_HIGH", title="account job count near the limit",
          severity=Severity.WARN, scope=WAVE, stage=PRE)
def quota_high(ctx: PreflightContext) -> Optional[Issue]:
    """Submitting now would just pile up PEND jobs.

    Not blocking -- the wave gate already waits for the quota to fall -- but
    saying so avoids the impression that the system is stuck.
    """
    njobs = ctx.current_njobs()
    if njobs is None:
        return None
    threshold = ctx.settings.gate.quota_threshold
    if njobs < threshold * ctx.settings.preflight.quota_warn_ratio:
        return None
    return ctx.warn(
        "NJOBS is %d and the gate threshold is %d" % (njobs, threshold),
        evidence={"njobs": njobs, "quota_threshold": threshold},
    )


@qa_check(id="PREFLIGHT_CFG_RELATIVE_PATH", title="arcx.cfg uses a relative path",
          severity=Severity.FATAL, scope=WAVE, stage=PRE)
def cfg_relative_path(ctx: PreflightContext) -> Optional[Issue]:
    """The cfg is copied into the wave directory, where a relative path would
    resolve to something else.

    Running from a snapshot rather than the original is deliberate: QA later
    reads the cfg the run actually used. The price is that paths must be
    absolute.
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
        "%d setting(s) use a relative path" % len(offenders),
        evidence={"paths": offenders,
                  "hint": "the cfg is copied into the wave directory; "
                          "use absolute paths"},
    )


@qa_check(id="PREFLIGHT_INDEX_IN_USE", title="index already used by another wave",
          severity=Severity.WARN, scope=WAVE, stage=PRE)
def index_in_use(ctx: PreflightContext) -> Optional[Issue]:
    """The same index appears in an existing wave.

    Two Arcx runs over one index interfere with each other. This only warns:
    the earlier run may well have finished long ago, and the system cannot
    tell, so the evidence goes to a human.
    """
    conflicts = ctx.existing_index_usage()
    if not conflicts:
        return None
    return ctx.warn(
        "%d index/indices were submitted in another wave" % len(conflicts),
        evidence={"conflicts": conflicts},
    )
