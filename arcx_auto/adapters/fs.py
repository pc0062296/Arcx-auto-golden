"""Filesystem adapter.

A real index run folder looks like this:

    .queue.NDIO_1                  markers; the case id is a cell name
    .run.PDIO_1
    .complete.NTN_1
    NDIO_1/  PDIO_1/  NTN_1/       per-case run dirs (a rerun deletes these)
    QC_Cc/  QC_Ct/  QC_Spice/      reports Arcx assembles, not cases
    submit_bjob_cmd_file_1.log     logs, named only by sequence number
    cmd_folder/cmd_file_1          the submitted script, containing `cd <dir>`

Two conventions drive the implementation:

  1. **Case ids are cell names with no shared pattern.** Case run dirs are
     therefore identified by exclusion (not QC_*, not cmd_folder, not hidden)
     rather than by an include pattern.

  2. **Log filenames say nothing about their case.** submit_bjob_cmd_file_1.log
     pairs by number with cmd_folder/cmd_file_1, and that script's `cd <path>`
     names the case. It is the only reliable mapping -- the numbering is not
     guaranteed to match any ordering.

Performance notes for NFS folders with tens of thousands of entries:
  * one os.scandir pass classifies the whole directory in memory; never
    per-case exists() calls, which become N NFS round trips
  * cmd_file contents are read once and cached; the script never changes
  * logs are only stat'ed for size and mtime, never read
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Set, Tuple

from arcx_auto.config.settings import LayoutSettings
from arcx_auto.domain.enums import MarkerKind
from arcx_auto.domain.models import CaseObservation, IndexRunObservation


class FsAdapter:
    """Wraps every read-only access to a run folder."""

    def __init__(self, layout: Optional[LayoutSettings] = None) -> None:
        self.layout = layout or LayoutSettings()
        self._marker_re = re.compile(self.layout.marker_regex)
        self._marker_any_re = re.compile(self.layout.marker_any_regex)
        self._log_re = re.compile(self.layout.log_regex)
        self._report_re = re.compile(self.layout.report_dir_regex)
        self._cd_re = re.compile(self.layout.cmd_cd_regex)
        self._non_case_res = [
            re.compile(pattern) for pattern in self.layout.non_case_dir_regexes
        ]
        # cmd_file path -> resolved execution path. Read once; scripts do not
        # change after submission.
        self._cmd_cache: Dict[str, Optional[str]] = {}

    # ------------------------------------------------------------------
    # Scanning an index run folder
    # ------------------------------------------------------------------

    def scan_index_run_folder(
        self,
        run_folder: str,
        index_key: Optional[str] = None,
        now: Optional[float] = None,
    ) -> IndexRunObservation:
        """Scan one index run folder into an observation.

        Cases are the **union** of markers, case run dirs, and cmd_file-resolved
        logs. Each missing piece signals a different failure, so looking at only
        one of them loses information:
          - dir but no marker  -> the case exists but was never submitted
          - marker but no dir  -> the directory was deleted, or not yet created
          - log but no marker  -> the marker write failed
        """
        import time

        now = now if now is not None else time.time()
        run_folder = os.path.abspath(os.path.expanduser(run_folder))
        key = index_key if index_key is not None else os.path.basename(run_folder)

        if not os.path.isdir(run_folder):
            return IndexRunObservation(
                index_key=key,
                run_folder=run_folder,
                observed_at=now,
                error="run folder does not exist or is not a directory",
            )

        try:
            entries = list(os.scandir(run_folder))
        except OSError as exc:
            return IndexRunObservation(
                index_key=key,
                run_folder=run_folder,
                observed_at=now,
                error="cannot read run folder: %s" % exc,
            )

        markers: Dict[str, Set[MarkerKind]] = {}
        unknown_markers: List[Tuple[str, str]] = []
        case_dirs: Dict[str, str] = {}
        logs_by_num: Dict[str, str] = {}
        report_dirs: List[str] = []
        unmatched: List[str] = []

        for entry in entries:
            name = entry.name

            marker_match = self._marker_re.match(name)
            if marker_match:
                try:
                    kind = MarkerKind(marker_match.group("kind"))
                except ValueError:  # pragma: no cover - regex limits the kinds
                    unknown_markers.append((name, marker_match.group("case")))
                    continue
                markers.setdefault(marker_match.group("case"), set()).add(kind)
                continue

            # Shaped like a marker but not one of the known three. Confirmed
            # abnormal, so it is recorded separately and surfaced in the UI.
            # It still proves the case exists, so its id joins the case list.
            any_marker = self._marker_any_re.match(name)
            if any_marker:
                unknown_markers.append((name, any_marker.group("case")))
                continue

            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False

            if is_dir:
                if self._report_re.match(name):
                    report_dirs.append(name)
                elif self._is_case_dir_name(name):
                    case_dirs[name] = entry.path
                # anything else (cmd_folder, hidden dirs) is a known non-case
                continue

            log_match = self._log_re.match(name)
            if log_match:
                logs_by_num[log_match.group("num")] = entry.path
                continue

            unmatched.append(name)

        logs_by_case, cmd_by_case, exec_by_case, unresolved = self._resolve_logs(
            run_folder, logs_by_num
        )

        all_case_ids = sorted(
            set(markers)
            | set(case_dirs)
            | set(logs_by_case)
            | {case_id for _name, case_id in unknown_markers},
            key=natural_key,
        )

        cases: Dict[str, CaseObservation] = {}
        for case_id in all_case_ids:
            log_path = logs_by_case.get(case_id)
            size, mtime = self.stat_file(log_path) if log_path else (None, None)
            case_dir = case_dirs.get(case_id)
            cases[case_id] = CaseObservation(
                case_id=case_id,
                markers=frozenset(markers.get(case_id, set())),
                case_dir=case_dir,
                case_dir_exists=case_dir is not None,
                log_path=log_path,
                log_size=size,
                log_mtime=mtime,
                cmd_file=cmd_by_case.get(case_id),
                exec_path=exec_by_case.get(case_id),
            )

        return IndexRunObservation(
            index_key=key,
            run_folder=run_folder,
            observed_at=now,
            cases=cases,
            report_dirs=tuple(sorted(report_dirs)),
            unmatched_entries=tuple(sorted(unmatched)),
            unresolved_logs=tuple(sorted(unresolved)),
            unknown_markers=tuple(sorted(unknown_markers)),
        )

    def _is_case_dir_name(self, name: str) -> bool:
        """Exclusion rule: not a report, not cmd_folder, not hidden -> a case.

        Exclusion rather than an include pattern because case ids are cell names
        (NDIO_1, PDIO_1, NTN_1) with nothing in common. The cost is that a new
        kind of non-case directory would be misread, which is why the exclusion
        list is configurable.
        """
        return not any(pattern.match(name) for pattern in self._non_case_res)

    # ------------------------------------------------------------------
    # log -> cmd_file -> case
    # ------------------------------------------------------------------

    def _resolve_logs(
        self, run_folder: str, logs_by_num: Dict[str, str]
    ) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str], List[str]]:
        """Pair each log with its cmd_file by number and resolve its case.

        Returns (logs_by_case, cmd_by_case, exec_by_case, unresolved_logs).
        """
        logs_by_case: Dict[str, str] = {}
        cmd_by_case: Dict[str, str] = {}
        exec_by_case: Dict[str, str] = {}
        unresolved: List[str] = []

        cmd_dir = os.path.join(run_folder, self.layout.cmd_dir_name)

        for num, log_path in logs_by_num.items():
            cmd_name = self.layout.cmd_file_template.format(num=num)
            cmd_path = os.path.join(cmd_dir, cmd_name)

            exec_path = self.read_cmd_exec_path(cmd_path, run_folder)
            if not exec_path:
                unresolved.append(os.path.basename(log_path))
                continue

            case_id = os.path.basename(exec_path.rstrip("/"))
            if not case_id:
                unresolved.append(os.path.basename(log_path))
                continue

            logs_by_case[case_id] = log_path
            cmd_by_case[case_id] = cmd_path
            exec_by_case[case_id] = exec_path

        return logs_by_case, cmd_by_case, exec_by_case, unresolved

    def read_cmd_exec_path(
        self, cmd_path: str, run_folder: Optional[str] = None
    ) -> Optional[str]:
        """Read the execution path a cmd_file script cd's into.

            #!/bin/csh -f
            source xxxx
            cd /path/to/index/NDIO_1
            ...

        A script may contain several cd lines. Prefer the one **under the run
        folder** (that is the case run dir); otherwise fall back to the last
        absolute cd. Cached, since the script never changes after submission.
        """
        cmd_path = os.path.abspath(cmd_path)
        cache_key = "%s|%s" % (cmd_path, run_folder or "")
        if cache_key in self._cmd_cache:
            return self._cmd_cache[cache_key]

        text = self.read_head(cmd_path, self.layout.cmd_file_head_bytes)
        best: Optional[str] = None
        last_absolute: Optional[str] = None

        prefix = None
        if run_folder:
            prefix = os.path.abspath(run_folder).rstrip("/") + "/"

        for line in text.splitlines():
            match = self._cd_re.match(line)
            if not match:
                continue
            path = match.group("path").rstrip("/")
            if not path.startswith("/"):
                continue
            last_absolute = path
            if prefix and (path + "/").startswith(prefix):
                best = path

        result = best or last_absolute
        self._cmd_cache[cache_key] = result
        return result

    # ------------------------------------------------------------------
    # Finding index run folders
    # ------------------------------------------------------------------

    def list_index_run_folders(self, wave_dir: str) -> List[Tuple[str, str]]:
        """List the index run folders Arcx created under a wave directory.

        Returns [(index_key, abs_path), ...], skipping our own .arcx_auto/ and
        any other hidden directory.
        """
        wave_dir = os.path.abspath(os.path.expanduser(wave_dir))
        result: List[Tuple[str, str]] = []
        if not os.path.isdir(wave_dir):
            return result
        try:
            entries = list(os.scandir(wave_dir))
        except OSError:
            return result
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            result.append((entry.name, entry.path))
        return sorted(result)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def stat_file(self, path: Optional[str]) -> Tuple[Optional[int], Optional[float]]:
        """Return (size, mtime), or (None, None) rather than raising."""
        if not path:
            return (None, None)
        try:
            st = os.stat(path)
        except OSError:
            return (None, None)
        return (st.st_size, st.st_mtime)

    def read_head(self, path: str, nbytes: int) -> str:
        """Read the beginning of a file.

        errors='replace' because EDA tool logs routinely contain non-UTF-8
        bytes, and an encoding problem must never take the monitor down.
        """
        try:
            with open(path, "rb") as handle:
                raw = handle.read(nbytes)
        except OSError:
            return ""
        return raw.decode("utf-8", errors="replace")

    def count_gds(self, index_path: str) -> Tuple[int, List[str]]:
        """Count GDS files under an index path; that is the case count.

        One scandir pass rather than glob, which would rescan the directory once
        per pattern.
        """
        index_path = os.path.abspath(os.path.expanduser(index_path))
        if not os.path.isdir(index_path):
            return (0, [])
        import fnmatch

        names: List[str] = []
        try:
            entries = list(os.scandir(index_path))
        except OSError:
            return (0, [])
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            for pattern in self.layout.gds_globs:
                if fnmatch.fnmatch(entry.name, pattern):
                    names.append(entry.name)
                    break
        names.sort()
        return (len(names), names)

    def disk_free_ratio(self, path: str) -> Optional[float]:
        """Free space as a ratio between 0.0 and 1.0, or None."""
        try:
            st = os.statvfs(os.path.expanduser(path))
        except OSError:
            return None
        if st.f_blocks == 0:
            return None
        return float(st.f_bavail) / float(st.f_blocks)


_NATURAL_RE = re.compile(r"(\d+)")


def natural_key(name: str) -> Tuple:
    """Natural sort: NDIO_1 < NDIO_2 < NDIO_10.

    Case ids are cell names, and plain string ordering would put NDIO_10 before
    NDIO_2, which reads as corrupted data in the UI.
    """
    parts = _NATURAL_RE.split(name)
    key: List[Tuple[int, object]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        elif part:
            key.append((1, part))
    return tuple(key)
