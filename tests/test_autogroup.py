"""Forming groups from a directory: the rules, and what they say about why.

The proposal is only useful if it can be checked. Every test here asserts the
reason as well as the verdict, because an index that quietly does not run is
the failure this whole system exists to prevent -- and an automatic grouping is
a new way to produce exactly that.
"""

import os
import tempfile
import unittest

from arcx_auto.config.settings import AutoGroupSettings, Settings
from arcx_auto.services.autogroup import (
    CornerCfg,
    IndexFacts,
    canonical_corner,
    corner_from_path,
    discover_cfgs,
    excluded_by_glob,
    plan_auto_groups,
    read_facts,
    scan_directory,
)
from tests.fixtures.fake_run import make_arcx_cfg, make_dir_map, make_index_source


def cfg(path, suffix, canonical=None):
    return CornerCfg(path=path, suffix=suffix,
                     canonical=canonical or suffix.lower())


class CornerFromPathTest(unittest.TestCase):
    def test_the_component_after_the_marker(self):
        self.assertEqual(
            corner_from_path("/proj/a/corner_v2g/Cbest_T", "corner_v2g"),
            "Cbest_T")

    def test_it_is_not_always_the_last_component(self):
        """An index sits further down; the corner is still the one after
        the marker.
        """
        self.assertEqual(
            corner_from_path("/proj/a/corner_v2g/Cbest_T/index1000/work",
                             "corner_v2g"),
            "Cbest_T")

    def test_no_marker_means_no_corner(self):
        self.assertEqual(corner_from_path("/proj/a/plain/index1000",
                                          "corner_v2g"), "")

    def test_the_marker_as_the_last_component_names_nothing(self):
        self.assertEqual(corner_from_path("/proj/a/corner_v2g", "corner_v2g"),
                         "")

    def test_the_marker_is_matched_case_insensitively(self):
        self.assertEqual(corner_from_path("/proj/CORNER_V2G/Cbest_T",
                                          "corner_v2g"), "Cbest_T")


class CanonicalCornerTest(unittest.TestCase):
    ALIASES = {"Cbest_T": ["cbest_t", "cbt", "c_best_t"],
               "Cworst_T": ["cwt"]}

    def test_members_of_a_group_are_one_corner(self):
        names = ["Cbest_T", "cbest_t", "cbt", "c_best_t", "CBT"]
        canonical = {canonical_corner(n, self.ALIASES) for n in names}
        self.assertEqual(canonical, {"Cbest_T"})

    def test_different_groups_stay_apart(self):
        self.assertNotEqual(canonical_corner("cbt", self.ALIASES),
                            canonical_corner("cwt", self.ALIASES))

    def test_an_unlisted_name_is_its_own_corner(self):
        """An unconfigured setup still matches names spelled with different
        case, which is the common half of the problem.
        """
        self.assertEqual(canonical_corner("Whot_T", self.ALIASES),
                         canonical_corner("whot_t", self.ALIASES))


class ExcludeGlobTest(unittest.TestCase):
    GLOBS = ["*_old", "*bak", "*backup", "*back"]

    def test_a_directory_anywhere_above_the_index_excludes_it(self):
        self.assertEqual(
            excluded_by_glob("1000", "/proj/run_old/corner_v2g/Cbest_T",
                             self.GLOBS),
            "*_old")

    def test_the_index_key_is_matched_too(self):
        self.assertEqual(excluded_by_glob("1000_old", "/proj/a", self.GLOBS),
                         "*_old")

    def test_a_normal_path_is_not_excluded(self):
        self.assertIsNone(
            excluded_by_glob("1000", "/proj/chipA/corner_v2g/Cbest_T",
                             self.GLOBS))

    def test_a_pattern_cannot_match_half_a_component(self):
        """Components, not the whole path: otherwise *bak would match
        /proj/bakery/... only by luck of where the slashes fall.
        """
        self.assertIsNone(excluded_by_glob("1000", "/proj/bak_data/live",
                                           ["*bak"]))
        self.assertEqual(excluded_by_glob("1000", "/proj/data_bak/live",
                                          ["*bak"]), "*bak")

    def test_matching_ignores_case(self):
        self.assertEqual(excluded_by_glob("1000", "/proj/RUN_OLD/x",
                                          self.GLOBS), "*_old")


class DiscoverCfgsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, *names):
        for name in names:
            with open(os.path.join(self.tmp.name, name), "w") as handle:
                handle.write("# cfg\n")

    def test_the_naming_comes_from_the_typical_cfg(self):
        self.write("chipA_typical.cfg", "chipA_Cbest_T.cfg")
        naming, cfgs, _warnings = discover_cfgs(self.tmp.name)
        self.assertEqual(naming, "chipA")
        self.assertEqual({c.suffix for c in cfgs}, {"typical", "Cbest_T"})

    def test_a_corner_with_an_underscore_survives(self):
        """The reason the file name is never parsed back into prefix and
        corner: Cbest_T contains the separator.
        """
        self.write("chipA_typical.cfg", "chipA_Cbest_T.cfg")
        _naming, cfgs, _warnings = discover_cfgs(self.tmp.name)
        self.assertIn("Cbest_T", [c.suffix for c in cfgs])

    def test_a_foreign_cfg_is_ignored_and_reported(self):
        self.write("chipA_typical.cfg", "special.cfg")
        _naming, cfgs, warnings = discover_cfgs(self.tmp.name)
        self.assertEqual([c.name for c in cfgs], ["chipA_typical.cfg"])
        self.assertTrue(any("special.cfg" in w for w in warnings))

    def test_no_typical_cfg_is_reported_rather_than_guessed(self):
        self.write("chipA_Cbest_T.cfg")
        naming, cfgs, warnings = discover_cfgs(self.tmp.name)
        self.assertEqual(cfgs, [])
        self.assertEqual(naming, "")
        self.assertTrue(warnings)

    def test_aliases_are_applied_to_the_cfg_names(self):
        self.write("chipA_typical.cfg", "chipA_cbt.cfg")
        _naming, cfgs, _warnings = discover_cfgs(
            self.tmp.name, aliases={"Cbest_T": ["cbt"]})
        self.assertIn("Cbest_T", [c.canonical for c in cfgs])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.settings = AutoGroupSettings()
        self.settings.corner_aliases = {"Cbest_T": ["cbest_t", "cbt"]}
        self.cfgs = [
            cfg("/d/chipA_typical.cfg", "typical"),
            cfg("/d/chipA_cbt.cfg", "cbt", "Cbest_T"),
        ]

    def plan(self, facts):
        return plan_auto_groups(facts, self.cfgs, self.settings)

    def fact(self, key, path, **kwargs):
        return IndexFacts(index_key=key, path=path, **kwargs)

    def test_a_corner_index_goes_to_its_corner_cfg(self):
        plan = self.plan([self.fact("1000", "/p/corner_v2g/Cbest_T/i")])
        got = plan.assignments[0]
        self.assertTrue(got.include)
        self.assertEqual(got.cfg, "/d/chipA_cbt.cfg")
        self.assertIn("Cbest_T", got.reason)

    def test_a_cornerless_index_goes_to_typical(self):
        plan = self.plan([self.fact("1000", "/p/plain/i")])
        self.assertEqual(plan.assignments[0].cfg, "/d/chipA_typical.cfg")

    def test_a_corner_with_no_cfg_is_left_out_and_says_so(self):
        """Rule: no matching corner cfg means not assigned -- never quietly
        swept into typical, which would run it against the wrong corner.
        """
        plan = self.plan([self.fact("1000", "/p/corner_v2g/Whot_T/i")])
        got = plan.assignments[0]
        self.assertFalse(got.include)
        self.assertIn("no cfg", got.reason)
        self.assertIn("Whot_T", got.reason)

    def test_the_disable_flag_wins_over_everything(self):
        plan = self.plan([self.fact("1000", "/p/corner_v2g/Cbest_T/i",
                                    disabled_by_flag=True)])
        got = plan.assignments[0]
        self.assertFalse(got.include)
        self.assertIn("disable flag", got.reason)

    def test_an_excluded_path_is_not_described_as_missing_a_cfg(self):
        """A backup directory reported as "no cfg for this corner" sounds
        like something to fix. It is not.
        """
        plan = self.plan([self.fact("1000", "/p/run_old/corner_v2g/Whot_T/i")])
        self.assertIn("excluded pattern", plan.assignments[0].reason)

    def test_an_unrunnable_index_keeps_its_own_reason(self):
        plan = self.plan([self.fact("1000", "/p/plain/i",
                                    error="no GDS files")])
        got = plan.assignments[0]
        self.assertFalse(got.include)
        self.assertIn("no GDS", got.reason)

    def test_every_index_appears_in_the_proposal(self):
        """Included or not. A grouping that drops rows makes the batch look
        complete when it finishes.
        """
        facts = [self.fact("1000", "/p/corner_v2g/Cbest_T/i"),
                 self.fact("1001", "/p/corner_v2g/Whot_T/i"),
                 self.fact("1002", "/p/plain/i"),
                 self.fact("1003", "/p/bak/i")]
        plan = self.plan(facts)
        self.assertEqual([a.index_key for a in plan.assignments],
                         ["1000", "1001", "1002", "1003"])
        self.assertEqual([a.index_key for a in plan.included],
                         ["1000", "1002"])

    def test_the_groups_are_one_per_cfg(self):
        facts = [self.fact("1000", "/p/corner_v2g/Cbest_T/i"),
                 self.fact("1001", "/p/corner_v2g/cbest_t/i"),
                 self.fact("1002", "/p/plain/i")]
        groups = self.plan(facts).groups()
        self.assertEqual([c.name for c, _ in groups],
                         ["chipA_typical.cfg", "chipA_cbt.cfg"])
        members = {c.name: [a.index_key for a in items] for c, items in groups}
        self.assertEqual(members["chipA_cbt.cfg"], ["1000", "1001"])
        self.assertEqual(members["chipA_typical.cfg"], ["1002"])

    def test_differently_spelled_corners_land_in_one_group(self):
        facts = [self.fact("1000", "/p/corner_v2g/Cbest_T/i"),
                 self.fact("1001", "/p/corner_v2g/cbt/i")]
        groups = self.plan(facts).groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual([a.index_key for a in groups[0][1]], ["1000", "1001"])

    def test_without_a_typical_cfg_cornerless_indices_are_left_out(self):
        self.cfgs = [cfg("/d/chipA_cbt.cfg", "cbt", "Cbest_T")]
        plan = self.plan([self.fact("1000", "/p/plain/i")])
        self.assertFalse(plan.assignments[0].include)
        self.assertIn("typical", plan.assignments[0].reason)


