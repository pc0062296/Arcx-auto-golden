"""A submission being built, before anybody presses submit.

Somebody picks a dir_map and an arcx.cfg, ticks some indices, and that is one
group; then they may pick a different pair and tick more. That half-built
selection has to survive page loads, so it is stored on disk rather than in a
session -- there is no session, and a tool that loses a selection because
somebody hit refresh is a tool people stop using.

Being a file also makes it inspectable: a draft that produced a surprising
submission can be read afterwards, and it is exactly what the submit command
carries.

Drafts are **not** the truth about anything. They are discarded once submitted,
and deleting the directory loses nothing but unfinished typing.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from arcx_auto.util.atomic import atomic_write_json, read_json


@dataclass
class DraftGroup:
    """One (dir_map, arcx.cfg, indices) selection."""

    name: str
    dir_map: str
    arcx_cfg: str
    index_keys: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "dir_map": self.dir_map,
                "arcx_cfg": self.arcx_cfg,
                "index_keys": list(self.index_keys)}


@dataclass
class Draft:
    """A submission in progress."""

    id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    run_id: str = ""
    max_slots: Optional[int] = None
    mode: str = "auto"
    groups: List[DraftGroup] = field(default_factory=list)
    #: The pair being chosen right now, one file at a time. Held here rather
    #: than in the URL so picking the second file cannot lose the first.
    pending: Dict[str, str] = field(default_factory=dict)
    #: Where the picker last was, so it reopens there instead of at home
    last_dir: str = ""

    @property
    def total_indices(self) -> int:
        return sum(len(g.index_keys) for g in self.groups)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_id": self.run_id,
            "max_slots": self.max_slots,
            "mode": self.mode,
            "groups": [g.as_dict() for g in self.groups],
            "pending": dict(self.pending),
            "last_dir": self.last_dir,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Draft":
        return cls(
            id=str(data.get("id") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            run_id=str(data.get("run_id") or ""),
            max_slots=data.get("max_slots"),
            mode=str(data.get("mode") or "auto"),
            groups=[
                DraftGroup(
                    name=str(g.get("name") or ""),
                    dir_map=str(g.get("dir_map") or ""),
                    arcx_cfg=str(g.get("arcx_cfg") or ""),
                    index_keys=[str(k) for k in (g.get("index_keys") or [])],
                )
                for g in (data.get("groups") or [])
                if isinstance(g, dict)
            ],
            pending={str(k): str(v)
                     for k, v in (data.get("pending") or {}).items()},
            last_dir=str(data.get("last_dir") or ""),
        )


class DraftStore:
    """Drafts on disk, one JSON file each."""

    def __init__(self, state_root: str) -> None:
        self.dir = os.path.join(
            os.path.abspath(os.path.expanduser(state_root)), "drafts")

    def path_for(self, draft_id: str) -> str:
        return os.path.join(self.dir, "%s.json" % _safe_id(draft_id))

    def create(self, now: Optional[float] = None) -> Draft:
        now = now if now is not None else time.time()
        draft = Draft(id=uuid.uuid4().hex[:10], created_at=now, updated_at=now,
                      run_id=time.strftime("run_%Y%m%d_%H%M%S",
                                           time.localtime(now)))
        self.save(draft, now=now)
        return draft

    def save(self, draft: Draft, now: Optional[float] = None) -> Draft:
        draft.updated_at = now if now is not None else time.time()
        atomic_write_json(self.path_for(draft.id), draft.as_dict())
        return draft

    def load(self, draft_id: str) -> Optional[Draft]:
        data = read_json(self.path_for(draft_id), default=None)
        if not isinstance(data, dict):
            return None
        return Draft.from_dict(data)

    def delete(self, draft_id: str) -> None:
        try:
            os.unlink(self.path_for(draft_id))
        except OSError:
            pass

    def list(self, limit: int = 20) -> List[Draft]:
        """Unsubmitted drafts, newest first."""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return []
        drafts: List[Draft] = []
        for name in names:
            if not name.endswith(".json"):
                continue
            data = read_json(os.path.join(self.dir, name), default=None)
            if isinstance(data, dict):
                drafts.append(Draft.from_dict(data))
        drafts.sort(key=lambda d: -d.updated_at)
        return drafts[:limit]


def _safe_id(draft_id: str) -> str:
    """Only hex ids exist, so anything else is somebody probing the URL.

    Filtering rather than rejecting keeps this a pure function; the caller gets
    a path that cannot escape the drafts directory either way.
    """
    cleaned = "".join(c for c in str(draft_id) if c.isalnum())
    return cleaned[:32] or "invalid"
