"""Index level QA checks.

These catch the problems where every case looks fine individually but the whole
does not add up -- a case that never appeared at all, or a report short of a
few entries. Those failures produce no error message, and looking only at cases
cannot find them.

Report directory layout:

    QC_Cc/
      Report_QC_Cc                    the main report
      Report_QC_Cc_Summary_SCCB3      the summary; the suffix is just naming,
                                      but there is exactly one per directory
    QC_Ct/    (the same)
    QC_Spice/ (the same, but it may not exist)
"""

from __future__ import annotations

from typing import List, Optional

from arcx_auto.domain.enums import CaseState, IssueScope, IssueStage, Severity
from arcx_auto.domain.qa import Issue
from arcx_auto.services.qa.context import IndexContext
from arcx_auto.services.qa.registry import qa_check

INDEX = IssueScope.INDEX
POST = IssueStage.POST
LIVE = IssueStage.LIVE


@qa_check(id="REPORT_DIR_MISSING", title="required QC report dir missing",
          severity=Severity.FATAL, scope=INDEX, stage=POST)
def report_dir_missing(index: IndexContext) -> Optional[Issue]:
    """QC_Cc and QC_Ct always exist. Missing means Arcx never finished."""
    missing = [d for d in index.qa.reports.required_dirs if not index.is_dir(d)]
    if not missing:
        return None
    return index.fail(
        "required report director(ies) missing: %s" % ", ".join(missing),
        evidence={"missing": missing, "found": index.listdir()},
    )


@qa_check(id="REPORT_FILE_MISSING", title="QC report file problem",
          severity=Severity.FATAL, scope=INDEX, stage=POST)
def report_file_missing(index: IndexContext) -> Optional[Issue]:
    """The report directory exists but its contents are wrong.

    Only existing directories are checked; a missing directory is
    REPORT_DIR_MISSING's job and is not reported twice.

    The Summary suffix (for example _SCCB3) is just naming, so it is matched by
    glob. There is exactly one Summary per QC_* directory, though, so more than
    one is also wrong -- usually a leftover from a previous attempt, which would
    feed the wrong report downstream.
    """
    reports = index.qa.reports
    problems: List[dict] = []

    for name in list(reports.required_dirs) + list(reports.optional_dirs):
        if not index.is_dir(name):
            continue
        main = reports.main_file_template.format(dir=name)
        if not index.exists("%s/%s" % (name, main)):
            problems.append({"dir": name, "missing": main})

        summary_glob = reports.summary_glob_template.format(dir=name)
        found = index.glob(summary_glob, rel_dir=name)
        if not found:
            problems.append({"dir": name, "missing_glob": summary_glob})
        elif len(found) > 1:
            problems.append({
                "dir": name,
                "expected_one_summary": summary_glob,
                "found": found,
            })

    if not problems:
        return None
    return index.fail(
        "%d report file problem(s)" % len(problems),
        evidence={"problems": problems})


@qa_check(id="INDEX_HAS_UNKNOWN_MARKER", title="unknown marker present",
          severity=Severity.UNKNOWN, scope=INDEX, stage=LIVE)
def index_has_unknown_marker(index: IndexContext) -> Optional[Issue]:
    """A marker other than .queue / .run / .complete.

    Confirmed abnormal. We do not know what it means, so it is UNKNOWN rather
    than FATAL or ignored: a human has to decide.
    """
    obs = index.observation
    if obs is None or not obs.unknown_markers:
        return None
    return index.unknown(
        "%d unknown marker(s)" % len(obs.unknown_markers),
        evidence={
            "markers": [{"file": n, "case_id": c} for n, c in obs.unknown_markers],
        },
    )


@qa_check(id="LOG_UNRESOLVED", title="log cannot be mapped to a case",
          severity=Severity.UNKNOWN, scope=INDEX, stage=LIVE)
def log_unresolved(index: IndexContext) -> Optional[Issue]:
    """A log whose cmd_file is missing or unparseable.

    It means there is a case we **cannot monitor**. Ignoring it silently would
    let the system report that all is well while it is partly blind.
    """
    obs = index.observation
    if obs is None or not obs.unresolved_logs:
        return None
    return index.unknown(
        "%d log(s) could not be mapped to a case" % len(obs.unresolved_logs),
        evidence={"logs": list(obs.unresolved_logs)},
    )


@qa_check(id="INDEX_INCOMPLETE", title="cases still unfinished",
          severity=Severity.WARN, scope=INDEX, stage=POST)
def index_incomplete(index: IndexContext) -> Optional[Issue]:
    """The index wrapped up but some cases never reached the end.

    These are exactly the run dirs a rerun would delete and redo.
    """
    pending = [
        case_id for case_id, snap in index.cases.items()
        if snap.state not in (CaseState.DONE, CaseState.COMPLETED_MARKER)
    ]
    if not pending:
        return None
    return index.warn(
        "%d of %d cases are not finished" % (len(pending), len(index.cases)),
        evidence={"pending": sorted(pending)[:50], "total": len(index.cases)},
    )


@qa_check(id="INDEX_CASE_COUNT_MISMATCH", title="case count differs from the GDS count",
          severity=Severity.WARN, scope=INDEX, stage=POST)
def case_count_mismatch(index: IndexContext) -> Optional[Issue]:
    """The run has a different number of cases than the index path has GDS.

    A cross-check, never a source of truth. The case roster comes from the
    markers and the cmd_files, which name real cases; the GDS files only say
    how many were expected, and even that loosely -- the run works on top cell
    names, which need not match the GDS filenames at all. So this can disagree
    for entirely legitimate reasons.

    It still earns its place: "we submitted 25 and the folder knows about 18"
    is exactly the shape of a silent partial submission, and nothing else in
    the system would notice.

    Only runs when a dir_map was supplied, and stays quiet otherwise rather
    than reporting UNKNOWN -- `status --run-folder` has no dir_map by design,
    and that is not a failure to check anything.
    """
    expected = index.gds_count
    if expected is None or expected <= 0:
        return None
    actual = len(index.cases)
    if actual == expected:
        return None
    return index.warn(
        "the run folder has %d case(s) but the index path holds %d GDS file(s)"
        % (actual, expected),
        evidence={"cases": actual, "gds_files": expected,
                  "case_ids": sorted(index.cases)},
    )
