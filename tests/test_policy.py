"""Phase 4: the policy engine, in shadow mode.

Almost every test here is about **not** acting. That is the design: the risk of
an automated action is never doing the wrong thing once, it is doing the right
thing two hundred times, so the budgets are the safety mechanism rather than a
safety net around one.

The engine is pure, so "what happens on the sixth failure, eleven minutes after
the last action, with the kill switch off" is a function call rather than an
afternoon.
"""

import os
import tempfile
import unittest

from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import Settings
from arcx_auto.domain.enums import IssueScope, IssueStage, Severity
from arcx_auto.domain.policy import (
    ActionHistory,
    ActionKind,
    IssueClass,
    PolicyMode,
)
from arcx_auto.domain.qa import Issue
from arcx_auto.services.policy import evaluate_policy, history_from_records


def issue(issue_id="NETLIST_MISSING", severity=Severity.FATAL, case="NTN_1"):
    return Issue(id=issue_id, severity=severity, message="broken",
                 scope=IssueScope.CASE, stage=IssueStage.POST,
                 index_key="1000", case_id=case)


def settings_with(rules=None, **budget):
    settings = Settings().policy
    settings.mode = budget.pop("mode", "active")
    if rules is not None:
        settings.rules = rules
    for key, value in budget.items():
        setattr(settings, key, value)
    return settings


AUTO = {"NETLIST_MISSING": {"action": "rerun_wave", "class": "tool",
                            "max_auto": 1}}


class ModeTest(unittest.TestCase):
    def test_shadow_is_the_default(self):
        self.assertEqual(Settings().policy.resolved_mode(), PolicyMode.SHADOW)

    def test_off_evaluates_nothing(self):
        outcome = evaluate_policy([issue()], settings_with(AUTO, mode="off"))
        self.assertEqual(outcome.decisions, ())

    def test_shadow_records_the_decision_it_would_have_made(self):
        outcome = evaluate_policy([issue()], settings_with(AUTO, mode="shadow"))
        decision = outcome.decisions[0]
        self.assertEqual(decision.action, ActionKind.RERUN_WAVE)
        self.assertTrue(decision.shadowed)
        self.assertIn("shadow mode", decision.reason)

    def test_an_unreadable_mode_falls_back_to_shadow_not_active(self):
        """A typo in the mode must not silently start acting."""
        self.assertEqual(
            settings_with(AUTO, mode="acive").resolved_mode(),
            PolicyMode.SHADOW)

    def test_active_acts(self):
        outcome = evaluate_policy([issue()], settings_with(AUTO))
        self.assertFalse(outcome.decisions[0].shadowed)
        self.assertEqual(outcome.decisions[0].action, ActionKind.RERUN_WAVE)


class RuleTest(unittest.TestCase):
    def test_an_unknown_issue_escalates(self):
        """An issue nobody has written a rule for is the last thing that
        should be handled automatically.
        """
        outcome = evaluate_policy([issue("NEVER_SEEN")], settings_with(AUTO))
        self.assertEqual(outcome.decisions[0].action, ActionKind.ESCALATE)
        self.assertEqual(outcome.decisions[0].issue_class, IssueClass.UNKNOWN)

    def test_the_shipped_rules_never_act(self):
        """Everything ships as escalate. Automation is opted into per rule,
        after the shadow log says it is safe.
        """
        settings = Settings().policy
        settings.mode = "active"
        for issue_id in settings.rules:
            outcome = evaluate_policy([issue(issue_id)], settings)
            self.assertEqual(
                outcome.decisions[0].action, ActionKind.ESCALATE, issue_id)

    def test_verify_failures_are_classified_as_such(self):
        outcome = evaluate_policy(
            [issue("NETLIST_NO_SIGNATURE")], Settings().policy)
        self.assertEqual(outcome.decisions[0].issue_class,
                         IssueClass.VERIFY_FAIL)

    def test_a_typo_in_an_action_does_not_become_an_action(self):
        outcome = evaluate_policy(
            [issue()],
            settings_with({"NETLIST_MISSING": {"action": "reurn_wave"}}))
        self.assertEqual(outcome.decisions[0].action, ActionKind.ESCALATE)

    def test_ignore_is_recorded_as_a_decision(self):
        outcome = evaluate_policy(
            [issue()],
            settings_with({"NETLIST_MISSING": {"action": "ignore",
                                               "note": "known noise"}}))
        self.assertEqual(outcome.decisions[0].action, ActionKind.IGNORE)
        self.assertIn("known noise", outcome.decisions[0].reason)

    def test_warnings_are_not_asking_for_a_decision(self):
        """A WARN is information for a person. Feeding routine noise into the
        automation path is how a budget gets spent on nothing.
        """
        outcome = evaluate_policy(
            [issue(severity=Severity.WARN)], settings_with(AUTO))
        self.assertEqual(outcome.decisions, ())

    def test_unknown_severity_does_ask_for_a_decision(self):
        """"I could not check" denies success, so it needs an answer."""
        outcome = evaluate_policy(
            [issue(severity=Severity.UNKNOWN)], settings_with(AUTO))
        self.assertEqual(len(outcome.decisions), 1)


