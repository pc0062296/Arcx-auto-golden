"""Forming the groups from a directory, instead of ticking them by hand.

A working directory holds one ``dir_map`` and several arcx cfgs -- one per
corner, plus a typical. Which index belongs to which cfg is already written in
the index paths:

    /proj/chipA/corner_v2g/Cbest_T/index1000   ->  chipA_Cbest_T.cfg
    /proj/chipA/corner_v2g/Cworst_T/index1002  ->  chipA_Cworst_T.cfg
    /proj/chipA/plain/index1005                ->  chipA_typical.cfg

So asking a person to read that off the screen and tick two hundred boxes is
transcription, not a decision. This module does the transcription.

Three things shape it.

**It proposes, it does not decide.** Every index comes back with a verdict
*and the reason for it*, including the excluded ones, and the caller renders
them all in the same table the manual flow uses. A grouping that silently drops
half the indices is worse than no grouping at all, because the batch looks
complete when it finishes.

**Names are matched, not constructed.** ``Cbest_T`` in a path may be ``cbt`` in
a cfg file name, and nothing can derive that -- so the cfgs in the directory
are discovered, their corner suffixes canonicalised through the alias table,
and the index's corner is matched against them. Building the expected file name
would also have to parse ``chipA_Cbest_T.cfg`` back into a prefix and a corner,
which is ambiguous the moment a corner contains an underscore. It always does.

**Opting out belongs to the owner of the data.** An index directory is not ours
to write to, so the flag is a file we read and never create.

The planning function is pure: it takes facts and returns a proposal. Reading
the directory is separate, so the interesting logic is testable without a
filesystem.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# Reasons, as constants, so the UI and the tests agree on the wording
REASON_DISABLED = "disable flag in the index directory"
REASON_EXCLUDED_PATH = "path matches an excluded pattern"
REASON_UNUSABLE = "cannot run"
REASON_NO_CFG = "no cfg for this corner"
REASON_NO_TYPICAL = "no typical cfg in this directory"


@dataclass(frozen=True)
class CornerCfg:
    """One arcx cfg found in the directory, and the corner it serves."""

    path: str
    suffix: str              # as spelled in the file name
    canonical: str           # after the alias table

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass(frozen=True)
class IndexFacts:
    """What is known about one index before any decision is made."""

    index_key: str
    path: str
    disabled_by_flag: bool = False
    error: str = ""          # from IndexSpec: no GDS, unsizable, ...


@dataclass(frozen=True)
class Assignment:
    """One index, its verdict, and why."""

    index_key: str
    path: str
    corner: str = ""         # as spelled in the path
    canonical: str = ""
    cfg: str = ""            # "" when nothing was assigned
    include: bool = False
    reason: str = ""

    @property
    def cfg_name(self) -> str:
        return os.path.basename(self.cfg) if self.cfg else ""


@dataclass
class AutoGroupPlan:
    """What the tool proposes for one directory."""

    directory: str = ""
    dir_map: str = ""
    naming: str = ""                       # the <naming> in <naming>_*.cfg
    cfgs: List[CornerCfg] = field(default_factory=list)
    assignments: List[Assignment] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def included(self) -> List[Assignment]:
        return [a for a in self.assignments if a.include]

    def groups(self) -> List[Tuple[CornerCfg, List[Assignment]]]:
        """The proposal as groups: one cfg, the indices going to it.

        In the order the cfgs were discovered, so the same directory always
        produces the same group order and two proposals can be compared.
        """
        by_cfg: Dict[str, List[Assignment]] = {}
        for item in self.included:
            by_cfg.setdefault(item.cfg, []).append(item)
        out = []
        for cfg in self.cfgs:
            members = by_cfg.get(cfg.path)
            if members:
                out.append((cfg, members))
        return out


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def canonical_corner(name: str, aliases: Optional[Dict[str, List[str]]] = None
                     ) -> str:
    """Reduce a corner name to the one thing it means.

    ``Cbest_T``, ``cbest_t`` and ``cbt`` are the same corner if somebody said
    so. Nothing can derive that, which is the whole reason the alias table
    exists; without a match the lowered name is its own canonical form, so an
    unconfigured setup still matches names that are merely spelled with
    different case.
    """
    lowered = (name or "").strip().lower()
    if not lowered:
        return ""
    for canonical, members in (aliases or {}).items():
        if lowered == canonical.strip().lower():
            return canonical
        for member in members or ():
            if lowered == str(member).strip().lower():
                return canonical
    return lowered


def corner_from_path(path: str, marker: str) -> str:
    """The corner a path announces: the component after the marker.

    Not the last component -- an index may sit further down -- and not a
    search for known corner names either, because a corner nobody configured
    still has to be recognised as a corner so it can be reported as one
    without a cfg.
    """
    if not marker:
        return ""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    needle = marker.strip().lower()
    for position, part in enumerate(parts[:-1]):
        if part.lower() == needle:
            return parts[position + 1]
    return ""


def excluded_by_glob(index_key: str, path: str,
                     globs: Sequence[str]) -> Optional[str]:
    """The first pattern that excludes this index, if any.

    Matched against every path component rather than the whole path, so
    ``*_old`` means "anything under a directory called something_old" without
    anybody having to write the surrounding wildcards -- and so a pattern
    cannot match half a component name by accident.
    """
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    parts.append(str(index_key or ""))
    for pattern in globs or ():
        needle = str(pattern).strip().lower()
        if not needle:
            continue
        for part in parts:
            if fnmatch.fnmatch(part.lower(), needle):
                return pattern
    return None


# ---------------------------------------------------------------------------
# Reading the directory
# ---------------------------------------------------------------------------

def discover_cfgs(directory: str, typical_suffix: str = "typical",
                  aliases: Optional[Dict[str, List[str]]] = None,
                  ) -> Tuple[str, List[CornerCfg], List[str]]:
    """Find the cfg family in a directory.

    Returns (naming, cfgs, warnings). The naming prefix is read from
    ``<naming>_<typical>.cfg`` rather than configured: that file has to exist
    anyway -- it is where everything without a corner goes -- so making it the
    source of the prefix is one less thing to keep in step.
    """
    warnings: List[str] = []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".cfg"))
    except OSError as exc:
        return "", [], ["cannot read %s: %s" % (directory, exc)]

    tail = "_%s.cfg" % typical_suffix
    typicals = [n for n in names if n.lower().endswith(tail.lower())]
    if not typicals:
        return "", [], ["no *%s in %s" % (tail, directory)]
    if len(typicals) > 1:
        warnings.append(
            "%d files match *%s (%s); using %s"
            % (len(typicals), tail, ", ".join(typicals), typicals[0]))
    naming = typicals[0][:-len(tail)]

    cfgs: List[CornerCfg] = []
    prefix = naming + "_"
    for name in names:
        if not name.lower().startswith(prefix.lower()):
            warnings.append("%s does not belong to the %s family; ignored"
                            % (name, naming))
            continue
        suffix = name[len(prefix):-len(".cfg")]
        if not suffix:
            continue
        cfgs.append(CornerCfg(path=os.path.join(directory, name),
                              suffix=suffix,
                              canonical=canonical_corner(suffix, aliases)))
    return naming, cfgs, warnings


def read_facts(entries: Dict[str, str], disable_flag: str,
               errors: Optional[Dict[str, str]] = None) -> List[IndexFacts]:
    """Turn a dir_map into facts: what is on disk, before any judgement."""
    facts: List[IndexFacts] = []
    for key in sorted(entries, key=_natural):
        path = entries[key]
        flagged = bool(disable_flag) and os.path.exists(
            os.path.join(os.path.expanduser(path), disable_flag))
        facts.append(IndexFacts(index_key=key, path=path,
                                disabled_by_flag=flagged,
                                error=(errors or {}).get(key, "")))
    return facts


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------

def plan_auto_groups(facts: Sequence[IndexFacts], cfgs: Sequence[CornerCfg],
                     settings, directory: str = "", dir_map: str = "",
                     naming: str = "", warnings: Sequence[str] = (),
                     ) -> AutoGroupPlan:
    """Decide, for every index, which cfg it belongs to and why.

    Pure. Everything it needs was read by the caller, which is what makes the
    interesting part -- the order the rules are applied in -- testable without
    building a directory tree.

    The order matters and is deliberate:

      1. the disable flag, because the owner of the data said no and no other
         reason should be able to overrule or obscure that;
      2. an excluded path pattern, so a backup directory is never described as
         "missing a cfg", which sounds like something to fix;
      3. the index being unrunnable at all;
      4. finally, which cfg it goes to.
    """
    aliases = getattr(settings, "corner_aliases", {}) or {}
    marker = getattr(settings, "corner_marker", "")
    globs = getattr(settings, "exclude_path_globs", ()) or ()
    typical_suffix = getattr(settings, "typical_suffix", "typical")

    by_corner: Dict[str, CornerCfg] = {}
    for cfg in cfgs:
        by_corner.setdefault(cfg.canonical, cfg)
    typical = by_corner.get(canonical_corner(typical_suffix, aliases))

    assignments: List[Assignment] = []
    for fact in facts:
        corner = corner_from_path(fact.path, marker)
        canonical = canonical_corner(corner, aliases) if corner else ""

        if fact.disabled_by_flag:
            assignments.append(Assignment(
                fact.index_key, fact.path, corner, canonical,
                reason=REASON_DISABLED))
            continue

        pattern = excluded_by_glob(fact.index_key, fact.path, globs)
        if pattern:
            assignments.append(Assignment(
                fact.index_key, fact.path, corner, canonical,
                reason="%s (%s)" % (REASON_EXCLUDED_PATH, pattern)))
            continue

        if fact.error:
            assignments.append(Assignment(
                fact.index_key, fact.path, corner, canonical,
                reason="%s: %s" % (REASON_UNUSABLE, fact.error)))
            continue

        if corner:
            cfg = by_corner.get(canonical)
            if cfg is None:
                assignments.append(Assignment(
                    fact.index_key, fact.path, corner, canonical,
                    reason="%s (%s)" % (REASON_NO_CFG, corner)))
                continue
            assignments.append(Assignment(
                fact.index_key, fact.path, corner, canonical,
                cfg=cfg.path, include=True,
                reason="corner %s -> %s" % (corner, cfg.name)))
            continue

        if typical is None:
            assignments.append(Assignment(
                fact.index_key, fact.path, corner, canonical,
                reason=REASON_NO_TYPICAL))
            continue
        assignments.append(Assignment(
            fact.index_key, fact.path, corner, canonical,
            cfg=typical.path, include=True,
            reason="no corner -> %s" % typical.name))

    return AutoGroupPlan(
        directory=directory, dir_map=dir_map, naming=naming,
        cfgs=list(cfgs), assignments=assignments, warnings=list(warnings))


def scan_directory(directory: str, settings, arcx=None) -> AutoGroupPlan:
    """Everything in one call: read the directory, propose the groups.

    The one place that touches the filesystem. Index sizing errors come from
    the same IndexSpec the manual flow uses, so an index that cannot run is
    described identically whichever way somebody arrived at it.
    """
    from arcx_auto.adapters.arcx import ArcxAdapter
    from arcx_auto.adapters.fs import FsAdapter

    options = settings.auto_group
    resolved = os.path.abspath(os.path.expanduser(directory or "."))
    dir_map = os.path.join(resolved, options.dir_map_name)
    if not os.path.isfile(dir_map):
        return AutoGroupPlan(
            directory=resolved,
            error="no %s in %s" % (options.dir_map_name, resolved))

    if arcx is None:
        arcx = ArcxAdapter(settings.layout, settings.plan,
                           fs=FsAdapter(settings.layout))
    parsed = arcx.parse_dir_map(dir_map)

    naming, cfgs, warnings = discover_cfgs(
        resolved, options.typical_suffix, options.corner_aliases)
    if not cfgs:
        return AutoGroupPlan(
            directory=resolved, dir_map=dir_map, warnings=list(warnings),
            error="no arcx cfg family found in %s" % resolved)

    errors = {}
    for key, path in parsed.entries.items():
        spec = arcx.build_index_spec(key, path)
        if spec.error:
            errors[key] = spec.error

    facts = read_facts(parsed.entries, options.disable_flag, errors)
    plan = plan_auto_groups(
        facts, cfgs, options, directory=resolved, dir_map=dir_map,
        naming=naming, warnings=list(warnings) + list(parsed.warnings))
    return plan


def _natural(text: str):
    """Sort index keys the way people read numbers."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", str(text))]
