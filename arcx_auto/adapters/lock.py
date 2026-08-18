"""檔案鎖。

兩個用途:

  1. **daemon 單一實例**。系統的核心不變式是「daemon 是唯一的寫入者」
     (architecture 決策 1), 兩個 daemon 同時跑會直接破壞它。

  2. **run folder 互斥**。防止同一個 index 被跑兩次 —— 使用者列為
     不可妥協的原則之一。

用 fcntl.flock: 它由 kernel 管理, process 死掉 (含被 kill -9) 時會自動釋放,
不會留下需要人工清理的 stale lock。鎖檔內容只是給人看的診斷資訊, 不是鎖本身。

注意: flock 在 NFS 上的行為依 mount 選項而異。run folder 的鎖是「盡力而為」
的保護, 真正的安全來自 daemon 單一寫入者這個架構性質。
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
except ImportError:  # pragma: no cover - Windows, 本專案不支援
    fcntl = None  # type: ignore


class LockBusy(Exception):
    """鎖已被其他 process 持有。"""

    def __init__(self, path: str, holder: Optional[Dict[str, Any]] = None) -> None:
        self.path = path
        self.holder = holder or {}
        who = ""
        if holder:
            who = " (持有者: pid=%s host=%s since=%s)" % (
                holder.get("pid"), holder.get("host"), holder.get("since"))
        super().__init__("無法取得鎖 %s%s" % (path, who))


class FileLock:
    """非阻塞的 advisory lock。用 with 陳述式使用。"""

    def __init__(self, path: str, purpose: str = "") -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        self.purpose = purpose
        self._fd: Optional[int] = None

    def acquire(self) -> "FileLock":
        if fcntl is None:  # pragma: no cover
            raise RuntimeError("這個平台沒有 fcntl, 無法取得檔案鎖")

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

        # 鎖已到手, 寫入診斷資訊給人看 (不是鎖本身)
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