class GroupingTest(unittest.TestCase):
    def test_one_cause_is_one_decision(self):
        """Two hundred cases hitting one broken cfg is one decision, not two
        hundred: acting on each would multiply one problem into 200 reruns.
        """
        issues = [issue(case="C%d" % i) for i in range(200)]
        outcome = evaluate_policy(issues, settings_with(AUTO))
        self.assertEqual(len(outcome.decisions), 1)
        self.assertEqual(len(outcome.decisions[0].targets), 200)

    def test_different_ids_are_different_decisions(self):
        outcome = evaluate_policy(
            [issue("NETLIST_MISSING"), issue("NETLIST_EMPTY")],
            settings_with(AUTO))
        self.assertEqual(len(outcome.decisions), 2)


class BudgetTest(unittest.TestCase):
    """Each of these exists because an automated action can repeat."""

    def test_the_kill_switch_beats_every_rule(self):
        outcome = evaluate_policy(
            [issue()], settings_with(AUTO, global_kill_switch=True))
        decision = outcome.decisions[0]
        self.assertEqual(decision.action, ActionKind.ESCALATE)
        self.assertTrue(decision.blocked_by_budget)
        self.assertIn("kill switch", decision.reason)

    def test_a_burst_of_one_id_is_a_shared_cause(self):
        """This is the guard that stops one broken cfg becoming N reruns."""
        issues = [issue(case="C%d" % i) for i in range(6)]
        outcome = evaluate_policy(
            [*issues], settings_with(AUTO, same_issue_burst_limit=5))
        decision = outcome.decisions[0]
        self.assertEqual(decision.action, ActionKind.ESCALATE)
        self.assertIn("shared cause", decision.reason)

    def test_a_burst_at_the_limit_is_still_allowed(self):
        issues = [issue(case="C%d" % i) for i in range(5)]
        outcome = evaluate_policy(
            issues, settings_with(AUTO, same_issue_burst_limit=5))
        self.assertEqual(outcome.decisions[0].action, ActionKind.RERUN_WAVE)

    def test_max_auto_per_issue_is_honoured(self):
        history = ActionHistory(per_issue={("w1", "NETLIST_MISSING"): 1})
        outcome = evaluate_policy(
            [issue()], settings_with(AUTO), history=history, wave="w1")
        self.assertEqual(outcome.decisions[0].action, ActionKind.ESCALATE)
        self.assertIn("already been handled", outcome.decisions[0].reason)

    def test_the_per_issue_count_is_per_wave(self):
        """A budget spent on one wave must not silence another."""
        history = ActionHistory(per_issue={("w1", "NETLIST_MISSING"): 1})
        outcome = evaluate_policy(
            [issue()], settings_with(AUTO), history=history, wave="w2")
        self.assertEqual(outcome.decisions[0].action, ActionKind.RERUN_WAVE)

    def test_the_run_budget_is_honoured(self):
        history = ActionHistory(total_actions=20)
        outcome = evaluate_policy(
            [issue()], settings_with(AUTO, max_auto_actions_per_run=20),
            history=history)
        self.assertIn("used its 20", outcome.decisions[0].reason)

    def test_the_cooldown_is_honoured(self):
        history = ActionHistory(last_action_at=1000.0)
        outcome = evaluate_policy(
            [issue()], settings_with(AUTO, cooldown_sec=900.0),
            history=history, now=1100.0)
        self.assertEqual(outcome.decisions[0].action, ActionKind.ESCALATE)
        self.assertIn("cooldown", outcome.decisions[0].reason)

    def test_after_the_cooldown_it_may_act(self):
        history = ActionHistory(last_action_at=1000.0)
        outcome = evaluate_policy(
            [issue()], settings_with(AUTO, cooldown_sec=900.0),
            history=history, now=2000.0)
        self.assertEqual(outcome.decisions[0].action, ActionKind.RERUN_WAVE)

    def test_the_budget_is_spent_within_one_evaluation(self):
        """Two actionable ids with one action left: the second must not slip
        through because both were judged against the same starting balance.
        """
        rules = {
            "A": {"action": "rerun_wave", "max_auto": 5},
            "B": {"action": "rerun_wave", "max_auto": 5},
        }
        outcome = evaluate_policy(
            [issue("A"), issue("B")],
            settings_with(rules, max_auto_actions_per_run=1))
        actions = sorted(d.action.value for d in outcome.decisions)
        self.assertEqual(actions, ["escalate", "rerun_wave"])

    def test_shadow_mode_spends_no_budget(self):
        """Nothing happened, so nothing may be counted as having happened."""
        rules = {"A": {"action": "rerun_wave", "max_auto": 5},
                 "B": {"action": "rerun_wave", "max_auto": 5}}
        outcome = evaluate_policy(
            [issue("A"), issue("B")],
            settings_with(rules, mode="shadow", max_auto_actions_per_run=1))
        self.assertEqual(
            [d.action for d in outcome.decisions],
            [ActionKind.RERUN_WAVE, ActionKind.RERUN_WAVE])

    def test_a_blocked_decision_still_says_what_it_wanted(self):
        outcome = evaluate_policy(
            [issue()], settings_with(AUTO, global_kill_switch=True))
        decision = outcome.decisions[0]
        self.assertEqual(decision.intended_action, ActionKind.RERUN_WAVE)
        self.assertTrue(decision.would_act)


