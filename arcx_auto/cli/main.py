"""CLI 進入點。

Phase 0 (監控) + Phase 2a (分波計畫) 的所有指令都是**唯讀**的:
不寫入任何 run folder, 不提交任何 job。目的是先驗證系統對
marker / log / dir_map / special.cfg 的理解是否正確。

    arcx-auto status  --run-folder <path>...      掃描 index run folder
    arcx-auto status  --wave-dir <path>           掃描整個 wave 目錄
    arcx-auto plan    --dir-map <file> --index .. 產生分波計畫 (不執行)
    arcx-auto inspect dir-map <file>              解析 dir_map
    arcx-auto inspect index <path>...             解析 special.cfg + 數 GDS
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Sequence

from arcx_auto import __version__
from arcx_auto.adapters.arcx import ArcxAdapter
from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.adapters.store import SnapshotStore
from arcx_auto.cli import render
from arcx_auto.config.settings import Settings, load_settings
from arcx_auto.domain.enums import PlanMode
from arcx_auto.domain.models import IndexRunSnapshot, IndexSpec, as_json_dict
from arcx_auto.services.collector import Collector
from arcx_auto.services.state_engine import (
    TransitionContext,
    transition_index_run,
)
from arcx_auto.services.wave_planner import plan_waves


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcx-auto",
        description="Arcx RC extraction 自動化監控與分波工具 (Phase 0 + 2a, 唯讀)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-c", "--config", help="設定檔路徑 (.yaml 或 .json)")
    sub = parser.add_subparsers(dest="command", required=True)

    # -- status --------------------------------------------------------
    status = sub.add_parser("status", help="掃描 run folder 並顯示 case 狀態")
    target = status.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-folder", action="append", default=[],
                        help="index run folder (可重複指定)")
    target.add_argument("--wave-dir",
                        help="wave 目錄, 會自動掃描底下所有 index run folder")
    status.add_argument("--state-file",
                        help="狀態快取檔。提供後 stall 偵測才能跨次呼叫累積計時")
    status.add_argument("--watch", type=float, metavar="SEC",
                        help="每 SEC 秒重新掃描一次 (Ctrl-C 結束)")
    status.add_argument("--detail", action="store_true", help="列出每個 case")
    status.add_argument("--json", action="store_true", help="輸出 JSON")
    status.add_argument("--no-lsf", action="store_true",
                        help="不查詢 LSF (離線測試用)")

    # -- plan ----------------------------------------------------------
    plan = sub.add_parser("plan", help="產生分波計畫 (不建立任何目錄、不提交)")
    plan.add_argument("--dir-map", required=True, help="dir_map 檔案路徑")
    plan.add_argument("--index", nargs="+", default=[],
                      help="要執行的 index (可多選)")
    plan.add_argument("--all", action="store_true", help="使用 dir_map 內全部 index")
    plan.add_argument("--arcx-cfg", default="arcx.cfg",
                      help="arcx.cfg 路徑 (只用於顯示指令)")
    plan.add_argument("--max-slots", type=int, help="單波 slot 上限")
    plan.add_argument("--mode", choices=["auto", "off"], default="auto",
                      help="auto=自動分波, off=不分波全部一次送出")
    plan.add_argument("--show-command", action="store_true",
                      help="顯示每個 wave 會執行的 Arcx 指令")
    plan.add_argument("--json", action="store_true", help="輸出 JSON")

    # -- inspect -------------------------------------------------------
    inspect = sub.add_parser("inspect", help="解析輸入檔案 (驗證格式理解是否正確)")
    inspect_sub = inspect.add_subparsers(dest="subject", required=True)

    dm = inspect_sub.add_parser("dir-map", help="解析 dir_map")
    dm.add_argument("path")
    dm.add_argument("--verify", action="store_true", help="檢查每個 path 是否存在")
    dm.add_argument("--json", action="store_true")

    idx = inspect_sub.add_parser("index", help="解析 index path 的 special.cfg 與 GDS")
    idx.add_argument("path", nargs="+")
    idx.add_argument("--json", action="store_true")

    return parser


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    fs = FsAdapter(settings.layout)
    lsf = LsfAdapter(settings.lsf)
    collector = Collector(settings=settings, fs=fs, lsf=lsf)
    store = SnapshotStore(args.state_file) if args.state_file else None

    interval = args.watch
    first = True
    while True:
        if not first and interval:
            time.sleep(interval)
        first = False

        snapshots, observations, lsf_note = _scan_once(
            args, settings, fs, collector, store
        )

        if args.json:
            payload = {
                "generated_at": time.time(),
                "lsf_note": lsf_note,
                "index_runs": [as_json_dict(s) for s in snapshots],
                "scan_issues": [
                    {
                        "index_key": o.index_key,
                        "unknown_markers": [
                            {"file": n, "case_id": c}
                            for n, c in o.unknown_markers
                        ],
                        "unresolved_logs": list(o.unresolved_logs),
                        "unmatched_entries": list(o.unmatched_entries),
                    }
                    for o in observations
                    if o.unknown_markers or o.unresolved_logs or o.unmatched_entries
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        else:
            if interval:
                print("\n" + "=" * 72)
                print("掃描時間: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
            print(render.render_status(
                snapshots, now=time.time(), detail=args.detail,
                lsf_note=lsf_note, observations=observations,
            ))

        if not interval:
            return 0


def _scan_once(args, settings, fs, collector, store):
    now = time.time()

    lsf_jobs: List = []
    lsf_note: Optional[str] = None
    if args.no_lsf:
        lsf_note = "已指定 --no-lsf"
    else:
        lsf_jobs, error = collector.fetch_lsf_jobs()
        if error:
            lsf_note = error

    lsf_available = lsf_note is None

    if args.wave_dir:
        observations = collector.collect_wave(args.wave_dir, lsf_jobs=lsf_jobs, now=now)
        if not observations:
            print("  (wave 目錄底下沒有找到任何 index run folder: %s)"
                  % args.wave_dir, file=sys.stderr)
    else:
        observations = [
            collector.collect_index_run(folder, lsf_jobs=lsf_jobs, now=now)
            for folder in args.run_folder
        ]

    previous: Dict[str, IndexRunSnapshot] = store.load() if store else {}
    ctx = TransitionContext.from_settings(
        settings.monitor, lsf_data_available=lsf_available, now=now
    )

    snapshots: List[IndexRunSnapshot] = []
    updated: Dict[str, IndexRunSnapshot] = dict(previous)
    for observation in observations:
        key = observation.run_folder
        snapshot, _events = transition_index_run(previous.get(key), observation, ctx)
        snapshots.append(snapshot)
        updated[key] = snapshot

    if store:
        store.save(updated)

    return snapshots, observations, lsf_note


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace, settings: Settings) -> int:
    arcx = ArcxAdapter(settings.layout, settings.plan)
    dir_map = arcx.parse_dir_map(args.dir_map)

    for warning in dir_map.warnings:
        print("  ! dir_map: %s" % warning, file=sys.stderr)

    if args.all:
        index_keys = dir_map.keys_sorted()
    else:
        index_keys = list(args.index)

    if not index_keys:
        print("錯誤: 請用 --index 指定 index, 或用 --all 選取全部", file=sys.stderr)
        return 2

    specs: List[IndexSpec] = []
    for key in index_keys:
        path = dir_map.resolve(key)
        if path is None:
            specs.append(IndexSpec(
                index_key=key, path="", gds_count=0, cpu_per_case=0,
                error="dir_map 內找不到這個 index",
            ))
            continue
        specs.append(arcx.build_index_spec(key, path))

    max_slots = args.max_slots or settings.plan.max_slots_per_wave
    mode = PlanMode.OFF if args.mode == "off" else PlanMode.AUTO
    plan = plan_waves(specs, max_slots_per_wave=max_slots, mode=mode)

    commands: Dict[str, str] = {}
    if args.show_command:
        for wave in plan.waves:
            argv = arcx.build_run_command(
                args.arcx_cfg, list(wave.index_keys), lsf_settings=settings.lsf
            )
            commands[wave.name] = " ".join(argv)

    if args.json:
        payload = as_json_dict(plan)
        payload["commands"] = commands
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render.render_plan(plan, show_command=args.show_command,
                                 commands=commands))
    return 0


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace, settings: Settings) -> int:
    arcx = ArcxAdapter(settings.layout, settings.plan)

    if args.subject == "dir-map":
        dir_map = arcx.parse_dir_map(args.path)
        if args.json:
            print(json.dumps(as_json_dict(dir_map), ensure_ascii=False,
                             indent=2, default=str))
        else:
            print(render.render_dir_map(dir_map, verify=args.verify))
        return 0

    specs = [
        arcx.build_index_spec(os.path.basename(p.rstrip("/")) or p, p)
        for p in args.path
    ]
    if args.json:
        print(json.dumps([as_json_dict(s) for s in specs],
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(render.render_index_specs(specs))
        for spec in specs:
            for warning in spec.warnings:
                print("  ! %s: %s" % (spec.index_key, warning), file=sys.stderr)
    return 0


# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings, warnings = load_settings(args.config)
    except (FileNotFoundError, RuntimeError) as exc:
        print("設定載入失敗: %s" % exc, file=sys.stderr)
        return 2
    for warning in warnings:
        print("  ! 設定: %s" % warning, file=sys.stderr)

    handlers = {
        "status": cmd_status,
        "plan": cmd_plan,
        "inspect": cmd_inspect,
    }
    handler = handlers[args.command]
    try:
        return handler(args, settings)
    except KeyboardInterrupt:
        print("\n中斷。", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
