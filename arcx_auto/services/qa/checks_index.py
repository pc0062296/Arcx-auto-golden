"""Index level QA checks.

These catch the problems where every case looks fine individually but the whole
does not add up -- a case that never appeared at all, or a report short of a
few entries. Those failures produce no error message, and looking only at cases
cannot find them.

**The POST checks here only run once every case in the index has finished**,
because that is when Arcx writes the reports. Running them earlier reports
missing report directories and half-written summaries as failures on a run that
is doing nothing wrong. Anything that is true while cases are still going --
"some are unfinished", an unknown marker, an unmappable log -- is LIVE instead.

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
from arcx_auto.services.qa.summary_table import find_bad_values, parse_summary_table

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
          severity=Severity.WARN, scope=INDEX, stage=LIVE)
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


@qa_check(id="INDEX_QUEUE_NOT_MOVING", title="cases queued while nothing runs",
          severity=Severity.WARN, scope=INDEX, stage=LIVE)
def queue_not_moving(index: IndexContext) -> Optional[Issue]:
    """Cases are waiting to start and nothing in this index is running.

    A queued case on its own says nothing. The .queue marker is Arcx's own
    queue, not LSF's -- Arcx runs only so many cases at a time within one
    index -- so on a large index most cases are queued most of the time, for
    hours, entirely normally. Reporting on the clock alone would mean
    reporting the ordinary state of the run.

    What is not ordinary is a queue that has stopped moving: work still
    waiting while **nothing is running to hold it back**. Nothing is going to
    start those cases, which is what a dead Arcx parent looks like from the
    outside -- and the reason it needs saying is that it produces no error
    anywhere. The index simply stops, and every case in it stays QUEUED
    looking like it is waiting its turn.

    The measure is how long the index has been idle, not how long a case has
    been queued: "queued for six hours" is a fact about the size of the index,
    while "queued for six hours with nothing running" is a fact about the run.

    A warning, not a failure: Arcx does legitimately go quiet between cases
    while it assembles reports or sets one up, and the threshold cannot know
    how long that takes here. What it can do is put a number in front of a
    person.
    """
    queued = [case_id for case_id, snap in index.cases.items()
              if snap.state == CaseState.QUEUED]
    if not queued:
        return None

    # Anything actually in flight means the queue has a reason to wait.
    # SUSPENDED counts -- those jobs still hold their slots -- and so does
    # STALLED, which is a running case with a quiet log. A finished case does
    # not: .complete is permanent, so counting it would silence this check
    # for good on any index that completed one case and then died.
    busy = [case_id for case_id, snap in index.cases.items()
            if snap.state in (CaseState.RUNNING, CaseState.STALLED,
                              CaseState.SUSPENDED)]
    if busy:
        return None

    # How long the queue has been standing still: the most recent thing that
    # happened to any case in this index. A case that entered QUEUED five
    # minutes ago means the index was doing something five minutes ago.
    last_change = max(snap.entered_state_at for snap in index.cases.values())
    idle_for = max(0.0, index.now - last_change)
    threshold = index.settings.qa.queue.idle_after_sec
    if idle_for < threshold:
        return None

    return index.warn(
        "%d case(s) queued and nothing has run for %.0f minutes"
        % (len(queued), idle_for / 60.0),
        evidence={
            "queued": sorted(queued)[:50],
            "queued_total": len(queued),
            "idle_sec": round(idle_for),
            "threshold_sec": threshold,
            "hint": "nothing is running to start them; check whether the Arcx "
                    "job for this directory is still alive",
        },
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


@qa_check(id="SUMMARY_TABLE_BAD_VALUE", title="summary table has values that are not numbers",
          severity=Severity.FATAL, scope=INDEX, stage=POST)
def summary_table_bad_value(index: IndexContext) -> Optional[Issue]:
    """A comparison in a QC_* Summary produced no usable number.

    The table after the `refReport =` line is where the run states its actual
    result. Every column after the two naming columns must be a real number; a
    blank cell, the word "fail", or a sentinel like 1e+15 all mean the
    comparison did not produce an answer.

    This is a false success in its purest form. The job finished, the marker is
    there, the report directory exists and the file is not empty -- and the
    number that was supposed to prove the result is missing. Nothing else in
    the system would notice.

    The number of comparison columns varies (cmpReport2, cmpReport3 and their
    diffs may or may not be present), so the header defines the width rather
    than any fixed expectation.
    """
    rules = index.qa.summary_table
    problems: List[dict] = []

    for dir_name in rules.dirs:
        if not index.is_dir(dir_name):
            continue                       # optional; REPORT_DIR_MISSING's job
        for relpath in _summary_files(index, dir_name):
            table = parse_summary_table(
                index.read_text(relpath, rules.max_bytes),
                ref_marker_regex=rules.ref_marker_regex,
                end_markers=rules.end_markers,
            )
            if not table.found:
                continue           # SUMMARY_TABLE_UNREADABLE reports this
            for bad in find_bad_values(
                table,
                fail_words=rules.fail_words,
                fail_value_threshold=rules.fail_value_threshold,
                name_columns=rules.name_columns,
            ):
                item = bad.as_dict()
                item["file"] = relpath
                problems.append(item)

    if not problems:
        return None

    return index.fail(
        "%d value(s) in the summary table(s) are not usable numbers"
        % len(problems),
        evidence={"problems": problems[:rules.max_reported],
                  "total": len(problems)},
    )


def _summary_files(index: IndexContext, dir_name: str) -> List[str]:
    """Summary reports inside one QC_* directory.

    Matched case insensitively: the directory is QC_Spice but the file is
    Report_QC_spice_Summary, and a check that silently found nothing because of
    one letter would be worse than no check at all.
    """
    names = index.listdir(dir_name)
    return [
        "%s/%s" % (dir_name, name)
        for name in names
        if "summary" in name.lower() and name.lower().startswith("report")
    ]


@qa_check(id="SUMMARY_TABLE_UNREADABLE", title="summary table could not be found",
          severity=Severity.UNKNOWN, scope=INDEX, stage=POST)
def summary_table_unreadable(index: IndexContext) -> Optional[Issue]:
    """A QC_* Summary exists but holds no comparison table we recognise.

    Deliberately UNKNOWN rather than FATAL. Not finding the table says nothing
    about the run -- the report may legitimately have no comparison in it, or
    the format may have moved on. Calling that a failure would cry wolf on
    every summary of an unfamiliar shape, and people stop reading a check that
    is usually wrong.

    It cannot be a pass either: the values that would have proved the result
    were not examined. UNKNOWN blocks success and says exactly that, which is
    what Severity.UNKNOWN is for.
    """
    rules = index.qa.summary_table
    unreadable: List[dict] = []

    for dir_name in rules.dirs:
        if not index.is_dir(dir_name):
            continue
        for relpath in _summary_files(index, dir_name):
            table = parse_summary_table(
                index.read_text(relpath, rules.max_bytes),
                ref_marker_regex=rules.ref_marker_regex,
                end_markers=rules.end_markers,
            )
            if not table.found:
                unreadable.append({"file": relpath, "reason": table.error})

    if not unreadable:
        return None
    return index.unknown(
        "%d summary report(s) hold no table this check understands, so their "
        "values were not verified" % len(unreadable),
        evidence={"files": unreadable[:rules.max_reported]},
    )
