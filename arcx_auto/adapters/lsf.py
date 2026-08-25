"""LSF adapter.

Design notes:

  * **Batch queries.** One bjobs call returns every job the account owns and we
    filter in memory. Never call bjobs per job -- a few hundred invocations will
    hammer the LSF master.
  * **Degrade gracefully.** On a dev box, or when LSF is temporarily down, every
    query returns None/empty plus a reason instead of raising. A monitoring
    system must not stop working because bjobs hiccuped.
  * Every external command has a timeout.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from arcx_auto.config.settings import LsfSettings
from arcx_auto.domain.enums import LsfState
from arcx_auto.domain.models import LsfJobView

# bjobs -o field order, matched one-to-one by _parse_bjobs
_BJOBS_FIELDS = ["jobid", "stat", "exec_cwd", "sub_cwd", "output_file",
                 "exec_host", "job_name"]

#: The fields that hold paths. bjobs truncates a value that does not fit its
#: column, and these are the ones long enough for that to happen.
_BJOBS_PATH_FIELDS = ("exec_cwd", "sub_cwd", "output_file")

# bjobs_manage.py -jp prints a summary, not a job list:
#     grep all jobs...
#     finished, total 304 jobs
#     total 299 jobs in path
# The second number is the one scoped to the requested path.
_JOBS_IN_PATH_RE = re.compile(r"total\s+(?P<count>\d+)\s+jobs?\s+in\s+path",
                              re.IGNORECASE)


class LsfUnavailable(Exception):
    """The LSF command is missing or cannot be executed."""


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    error: Optional[str] = None


class LsfAdapter:
    """Wraps every LSF access."""

    def __init__(
        self,
        settings: Optional[LsfSettings] = None,
        user: Optional[str] = None,
    ) -> None:
        self.settings = settings or LsfSettings()
        self.user = user or _current_user()
        self._availability: Dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self, command: Optional[str] = None) -> bool:
        """Whether the command exists on PATH. Cached to avoid re-checking
        on every tick.
        """
        cmd = command or self.settings.bjobs_cmd
        if cmd not in self._availability:
            self._availability[cmd] = shutil.which(cmd) is not None
        return self._availability[cmd]

    def _run(self, argv: List[str],
             cwd: Optional[str] = None) -> CommandResult:
        """Run an external command.

        ``cwd`` matters for bsub: LSF records the submission directory, and
        Arcx creates its per-index run folders relative to it. Wave isolation
        depends entirely on submitting from inside the wave directory.
        """
        if not self.is_available(argv[0]):
            return CommandResult(
                ok=False, stdout="", stderr="", returncode=127,
                error="command not found: %s" % argv[0],
            )
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.settings.command_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                ok=False, stdout="", stderr="", returncode=-1,
                error="command timed out after %.0fs: %s"
                      % (self.settings.command_timeout_sec, " ".join(argv)),
            )
        except OSError as exc:
            return CommandResult(
                ok=False, stdout="", stderr="", returncode=-1,
                error="command failed: %s" % exc,
            )
        return CommandResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
            returncode=proc.returncode,
        )

    # ------------------------------------------------------------------
    # bjobs
    # ------------------------------------------------------------------

    def list_user_jobs(self) -> Tuple[List[LsfJobView], Optional[str]]:
        """Fetch every job owned by this account in one call.

        exec_cwd / sub_cwd / output_file are how a job is mapped back to a wave
        directory: the child jobs Arcx submits carry no identifiable job name,
        so path ownership is the only handle we have.
        """
        argv = [
            self.settings.bjobs_cmd,
            "-u", self.user,
            "-o", self._bjobs_format(),
            "-noheader",
        ]
        result = self._run(argv)
        if not result.ok:
            # With no jobs at all bjobs exits non-zero and prints this on stderr
            if "No unfinished job found" in (result.stderr + result.stdout):
                return ([], None)
            return ([], result.error or result.stderr.strip() or "bjobs failed")
        return (self._parse_bjobs(result.stdout), None)

    def _bjobs_format(self) -> str:
        """The -o spec, with the path columns made wide enough to survive.

        Without an explicit width bjobs uses its own, which is far narrower
        than a real NFS path once a run id, a wave and a batch directory are
        in it. A truncated path matches no case, and the case then looks like
        one whose job has disappeared -- which is a false alarm produced
        entirely by a display width.
        """
        width = max(0, int(getattr(self.settings, "bjobs_path_width", 0) or 0))
        parts = []
        for field in _BJOBS_FIELDS:
            if width and field in _BJOBS_PATH_FIELDS:
                parts.append("%s:%d" % (field, width))
            else:
                parts.append(field)
        return " ".join(parts)

    @staticmethod
    def _parse_bjobs(text: str) -> List[LsfJobView]:
        jobs: List[LsfJobView] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            padded = parts + ["-"] * (len(_BJOBS_FIELDS) - len(parts))
            values = dict(zip(_BJOBS_FIELDS, padded))
            try:
                state = LsfState(values["stat"].upper())
            except ValueError:
                state = LsfState.UNKWN
            paths = {}
            truncated = False
            for field in _BJOBS_PATH_FIELDS:
                value, was_cut = _untruncate(values[field])
                paths[field] = value
                truncated = truncated or was_cut
            jobs.append(
                LsfJobView(
                    job_id=values["jobid"],
                    state=state,
                    exec_cwd=paths["exec_cwd"],
                    sub_cwd=paths["sub_cwd"],
                    output_file=paths["output_file"],
                    exec_host=_none_if_dash(values["exec_host"]),
                    job_name=_none_if_dash(values["job_name"]),
                    truncated=truncated,
                )
            )
        return jobs

    def jobs_under_path(self, path: str) -> Tuple[List[LsfJobView], Optional[str]]:
        """Jobs owned by this account that live under a path prefix."""
        jobs, error = self.list_user_jobs()
        if error:
            return ([], error)
        prefix = os.path.abspath(os.path.expanduser(path))
        return ([j for j in jobs if j.belongs_to(prefix)], None)

    # ------------------------------------------------------------------
    # busers -- the quota source for the submission gate
    # ------------------------------------------------------------------

    def current_njobs(self) -> Tuple[Optional[int], Optional[str]]:
        """Read the NJOBS column from busers.

        Sample output:
            USER/GROUP   JL/P  MAX  NJOBS  PEND  RUN  SSUSP  USUSP  RSV
            myuser          -    -     12     3    9      0      0    0
        """
        result = self._run([self.settings.busers_cmd, self.user])
        if not result.ok:
            return (None, result.error or result.stderr.strip() or "busers failed")

        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if len(lines) < 2:
            return (None, "could not parse busers output")

        header = lines[0].split()
        try:
            njobs_idx = header.index("NJOBS")
        except ValueError:
            return (None, "no NJOBS column in busers output")

        for line in lines[1:]:
            parts = line.split()
            if len(parts) <= njobs_idx:
                continue
            try:
                return (int(parts[njobs_idx]), None)
            except ValueError:
                continue
        return (None, "no row for this account in busers output")

    # ------------------------------------------------------------------
    # bjobs_manage.py -- list / delete jobs by path
    # ------------------------------------------------------------------

    def count_jobs_under_path(
        self, path: str
    ) -> Tuple[Optional[int], str, Optional[str]]:
        """Count the jobs under a path using bjobs_manage.py -jp.

        Returns (count, raw_output, error). The tool reports a *count*, not a
        job list, so this is a count -- see _JOBS_IN_PATH_RE.

        **A count of None means "unknown", never "zero".** This feeds the drain
        safety gate (VERIFY_QUIESCENT); treating "I could not tell" as "no jobs
        left" would let us delete files while jobs are still running, which is
        the single most destructive mistake this system could make.
        """
        argv = [
            self.settings.bjobs_manage_cmd,
            self.settings.bjobs_manage_list_flag,
            _trailing_slash(path),
        ]
        result = self._run(argv)
        if not result.ok:
            return (None, result.stdout,
                    result.error or result.stderr.strip()
                    or "bjobs_manage.py failed")

        count = parse_jobs_in_path(result.stdout)
        if count is None:
            return (None, result.stdout,
                    "could not find a 'total N jobs in path' line in the output")
        return (count, result.stdout, None)

    def kill_jobs_under_path(
        self, path: str
    ) -> Tuple[bool, str, Optional[str]]:
        """Delete every job under a path using bjobs_manage.py -djp.

        The caller must bkill the parent Arcx job *first*; otherwise the parent
        simply submits replacements (architecture 9.4). This adapter does not
        imply that ordering -- the Remediator enforces it.
        """
        argv = [
            self.settings.bjobs_manage_cmd,
            self.settings.bjobs_manage_delete_flag,
            _trailing_slash(path),
        ]
        result = self._run(argv)
        if not result.ok:
            return (False, result.stdout,
                    result.error or result.stderr.strip()
                    or "bjobs_manage.py failed")
        return (True, result.stdout, None)

    def kill_job(self, job_id: str) -> Tuple[bool, Optional[str]]:
        """bkill a single job (used for the parent Arcx job)."""
        result = self._run([self.settings.bkill_cmd, str(job_id)])
        if not result.ok:
            return (False, result.error or result.stderr.strip() or "bkill failed")
        return (True, None)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_jobs_in_path(text: str) -> Optional[int]:
    """Extract the job count from bjobs_manage.py -jp output.

        grep all jobs...
        finished, total 304 jobs      <- every job, not what we want
        total 299 jobs in path        <- scoped to the path, this one

    Returns None when the line is absent. Callers must treat None as unknown.
    """
    for line in text.splitlines():
        match = _JOBS_IN_PATH_RE.search(line)
        if match:
            return int(match.group("count"))
    return None


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - rare environments without a pwd entry
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def _untruncate(value: str) -> Tuple[Optional[str], bool]:
    """A path from bjobs, and whether bjobs cut it short.

    bjobs marks a value it had to shorten with a trailing asterisk. What is
    left is still a valid prefix -- good enough to say which wave a job
    belongs to -- but it can never be compared for equality again, and doing
    so anyway is how a job silently stops being matched to its case.
    """
    cleaned = _none_if_dash(value)
    if cleaned is None:
        return (None, False)
    if cleaned.endswith("*"):
        return (cleaned[:-1] or None, True)
    return (cleaned, False)


def _none_if_dash(value: str) -> Optional[str]:
    value = (value or "").strip()
    return None if value in ("", "-") else value


def _trailing_slash(path: str) -> str:
    resolved = os.path.abspath(os.path.expanduser(path))
    return resolved.rstrip("/") + "/"
