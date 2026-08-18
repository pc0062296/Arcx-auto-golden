"""假的 run folder 產生器 —— 本專案最重要的測試工具。

真實 job 要跑好幾天, 靠實跑來驗證判定邏輯的迭代速度完全無法接受。
這個產生器讓我們可以在**毫秒內**造出各種情境, 在沒有 LSF、沒有 NFS、
沒有 Arcx 的機器上驗證整條 Collector -> StateEngine 的判定鏈。

產生的結構精確複製真實的 index run folder:

    .queue.NDIO_1                  <- marker, case id 是 cell 名稱
    .run.PDIO_1
    .complete.NTN_1
    NDIO_1/  PDIO_1/  NTN_1/       <- 每個 case 的 run dir
    QC_Cc/  QC_Ct/  QC_Spice/      <- Arcx 整理的 report
    submit_bjob_cmd_file_1.log     <- log, 檔名只有流水號
    cmd_folder/cmd_file_1          <- script, 內含 `cd <case run dir>`

也可以直接執行來造一份 demo 資料:

    python3 tests/fixtures/fake_run.py /tmp/demo
    python3 -m arcx_auto status --wave-dir /tmp/demo/wave_001 --no-lsf --detail
"""

from __future__ import annotations

import os
import time
from typing import Dict, Iterable, List, Optional

CMD_TEMPLATE = """\
#!/bin/csh -f
source /some/env/setup.csh
setenv ARCX_SOMETHING 1
cd {exec_path}
{tool} -in {case}.gds -out {case}.spf
"""


class CaseSpec:
    """描述一個 case 要造成什麼樣子。

    kind:
        complete       .complete marker + run dir + log + cmd_file   (正常完成)
        running        .run marker + run dir + log + cmd_file        (執行中)
        queued         .queue marker + run dir + cmd_file            (排隊中)
        stalled        .run marker, 但 log 的 mtime 設在很久以前      (疑似卡住)
        inconsistent   .complete 與 .run 同時存在                     (收尾不完整)
        orphan_dir     只有 run dir, 沒有 marker 也沒有 log           (從未被提交)
        orphan_marker  只有 marker, 沒有 run dir                      (目錄被誤刪)
        no_cmd_file    有 log 但 cmd_file 缺失                        (無法對應到 case)

    artifacts 控制產出物 (case run dir 內的巢狀結構):
        full            每個 block 的 netlist 都完整
        missing_netlist flow 目錄與 work 目錄都在, 但 netlist 沒產出來 (假成功)
        empty_netlist   netlist 存在但是 0 byte (job 一開始就死了)
        missing_flow    整個 <block>_<flow>/ 目錄不存在 (該 flow 根本沒跑)
        none            什麼都沒有
    """

    def __init__(
        self,
        case_id: str,
        kind: str = "complete",
        log_bytes: int = 4096,
        age_sec: float = 0.0,
        artifacts: str = "full",
    ) -> None:
        self.case_id = case_id
        self.kind = kind
        self.log_bytes = log_bytes
        self.age_sec = age_sec
        self.artifacts = artifacts


_MARKERS: Dict[str, List[str]] = {
    "complete": ["complete"],
    "running": ["run"],
    "queued": ["queue"],
    "stalled": ["run"],
    "inconsistent": ["complete", "run"],
    "orphan_dir": [],
    "orphan_marker": ["run"],
    "no_cmd_file": ["run"],
}
_HAS_DIR = {
    "complete": True, "running": True, "queued": True, "stalled": True,
    "inconsistent": True, "orphan_dir": True, "orphan_marker": False,
    "no_cmd_file": True,
}
_HAS_LOG = {
    "complete": True, "running": True, "queued": False, "stalled": True,
    "inconsistent": True, "orphan_dir": False, "orphan_marker": True,
    "no_cmd_file": True,
}
_HAS_CMD = {
    "complete": True, "running": True, "queued": True, "stalled": True,
    "inconsistent": True, "orphan_dir": False, "orphan_marker": True,
    "no_cmd_file": False,
}


