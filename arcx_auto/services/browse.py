"""Listing a directory so somebody can click a file instead of typing its path.

Typing an absolute path into a text box is the worst part of the flow: it is
long, it is easy to get subtly wrong, and being wrong produces "not found"
after several other steps. Clicking is not a nicety here, it removes a whole
class of mistake.

The listing is deliberately dumb -- names, sizes, and a guess at what each file
is for -- and it exists as a service rather than inside the web layer so it can
be tested without a browser.

**It reads, and only reads.** No creating, no renaming, no deleting. The
process already runs as the person using it, so this exposes nothing they could
not already see with `ls`; the guard that matters is that there is no code path
here that writes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

#: What a file is probably for, so the useful ones are obvious at a glance
DIR_MAP_HINTS = ("dir_map",)
CFG_SUFFIXES = (".cfg",)


@dataclass(frozen=True)
class Entry:
    """One row of a directory listing."""

    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None
    kind: str = ""          # "dir_map", "arcx_cfg", "dir", ""

    @property
    def sort_key(self):
        # Directories first, then by name the way people read numbers
        return (0 if self.is_dir else 1, _natural(self.name))


@dataclass(frozen=True)
class Listing:
    """A directory, its contents, and how to go up."""

    path: str
    entries: Tuple[Entry, ...] = ()
    parent: Optional[str] = None
    error: Optional[str] = None

    @property
    def crumbs(self) -> Tuple[Tuple[str, str], ...]:
        """(label, path) from the root down, so any level is one click away."""
        parts = [p for p in self.path.split(os.sep) if p]
        out: List[Tuple[str, str]] = [("/", "/")]
        walk = ""
        for part in parts:
            walk = walk + os.sep + part
            out.append((part, walk))
        return tuple(out)


def list_dir(path: str, show_hidden: bool = False) -> Listing:
    """List one directory.

    An unreadable directory is a Listing carrying the reason, not an exception:
    somebody clicking through a filesystem will hit directories they cannot
    read, and that is navigation, not failure.
    """
    resolved = os.path.abspath(os.path.expanduser(path or os.path.expanduser("~")))
    parent = os.path.dirname(resolved) if resolved != os.sep else None

    try:
        raw = list(os.scandir(resolved))
    except NotADirectoryError:
        return Listing(path=resolved, parent=parent,
                       error="not a directory")
    except PermissionError:
        return Listing(path=resolved, parent=parent,
                       error="permission denied")
    except OSError as exc:
        return Listing(path=resolved, parent=parent, error=str(exc))

    entries: List[Entry] = []
    for item in raw:
        if not show_hidden and item.name.startswith("."):
            continue
        try:
            is_dir = item.is_dir()
        except OSError:
            is_dir = False
        size = None
        if not is_dir:
            try:
                size = item.stat().st_size
            except OSError:
                size = None
        entries.append(Entry(
            name=item.name, path=item.path, is_dir=is_dir, size=size,
            kind=classify(item.name, is_dir),
        ))

    entries.sort(key=lambda e: e.sort_key)
    return Listing(path=resolved, entries=tuple(entries), parent=parent)


def classify(name: str, is_dir: bool) -> str:
    """What this entry is probably for.

    A hint, never a filter: a dir_map that is not called dir_map still has to
    be selectable, so nothing is hidden on the strength of a guess.
    """
    if is_dir:
        return "dir"
    lowered = name.lower()
    if any(hint in lowered for hint in DIR_MAP_HINTS):
        return "dir_map"
    if lowered.endswith(CFG_SUFFIXES):
        return "arcx_cfg"
    return ""


def suggest(path: str, kind: str, limit: int = 5) -> List[str]:
    """Files in this directory that look like what is being picked.

    Shown above the listing so the common case -- the file is right here and
    obvious -- takes one click rather than a scan down a long list.
    """
    listing = list_dir(path)
    return [e.path for e in listing.entries
            if e.kind == kind and not e.is_dir][:limit]


def _natural(text: str):
    return tuple(int(p) if p.isdigit() else p.lower()
                 for p in re.split(r"(\d+)", text))
