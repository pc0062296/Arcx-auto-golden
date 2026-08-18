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
from arcx_auto.adapters.arcx_cfg import ArcxConfig, parse_arcx_cfg
from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.adapters.store import SnapshotStore
from arcx_auto.cli import render
from arcx_auto.config.settings import Settings, load_settings
from arcx_auto.daemon import Daemon, DaemonOptions
from arcx_auto.domain.enums import PlanMode
from arcx_auto.domain.models import IndexRunSnapshot, IndexSpec, as_json_dict
from arcx_auto.services.collector import Collector
from arcx_auto.services.monitor import MonitorService
from arcx_auto.services.submitter import Submitter
from arcx_auto.services.qa import QaRunner
from arcx_auto.services.qa.runner import IndexQaReport
from arcx_auto.services.state_engine import (
    TransitionContext,
    transition_index_run,
)
from arcx_auto.services.state_resolver import resolve_case_state
from arcx_auto.web import WebOptions, serve
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
    status.add_argument("--arcx-cfg",
                        help="arcx.cfg 路徑。QA 需要它才知道該檢查哪些產出物; "
                             "未指定時會在 wave 目錄下自動尋找")
    status.add_argument("--no-qa", action="store_true",
                        help="只做觀測, 不跑 QA 檢查")
    status.add_argument("--issues", action="store_true",
                        help="列出所有 QA issue")

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

    # -- submit --------------------------------------------------------
    submit = sub.add_parser(
        "submit", help="檢查 -> 建立 wave 目錄 -> 逐波提交 (預設為 dry-run)")
    submit.add_argument("--dir-map", required=True)
    submit.add_argument("--arcx-cfg", required=True)
    submit.add_argument("--index", nargs="+", default=[],
                        help="要執行的 index (可多選)")
    submit.add_argument("--all", action="store_true",
                        help="使用 dir_map 內全部 index")
    submit.add_argument("--max-slots", type=int, help="單波 slot 上限")
    submit.add_argument("--mode", choices=["auto", "off"], default="auto")
    submit.add_argument("--run-id", help="這次提交的名稱 (預設由時間產生)")
    submit.add_argument("--run-root", help="wave 目錄的根 (預設取自設定)")
    submit.add_argument("--max-waves", type=int,
                        help="最多送出幾個 wave (預設全部)")
    submit.add_argument("--no-wait", action="store_true",
                        help="閘門擋住時直接結束, 不等待")
    submit.add_argument(
        "--yes", action="store_true",
        help="真的執行。**不加這個參數就是 dry-run** —— 只檢查與顯示指令, "
             "不建立任何目錄、不提交任何 job")

    # -- daemon --------------------------------------------------------
    daemon = sub.add_parser(
        "daemon", help="持續監控並把狀態寫進 state root (供 Web UI 讀取)")
    daemon.add_argument("--run-id", help="這次監控的名稱 (預設由時間產生)")
    daemon.add_argument("--wave-dir", action="append", default=[],
                        help="要監控的 wave 目錄 (可重複)")
    daemon.add_argument("--run-folder", action="append", default=[],
                        help="要監控的 index run folder (可重複)")
    daemon.add_argument("--arcx-cfg",
                        help="arcx.cfg 路徑 (預設在 wave 目錄下自動尋找)")
    daemon.add_argument("--interval", type=float,
                        help="固定掃描間隔 (秒)。預設依是否有 case 在跑分層調整")
    daemon.add_argument("--once", action="store_true", help="只掃一次就結束")
    daemon.add_argument("--no-lsf", action="store_true", help="不查詢 LSF")

    # -- web -----------------------------------------------------------
    web = sub.add_parser("web", help="啟動本機 Web UI (唯讀, 只綁 127.0.0.1)")
    web.add_argument("--host", default="127.0.0.1",
                     help="預設只聽 loopback。改成別的位址等於對外開放服務。")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--refresh", type=int, default=30,
                     help="頁面自動刷新間隔 (秒), 0 為關閉")

    # -- check-cfg -----------------------------------------------------
    check = sub.add_parser("check-cfg",
                           help="提交前檢查 arcx.cfg (PRE 檢查, 不需要 run folder)")
    check.add_argument("path", help="arcx.cfg 路徑")
    check.add_argument("--json", action="store_true")

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

        snapshots, observations, reports, lsf_note = _scan_once(
            args, settings, fs, collector, store
        )

        if args.json:
            payload = {
                "generated_at": time.time(),
                "lsf_note": lsf_note,
                "index_runs": [as_json_dict(s) for s in snapshots],
                "qa_issues": [
                    as_json_dict(i) for r in reports for i in r.all_issues()
                ],
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
                qa_reports=reports, show_issues=args.issues,
            ))

        if not interval:
            return 0


