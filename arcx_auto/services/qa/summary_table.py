"""Parsing and judging a QC_* Summary comparison table. **Pure** -- no I/O.

The file looks like this:

    input report: /path/to/a
    input report: /path/to/b

    refReport = /path/to/a
    rep        item      refReport  cmpReport1  diffCmp1
    some_cell  some_item      1.234       1.240     0.006
    other_cell other_item     2.000        fail         -
    ########

Only the table after ``refReport =`` matters. The first two columns name the
row; everything after is a value, and **every value must be a real number**.
Three things mean the comparison produced no answer:

  * a blank column
  * the word "fail"
  * a sentinel magnitude such as 1e+15, the tool's default failure value

Any of them on a case that is otherwise reported complete is a false success:
the run finished, the report exists and is non-empty, and the number that was
supposed to prove the result is simply not there.

The number of comparison columns varies -- cmpReport2, cmpReport3 and their
diffs may or may not be present -- so the header is what defines the width
rather than any fixed expectation.

Kept separate from the check itself, and from the filesystem, so every shape of
malformed table can be enumerated in milliseconds against strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class SummaryTable:
    """The parsed comparison table."""

    header: Tuple[str, ...] = ()
    rows: Tuple[Tuple[str, ...], ...] = ()
    ref_report: str = ""
    #: Why there is no table, when there is none
    error: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.error is None and bool(self.header)

    def value_columns(self, name_columns: int) -> Tuple[str, ...]:
        return tuple(self.header[name_columns:])


@dataclass(frozen=True)
class BadValue:
    """One cell that is not a number, with enough context to find it by eye."""

    row: str
    column: str
    value: str
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {"row": self.row, "column": self.column,
                "value": self.value, "reason": self.reason}


def parse_summary_table(
    text: str,
    ref_marker_regex: str = r"^\s*refReport\s*=",
    end_markers: Sequence[str] = ("########",),
) -> SummaryTable:
    """Extract the comparison table that follows the ``refReport =`` line.

    Everything before that line is input bookkeeping. The first non-empty line
    after it is the header; the rows run until a blank line or an end marker.
    """
    marker = re.compile(ref_marker_regex)
    lines = text.splitlines()

    start = None
    ref_report = ""
    for i, line in enumerate(lines):
        if marker.match(line):
            start = i
            _, _, rest = line.partition("=")
            ref_report = rest.strip()
            break

    if start is None:
        return SummaryTable(error="no 'refReport =' line found")

    # The header is the next line that has content
    header: Tuple[str, ...] = ()
    body_start = None
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if _is_end(stripped, end_markers):
            return SummaryTable(
                ref_report=ref_report,
                error="the table ended before a header line")
        header = tuple(stripped.split())
        body_start = i + 1
        break

    if not header or body_start is None:
        return SummaryTable(ref_report=ref_report,
                            error="no header line after 'refReport ='")

    rows: List[Tuple[str, ...]] = []
    for i in range(body_start, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            break
        if _is_end(stripped, end_markers):
            break
        rows.append(tuple(stripped.split()))

    return SummaryTable(header=header, rows=tuple(rows), ref_report=ref_report)


def find_bad_values(
    table: SummaryTable,
    fail_words: Sequence[str] = ("fail",),
    fail_value_threshold: float = 1e15,
    name_columns: int = 2,
) -> List[BadValue]:
    """Every cell in the table that is not a usable number.

    A row shorter than the header is reported as missing values rather than
    skipped: columns that stop early are exactly what a comparison that died
    part way through leaves behind.
    """
    if not table.found:
        return []

    lowered = {word.strip().lower() for word in fail_words if word.strip()}
    bad: List[BadValue] = []

    for row in table.rows:
        name = " ".join(row[:name_columns]) if row else "(empty row)"

        if len(row) <= name_columns:
            bad.append(BadValue(
                row=name or "(unnamed)",
                column="(all)",
                value="",
                reason="the row has no values at all",
            ))
            continue

        for position, column in enumerate(table.header[name_columns:],
                                          start=name_columns):
            if position >= len(row):
                bad.append(BadValue(
                    row=name, column=column, value="",
                    reason="the row stops before this column"))
                continue
            problem = _judge(row[position], lowered, fail_value_threshold)
            if problem is not None:
                bad.append(BadValue(row=name, column=column,
                                    value=row[position], reason=problem))
    return bad


def _judge(raw: str, fail_words: set, threshold: float) -> Optional[str]:
    """Why this cell is not a usable number, or None when it is fine."""
    value = raw.strip()
    if not value:
        return "empty"
    if value.lower() in fail_words:
        return "reported as %s" % value
    try:
        number = float(value)
    except ValueError:
        return "not a number"
    if number != number:                       # NaN
        return "not a number (nan)"
    if abs(number) >= threshold:
        return "%s is the tool's default failure value" % value
    return None


def _is_end(line: str, end_markers: Sequence[str]) -> bool:
    return any(marker and line.startswith(marker) for marker in end_markers)