def make_index_run_folder(
    root: str,
    index_key: str,
    cases: Iterable[CaseSpec],
    report_dirs: Iterable[str] = ("QC_Cc", "QC_Ct", "QC_Spice"),
    start_num: int = 1,
    tool: str = "starrc",
) -> str:
    """造一個 index run folder, 結構與真實範例一致。

    log 的流水號與 case 的順序**刻意錯開**(見 build_demo), 用來驗證
    「絕不能用編號猜 case」這件事 —— 唯一可靠的對應是讀 cmd_file。
    """
    folder = os.path.join(root, index_key)
    os.makedirs(folder, exist_ok=True)
    cmd_dir = os.path.join(folder, "cmd_folder")

    if report_dirs:
        make_report_dirs(folder, tuple(report_dirs))

    now = time.time()
    num = start_num
    for spec in cases:
        case_dir = os.path.join(folder, spec.case_id)

        for kind in _MARKERS[spec.kind]:
            with open(os.path.join(folder, ".%s.%s" % (kind, spec.case_id)),
                      "w", encoding="utf-8") as handle:
                handle.write("")

        if _HAS_DIR[spec.kind]:
            os.makedirs(case_dir, exist_ok=True)
            _make_artifacts(case_dir, spec.case_id, spec.artifacts)

        if _HAS_CMD[spec.kind]:
            os.makedirs(cmd_dir, exist_ok=True)
            with open(os.path.join(cmd_dir, "cmd_file_%d" % num),
                      "w", encoding="utf-8") as handle:
                handle.write(CMD_TEMPLATE.format(
                    exec_path=case_dir, case=spec.case_id, tool=tool))

        if _HAS_LOG[spec.kind]:
            log_path = os.path.join(folder, "submit_bjob_cmd_file_%d.log" % num)
            body = "# %s output for %s\n" % (tool, spec.case_id)
            padding = max(0, spec.log_bytes - len(body))
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write(body + ("x" * padding))
            if spec.age_sec:
                stamp = now - spec.age_sec
                os.utime(log_path, (stamp, stamp))

        num += 1

    return folder


DEFAULT_BLOCKS = (
    ("blocking_naming_qcap", "calQCAP", "CCI_DB.spice"),
    ("blocking_naming_qrcfs", "calQRCFS", "{case}.spf"),
)


def make_arcx_cfg(path, blocks=DEFAULT_BLOCKS):
    """造一份 arcx.cfg, 格式與真實範例一致 (含行首的旗標欄)。"""
    lines = []
    for name, flow, _netlist in blocks:
        lines.append("1 BEGIN_SETTINGS: %s" % name)
        lines.append("1 QC_FLOW = %s" % flow)
        lines.append("1 RCX_TECH_QTF = /path/to/%s.qtf" % flow)
        lines.append("1 RCX_LAYER_NAME_MAP = /path/to/%s.map" % flow)
        lines.append("END_SETTINGS")
        lines.append("")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


def _make_artifacts(case_dir, case_id, mode, blocks=DEFAULT_BLOCKS):
    """造出 <block>_<flow>/work_<flow>/<netlist> 的巢狀結構。"""
    if mode == "none":
        return
    for name, flow, netlist_tmpl in blocks:
        flow_dir = os.path.join(case_dir, "%s_%s" % (name, flow))
        if mode == "missing_flow":
            continue
        work_dir = os.path.join(flow_dir, "work_%s" % flow)
        os.makedirs(work_dir, exist_ok=True)
        if mode == "missing_netlist":
            continue
        netlist = os.path.join(work_dir, netlist_tmpl.format(case=case_id))
        body = b"" if mode == "empty_netlist" else (
            ("* netlist for %s (%s)\n" % (case_id, flow)).encode() + b"R1 a b 1k\n" * 200
        )
        with open(netlist, "wb") as handle:
            handle.write(body)


def make_report_dirs(folder, names=("QC_Cc", "QC_Ct", "QC_Spice"),
                     summary_suffix="SCCB3", skip_files=()):
    """造 QC_* report 目錄:

        QC_Cc/Report_QC_Cc
        QC_Cc/Report_QC_Cc_Summary_SCCB3
    """
    for name in names:
        path = os.path.join(folder, name)
        os.makedirs(path, exist_ok=True)
        if name in skip_files:
            continue
        for filename in ("Report_%s" % name,
                         "Report_%s_Summary_%s" % (name, summary_suffix)):
            with open(os.path.join(path, filename), "w", encoding="utf-8") as h:
                h.write("# %s\n" % filename)


def make_index_source(
    root: str,
    index_key: str,
    gds_count: int,
    cpu_per_case: int = 4,
    name_hint: str = "",
) -> str:
    """造一個 index 的來源目錄 (含 special.cfg 與 GDS)。"""
    dir_name = "%s_%s" % (index_key, name_hint) if name_hint else index_key
    path = os.path.join(root, dir_name)
    os.makedirs(path, exist_ok=True)

    with open(os.path.join(path, "special.cfg"), "w", encoding="utf-8") as handle:
        handle.write("# Arcx special config\n")
        handle.write("O_QCAP_LSF_NUM = %d\n" % cpu_per_case)
        handle.write("O_SOMETHING_ELSE = foo\n")

    for i in range(gds_count):
        with open(os.path.join(path, "cell_%03d.gds" % i), "wb") as handle:
            handle.write(b"\x00" * 16)
    return path


