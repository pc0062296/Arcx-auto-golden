"""Filesystem adapter.

A real index run folder looks like this:

    .queue.NDIO_1                  markers; the case id is a cell name
    .run.PDIO_1
    .complete.NTN_1
    NDIO_1/  PDIO_1/  NTN_1/       per-case run dirs (a rerun deletes these)
    QC_Cc/  QC_Ct/  QC_Spice/      reports Arcx assembles, not cases
    submit_bjob_cmd_file_1.log     logs, named only by sequence number
    cmd_folder/cmd_file_1          the submitted script, containing `cd <dir>`
    zmwu.cfg                       Arcx's own snapshot of the arcx.cfg it ran

Two conventions drive the implementation:

  1. **The case roster comes from markers and cmd_files, never from the
     directory listing.** Every cmd_folder/cmd_file_N is one submitted case,
     and every .queue/.run/.complete marker names one. Case ids are cell names
     with no shared pattern, so a directory cannot be recognised as a case by
     its name -- it can only be matched against a roster built elsewhere.

     Deciding by exclusion instead ("not QC_*, not cmd_folder, not hidden, so
     it must be a case") is what turned a real five case run into seven: two
     directories nobody had told the scanner about became two UNKNOWN cases.
     Any list of things-that-are-not-cases is a list of the ones we happened to
     think of. Directories that match no case are now reported as
     ``unexpected_dirs`` and counted as nothing.

     A third source, the GDS files in the index path, is deliberately *not*
     used for identity: the run uses top cell names, which need not match the
     GDS filenames. It is only good for a count, so it is a cross-check that
     warns (QA's INDEX_CASE_COUNT_MISMATCH), never a source of truth.

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
from re import error
from typing import Dict, List, Optional, Set, Tuple

from arcx_auto.config.settings import LayoutSettings
from arcx_auto.domain.enums import MarkerKind
from arcx_auto.domain.models import CaseObservation, IndexRunObservation
from arcx_auto.util.atomic import read_json


class FsAdapter:
    """Wraps every read-only access to a run folder."""

    def __init__(self, layout: Optional[LayoutSettings] = None) -> None:
        self.layout = layout or LayoutSettings()
        self._marker_re = re.compile(self.layout.marker_regex)
        self._marker_any_re = re.compile(self.layout.marker_any_regex)
        self._log_re = re.compile(self.layout.log_regex)
        self._report_re = re.compile(self.layout.report_dir_regex)
        self._cd_re = re.compile(self.layout.cmd_cd_regex)
        self._cmd_file_re = re.compile(self.layout.cmd_file_regex)
        self._index_run_re = re.compile(self.layout.index_run_folder_regex)
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

        The case roster is the union of two sources that **name** cases:
        cmd_folder/cmd_file_N (one per submitted case) and the .queue/.run/
        .complete markers. Directories are matched against that roster, never
        used to extend it.

        Each missing piece still signals a different failure, so both sources
        are kept rather than one preferred:
          - cmd_file but no marker -> submitted, marker never written
          - marker but no dir      -> the directory was deleted, or not created
          - dir but no roster entry -> not a case at all; unexpected_dirs
        """
        import time

        now = now if now is not None else time.time()
        run_folder = os.path.abspath(os.path.expanduser(run_folder))
        key = (index_key if index_key is not None
               else self.index_key_for(run_folder))

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
        dirs_by_name: Dict[str, str] = {}
        logs_by_num: Dict[str, str] = {}
        report_dirs: List[str] = []
        cfg_files: List[str] = []
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
            # It still names a case, so its id joins the roster.
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
                elif name != self.layout.cmd_dir_name and not name.startswith("."):
                    # Held, not classified. Whether it is a case run dir is
                    # decided by the roster below, not by its name.
                    dirs_by_name[name] = entry.path
                continue

            log_match = self._log_re.match(name)
            if log_match:
                logs_by_num[log_match.group("num")] = entry.path
                continue

            if name.endswith(".cfg"):
                # Arcx's snapshot of the cfg it ran, named after the user
                # (zmwu.cfg). We read it in discover_arcx_cfg, so calling it
                # unclassified would be reporting our own input as an anomaly.
                cfg_files.append(name)
                continue

            unmatched.append(name)

        # -- the roster: every cmd_file is one submitted case ------------
        cmd_by_case, exec_by_case, unreadable_cmds = self._scan_cmd_folder(
            run_folder)
        logs_by_case, unresolved = self._resolve_logs(
            run_folder, logs_by_num, exec_by_case)

        all_case_ids = sorted(
            set(markers)
            | set(cmd_by_case)
            | {case_id for _name, case_id in unknown_markers},
            key=natural_key,
        )

        cases: Dict[str, CaseObservation] = {}
        for case_id in all_case_ids:
            log_path = logs_by_case.get(case_id)
            size, mtime = self.stat_file(log_path) if log_path else (None, None)
            case_dir = self._case_dir_for(
                case_id, exec_by_case.get(case_id), dirs_by_name, run_folder)
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

        unexpected = sorted(
            name for name in dirs_by_name if name not in cases)

        return IndexRunObservation(
            index_key=key,
            run_folder=run_folder,
            observed_at=now,
            cases=cases,
            report_dirs=tuple(sorted(report_dirs)),
            cfg_files=tuple(sorted(cfg_files)),
            unexpected_dirs=tuple(unexpected),
            unmatched_entries=tuple(sorted(unmatched)),
            unresolved_logs=tuple(sorted(unresolved + unreadable_cmds)),
            unknown_markers=tuple(sorted(unknown_markers)),
        )

    def _case_dir_for(
        self,
        case_id: str,
        exec_path: Optional[str],
        dirs_by_name: Dict[str, str],
        run_folder: str,
    ) -> Optional[str]:
        """Where this case's run dir is, if it exists yet.

        The cmd_file's own `cd` path is preferred: it is what the job actually
        used, so it stays right even when the directory is not a direct child
        of the run folder. Falling back to a same-named directory covers a case
        known only from its marker.
        """
        if exec_path and os.path.isdir(exec_path):
            return exec_path
        return dirs_by_name.get(case_id)

    def _scan_cmd_folder(
        self, run_folder: str
    ) -> Tuple[Dict[str, str], Dict[str, str], List[str]]:
        """Read every cmd_folder/cmd_file_N: one submitted case each.

        This is the roster. It is read directly rather than reached through the
        logs, because a case that has been submitted but has not written a log
        yet still exists and still has to be shown.

        Returns (cmd_by_case, exec_by_case, unreadable).
        """
        cmd_by_case: Dict[str, str] = {}
        exec_by_case: Dict[str, str] = {}
        unreadable: List[str] = []

        cmd_dir = os.path.join(run_folder, self.layout.cmd_dir_name)
        try:
            entries = list(os.scandir(cmd_dir))
        except OSError:
            return (cmd_by_case, exec_by_case, unreadable)

        for entry in entries:
            if not self._cmd_file_re.match(entry.name):
                continue
            exec_path = self.read_cmd_exec_path(entry.path, run_folder)
            case_id = os.path.basename(exec_path.rstrip("/")) if exec_path else ""
            if not case_id:
                # A submitted case we cannot name is a case we cannot monitor.
                unreadable.append("%s/%s" % (self.layout.cmd_dir_name, entry.name))
                continue
            cmd_by_case[case_id] = entry.path
            exec_by_case[case_id] = exec_path
        return (cmd_by_case, exec_by_case, unreadable)

    # ------------------------------------------------------------------
    # log -> cmd_file -> case
    # ------------------------------------------------------------------

    def _resolve_logs(
        self,
        run_folder: str,
        logs_by_num: Dict[str, str],
        exec_by_case: Dict[str, str],
    ) -> Tuple[Dict[str, str], List[str]]:
        """Pair each log with its cmd_file by number and resolve its case.

        The cmd_files have already been read into the roster, so this only
        pairs numbers; ``exec_by_case`` is passed in to confirm the case the
        number points at is one we know about.

        Returns (logs_by_case, unresolved_logs).
        """
        logs_by_case: Dict[str, str] = {}
        unresolved: List[str] = []

        cmd_dir = os.path.join(run_folder, self.layout.cmd_dir_name)

        for num, log_path in logs_by_num.items():
            cmd_name = self.layout.cmd_file_template.format(num=num)
            cmd_path = os.path.join(cmd_dir, cmd_name)

            exec_path = self.read_cmd_exec_path(cmd_path, run_folder)
            case_id = os.path.basename(exec_path.rstrip("/")) if exec_path else ""
            if not case_id or case_id not in exec_by_case:
                unresolved.append(os.path.basename(log_path))
                continue

            logs_by_case[case_id] = log_path

        return logs_by_case, unresolved

    def read_cmd_exec_path(
        self, cmd_path: str, run_folder: Optional[str] = None
    ) -> Optional[str]:
        """Read the execution path a cmd_file script cd's into.

            #!/bin/csh -f
            source xxxx
            cd /path/to/index/NDIO_1     <- the case; the first cd that counts
            ...
            cd /path/to/index/QC_Cc      <- report assembly, later in the script

        A script may contain several cd lines, and **the first one is the
        case**. The script starts by entering the case run dir and may cd
        elsewhere afterwards to assemble reports; taking the last one made
        QC_Cc the answer, so a report directory was reported as a case.

        The rule is therefore: the **first** absolute cd under the run folder
        wins; if none is under the run folder, the first absolute cd wins. The
        run folder preference is kept so a leading `cd` into a tools or setup
        directory elsewhere cannot claim the case.

        Cached, since the script never changes after submission.
        """
        cmd_path = os.path.abspath(cmd_path)
        cache_key = "%s|%s" % (cmd_path, run_folder or "")
        if cache_key in self._cmd_cache:
            return self._cmd_cache[cache_key]

        text = self.read_head(cmd_path, self.layout.cmd_file_head_bytes)
        best: Optional[str] = None
        first_absolute: Optional[str] = None

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
            if first_absolute is None:
                first_absolute = path
            if best is None and prefix and (path + "/").startswith(prefix):
                best = path

        result = best or first_absolute
        self._cmd_cache[cache_key] = result
        return result

    # ------------------------------------------------------------------
    # Finding index run folders
    # ------------------------------------------------------------------

    def list_index_run_folders(self, wave_dir: str) -> List[Tuple[str, str]]:
        """List the index run folders Arcx created under a wave directory.

        Arcx names them ``<basename of the index path>_run``, so this **matches
        a pattern** rather than listing every directory. It used to list all of
        them, which swept up whatever else lived in the wave directory and then
        reported QA failures against folders that were never cases.

        Returns [(index_key, abs_path), ...]; the key is the name with ``_run``
        removed, which is what dir_map and the reports call it.
        """
        wave_dir = os.path.abspath(os.path.expanduser(wave_dir))
        result: List[Tuple[str, str]] = []
        if not os.path.isdir(wave_dir):
            return result
        try:
            entries = list(os.scandir(wave_dir))
        except OSError:
            return result
        by_folder = self._index_keys_by_folder(wave_dir)
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            match = self._index_run_re.match(entry.name)
            if not match:
                continue
            result.append((by_folder.get(entry.name) or _group_or(
                match, "index", entry.name), entry.path))
        return sorted(result)

    def index_key_for(self, run_folder: str) -> str:
        """The dir_map key for a run folder, given only its path.

        Scanning one folder directly -- `status --run-folder` -- has no wave to
        list, so the key has to come from the folder itself. Stripping _run
        gives the index path's basename; the wave manifest above it, if there
        is one, maps that back to the actual dir_map key.
        """
        run_folder = os.path.abspath(os.path.expanduser(run_folder))
        name = os.path.basename(run_folder)
        mapping = self._index_keys_by_folder(os.path.dirname(run_folder))
        if name in mapping:
            return mapping[name]
        match = self._index_run_re.match(name)
        return _group_or(match, "index", name) if match else name

    def _index_keys_by_folder(self, wave_dir: str) -> Dict[str, str]:
        """Map each run folder name back to its dir_map key.

        The folder is named after the **index path's basename**, which is not
        the dir_map key: "1000" may point at /proj/foo/index1000, giving
        index1000_run. Stripping _run recovers "index1000", not "1000" -- and
        the key is what the special.cfg snapshot and the GDS cross-check are
        stored under.

        The wave manifest already records both for every index, so the mapping
        is read from there. Without a manifest -- somebody pointing at a folder
        this tool did not create -- the stripped name is the best available
        answer and is used as-is.
        """
        manifest = read_json(
            os.path.join(wave_dir, ".arcx_auto", "manifest.json"), default=None)
        if not isinstance(manifest, dict):
            return {}
        mapping: Dict[str, str] = {}
        for entry in manifest.get("indexes") or []:
            if not isinstance(entry, dict):
                continue
            key = entry.get("index_key")
            path = entry.get("path")
            if key and path:
                folder = "%s_run" % os.path.basename(str(path).rstrip("/"))
                mapping[folder] = str(key)
        return mapping

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


def _group_or(match, name: str, fallback: str) -> str:
    """A named group if the pattern has one, otherwise the whole name.

    The pattern is configurable, so it may not define ``index`` at all.
    """
    try:
        return match.group(name) or fallback
    except (IndexError, error):
        return fallback
