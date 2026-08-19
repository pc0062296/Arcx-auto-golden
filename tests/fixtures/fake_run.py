"""Fake run folder generator -- the most important test tool in the project.

Real jobs take days, so validating decision logic by running them is hopeless.
This generator builds any situation in **milliseconds**, letting the whole
Collector -> StateEngine chain be exercised on a machine with no LSF, no NFS
and no Arcx.

The structure it produces mirrors a real index run folder exactly:

    .queue.NDIO_1                  markers; the case id is a cell name
    .run.PDIO_1
    .complete.NTN_1
    NDIO_1/  PDIO_1/  NTN_1/       per-case run dirs
    QC_Cc/  QC_Ct/  QC_Spice/      reports Arcx assembles
    submit_bjob_cmd_file_1.log     logs, named only by sequence number
    cmd_folder/cmd_file_1          the script, containing `cd <case run dir>`

It can also be run directly to build a demo data set:

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
    """Describes the shape one case should be built in.

    kind:
        complete       .complete marker + run dir + log + cmd_file
        running        .run marker + run dir + log + cmd_file
        queued         .queue marker + run dir + cmd_file
        stalled        .run marker, but the log mtime is set far in the past
        inconsistent   .complete and .run both present (tidy-up incomplete)
        orphan_dir     only a run dir, no marker and no log (never submitted)
        orphan_marker  only a marker, no run dir (the directory was deleted)
        no_cmd_file    a log but no cmd_file (cannot be mapped to a case)

    artifacts controls what is produced inside the case run dir:
        full            every block's netlist is complete
        missing_netlist flow and work dirs exist but no netlist (false success)
        empty_netlist   the netlist exists but is 0 bytes (job died at once)
        no_signature    a full sized netlist with no QuickCap banner on line 1,
                        which is what a truncated extraction leaves behind
        missing_flow    the whole <block>_<flow>/ directory is absent
        none            nothing at all
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
    """Build an index run folder matching the real structure.

    Log sequence numbers are **deliberately out of step** with case ordering
    (see build_demo) to prove that case identity is never guessed from the
    number: reading the cmd_file is the only reliable mapping.
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
    """Build an arcx.cfg matching the real format, including the flag column."""
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
    """Build the nested <block>_<flow>/work_<flow>/<netlist> structure."""
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
        # A complete calQCAP netlist opens with the extraction engine's own
        # banner. Leaving it out of the "full" fixture would mean every test
        # ran against a netlist that real QA would reject.
        header = "* netlist for %s (%s)\n" % (case_id, flow)
        if flow == "calQCAP" and mode != "no_signature":
            header = "* QuickCap extraction for %s\n" % case_id + header
        body = b"" if mode == "empty_netlist" else (
            header.encode() + b"R1 a b 1k\n" * 200
        )
        with open(netlist, "wb") as handle:
            handle.write(body)


def make_report_dirs(folder, names=("QC_Cc", "QC_Ct", "QC_Spice"),
                     summary_suffix="SCCB3", skip_files=(), summary_bad=()):
    """Build the QC_* report directories:

        QC_Cc/Report_QC_Cc
        QC_Cc/Report_QC_Cc_Summary_SCCB3
    """
    for name in names:
        path = os.path.join(folder, name)
        os.makedirs(path, exist_ok=True)
        if name in skip_files:
            continue
        with open(os.path.join(path, "Report_%s" % name), "w",
                  encoding="utf-8") as h:
            h.write("# Report_%s\n" % name)
        summary = os.path.join(
            path, "Report_%s_Summary_%s" % (name, summary_suffix))
        with open(summary, "w", encoding="utf-8") as h:
            h.write(make_summary_table(bad=summary_bad))


def make_summary_table(bad=(), rows=("cell_a", "cell_b")):
    """A QC_* Summary in the real shape.

    ``bad`` names the rows whose comparison failed, so a test can ask for a
    table that looks complete but is not -- which is the whole point of the
    check that reads it.
    """
    lines = [
        "input report: /path/to/ref",
        "input report: /path/to/cmp",
        "",
        "refReport = /path/to/ref",
        "rep item refReport cmpReport1 diffCmp1",
    ]
    for row in rows:
        if row in bad:
            lines.append("%s total_cap 1.234 fail -" % row)
        else:
            lines.append("%s total_cap 1.234 1.240 0.006" % row)
    lines.append("########")
    return "\n".join(lines) + "\n"


def make_index_source(
    root: str,
    index_key: str,
    gds_count: int,
    cpu_per_case: int = 4,
    name_hint: str = "",
) -> str:
    """Build an index source directory, with special.cfg and GDS files."""
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
    """Build a Perl style dir_map, reproducing the real sample's quirk that
    the "min" line has no trailing comma.
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
    """Build a complete demo data set covering every situation."""
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
    # 1004 is deliberately broken: a directory with no special.cfg,
    # to exercise the error handling
    broken = os.path.join(sources, "1004_broken")
    os.makedirs(broken, exist_ok=True)
    entries["1004"] = broken

    dir_map_path = make_dir_map(os.path.join(root, "dir_map"), entries)

    # QA needs arcx.cfg to know which artifacts to check
    cfg_path = make_arcx_cfg(os.path.join(wave, "arcx.cfg"))

    # index 1000: exactly the real ls -a we were given
    #   .queue.NDIO_1 / .run.PDIO_1 / .complete.NTN_1
    make_index_run_folder(wave, "1000", [
        CaseSpec("NDIO_1", "queued", artifacts="none"),
        CaseSpec("PDIO_1", "running", log_bytes=20480, artifacts="none"),
        CaseSpec("NTN_1", "complete", artifacts="full"),
    ])

    # index 1001: assorted anomalies.
    # The cell names deliberately make sequence order differ from alphabetical
    # order, proving nothing guesses the case from the number.
    # The first three all carry a .complete marker -- by eye they all look
    # successful, but only the first one really is.
    make_index_run_folder(wave, "1001", [
        CaseSpec("PMOS_10", "complete", artifacts="full"),            # real
        CaseSpec("NMOS_1", "complete", artifacts="missing_netlist"),  # false
        CaseSpec("CAP_MIM", "complete", artifacts="empty_netlist"),   # false
        CaseSpec("PMOS_2", "stalled", log_bytes=2048,
                 age_sec=9 * 3600, artifacts="none"),            # quiet 9 hours
        CaseSpec("RES_HI", "orphan_dir", artifacts="none"),      # never submitted
        CaseSpec("DIODE_X", "no_cmd_file", artifacts="none"),    # unmonitorable
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
    print("demo data created:")
    for key, value in info.items():
        print("  %-9s %s" % (key + ":", value))
    print()
    print("try:")
    print("  python3 -m arcx_auto inspect dir-map %s --verify" % info["dir_map"])
    print("  python3 -m arcx_auto status --wave-dir %s --no-lsf --detail"
          % info["wave_dir"])
    print("  python3 -m arcx_auto plan --dir-map %s --all --max-slots 100 "
          "--show-command" % info["dir_map"])
