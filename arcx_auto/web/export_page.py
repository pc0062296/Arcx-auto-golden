"""The static pages written to the shared disk.

The web server serves several linked pages that a browser fetches on demand.
The shared disk has no server, so this renders **one self-contained page** per
user instead: no links to follow, no requests to make, and everything worth
knowing already on it. Somebody opens a file off a mounted share and sees the
current state.

That is a genuinely different shape from the served UI, which is why it is a
separate renderer rather than a flag on the existing one -- but every primitive
(CSS, tables, pills, formatting) comes from html.py, so the two cannot drift in
appearance.

What goes on the page is chosen by one rule: **what would somebody ask if they
could not run the tool themselves?** They want to know whether anything needs a
person, and if so which case and why. So problems come first and detail comes
last, and the thousands of healthy cases are counted rather than listed.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from arcx_auto.web.html import (
    CSS,
    cards,
    duration,
    esc,
    progress_bar,
    severity_pill,
    size,
    state_pill,
    table,
    timestamp,
)

#: A run with none of these is finished, and belongs in history
IN_FLIGHT_STATES = ("RUNNING", "QUEUED", "PENDING", "STALLED", "SUSPENDED",
                    "LOST", "COMPLETED_MARKER")


def render_export(payload: Dict[str, Any]) -> str:
    """One self-contained page describing everything one user is running."""
    runs = payload.get("runs") or []
    active = [r for r in runs if _is_active(r)]
    finished = [r for r in runs if not _is_active(r)]

    # Attention is scanned across **every** run, finished ones included. A run
    # that ended with failures is still something a person has to look at --
    # more so, because nothing is going to change on its own. Scanning only the
    # active runs let a finished run full of FAILED cases sit under the words
    # "nothing needs a person right now", which is the exact false
    # reassurance this page exists to prevent.
    body = [
        _summary(payload, active, finished),
        _attention_first(runs),
    ]
    # Detail for anything still moving, plus anything finished that went wrong.
    detailed = active + [r for r in finished if _has_problems(r)]
    for run in detailed:
        body.append(_run_section(run))
    body.append(_history(finished))

    return _page(
        title="arcx-auto %s" % payload.get("user", "?"),
        meta="%s@%s" % (payload.get("user", "?"), payload.get("host", "?")),
        generated_at=payload.get("generated_at"),
        body="".join(part for part in body if part),
    )


def render_shared_index(users: Sequence[Dict[str, Any]],
                        generated_at: Optional[float] = None) -> str:
    """The root page: one row per user who has exported.

    Built by whoever exported last, from every ``<user>/status.json`` on the
    share. Each user only ever writes their own directory, so this file is the
    single place they overlap -- and it is derived, so losing a race just means
    the next export rewrites it.
    """
    rows = []
    for user in sorted(users, key=lambda u: u.get("user") or ""):
        totals = user.get("totals") or {}
        attention = totals.get("attention") or 0
        rows.append([
            '<a href="%s/status.html">%s</a>'
            % (esc(user.get("user", "")), esc(user.get("user", "?"))),
            esc(user.get("host", "-")),
            str(totals.get("runs") or 0),
            str(totals.get("cases") or 0),
            str(totals.get("done") or 0),
            ("<span class='bad'>%d</span>" % attention) if attention else "0",
            _staleness(user.get("generated_at")),
        ])

    content = table(
        ["user", "host", "runs", "cases", "done", "attention", "updated"],
        rows) if rows else "<div class='empty'>nobody has exported yet</div>"

    return _page(
        title="arcx-auto",
        meta="everyone",
        generated_at=generated_at,
        body="<h2>users</h2>" + content + _shared_footer(),
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _summary(payload: Dict[str, Any], active: Sequence[Dict[str, Any]],
             finished: Sequence[Dict[str, Any]]) -> str:
    totals = payload.get("totals") or {}
    attention = totals.get("attention") or 0
    items = [
        ("active runs", len(active), ""),
        ("cases", totals.get("cases") or 0, ""),
        ("done", totals.get("done") or 0, "good"),
        ("attention", attention, "bad" if attention else ""),
        ("finished runs", len(finished), ""),
    ]
    return "<h2>summary</h2>" + cards(
        [(label, value, klass) for label, value, klass in items])


def _attention_first(runs: Sequence[Dict[str, Any]]) -> str:
    """Every case a person may need to look at, across every run.

    Deliberately the first thing on the page. Somebody reading this off a share
    is asking one question -- is anything wrong -- and making them scroll
    through healthy runs to find out is how a status page stops being read.
    """
    rows = []
    for run in runs:
        for index in run.get("indexes") or []:
            for case in index.get("cases") or []:
                if not _needs_attention(case):
                    continue
                rows.append([
                    esc(run.get("run_id", "?")),
                    esc(index.get("index_key", "?")),
                    esc(case.get("case_id", "?")),
                    state_pill(case.get("state", "")),
                    duration(case.get("in_state_sec")),
                    duration(case.get("silent_sec")),
                    esc(case.get("note") or ""),
                ])

    if not rows:
        return ("<h2>needs attention</h2>"
                "<div class='empty good'>nothing needs a person right now</div>")
    return ("<h2>needs attention (%d)</h2>" % len(rows)) + table(
        ["run", "index", "case", "state", "in state", "log quiet", "reason"],
        rows)


def _run_section(run: Dict[str, Any]) -> str:
    counts = _run_counts(run)
    parts = [
        "<h2>run %s</h2>" % esc(run.get("run_id", "?")),
        _run_banner(run),
        progress_bar(counts),
        table(
            ["index", "cases", "done", "running", "queued", "attention",
             "run folder"],
            [
                [
                    esc(index.get("index_key", "?")),
                    str(sum((index.get("counts") or {}).values())),
                    str((index.get("counts") or {}).get("DONE", 0)),
                    str((index.get("counts") or {}).get("RUNNING", 0)),
                    str((index.get("counts") or {}).get("QUEUED", 0)),
                    _attention_cell(index.get("attention") or 0),
                    "<span class='muted'>%s</span>"
                    % esc(index.get("run_folder", "")),
                ]
                for index in run.get("indexes") or []
            ],
        ),
        _issues(run),
    ]
    return "".join(parts)


def _run_banner(run: Dict[str, Any]) -> str:
    notes = []
    lsf = run.get("lsf") or {}
    if not lsf.get("available", True):
        notes.append(
            "<span class='warn'>LSF unavailable: %s -- LOST and SUSPENDED "
            "detection is off</span>" % esc(lsf.get("note") or "?"))
    daemon = run.get("daemon") or {}
    if daemon.get("last_error"):
        notes.append("<span class='bad'>the last scan failed</span>")
    stale = _staleness(run.get("updated_at"), warn_after=600)
    notes.append("<span class='muted'>updated %s</span>" % stale)
    return "<p class='doc'>%s</p>" % " &middot; ".join(notes)


def _issues(run: Dict[str, Any]) -> str:
    """Issues grouped by id: 200 cases hitting one problem is one line."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for issue in run.get("issues") or []:
        entry = grouped.setdefault(issue.get("id", "?"), {
            "severity": issue.get("severity", "INFO"),
            "title": issue.get("title", ""),
            "targets": [],
        })
        target = issue.get("case_id") or issue.get("index_key")
        if target and target not in entry["targets"]:
            entry["targets"].append(target)

    blocking = {k: v for k, v in grouped.items()
                if v["severity"] in ("FATAL", "UNKNOWN", "WARN")}
    if not blocking:
        return ""

    rows = []
    for issue_id in sorted(blocking, key=lambda k: _severity_rank(
            blocking[k]["severity"])):
        entry = blocking[issue_id]
        targets = entry["targets"]
        shown = ", ".join(targets[:8])
        if len(targets) > 8:
            shown += " and %d more" % (len(targets) - 8)
        rows.append([
            severity_pill(entry["severity"]),
            esc(issue_id),
            str(len(targets)),
            esc(entry["title"]),
            esc(shown),
        ])
    return "<h3>issues</h3>" + table(
        ["severity", "id", "count", "what", "where"], rows)