def _scan_once(args, settings, fs, collector, store):
    """一次掃描。業務邏輯全在 MonitorService 裡, 這裡只負責前後的 I/O。"""
    monitor = MonitorService(
        settings=settings, collector=collector, enable_qa=not args.no_qa)
    if store:
        monitor.prime(store.load())

    result = monitor.scan(
        wave_dirs=[args.wave_dir] if args.wave_dir else [],
        run_folders=list(args.run_folder or []),
        arcx_config=_load_arcx_cfg(args, settings),
        use_lsf=not args.no_lsf,
    )

    if args.wave_dir and not result.observations:
        print("  (wave 目錄底下沒有找到任何 index run folder: %s)"
              % args.wave_dir, file=sys.stderr)

    if store:
        store.save(monitor.previous)

    return (list(result.snapshots), list(result.observations),
            list(result.qa_reports), result.lsf_note)


def _load_arcx_cfg(args, settings: Settings) -> Optional[ArcxConfig]:
    """找出 arcx.cfg。

    優先順序: --arcx-cfg > <wave_dir>/arcx.cfg > <run_folder>/../arcx.cfg
    找不到不是錯誤 —— QA 會產生 CFG_EXPECTATION_UNAVAILABLE 這個
    UNKNOWN issue, 明確說「我不知道該檢查什麼」而不是默默放行。
    """
    candidates: List[str] = []
    if getattr(args, "arcx_cfg", None):
        candidates.append(args.arcx_cfg)
    elif getattr(args, "wave_dir", None):
        candidates.append(os.path.join(args.wave_dir, "arcx.cfg"))
    else:
        for folder in getattr(args, "run_folder", []) or []:
            candidates.append(os.path.join(folder, os.pardir, "arcx.cfg"))

    for candidate in candidates:
        resolved = os.path.abspath(os.path.expanduser(candidate))
        if os.path.isfile(resolved):
            config = parse_arcx_cfg(resolved)
            for warning in config.warnings:
                print("  ! arcx.cfg: %s" % warning, file=sys.stderr)
            return config
    return None


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
# submit
# --------------------------------------------------------------------------

