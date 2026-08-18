"""L1 Adapter Layer —— 唯一有 side effect 的地方。

外部世界只透過這一層進入系統:
    FsAdapter    檔案系統 (NFS)
    ArcxAdapter  dir_map / special.cfg / Arcx 指令組裝
    LsfAdapter   bjobs / busers / bkill / bjobs_manage.py

把 I/O 集中在這裡, 上層 (services) 才能在沒有 LSF、沒有 NFS、沒有 Arcx
的機器上完整測試。
"""

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.arcx import ArcxAdapter
from arcx_auto.adapters.lsf import LsfAdapter, LsfUnavailable
from arcx_auto.adapters.store import SnapshotStore

__all__ = ["FsAdapter", "ArcxAdapter", "LsfAdapter", "LsfUnavailable", "SnapshotStore"]
