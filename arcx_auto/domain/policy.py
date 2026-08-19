"""What the system would do about a problem. Pure data, no behaviour.

The policy engine answers one question per problem: **does this need a person?**
Almost always the answer is yes, and that is the design rather than a
limitation.

Three ideas hold this together.

**Shadow mode is the default, and it is not a testing convenience.** Deciding
and acting are separated permanently, so the engine can run for weeks recording
what it *would* have done while changing nothing. Turning automation on is then
a decision made against a log of real cases rather than against an intention.

**A budget is not a safety net, it is the safety mechanism.** Any automated
action can be triggered by a condition that repeats -- a broken cfg fails every
case identically -- so the interesting question is never "is this action safe"
but "what happens when it fires two hundred times". Every budget here answers
that.

**Escalation is a real outcome, not a failure to decide.** ESCALATE means the
system understood the problem well enough to know a person should look at it.
An engine that only counts automated actions as success ends up automating the
decisions it should not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class PolicyMode(Enum):
    """How much the engine is allowed to do.

    The order is the order these should ever be adopted in.
    """

    OFF = "off"            # do not even evaluate
    SHADOW = "shadow"      # evaluate and record; never act
    ACTIVE = "active"      # act, within budget

    @property
    def evaluates(self) -> bool:
        return self is not PolicyMode.OFF

    @property
    def acts(self) -> bool:
        return self is PolicyMode.ACTIVE


class ActionKind(Enum):
    """What could be done about an issue."""

    #: Stop, drain, clean the unfinished cases and resubmit the wave
    RERUN_WAVE = "rerun_wave"
    #: Put it in front of a person, with the evidence
    ESCALATE = "escalate"
    #: Known, understood, and not worth anybody's time
    IGNORE = "ignore"

    @property
    def is_automatic(self) -> bool:
        return self is ActionKind.RERUN_WAVE


class IssueClass(Enum):
    """Why an issue happens, which decides whether retrying can ever help.

    This is the distinction that matters most. A transient failure and a setup
    failure look identical in a log, and retrying the second one fails
    identically a hundred times while burning turnaround and machines.
    """

    TRANSIENT = "transient"      # licence blip, host down, stale NFS handle
    INFRA = "infra"              # memlimit, runlimit, disk full
    SETUP = "setup"              # wrong path, cfg syntax, missing layer map
    TOOL = "tool"                # segfault, internal error
    VERIFY_FAIL = "verify_fail"  # a false success
    UNKNOWN = "unknown"          # no rule matched


@dataclass(frozen=True)
class PolicyRule:
    """What to do about one issue id."""

    action: ActionKind = ActionKind.ESCALATE
    issue_class: IssueClass = IssueClass.UNKNOWN
    #: How many times this may be handled automatically for one wave, ever
    max_auto: int = 0
    note: str = ""


@dataclass(frozen=True)
class Budgets:
    """The limits that decide what happens when an action repeats.

    Every one of these exists because an automated action's real risk is not
    doing the wrong thing once, it is doing the right thing two hundred times.
    """

    max_auto_actions_per_run: int = 20
    cooldown_sec: float = 900.0
    #: A burst of one issue id means a systemic cause, not N independent faults
    same_issue_burst_limit: int = 5
    #: Set by a human to stop everything without editing rules
    global_kill_switch: bool = False


@dataclass(frozen=True)
class ActionHistory:
    """What automation has already done, so budgets can be applied.

    Read from the policy journal rather than held in memory: a daemon restart
    must not hand the engine a fresh budget.
    """

    #: (wave, issue_id) -> how many times acted on automatically
    per_issue: Dict[Tuple[str, str], int] = field(default_factory=dict)
    total_actions: int = 0
    last_action_at: Optional[float] = None

    def count_for(self, wave: str, issue_id: str) -> int:
        return self.per_issue.get((wave, issue_id), 0)

    def since_last_action(self, now: float) -> Optional[float]:
        if self.last_action_at is None:
            return None
        return max(0.0, now - self.last_action_at)


@dataclass(frozen=True)
class Decision:
    """What the engine decided about one group of issues, and why.

    ``reason`` is written for a person reading the journal months later, so it
    always names the rule and the budget that applied -- "escalate" on its own
    is not an answer to "why did nothing happen".
    """

    issue_id: str
    action: ActionKind
    issue_class: IssueClass
    reason: str
    wave: str = ""
    run_id: str = ""
    targets: Tuple[str, ...] = ()
    severity: str = ""
    #: What it would have done had no budget stopped it
    intended_action: Optional[ActionKind] = None
    #: True when the mode meant this was recorded but not performed
    shadowed: bool = False
    ts: float = 0.0

    @property
    def blocked_by_budget(self) -> bool:
        return (self.intended_action is not None
                and self.intended_action != self.action)

    @property
    def would_act(self) -> bool:
        """Whether automation was called for, whatever actually happened."""
        return (self.intended_action or self.action).is_automatic

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "run_id": self.run_id,
            "wave": self.wave,
            "issue_id": self.issue_id,
            "action": self.action.value,
            "intended_action": (self.intended_action.value
                                if self.intended_action else None),
            "issue_class": self.issue_class.value,
            "severity": self.severity,
            "reason": self.reason,
            "targets": list(self.targets),
            "shadowed": self.shadowed,
        }


@dataclass(frozen=True)
class PolicyOutcome:
    """Everything one evaluation produced."""

    decisions: Tuple[Decision, ...] = ()
    mode: PolicyMode = PolicyMode.SHADOW
    evaluated_at: float = 0.0

    @property
    def automatic(self) -> Tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.action.is_automatic)

    @property
    def escalations(self) -> Tuple[Decision, ...]:
        return tuple(d for d in self.decisions
                     if d.action == ActionKind.ESCALATE)

    @property
    def blocked(self) -> Tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.blocked_by_budget)
