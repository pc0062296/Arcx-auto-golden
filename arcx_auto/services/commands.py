"""The command queue: how the UI asks for something without doing it.

The web UI must never submit an LSF job or delete a directory. Not because a
browser cannot run the code, but because two writers on one run folder is the
failure this whole system is built to avoid, and "the UI only writes when the
daemon is not looking" is not an invariant anybody can keep.

So the UI writes an **intent** -- a small JSON file saying what somebody asked
for -- and the daemon, which is already the single writer, picks it up and
does it. The UI's write is to the queue directory and nowhere else.

    <state_root>/commands/
      pending/<ts>-<id>.json     written by the UI
      running/<ts>-<id>.json     claimed by the daemon, atomically
      done/<ts>-<id>.json        finished, with the result appended

Claiming is `os.replace` from pending to running, which is atomic within a
filesystem: two daemons racing for the same file, one wins and the other gets
FileNotFoundError. That is also what makes a crash recoverable -- a command
left in running/ was interrupted, and it is visible rather than lost.

**Submission has to be asynchronous.** The gate can wait two hours for the LSF
quota to drop, and a browser request cannot. That is the real reason this queue
exists rather than the UI simply calling the service directly.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from arcx_auto.util.atomic import atomic_write_json, read_json

PENDING = "pending"
RUNNING = "running"
DONE = "done"

#: What a command may ask for. Anything else is refused when it is read, not
#: when it is executed, so a malformed queue cannot reach the executor at all.
KINDS = ("submit", "rerun")


@dataclass
class Command:
    """One thing somebody asked for."""

    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    id: str = ""
    created_at: float = 0.0
    requested_by: str = ""
    #: Filled in once it has run
    state: str = PENDING
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    ok: Optional[bool] = None
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    path: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
            "requested_by": self.requested_by,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: str = "") -> "Command":
        return cls(
            kind=str(data.get("kind") or ""),
            payload=data.get("payload") or {},
            id=str(data.get("id") or ""),
            created_at=float(data.get("created_at") or 0.0),
            requested_by=str(data.get("requested_by") or ""),
            state=str(data.get("state") or PENDING),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            ok=data.get("ok"),
            result=data.get("result") or {},
            error=str(data.get("error") or ""),
            path=path,
        )


class CommandQueue:
    """A directory-backed queue. No database, no locking beyond os.replace."""

    def __init__(self, state_root: str) -> None:
        self.root = os.path.join(
            os.path.abspath(os.path.expanduser(state_root)), "commands")

    def dir_for(self, state: str) -> str:
        return os.path.join(self.root, state)

    def ensure(self) -> None:
        for state in (PENDING, RUNNING, DONE):
            os.makedirs(self.dir_for(state), exist_ok=True)

    # ------------------------------------------------------------------

    def submit(self, kind: str, payload: Dict[str, Any],
               requested_by: str = "", now: Optional[float] = None) -> Command:
        """Queue a request. Raises ValueError on a kind nobody handles.

        Rejecting here rather than at execution time means a malformed queue
        cannot reach the executor at all.
        """
        if kind not in KINDS:
            raise ValueError("unknown command kind: %r" % kind)
        now = now if now is not None else time.time()
        command = Command(
            kind=kind,
            payload=dict(payload),
            id=uuid.uuid4().hex[:12],
            created_at=now,
            requested_by=requested_by or _current_user(),
            state=PENDING,
        )
        self.ensure()
        command.path = os.path.join(
            self.dir_for(PENDING), "%d-%s.json" % (int(now), command.id))
        atomic_write_json(command.path, command.as_dict())
        return command

    def claim_next(self, now: Optional[float] = None) -> Optional[Command]:
        """Take the oldest pending command, atomically.

        Two daemons racing for the same file: one os.replace succeeds and the
        other raises, so exactly one runs it. Nothing else is needed, and
        anything else would be a lock to get wrong.
        """
        now = now if now is not None else time.time()
        for name in self._sorted_names(PENDING):
            source = os.path.join(self.dir_for(PENDING), name)
            target = os.path.join(self.dir_for(RUNNING), name)
            try:
                os.replace(source, target)
            except OSError:
                continue           # somebody else got it, or it vanished
            data = read_json(target, default=None)
            if not isinstance(data, dict):
                # Unreadable: retire it rather than leaving it to be retried
                # forever, and keep the file so somebody can look at it.
                self._retire(target, {"id": "", "kind": "", "state": DONE,
                                      "ok": False,
                                      "error": "unreadable command file"})
                continue
            command = Command.from_dict(data, path=target)
            command.state = RUNNING
            command.started_at = now
            atomic_write_json(target, command.as_dict())
            return command
        return None

    def complete(self, command: Command, ok: bool,
                 result: Optional[Dict[str, Any]] = None,
                 error: str = "", now: Optional[float] = None) -> None:
        """Record the outcome and move the command out of running/."""
        now = now if now is not None else time.time()
        command.state = DONE
        command.finished_at = now
        command.ok = ok
        command.result = result or {}
        command.error = error
        self._retire(command.path, command.as_dict())

    def cancel(self, command_id: str, now: Optional[float] = None) -> bool:
        """Withdraw a request that has not started yet.

        Only from pending: once the daemon has claimed it, jobs may already
        have been sent, and "cancel" would be a promise this cannot keep.
        """
        now = now if now is not None else time.time()
        for name in self._sorted_names(PENDING):
            path = os.path.join(self.dir_for(PENDING), name)
            data = read_json(path, default=None)
            if not isinstance(data, dict) or data.get("id") != command_id:
                continue
            data.update({"state": DONE, "ok": False, "finished_at": now,
                         "error": "cancelled before it started"})
            self._retire(path, data)
            return True
        return False

    def requeue_stale(self, older_than_sec: float = 3600.0,
                      now: Optional[float] = None) -> List[str]:
        """Retire commands left in running/ by a daemon that died.

        They are **not** put back into pending. A submit that was interrupted
        may already have created wave directories and sent jobs, and repeating
        it would double-submit; a person needs to look. Being visible in done/
        with an explanation is the safe outcome.
        """
        now = now if now is not None else time.time()
        retired: List[str] = []
        for name in self._sorted_names(RUNNING):
            path = os.path.join(self.dir_for(RUNNING), name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age < older_than_sec:
                continue
            data = read_json(path, default=None) or {}
            data.update({
                "state": DONE, "ok": False, "finished_at": now,
                "error": "interrupted: the daemon stopped while this was "
                         "running. It was not retried, because a partly "
                         "finished submission would double-submit.",
            })
            self._retire(path, data)
            retired.append(name)
        return retired

    # ------------------------------------------------------------------

    def list(self, state: str, limit: int = 50) -> List[Command]:
        """Commands in one state, newest first."""
        out: List[Command] = []
        for name in reversed(self._sorted_names(state)[-limit:]):
            path = os.path.join(self.dir_for(state), name)
            data = read_json(path, default=None)
            if isinstance(data, dict):
                out.append(Command.from_dict(data, path=path))
        return out

    def pending_count(self) -> int:
        return len(self._sorted_names(PENDING))

    def get(self, command_id: str) -> Optional[Command]:
        for state in (RUNNING, PENDING, DONE):
            for command in self.list(state, limit=200):
                if command.id == command_id:
                    return command
        return None

    # ------------------------------------------------------------------

    def _sorted_names(self, state: str) -> List[str]:
        try:
            names = os.listdir(self.dir_for(state))
        except OSError:
            return []
        return sorted(n for n in names if n.endswith(".json"))

    def _retire(self, path: str, data: Dict[str, Any]) -> None:
        self.ensure()
        target = os.path.join(self.dir_for(DONE), os.path.basename(path))
        atomic_write_json(target, data)
        try:
            os.unlink(path)
        except OSError:
            pass


def _current_user() -> str:
    for key in ("USER", "LOGNAME", "USERNAME"):
        value = os.environ.get(key)
        if value:
            return value
    return "unknown"
