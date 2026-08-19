"""arcx.cfg parsing.

Format:

    g:QCA = Yes
    g:O_CAL_SET_ENV = setenv LICENSE 123@lic9

    1 BEGIN_SETTING : blocking_nameing_1
    1 QC_FLOW = calQCAP
    1 PROCESS = xx
    1 TOOL_VERSION_LVS = /source.csh tool_build
    1 RCX_TECH_QTF = /path/to/file
    1 LVS_DFM_DIR = /path/to/dir
    END_SETTINGS

A cfg can hold several blocks; each is one EDA tool configuration, and its
``QC_FLOW`` picks the flow (currently calQCAP or calQRCFS).

``g:`` entries are variables shared by every block. They sit at the top,
outside any block, and are not path checked.

The leading integer is an enable flag: 1 enables, 0 disables. Disabled lines,
and lines commented out with #, never take effect, so pre-submission checks do
**not** verify the paths they mention -- those paths are never used and
checking them would only produce false alarms.

Keyword spelling varies in real files (BEGIN_SETTING singular against
END_SETTINGS plural), so singular and plural are treated as equivalent. Only an
N typed as M is warned about.

**This file is QA's map.** What a case run dir should contain is derived
entirely from the cfg blocks (see services/qa/expectations.py):

    <block_name>_<QC_FLOW>/work_<QC_FLOW>/<netlist for that flow>

which is why the cfg must be snapshotted at submission time: QA three days
later has to read the cfg the run actually used.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Optional leading integer flag, then the content
_LINE_RE = re.compile(r"^\s*(?:(?P<flag>-?\d+)\s+)?(?P<body>.*?)\s*$")
# BEGIN_SETTING(S) : <name> -- singular and plural are equivalent, and the
# colon may be surrounded by spaces. Only SETTIMG (N typed as M) is warned
# about: that misspelling can make Arcx skip the whole block silently.
_BEGIN_RE = re.compile(
    r"^(?P<kw>BEGIN_SETT(?:ING|IMG)S?)\s*:\s*(?P<name>\S+)\s*$", re.IGNORECASE
)
_END_RE = re.compile(r"^(?P<kw>END_SETT(?:ING|IMG)S?)\s*$", re.IGNORECASE)
# Variables shared by all blocks, e.g. g:QCA = Yes
_GLOBAL_RE = re.compile(
    r"^g\s*:\s*(?P<key>[A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(?P<value>.*)$",
    re.IGNORECASE,
)
_KV_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(?P<value>.*)$")


@dataclass(frozen=True)
class CfgBlock:
    """One settings block in arcx.cfg."""

    name: str                       # blocking_nameing_1
    enabled: bool = True
    line_no: int = 0
    settings: Dict[str, str] = field(default_factory=dict)
    # Settings whose line had a leading 0. They do not take effect, so
    # pre-submission checks skip their paths.
    disabled_keys: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    @property
    def flow(self) -> Optional[str]:
        """QC_FLOW -- picks the EDA tool flow."""
        return self.settings.get("QC_FLOW")

    @property
    def output_dir_name(self) -> Optional[str]:
        """The directory this block produces inside a case run dir."""
        if not self.flow:
            return None
        return "%s_%s" % (self.name, self.flow)


@dataclass(frozen=True)
class ArcxConfig:
    """A parsed arcx.cfg."""

    source_path: str
    blocks: Tuple[CfgBlock, ...] = ()
    # g: entries shared by every block. Not path checked.
    globals: Dict[str, str] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()
    error: Optional[str] = None

    @property
    def enabled_blocks(self) -> Tuple[CfgBlock, ...]:
        return tuple(b for b in self.blocks if b.enabled)

    def block(self, name: str) -> Optional[CfgBlock]:
        for candidate in self.blocks:
            if candidate.name == name:
                return candidate
        return None


def parse_arcx_cfg(path: str) -> ArcxConfig:
    """Parse arcx.cfg.

    Lenient matching plus explicit warnings: read as much as possible when the
    format deviates and surface the doubts, rather than raising and stopping
    everything.
    """
    resolved = os.path.abspath(os.path.expanduser(path))
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
            raw_lines = handle.readlines()
    except OSError as exc:
        return ArcxConfig(source_path=resolved, error="cannot read arcx.cfg: %s" % exc)

    blocks: List[CfgBlock] = []
    globals_: Dict[str, str] = {}
    warnings: List[str] = []

    current_name: Optional[str] = None
    current_line_no = 0
    current_enabled = True
    current_settings: Dict[str, str] = {}
    current_disabled: List[str] = []
    current_warnings: List[str] = []

    def close_block(end_line: int) -> None:
        if current_name is None:
            return
        blocks.append(CfgBlock(
            name=current_name,
            enabled=current_enabled,
            line_no=current_line_no,
            settings=dict(current_settings),
            disabled_keys=tuple(current_disabled),
            warnings=tuple(current_warnings),
        ))

    for index, raw in enumerate(raw_lines, start=1):
        line = raw.split("#", 1)[0].rstrip("\n")
        if not line.strip():
            continue

        match = _LINE_RE.match(line)
        body = match.group("body") if match else line.strip()
        flag_raw = match.group("flag") if match else None
        if not body:
            continue

        # g: shared variables. Outside any block, never path checked.
        global_match = _GLOBAL_RE.match(body)
        if global_match:
            if flag_raw == "0":
                continue
            globals_[global_match.group("key")] = (
                global_match.group("value").strip().strip(";").strip().strip("'\""))
            continue

        begin = _BEGIN_RE.match(body)
        if begin:
            if "IMG" in begin.group("kw").upper():
                warnings.append(
                    "line %d spells the keyword %s (N typed as M); "
                    "Arcx may skip the entire block"
                    % (index, begin.group("kw"))
                )
            if current_name is not None:
                current_warnings.append(
                    "line %d starts a new BEGIN_SETTINGS but the previous block "
                    "had no END_SETTINGS" % index
                )
                close_block(index)
            current_name = begin.group("name")
            current_line_no = index
            current_enabled = flag_raw is None or flag_raw != "0"
            current_settings = {}
            current_disabled = []
            current_warnings = []
            continue

        end_match = _END_RE.match(body)
        if end_match:
            if "IMG" in end_match.group("kw").upper():
                warnings.append(
                    "line %d spells the keyword %s (N typed as M)"
                    % (index, end_match.group("kw"))
                )
            if current_name is None:
                warnings.append(
                    "line %d has END_SETTINGS without a matching BEGIN" % index)
            else:
                close_block(index)
                current_name = None
            continue

        kv = _KV_RE.match(body)
        if not kv:
            if current_name is None:
                warnings.append(
                    "line %d is outside any block and unparseable: %r"
                    % (index, body))
            else:
                current_warnings.append(
                    "line %d is unparseable: %r" % (index, body))
            continue

        key = kv.group("key")
        value = kv.group("value").strip().strip(";").strip().strip("'\"")

        if current_name is None:
            warnings.append(
                "line %d sets %s outside any block; ignored" % (index, key))
            continue

        if flag_raw == "0":
            current_disabled.append(key)
            continue

        if key in current_settings and current_settings[key] != value:
            current_warnings.append(
                "%s is defined twice with different values; last one wins" % key)
        current_settings[key] = value

    if current_name is not None:
        current_warnings.append(
            "block %s has no END_SETTINGS before end of file" % current_name)
        close_block(len(raw_lines))

    seen: Dict[str, int] = {}
    for block in blocks:
        seen[block.name] = seen.get(block.name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            warnings.append(
                "block name %s appears %d times; their output directories "
                "would overwrite each other" % (name, count)
            )

    if not blocks:
        warnings.append("no settings block was parsed from arcx.cfg")

    return ArcxConfig(
        source_path=resolved,
        blocks=tuple(blocks),
        globals=dict(globals_),
        warnings=tuple(warnings),
    )


def discover_arcx_cfg(run_folder: str) -> Optional["ArcxConfig"]:
    """Find the arcx.cfg snapshot Arcx leaves inside an index run folder.

    Arcx copies the cfg it ran into the run folder under a name derived from
    the user (for example ``zmwu.cfg``), so the name cannot be hard coded and
    cannot be guessed. Every ``*.cfg`` in the folder is parsed instead, and the
    one that actually looks like an arcx.cfg -- at least one BEGIN_SETTINGS
    block -- wins. ``special.cfg`` is plain key = value, so it fails that test
    on its own rather than needing to be named as an exception.

    This matters more than it looks. Without it, running `status` against a
    finished run folder produced CFG_EXPECTATION_UNAVAILABLE for every case: an
    UNKNOWN issue, which blocks success, so five genuinely complete cases were
    reported as FAILED. The cfg was sitting in the folder the whole time.

    Returns None when nothing in the folder parses as an arcx.cfg. That is not
    an error -- the caller falls back to whatever cfg it was given.
    """
    try:
        names = sorted(os.listdir(run_folder))
    except OSError:
        return None

    for name in names:
        if not name.endswith(".cfg"):
            continue
        path = os.path.join(run_folder, name)
        if not os.path.isfile(path):
            continue
        config = parse_arcx_cfg(path)
        if config.error is None and config.blocks:
            return config
    return None
