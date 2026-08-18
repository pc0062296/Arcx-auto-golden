"""Atomic writes.

state.json can be read by the UI or by other people through the shared disk at
any moment, so a half-written file must never be observable. The recipe is
always write-temp, fsync, os.replace -- replace is atomic within a filesystem.

JSONL is append only: a power cut damages at most the final line and every
earlier record survives. That is why events and audit use JSONL instead of
rewriting one big list.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Iterator, List, Optional


def atomic_write_json(path: str, data: Any, file_mode: int = 0o644) -> None:
    """Write JSON atomically."""
    path = os.path.abspath(os.path.expanduser(path))
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, file_mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json(path: str, default: Optional[Any] = None) -> Any:
    """Read JSON, returning default on a missing or corrupt file.

    A damaged cache must not stop monitoring; the filesystem is the truth.
    """
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def append_jsonl(path: str, record: Dict[str, Any], file_mode: int = 0o644) -> None:
    """Append one record to a JSONL file."""
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    if not exists:
        try:
            os.chmod(path, file_mode)
        except OSError:
            pass


def iter_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    """Read a JSONL file line by line, skipping damaged lines."""
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return
