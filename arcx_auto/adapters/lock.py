"""File locking.

Two uses:

  1. **A single daemon instance.** The core invariant is that the daemon is the
     only writer (architecture decision 1); two daemons at once break it
     outright.

  2. **Run folder exclusion.** Stops the same index from being run twice, one
     of the non-negotiable rules.

fcntl.flock is used because the kernel owns it: the lock is released
automatically when the process dies, including kill -9, so no stale lock ever
needs manual cleanup. The file content is diagnostic information for humans,
not the lock itself.

Note that flock behaviour on NFS depends on mount options. The run folder lock
is best-effort protection; the real safety comes from the single-writer daemon
being an architectural property.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import time
from typing import Any, Dict, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is not supported
    fcntl = None  # type: ignore


class LockBusy(Exception):
    """The lock is held by another process."""

    def __init__(self, path: str, holder: Optional[Dict[str, Any]] = None) -> None:
        self.path = path
        self.holder = holder or {}
        who = ""
        if holder:
            who = " (held by pid=%s host=%s since=%s)" % (
                holder.get("pid"), holder.get("host"), holder.get("since"))
        super().__init__("could not acquire lock %s%s" % (path, who))


class FileLock:
    """A non-blocking advisory lock. Use it as a context manager."""

    def __init__(self, path: str, purpose: str = "") -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        self.purpose = purpose
        self._fd: Optional[int] = None

    def acquire(self) -> "FileLock":
        if fcntl is None:  # pragma: no cover
            raise RuntimeError("this platform has no fcntl; file locking is unavailable")

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self._read_holder()
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockBusy(self.path, holder) from exc
            raise

        # Lock acquired; record diagnostics for humans (not the lock itself)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "user": os.environ.get("USER", "?"),
            "purpose": self.purpose,
            "since": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False).encode("utf-8"))
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def _read_holder(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
