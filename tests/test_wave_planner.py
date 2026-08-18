"""WavePlanner: wave splitting. Pure function tests."""

import unittest

from arcx_auto.domain.enums import PlanMode
from arcx_auto.domain.models import IndexSpec
from arcx_auto.services.wave_planner import plan_waves, regroup_manual


def spec(key, gds, cpu, keywords=(), error=None):
    return IndexSpec(
        index_key=key, path="/p/%s" % key, gds_count=gds, cpu_per_case=cpu,
        keywords=tuple(keywords), priority=1 if keywords else 0, error=error,
    )


class FillTest(unittest.TestCase):
    def test_single_wave_when_under_limit(self):
        plan = plan_waves([spec("a", 2, 4), spec("b", 3, 4)], max_slots_per_wave=100)
        self.assertEqual(len(plan.waves), 1)
        self.assertEqual(plan.waves[0].total_slots, 20)

    def test_splits_when_over_limit(self):
        plan = plan_waves(
            [spec("a", 10, 4), spec("b", 10, 4), spec("c", 10, 4)],
            max_slots_per_wave=80,
        )
        self.assertEqual(len(plan.waves), 2)
        self.assertEqual(plan.waves[0].index_keys, ("a", "b"))
        self.assertEqual(plan.waves[1].index_keys, ("c",))

    def test_wave_names_are_sequential(self):
        plan = plan_waves([spec("a", 10, 4), spec("b", 10, 4)],
                          max_slots_per_wave=40)
        self.assertEqual([w.name for w in plan.waves], ["wave_001", "wave_002"])

    def test_totals(self):
        plan = plan_waves([spec("a", 3, 4), spec("b", 5, 2)],
                          max_slots_per_wave=100)
        self.assertEqual(plan.total_cases, 8)
        self.assertEqual(plan.total_slots, 22)


class PriorityTest(unittest.TestCase):
    def test_keyword_indices_go_first(self):
        """Indices whose path hits a keyword (sram, ro) land in earlier waves."""
        plan = plan_waves(
            [spec("plain", 5, 4), spec("sram_a", 5, 4, ("sram",))],
            max_slots_per_wave=20,
        )
        self.assertEqual(plan.waves[0].index_keys, ("sram_a",))
        self.assertEqual(plan.waves[1].index_keys, ("plain",))

    def test_user_order_preserved_within_same_priority(self):
        """Stable sort: selection order is preserved within a priority.

        The order an engineer ticked things in is explicit intent, and
        reordering makes the result unpredictable.
        """
        plan = plan_waves(
            [spec("c", 1, 1), spec("a", 1, 1), spec("b", 1, 1)],
            max_slots_per_wave=100,
        )
        self.assertEqual(plan.waves[0].index_keys, ("c", "a", "b"))


class OversizeTest(unittest.TestCase):
    def test_oversized_index_gets_its_own_wave(self):
        """An index over the cap gets its own wave: it can still run, and it
        does not drag the others down.
        """
        plan = plan_waves(
            [spec("small", 1, 4), spec("huge", 100, 8)],
            max_slots_per_wave=50,
        )
        huge_wave = [w for w in plan.waves if "huge" in w.index_keys][0]
        self.assertEqual(huge_wave.index_keys, ("huge",))

    def test_oversized_wave_produces_warning(self):
        plan = plan_waves([spec("huge", 100, 8)], max_slots_per_wave=50)
        self.assertEqual(len(plan.oversized_waves), 1)
        self.assertTrue(any("over the cap" in w for w in plan.warnings), plan.warnings)


class ExclusionTest(unittest.TestCase):
    def test_unusable_index_excluded_not_dropped(self):
        """Indices with incomplete data go to `excluded` with a reason; never
        silently dropped.
        """
        plan = plan_waves(
            [spec("ok", 2, 4), spec("bad", 0, 0, error="no GDS")],
            max_slots_per_wave=100,
        )
        self.assertEqual([s.index_key for s in plan.excluded], ["bad"])
        self.assertTrue(any("bad" in w for w in plan.warnings))
        self.assertEqual(plan.waves[0].index_keys, ("ok",))

    def test_all_unusable_yields_no_waves(self):
        plan = plan_waves([spec("bad", 0, 0, error="x")], max_slots_per_wave=100)
        self.assertEqual(plan.waves, ())
        self.assertEqual(len(plan.excluded), 1)

    def test_empty_input(self):
        plan = plan_waves([], max_slots_per_wave=100)
        self.assertEqual(plan.waves, ())


class ModeTest(unittest.TestCase):
    def test_off_mode_produces_single_wave(self):
        """OFF is just the "single wave" case; nothing downstream branches."""
        plan = plan_waves(
            [spec("a", 100, 8), spec("b", 100, 8)],
            max_slots_per_wave=10, mode=PlanMode.OFF,
        )
        self.assertEqual(len(plan.waves), 1)
        self.assertEqual(plan.waves[0].index_keys, ("a", "b"))

    def test_invalid_limit_falls_back_to_off(self):
        plan = plan_waves([spec("a", 1, 1), spec("b", 1, 1)], max_slots_per_wave=0)
        self.assertEqual(len(plan.waves), 1)
        self.assertTrue(any("invalid" in w for w in plan.warnings))


class ManualTest(unittest.TestCase):
    def test_manual_grouping(self):
        specs = [spec("a", 1, 1), spec("b", 1, 1), spec("c", 1, 1)]
        plan = regroup_manual(specs, [["a", "c"], ["b"]], max_slots_per_wave=100)
        self.assertEqual(plan.mode, PlanMode.MANUAL)
        self.assertEqual(plan.waves[0].index_keys, ("a", "c"))
        self.assertEqual(plan.waves[1].index_keys, ("b",))

    def test_index_cannot_appear_in_two_waves(self):
        """One index in two waves would make Arcx process the same GDS files
        in two isolated directories at once.
        """
        specs = [spec("a", 1, 1)]
        plan = regroup_manual(specs, [["a"], ["a"]], max_slots_per_wave=100)
        self.assertEqual(len(plan.waves), 1)
        self.assertTrue(any("duplicate" in w for w in plan.warnings), plan.warnings)

    def test_unassigned_index_is_warned_not_dropped(self):
        specs = [spec("a", 1, 1), spec("b", 1, 1)]
        plan = regroup_manual(specs, [["a"]], max_slots_per_wave=100)
        self.assertTrue(any("not assigned" in w for w in plan.warnings), plan.warnings)

    def test_unknown_index_is_warned(self):
        plan = regroup_manual([spec("a", 1, 1)], [["a", "zzz"]],
                              max_slots_per_wave=100)
        self.assertTrue(any("unknown index" in w for w in plan.warnings), plan.warnings)

    def test_manual_still_checks_oversize(self):
        """A hand-made grouping can be over the cap too, and still deserves
        the warning.
        """
        plan = regroup_manual([spec("a", 100, 8)], [["a"]], max_slots_per_wave=50)
        self.assertTrue(any("over the cap" in w for w in plan.warnings), plan.warnings)


if __name__ == "__main__":
    unittest.main()
