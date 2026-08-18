"""Arcx file formats and command assembly.

Three things:
  1. dir_map  (a Perl hash)      -> DirMap
  2. special.cfg (key = value)   -> cpu_per_case
  3. Arcx command assembly       -> List[str]

Parsing is lenient with explicit warnings: read as much as possible when the
format deviates and surface the doubts, rather than raising and stopping the
whole flow.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.config.settings import LayoutSettings, PlanSettings
from arcx_auto.domain.models import DirMap, IndexSpec

# "1000" => "/path/to/index1000/"  -- single or double quotes, tolerant of
# whitespace and of a missing trailing comma
_DIR_MAP_ENTRY_RE = re.compile(
    r"""["'](?P<key>[^"']+)["']\s*=>\s*["'](?P<value>[^"']*)["']"""
)
# O_QCAP_LSF_NUM = 4  -- accepts = or :, optional quotes, # starts a comment
_KV_RE = re.compile(
    r"""^\s*(?P<key>[A-Za-z_][A-Za-z0-9_.]*)\s*[=:]\s*(?P<value>.*?)\s*$"""
)


class ArcxAdapter:
    """Arcx file formats and command interface."""

    def __init__(
        self,
        layout: Optional[LayoutSettings] = None,
        plan: Optional[PlanSettings] = None,
        fs: Optional[FsAdapter] = None,
    ) -> None:
        self.layout = layout or LayoutSettings()
        self.plan = plan or PlanSettings()
        self.fs = fs or FsAdapter(self.layout)

    # ------------------------------------------------------------------
    # dir_map
    # ------------------------------------------------------------------

    def parse_dir_map(self, path: str) -> DirMap:
        """Parse the Perl-style dir_map.

            %dir_map =(
            "1000" => "/path/to/index1000/"  ,
            "1001" => "/path/to/index1001/" ,
            "min" => "1000"
            "max" => "1014"
            );
            return 1 ;

        min and max are reserved meta keys, **not real indices**, and must
        never be passed to Arcx. In real files the "min" line does not even
        have a trailing comma, which is exactly why entries are extracted with
        a regex rather than by parsing the syntax strictly.
        """
        resolved = os.path.abspath(os.path.expanduser(path))
        warnings: List[str] = []

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            return DirMap(
                source_path=resolved,
                warnings=("cannot read dir_map: %s" % exc,),
            )

        # Drop whole-line comments so commented-out entries are not picked up
        lines = []
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            lines.append(line.split("#", 1)[0] if "#" in line else line)
        cleaned = "\n".join(lines)

        reserved = {k.lower() for k in self.layout.dir_map_reserved_keys}
        entries: Dict[str, str] = {}
        meta: Dict[str, str] = {}

        for match in _DIR_MAP_ENTRY_RE.finditer(cleaned):
            key = match.group("key").strip()
            value = match.group("value").strip()
            if key.lower() in reserved:
                meta[key] = value
                continue
            if key in entries and entries[key] != value:
                warnings.append(
                    "index %s is defined twice with different values; "
                    "last one wins" % key
                )
            entries[key] = value

        if not entries:
            warnings.append("no index entries were parsed from dir_map")

        warnings.extend(self._check_dir_map_range(entries, meta))

        return DirMap(
            source_path=resolved,
            entries=entries,
            meta=meta,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _check_dir_map_range(
        entries: Dict[str, str], meta: Dict[str, str]
    ) -> List[str]:
        """Use min/max to detect missing indices.

        min and max declare the expected range. Fewer entries than the range
        suggests the dir_map was damaged -- invisible to the eye, but it makes
        some indices silently never run.
        """
        warnings: List[str] = []
        low_raw = meta.get("min")
        high_raw = meta.get("max")
        if low_raw is None or high_raw is None:
            return warnings
        try:
            low, high = int(low_raw), int(high_raw)
        except ValueError:
            warnings.append(
                "dir_map min/max are not integers: %r / %r" % (low_raw, high_raw))
            return warnings
        if low > high:
            warnings.append("dir_map min (%d) is greater than max (%d)" % (low, high))
            return warnings

        numeric = set()
        for key in entries:
            try:
                numeric.add(int(key))
            except ValueError:
                continue
        missing = [n for n in range(low, high + 1) if n not in numeric]
        if missing:
            preview = ", ".join(str(n) for n in missing[:10])
            suffix = " ..." if len(missing) > 10 else ""
            warnings.append(
                "min/max declare %d-%d but %d index/indices are missing: %s%s"
                % (low, high, len(missing), preview, suffix)
            )
        return warnings

    # ------------------------------------------------------------------
    # special.cfg
    # ------------------------------------------------------------------

    def parse_key_values(self, path: str) -> Tuple[Dict[str, str], List[str]]:
        """Parse a key = value settings file. Returns (values, warnings)."""
        warnings: List[str] = []
        values: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                raw_lines = handle.readlines()
        except OSError as exc:
            return values, ["cannot read %s: %s" % (path, exc)]

        for raw in raw_lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            match = _KV_RE.match(line)
            if not match:
                continue
            value = match.group("value").strip().strip(";").strip()
            value = value.strip("'\"")
            values[match.group("key")] = value
        return values, warnings

    def read_cpu_per_case(self, index_path: str) -> Tuple[int, List[str]]:
        """Read the per-case CPU count from <index_path>/special.cfg.

        The key is O_QCAP_LSF_NUM, overridable in settings.
        """
        warnings: List[str] = []
        cfg_path = os.path.join(index_path, self.layout.special_cfg_name)
        key = self.layout.special_cfg_cpu_key

        if not os.path.isfile(cfg_path):
            warnings.append("%s not found" % cfg_path)
            return (self.plan.default_cpu_per_case, warnings)

        values, parse_warnings = self.parse_key_values(cfg_path)
        warnings.extend(parse_warnings)

        if key not in values:
            warnings.append("%s does not contain %s" % (cfg_path, key))
            return (self.plan.default_cpu_per_case, warnings)

        raw = values[key]
        try:
            cpu = int(float(raw))
        except ValueError:
            warnings.append("%s = %r is not a number" % (key, raw))
            return (self.plan.default_cpu_per_case, warnings)

        if cpu <= 0:
            warnings.append("%s = %d is not positive" % (key, cpu))
        return (cpu, warnings)

    # ------------------------------------------------------------------
    # IndexSpec
    # ------------------------------------------------------------------

    def build_index_spec(self, index_key: str, index_path: str) -> IndexSpec:
        """Turn one index into the resource footprint WavePlanner consumes.

            slots = O_QCAP_LSF_NUM * gds_count
        """
        resolved = os.path.abspath(os.path.expanduser(index_path))
        warnings: List[str] = []

        if not os.path.isdir(resolved):
            return IndexSpec(
                index_key=index_key,
                path=resolved,
                gds_count=0,
                cpu_per_case=0,
                error="index path does not exist or is not a directory",
            )

        gds_count, _names = self.fs.count_gds(resolved)
        if gds_count == 0:
            warnings.append("no GDS files found in the index path")

        cpu, cpu_warnings = self.read_cpu_per_case(resolved)
        warnings.extend(cpu_warnings)

        keywords = self.match_keywords(resolved)

        error: Optional[str] = None
        if gds_count == 0:
            error = "no GDS files, cannot size the work"
        elif cpu <= 0:
            error = ("cannot read %s, cannot size the work"
                     % self.layout.special_cfg_cpu_key)

        return IndexSpec(
            index_key=index_key,
            path=resolved,
            gds_count=gds_count,
            cpu_per_case=cpu,
            keywords=keywords,
            priority=1 if keywords else 0,
            warnings=tuple(warnings),
            error=error,
        )

    def match_keywords(self, path: str) -> Tuple[str, ...]:
        """Match priority keywords (sram, ro, ...) against the path."""
        haystack = path.lower() if self.plan.keyword_ignore_case else path
        hits: List[str] = []
        for keyword in self.plan.priority_keywords:
            needle = keyword.lower() if self.plan.keyword_ignore_case else keyword
            if needle and needle in haystack:
                hits.append(keyword)
        return tuple(hits)

    # ------------------------------------------------------------------
    # Command assembly
    # ------------------------------------------------------------------

    def build_run_command(
        self,
        cfg_path: str,
        index_keys: List[str],
        rerun: bool = False,
        lsf_settings: Optional["object"] = None,
    ) -> List[str]:
        """Assemble the Arcx invocation.

            Arcx -p arcx.cfg -d 1000 1001 -lsf0 -nt 50 --run
            Arcx -p arcx.cfg -d 1000 1001 -lsf0 -nt 50 -keep_dir --run   (rerun)
        """
        from arcx_auto.config.settings import LsfSettings

        lsf = lsf_settings if lsf_settings is not None else LsfSettings()
        assert isinstance(lsf, LsfSettings)

        cmd: List[str] = [lsf.arcx_cmd, "-p", cfg_path, "-d"]
        cmd.extend(index_keys)
        cmd.extend(lsf.arcx_fixed_args)
        if rerun:
            cmd.extend(lsf.arcx_rerun_args)
        cmd.append("--run")
        return cmd
