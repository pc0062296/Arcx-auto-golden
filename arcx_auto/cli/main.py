"""CLI entry point.

Commands and what they do:

    arcx-auto status     scan run folders and report case states
    arcx-auto plan       produce a wave plan (computes only, never submits)
    arcx-auto submit     check -> create wave dirs -> submit (dry run by default)
    arcx-auto daemon     keep monitoring and write state for the web UI
    arcx-auto web        serve the local read-only dashboard
    arcx-auto check-cfg  validate arcx.cfg before submitting
    arcx-auto inspect    parse dir_map / special.cfg to verify assumptions
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
from arcx_auto.adapters.store import RunStore, SnapshotStore
from arcx_auto.cli import render
from arcx_auto.config.settings import Settings, load_settings
from arcx_auto.daemon import Daemon, DaemonOptions
from arcx_auto.domain.enums import PlanMode
from arcx_auto.domain.models import IndexRunSnapshot, IndexSpec, as_json_dict
from arcx_auto.util.atomic import read_json
from arcx_auto.services.collector import Collector
from arcx_auto.services.launcher import read_launch
from arcx_auto.services.monitor import MonitorService
from arcx_auto.services.remediator import Remediator
from arcx_auto.services.rerun_planner import build_rerun_plan
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
# Argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcx-auto",
        description="Arcx RC extraction monitoring, planning and submission",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-c", "--config", help="settings file (.yaml or .json)")
    sub = parser.add_subparsers(dest="command", required=True)

    # -- status --------------------------------------------------------
    status = sub.add_parser("status", help="scan run folders and show states")
    target = status.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-folder", action="append", default=[],
                        help="index run folder (repeatable)")
    target.add_argument("--wave-dir",
                        help="wave directory; every index run folder under it")
    status.add_argument("--state-file",
                        help="state cache file; needed for stall timing to "
                             "accumulate across invocations")
    status.add_argument("--watch", type=float, metavar="SEC",
                        help="rescan every SEC seconds (Ctrl-C to stop)")
    status.add_argument("--detail", action="store_true", help="list every case")
    status.add_argument("--json", action="store_true", help="output JSON")
    status.add_argument("--no-lsf", action="store_true",
                        help="do not query LSF (for offline use)")
    status.add_argument("--arcx-cfg",
                        help="path to arcx.cfg; QA needs it to know which "
                             "artifacts to check. Found automatically in the "
                             "wave directory when omitted")
    status.add_argument(
        "--dir-map", metavar="FILE",
        help="cross-check the case count against the GDS files in each index "
             "path. Warning only: the run uses top cell names, which need not "
             "match the GDS filenames")
    status.add_argument("--no-qa", action="store_true",
                        help="observe only, run no QA checks")
    status.add_argument("--issues", action="store_true",
                        help="list every QA issue")

    # -- plan ----------------------------------------------------------
    plan = sub.add_parser("plan", help="produce a wave plan (never submits)")
    plan.add_argument("--dir-map", required=True, help="path to dir_map")
    plan.add_argument("--index", nargs="+", default=[],
                      help="indices to run (repeatable)")
    plan.add_argument("--all", action="store_true",
                      help="use every index in dir_map")
    plan.add_argument("--arcx-cfg", default="arcx.cfg",
                      help="path to arcx.cfg (only used to show the command)")
    plan.add_argument("--max-slots", type=int, help="slot cap per wave")
    plan.add_argument("--mode", choices=["auto", "off"], default="auto",
                      help="auto splits into waves; off submits everything at once")
    plan.add_argument("--show-command", action="store_true",
                      help="show the Arcx command each wave would run")
    plan.add_argument("--json", action="store_true", help="output JSON")

    # -- submit --------------------------------------------------------
    submit = sub.add_parser(
        "submit",
        help="check -> create wave dirs -> submit wave by wave (dry run by default)")
    submit.add_argument("--dir-map", required=True)
    submit.add_argument("--arcx-cfg", required=True)
    submit.add_argument("--index", nargs="+", default=[],
                        help="indices to run (repeatable)")
    submit.add_argument("--all", action="store_true",
                        help="use every index in dir_map")
    submit.add_argument("--max-slots", type=int, help="slot cap per wave")
    submit.add_argument("--mode", choices=["auto", "off"], default="auto")
    submit.add_argument("--run-id", help="name for this submission "
                        "(defaults to a timestamp)")
    submit.add_argument("--run-root", help="root for wave directories "
                        "(defaults to settings)")
    submit.add_argument("--max-waves", type=int,
                        help="submit at most this many waves (default: all)")
    submit.add_argument("--no-wait", action="store_true",
                        help="stop instead of waiting when the gate blocks")
    submit.add_argument(
        "--yes", action="store_true",
        help="actually do it. **Without this flag it is a dry run** that only "
             "checks and prints the commands, creating nothing and "
             "submitting nothing")

    # -- rerun ---------------------------------------------------------
    rerun = sub.add_parser(
        "rerun",
        help="stop, drain, clean and resubmit one wave (dry run by default)")
    rerun.add_argument("--wave-dir", required=True,
                       help="the wave directory to rerun")
    rerun.add_argument("--run-id", default="",
                       help="run id, used for the audit trail")
    rerun.add_argument("--arcx-cfg",
                       help="arcx.cfg for QA (the wave snapshot by default)")
    rerun.add_argument("--no-lsf", action="store_true",
                       help="do not query LSF while judging (offline preview)")
    rerun.add_argument(
        "--yes", action="store_true",
        help="actually do it. **Without this flag it is a dry run** that only "
             "shows which run dirs would be moved aside and rerun")

    # -- daemon --------------------------------------------------------
    daemon = sub.add_parser(
        "daemon",
        help="keep monitoring and write state for the web UI to read")
    daemon.add_argument("--run-id", help="name for this monitoring session "
                        "(defaults to a timestamp)")
    daemon.add_argument("--wave-dir", action="append", default=[],
                        help="wave directory to monitor (repeatable)")
    daemon.add_argument("--run-folder", action="append", default=[],
                        help="index run folder to monitor (repeatable)")
    daemon.add_argument("--arcx-cfg",
                        help="path to arcx.cfg (found in the wave dir by default)")
    daemon.add_argument("--interval", type=float,
                        help="fixed scan interval in seconds; by default it "
                             "adapts to whether cases are running")
    daemon.add_argument("--once", action="store_true", help="scan once and exit")
    daemon.add_argument("--no-lsf", action="store_true", help="do not query LSF")

    # -- web -----------------------------------------------------------
    web = sub.add_parser("web",
                         help="serve the local web UI (read only, 127.0.0.1)")
    web.add_argument("--host", default="127.0.0.1",
                     help="loopback only by default; any other address "
                          "publishes this as a service")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--refresh", type=int, default=30,
                     help="page auto-refresh in seconds, 0 to disable")

    # -- check-cfg -----------------------------------------------------
    check = sub.add_parser("check-cfg",
                           help="validate arcx.cfg before submitting "
                                "(no run folder needed)")
    check.add_argument("path", help="path to arcx.cfg")
    check.add_argument("--json", action="store_true")

    # -- inspect -------------------------------------------------------
    inspect = sub.add_parser("inspect",
                             help="parse input files to verify our assumptions")
    inspect_sub = inspect.add_subparsers(dest="subject", required=True)

    dm = inspect_sub.add_parser("dir-map", help="parse dir_map")
    dm.add_argument("path")
    dm.add_argument("--verify", action="store_true",
                    help="check that each path exists")
    dm.add_argument("--json", action="store_true")

    idx = inspect_sub.add_parser("index",
                                 help="parse an index path: special.cfg and GDS")
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
                print("scanned at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
            print(render.render_status(
                snapshots, now=time.time(), detail=args.detail,
                lsf_note=lsf_note, observations=observations,
                qa_reports=reports, show_issues=args.issues,
            ))

        if not interval:
            return 0


def _scan_once(args, settings, fs, collector, store):
    """One scan. The logic lives in MonitorService; this only does the I/O."""
    monitor = MonitorService(
        settings=settings, collector=collector, enable_qa=not args.no_qa)
    if store:
        monitor.prime(store.load())

    result = monitor.scan(
        wave_dirs=[args.wave_dir] if args.wave_dir else [],
        run_folders=list(args.run_folder or []),
        arcx_config=_load_arcx_cfg(args, settings),
        use_lsf=not args.no_lsf,
        gds_counts=_gds_counts(args, settings, fs),
    )

    if args.wave_dir and not result.observations:
        print("  (no index run folder found under the wave directory: %s)"
              % args.wave_dir, file=sys.stderr)

    if store:
        store.save(monitor.previous)

    return (list(result.snapshots), list(result.observations),
            list(result.qa_reports), result.lsf_note)


def _gds_counts(args, settings: Settings, fs) -> Dict[str, int]:
    """How many GDS files each index path holds, when a dir_map was given.

    Used only as a cross-check on the case count. The run works on top cell
    names, which need not match the GDS filenames, so this can never name a
    case -- it can only notice that the totals disagree.
    """
    path = getattr(args, "dir_map", None)
    if not path:
        return {}
    from arcx_auto.adapters.arcx import ArcxAdapter

    arcx = ArcxAdapter(settings.layout, settings.plan, fs=fs)
    dir_map = arcx.parse_dir_map(path)
    counts: Dict[str, int] = {}
    for index_key, index_path in dir_map.entries.items():
        count, _names = fs.count_gds(index_path)
        if count:
            counts[index_key] = count
    return counts


def _load_arcx_cfg(args, settings: Settings) -> Optional[ArcxConfig]:
    """Locate arcx.cfg.

    Order: --arcx-cfg > <wave_dir>/arcx.cfg > <run_folder>/../arcx.cfg
    Not finding one is not an error: QA raises CFG_EXPECTATION_UNAVAILABLE,
    an UNKNOWN issue that says "I do not know what to check" instead of
    quietly passing.
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
        print("error: name indices with --index, or use --all", file=sys.stderr)
        return 2

    specs: List[IndexSpec] = []
    for key in index_keys:
        path = dir_map.resolve(key)
        if path is None:
            specs.append(IndexSpec(
                index_key=key, path="", gds_count=0, cpu_per_case=0,
                error="index not found in dir_map",
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
    """The submission flow. **A dry run by default** -- this is the first
    command in the system that writes to disk, so it takes an explicit --yes.
    """
    arcx = ArcxAdapter(settings.layout, settings.plan)
    dir_map = arcx.parse_dir_map(args.dir_map)
    for warning in dir_map.warnings:
        print("  ! dir_map: %s" % warning, file=sys.stderr)

    index_keys = dir_map.keys_sorted() if args.all else list(args.index)
    if not index_keys:
        print("error: name indices with --index, or use --all", file=sys.stderr)
        return 2

    specs = []
    for key in index_keys:
        path = dir_map.resolve(key)
        if path is None:
            specs.append(IndexSpec(index_key=key, path="", gds_count=0,
                                   cpu_per_case=0,
                                   error="index not found in dir_map"))
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
# rerun
# --------------------------------------------------------------------------

def cmd_rerun(args: argparse.Namespace, settings: Settings) -> int:
    """Rerun one wave.

    **A dry run by default.** This is the only command that deletes anything,
    so it takes an explicit --yes, and even then the "deletion" is a move into
    .arcx_auto/attempts/N/ so the failed state survives.
    """
    wave_dir = os.path.abspath(os.path.expanduser(args.wave_dir))
    if not os.path.isdir(wave_dir):
        print("error: no such wave directory: %s" % wave_dir, file=sys.stderr)
        return 2

    # Observe the wave exactly as the monitor does, so the rerun decision and
    # the status display can never disagree.
    monitor = MonitorService(settings)
    result = monitor.scan(
        wave_dirs=[wave_dir],
        arcx_config=_load_arcx_cfg(args, settings),
        use_lsf=not args.no_lsf,
    )
    if not result.snapshots:
        print("error: no index run folder found under %s" % wave_dir,
              file=sys.stderr)
        return 2

    launch = read_launch(wave_dir)
    manifest = read_json(
        os.path.join(wave_dir, ".arcx_auto", "manifest.json"), default={}) or {}

    plan = build_rerun_plan(
        wave_dir=wave_dir,
        wave_name=manifest.get("wave") or os.path.basename(wave_dir),
        snapshots=result.snapshots,
        qa_reports=result.qa_reports,
        launch=launch,
        manifest=manifest,
    )

    dry_run = not args.yes
    print(render.render_rerun_plan(plan, dry_run=dry_run))

    if plan.blockers:
        return 1
    if not plan.to_delete:
        return 0
    if dry_run:
        return 0

    store = RunStore(settings.expanded_state_root(),
                     args.run_id or plan.wave_name)
    store.ensure()
    remediator = Remediator(settings, store=store)
    outcome = remediator.run(
        plan, run_id=args.run_id or plan.wave_name,
        on_progress=lambda msg: print("  %s" % msg))

    print(render.render_rerun_outcome(outcome))
    return 0 if outcome.ok else 1


# --------------------------------------------------------------------------
# daemon / web
# --------------------------------------------------------------------------

def cmd_daemon(args: argparse.Namespace, settings: Settings) -> int:
    if not args.wave_dir and not args.run_folder:
        print("error: give at least one --wave-dir or --run-folder",
              file=sys.stderr)
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
    print("monitoring run_id=%s  state=%s" % (run_id, daemon.store.dir))
    if not args.once:
        print("web UI: arcx-auto web    (Ctrl-C stops the daemon)")
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
        print("  ! note: binding to %s publishes this service to others."
              % options.host,
              file=sys.stderr)
    print("Ctrl-C to stop.")
    try:
        serve(options)
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------
# check-cfg
# --------------------------------------------------------------------------

def cmd_check_cfg(args: argparse.Namespace, settings: Settings) -> int:
    """Validate arcx.cfg before submitting.

    Exits 1 when there is a FATAL, so it can be chained into a submit script.
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
        print("failed to load settings: %s" % exc, file=sys.stderr)
        return 2
    for warning in warnings:
        print("  ! settings: %s" % warning, file=sys.stderr)

    handlers = {
        "status": cmd_status,
        "submit": cmd_submit,
        "rerun": cmd_rerun,
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
        print("\ninterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
