"""終端輸出格式化。

刻意不依賴任何第三方套件 (rich / tabulate) —— 內網環境安裝相依套件是摩擦,
而表格渲染只需要幾十行。
"""

from __future__ import annotations

import time
import unicodedata
from typing import List, Optional, Sequence


def display_width(text: str) -> int:
    """計算終端顯示寬度 (CJK 字元佔 2 格)。"""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int, align: str = "left") -> str:
    padding = max(0, width - display_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def truncate(text: str, limit: int) -> str:
    if limit <= 1 or display_width(text) <= limit:
        return text
    out = ""
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + char_width > limit - 1:
            break
        out += char
        used += char_width
    return out + "~"


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: Optional[Sequence[str]] = None,
    max_col_width: int = 60,
) -> str:
    """渲染一張對齊的文字表格。"""
    if not rows:
        return "  (無資料)"

    columns = len(headers)
    aligns = list(aligns or ["left"] * columns)
    cells = [[truncate(str(c), max_col_width) for c in row] for row in rows]

    widths: List[int] = []
    for i in range(columns):
        width = display_width(str(headers[i]))
        for row in cells:
            if i < len(row):
                width = max(width, display_width(row[i]))
        widths.append(width)

    lines = []
    lines.append("  " + "  ".join(
        _pad(str(headers[i]), widths[i], aligns[i]) for i in range(columns)
    ))
    lines.append("  " + "  ".join("-" * widths[i] for i in range(columns)))
    for row in cells:
        lines.append("  " + "  ".join(
            _pad(row[i] if i < len(row) else "", widths[i], aligns[i])
            for i in range(columns)
        ))
    return "\n".join(lines)


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%ds" % int(seconds)
    if seconds < 3600:
        return "%dm%02ds" % (int(seconds) // 60, int(seconds) % 60)
    if seconds < 86400:
        return "%dh%02dm" % (int(seconds) // 3600, (int(seconds) % 3600) // 60)
    return "%dd%02dh" % (int(seconds) // 86400, (int(seconds) % 86400) // 3600)


def format_timestamp(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def format_size(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "-"
    value = float(num_bytes)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return "%.0f%s" % (value, unit) if unit == "B" else "%.1f%s" % (value, unit)
        value /= 1024.0
    return "%.1fT" % value
