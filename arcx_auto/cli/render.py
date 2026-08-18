"""Terminal rendering. Pure functions: data in, string out."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from arcx_auto.domain.enums import CaseState, Completeness, PlanMode, Severity
from arcx_auto.domain.models import (
    DirMap,
    IndexRunObservation,
    IndexRunSnapshot,
    IndexSpec,
    WavePlan,
)
from arcx_auto.domain.qa import Issue
from arcx_auto.services.state_engine import classify_completeness
from arcx_auto.util.textfmt import (
    format_duration,
    format_size,
    format_timestamp,
    render_table,
)

# Display order: things needing attention first, so problems stand out
_STATE_ORDER = [
    CaseState.FAILED,
    CaseState.LOST,
    CaseState.STALLED,
    CaseState.SUSPENDED,
    CaseState.UNKNOWN,
    CaseState.RUNNING,
    CaseState.QUEUED,
    CaseState.PENDING,
    CaseState.COMPLETED_MARKER,
    CaseState.DONE,
]


def render_status(
    snapshots: Sequence[IndexRunSnapshot],
    now: float,
    detail: bool = False,
    lsf_note: Optional[str] = None,
    observations: Optional[Sequence["IndexRunObservation"]] = None,
    qa_reports: Optional[Sequence[object]] = None,
    show_issues: bool = False,
) -> str:
    """The status overview."""
    lines: List[str] = []

    if lsf_note:
        lines.append("  ! LSF data unavailable: %s" % lsf_note)
        lines.append("    -> LOST / SUSPENDED detection is disabled; "
                     "only markers and logs are used.")
        lines.append("")

    totals: Dict[str, int] = {}
    for snapshot in snapshots:
        for state, count in snapshot.count_by_state().items():
            totals[state] = totals.get(state, 0) + count

    lines.append("== overview ==")
    lines.append(_render_state_summary(totals))
    lines.append("")

    lines.append("== index run folders ==")
    rows = []
    for snapshot in snapshots:
        counts = snapshot.count_by_state()
        attention = len(snapshot.cases_needing_attention())
        rows.append([
            snapshot.index_key,
            str(len(snapshot.cases)),
            str(counts.get(CaseState.COMPLETED_MARKER.value, 0)
                + counts.get(CaseState.DONE.value, 0)),
            str(counts.get(CaseState.RUNNING.value, 0)),
            str(counts.get(CaseState.QUEUED.value, 0)),
            str(attention) if attention else "-",
            snapshot.error or "",
        ])
    lines.append(render_table(
        ["index", "cases", "done", "running", "queued", "attention", "note"],
        rows,
        aligns=["left", "right", "right", "right", "right", "right", "left"],
    ))

    attention_rows = []
    for snapshot in snapshots:
        for case in sorted(snapshot.cases_needing_attention(),
                           key=lambda c: c.case_id):
            attention_rows.append([
                snapshot.index_key,
                case.case_id,
                case.state.value,
                format_duration(now - case.entered_state_at),
                format_duration(case.silent_for(now)),
                case.note or "",
            ])
    if attention_rows:
        lines.append("")
        lines.append("== cases needing attention (%d) ==" % len(attention_rows))
        lines.append(render_table(
            ["index", "case", "state", "in state", "log quiet", "reason"],
            attention_rows,
        ))

    qa_block = _render_qa_issues(qa_reports or (), show_all=show_issues)
    if qa_block:
        lines.append("")
        lines.append(qa_block)

    scan_issues = _render_scan_issues(observations or ())
    if scan_issues:
        lines.append("")
        lines.append(scan_issues)

    if detail:
        for snapshot in snapshots:
            lines.append("")
            lines.append("== %s (%s) ==" % (snapshot.index_key, snapshot.run_folder))
            rows = []
            for case_id in sorted(snapshot.cases, key=_case_sort):
                case = snapshot.cases[case_id]
                completeness, _why = classify_completeness(case)
                rows.append([
                    case.case_id,
                    case.state.value,
                    case.lsf_state.value if case.lsf_state else "-",
                    case.lsf_job_id or "-",
                    format_size(case.last_progress_size),
                    format_duration(case.silent_for(now)),
                    _completeness_label(completeness),
                ])
            lines.append(render_table(
                ["case", "state", "LSF", "job id", "log size", "quiet",
                 "rerun verdict"],
                rows,
                aligns=["left", "left", "left", "right", "right", "right", "left"],
            ))
    return "\n".join(lines)


#: Severity display order and markers. UNKNOWN sits above WARN on purpose:
#: "could not check" needs a human more than "a minor problem" does.
_SEVERITY_ORDER = [Severity.FATAL, Severity.UNKNOWN, Severity.WARN, Severity.INFO]
_SEVERITY_MARK = {
    Severity.FATAL: "!!",
    Severity.UNKNOWN: "??",
    Severity.WARN: "! ",
    Severity.INFO: "  ",
}


def _render_qa_issues(reports: Sequence[object], show_all: bool = False) -> str:
    """Problems QA found.

    By default only FATAL and UNKNOWN are listed, the ones needing action;
    --issues lists everything. Issues sharing an id are grouped: when 200
    cases hit the same problem an engineer needs "NETLIST_MISSING x 200",
    not 200 identical lines.
    """
    issues: List[Issue] = []
    for report in reports:
        issues.extend(getattr(report, "all_issues")())
    if not issues:
        return ""

    if not show_all:
        issues = [i for i in issues
                  if i.severity in (Severity.FATAL, Severity.UNKNOWN)]
        if not issues:
            return ""

    grouped: Dict[str, List[Issue]] = {}
    for issue in issues:
        grouped.setdefault(issue.id, []).append(issue)

    def group_key(item):
        first = item[1][0]
        rank = (_SEVERITY_ORDER.index(first.severity)
                if first.severity in _SEVERITY_ORDER else 99)
        return (rank, -len(item[1]), item[0])

    rows = []
    for issue_id, group in sorted(grouped.items(), key=group_key):
        first = group[0]
        targets = sorted({i.case_id or i.index_key or "-" for i in group})
        shown = ", ".join(targets[:4])
        if len(targets) > 4:
            shown += " ... (+%d)" % (len(targets) - 4)
        rows.append([
            _SEVERITY_MARK.get(first.severity, "  ") + " " + first.severity.value,
            issue_id,
            str(len(group)),
            first.title or "",
            shown,
        ])

    total = sum(len(g) for g in grouped.values())
    header = "== QA issues (%d) ==" % total
    if not show_all:
        header += "   (FATAL/UNKNOWN only; add --issues for all)"
    return header + "\n" + render_table(
        ["severity", "issue id", "count", "description", "targets"],
        rows,
        aligns=["left", "left", "right", "left", "left"],
        max_col_width=44,
    )


def _render_scan_issues(observations: Sequence["IndexRunObservation"]) -> str:
    """Things the scan could not classify.

    These two are the early signal that Arcx file conventions changed, or that
    a case cannot be monitored at all. Ignoring them silently would let the
    system report all-clear while it is partly blind.
    """
    rows = []
    for obs in observations:
        for name, case_id in obs.unknown_markers:
            rows.append([
                obs.index_key, "!! unknown marker", name,
                "only queue/run/complete are known; case=%s needs a look"
                % case_id,
            ])
        for name in obs.unresolved_logs:
            rows.append([
                obs.index_key, "log unmapped to a case", name,
                "its cmd_file is missing or unparseable",
            ])
        for name in obs.unmatched_entries:
            rows.append([obs.index_key, "unclassified file", name,
                         "matches no known convention"])
    if not rows:
        return ""
    return "== scan anomalies (%d) ==\n" % len(rows) + render_table(
        ["index", "kind", "name", "description"], rows, max_col_width=50
    )


def _render_state_summary(totals: Dict[str, int]) -> str:
    if not totals:
        return "  (no cases observed)"
    rows = []
    for state in _STATE_ORDER:
        count = totals.get(state.value, 0)
        if count:
            rows.append([state.value, str(count),
                         "attention" if state.needs_attention else ""])
    for state_name, count in sorted(totals.items()):
        if state_name not in {s.value for s in _STATE_ORDER}:
            rows.append([state_name, str(count), ""])
    return render_table(["state", "count", ""], rows,
                        aligns=["left", "right", "left"])


def _completeness_label(completeness: Completeness) -> str:
    return {
        Completeness.COMPLETE: "keep",
        Completeness.INCOMPLETE: "delete and rerun",
        Completeness.UNKNOWN: "delete and rerun (uncertain)",
    }[completeness]


def _case_sort(case_id: str):
    """Case ids are cell names; natural sort keeps NDIO_2 before NDIO_10."""
    from arcx_auto.adapters.fs import natural_key
    return natural_key(case_id)


# --------------------------------------------------------------------------
# Wave plan
# --------------------------------------------------------------------------

def render_plan(plan: WavePlan, show_command: bool = False,
                commands: Optional[Dict[str, str]] = None) -> str:
    lines: List[str] = []
    lines.append("== wave plan ==")
    lines.append("  mode          : %s" % plan.mode.value)
    lines.append("  slots per wave: %d" % plan.max_slots_per_wave)
    lines.append("  waves         : %d" % len(plan.waves))
    lines.append("  indices       : %d" % sum(len(w.indices) for w in plan.waves))
    lines.append("  cases         : %d" % plan.total_cases)
    lines.append("  slots         : %d" % plan.total_slots)
    lines.append("")

    rows = []
    for wave in plan.waves:
        over = " (over cap)" if wave.total_slots > plan.max_slots_per_wave else ""
        rows.append([
            wave.name,
            str(len(wave.indices)),
            str(wave.total_cases),
            str(wave.total_slots) + over,
            ", ".join(wave.index_keys),
        ])
    lines.append(render_table(
        ["wave", "indices", "cases", "slots", "index"],
        rows,
        aligns=["left", "right", "right", "right", "left"],
    ))

    lines.append("")
    lines.append("== index detail ==")
    detail_rows = []
    for wave in plan.waves:
        for spec in wave.indices:
            detail_rows.append([
                wave.name,
                spec.index_key,
                str(spec.gds_count),
                str(spec.cpu_per_case),
                str(spec.slots),
                ",".join(spec.keywords) or "-",
                spec.path,
            ])
    lines.append(render_table(
        ["wave", "index", "GDS", "cpu/case", "slots", "keywords", "path"],
        detail_rows,
        aligns=["left", "left", "right", "right", "right", "left", "left"],
        max_col_width=70,
    ))

    if plan.excluded:
        lines.append("")
        lines.append("== excluded indices (%d) ==" % len(plan.excluded))
        lines.append(render_table(
            ["index", "reason", "path"],
            [[s.index_key, s.error or "incomplete data", s.path]
             for s in plan.excluded],
            max_col_width=70,
        ))

    if plan.warnings:
        lines.append("")
        lines.append("== warnings (%d) ==" % len(plan.warnings))
        for warning in plan.warnings:
            lines.append("  ! %s" % warning)

    if show_command and commands:
        lines.append("")
        lines.append("== commands each wave would run (dry-run, not executed) ==")
        for wave in plan.waves:
            lines.append("  %s:" % wave.name)
            lines.append("    cwd: <run_root>/<run_id>/%s" % wave.name)
            lines.append("    cmd: %s" % commands.get(wave.name, ""))

    return "\n".join(lines)


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def render_submit(outcome, dry_run: bool = False) -> str:
    """The submission report."""
    lines: List[str] = []

    for label, result in (("arcx.cfg checks", outcome.cfg_check),
                          ("pre-submission checks", outcome.preflight)):
        if result is None:
            continue
        lines.append("== %s ==" % label)
        if not result.issues:
            lines.append("  OK  all checks passed")
        else:
            rows = []
            for issue in sorted(
                result.issues,
                key=lambda i: (_SEVERITY_ORDER.index(i.severity)
                               if i.severity in _SEVERITY_ORDER else 99, i.id),
            ):
                rows.append([
                    _SEVERITY_MARK.get(issue.severity, "  ") + " "
                    + issue.severity.value,
                    issue.id,
                    issue.message,
                ])
            lines.append(render_table(
                ["severity", "issue id", "description"], rows,
                                      max_col_width=60))
            for issue in result.issues:
                if issue.severity != Severity.FATAL or not issue.evidence:
                    continue
                lines.append("")
                lines.append("  [%s] evidence:" % issue.id)
                for key, value in sorted(issue.evidence.items()):
                    if isinstance(value, list):
                        for item in value[:10]:
                            lines.append("    - %s" % item)
                    else:
                        lines.append("    %s: %s" % (key, value))
        lines.append("")

    if outcome.blocked:
        lines.append("  BLOCKED  FATAL problems found; nothing was created "
                     "and no job was submitted")
        return "\n".join(lines)

    if outcome.error:
        lines.append("  ERROR  %s" % outcome.error)

    lines.append("== submission ==")
    lines.append("  run id : %s" % outcome.run_id)
    lines.append("  directory : %s" % outcome.run_dir)
    lines.append("  mode      : %s"
                 % ("dry-run (nothing executed)" if dry_run else "live"))

    if outcome.launches:
        rows = []
        for launch in outcome.launches:
            rows.append([
                launch.wave_name,
                "dry-run" if launch.dry_run else ("ok" if launch.ok else "failed"),
                launch.job_id or "-",
                launch.error or " ".join(launch.command),
            ])
        lines.append("")
        lines.append(render_table(
            ["wave", "result", "job id", "command / error"], rows,
                                  max_col_width=90))

    if outcome.pending_waves:
        lines.append("")
        lines.append("  waves not yet submitted (%d): %s"
                     % (len(outcome.pending_waves),
                        ", ".join(outcome.pending_waves)))
        lines.append("  the gate releases them once the quota drops or the "
                     "wait limit is reached.")

    if dry_run:
        lines.append("")
        lines.append("  This was a dry run. Add --yes to create the "
                     "directories and submit for real.")
    elif outcome.submitted_count:
        lines.append("")
        lines.append("  next: arcx-auto daemon --run-id %s --wave-dir %s/wave_001"
                     % (outcome.run_id, outcome.run_dir))

    return "\n".join(lines)


def render_rerun_plan(plan, dry_run: bool = True) -> str:
    """What a rerun would do, before anything is touched."""
    lines: List[str] = []
    lines.append("== rerun plan ==")
    lines.append("  wave      : %s" % plan.wave_name)
    lines.append("  directory : %s" % plan.wave_dir)
    lines.append("  indices   : %s" % (", ".join(plan.index_keys) or "-"))
    lines.append("  parent job: %s" % (plan.arcx_job_id or "-"))
    lines.append("  attempt   : %d" % plan.attempt)
    lines.append("")

    counts = {
        "delete and rerun": len(plan.to_delete),
        "keep": len(plan.to_keep),
        "uncertain (will be deleted)": len(plan.uncertain),
    }
    lines.append(render_table(
        ["outcome", "cases"],
        [[k, str(v)] for k, v in counts.items()],
        aligns=["left", "right"]))

    if plan.to_delete:
        lines.append("")
        lines.append("== would be deleted and rerun (%d) ==" % len(plan.to_delete))
        rows = []
        for decision in plan.to_delete:
            mark = "?? " if decision.uncertain else "   "
            rows.append([
                mark + decision.index_key,
                decision.case_id,
                decision.state.value,
                decision.reason,
                decision.case_dir or "(no run dir)",
            ])
        lines.append(render_table(
            ["index", "case", "state", "reason", "run dir"],
            rows, max_col_width=48))

    if plan.to_keep:
        lines.append("")
        lines.append("== would be kept (%d) ==" % len(plan.to_keep))
        lines.append(render_table(
            ["index", "case", "state", "reason"],
            [[d.index_key, d.case_id, d.state.value, d.reason]
             for d in plan.to_keep],
            max_col_width=48))

    if plan.warnings:
        lines.append("")
        lines.append("== warnings (%d) ==" % len(plan.warnings))
        for warning in plan.warnings:
            lines.append("  ! %s" % warning)

    if plan.blockers:
        lines.append("")
        lines.append("== blocked (%d) ==" % len(plan.blockers))
        for blocker in plan.blockers:
            lines.append("  BLOCKED  %s" % blocker)
        lines.append("")
        lines.append("  Nothing will be stopped, moved or submitted.")
        return "\n".join(lines)

    if dry_run:
        lines.append("")
        lines.append("  This is a dry run. Add --yes to stop the jobs, move the")
        lines.append("  listed run dirs into .arcx_auto/attempts/%d/ and resubmit."
                     % plan.attempt)
    return "\n".join(lines)


def render_rerun_outcome(outcome) -> str:
    """The result of executing a rerun."""
    lines: List[str] = []
    lines.append("")
    lines.append("== rerun result ==")
    lines.append("  phase : %s" % outcome.phase.value)

    if outcome.drain_attempts:
        rows = []
        for attempt in outcome.drain_attempts:
            rows.append([
                str(attempt.attempt),
                "ok" if attempt.deleted_ok else "failed",
                "unknown" if attempt.remaining is None else str(attempt.remaining),
                attempt.error or "",
            ])
        lines.append("")
        lines.append("== drain attempts ==")
        lines.append(render_table(
            ["attempt", "delete", "jobs left", "error"], rows,
            aligns=["right", "left", "right", "left"], max_col_width=60))

    if outcome.quiescent_checks:
        rows = []
        for check in outcome.quiescent_checks:
            rows.append([
                str(check.index),
                "unknown" if check.jobs is None else str(check.jobs),
                "yes" if check.markers_changed else "no",
                check.error or "",
            ])
        lines.append("")
        lines.append("== quiet confirmations ==")
        lines.append(render_table(
            ["check", "jobs", "markers changed", "error"], rows,
            aligns=["right", "right", "left", "left"], max_col_width=60))

    if outcome.backed_up:
        lines.append("")
        lines.append("  moved aside: %d run dir(s) into .arcx_auto/attempts/%d/"
                     % (len(outcome.backed_up), outcome.plan.attempt))

    if outcome.resubmit_job_id:
        lines.append("  resubmitted: job id %s" % outcome.resubmit_job_id)

    if outcome.aborted:
        lines.append("")
        lines.append("  ABORTED  %s" % (outcome.error or "unknown reason"))
        lines.append("  Nothing was deleted beyond what is listed above.")
    elif outcome.error:
        lines.append("")
        lines.append("  ERROR  %s" % outcome.error)

    return "\n".join(lines)


def render_cfg_check(config, result) -> str:
    """The arcx.cfg PRE check report."""
    lines: List[str] = []
    lines.append("== arcx.cfg checks ==")
    lines.append("  source: %s" % (config.source_path if config else "-"))

    if config and config.blocks:
        lines.append("")
        rows = []
        for block in config.blocks:
            rows.append([
                block.name,
                "enabled" if block.enabled else "disabled",
                block.flow or "(no QC_FLOW)",
                block.output_dir_name or "-",
                str(len(block.settings)),
                ",".join(block.disabled_keys) or "-",
            ])
        lines.append(render_table(
            ["block", "state", "QC_FLOW", "output dir", "settings",
             "disabled settings"],
            rows, max_col_width=44,
        ))

    lines.append("")
    if result.passed and not result.issues:
        lines.append("  OK  all checks passed")
        return "\n".join(lines)

    rows = []
    for issue in sorted(
        result.issues,
        key=lambda i: (_SEVERITY_ORDER.index(i.severity)
                       if i.severity in _SEVERITY_ORDER else 99, i.id),
    ):
        rows.append([
            _SEVERITY_MARK.get(issue.severity, "  ") + " " + issue.severity.value,
            issue.id,
            issue.message,
        ])
    lines.append("== problems (%d) ==" % len(result.issues))
    lines.append(render_table(["severity", "issue id", "description"], rows,
                              max_col_width=60))

    for issue in result.issues:
        if not issue.evidence:
            continue
        lines.append("")
        lines.append("  [%s] evidence:" % issue.id)
        for key, value in sorted(issue.evidence.items()):
            if isinstance(value, list):
                if not value:
                    continue
                lines.append("    %s:" % key)
                for item in value[:20]:
                    lines.append("      - %s" % item)
                if len(value) > 20:
                    lines.append("      ... (+%d)" % (len(value) - 20))
            elif isinstance(value, dict):
                lines.append("    %s: %s" % (key, value))
            else:
                lines.append("    %s: %s" % (key, value))

    if result.fatal:
        lines.append("")
        lines.append("  BLOCKED  %d FATAL problem(s); submitting is not "
                     "advisable" % len(result.fatal))
    return "\n".join(lines)


def render_dir_map(dir_map: DirMap, verify: bool = False) -> str:
    import os

    lines: List[str] = []
    lines.append("== dir_map ==")
    lines.append("  source: %s" % dir_map.source_path)
    lines.append("  index : %d entries" % len(dir_map.entries))
    if dir_map.meta:
        lines.append("  meta  : %s" % ", ".join(
            "%s=%s" % (k, v) for k, v in sorted(dir_map.meta.items())
        ))
    lines.append("")

    rows = []
    for key in dir_map.keys_sorted():
        path = dir_map.entries[key]
        status = ""
        if verify:
            status = ("ok" if os.path.isdir(os.path.expanduser(path))
                      else "path not found")
        rows.append([key, path, status])
    headers = ["index", "path"] + (["check"] if verify else [""])
    lines.append(render_table(headers, rows, max_col_width=80))

    if dir_map.warnings:
        lines.append("")
        lines.append("== warnings (%d) ==" % len(dir_map.warnings))
        for warning in dir_map.warnings:
            lines.append("  ! %s" % warning)
    return "\n".join(lines)


def render_index_specs(specs: Sequence[IndexSpec]) -> str:
    lines: List[str] = []
    lines.append("== index resource requirements ==")
    rows = []
    for spec in specs:
        rows.append([
            spec.index_key,
            str(spec.gds_count),
            str(spec.cpu_per_case),
            str(spec.slots),
            ",".join(spec.keywords) or "-",
            spec.error or ("; ".join(spec.warnings) if spec.warnings else "ok"),
        ])
    lines.append(render_table(
        ["index", "GDS", "cpu/case", "slots", "keywords", "state"],
        rows,
        aligns=["left", "right", "right", "right", "left", "left"],
        max_col_width=70,
    ))
    return "\n".join(lines)
