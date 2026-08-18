"""L1 Adapter Layer - the only place with side effects.

The outside world enters the system only through here:
    FsAdapter    the filesystem (NFS)
    ArcxAdapter  dir_map / special.cfg / Arcx command assembly
    LsfAdapter   bjobs / busers / bkill / bjobs_manage.py

Keeping I/O confined to this layer is what lets everything above it be tested
on a machine with no LSF, no NFS and no Arcx.
"""

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.arcx import ArcxAdapter
from arcx_auto.adapters.lsf import LsfAdapter, LsfUnavailable
from arcx_auto.adapters.store import SnapshotStore

__all__ = ["FsAdapter", "ArcxAdapter", "LsfAdapter", "LsfUnavailable",
           "SnapshotStore"]