class HistoryFromJournalTest(unittest.TestCase):
    """Budgets are rebuilt from the journal, not held in memory.

    A daemon restart handing the engine a fresh budget is the failure that
    turns a budget into a suggestion.
    """

    def test_performed_actions_count(self):
        history = history_from_records([
            {"wave": "w1", "issue_id": "A", "action": "rerun_wave",
             "shadowed": False, "ts": 100.0},
            {"wave": "w1", "issue_id": "A", "action": "rerun_wave",
             "shadowed": False, "ts": 200.0},
        ])
        self.assertEqual(history.count_for("w1", "A"), 2)
        self.assertEqual(history.total_actions, 2)
        self.assertEqual(history.last_action_at, 200.0)

    def test_shadow_entries_never_consume_a_budget(self):
        """They are the record of a decision, not of anything happening."""
        history = history_from_records([
            {"wave": "w1", "issue_id": "A", "action": "rerun_wave",
             "shadowed": True, "ts": 100.0},
        ])
        self.assertEqual(history.total_actions, 0)
        self.assertIsNone(history.last_action_at)

    def test_escalations_consume_nothing(self):
        history = history_from_records([
            {"wave": "w1", "issue_id": "A", "action": "escalate",
             "shadowed": False, "ts": 100.0},
        ])
        self.assertEqual(history.total_actions, 0)

    def test_a_damaged_record_does_not_break_the_rebuild(self):
        history = history_from_records([
            {"nonsense": True},
            {"wave": "w1", "issue_id": "A", "action": "rerun_wave",
             "shadowed": False, "ts": 100.0},
        ])
        self.assertEqual(history.total_actions, 1)


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(self.tmp.name, "demo")
        self.store.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_decisions_round_trip(self):
        outcome = evaluate_policy([issue()], settings_with(AUTO), wave="w1")
        self.store.append_policy(d.as_dict() for d in outcome.decisions)
        records = self.store.read_policy()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["issue_id"], "NETLIST_MISSING")
        self.assertEqual(records[0]["action"], "rerun_wave")

    def test_the_journal_is_separate_from_the_audit(self):
        """audit.jsonl records what was *done*, and in shadow mode nothing was.
        Mixing them makes "what has this system actually changed"
        unanswerable.
        """
        outcome = evaluate_policy(
            [issue()], settings_with(AUTO, mode="shadow"))
        self.store.append_policy(d.as_dict() for d in outcome.decisions)
        self.assertTrue(os.path.isfile(self.store.policy_path))
        self.assertFalse(os.path.exists(self.store.audit_path))

    def test_budgets_survive_a_restart(self):
        outcome = evaluate_policy([issue()], settings_with(AUTO), wave="w1")
        self.store.append_policy(d.as_dict() for d in outcome.decisions)

        # A fresh process, rebuilding history from disk
        history = history_from_records(RunStore(self.tmp.name, "demo")
                                       .read_policy())
        again = evaluate_policy([issue()], settings_with(AUTO),
                                history=history, wave="w1")
        self.assertEqual(again.decisions[0].action, ActionKind.ESCALATE)


