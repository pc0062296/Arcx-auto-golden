"""WorkspaceBuilder —— 建立 wave 的隔離目錄。

    <run_root>/<run_id>/wave_001/
      arcx.cfg                    快照 (含 sha256)
      dir_map                     快照
      .arcx_auto/
        manifest.json             這個 wave 的完整意圖
        special_cfg/<index>.cfg   每個 index 的 special.cfg 快照
        lock                      防止同一個 wave 被跑兩次
        attempts/                 rerun 前備份失敗現場的地方
      <index run folders>/        Arcx 自己建立

**為什麼要快照而不是直接用原檔**: 三天後回頭做 QA 或查問題時, 用的必須是
提交當下那份 cfg。原檔在這期間被改過是常態, 而「當時到底用了什麼設定」
是除錯的救命稻草。代價是 cfg 內的路徑必須是絕對的 (Preflight 會擋)。

**wave 目錄是三個邊界的交集**: Arcx 的隔離邊界、bjobs_manage.py 的操作邊界、
rerun 的作用域。三者用同一個路徑前綴定義, 不需要額外的對映表。
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
    """建立 workspace 時的錯誤。刻意讓它中止流程 —— 半成品的 workspace
    比沒有 workspace 更糟。
    """


@dataclass(frozen=True)
class WaveWorkspace:
    """一個已建立好的 wave 目錄。"""

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
    """把 WavePlan 變成磁碟上的目錄。"""

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
        """為 plan 中的每個 wave 建立目錄。

        任何一個 wave 建立失敗就整個中止並丟出 WorkspaceError ——
        留下一半的 workspace 會讓後續的狀態判斷變得無法信任。
        """
        now = now if now is not None else time.time()
        run_dir = os.path.abspath(os.path.expanduser(run_dir))
        arcx_cfg = os.path.abspath(os.path.expanduser(arcx_cfg))
        dir_map = os.path.abspath(os.path.expanduser(dir_map))

        for path, label in ((arcx_cfg, "arcx.cfg"), (dir_map, "dir_map")):
            if not os.path.isfile(path):
                raise WorkspaceError("找不到 %s: %s" % (label, path))

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
            # 絕不覆蓋既有結果。Preflight 應該已經擋下來, 這裡是最後一道防線。
            raise WorkspaceError("目標目錄已存在且非空: %s" % path)

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
        """把每個 index 的 special.cfg 也存一份。

        「當初 O_QCAP_LSF_NUM 設多少」是事後檢討分波是否合理的關鍵資訊,
        而那個檔案隨時可能被改。
        """
        result: Dict[str, Any] = {}
        for spec in wave.indices:
            source = os.path.join(spec.path, self.layout.special_cfg_name)
            if not os.path.isfile(source):
                result[spec.index_key] = {"error": "找不到 %s" % source}
                continue
            dest = os.path.join(special_dir, "%s.cfg" % spec.index_key)
            shutil.copy2(source, dest)
            result[spec.index_key] = {
                "path": dest, "sha256": sha256(dest), "source": source}
        return result


def sha256(path: str) -> str:
    """檔案內容的 sha256。用來判斷快照與原檔是否已經分歧。"""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()
