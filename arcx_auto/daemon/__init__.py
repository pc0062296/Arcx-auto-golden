"""L3 Orchestration - the daemon.

**The single writer in the system** (architecture decision 1).
The UI and the CLI are read only; actions are posted into commands/ and the
daemon serialises them.

That is what makes "never touch the same run folder twice at once" and "every
write is in the audit log" architectural properties rather than something each
entry point has to remember.
"""

from arcx_auto.daemon.loop import Daemon, DaemonOptions
from arcx_auto.daemon.state import build_state_payload

__all__ = ["Daemon", "DaemonOptions", "build_state_payload"]