class DaemonPolicyTest(unittest.TestCase):
    """The daemon decides every tick and journals what is new."""

    def setUp(self):
        from tests.fixtures.fake_run import build_demo

        self.tmp = tempfile.TemporaryDirectory()
        self.demo = build_demo(os.path.join(self.tmp.name, "demo"))
        self.settings = Settings()
        self.settings.state_root = os.path.join(self.tmp.name, "state")
        self.settings.export.shared_root = os.path.join(self.tmp.name, "shared")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, ticks=1):
        from arcx_auto.daemon.loop import Daemon, DaemonOptions

        Daemon(
            DaemonOptions(run_id="t", wave_dirs=[self.demo["wave_dir"]],
                          use_lsf=False, once=(ticks == 1),
                          max_ticks=ticks,
                          # Without this the loop sleeps a real poll interval
                          # between ticks, so a three tick test takes a minute.
                          interval_sec=0.01),
            settings=self.settings,
        ).run()
        return RunStore(self.settings.expanded_state_root(), "t").read_policy()

    def test_the_daemon_journals_its_decisions(self):
        records = self._run()
        self.assertTrue(records)
        self.assertTrue(all(r["shadowed"] for r in records))

    def test_the_same_problem_is_not_journalled_every_tick(self):
        """The same broken case is present on every tick, and a line each time
        would bury the log under thousands of identical rows.
        """
        once = len(self._run(ticks=1))
        self.tearDown()
        self.setUp()
        twice = len(self._run(ticks=3))
        self.assertEqual(once, twice)

    def test_shadow_mode_writes_no_audit_entry(self):
        """Nothing was done, so nothing may appear in the record of what was
        done.
        """
        self._run()
        store = RunStore(self.settings.expanded_state_root(), "t")
        actions = [r.get("action") for r in
                   __import__("arcx_auto.util.atomic", fromlist=["iter_jsonl"])
                   .iter_jsonl(store.audit_path)]
        self.assertNotIn("rerun_wave", actions)

    def test_the_engine_can_be_turned_off(self):
        self.settings.policy.mode = "off"
        self.assertEqual(self._run(), [])


if __name__ == "__main__":
    unittest.main()
