"""Settings model and loading.

Design rules:
  * Defaults live in the dataclasses, so **no settings file is required**.
  * PyYAML is optional. Without it, use a .json settings file with the same
    structure.
  * User settings are deep merged onto the defaults; only the fields present
    are overridden.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SETTINGS_PATHS: Tuple[str, ...] = (
    "./arcx_auto.yaml",
    "~/.arcx-auto/config/default.yaml",
)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

@dataclass
class LayoutSettings:
    """File conventions inside an index run folder (architecture 9.3)."""

    # .queue.NDIO_1 / .run.PDIO_1 / .complete.NTN_1
    # Case ids are cell names, not sequence numbers, so this has to stay loose.
    marker_regex: str = r"^\.(?P<kind>queue|run|complete)\.(?P<case>.+)$"
    # Looser than marker_regex: anything shaped like .<word>.<something>.
    # Used to surface markers outside the known three, which are abnormal and
    # must be reported explicitly rather than treated as noise.
    marker_any_regex: str = r"^\.(?P<kind>[A-Za-z_][A-Za-z0-9_]*)\.(?P<case>.+)$"

    # submit_bjob_cmd_file_1.log
    #   -> pair by number with cmd_folder/cmd_file_1
    #   -> read that script's `cd <path>` to learn which case the log belongs to
    # The log filename contains no case name, and the numbering has no fixed
    # relationship to any ordering, so reading the cmd_file is the only
    # reliable mapping.
    log_regex: str = r"^submit_bjob_cmd_file_(?P<num>\d+)\.log$"
    cmd_dir_name: str = "cmd_folder"
    cmd_file_template: str = "cmd_file_{num}"
    cmd_cd_regex: str = r"^\s*cd\s+[\"']?(?P<path>[^\s\"';#]+)"
    cmd_file_head_bytes: int = 16384

    # QC_Cc/ QC_Ct/ QC_Spice/ -- reports Arcx assembles, not case dirs
    report_dir_regex: str = r"^QC_.+$"

    # Case run dirs are named after the cell, so there is no shared pattern to
    # match. They are identified by exclusion instead: any directory in the run
    # folder that is not one of these. (A rerun deletes exactly these dirs.)
    non_case_dir_regexes: List[str] = field(
        default_factory=lambda: [r"^QC_.+$", r"^cmd_folder$", r"^\..*$"]
    )

    # Index run folders Arcx creates under a wave directory
    index_run_folder_regex: str = r"^(?P<index>[^./].*)$"

    # GDS files inside an index path, used to count cases
    gds_globs: List[str] = field(
        default_factory=lambda: ["*.gds", "*.gds.gz", "*.GDS", "*.gds.bz2"]
    )
    # Arcx resource settings file inside an index path
    special_cfg_name: str = "special.cfg"
    special_cfg_cpu_key: str = "O_QCAP_LSF_NUM"

    # Keys in dir_map that are not real indices
    dir_map_reserved_keys: List[str] = field(default_factory=lambda: ["min", "max"])


@dataclass
class MonitorSettings:
    """Monitoring and state thresholds."""

    # How long a log may stay the same size before the case counts as stalled.
    #
    # This is the **fallback** path, used when QA is switched off. Normally
    # QuietSettings does the grading (4h warn, 8h stalled) and StateResolver
    # promotes RUNNING to STALLED from the resulting issue, which is where the
    # architecture wants that judgement to live.
    #
    # It must therefore agree with QuietSettings.stalled_after_sec. It used to
    # be one hour, and because StateEngine runs first and sets base_state, that
    # shorter clock silently won: a case quiet for seventy minutes -- routine
    # for this workload -- was already displayed as STALLED, and the graded
    # 4h/8h escalation never got the chance to apply.
    stall_threshold_sec: float = 28800.0
    # How long a PEND is worth noticing (informational only)
    long_pend_warn_sec: float = 7200.0
    # How long to wait after an LSF job disappears before declaring it LOST.
    # Gives NFS attribute caching and Arcx's own tidy-up some slack.
    lost_grace_sec: float = 300.0
    # How many bytes of a log to read when inferring case ownership
    log_head_bytes: int = 8192
    # Tiered polling
    poll_active_sec: float = 30.0
    poll_idle_sec: float = 300.0


@dataclass
class PlanSettings:
    """Wave planning (architecture 5)."""

    # Slot cap per wave. slots = O_QCAP_LSF_NUM * gds_count
    max_slots_per_wave: int = 200
    # Indices whose path contains one of these are planned into earlier waves
    priority_keywords: List[str] = field(default_factory=lambda: ["sram", "ro"])
    keyword_ignore_case: bool = True
    # Fallback when special.cfg is missing. 0 means "treat as an error, do not
    # guess" -- a wrong guess here silently mis-sizes every wave.
    default_cpu_per_case: int = 0


@dataclass
class GateSettings:
    """Submission gate (architecture 5.4).

        release = min_interval elapsed
                  AND ( NJOBS < quota_threshold OR max_wait elapsed )

    A plain OR has a hole: once the timer expires, submitting while the quota
    is still full floods the queue anyway. This combination covers all three of
    "not too dense", "not flooding", and "never stuck forever".
    """

    min_interval_sec: float = 600.0
    quota_threshold: int = 100
    max_wait_sec: float = 7200.0


@dataclass
class LsfSettings:
    """LSF commands (architecture 9.4)."""

    bjobs_cmd: str = "bjobs"
    busers_cmd: str = "busers"
    bkill_cmd: str = "bkill"
    bsub_cmd: str = "bsub"
    # In-house tool that lists / deletes every job under a path
    bjobs_manage_cmd: str = "bjobs_manage.py"
    bjobs_manage_list_flag: str = "-jp"
    bjobs_manage_delete_flag: str = "-djp"
    command_timeout_sec: float = 60.0

    # Arcx invocation. These are fixed by convention.
    arcx_cmd: str = "Arcx"
    arcx_fixed_args: List[str] = field(default_factory=lambda: ["-lsf0", "-nt", "50"])
    arcx_rerun_args: List[str] = field(default_factory=lambda: ["-keep_dir"])

    # Draining: deletion can lag behind the request, so the delete is reissued
    # and rechecked a few times before giving up.
    drain_attempts: int = 3
    drain_retry_delay_sec: float = 10.0

    # Drain safety gate: how many consecutive zero readings mean "quiescent"
    quiescent_confirm_times: int = 3
    quiescent_interval_sec: float = 30.0
    quiescent_timeout_sec: float = 900.0


@dataclass
class FlowProfile:
    """What one EDA tool flow leaves behind in a case run dir.

    Template variables:
        {flow}   the QC_FLOW value, e.g. calQCAP
        {block}  the block name from arcx.cfg
        {case}   the case id (cell name), e.g. NTN_1

    Adding a new EDA tool is a settings change, not a code change.
    """

    work_dir: str = "work_{flow}"
    netlists: List[str] = field(default_factory=list)
    min_bytes: int = 1


@dataclass
class ReportSettings:
    """QC_* report directories under an index run folder."""

    # Always present; missing means FATAL
    required_dirs: List[str] = field(default_factory=lambda: ["QC_Cc", "QC_Ct"])
    # Checked when present, but absence is fine
    optional_dirs: List[str] = field(default_factory=lambda: ["QC_Spice"])
    # QC_Cc/Report_QC_Cc
    main_file_template: str = "Report_{dir}"
    # QC_Cc/Report_QC_Cc_Summary_SCCB3 -- the suffix is just naming, so glob,
    # but there is exactly one per directory, so more than one is also wrong.
    summary_glob_template: str = "Report_{dir}_Summary_*"


@dataclass
class QuietSettings:
    """Thresholds for "the log has not grown in a while".

    Graded rather than binary on purpose: a single case can take ten minutes or
    three days, and there is a legitimate pattern where the log goes quiet
    because the artifacts are already written. The system states how long it
    has been quiet and escalates the visual weight; the judgement stays human.
    """

    warn_after_sec: float = 14400.0      # 4h  -- uncommon, worth a look
    stalled_after_sec: float = 28800.0   # 8h  -- effectively stuck
    # Quiet with every artifact already present usually just means tidy-up,
    # so downgrade one level rather than crying wolf.
    downgrade_when_artifacts_ready: bool = True


@dataclass
class QaSettings:
    """QA checks."""

    flows: Dict[str, FlowProfile] = field(default_factory=lambda: {
        "calQCAP": FlowProfile(netlists=["CCI_DB.spice"]),
        "calQRCFS": FlowProfile(netlists=["{case}.spf"]),
    })
    min_netlist_bytes: int = 1
    reports: ReportSettings = field(default_factory=ReportSettings)
    quiet: QuietSettings = field(default_factory=QuietSettings)

    # Which arcx.cfg keys to verify as existing files/directories before
    # submission. An explicit list rather than "anything that looks like a
    # path": the latter produces false alarms on output paths and on values
    # like TOOL_VERSION_LVS that are a command plus arguments, and once there
    # are false alarms nobody reads the warnings.
    #
    # Lines disabled with a leading 0, or commented out with #, are skipped:
    # those settings never take effect, so checking them is pure noise.
    verify_cfg_paths: bool = True
    cfg_path_keys: List[str] = field(default_factory=lambda: [
        "RCX_TECH_QTF",
        "RCX_LAYER_NAME_MAP",
        "LVS_DFM_DIR",
        # Both spellings are kept: an absent key is simply not checked, so
        # listing one extra costs nothing while omitting one misses a check.
        "LVS_DECK",
        "LVS_DECL",
        "LVS_QUERY_CMD",
        "RCX_STAR_CMD",
    ])
    cfg_path_check_skip_keys: List[str] = field(default_factory=list)
    # Check ids to disable. Here rather than by deleting code.
    disabled_checks: List[str] = field(default_factory=list)


@dataclass
class PreflightSettings:
    """Thresholds for the pre-submission checks."""

    # Block submission below this free-space ratio on the target filesystem.
    # A full disk is the number one silent killer of RC extraction: jobs die
    # part way through, in a way that does not say why.
    min_disk_free_ratio: float = 0.05
    warn_disk_free_ratio: float = 0.15
    # Warn once NJOBS reaches this fraction of the gate threshold, since
    # submitting then only produces a pile of PEND.
    quota_warn_ratio: float = 0.8


@dataclass
class LaunchSettings:
    """bsub parameters used when submitting Arcx.

    Matches the shape used by hand:

        bsub -q LVSRCE-0E.q -oo Arcx.log "Arcx -p arcx.cfg -d ... --run"

    Arcx itself is submitted to LSF (architecture decision A1c) so that our
    daemon can restart without disturbing running work. The outer job is only
    a coordinator and reserves no memory or CPU -- Arcx submits the real work
    as further LSF jobs.
    """

    queue: str = "LVSRCE-0E.q"
    # Relative to the wave directory, which is bsub's cwd.
    # -oo overwrites, so a rerun does not stack onto the previous attempt.
    output_file: str = "Arcx.log"
    # Empty disables -J. The job id in launch.json is the authoritative handle;
    # a name is only there to make bjobs output readable.
    job_name_template: str = "arcx_{run_id}_{wave}"
    # Anything else to pass to bsub, e.g. ["-R", "rusage[mem=2048]"]
    bsub_args: List[str] = field(default_factory=list)


@dataclass
class ExportSettings:
    """Export to the shared disk (architecture 9.5).

    The shared disk only ever holds derived data. The truth lives in
    ~/.arcx-auto/ and in the run folders, so this can be deleted and rebuilt at
    any time. The path is expected to change, hence configurable.
    """

    shared_root: str = "/tmp1/.auto_golden"
    interval_sec: float = 60.0
    dir_mode: int = 0o755
    file_mode: int = 0o644


@dataclass
class Settings:
    """Root settings object."""

    state_root: str = "~/.arcx-auto"
    run_root: str = "./arcx_runs"
    layout: LayoutSettings = field(default_factory=LayoutSettings)
    monitor: MonitorSettings = field(default_factory=MonitorSettings)
    plan: PlanSettings = field(default_factory=PlanSettings)
    gate: GateSettings = field(default_factory=GateSettings)
    lsf: LsfSettings = field(default_factory=LsfSettings)
    qa: QaSettings = field(default_factory=QaSettings)
    preflight: PreflightSettings = field(default_factory=PreflightSettings)
    launch: LaunchSettings = field(default_factory=LaunchSettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    source_path: Optional[str] = None

    def expanded_state_root(self) -> str:
        return os.path.abspath(os.path.expanduser(self.state_root))

    def expanded_run_root(self) -> str:
        return os.path.abspath(os.path.expanduser(self.run_root))


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _apply_overrides(target: Any, data: Dict[str, Any], path: str = "") -> List[str]:
    """Apply a dict onto a dataclass instance; return warnings for unknown keys."""
    warnings: List[str] = []
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        where = "%s.%s" % (path, key) if path else key
        if key not in known:
            warnings.append("unknown settings field: %s" % where)
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            warnings.extend(_apply_overrides(current, value, where))
        elif isinstance(current, dict) and isinstance(value, dict):
            # e.g. qa.flows: {calQCAP: {...}} -- merge item by item instead of
            # replacing wholesale, so overriding one field of one flow does not
            # drop the other defaults.
            merged = dict(current)
            for sub_key, sub_value in value.items():
                existing = merged.get(sub_key)
                if is_dataclass(existing) and isinstance(sub_value, dict):
                    warnings.extend(_apply_overrides(
                        existing, sub_value, "%s.%s" % (where, sub_key)))
                elif isinstance(sub_value, dict) and _prototype(current) is not None:
                    prototype = _prototype(current)
                    fresh = prototype()
                    warnings.extend(_apply_overrides(
                        fresh, sub_value, "%s.%s" % (where, sub_key)))
                    merged[sub_key] = fresh
                else:
                    merged[sub_key] = sub_value
            setattr(target, key, merged)
        else:
            setattr(target, key, value)
    return warnings


def _prototype(mapping: Dict[str, Any]) -> Optional[type]:
    """Infer which dataclass a dict of settings holds.

    Needed when the user adds an entry that is not in the defaults, such as a
    new flow: we have to know what type to build it as.
    """
    for value in mapping.values():
        if is_dataclass(value) and not isinstance(value, type):
            return type(value)
    return None


def _read_config_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if path.endswith(".json"):
        return json.loads(text) or {}
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "reading a YAML settings file needs PyYAML; "
            "use a .json settings file instead, or install PyYAML"
        ) from exc
    return yaml.safe_load(text) or {}


def load_settings(
    path: Optional[str] = None,
    search_defaults: bool = True,
) -> Tuple[Settings, List[str]]:
    """Load settings, returning (settings, warnings).

    With no settings file anywhere the pure defaults are returned: running
    without a config file is a supported case, not an error.
    """
    settings = Settings()
    warnings: List[str] = []

    candidates: List[str] = []
    if path:
        candidates.append(path)
    elif search_defaults:
        candidates.extend(DEFAULT_SETTINGS_PATHS)

    for candidate in candidates:
        resolved = os.path.abspath(os.path.expanduser(candidate))
        if not os.path.isfile(resolved):
            if path:  # explicitly requested but missing -> an error, not silence
                raise FileNotFoundError("settings file not found: %s" % resolved)
            continue
        data = _read_config_file(resolved)
        warnings.extend(_apply_overrides(settings, data))
        settings.source_path = resolved
        break

    return settings, warnings
