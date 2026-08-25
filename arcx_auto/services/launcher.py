"""Launcher -- submit Arcx itself through bsub.

    bsub -q LVSRCE-0E.q -oo Arcx.log "Arcx -p arcx.cfg -d 1000 1001 -lsf0 -nt 50 --run"

Two details that are easy to get wrong and expensive to debug:

  * **The Arcx invocation is one shell string**, not separate argv entries.
    That is how it is written by hand, and bsub treats the trailing argument
    as the command to run.
  * **bsub must be executed from inside the directory the run belongs to.**
    LSF records the submission directory and Arcx creates its per-index run
    folders relative to it, so isolation depends entirely on the cwd. That is
    also why one wave is several submissions: each source folder has its own
    directory, and a directory is one Arcx command.

Why Arcx itself is submitted rather than spawned (architecture decision A1c):
our daemon can then be restarted, crash, or be killed without touching work
that is already running -- and these runs last for days. The outer job is only
a coordinator, so it needs no memory or CPU reservation; the real work is
submitted by Arcx as further LSF jobs.

The job id is written to launch.json in the wave directory. A rerun must bkill
that parent first and only then clear the child jobs -- the other order lets
the parent submit replacements.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from arcx_auto.adapters.arcx import ArcxAdapter
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.config.settings import Settings
from arcx_auto.services.workspace import WaveWorkspace
from arcx_auto.util.atomic import atomic_write_json, read_json

# Standard bsub acknowledgement: Job <12345> is submitted to queue <normal>.
_JOB_ID_RE = re.compile(r"Job\s+<(?P<job_id>\d+)>")


@dataclass(frozen=True)
class LaunchResult:
    wave_name: str
    ok: bool
    #: Which directory inside the wave this was, when the wave has batches.
    batch_name: str = ""
    job_id: Optional[str] = None
    command: tuple = ()
    cwd: Optional[str] = None
    stdout: str = ""
    error: Optional[str] = None
    dry_run: bool = False


class Launcher:
    """Submits one wave."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        lsf: Optional[LsfAdapter] = None,
        arcx: Optional[ArcxAdapter] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.lsf = lsf or LsfAdapter(self.settings.lsf)
        self.arcx = arcx or ArcxAdapter(self.settings.layout, self.settings.plan)

    # ------------------------------------------------------------------

    def build_command(self, workspace: WaveWorkspace, run_id: str,
                      rerun: bool = False) -> List[str]:
        """Assemble the full bsub argv.

        The cfg is the *snapshot* inside the wave directory, not the original.
        Later edits to the original then cannot affect work already submitted,
        and QA three days from now reads the same file the run used.
        """
        launch = self.settings.launch

        argv: List[str] = [self.settings.lsf.bsub_cmd]
        if launch.queue:
            argv.extend(["-q", launch.queue])
        if launch.output_file:
            # -oo overwrites rather than appends, so a rerun does not stack
            # its output on top of the previous attempt's.
            argv.extend(["-oo", launch.output_file])
        if launch.job_name_template:
            # The batch, when there is one: several Arcx parents belong to one
            # wave now, and identical job names in bjobs help nobody.
            argv.extend(["-J", launch.job_name_template.format(
                run_id=run_id or "run", wave=workspace.label)])
        argv.extend(launch.bsub_args)

        arcx_argv = self.arcx.build_run_command(
            workspace.arcx_cfg, list(workspace.index_keys),
            rerun=rerun, lsf_settings=self.settings.lsf,
        )
        # One string, matching how the command is written by hand.
        argv.append(" ".join(arcx_argv))
        return argv

    def launch(
        self,
        workspace: WaveWorkspace,
        run_id: str,
        rerun: bool = False,
        dry_run: bool = False,
        now: Optional[float] = None,
    ) -> LaunchResult:
        """Submit. With dry_run the command is assembled but never executed."""
        now = now if now is not None else time.time()
        argv = self.build_command(workspace, run_id, rerun=rerun)

        if dry_run:
            return LaunchResult(wave_name=workspace.wave_name, ok=True,
                                batch_name=workspace.batch_name,
                                command=tuple(argv), cwd=workspace.path,
                                dry_run=True)

        # cwd is what makes wave isolation work -- see the module docstring.
        result = self.lsf._run(argv, cwd=workspace.path)  # noqa: SLF001
        if not result.ok:
            error = result.error or result.stderr.strip() or "bsub failed"
            self._record(workspace, run_id, argv, None, error, now, rerun)
            return LaunchResult(wave_name=workspace.wave_name, ok=False,
                                batch_name=workspace.batch_name,
                                command=tuple(argv), cwd=workspace.path,
                                stdout=result.stdout, error=error)

        match = _JOB_ID_RE.search(result.stdout)
        job_id = match.group("job_id") if match else None
        error = None if job_id else "bsub succeeded but no job id in its output"

        self._record(workspace, run_id, argv, job_id, error, now, rerun)
        return LaunchResult(
            wave_name=workspace.wave_name, ok=job_id is not None, job_id=job_id,
            batch_name=workspace.batch_name,
            command=tuple(argv), cwd=workspace.path, stdout=result.stdout,
            error=error,
        )

    # ------------------------------------------------------------------

    def _record(self, workspace: WaveWorkspace, run_id: str, argv: List[str],
                job_id: Optional[str], error: Optional[str], now: float,
                rerun: bool) -> None:
        """Append this submission to the wave's launch.json.

        Appending rather than replacing keeps every attempt: after a rerun you
        can still see when and how the first submission went out.
        """
        existing = read_json(workspace.launch_json, default=None) or {}
        attempts = list(existing.get("attempts") or [])
        attempts.append({
            "attempt": len(attempts) + 1,
            "submitted_at": now,
            "job_id": job_id,
            "command": argv,
            "cwd": workspace.path,
            "rerun": rerun,
            "error": error,
        })
        atomic_write_json(workspace.launch_json, {
            "run_id": run_id,
            "wave": workspace.wave_name,
            "batch": workspace.batch_name,
            "folder": workspace.folder,
            "index_keys": list(workspace.index_keys),
            "arcx_job_id": job_id or existing.get("arcx_job_id"),
            "attempts": attempts,
        })


def read_launch(workspace_path: str) -> Dict[str, Any]:
    """Read a wave's submission record. A rerun reads the parent job id here."""
    return read_json(
        os.path.join(workspace_path, ".arcx_auto", "launch.json"),
        default={}) or {}
