"""Domain data model.

Everything is a frozen dataclass: observations and plans are immutable
snapshots handed to pure functions, so nothing can be mutated behind your back.

Three families:
  * Observation  the facts we saw          (produced by Collector)
  * Snapshot     the interpretation        (produced by StateEngine)
  * Plan         the wave plan             (produced by WavePlanner)
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from arcx_auto.domain.enums import (
    CaseState,
    Completeness,
    LsfState,
    MarkerKind,
    PlanMode,
    WaveState,
)


# --------------------------------------------------------------------------
# Serialisation helpers
# --------------------------------------------------------------------------

def _to_jsonable(value: Any) -> Any:
    """Turn dataclasses, enums, sets and tuples into json.dumps-able data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_to_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    # After asdict a str-based Enum is already a str; this handles enums
    # passed in directly.
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return value.value
    return value


def as_json_dict(obj: Any) -> Dict[str, Any]:
    """Public serialisation entry point."""
    result = _to_jsonable(obj)
    if not isinstance(result, dict):
        raise TypeError("as_json_dict only accepts dataclass instances")
    return result


# --------------------------------------------------------------------------
# Observation -- the facts
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LsfJobView:
    """One LSF job as seen by bjobs.

    ``exec_cwd`` is how a job is mapped back to a wave directory: the wave
    directory is simultaneously Arcx's isolation boundary and the ownership
    boundary for LSF jobs (architecture 8).
    """

    job_id: str
    state: LsfState
    exec_cwd: Optional[str] = None
    sub_cwd: Optional[str] = None
    output_file: Optional[str] = None
    exec_host: Optional[str] = None
    job_name: Optional[str] = None

    def belongs_to(self, path_prefix: str) -> bool:
        """Whether this job lives under a path prefix."""
        prefix = path_prefix.rstrip("/") + "/"
        for candidate in (self.exec_cwd, self.sub_cwd, self.output_file):
            if candidate and (candidate.rstrip("/") + "/").startswith(prefix):
                return True
        return False


@dataclass(frozen=True)
class CaseObservation:
    """What one case looked like at a point in time.

    Facts, not interpretation: it records what was seen and decides nothing.
    Deciding is StateEngine's (pure) and the QA registry's job.
    """

    case_id: str                          # cell name, e.g. NDIO_1
    markers: FrozenSet[MarkerKind] = frozenset()
    case_dir: Optional[str] = None        # absolute run dir; a rerun deletes it
    case_dir_exists: bool = False
    log_path: Optional[str] = None
    log_size: Optional[int] = None
    log_mtime: Optional[float] = None
    # cmd_folder/cmd_file_N -- the script submitted to LSF. Its `cd <path>`
    # line is the only reliable way to map a log back to its case.
    cmd_file: Optional[str] = None
    exec_path: Optional[str] = None       # the path the cmd_file cd's into
    artifacts: Tuple[str, ...] = ()       # used by QA
    lsf: Optional[LsfJobView] = None

    @property
    def has_complete_marker(self) -> bool:
        return MarkerKind.COMPLETE in self.markers

    @property
    def has_run_marker(self) -> bool:
        return MarkerKind.RUN in self.markers

    @property
    def has_queue_marker(self) -> bool:
        return MarkerKind.QUEUE in self.markers

    @property
    def marker_inconsistent(self) -> bool:
        """.complete is present but .run / .queue were not cleared.

        Either Arcx did not finish tidying up, or an old marker is left over.
        Worth a human glance either way.
        """
        return self.has_complete_marker and (
            self.has_run_marker or self.has_queue_marker
        )


@dataclass(frozen=True)
class IndexRunObservation:
    """A complete observation of one index run folder."""

    index_key: str
    run_folder: str
    observed_at: float
    cases: Dict[str, CaseObservation] = field(default_factory=dict)
    report_dirs: Tuple[str, ...] = ()     # QC_Cc / QC_Ct / QC_Spice ...
    # Directories that are neither a case run dir, a QC_* report, nor
    # cmd_folder. They are reported, never counted as cases: guessing that an
    # unrecognised directory must be a case is what turned a five case run into
    # a seven case one.
    unexpected_dirs: Tuple[str, ...] = ()
    # Arcx's own snapshot of the cfg it ran, kept in the run folder under a
    # name derived from the user (zmwu.cfg). Recognised rather than reported.
    cfg_files: Tuple[str, ...] = ()
    unmatched_entries: Tuple[str, ...] = ()  # unclassifiable entries
    # A log whose owning case could not be resolved from its cmd_file.
    # That means a case we cannot monitor at all, so it must surface rather
    # than be silently dropped.
    unresolved_logs: Tuple[str, ...] = ()
    # Markers other than the known .queue/.run/.complete. Confirmed abnormal,
    # so they are reported separately instead of being mixed into the noise of
    # unmatched_entries. Each entry is (marker filename, inferred case id).
    unknown_markers: Tuple[Tuple[str, str], ...] = ()
    error: Optional[str] = None           # why the scan failed, if it did

    @property
    def case_count(self) -> int:
        return len(self.cases)


