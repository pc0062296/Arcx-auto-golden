"""Turn a queued intent into work. The only consumer of the command queue.

This is where a request from the browser becomes a submission or a rerun. It
sits between the queue (which carries what somebody asked for) and the services
that already know how to do it, and it adds exactly one thing: **the intent is
validated before anything is touched.**

That matters because a command file is the one input to this system that was
not written by the system. It arrives as JSON with whatever fields it has, and
by the time it reaches WorkspaceBuilder it is deciding where directories get
created. Every path in it is resolved and checked here first, and a command
that does not make sense is completed as a failure rather than half executed.

Executing runs in the daemon, never in the web server, for two reasons that are
both about time rather than about permissions:

  * the gate can wait two hours for the LSF quota, and a browser request cannot
  * the daemon is already the single writer, so nothing new has to be made safe
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from arcx_auto.adapters.arcx import ArcxAdapter
from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import Settings
from arcx_auto.domain.enums import PlanMode
from arcx_auto.domain.models import IndexSpec, SubmitGroup
from arcx_auto.services.commands import Command, CommandQueue
from arcx_auto.services.monitor import MonitorService
from arcx_auto.services.rerun_planner import build_rerun_plan
from arcx_auto.services.remediator import Remediator
from arcx_auto.services.submitter import Submitter
from arcx_auto.services.wave_planner import plan_groups
from arcx_auto.util.atomic import read_json


class IntentError(Exception):
    """The command does not describe something that can be done.

    Deliberately raised before any directory is created or any job is sent: a
    command that is wrong should fail having changed nothing.
    """


@dataclass
class ExecutionResult:
    ok: bool
    summary: str = ""
    detail: Dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.detail is None:
            self.detail = {}


class CommandExecutor:
    """Executes queued commands. Called only by the daemon."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        submitter: Optional[Submitter] = None,
        remediator: Optional[Remediator] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        stop=None,
    ) -> None:
        self.settings = settings or Settings()
        # A submission waits at the gate, and that wait has to end when the
        # daemon is asked to stop, or Ctrl-C looks like it did nothing.
        self.stop = stop
        self.submitter = submitter or Submitter(self.settings, stop=stop)
        self.remediator = remediator
        self.queue = CommandQueue(self.settings.expanded_state_root())
        self.say = on_progress or (lambda _msg: None)

    # ------------------------------------------------------------------

    def drain(self, limit: int = 1, now: Optional[float] = None) -> List[Command]:
        """Run up to ``limit`` queued commands.

        One per tick by default. A submission can sit at the gate for a long
        time, and running several at once would put the whole queue behind
        whichever is slowest while also spending the LSF quota in parallel --
        the exact thing the gate exists to prevent.
        """
        done: List[Command] = []
        for _ in range(max(1, limit)):
            command = self.queue.claim_next(now=now)
            if command is None:
                break
            self.run_one(command)
            done.append(command)
        return done

    def run_one(self, command: Command) -> ExecutionResult:
        """Execute one claimed command and record its outcome."""
        try:
            if command.kind == "submit":
                result = self._submit(command)
            elif command.kind == "rerun":
                result = self._rerun(command)
            else:
                raise IntentError("unknown command kind: %r" % command.kind)
        except IntentError as exc:
            self.queue.complete(command, ok=False, error=str(exc))
            return ExecutionResult(False, str(exc))
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the daemon
            import traceback

            self.queue.complete(
                command, ok=False, error=traceback.format_exc(limit=6))
            return ExecutionResult(False, str(exc))

        self.queue.complete(command, ok=result.ok, result=result.detail,
                            error="" if result.ok else result.summary)
        return result

    # ------------------------------------------------------------------
    # submit
    # ------------------------------------------------------------------

    def _submit(self, command: Command) -> ExecutionResult:
        payload = command.payload
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            raise IntentError("the command has no run_id")

        groups = self._read_groups(payload)
        specs = self._build_specs(groups)
        plan = plan_groups(
            specs,
            max_slots_per_wave=int(payload.get("max_slots")
                                   or self.settings.plan.max_slots_per_wave),
            mode=(PlanMode.AUTO if payload.get("mode", "auto") == "auto"
                  else PlanMode.OFF),
        )
        if not plan.waves:
            raise IntentError(
                "nothing to submit: %s"
                % ("; ".join(plan.warnings) or "no usable index was selected"))

        run_root = str(payload.get("run_root")
                       or self.settings.expanded_run_root())

        self.say("submitting %s: %d wave(s)" % (run_id, len(plan.waves)))
        outcome = self.submitter.submit(
            plan=plan,
            run_id=run_id,
            run_root=run_root,
            # Each wave carries its own pair; these are only the fallback for a
            # single-group submission.
            arcx_cfg=plan.waves[0].arcx_cfg,
            dir_map=plan.waves[0].dir_map,
            dry_run=False,
            on_progress=self.say,
        )

        detail = {
            "run_id": run_id,
            "run_dir": outcome.run_dir,
            "waves": [w.wave_name for w in outcome.workspaces],
            "submitted": outcome.submitted_count,
            "pending": list(outcome.pending_waves),
            "blocked": outcome.blocked,
            "error": outcome.error,
        }
        if outcome.blocked:
            return ExecutionResult(
                False, "blocked by pre-submission checks", detail)
        if outcome.error:
            return ExecutionResult(False, outcome.error, detail)
        return ExecutionResult(
            True, "submitted %d wave(s)" % outcome.submitted_count, detail)

    def _read_groups(self, payload: Dict[str, Any]) -> List[SubmitGroup]:
        """Read and validate the groups a person selected.

        Every path is resolved and checked here, before anything is created.
        A command file is the one input this system did not write itself.
        """
        raw = payload.get("groups")
        if not isinstance(raw, list) or not raw:
            raise IntentError("the command lists no groups")

        groups: List[SubmitGroup] = []
        for position, entry in enumerate(raw, start=1):
            if not isinstance(entry, dict):
                raise IntentError("group %d is not an object" % position)
            name = str(entry.get("name") or "group_%d" % position)
            dir_map = _require_file(entry.get("dir_map"),
                                    "%s: dir_map" % name)
            arcx_cfg = _require_file(entry.get("arcx_cfg"),
                                     "%s: arcx.cfg" % name)
            keys = entry.get("index_keys") or []
            if not isinstance(keys, list) or not keys:
                raise IntentError("%s selects no index" % name)
            groups.append(SubmitGroup(
                name=name, dir_map=dir_map, arcx_cfg=arcx_cfg,
                index_keys=tuple(str(k) for k in keys),
                # Absent means on: an older queued command, or one written by
                # hand, gets the behaviour the settings describe rather than
                # silently the other one.
                keep_folders_together=bool(
                    entry.get("keep_folders_together", True))))
        return groups

    def _build_specs(
        self, groups: Sequence[SubmitGroup]
    ) -> List[Tuple[SubmitGroup, List[IndexSpec]]]:
        """Read each group's dir_map and size the indices it selected."""
        fs = FsAdapter(self.settings.layout)
        arcx = ArcxAdapter(self.settings.layout, self.settings.plan, fs=fs)

        out: List[Tuple[SubmitGroup, List[IndexSpec]]] = []
        for group in groups:
            dir_map = arcx.parse_dir_map(group.dir_map)
            specs: List[IndexSpec] = []
            for key in group.index_keys:
                path = dir_map.entries.get(key)
                if not path:
                    raise IntentError(
                        "%s: index %s is not in %s"
                        % (group.label, key, group.dir_map))
                specs.append(arcx.build_index_spec(key, path))
            out.append((group, specs))
        return out

    # ------------------------------------------------------------------
    # rerun
    # ------------------------------------------------------------------

    def _rerun(self, command: Command) -> ExecutionResult:
        payload = command.payload
        wave_dir = _require_dir(payload.get("wave_dir"), "wave_dir")
        run_id = str(payload.get("run_id") or os.path.basename(wave_dir))

        monitor = MonitorService(self.settings)
        result = monitor.scan(wave_dirs=[wave_dir], use_lsf=True)
        if not result.snapshots:
            raise IntentError(
                "no index run folder found under %s" % wave_dir)

        manifest = read_json(
            os.path.join(wave_dir, ".arcx_auto", "manifest.json"),
            default={}) or {}
        launch = read_json(
            os.path.join(wave_dir, ".arcx_auto", "launch.json"),
            default={}) or {}

        plan = build_rerun_plan(
            wave_dir=wave_dir,
            wave_name=manifest.get("wave") or os.path.basename(wave_dir),
            snapshots=result.snapshots,
            qa_reports=result.qa_reports,
            launch=launch,
            manifest=manifest,
        )

        # What the person was shown must be what happens. If the wave moved on
        # between the page being rendered and this running, the operation is
        # refused rather than applied to a different set of cases.
        expected = payload.get("expect_delete")
        actual = sorted(d.case_id for d in plan.to_delete)
        if expected is not None and sorted(str(c) for c in expected) != actual:
            raise IntentError(
                "the wave changed since the page was shown: it asked to rerun "
                "%s, and the plan now says %s. Nothing was done; reload and "
                "decide again."
                % (", ".join(sorted(str(c) for c in expected)) or "(none)",
                   ", ".join(actual) or "(none)"))

        if plan.blockers:
            raise IntentError("; ".join(plan.blockers))
        if not plan.to_delete:
            return ExecutionResult(
                True, "nothing needed rerunning", {"wave_dir": wave_dir})

        store = RunStore(self.settings.expanded_state_root(), run_id)
        store.ensure()
        remediator = self.remediator or Remediator(self.settings, store=store)
        outcome = remediator.run(plan, run_id=run_id, on_progress=self.say)

        detail = {
            "wave_dir": wave_dir,
            "attempt": plan.attempt,
            "moved": list(outcome.backed_up),
            "resubmit_job_id": outcome.resubmit_job_id,
            "phase": outcome.phase.value,
        }
        if not outcome.ok:
            return ExecutionResult(
                False, outcome.error or "the rerun did not complete", detail)
        return ExecutionResult(
            True, "reran %d case(s)" % len(outcome.backed_up), detail)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _require_file(value: Any, label: str) -> str:
    path = str(value or "").strip()
    if not path:
        raise IntentError("%s is missing" % label)
    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(resolved):
        raise IntentError("%s not found: %s" % (label, resolved))
    return resolved


def _require_dir(value: Any, label: str) -> str:
    path = str(value or "").strip()
    if not path:
        raise IntentError("%s is missing" % label)
    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        raise IntentError("%s is not a directory: %s" % (label, resolved))
    return resolved
