"""Workspaces: which run_root a daemon owns, so a submission can name one.

A workspace is a ``run_root`` -- the directory waves are created in and
monitored under. One per project directory is the normal way to work: you keep
``./arcx_runs`` beside the data it belongs to, and you start a daemon in each
directory you are working in.

That model only works if the pieces can see each other. The web UI runs in one
directory and the daemons in others, so "the run_root of whoever happens to be
asking" is not an answer -- a submission has to name the workspace it is going
to. This registry is how it can:

    <state_root>/workspaces/<run_id>.json

Each daemon writes one file saying where it is and what it owns, and refreshes
it while it lives. The UI reads them to offer a choice, and to say plainly when
a workspace has nobody watching it.

It is a **hint, not a lock.** A stale file means a daemon that died without
tidying up, and the worst it can cause is a workspace offered in the UI that
nobody is serving -- which the page says out loud. Nothing here decides whether
work may proceed; the daemon lock still does that.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from arcx_auto.util.atomic import atomic_write_json, read_json

#: A daemon refreshes its file about this often, so anything older than a few
#: minutes has stopped without saying so. Comfortably above the idle poll
#: interval: a daemon watching a finished run is quiet, not gone.
STALE_AFTER_SEC = 900.0

#: How often the daemon rewrites its own file. Cheap (one small atomic write),
#: but not something to do on every two-second queue poll.
HEARTBEAT_SEC = 30.0


@dataclass(frozen=True)
class Workspace:
    """One daemon's claim on one run_root."""

    run_id: str
    run_root: str
    cwd: str = ""
    pid: int = 0
    host: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    stopped_at: Optional[float] = None
    path: str = ""

    @property
    def name(self) -> str:
        """A short label: the directory the workspace belongs to."""
        parent = os.path.basename(os.path.dirname(self.run_root.rstrip("/")))
        return parent or os.path.basename(self.run_root.rstrip("/")) or "/"

    def alive(self, now: Optional[float] = None,
              stale_after: float = STALE_AFTER_SEC) -> bool:
        if self.stopped_at:
            return False
        now = now if now is not None else time.time()
        return (now - self.updated_at) < stale_after

    def age_sec(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        return max(0.0, now - self.updated_at)

    def as_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "run_root": self.run_root,
                "cwd": self.cwd, "pid": self.pid, "host": self.host,
                "started_at": self.started_at, "updated_at": self.updated_at,
                "stopped_at": self.stopped_at}

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: str = "") -> "Workspace":
        return cls(
            run_id=str(data.get("run_id") or ""),
            run_root=str(data.get("run_root") or ""),
            cwd=str(data.get("cwd") or ""),
            pid=int(data.get("pid") or 0),
            host=str(data.get("host") or ""),
            started_at=float(data.get("started_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            stopped_at=data.get("stopped_at"),
            path=path,
        )


def workspace_id(run_root: str) -> str:
    """A stable id for a run_root: readable, and unique to the path.

    Stable matters more than pretty. The daemon's run id used to be a
    timestamp, so restarting it created a brand new run page and left the old
    one looking abandoned -- after a week of restarts the home page was a list
    of ghosts. Deriving it from run_root instead means restarting a daemon
    continues the same page, and two directories are two pages.

    The directory name alone would collide: half the projects on a disk have a
    subdirectory called ``work``. The suffix is what makes it an identity
    rather than a label.
    """
    import hashlib

    resolved = os.path.abspath(os.path.expanduser(run_root or "."))
    parent = os.path.basename(os.path.dirname(resolved))
    label = parent or os.path.basename(resolved) or "root"
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in label)
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:6]
    return "%s-%s" % (safe.strip("_") or "root", digest)


def registry_dir(state_root: str) -> str:
    return os.path.join(
        os.path.abspath(os.path.expanduser(state_root)), "workspaces")


def _safe_name(run_id: str) -> str:
    """A file name that cannot leave the registry directory."""
    return "".join(c if (c.isalnum() or c in "-_") else "_"
                   for c in run_id) or "unnamed"


def register(state_root: str, run_id: str, run_root: str,
             now: Optional[float] = None,
             started_at: Optional[float] = None) -> Workspace:
    """Record that this process owns this run_root."""
    now = now if now is not None else time.time()
    workspace = Workspace(
        run_id=run_id,
        run_root=os.path.abspath(os.path.expanduser(run_root)),
        cwd=os.getcwd(),
        pid=os.getpid(),
        host=socket.gethostname(),
        started_at=started_at if started_at is not None else now,
        updated_at=now,
        path=os.path.join(registry_dir(state_root),
                          "%s.json" % _safe_name(run_id)),
    )
    os.makedirs(registry_dir(state_root), exist_ok=True)
    atomic_write_json(workspace.path, workspace.as_dict())
    return workspace


def heartbeat(state_root: str, workspace: Workspace,
              now: Optional[float] = None) -> Workspace:
    return register(state_root, workspace.run_id, workspace.run_root,
                    now=now, started_at=workspace.started_at)


def mark_stopped(state_root: str, workspace: Workspace,
                 now: Optional[float] = None) -> None:
    """Say the daemon has gone, rather than leaving the file to go stale.

    A stale file is only distinguishable from a live one by a clock, and for
    fifteen minutes after a clean shutdown the UI would keep offering a
    workspace nobody is serving.
    """
    now = now if now is not None else time.time()
    data = workspace.as_dict()
    data.update({"updated_at": now, "stopped_at": now})
    try:
        atomic_write_json(workspace.path, data)
    except OSError:                      # shutting down must not fail
        pass


def list_workspaces(state_root: str) -> List[Workspace]:
    """Every workspace ever registered, newest heartbeat first."""
    root = registry_dir(state_root)
    found: List[Workspace] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        data = read_json(path, default=None)
        if isinstance(data, dict) and data.get("run_root"):
            found.append(Workspace.from_dict(data, path=path))
    found.sort(key=lambda w: (-w.updated_at, w.run_root))
    return found


def live_workspaces(state_root: str, now: Optional[float] = None,
                    stale_after: float = STALE_AFTER_SEC) -> List[Workspace]:
    return [w for w in list_workspaces(state_root)
            if w.alive(now, stale_after)]


def find(state_root: str, run_root: str) -> Optional[Workspace]:
    """The workspace owning a run_root, if one is registered."""
    target = os.path.abspath(os.path.expanduser(run_root or "."))
    for workspace in list_workspaces(state_root):
        if workspace.run_root == target:
            return workspace
    return None
