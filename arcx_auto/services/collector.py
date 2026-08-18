"""Collector -- combine filesystem and LSF observations into one snapshot.

Scope: the Collector records **what was seen** and decides nothing. Deciding
belongs to StateEngine (pure) and to the QA registry.

Mapping LSF jobs back to cases is awkward because the child jobs Arcx submits
carry no identifiable job name. But the `cd <path>` line inside
cmd_folder/cmd_file_N states the job's execution path directly, which is
**deterministic** -- no guessing at log formats. Cheapest first:

  1. output_file equals the case's log path
  2. exec_cwd / sub_cwd sits under the path the cmd_file names
  3. exec_cwd / sub_cwd sits under the case run dir (fallback when exec_path
     could not be resolved)
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.lsf import LsfAdapter
from arcx_auto.config.settings import Settings
from arcx_auto.domain.models import (
    CaseObservation,
    IndexRunObservation,
    LsfJobView,
)


class Collector:
    """Combines FsAdapter and LsfAdapter observations."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        fs: Optional[FsAdapter] = None,
        lsf: Optional[LsfAdapter] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.fs = fs or FsAdapter(self.settings.layout)
        self.lsf = lsf or LsfAdapter(self.settings.lsf)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def collect_index_run(
        self,
        run_folder: str,
        index_key: Optional[str] = None,
        lsf_jobs: Optional[List[LsfJobView]] = None,
        now: Optional[float] = None,
    ) -> IndexRunObservation:
        """Observe one index run folder.

        ``lsf_jobs`` is fetched once by the caller and passed in: monitoring
        several run folders must not call bjobs once per folder (see the batch
        query design in LsfAdapter).
        """
        now = now if now is not None else time.time()
        observation = self.fs.scan_index_run_folder(run_folder, index_key, now=now)
        if lsf_jobs:
            observation = self.attach_lsf(observation, lsf_jobs)
        return observation

    def collect_wave(
        self,
        wave_dir: str,
        lsf_jobs: Optional[List[LsfJobView]] = None,
        now: Optional[float] = None,
    ) -> List[IndexRunObservation]:
        """Observe every index run folder under a wave directory."""
        now = now if now is not None else time.time()
        results: List[IndexRunObservation] = []
        for index_key, path in self.fs.list_index_run_folders(wave_dir):
            results.append(
                self.collect_index_run(path, index_key, lsf_jobs=lsf_jobs, now=now)
            )
        return results

    def fetch_lsf_jobs(self) -> Tuple[List[LsfJobView], Optional[str]]:
        """Fetch every job this account owns. Returns (jobs, error).

        A non-None error means LSF data is unavailable, and the caller must set
        ``TransitionContext.lsf_data_available`` to False or cases will be
        falsely declared LOST.
        """
        return self.lsf.list_user_jobs()

    # ------------------------------------------------------------------
    # Mapping LSF jobs to cases
    # ------------------------------------------------------------------

    def attach_lsf(
        self,
        observation: IndexRunObservation,
        jobs: List[LsfJobView],
    ) -> IndexRunObservation:
        """Attach each LSF job to the case it belongs to."""
        if not observation.cases:
            return observation

        # Narrow the candidates to jobs under this run folder first
        candidates = [j for j in jobs if j.belongs_to(observation.run_folder)]
        if not candidates:
            return observation

        by_log: Dict[str, LsfJobView] = {}
        by_dir: List[Tuple[str, LsfJobView]] = []
        for job in candidates:
            if job.output_file:
                by_log[os.path.abspath(job.output_file)] = job
            for cwd in (job.exec_cwd, job.sub_cwd):
                if cwd:
                    by_dir.append((os.path.abspath(cwd).rstrip("/") + "/", job))

        updated: Dict[str, CaseObservation] = {}
        for case_id, case in observation.cases.items():
            match = self._match_job(case, by_log, by_dir)
            updated[case_id] = replace(case, lsf=match) if match else case

        return replace(observation, cases=updated)

    @staticmethod
    def _match_job(
        case: CaseObservation,
        by_log: Dict[str, LsfJobView],
        by_dir: List[Tuple[str, LsfJobView]],
    ) -> Optional[LsfJobView]:
        # Method 1: output_file is this case's log -- the most reliable
        if case.log_path:
            direct = by_log.get(os.path.abspath(case.log_path))
            if direct is not None:
                return direct

        # Methods 2 and 3: the job cwd sits under the path from the cmd_file,
        # or under the case run dir.
        #
        # Only this direction is accepted -- the job cwd inside the case path.
        # Matching the other way (the job cwd being an *ancestor* of the case
        # dir) would attach the parent Arcx job, which runs in the index run
        # folder, to every case beneath it, so the whole table would show one
        # job. That is worse than matching nothing.
        for base in (case.exec_path, case.case_dir):
            if not base:
                continue
            prefix = os.path.abspath(base).rstrip("/") + "/"
            for cwd, job in by_dir:
                if cwd.startswith(prefix):
                    return job
        return None
