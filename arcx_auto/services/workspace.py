"""WorkspaceBuilder -- create the isolated directory for each wave.

    <run_root>/<run_id>/wave_001/
      arcx.cfg                    snapshot (with sha256)
      dir_map                     snapshot
      .arcx_auto/
        manifest.json             this wave's full intent
        special_cfg/<index>.cfg   snapshot of each index's special.cfg
        lock                      stops the same wave running twice
        attempts/                 where a rerun backs up the failed state
      <Cbest_T_blockA>/           one per source folder -- the same shape
        arcx.cfg  dir_map           again, so the directory Arcx ran in holds
        .arcx_auto/...              everything the run used
        <index run folders>/      created by Arcx itself

**Why a directory per source folder**: Arcx creates its run folders relative
to the directory it was started in, so keeping two source folders' cases apart
on disk means starting Arcx twice. The folder structure is how the work is
classified, and a run directory that mirrors it is one somebody can read.

**Why snapshot instead of using the originals**: doing QA or debugging three
days later has to read the cfg the run actually used. The original being edited
in the meantime is normal, and "what settings were in force at the time" is the
single most useful thing when debugging. The cost is that paths inside the cfg
must be absolute, which Preflight enforces.

**The wave directory is where three boundaries coincide**: Arcx's isolation
boundary, bjobs_manage.py's operating scope, and the scope of a rerun. One path
prefix defines all three, so no mapping table is needed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from arcx_auto.config.settings import LayoutSettings, Settings
from arcx_auto.domain.models import Wave, WaveBatch, WavePlan
from arcx_auto.util.atomic import atomic_write_json, read_json

META_DIR = ".arcx_auto"


class WorkspaceError(Exception):
    """An error while building a workspace. It deliberately aborts the flow:
    a half-built workspace is worse than none.
    """


@dataclass(frozen=True)
class WaveWorkspace:
    """A directory Arcx runs in.

    Both a wave directory and a batch directory inside it are described by
    this: they have the same shape -- snapshots, a manifest, a launch record,
    a lock, an attempts directory -- because they are the same kind of thing.
    A batch is where Arcx is actually started; the wave above it is the unit
    the gate releases.

    Keeping one type is what lets the launcher, the rerun planner and the
    remediator work on a batch without knowing batches exist.
    """

    wave_name: str
    path: str
    arcx_cfg: str
    dir_map: str
    meta_dir: str
    index_keys: tuple
    #: One per source folder. Empty for a batch, and for the flat layout that
    #: waves used before batches existed.
    batches: tuple = ()
    #: Set on a batch: its directory name, and the folder it came from.
    batch_name: str = ""
    folder: str = ""

    @property
    def label(self) -> str:
        """What to call this in a message: the batch if it is one."""
        return self.batch_name or self.wave_name

    @property
    def runnable(self) -> tuple:
        """The directories Arcx is started in, one command each."""
        return self.batches or (self,)

    @property
    def launch_json(self) -> str:
        return os.path.join(self.meta_dir, "launch.json")

    @property
    def lock_path(self) -> str:
        return os.path.join(self.meta_dir, "lock")

    @property
    def attempts_dir(self) -> str:
        return os.path.join(self.meta_dir, "attempts")


def _resolve(path: Optional[str], label: str) -> str:
    """Absolute path to an input that must exist before anything is created."""
    if not path:
        raise WorkspaceError("no %s was given" % label)
    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(resolved):
        raise WorkspaceError("%s not found: %s" % (label, resolved))
    return resolved


class WorkspaceBuilder:
    """Turns a WavePlan into directories on disk."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()
        self.layout: LayoutSettings = self.settings.layout

    def build(
        self,
        plan: WavePlan,
        run_dir: str,
        arcx_cfg: str = "",
        dir_map: str = "",
        run_id: str = "",
        now: Optional[float] = None,
    ) -> List[WaveWorkspace]:
        """Create a directory for every wave in the plan.

        If any wave fails the whole thing aborts with WorkspaceError: half a
        workspace would make every later state judgement untrustworthy.
        """
        now = now if now is not None else time.time()
        run_dir = os.path.abspath(os.path.expanduser(run_dir))

        workspaces: List[WaveWorkspace] = []
        for wave in plan.waves:
            # Each wave carries the pair its group was built from. The
            # arguments are the fallback, for the single-group case where the
            # caller supplied one of each.
            cfg = _resolve(wave.arcx_cfg or arcx_cfg, "arcx.cfg")
            mapping = _resolve(wave.dir_map or dir_map, "dir_map")
            workspaces.append(
                self._build_wave(wave, run_dir, cfg, mapping, run_id, now))
        return workspaces

    # ------------------------------------------------------------------

    def _build_wave(self, wave: Wave, run_dir: str, arcx_cfg: str,
                    dir_map: str, run_id: str, now: float) -> WaveWorkspace:
        path = os.path.join(run_dir, wave.name)
        if os.path.isdir(path) and os.listdir(path):
            # Never overwrite existing results. Preflight should have caught
            # this already; this is the last line of defence.
            raise WorkspaceError(
                "target directory already exists and is not empty: %s" % path)

        meta_dir = os.path.join(path, META_DIR)
        special_dir = os.path.join(meta_dir, "special_cfg")
        os.makedirs(special_dir, exist_ok=True)
        os.makedirs(os.path.join(meta_dir, "attempts"), exist_ok=True)

        cfg_dest = os.path.join(path, os.path.basename(arcx_cfg))
        map_dest = os.path.join(path, os.path.basename(dir_map))
        shutil.copy2(arcx_cfg, cfg_dest)
        shutil.copy2(dir_map, map_dest)

        specials = self._snapshot_special_cfgs(wave.indices, special_dir)

        manifest: Dict[str, Any] = {
            "run_id": run_id,
            "wave": wave.name,
            "group": wave.group,
            "created_at": now,
            "index_keys": list(wave.index_keys),
            "total_cases": wave.total_cases,
            "total_slots": wave.total_slots,
            "sources": {
                "arcx_cfg": arcx_cfg,
                "dir_map": dir_map,
            },
            "snapshots": {
                "arcx_cfg": cfg_dest,
                "arcx_cfg_sha256": sha256(cfg_dest),
                "dir_map": map_dest,
                "dir_map_sha256": sha256(map_dest),
                "special_cfg": specials,
            },
            "indexes": [
                {
                    "index_key": spec.index_key,
                    "path": spec.path,
                    "gds_count": spec.gds_count,
                    "cpu_per_case": spec.cpu_per_case,
                    "slots": spec.slots,
                    "keywords": list(spec.keywords),
                }
                for spec in wave.indices
            ],
        }
        manifest["batches"] = [
            {"name": batch.name, "folder": batch.folder,
             "index_keys": list(batch.index_keys)}
            for batch in wave.batches
        ]
        atomic_write_json(os.path.join(meta_dir, "manifest.json"), manifest)

        batches = tuple(
            self._build_batch(wave, batch, path, arcx_cfg, dir_map, run_id, now)
            for batch in wave.batches
        )

        return WaveWorkspace(
            wave_name=wave.name,
            path=path,
            arcx_cfg=cfg_dest,
            dir_map=map_dest,
            meta_dir=meta_dir,
            index_keys=wave.index_keys,
            batches=batches,
        )

    def _build_batch(self, wave: Wave, batch: WaveBatch, wave_path: str,
                     arcx_cfg: str, dir_map: str, run_id: str,
                     now: float) -> WaveWorkspace:
        """One folder's directory inside the wave.

        It gets its own copy of the cfg and the dir_map rather than reaching
        up to the wave's: Arcx is started here, and a run directory that
        contains everything the run used is what makes reading it three days
        later possible without reconstructing where it came from.
        """
        path = os.path.join(wave_path, batch.name)
        if os.path.isdir(path) and os.listdir(path):
            raise WorkspaceError(
                "batch directory already exists and is not empty: %s" % path)

        meta_dir = os.path.join(path, META_DIR)
        special_dir = os.path.join(meta_dir, "special_cfg")
        os.makedirs(special_dir, exist_ok=True)
        os.makedirs(os.path.join(meta_dir, "attempts"), exist_ok=True)

        cfg_dest = os.path.join(path, os.path.basename(arcx_cfg))
        map_dest = os.path.join(path, os.path.basename(dir_map))
        shutil.copy2(arcx_cfg, cfg_dest)
        shutil.copy2(dir_map, map_dest)

        specials = self._snapshot_special_cfgs(batch.indices, special_dir)

        atomic_write_json(os.path.join(meta_dir, "manifest.json"), {
            "run_id": run_id,
            "wave": wave.name,
            "batch": batch.name,
            "folder": batch.folder,
            "group": wave.group,
            "created_at": now,
            "index_keys": list(batch.index_keys),
            "total_cases": batch.total_cases,
            "total_slots": batch.total_slots,
            "sources": {"arcx_cfg": arcx_cfg, "dir_map": dir_map},
            "snapshots": {
                "arcx_cfg": cfg_dest,
                "arcx_cfg_sha256": sha256(cfg_dest),
                "dir_map": map_dest,
                "dir_map_sha256": sha256(map_dest),
                "special_cfg": specials,
            },
            "indexes": [
                {
                    "index_key": spec.index_key,
                    "path": spec.path,
                    "gds_count": spec.gds_count,
                    "cpu_per_case": spec.cpu_per_case,
                    "slots": spec.slots,
                    "keywords": list(spec.keywords),
                }
                for spec in batch.indices
            ],
        })

        return WaveWorkspace(
            wave_name=wave.name,
            path=path,
            arcx_cfg=cfg_dest,
            dir_map=map_dest,
            meta_dir=meta_dir,
            index_keys=batch.index_keys,
            batch_name=batch.name,
            folder=batch.folder,
        )

    def _snapshot_special_cfgs(self, indices: Sequence[Any],
                               special_dir: str) -> Dict[str, Any]:
        """Snapshot each index's special.cfg as well.

        "What was O_QCAP_LSF_NUM at the time" is the key input when reviewing
        whether the wave sizing was sensible, and that file can change at any
        moment.
        """
        result: Dict[str, Any] = {}
        for spec in indices:
            source = os.path.join(spec.path, self.layout.special_cfg_name)
            if not os.path.isfile(source):
                result[spec.index_key] = {"error": "%s not found" % source}
                continue
            dest = os.path.join(special_dir, "%s.cfg" % spec.index_key)
            shutil.copy2(source, dest)
            result[spec.index_key] = {
                "path": dest, "sha256": sha256(dest), "source": source}
        return result