def cmd_submit(args: argparse.Namespace, settings: Settings) -> int:
    """提交流程。**預設是 dry-run** —— 這是系統第一個會寫入磁碟的指令,
    所以要求使用者明確加 --yes 才會動手。
    """
    arcx = ArcxAdapter(settings.layout, settings.plan)
    dir_map = arcx.parse_dir_map(args.dir_map)
    for warning in dir_map.warnings:
        print("  ! dir_map: %s" % warning, file=sys.stderr)

    index_keys = dir_map.keys_sorted() if args.all else list(args.index)
    if not index_keys:
        print("錯誤: 請用 --index 指定 index, 或用 --all 選取全部", file=sys.stderr)
        return 2

    specs = []
    for key in index_keys:
        path = dir_map.resolve(key)
        if path is None:
            specs.append(IndexSpec(index_key=key, path="", gds_count=0,
                                   cpu_per_case=0,
                                   error="dir_map 內找不到這個 index"))
        else:
            specs.append(arcx.build_index_spec(key, path))

    plan = plan_waves(
        specs,
        max_slots_per_wave=args.max_slots or settings.plan.max_slots_per_wave,
        mode=PlanMode.OFF if args.mode == "off" else PlanMode.AUTO,
    )

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    run_root = os.path.abspath(os.path.expanduser(
        args.run_root or settings.run_root))
    dry_run = not args.yes

    print(render.render_plan(plan))
    print()

    submitter = Submitter(settings)
    outcome = submitter.submit(
        plan=plan,
        run_id=run_id,
        run_root=run_root,
        arcx_cfg=args.arcx_cfg,
        dir_map=args.dir_map,
        dry_run=dry_run,
        max_waves=args.max_waves,
        wait_for_gate=not args.no_wait,
        on_progress=lambda msg: print("  %s" % msg),
    )

    print(render.render_submit(outcome, dry_run=dry_run))

    if outcome.blocked:
        return 1
    if outcome.error:
        return 1
    return 0


# --------------------------------------------------------------------------
# daemon / web
# --------------------------------------------------------------------------

def cmd_daemon(args: argparse.Namespace, settings: Settings) -> int:
    if not args.wave_dir and not args.run_folder:
        print("錯誤: 至少要指定一個 --wave-dir 或 --run-folder", file=sys.stderr)
        return 2

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    options = DaemonOptions(
        run_id=run_id,
        wave_dirs=[os.path.abspath(os.path.expanduser(p)) for p in args.wave_dir],
        run_folders=[os.path.abspath(os.path.expanduser(p))
                     for p in args.run_folder],
        arcx_cfg=args.arcx_cfg,
        interval_sec=args.interval,
        use_lsf=not args.no_lsf,
        once=args.once,
    )
    daemon = Daemon(options, settings=settings)
    print("監控中 run_id=%s  state=%s" % (run_id, daemon.store.dir))
    if not args.once:
        print("Web UI: arcx-auto web    (Ctrl-C 停止 daemon)")
    return daemon.run()


def cmd_web(args: argparse.Namespace, settings: Settings) -> int:
    options = WebOptions(
        state_root=settings.expanded_state_root(),
        host=args.host,
        port=args.port,
        refresh_sec=args.refresh,
    )
    url = "http://%s:%d/" % (options.host, options.port)
    print("Web UI: %s" % url)
    print("state root: %s" % options.state_root)
    if options.host not in ("127.0.0.1", "localhost", "::1"):
        print("  ! 注意: 綁在 %s 等於對外開放這個服務。" % options.host,
              file=sys.stderr)
    print("Ctrl-C 結束。")
    try:
        serve(options)
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------
# check-cfg
# --------------------------------------------------------------------------

def cmd_check_cfg(args: argparse.Namespace, settings: Settings) -> int:
    """提交前的 arcx.cfg 檢查。

    有 FATAL 時回傳 exit code 1 —— 這樣可以直接串進提交前的 script。
    """
    config = parse_arcx_cfg(args.path)
    result = QaRunner(settings).run_config(config)

    if args.json:
        print(json.dumps({
            "path": args.path,
            "passed": result.passed,
            "issues": [as_json_dict(i) for i in result.issues],
            "blocks": [
                {
                    "name": b.name,
                    "enabled": b.enabled,
                    "flow": b.flow,
                    "output_dir": b.output_dir_name,
                }
                for b in (config.blocks if config else ())
            ],
        }, ensure_ascii=False, indent=2, default=str))
    else:
        print(render.render_cfg_check(config, result))

    return 0 if not result.fatal else 1


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
        "submit": cmd_submit,
        "daemon": cmd_daemon,
        "web": cmd_web,
        "plan": cmd_plan,
        "check-cfg": cmd_check_cfg,
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
