"""Reading a run's own files, so investigating does not mean leaving the page.

When a case reads FAILED the very next thing anybody does is tail the log. That
was a trip out to a terminal on every single investigation -- the most repeated
action in the whole review, and the one the tool made hardest.

Two things decide the shape of this module.

**It is confined.** This is the first read endpoint that takes a path from the
outside, so a path is served only if it resolves to somewhere inside a
configured root. Symlinks are resolved before the check, because
``<run>/link -> /etc/shadow`` is otherwise a valid-looking path under the root.
The process already runs as the person using it, so this exposes nothing they
could not read with `cat` -- but a tool that will open any file when asked is a
tool nobody should point at a shared disk later.

**It is bounded.** A netlist is routinely hundreds of megabytes. Nothing here
reads a whole file: a tail seeks to the end and reads backwards, a head stops
after its budget, and the caller gets told when it was cut short.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

#: How much to read for one view, however many lines were asked for
DEFAULT_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class FileView:
    """A window onto a file, plus what was left out."""

    path: str
    text: str = ""
    size: Optional[int] = None
    mtime: Optional[float] = None
    mode: str = "tail"                  # "tail" or "head"
    lines_asked: int = 0                # what was requested
    lines_shown: int = 0                # what the file actually had
    truncated: bool = False             # there is more above/below
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict:
        """A plain dict, because the page functions take data, not services.

        The UI modules deliberately import no service (architecture decision
        1). Handing them a dict keeps that true for the one place where a page
        finally needs something the daemon did not compute.
        """
        return {
            "path": self.path, "text": self.text, "size": self.size,
            "mtime": self.mtime, "mode": self.mode,
            "lines_asked": self.lines_asked,
            "lines_shown": self.lines_shown, "truncated": self.truncated,
            "error": self.error,
        }


@dataclass(frozen=True)
class FileEntry:
    """One file a case produced, offered for viewing."""

    name: str                            # relative to the case dir
    path: str
    size: Optional[int] = None
    is_dir: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "size": self.size,
                "is_dir": self.is_dir}


def within(path: str, roots: Sequence[str]) -> bool:
    """Whether a path resolves to somewhere inside one of the roots.

    ``realpath`` first, so a symlink planted inside a run folder cannot point
    the viewer at something outside it. An empty root list denies everything --
    failing closed is the only safe direction for a check like this.
    """
    if not roots:
        return False
    try:
        target = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except OSError:
        return False
    for root in roots:
        if not root:
            continue
        try:
            base = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
        except OSError:
            continue
        if target == base or target.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def read_view(
    path: str,
    mode: str = "tail",
    lines: int = 100,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FileView:
    """Read the last (or first) ``lines`` lines of a file.

    A tail seeks to the end and reads backwards in chunks, so the cost does not
    depend on the size of the file -- which matters because these are netlists
    and logs, routinely hundreds of megabytes.
    """
    resolved = os.path.abspath(os.path.expanduser(path))
    try:
        stat = os.stat(resolved)
    except OSError as exc:
        return FileView(path=resolved, mode=mode, lines_asked=lines,
                        error=str(exc))
    if os.path.isdir(resolved):
        return FileView(path=resolved, mode=mode, lines_asked=lines,
                        size=stat.st_size, mtime=stat.st_mtime,
                        error="this is a directory")

    try:
        if mode == "head":
            text, truncated = _read_head(resolved, lines, max_bytes)
        else:
            text, truncated = _read_tail(resolved, lines, max_bytes)
    except OSError as exc:
        return FileView(path=resolved, mode=mode, lines_asked=lines,
                        size=stat.st_size, mtime=stat.st_mtime,
                        error=str(exc))

    return FileView(
        path=resolved, text=text, size=stat.st_size, mtime=stat.st_mtime,
        mode=mode, lines_asked=lines,
        lines_shown=text.count("\n") + (1 if text else 0),
        truncated=truncated,
    )


def _read_head(path: str, lines: int, max_bytes: int) -> Tuple[str, bool]:
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    cut = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    parts = text.splitlines()
    if len(parts) > lines:
        return "\n".join(parts[:lines]), True
    return "\n".join(parts), cut


def _read_tail(path: str, lines: int, max_bytes: int) -> Tuple[str, bool]:
    """Read backwards from the end until enough newlines have been seen."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        block = 8192
        data = b""
        position = size
        while position > 0 and data.count(b"\n") <= lines and len(data) < max_bytes:
            step = min(block, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data

    truncated = position > 0
    text = data.decode("utf-8", errors="replace")
    parts = text.splitlines()
    if len(parts) > lines:
        parts = parts[-lines:]
        truncated = True
    return "\n".join(parts), truncated


def list_case_files(
    case_dir: str,
    limit: int = 200,
    max_depth: int = 4,
) -> List[FileEntry]:
    """The files one case produced, flattened and named relative to its dir.

    Flattened rather than a tree to click down: the interesting files sit three
    levels in (``<block>_<flow>/work_<flow>/<netlist>``) and making somebody
    walk there every time is the friction this is meant to remove.

    Depth and count are capped. A case directory can hold an intermediate
    database with thousands of files, and a page listing all of them is no more
    useful than none.
    """
    root = os.path.abspath(os.path.expanduser(case_dir))
    out: List[FileEntry] = []
    if not os.path.isdir(root):
        return out

    stack: List[Tuple[str, int]] = [(root, 0)]
    while stack and len(out) < limit:
        current, depth = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if len(out) >= limit:
                break
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if depth + 1 <= max_depth:
                    stack.append((entry.path, depth + 1))
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = None
            out.append(FileEntry(
                name=os.path.relpath(entry.path, root),
                path=entry.path, size=size))

    out.sort(key=lambda e: e.name)
    return out
