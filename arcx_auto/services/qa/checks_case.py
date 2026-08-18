"""Case level QA checks.

**This file is meant to be read and edited by people.** Each check should be
short enough to take in at a glance, and its docstring is what the UI shows,
so documentation and code cannot drift apart.

To add a check: copy an existing one and change the id, title, severity and
logic. To disable one: add its id to qa.disabled_checks in settings; no code
needs deleting.

Expected artifact layout, derived from arcx.cfg (see expectations.py):

    <case_run_dir>/<block>_<QC_FLOW>/work_<QC_FLOW>/<netlist>
"""

from __future__ import annotations

from typing import List, Optional

from arcx_auto.domain.enums import CaseState, IssueScope, IssueStage, Severity
from arcx_auto.domain.qa import Issue
from arcx_auto.services.qa.context import CaseContext
from arcx_auto.services.qa.registry import qa_check

CASE = IssueScope.CASE
POST = IssueStage.POST
LIVE = IssueStage.LIVE


# ===========================================================================
# POST -- runs once after .complete, answering "did it really succeed?"
# ===========================================================================

@qa_check(id="CASE_DIR_MISSING", title="case run dir is missing",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def case_dir_missing(case: CaseContext) -> Optional[Issue]:
    """The marker says it finished, but the case run dir is not there.

    Usually the directory was deleted, or the marker is left over from an
    earlier attempt.
    """
    if case.case_dir_exists:
        return None
    return case.fail("case run dir not found", evidence={"path": case.root})


@qa_check(id="CFG_EXPECTATION_UNAVAILABLE", title="cannot tell what to check",
          severity=Severity.UNKNOWN, scope=CASE, stage=POST)
def cfg_expectation_unavailable(case: CaseContext) -> Optional[Issue]:
    """There is no arcx.cfg, or it yielded no expected artifacts.

    This **cannot count as a pass**: we simply do not know what to look for.
    A cfg snapshot should exist from submission time; its absence means the
    workspace is incomplete.
    """
    if case.expected_artifacts:
        return None
    if case.arcx_config is None:
        return case.unknown(
            "no arcx.cfg snapshot, so expected artifacts cannot be derived",
            evidence={"hint": "submission should snapshot arcx.cfg into the "
                              "wave directory"},
        )
    return case.unknown(
        "arcx.cfg yielded no expected artifacts",
        evidence={"problems": list(case.expectation_problems)},
    )


@qa_check(id="CFG_EXPECTATION_PROBLEM", title="arcx.cfg has a problem",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def cfg_expectation_problem(case: CaseContext) -> Optional[Issue]:
    """A cfg block has no QC_FLOW, or names a flow the system does not know.

    An unknown flow means its output cannot be verified: either the cfg has a
    typo, or settings.qa.flows needs a definition for it.
    """
    problems = case.expectation_problems
    if not problems:
        return None
    return case.fail(
        "arcx.cfg has %d problem(s); some artifacts cannot be verified"
        % len(problems),
        evidence={"problems": list(problems)},
    )


@qa_check(id="FLOW_DIR_MISSING", title="flow directory missing",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def flow_dir_missing(case: CaseContext) -> Optional[Issue]:
    """arcx.cfg declares this block, but its directory is not in the run dir.

    That means the flow never ran at all, which is worse than an artifact
    failing to be written.
    """
    expected = case.expected_flow_dirs
    if not expected:
        return None
    missing = [name for name in expected if not case.is_dir(name)]
    if not missing:
        return None
    return case.fail(
        "%d flow director(ies) missing" % len(missing),
        evidence={"missing": missing, "found": case.listdir()},
    )


@qa_check(id="NETLIST_MISSING", title="netlist missing",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def netlist_missing(case: CaseContext) -> Optional[Issue]:
    """An expected netlist file does not exist.

    This is the main way false success is caught: Arcx wrote a .complete
    marker, but the file was never produced.
    """
    missing = [a for a in case.expected_artifacts if not case.exists(a.relpath)]
    if not missing:
        return None
    return case.fail(
        "%d netlist(s) missing" % len(missing),
        evidence={
            "missing": [
                {"block": a.block, "flow": a.flow, "path": a.relpath}
                for a in missing
            ],
        },
    )


@qa_check(id="NETLIST_EMPTY", title="netlist too small or empty",
          severity=Severity.FATAL, scope=CASE, stage=POST)
def netlist_empty(case: CaseContext) -> Optional[Issue]:
    """The netlist exists but is too small to be a real result.

    This is what a job that created the file and then died immediately leaves
    behind -- checking only for existence would miss it.
    """
    offenders = []
    for artifact in case.expected_artifacts:
        size = case.size(artifact.relpath)
        if size is None:
            continue                       # NETLIST_MISSING covers this
        if size < artifact.min_bytes:
            offenders.append({
                "path": artifact.relpath,
                "size": size,
                "min_bytes": artifact.min_bytes,
            })
    if not offenders:
        return None
    return case.fail(
        "%d netlist(s) below the size threshold" % len(offenders),
        evidence={"files": offenders})


@qa_check(id="MARKER_INCONSISTENT", title="markers not cleaned up",
          severity=Severity.WARN, scope=CASE, stage=POST)
def marker_inconsistent(case: CaseContext) -> Optional[Issue]:
    """.complete appeared but .run / .queue were not cleared.

    The artifacts may well be fine, but Arcx did not finish tidying up. Worth
    a look, and it makes the rerun completeness decision doubtful.
    """
    if not case.case.marker_inconsistent:
        return None
    return case.warn(
        ".complete is present but .run/.queue were not cleared",
        evidence={"case_id": case.case_id},
    )


# ===========================================================================
# LIVE -- runs every tick, answering "is it healthy right now?"
# ===========================================================================

@qa_check(id="CASE_QUIET", title="log has not grown in a long time",
          severity=Severity.WARN, scope=CASE, stage=LIVE)
def case_quiet(case: CaseContext) -> Optional[Issue]:
    """The log has been quiet for a long time.

    Graded rather than binary on purpose: a single case can take ten minutes
    or three days, and there really is a pattern where the log goes quiet
    because the artifacts are already written. The system states how long it
    has been quiet and escalates as that grows; the judgement stays human.

        < 4h        not reported
        4h - 8h     WARN   -- uncommon, worth a look
        > 8h        FATAL  -- effectively stuck

    Downgraded one level when every artifact is already present, since that is
    usually just tidy-up.
    """
    if case.state not in (CaseState.RUNNING, CaseState.SUSPENDED,
                          CaseState.STALLED):
        return None

    quiet = case.qa.quiet
    silent = case.silent_for
    if silent < quiet.warn_after_sec:
        return None

    severity = (Severity.FATAL if silent >= quiet.stalled_after_sec
                else Severity.WARN)

    ready = case.artifacts_ready()
    if ready and quiet.downgrade_when_artifacts_ready:
        severity = Severity.WARN if severity == Severity.FATAL else Severity.INFO

    return case.at(
        severity,
        "log has not grown for %.1f hours%s" % (
            silent / 3600.0,
            " (but every expected artifact is already present)" if ready else ""),
        evidence={
            "silent_sec": round(silent, 1),
            "warn_after_sec": quiet.warn_after_sec,
            "stalled_after_sec": quiet.stalled_after_sec,
            "artifacts_ready": ready,
            "log_path": case.case.log_path,
        },
    )


@qa_check(id="LSF_SUSPENDED", title="LSF job is suspended",
          severity=Severity.WARN, scope=CASE, stage=LIVE)
def lsf_suspended(case: CaseContext) -> Optional[Issue]:
    """LSF reports this job in a *SUSP state.

    It holds a slot without making progress, usually through preemption or a
    manual stop.
    """
    lsf_state = case.case.lsf_state
    if lsf_state is None or not lsf_state.is_suspended:
        return None
    return case.warn(
        "LSF state is %s" % lsf_state.value,
        evidence={"lsf_state": lsf_state.value, "job_id": case.case.lsf_job_id},
    )


@qa_check(id="CASE_NEVER_STARTED", title="case was never submitted",
          severity=Severity.FATAL, scope=CASE, stage=LIVE)
def case_never_started(case: CaseContext) -> Optional[Issue]:
    """A case run dir exists but no marker and no log ever appeared.

    This is the failure the current process misses most easily: it does not
    fail, it simply does not exist, so no error message ever mentions it.
    """
    if case.state != CaseState.PENDING:
        return None
    if not case.case_dir_exists:
        return None
    if case.case.log_path:
        return None
    return case.fail(
        "run dir exists but no marker or log; it may never have been submitted",
        evidence={"case_dir": case.root},
    )
