"""CLI 端到端測試 (唯讀)。

同時驗證一個硬性要求: Phase 0/2a 的所有指令**不得寫入任何 run folder**。
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from arcx_auto.cli.main import main
from tests.fixtures.fake_run import build_demo


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def snapshot_tree(root):
    """記錄整棵目錄樹的 (路徑, 大小), 用來證明沒有被修改。"""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            result[os.path.join(dirpath, name)] = None
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                result[path] = os.path.getsize(path)
            except OSError:
                result[path] = -1
    return result


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.demo = build_demo(os.path.join(cls.tmp.name, "demo"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- inspect -------------------------------------------------------

    def test_inspect_dir_map(self):
        code, out, _ = run_cli(["inspect", "dir-map", self.demo["dir_map"]])
        self.assertEqual(code, 0)
        self.assertIn("1000", out)
        self.assertIn("max=1004", out)

    def test_inspect_dir_map_json(self):
        code, out, _ = run_cli(
            ["inspect", "dir-map", self.demo["dir_map"], "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("1000", data["entries"])
        self.assertNotIn("min", data["entries"])

    def test_inspect_index(self):
        path = os.path.join(self.demo["sources"], "1000_sram_core")
        code, out, _ = run_cli(["inspect", "index", path, "--json"])
        self.assertEqual(code, 0)
        spec = json.loads(out)[0]
        self.assertEqual(spec["gds_count"], 3)
        self.assertEqual(spec["cpu_per_case"], 4)
        self.assertIn("sram", spec["keywords"])

    # -- status --------------------------------------------------------

    def test_status_wave_dir(self):
        code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf"])
        self.assertEqual(code, 0)
        self.assertIn("總覽", out)
        self.assertIn("COMPLETED_MARKER", out)
        self.assertIn("STALLED", out)

    def test_status_json(self):
        code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["index_runs"]), 2)
        keys = {r["index_key"] for r in data["index_runs"]}
        self.assertEqual(keys, {"1000", "1001"})

    def test_status_reports_lsf_unavailable(self):
        """LSF 不可用時必須明說判定已降級, 不能讓人以為看到的是完整資訊。"""
        _code, out, _ = run_cli(
            ["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf"])
        self.assertIn("LSF 資料不可用", out)
        self.assertIn("判定已停用", out)

    def test_status_missing_run_folder_is_reported(self):
        code, out, _ = run_cli(
            ["status", "--run-folder", "/definitely/not/here", "--no-lsf"])
        self.assertEqual(code, 0)
        self.assertIn("不存在", out)

    def test_status_state_file_persists_across_calls(self):
        state = os.path.join(self.tmp.name, "state.json")
        run_cli(["status", "--wave-dir", self.demo["wave_dir"],
                 "--no-lsf", "--state-file", state])
        self.assertTrue(os.path.exists(state))
        code, out, _ = run_cli(["status", "--wave-dir", self.demo["wave_dir"],
                                "--no-lsf", "--state-file", state])
        self.assertEqual(code, 0)
        self.assertIn("總覽", out)

    def test_status_does_not_modify_run_folder(self):
        """硬性要求: Phase 0 是唯讀的。"""
        before = snapshot_tree(self.demo["wave_dir"])
        run_cli(["status", "--wave-dir", self.demo["wave_dir"], "--no-lsf",
                 "--detail"])
        self.assertEqual(snapshot_tree(self.demo["wave_dir"]), before)

    # -- plan ----------------------------------------------------------

    def test_plan_all(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--max-slots", "100"])
        self.assertEqual(code, 0)
        self.assertIn("分波計畫", out)
        self.assertIn("wave_001", out)

    def test_plan_json_structure(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--max-slots", "100", "--json"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["mode"], "AUTO")
        self.assertTrue(plan["waves"])
        # 1004 沒有 GDS 也沒有 special.cfg -> 必須被排除且說明原因
        self.assertEqual([s["index_key"] for s in plan["excluded"]], ["1004"])

    def test_plan_priority_keyword_first(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--max-slots", "1000", "--json"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        first_wave_keys = [i["index_key"] for i in plan["waves"][0]["indices"]]
        # 1000 (sram) 與 1002 (ro) 命中關鍵字, 應排在最前面
        self.assertEqual(first_wave_keys[:2], ["1000", "1002"])

    def test_plan_off_mode_single_wave(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--all",
             "--mode", "off", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["waves"]), 1)

    def test_plan_show_command(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--index", "1000",
             "--arcx-cfg", "arcx.cfg", "--show-command"])
        self.assertEqual(code, 0)
        self.assertIn("Arcx -p arcx.cfg -d 1000 -lsf0 -nt 50 --run", out)

    def test_plan_unknown_index_is_excluded_with_reason(self):
        code, out, _ = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"], "--index", "9999",
             "--json"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["waves"], [])
        self.assertIn("找不到", plan["excluded"][0]["error"])

    def test_plan_requires_index_selection(self):
        code, _out, err = run_cli(
            ["plan", "--dir-map", self.demo["dir_map"]])
        self.assertEqual(code, 2)
        self.assertIn("--index", err)

    def test_plan_creates_no_directories(self):
        """硬性要求: plan 只計算, 不建立任何目錄、不提交任何 job。"""
        before = snapshot_tree(self.demo["root"])
        run_cli(["plan", "--dir-map", self.demo["dir_map"], "--all",
                 "--show-command"])
        self.assertEqual(snapshot_tree(self.demo["root"]), before)


class ConfigTest(unittest.TestCase):
    def test_missing_config_file_is_an_error(self):
        code, _out, err = run_cli(
            ["-c", "/no/such/config.json", "inspect", "dir-map", "/x"])
        self.assertEqual(code, 2)
        self.assertIn("設定載入失敗", err)

    def test_json_config_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "c.json")
            with open(cfg, "w") as handle:
                json.dump({"plan": {"max_slots_per_wave": 7}}, handle)
            demo = build_demo(os.path.join(tmp, "demo"))
            code, out, _ = run_cli(
                ["-c", cfg, "plan", "--dir-map", demo["dir_map"],
                 "--all", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["max_slots_per_wave"], 7)

    def test_unknown_config_field_warns_but_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "c.json")
            with open(cfg, "w") as handle:
                json.dump({"nonsense_field": 1}, handle)
            demo = build_demo(os.path.join(tmp, "demo"))
            code, _out, err = run_cli(
                ["-c", cfg, "inspect", "dir-map", demo["dir_map"]])
            self.assertEqual(code, 0)
            self.assertIn("未知設定欄位", err)


if __name__ == "__main__":
    unittest.main()