def _history(runs: Sequence[Dict[str, Any]]) -> str:
    """Finished runs, one row each.

    History is what makes "did this ever work" answerable without asking the
    person who ran it.
    """
    if not runs:
        return ""
    rows = []
    for run in runs:
        counts = _run_counts(run)
        total = sum(counts.values())
        done = counts.get("DONE", 0)
        failed = counts.get("FAILED", 0)
        rows.append([
            esc(run.get("run_id", "?")),
            str(total),
            "<span class='good'>%d</span>" % done if done else "0",
            "<span class='bad'>%d</span>" % failed if failed else "0",
            timestamp(run.get("updated_at")),
        ])
    return ("<h2>finished runs (%d)</h2>" % len(runs)) + table(
        ["run", "cases", "done", "failed", "last seen"], rows)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page(title: str, meta: str, generated_at: Optional[float],
          body: str) -> str:
    """Like html.page, but with no navigation and a generated-at stamp.

    No `crumbs`, because there is nowhere to navigate to: this is one file on a
    share, not a site.
    """
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>%s</style></head><body>"
        "<header><h1>%s</h1><span class='meta'>%s</span>"
        "<span class='meta'>generated %s</span></header><main>%s</main>"
        "</body></html>"
        % (esc(title), CSS, esc(title), esc(meta),
           esc(timestamp(generated_at)), body)
    )


def _shared_footer() -> str:
    return ("<p class='doc'>Derived data, rewritten by each user's daemon. "
            "The truth lives in the run folders; this can be deleted and "
            "rebuilt at any time.</p>")


def _run_counts(run: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for index in run.get("indexes") or []:
        for state, number in (index.get("counts") or {}).items():
            counts[state] = counts.get(state, 0) + number
    return counts


def _is_active(run: Dict[str, Any]) -> bool:
    counts = _run_counts(run)
    return any(counts.get(state) for state in IN_FLIGHT_STATES)


def _has_problems(run: Dict[str, Any]) -> bool:
    """Whether a finished run still deserves a full section.

    Finishing is not the same as being fine.
    """
    if any(index.get("attention") for index in run.get("indexes") or []):
        return True
    return any(issue.get("severity") in ("FATAL", "UNKNOWN")
               for issue in run.get("issues") or [])


def _needs_attention(case: Dict[str, Any]) -> bool:
    return case.get("state") in ("FAILED", "LOST", "STALLED", "SUSPENDED",
                                 "UNKNOWN")


def _attention_cell(count: int) -> str:
    return "<span class='bad'>%d</span>" % count if count else "-"


def _severity_rank(severity: str) -> int:
    order = {"FATAL": 0, "UNKNOWN": 1, "WARN": 2, "INFO": 3}
    return order.get(severity, 4)


def _staleness(value: Optional[float], warn_after: float = 900.0) -> str:
    """How long ago, said loudly when it is long enough to distrust.

    A page frozen at a moment hours ago looks exactly like a healthy one, which
    is the most misleading thing a status display can do.
    """
    if not value:
        return "<span class='muted'>never</span>"
    age = time.time() - value
    text = "%s ago" % duration(age)
    if age > warn_after:
        return "<span class='bad'>%s</span>" % esc(text)
    return "<span class='muted'>%s</span>" % esc(text)