def load_workspace(wave_dir: str) -> Optional[WaveWorkspace]:
    """Rebuild a WaveWorkspace from a wave directory that already exists.

    A rerun needs the same handle the original submission had, and the manifest
    written at submission time is the record of what that was.
    """
    wave_dir = os.path.abspath(os.path.expanduser(wave_dir))
    meta_dir = os.path.join(wave_dir, META_DIR)
    manifest = read_json(os.path.join(meta_dir, "manifest.json"), default=None)
    if not manifest:
        return None
    snapshots = manifest.get("snapshots") or {}
    batches = []
    for entry in manifest.get("batches") or []:
        name = str(entry.get("name") or "")
        if not name:
            continue
        loaded = load_workspace(os.path.join(wave_dir, name))
        if loaded is not None:
            batches.append(loaded)
    return WaveWorkspace(
        wave_name=manifest.get("wave") or os.path.basename(wave_dir),
        path=wave_dir,
        arcx_cfg=snapshots.get("arcx_cfg") or os.path.join(wave_dir, "arcx.cfg"),
        dir_map=snapshots.get("dir_map") or os.path.join(wave_dir, "dir_map"),
        meta_dir=meta_dir,
        index_keys=tuple(manifest.get("index_keys") or ()),
        batches=tuple(batches),
        batch_name=str(manifest.get("batch") or ""),
        folder=str(manifest.get("folder") or ""),
    )


def sha256(path: str) -> str:
    """sha256 of a file, used to tell a snapshot from a diverged original."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()