# --------------------------------------------------------------------------
# Snapshot -- the interpretation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseSnapshot:
    """StateEngine's verdict for one case, plus the memory it needs next time.

    ``last_progress_at`` / ``last_progress_size`` drive stall detection: they
    only advance when the log actually grows, so ``now - last_progress_at`` is
    how long the case has been quiet. Size rather than mtime, because NFS
    mtimes are unreliable and some tools touch a file without writing anything.
    """

    case_id: str
    state: CaseState              # final state, after StateResolver
    entered_state_at: float
    last_progress_at: float
    last_progress_size: int = 0
    last_seen_at: float = 0.0
    lsf_job_id: Optional[str] = None
    lsf_state: Optional[LsfState] = None
    # When we first saw "there should be an LSF job but there is none".
    # LOST needs a grace period, or the visibility lag between markers and LSF
    # produces false positives.
    lsf_missing_since: Optional[float] = None
    case_dir: Optional[str] = None
    log_path: Optional[str] = None
    exec_path: Optional[str] = None
    marker_inconsistent: bool = False
    note: Optional[str] = None
    # The structural state StateEngine derived from markers and LSF alone.
    # Kept apart from `state` because transitions must compare base to base:
    # comparing against the resolved state would make COMPLETED_MARKER -> DONE
    # look like a change on every single tick.
    base_state: Optional[CaseState] = None

    def silent_for(self, now: float) -> float:
        """How long the log has not grown, in seconds."""
        return max(0.0, now - self.last_progress_at)


@dataclass(frozen=True)
class IndexRunSnapshot:
    """The verdict for one index run folder."""

    index_key: str
    run_folder: str
    updated_at: float
    cases: Dict[str, CaseSnapshot] = field(default_factory=dict)
    error: Optional[str] = None

    def count_by_state(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for snap in self.cases.values():
            counts[snap.state.value] = counts.get(snap.state.value, 0) + 1
        return counts

    def cases_needing_attention(self) -> List[CaseSnapshot]:
        return [s for s in self.cases.values() if s.state.needs_attention]


@dataclass(frozen=True)
class StateEvent:
    """One state transition. Appended to events.jsonl, never rewritten."""

    ts: float
    index_key: str
    case_id: str
    from_state: Optional[CaseState]
    to_state: CaseState
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Plan -- wave planning
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DirMap:
    """A parsed dir_map.

    ``meta`` holds reserved keys such as min/max. They are not real indices and
    must never be passed to Arcx.
    """

    source_path: str
    entries: Dict[str, str] = field(default_factory=dict)
    meta: Dict[str, str] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()

    def resolve(self, index_key: str) -> Optional[str]:
        return self.entries.get(index_key)

    def keys_sorted(self) -> List[str]:
        """Numeric indices sort numerically; anything else sorts after them."""

        def sort_key(k: str) -> Tuple[int, float, str]:
            try:
                return (0, float(k), "")
            except ValueError:
                return (1, 0.0, k)

        return sorted(self.entries.keys(), key=sort_key)


@dataclass(frozen=True)
class IndexSource:
    """What we know about an index's *source* directory, from dir_map.

    Everything here is optional context for QA: the run folder is the truth,
    and the source only ever confirms or contradicts it. Absent means "we were
    not told", which is normal for `status --run-folder`, not a failure.
    """

    index_key: str
    path: str = ""
    gds_count: Optional[int] = None


@dataclass(frozen=True)
class IndexSpec:
    """The resource footprint of one index; WavePlanner's input unit.

        slots = cpu_per_case * gds_count

    cpu_per_case comes from O_QCAP_LSF_NUM in <index_path>/special.cfg.
    """

    index_key: str
    path: str
    gds_count: int
    cpu_per_case: int
    keywords: Tuple[str, ...] = ()
    priority: int = 0
    warnings: Tuple[str, ...] = ()
    error: Optional[str] = None
    #: True when cpu_per_case is a configured default rather than a value read
    #: from special.cfg. The slot cap exists to keep the queue from flooding,
    #: so a guessed input to it has to be visible rather than merely warned
    #: about in passing.
    cpu_estimated: bool = False

    @property
    def slots(self) -> int:
        return self.cpu_per_case * self.gds_count

    @property
    def usable(self) -> bool:
        """Complete enough to be placed into a wave."""
        return self.error is None and self.gds_count > 0 and self.cpu_per_case > 0


@dataclass(frozen=True)
class Wave:
    """A batch of indices submitted together: one Arcx command, one directory."""

    seq: int
    indices: Tuple[IndexSpec, ...]
    state: WaveState = WaveState.PLANNED

    @property
    def name(self) -> str:
        return "wave_%03d" % self.seq

    @property
    def total_slots(self) -> int:
        return sum(i.slots for i in self.indices)

    @property
    def total_cases(self) -> int:
        return sum(i.gds_count for i in self.indices)

    @property
    def index_keys(self) -> Tuple[str, ...]:
        return tuple(i.index_key for i in self.indices)


@dataclass(frozen=True)
class WavePlan:
    """The complete wave plan: pure data, previewable, editable, replayable.

    It deliberately performs no action (architecture decision 5).
    """

    mode: PlanMode
    max_slots_per_wave: int
    waves: Tuple[Wave, ...] = ()
    excluded: Tuple[IndexSpec, ...] = ()   # incomplete data, cannot be planned
    warnings: Tuple[str, ...] = ()
    created_at: float = 0.0

    @property
    def total_slots(self) -> int:
        return sum(w.total_slots for w in self.waves)

    @property
    def total_cases(self) -> int:
        return sum(w.total_cases for w in self.waves)

    @property
    def oversized_waves(self) -> Tuple[Wave, ...]:
        """Waves whose slot demand alone exceeds the cap.

        Usually one unusually large index, or a cap set too low. Either way it
        should be visible while it is still just a plan.
        """
        return tuple(
            w for w in self.waves if w.total_slots > self.max_slots_per_wave
        )
