"""arcx.cfg parsing.

This file is QA's map: parse it wrongly and every expected artifact is wrong.
"""

import os
import tempfile
import unittest

from arcx_auto.adapters.arcx_cfg import parse_arcx_cfg

REAL_CFG = """\
1 BEGIN_SETTINGS: blocking_naming_qcap
1 QC_FLOW = calQCAP
1 RCX_TECH_QTF = /path/to/file
1 RCX_LAYER_NAME_MAP = /path/to/file
END_SETTINGS

1 BEGIN_SETTINGS: blocking_naming_qrcfs
1 QC_FLOW = calQRCFS
END_SETTINGS
"""


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text, name="arcx.cfg"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_parses_real_format(self):
        cfg = parse_arcx_cfg(self._write(REAL_CFG))
        self.assertEqual([b.name for b in cfg.blocks],
                         ["blocking_naming_qcap", "blocking_naming_qrcfs"])
        self.assertEqual(cfg.blocks[0].flow, "calQCAP")
        self.assertEqual(cfg.blocks[1].flow, "calQRCFS")

    def test_settings_captured(self):
        cfg = parse_arcx_cfg(self._write(REAL_CFG))
        self.assertEqual(cfg.blocks[0].settings["RCX_TECH_QTF"], "/path/to/file")

    def test_output_dir_name(self):
        """The directory inside a case run dir is <block>_<QC_FLOW>."""
        cfg = parse_arcx_cfg(self._write(REAL_CFG))
        self.assertEqual(cfg.blocks[0].output_dir_name,
                         "blocking_naming_qcap_calQCAP")

    def test_leading_zero_disables_a_setting(self):
        """A leading 0 disables the line."""
        cfg = parse_arcx_cfg(self._write(
            "1 BEGIN_SETTINGS: a\n1 QC_FLOW = calQCAP\n0 OPT = x\nEND_SETTINGS\n"))
        self.assertIn("OPT", cfg.blocks[0].disabled_keys)
        self.assertNotIn("OPT", cfg.blocks[0].settings)

    def test_leading_zero_disables_a_block(self):
        cfg = parse_arcx_cfg(self._write(
            "0 BEGIN_SETTINGS: a\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n"))
        self.assertFalse(cfg.blocks[0].enabled)
        self.assertEqual(cfg.enabled_blocks, ())

    def test_lines_without_flag_still_parse(self):
        cfg = parse_arcx_cfg(self._write(
            "BEGIN_SETTINGS: a\nQC_FLOW = calQCAP\nEND_SETTINGS\n"))
        self.assertEqual(cfg.blocks[0].flow, "calQCAP")

    def test_settimgs_typo_tolerated(self):
        """The BEGIN_SETTIMGS misspelling has been seen in the wild."""
        cfg = parse_arcx_cfg(self._write(
            "1 BEGIN_SETTIMGS: a\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n"))
        self.assertEqual([b.name for b in cfg.blocks], ["a"])

    def test_comments_ignored(self):
        cfg = parse_arcx_cfg(self._write(
            "# comment\n1 BEGIN_SETTINGS: a\n1 QC_FLOW = calQCAP  # inline\n"
            "END_SETTINGS\n"))
        self.assertEqual(cfg.blocks[0].flow, "calQCAP")

    def test_missing_end_settings_warns_but_keeps_block(self):
        cfg = parse_arcx_cfg(self._write(
            "1 BEGIN_SETTINGS: a\n1 QC_FLOW = calQCAP\n"))
        self.assertEqual(len(cfg.blocks), 1)
        self.assertTrue(cfg.blocks[0].warnings)

    def test_duplicate_block_name_warns(self):
        """Two blocks with one name write into the same output directory."""
        cfg = parse_arcx_cfg(self._write(
            "1 BEGIN_SETTINGS: a\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n"
            "1 BEGIN_SETTINGS: a\n1 QC_FLOW = calQRCFS\nEND_SETTINGS\n"))
        self.assertTrue(any("appears 2 times" in w for w in cfg.warnings),
                        cfg.warnings)

    def test_empty_file_warns(self):
        cfg = parse_arcx_cfg(self._write(""))
        self.assertEqual(cfg.blocks, ())
        self.assertTrue(cfg.warnings)

    def test_missing_file_returns_error_not_exception(self):
        cfg = parse_arcx_cfg(os.path.join(self.tmp.name, "nope"))
        self.assertIsNotNone(cfg.error)


if __name__ == "__main__":
    unittest.main()


REAL_SAMPLE_CFG = """\
g:QCA = Yes
g:O_CAL_SET_ENV = setenv LICENSE 123@lic9

1 BEGIN_SETTING : blocking_nameing_1
1 QC_FLOW = calQCAP
1 PROCESS = xx
1 TOOL_VERSION_LVS = /source.csh tool_build
1 RCX_TECH_QTF = /path/to/file
1 LVS_DFM_DIR = /path/to/dir
1 LVS_DECL = /path/to/file
1 O_XXXX_SETTING = XXXX
END_SETTINGS
"""


