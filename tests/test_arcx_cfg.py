"""arcx.cfg 解析。

這個檔案是 QA 的地圖 —— 解析錯了, 期望產出物就全錯。
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
        """case run dir 底下的目錄名 = <block>_<QC_FLOW>。"""
        cfg = parse_arcx_cfg(self._write(REAL_CFG))
        self.assertEqual(cfg.blocks[0].output_dir_name,
                         "blocking_naming_qcap_calQCAP")

    def test_leading_zero_disables_a_setting(self):
        """行首旗標 0 目前解讀為停用。"""
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
        """使用者範例中出現過 BEGIN_SETTIMGS 這個拼法。"""
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
        """兩個同名 block 的產出目錄會互相覆蓋。"""
        cfg = parse_arcx_cfg(self._write(
            "1 BEGIN_SETTINGS: a\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n"
            "1 BEGIN_SETTINGS: a\n1 QC_FLOW = calQRCFS\nEND_SETTINGS\n"))
        self.assertTrue(any("出現 2 次" in w for w in cfg.warnings), cfg.warnings)

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
    """使用者提供的真實 arcx.cfg 格式。

    這份範例揭露了兩個會讓 parser 完全失效的差異:
    BEGIN_SETTING 是單數 (END_SETTINGS 卻是複數), 以及 g: 開頭的共同變數。
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
        """BEGIN_SETTING (單數) 必須能解析 —— 否則整個檔案變成 0 個 block。"""
        self.assertEqual([b.name for b in self.cfg.blocks],
                         ["blocking_nameing_1"])

    def test_spaces_around_colon(self):
        self.assertEqual(self.cfg.blocks[0].flow, "calQCAP")

    def test_globals_collected_separately(self):
        """g: 開頭的共同變數不屬於任何 block。"""
        self.assertEqual(self.cfg.globals["QCA"], "Yes")
        self.assertEqual(self.cfg.globals["O_CAL_SET_ENV"],
                         "setenv LICENSE 123@lic9")
        self.assertNotIn("QCA", self.cfg.blocks[0].settings)

    def test_globals_do_not_create_a_block(self):
        self.assertEqual(len(self.cfg.blocks), 1)

    def test_value_with_spaces_preserved(self):
        """TOOL_VERSION_LVS 的值是「指令 + 參數」, 不是單純的路徑。"""
        self.assertEqual(self.cfg.blocks[0].settings["TOOL_VERSION_LVS"],
                         "/source.csh tool_build")

    def test_no_spurious_warnings(self):
        """真實格式不該產生任何警告 —— 每次都警告等於沒有警告。"""
        self.assertEqual(self.cfg.warnings, ())

    def test_settimg_typo_still_warns(self):
        """只有 N 打成 M 這種明顯筆誤才警告。"""
        path = os.path.join(self.tmp.name, "typo.cfg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1 BEGIN_SETTIMG : x\n1 QC_FLOW = calQCAP\nEND_SETTINGS\n")
        cfg = parse_arcx_cfg(path)
        self.assertEqual([b.name for b in cfg.blocks], ["x"])
        self.assertTrue(any("M" in w for w in cfg.warnings), cfg.warnings)

    def test_disabled_global_skipped(self):
        path = os.path.join(self.tmp.name, "g.cfg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("0 g:QCA = Yes\n1 BEGIN_SETTING : x\n"
                         "1 QC_FLOW = calQCAP\nEND_SETTINGS\n")
        self.assertEqual(parse_arcx_cfg(path).globals, {})
