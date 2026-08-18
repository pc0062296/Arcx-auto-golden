"""假的 run folder 產生器 —— 本專案最重要的測試工具。

真實 job 要跑好幾天, 靠實跑來驗證判定邏輯的迭代速度完全無法接受。
這個產生器讓我們可以在**毫秒內**造出各種情境 (完成 / 執行中 / 卡住 /
marker 不一致 / 孤兒目錄 / 從未啟動), 在沒有 LSF、沒有 NFS、沒有 Arcx
的機器上驗證整條 Collector -> StateEngine 的判定鏈。

也可以直接執行來造一份 demo 資料:

    python3 tests/fixtures/fake_run.py /tmp/demo
    python3 -m arcx_auto status --wave-dir /tmp/demo/wave_001 --no-lsf --detail
"""

from __future__ import annotations

import os
import time
from typing import Dict, Iterable, List, Optional


class CaseSpec:
    """描述一個 case 要造成什麼樣子。

    kind:
        complete       .complete marker + case dir + log            (正常完成)
        running        .run marker + case dir + log                 (執行中)
        queued         .queue marker + case dir                     (排隊中)
        stalled        .run marker, 但 log 的 mtime 設在很久以前     (疑似卡住)
        inconsistent   .complete 與 .run 同時存在                    (收尾不完整)
        orphan_dir     只有 case dir, 沒有任何 marker                (從未被提交)
        orphan_marker  只有 marker, 沒有 case dir                    (目錄被誤刪)
    """

    def __init__(
        self,
        case_id: str,
        kind: str = "complete",
        log_bytes: int = 4096,
        age_sec: float = 0.0,
    ) -> None:
        self.case_id = case_id
        self.kind = kind
        self.log_bytes = log_bytes
        self.age_sec = age_sec


_MARKERS: Dict[str, List[str]] = {
    "complete": ["complete"],
    "running": ["run"],
    "queued": ["queue"],
    "stalled": ["run"],
    "inconsistent": ["complete", "run"],
    "orphan_dir": [],
    "orphan_marker": ["run"],
}
_HAS_DIR = {
    "complete": True, "running": True, "queued": True, "stalled": True,
    "inconsistent": True, "orphan_dir": True, "orphan_marker": False,
}
_HAS_LOG = {
    "complete": True, "running": True, "queued": False, "stalled": True,
    "inconsistent": True, "orphan_dir": False, "orphan_marker": True,
}


def make_index_run_folder(
    root: str,
    index_key: str,
    cases: Iterable[CaseSpec],
    report_dirs: Iterable[str] = ("QC_Cc", "QC_Ct", "QC_Spice"),
) -> str:
    """造一個 index run folder, 結構與使用者提供的真實範例一致:

        .complete.case1
        QC_Cc/  QC_Ct/  QC_Spice/
        submit_bjob_cmd_file_1.log
        case1/
    """
    folder = os.path.join(root, index_key)
    os.makedirs(folder, exist_ok=True)

    for name in report_dirs:
        os.makedirs(os.path.join(folder, name), exist_ok=True)

    now = time.time()
    for spec in cases:
        num = "".join(ch for ch in spec.case_id if ch.isdigit()) or "0"

        for kind in _MARKERS[spec.kind]:
            marker = os.path.join(folder, ".%s.%s" % (kind, spec.case_id))
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("")

        if _HAS_DIR[spec.kind]:
            case_dir = os.path.join(folder, spec.case_id)
            os.makedirs(case_dir, exist_ok=True)

        if _HAS_LOG[spec.kind]:
            log_path = os.path.join(folder, "submit_bjob_cmd_file_%s.log" % num)
            body = (
                "# Arcx submit log\n"
                "working directory: %s\n" % os.path.join(folder, spec.case_id)
            )
            padding = max(0, spec.log_bytes - len(body))
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write(body + ("x" * padding))
            if spec.age_sec:
                stamp = now - spec.age_sec
                os.utime(log_path, (stamp, stamp))

    return folder


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

    # index 1000: 全部完成 (與使用者提供的真實範例一致)
    make_index_run_folder(wave, "1000", [
        CaseSpec("case1", "complete"),
        CaseSpec("case2", "complete"),
        CaseSpec("case3", "complete"),
    ])

    # index 1001: 部分完成 + 各種異常
    make_index_run_folder(wave, "1001", [
        CaseSpec("case1", "complete"),
        CaseSpec("case2", "running", log_bytes=20480),
        CaseSpec("case3", "stalled", log_bytes=2048, age_sec=7200),
        CaseSpec("case4", "queued"),
        CaseSpec("case5", "inconsistent"),
        CaseSpec("case6", "orphan_dir"),
        CaseSpec("case7", "orphan_marker"),
    ])

    return {
        "root": root,
        "dir_map": dir_map_path,
        "wave_dir": wave,
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