class ScanDirectoryTest(unittest.TestCase):
    """The whole thing against a real directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings()
        self.settings.auto_group.corner_aliases = {"Cbest_T": ["cbt"]}

        self.work = os.path.join(self.tmp.name, "work")
        os.makedirs(self.work)
        make_arcx_cfg(os.path.join(self.work, "chipA_typical.cfg"))
        make_arcx_cfg(os.path.join(self.work, "chipA_cbt.cfg"))

        sources = os.path.join(self.tmp.name, "src")
        entries = {
            "1000": make_index_source(
                os.path.join(sources, "corner_v2g", "Cbest_T"), "1000",
                gds_count=2),
            "1001": make_index_source(os.path.join(sources, "plain"), "1001",
                                      gds_count=2),
            "1002": make_index_source(
                os.path.join(sources, "corner_v2g", "Whot_T"), "1002",
                gds_count=2),
            "1003": make_index_source(os.path.join(sources, "run_bak"), "1003",
                                      gds_count=2),
            "1004": make_index_source(os.path.join(sources, "plain"), "1004",
                                      gds_count=2),
        }
        self.entries = entries
        with open(os.path.join(entries["1004"], "disable_qcap_golden"),
                  "w") as handle:
            handle.write("")
        make_dir_map(os.path.join(self.work, "dir_map"), entries)

    def test_it_reads_the_directory_and_proposes_groups(self):
        plan = scan_directory(self.work, self.settings)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(plan.naming, "chipA")
        verdicts = {a.index_key: a for a in plan.assignments}
        self.assertEqual(verdicts["1000"].cfg_name, "chipA_cbt.cfg")
        self.assertEqual(verdicts["1001"].cfg_name, "chipA_typical.cfg")
        self.assertFalse(verdicts["1002"].include)   # no Whot_T cfg
        self.assertFalse(verdicts["1003"].include)   # run_bak
        self.assertFalse(verdicts["1004"].include)   # disable flag
        self.assertIn("disable flag", verdicts["1004"].reason)

    def test_a_directory_with_no_dir_map_says_so(self):
        plan = scan_directory(self.tmp.name, self.settings)
        self.assertFalse(plan.ok)
        self.assertIn("dir_map", plan.error)

    def test_a_directory_with_no_cfg_family_says_so(self):
        bare = os.path.join(self.tmp.name, "bare")
        os.makedirs(bare)
        make_dir_map(os.path.join(bare, "dir_map"), self.entries)
        plan = scan_directory(bare, self.settings)
        self.assertFalse(plan.ok)
        self.assertIn("cfg", plan.error)

    def test_reading_it_creates_nothing(self):
        before = sorted(os.listdir(self.work))
        scan_directory(self.work, self.settings)
        self.assertEqual(sorted(os.listdir(self.work)), before)


class ReadFactsTest(unittest.TestCase):
    def test_the_flag_is_read_never_written(self):
        """Those index directories are not ours to write to."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index1000")
            os.makedirs(path)
            facts = read_facts({"1000": path}, "disable_qcap_golden")
            self.assertFalse(facts[0].disabled_by_flag)
            self.assertEqual(os.listdir(path), [])

    def test_indices_come_back_in_natural_order(self):
        facts = read_facts({"1010": "/a", "1002": "/b", "1001": "/c"}, "")
        self.assertEqual([f.index_key for f in facts],
                         ["1001", "1002", "1010"])


if __name__ == "__main__":
    unittest.main()
