"""原子寫入。

state.json 隨時可能被 UI 或其他 user (透過公用碟) 讀到, 因此絕不能出現
「讀到寫到一半的檔案」。作法固定為 write tmp -> fsync -> os.replace,
os.replace 在同一個檔案系統上是原子操作。

JSONL 則是 append-only: 斷電最多壞掉最後一行, 前面的紀錄全部保得住 ——
這是選 JSONL 而非「每次覆寫整個 list」的原因。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Iterator, List, Optional


def atomic_write_json(path: str, data: Any, file_mode: int = 0o644) -> None:
    """原子地寫出 JSON。"""
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
    """讀 JSON。檔案不存在或損毀時回傳 default 而不是丟例外 ——
    快取檔損毀不應該讓監控停擺 (檔案系統才是唯一真相)。
    """
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def append_jsonl(path: str, record: Dict[str, Any], file_mode: int = 0o644) -> None:
    """append 一筆紀錄到 JSONL。"""
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
    """逐行讀 JSONL, 自動跳過損毀的行。"""
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
