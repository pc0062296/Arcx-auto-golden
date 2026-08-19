"""The policy engine -- **completely pure**, zero I/O.

    in:  issues + what automation has already done + rules and budgets
    out: one Decision per issue id, each carrying the reason it was made

Two properties follow from being pure, and both matter more than they look.

**Every budget can be tested without waiting.** "What happens on the sixth
consecutive failure, eleven minutes after the last action, when the kill switch
is off" is a function call, not an afternoon.

**Deciding cannot accidentally act.** The engine has no way to reach LSF or the
filesystem, so shadow mode is not a flag that something might forget to check
-- it is the absence of a capability. Execution lives in the Remediator, behind
a separate explicit call.

The order of the guards below is deliberate: kill switch, then mode, then rule,
then budget. A person turning off the kill switch expects that to win over
every rule in the file.
"""

from __future__ import annotations

import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from arcx_auto.config.settings import PolicySettings
from arcx_auto.domain.policy import (
    ActionHistory,
    ActionKind,
    Budgets,
    Decision,
    IssueClass,
    PolicyMode,
    PolicyOutcome,
    PolicyRule,
)
from arcx_auto.domain.qa import Issue


def evaluate_policy(
    issues: Sequence[Issue],
    settings: PolicySettings,
    history: Optional[ActionHistory] = None,
    run_id: str = "",
    wave: str = "",
    now: Optional[float] = None,
) -> PolicyOutcome:
    """Decide what to do about every issue that is asking for a decision.

    Issues are grouped by id first. Two hundred cases hitting one broken cfg is
    **one** decision, not two hundred: the cause is shared, so acting on each
    separately would multiply one problem into two hundred reruns.
    """
    now = now if now is not None else time.time()
    history = history or ActionHistory()
    mode = settings.resolved_mode()

    if not mode.evaluates:
        return PolicyOutcome(mode=mode, evaluated_at=now)

    grouped = _group(issues)
    decisions: List[Decision] = []
    budget_used = history.total_actions

    for issue_id in sorted(grouped):
        members = grouped[issue_id]
        rule = settings.rule_for(issue_id)
        decision, spent = _decide_one(
            issue_id=issue_id,
            members=members,
            rule=rule,
            budgets=settings.budgets(),
            history=history,
            budget_used=budget_used,
            mode=mode,
            run_id=run_id,
            wave=wave,
            now=now,
        )
        decisions.append(decision)
        budget_used += spent

    return PolicyOutcome(decisions=tuple(decisions), mode=mode,
                         evaluated_at=now)


def _decide_one(
    issue_id: str,
    members: Sequence[Issue],
    rule: PolicyRule,
    budgets: Budgets,
    history: ActionHistory,
    budget_used: int,
    mode: PolicyMode,
    run_id: str,
    wave: str,
    now: float,
) -> Tuple[Decision, int]:
    """Decide one issue id. Returns (decision, budget consumed)."""
    targets = tuple(sorted({(i.case_id or i.index_key or "?") for i in members}))
    severity = _worst_severity(members)

    def build(action: ActionKind, reason: str,
              intended: Optional[ActionKind] = None) -> Decision:
        return Decision(
            issue_id=issue_id,
            action=action,
            issue_class=rule.issue_class,
            reason=reason,
            wave=wave,
            run_id=run_id,
            targets=targets,
            severity=severity,
            intended_action=intended,
            shadowed=not mode.acts,
            ts=now,
        )

    if rule.action == ActionKind.IGNORE:
        return build(ActionKind.IGNORE,
                     "rule says ignore%s"
                     % (": " + rule.note if rule.note else "")), 0

    if not rule.action.is_automatic:
        return build(ActionKind.ESCALATE,
                     "rule says escalate%s"
                     % (": " + rule.note if rule.note else "")), 0

    # -- from here the rule wants to act, so every budget applies ---------
    intended = rule.action

    if budgets.global_kill_switch:
        return build(ActionKind.ESCALATE,
                     "the kill switch is on, so nothing is done automatically",
                     intended), 0

    if len(targets) > budgets.same_issue_burst_limit:
        # One id hitting many cases at once is a shared cause, and a shared
        # cause is not fixed by rerunning. This is the guard that stops one
        # broken cfg from becoming two hundred reruns.
        return build(
            ActionKind.ESCALATE,
            "%d cases hit %s at once, over the burst limit of %d; a burst that "
            "size is one shared cause, which rerunning does not fix"
            % (len(targets), issue_id, budgets.same_issue_burst_limit),
            intended), 0

    already = history.count_for(wave, issue_id)
    if already >= rule.max_auto:
        return build(
            ActionKind.ESCALATE,
            "%s has already been handled automatically %d time(s) for this "
            "wave, and the rule allows %d"
            % (issue_id, already, rule.max_auto),
            intended), 0

    if budget_used >= budgets.max_auto_actions_per_run:
        return build(
            ActionKind.ESCALATE,
            "this run has used its %d automatic action(s)"
            % budgets.max_auto_actions_per_run,
            intended), 0

    waited = history.since_last_action(now)
    if waited is not None and waited < budgets.cooldown_sec:
        return build(
            ActionKind.ESCALATE,
            "only %.0f seconds since the last automatic action; the cooldown "
            "is %.0f" % (waited, budgets.cooldown_sec),
            intended), 0

    if not mode.acts:
        # Shadow: the answer is recorded exactly as it would have been, and
        # the budget is not spent, because nothing happened.
        return build(intended,
                     "would %s (%s); shadow mode, so nothing was done"
                     % (intended.value, rule.issue_class.value)), 0

    return build(intended,
                 "%s allowed: attempt %d of %d for this wave"
                 % (intended.value, already + 1, rule.max_auto)), 1


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _group(issues: Iterable[Issue]) -> Dict[str, List[Issue]]:
    """Group by issue id.

    Only issues that deny success are considered. A WARN is information for a
    person, not a request for a decision -- treating it as one would put
    routine noise into the automation path.
    """
    grouped: Dict[str, List[Issue]] = {}
    for issue in issues:
        if not issue.blocks_success:
            continue
        grouped.setdefault(issue.id, []).append(issue)
    return grouped


def _worst_severity(issues: Sequence[Issue]) -> str:
    for name in ("FATAL", "UNKNOWN", "WARN", "INFO"):
        if any(i.severity.value == name for i in issues):
            return name
    return ""


def history_from_records(
    records: Iterable[Dict], now: Optional[float] = None
) -> ActionHistory:
    """Rebuild the budget history from the policy journal.

    Read from disk rather than kept in memory on purpose: restarting the daemon
    must not hand the engine a fresh budget, which is exactly the failure that
    turns a budget into a suggestion.

    Only performed actions count. Shadow entries are the record of a decision,
    not of anything having happened, so they must never consume a budget.
    """
    per_issue: Dict[Tuple[str, str], int] = {}
    total = 0
    last_at: Optional[float] = None

    for record in records:
        if record.get("shadowed"):
            continue
        action = record.get("action")
        if action != ActionKind.RERUN_WAVE.value:
            continue
        key = (record.get("wave") or "", record.get("issue_id") or "")
        per_issue[key] = per_issue.get(key, 0) + 1
        total += 1
        ts = record.get("ts")
        if isinstance(ts, (int, float)) and (last_at is None or ts > last_at):
            last_at = float(ts)

    return ActionHistory(per_issue=per_issue, total_actions=total,
                         last_action_at=last_at)