class RealFormatTest(unittest.TestCase):
    """The real arcx.cfg format.

    This sample exposed two differences that broke the parser outright:
    BEGIN_SETTING is singular while END_SETTINGS is plural, and the g: prefixed
    shared variables.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "arcx.cfg")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(REAL_SAMPLE_CFG)
        self.cfg = parse_arcx_cfg(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_begin_setting_singular_parses(self):
        """BEGIN_SETTING (singular) must parse, or the whole file yields
        zero blocks.
        """
        self.assertEqual([b.name for b in self.cfg.blocks],
                         ["blocking_nameing_1"])

    def test_spaces_around_colon(self):
        self.assertEqual(self.cfg.blocks[0].flow, "calQCAP")

    def test_globals_collected_separately(self):
        """g: prefixed shared variables belong to no block."""
        self.assertEqual(self.cfg.globals["QCA"], "Yes")
        self.assertEqual(self.cfg.globals["O_CAL_SET_ENV"],
                         "setenv LICENSE 123@lic9")
        self.assertNotIn("QCA", self.cfg.blocks[0].settings)

    def test_globals_do_not_create_a_block(self):
        self.assertEqual(len(self.cfg.blocks), 1)

    def test_value_with_spaces_preserved(self):
        """TOOL_VERSION_LVS holds a command plus arguments, not a bare path."""
        self.assertEqual(self.cfg.blocks[0].settings["TOOL_VERSION_LVS"],
                         "/source.csh tool_build")

    def test_no_spurious_warnings(self):
        """The real format must produce no warnings: warning every time is
        the same as not warning at all.
        """
        self.assertEqual(self.cfg.warnings, ())

    def test_settimg_typo_still_warns(self):
        """Only an obvious typo -- N written as M -- warrants a warning."""
        path = os.path.join(self.tmp.name, "typo.cfg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1 BEGIN_SETTIMG : x\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n")
        cfg = parse_arcx_cfg(path)
        self.assertEqual([b.name for b in cfg.blocks], ["x"])
        self.assertTrue(any("N typed as M" in w for w in cfg.warnings), cfg.warnings)

    def test_disabled_global_skipped(self):
        path = os.path.join(self.tmp.name, "g.cfg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("0 g:QCA = Yes\n1 BEGIN_SETTING : x\n"
                         "1 QC_FLOW = calQCAP\nEND_SETTINGS\n")
        self.assertEqual(parse_arcx_cfg(path).globals, {})


class DiscoverCfgSnapshotTest(unittest.TestCase):
    """Arcx keeps its own copy of the cfg it ran inside the index run folder.

    Without finding it, `status --run-folder` raised CFG_EXPECTATION_UNAVAILABLE
    for every case -- an UNKNOWN issue, which blocks success -- so five
    genuinely complete cases were reported as FAILED. The cfg was in the folder
    the whole time, under a name derived from the user.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_finds_a_user_named_cfg(self):
        from arcx_auto.adapters.arcx_cfg import discover_arcx_cfg

        self._write("zmwu.cfg",
                    "1 BEGIN_SETTING : blk\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n")
        config = discover_arcx_cfg(self.tmp.name)
        self.assertIsNotNone(config)
        self.assertEqual([b.name for b in config.blocks], ["blk"])

    def test_special_cfg_is_rejected_without_being_named(self):
        """special.cfg is plain key = value, so it fails the "has blocks" test
        on its own. Nothing has to list it as an exception.
        """
        from arcx_auto.adapters.arcx_cfg import discover_arcx_cfg

        self._write("special.cfg", "O_QCAP_LSF_NUM = 4\nOTHER = 1\n")
        self.assertIsNone(discover_arcx_cfg(self.tmp.name))

    def test_picks_the_real_cfg_when_both_are_present(self):
        from arcx_auto.adapters.arcx_cfg import discover_arcx_cfg

        self._write("special.cfg", "O_QCAP_LSF_NUM = 4\n")
        self._write("zmwu.cfg",
                    "1 BEGIN_SETTING : blk\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n")
        config = discover_arcx_cfg(self.tmp.name)
        self.assertIsNotNone(config)
        self.assertTrue(config.blocks)

    def test_no_cfg_at_all_is_not_an_error(self):
        from arcx_auto.adapters.arcx_cfg import discover_arcx_cfg

        self.assertIsNone(discover_arcx_cfg(self.tmp.name))

    def test_a_missing_folder_is_not_an_error(self):
        from arcx_auto.adapters.arcx_cfg import discover_arcx_cfg

        self.assertIsNone(discover_arcx_cfg(os.path.join(self.tmp.name, "nope")))
