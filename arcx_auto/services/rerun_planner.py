"""Build a RerunPlan -- **pure**, and it deletes nothing.

Separating the plan from the execution is the whole safety story of this
feature. The plan says which directories would go and why, so it can be
printed, reviewed, stored and diffed before a single file is touched.

The delete rule (architecture 6.1): a case is kept only when QA is positive it
finished. INCOMPLETE and **UNKNOWN both delete**, because the costs are
asymmetric -- deleting something complete wastes one run and the result stays
correct, while keeping something incomplete ships a truncated result as a
success.

Arcx's own completion logic is complex, but it offers one guaranteed contract:
delete a case's run dir and `-keep_dir --run` redoes it. So the internals never
have to be understood; only the delete list does.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

from arcx_auto.domain.enums import CaseState, Completeness
from arcx_auto.domain.models import IndexRunSnapshot
from arcx_auto.domain.rerun import CaseDecision, RerunPlan
from arcx_auto.services.qa.runner import IndexQaReport
from arcx_auto.services.state_engine import classify_completeness


def build_rerun_plan(
    wave_dir: str,
    wave_name: str,
    snapshots: Sequence[IndexRunSnapshot],
    qa_reports: Sequence[IndexQaReport] = (),
    launch: Optional[Dict] = None,
    manifest: Optional[Dict] = None,
) -> RerunPlan:
    """Work out what a rerun of this wave would do.

    ``launch`` is the wave's launch.json and ``manifest`` its manifest.json.
    Anything missing from them becomes a blocker rather than an assumption:
    this is a destructive operation, so "not sure" stops (the one exception
    being the per-case delete decision, see the module docstring).
    """
    launch = launch or {}
    manifest = manifest or {}

    qa_by_index = {r.index_key: r for r in qa_reports}
    decisions: List[CaseDecision] = []
    for snapshot in snapshots:
        report = qa_by_index.get(snapshot.index_key)
        for case_id in sorted(snapshot.cases):
            decisions.append(
                _decide(snapshot, case_id, report))

    blockers: List[str] = []
    warnings: List[str] = []

    arcx_job_id = launch.get("arcx_job_id")
    if not arcx_job_id:
        # Without the parent's job id we cannot stop it, and a live parent will
        # simply submit replacements for everything we drain. Refusing is the
        # only safe answer: see the note in the CLI for how to proceed by hand.
        blockers.append(
            "launch.json has no arcx_job_id, so the parent Arcx job cannot be "
            "stopped; draining without stopping it lets it resubmit the work")

    index_keys = tuple(launch.get("index_keys")
                       or manifest.get("index_keys") or ())
    if not index_keys:
        blockers.append(
            "neither launch.json nor manifest.json lists the indices, so the "
            "rerun command cannot be rebuilt")

    arcx_cfg = (manifest.get("snapshots") or {}).get("arcx_cfg")
    if not arcx_cfg:
        candidate = os.path.join(wave_dir, "arcx.cfg")
        if os.path.isfile(candidate):
            arcx_cfg = candidate
        else:
            blockers.append("no arcx.cfg snapshot found in the wave directory")

    missing_dirs = [d.case_id for d in decisions if d.delete and not d.case_dir]
    if missing_dirs:
        warnings.append(
            "%d case(s) marked for rerun have no run dir on disk, so there is "
            "nothing to delete for them: %s"
            % (len(missing_dirs), ", ".join(sorted(missing_dirs)[:10])))

    uncertain = [d for d in decisions if d.uncertain]
    if uncertain:
        warnings.append(
            "%d case(s) could not be judged and will be deleted and rerun; "
            "review them if that is not what you want: %s"
            % (len(uncertain), ", ".join(sorted(d.case_id for d in uncertain)[:10])))

    if not any(d.delete for d in decisions):
        warnings.append("nothing needs rerunning; every case is complete")

    return RerunPlan(
        wave_dir=os.path.abspath(wave_dir),
        wave_name=wave_name,
        index_keys=index_keys,
        arcx_job_id=arcx_job_id,
        arcx_cfg=arcx_cfg,
        attempt=len(launch.get("attempts") or []) + 1,
        decisions=tuple(decisions),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


#: How strongly each verdict argues for deleting. Combining takes the maximum,
#: so neither source can talk the other into keeping something.
_RANK = {
    Completeness.COMPLETE: 0,
    Completeness.UNKNOWN: 1,
    Completeness.INCOMPLETE: 2,
}


def _decide(snapshot: IndexRunSnapshot, case_id: str,
            report: Optional[IndexQaReport]) -> CaseDecision:
    """Decide one case by combining two independent views.

    The state machine answers "did it reach the end", from markers and LSF.
    QA answers "are the artifacts right", from the files on disk.

    They are combined by taking whichever argues harder for deleting, because
    **neither may upgrade the other**:

      * QA alone cannot say a case is complete. A case that never reached a
        POST state has only had the LIVE checks run against it, and finding no
        issues there means "nothing looks wrong right now", not "it finished".
        A RUNNING case with a clean QA result is still unfinished.

      * The state machine alone cannot say a case is complete either. A
        .complete marker with no netlist behind it is exactly the false success
        this system exists to catch.

    So a case is kept only when both agree it is done.
    """
    case = snapshot.cases[case_id]

    completeness, reason = classify_completeness(case)
    if report is None:
        reason = "%s (no QA result; markers only)" % reason
    elif case_id in report.case_results:
        qa_completeness, qa_reason = report.merged_case_result(
            case_id).completeness()
        if _RANK[qa_completeness] > _RANK[completeness]:
            completeness, reason = qa_completeness, qa_reason

    return CaseDecision(
        index_key=snapshot.index_key,
        case_id=case_id,
        case_dir=case.case_dir,
        state=case.state,
        completeness=completeness,
        reason=reason,
    )