def make_dir_map(
    path: str,
    entries: Dict[str, str],
    add_min_max: bool = True,
) -> str:
    """造一份 Perl 格式的 dir_map, 刻意複製真實範例的怪癖
    (min 那行沒有結尾逗號)。
    """
    lines = ["%dir_map =("]
    for key in sorted(entries, key=lambda k: (0, int(k)) if k.isdigit() else (1, 0)):
        lines.append('"%s" => "%s"  ,' % (key, entries[key]))
    if add_min_max:
        numeric = sorted(int(k) for k in entries if k.isdigit())
        if numeric:
            lines.append('"min" => "%d"' % numeric[0])
            lines.append('"max" => "%d"' % numeric[-1])
    lines.append(");")
    lines.append("return 1 ;")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def build_demo(root: str) -> Dict[str, str]:
    """造一份涵蓋所有情境的完整 demo 資料集。"""
    root = os.path.abspath(os.path.expanduser(root))
    sources = os.path.join(root, "sources")
    wave = os.path.join(root, "wave_001")
    os.makedirs(wave, exist_ok=True)

    entries = {
        "1000": make_index_source(sources, "1000", gds_count=3, cpu_per_case=4,
                                  name_hint="sram_core"),
        "1001": make_index_source(sources, "1001", gds_count=8, cpu_per_case=4,
                                  name_hint="logic"),
        "1002": make_index_source(sources, "1002", gds_count=2, cpu_per_case=16,
                                  name_hint="ro_ring"),
        "1003": make_index_source(sources, "1003", gds_count=40, cpu_per_case=8,
                                  name_hint="bigblock"),
    }
    # 1004 刻意做成壞的: 有目錄但沒有 special.cfg, 用來驗證錯誤處理
    broken = os.path.join(sources, "1004_broken")
    os.makedirs(broken, exist_ok=True)
    entries["1004"] = broken

    dir_map_path = make_dir_map(os.path.join(root, "dir_map"), entries)

    # QA 需要 arcx.cfg 才知道該檢查哪些產出物
    cfg_path = make_arcx_cfg(os.path.join(wave, "arcx.cfg"))

    # index 1000: 與使用者提供的真實 ls -a 完全一致
    #   .queue.NDIO_1 / .run.PDIO_1 / .complete.NTN_1
    make_index_run_folder(wave, "1000", [
        CaseSpec("NDIO_1", "queued", artifacts="none"),
        CaseSpec("PDIO_1", "running", log_bytes=20480, artifacts="none"),
        CaseSpec("NTN_1", "complete", artifacts="full"),
    ])

    # index 1001: 各種異常。
    # cell 名稱刻意讓「編號順序 != 字母順序」, 證明系統沒有偷偷用編號猜 case。
    # 前三個都有 .complete marker —— 人工看全部像成功, 只有第一個是真的。
    make_index_run_folder(wave, "1001", [
        CaseSpec("PMOS_10", "complete", artifacts="full"),           # 真成功
        CaseSpec("NMOS_1", "complete", artifacts="missing_netlist"), # 假成功
        CaseSpec("CAP_MIM", "complete", artifacts="empty_netlist"),  # 假成功
        CaseSpec("PMOS_2", "stalled", log_bytes=2048,
                 age_sec=9 * 3600, artifacts="none"),                # 卡住 9 小時
        CaseSpec("RES_HI", "orphan_dir", artifacts="none"),          # 從未提交
        CaseSpec("DIODE_X", "no_cmd_file", artifacts="none"),        # 監控不到
    ])

    return {
        "root": root,
        "dir_map": dir_map_path,
        "wave_dir": wave,
        "arcx_cfg": cfg_path,
        "sources": sources,
    }


if __name__ == "__main__":  # pragma: no cover
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/arcx-auto-demo"
    info = build_demo(target)
    print("demo 資料已建立:")
    for key, value in info.items():
        print("  %-9s %s" % (key + ":", value))
    print()
    print("試試看:")
    print("  python3 -m arcx_auto inspect dir-map %s --verify" % info["dir_map"])
    print("  python3 -m arcx_auto status --wave-dir %s --no-lsf --detail"
          % info["wave_dir"])
    print("  python3 -m arcx_auto plan --dir-map %s --all --max-slots 100 "
          "--show-command" % info["dir_map"])
