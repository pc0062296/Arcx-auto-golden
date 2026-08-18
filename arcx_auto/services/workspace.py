"""WorkspaceBuilder -- create the isolated directory for each wave.

    <run_root>/<run_id>/wave_001/
      arcx.cfg                    snapshot (with sha256)
      dir_map                     snapshot
      .arcx_auto/
        manifest.json             this wave's full intent
        special_cfg/<index>.cfg   snapshot of each index's special.cfg
        lock                      stops the same wave running twice
        attempts/                 where a rerun backs up the failed state
      <index run folders>/        created by Arcx itself

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
from arcx_auto.domain.models import Wave, WavePlan
from arcx_auto.util.atomic import atomic_write_json

META_DIR = ".arcx_auto"


class WorkspaceError(Exception):
    """An error while building a workspace. It deliberately aborts the flow:
    a half-built workspace is worse than none.
    """


@dataclass(frozen=True)
class WaveWorkspace:
    """A wave directory that has been created."""

    wave_name: str
    path: str
    arcx_cfg: str
    dir_map: str
    meta_dir: str
    index_keys: tuple

    @property
    def launch_json(self) -> str:
        return os.path.join(self.meta_dir, "launch.json")

    @property
    def lock_path(self) -> str:
        return os.path.join(self.meta_dir, "lock")

    @property
    def attempts_dir(self) -> str:
        return os.path.join(self.meta_dir, "attempts")


class WorkspaceBuilder:
    """Turns a WavePlan into directories on disk."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()
        self.layout: LayoutSettings = self.settings.layout

    def build(
        self,
        plan: WavePlan,
        run_dir: str,
        arcx_cfg: str,
        dir_map: str,
        run_id: str = "",
        now: Optional[float] = None,
    ) -> List[WaveWorkspace]:
        """Create a directory for every wave in the plan.

        If any wave fails the whole thing aborts with WorkspaceError: half a
        workspace would make every later state judgement untrustworthy.
        """
        now = now if now is not None else time.time()
        run_dir = os.path.abspath(os.path.expanduser(run_dir))
        arcx_cfg = os.path.abspath(os.path.expanduser(arcx_cfg))
        dir_map = os.path.abspath(os.path.expanduser(dir_map))

        for path, label in ((arcx_cfg, "arcx.cfg"), (dir_map, "dir_map")):
            if not os.path.isfile(path):
                raise WorkspaceError("%s not found: %s" % (label, path))

        workspaces: List[WaveWorkspace] = []
        for wave in plan.waves:
            workspaces.append(
                self._build_wave(wave, run_dir, arcx_cfg, dir_map, run_id, now))
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

        specials = self._snapshot_special_cfgs(wave, special_dir)

        manifest: Dict[str, Any] = {
            "run_id": run_id,
            "wave": wave.name,
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
        atomic_write_json(os.path.join(meta_dir, "manifest.json"), manifest)

        return WaveWorkspace(
            wave_name=wave.name,
            path=path,
            arcx_cfg=cfg_dest,
            dir_map=map_dest,
            meta_dir=meta_dir,
            index_keys=wave.index_keys,
        )

    def _snapshot_special_cfgs(self, wave: Wave,
                               special_dir: str) -> Dict[str, Any]:
        """Snapshot each index's special.cfg as well.

        "What was O_QCAP_LSF_NUM at the time" is the key input when reviewing
        whether the wave sizing was sensible, and that file can change at any
        moment.
        """
        result: Dict[str, Any] = {}
        for spec in wave.indices:
            source = os.path.join(spec.path, self.layout.special_cfg_name)
            if not os.path.isfile(source):
                result[spec.index_key] = {"error": "%s not found" % source}
                continue
            dest = os.path.join(special_dir, "%s.cfg" % spec.index_key)
            shutil.copy2(source, dest)
            result[spec.index_key] = {
                "path": dest, "sha256": sha256(dest), "source": source}
        return result


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
