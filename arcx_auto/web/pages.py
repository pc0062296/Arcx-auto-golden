"""Page content generation. Pure functions: state.json contents -> HTML.

Deliberately imports no service: the UI renders what the daemon already
computed. That is what lets the UI be closed, reopened, crash or be replaced
entirely without disturbing the daemon (architecture decision 1).
"""

from __future__ import annotations

import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple

from arcx_auto.web.html import (
    cards,
    duration,
    esc,
    page,
    progress_bar,
    severity_pill,
    size,
    state_pill,
    table,
    timestamp,
)

_SEVERITY_RANK = {"FATAL": 0, "UNKNOWN": 1, "WARN": 2, "INFO": 3}


def _q(*parts: str) -> str:
    return "/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)


# ---------------------------------------------------------------------------
# Home: an overview of every run
# ---------------------------------------------------------------------------

def render_home(states: Sequence[Dict[str, Any]], refresh: int,
                workspaces: Sequence[Any] = ()) -> str:
    rows = []
    total_attention = 0
    for state in states:
        totals = state.get("totals") or {}
        attention = totals.get("attention", 0)
        total_attention += attention
        run_id = state.get("run_id", "?")
        daemon = state.get("daemon") or {}
        rows.append([
            "<a href='%s'>%s</a>" % (esc(_q("run", run_id)), esc(run_id)),
            progress_bar(totals.get("states") or {}),
            esc(totals.get("cases", 0)),
            esc(totals.get("indexes", 0)),
            ("<span class='bad'>%d</span>" % attention) if attention else "-",
            esc(totals.get("issues", 0)),
            esc(timestamp(state.get("updated_at"))),
            _daemon_health(daemon, state.get("updated_at")),
        ])

    body = cards([
        ("needs your decision", total_attention,
         "alert" if total_attention else ""),
        ("runs monitored", len(states), ""),
    ])
    # The first question anybody opening this page has is "is anything wrong",
    # and answering it with a table of runs makes them work it out from
    # numbers. Name the cases instead, before anything else.
    body += _attention_across_runs(states)
    body += _workspaces_section(workspaces)
    body += "<h2>runs</h2>"
    body += table(
        ["run", "progress", "cases", "index", "attention", "issues",
         "updated", "daemon"],
        rows, numeric=[2, 3, 4, 5],
        empty="no run is being monitored yet; start one with arcx-auto daemon",
    )
    return page("Arcx Auto Golden", body, refresh=refresh)


def _workspaces_section(entries: Sequence[Any]) -> str:
    """Which directory each daemon is working in.

    A workspace is a run_root, and ./arcx_runs beside the data is the normal
    way to keep them apart. That makes "which daemon owns which directory" a
    question with a real answer, and one nobody can get at from a process
    list: two daemons look identical from outside.
    """
    hint = ("<p class='doc'>A workspace is a run_root -- normally "
            "<code>./arcx_runs</code> beside the data. To add one: "
            "<code>cd &lt;directory&gt; &amp;&amp; arcx-auto daemon</code></p>")
    if not entries:
        return "<h2>workspaces</h2>" + hint + (
            "<div class='empty'>no daemon has registered a workspace</div>")

    rows = []
    for entry in entries:
        if entry.alive():
            health = "<span class='pill good'>watching</span>"
        elif entry.stopped_at:
            health = "<span class='pill muted'>stopped</span>"
        else:
            health = ("<span class='pill warn'>silent for %s</span>"
                      % esc(duration(entry.age_sec())))
        rows.append([
            esc(entry.run_root),
            health,
            "<a href='%s'>%s</a>" % (esc(_q("run", entry.run_id)),
                                     esc(entry.run_id)),
            "<span class='muted'>%s</span>" % esc(entry.cwd),
            "<span class='muted'>%s@%s</span>" % (esc(entry.pid),
                                                  esc(entry.host)),
        ])
    return ("<h2>workspaces</h2>" + hint
            + table(["run_root", "daemon", "run", "started in", "pid"], rows))


ATTENTION_STATES = ("FAILED", "LOST", "STALLED", "SUSPENDED", "UNKNOWN")


def _attention_across_runs(states: Sequence[Dict[str, Any]],
                           limit: int = 25) -> str:
    """Every case needing a person, across every run, named and linked.

    Deliberately above the run table. Somebody opening this page is asking one
    thing, and a row of counts makes them derive the answer instead of reading
    it.
    """
    rows = []
    for state in states:
        run_id = state.get("run_id", "?")
        for index in state.get("indexes") or []:
            index_key = index.get("index_key", "?")
            for case in index.get("cases") or []:
                if case.get("state") not in ATTENTION_STATES:
                    continue
                rows.append([
                    "<a href='%s'>%s</a>"
                    % (esc(_q("run", run_id)), esc(run_id)),
                    "<a href='%s'>%s</a>"
                    % (esc(_q("run", run_id, "index", index_key)),
                       esc(index_key)),
                    "<a href='%s'>%s</a>"
                    % (esc(_q("run", run_id, "index", index_key, "case",
                              case.get("case_id", ""))),
                       esc(case.get("case_id", "?"))),
                    state_pill(case.get("state", "")),
                    esc(duration(case.get("in_state_sec"))),
                    "<span class='muted'>%s</span>" % esc(case.get("note") or ""),
                ])

    if not rows:
        return ("<h2>needs attention</h2>"
                "<div class='empty good'>nothing needs a person right now</div>")

    more = ""
    if len(rows) > limit:
        more = ("<p class='doc'>and %d more, in the runs below.</p>"
                % (len(rows) - limit))
    return ("<h2>needs attention (%d)</h2>" % len(rows)) + table(
        ["run", "index", "case", "state", "in state", "why"],
        rows[:limit]) + more


def _daemon_health(daemon: Dict[str, Any], updated_at: Optional[float]) -> str:
    """The daemon has to be monitored too: if it dies quietly the display
    freezes at the last moment and looks perfectly healthy, which is the most
    dangerous state of all.
    """
    import time

    if not daemon:
        return "<span class='muted'>-</span>"
    # A daemon that stopped cleanly says so. Otherwise its last snapshot stays
    # on the page looking live, and the moment it froze at is exactly the
    # moment it was healthy.
    if daemon.get("running") is False:
        return "<span class='muted'>stopped</span>"
    if daemon.get("last_error"):
        return "<span class='bad'>error</span>"
    if updated_at and time.time() - updated_at > 900:
        return "<span class='warn'>no update for over 15 min</span>"
    return "<span class='good'>pid %s</span>" % esc(daemon.get("pid"))


# ---------------------------------------------------------------------------
# Run detail
# ---------------------------------------------------------------------------

def render_run(state: Dict[str, Any], refresh: int) -> str:
    run_id = state.get("run_id", "?")
    totals = state.get("totals") or {}
    severities = totals.get("severities") or {}

    body = cards([
        ("needs your decision", totals.get("attention", 0),
         "alert" if totals.get("attention") else ""),
        ("cases", totals.get("cases", 0), ""),
        ("FATAL", severities.get("FATAL", 0), "alert" if severities.get("FATAL") else ""),
        ("UNKNOWN", severities.get("UNKNOWN", 0), ""),
        ("WARN", severities.get("WARN", 0), ""),
    ])

    body += _finished_banner(state)
    body += _lsf_banner(state)
    body += _daemon_banner(state)

    body += "<h2>issue summary</h2>" + _issue_summary(state, run_id)

    body += "<h2>index</h2>" + _index_sections(
        run_id, state.get("indexes") or [])

    return page("run %s" % run_id, body, refresh=refresh,
                crumbs=[("/", "all runs"), (_q("run", run_id), run_id)],
                meta="updated %s" % timestamp(state.get("updated_at")))


_INDEX_COLUMNS = ["index", "progress", "cases", "done", "attention",
                  "scan anomalies", "run folder"]


def _index_rows(run_id: str,
                indexes: Sequence[Dict[str, Any]]) -> List[List[str]]:
    rows = []
    for index in indexes:
        counts = index.get("counts") or {}
        attention = index.get("attention", 0)
        rows.append([
            "<a href='%s'>%s</a>" % (
                esc(_q("run", run_id, "index", index.get("index_key", "?"))),
                esc(index.get("index_key"))),
            progress_bar(counts),
            esc(sum(counts.values())),
            esc(counts.get("DONE", 0)),
            ("<span class='bad'>%d</span>" % attention) if attention else "-",
            _anomaly_cell(index),
            "<span class='muted'>%s</span>" % esc(index.get("run_folder")),
        ])
    return rows


def _index_table(run_id: str, indexes: Sequence[Dict[str, Any]]) -> str:
    return table(_INDEX_COLUMNS, _index_rows(run_id, indexes),
                 numeric=[2, 3, 4])


def _index_sections(run_id: str, indexes: Sequence[Dict[str, Any]]) -> str:
    """The index table, inside the structure the submission actually had.

    One submission can carry several cfgs and, under each, several source
    folders. Flattened into one table of two hundred rows that structure is
    invisible, and "which corner was this one" becomes a question about a
    path. Nested, the shape of the run is the shape of the page:

        typical  (3 folders, 40 index, 2 need a person)
          corner_v2g/Cbest_T/blockA   (12 index)
            <the same index table as before>

    Open where something needs a person, closed where nothing does -- the
    point is to stop having to read everything, so everything cannot be open.

    A run made before the batch layout carries none of this, and gets the flat
    table it always had rather than a container called "".
    """
    if not indexes:
        return _index_table(run_id, indexes)
    if not any(i.get("group") or i.get("cfg") or i.get("folder")
               for i in indexes):
        return _index_table(run_id, indexes)

    groups: List[Tuple[str, List[Dict[str, Any]]]] = []
    seen: Dict[str, int] = {}
    for index in indexes:
        key = index.get("group") or index.get("cfg") or "(no cfg recorded)"
        position = seen.get(key)
        if position is None:
            seen[key] = len(groups)
            groups.append((key, [index]))
        else:
            groups[position][1].append(index)

    out = []
    for name, members in groups:
        inner = []
        folders: List[Tuple[str, List[Dict[str, Any]]]] = []
        by_folder: Dict[str, int] = {}
        for index in members:
            key = index.get("folder") or ""
            position = by_folder.get(key)
            if position is None:
                by_folder[key] = len(folders)
                folders.append((key, [index]))
            else:
                folders[position][1].append(index)

        for folder, items in folders:
            inner.append(_container(
                _folder_label(folder), items, _index_table(run_id, items),
                only_one=len(folders) == 1))
        out.append(_container("<strong>%s</strong>" % esc(name), members,
                              "".join(inner), only_one=len(groups) == 1))
    return "".join(out)


def _folder_label(folder: str) -> str:
    """The last two components, which is what tells two folders apart.

    The full path is on every row already; a heading repeating sixty
    characters of shared prefix says nothing.
    """
    if not folder:
        return "<span class='muted'>no source folder recorded</span>"
    parts = [p for p in folder.rstrip("/").split("/") if p]
    tail = "/".join(parts[-2:]) if len(parts) > 1 else parts[-1]
    return "%s <span class='muted'>%s</span>" % (esc(tail), esc(folder))


def _container(label: str, indexes: Sequence[Dict[str, Any]],
               inner: str, only_one: bool = False) -> str:
    """One collapsible level, with enough on the closed line to skip it."""
    attention = sum(i.get("attention", 0) for i in indexes)
    cases = sum(sum((i.get("counts") or {}).values()) for i in indexes)
    summary = ("%s <span class='muted'>%d index, %d case(s)</span>"
               % (label, len(indexes), cases))
    if attention:
        summary += " <span class='pill bad'>%d need a person</span>" % attention
    # Open when there is something to act on, or when it is the only one.
    # Everything open is the flat table again.
    is_open = " open" if (attention or only_one) else ""
    return ("<details%s style='margin:6px 0;padding:4px 0 4px 10px;"
            "border-left:3px solid rgba(128,128,128,.35)'>"
            "<summary style='cursor:pointer'>%s</summary>%s</details>"
            % (is_open, summary, inner))


def _finished_banner(state: Dict[str, Any]) -> str:
    """Say when a run is over, and whether it is over *well*.

    Somebody watching a run needs to know it has stopped needing them, and the
    case table does not say that -- it says a lot of numbers that they have to
    add up. Finishing and succeeding are shown separately, because a run can do
    the first without the second.
    """
    counts: Dict[str, int] = {}
    for index in state.get("indexes") or []:
        for name, number in (index.get("counts") or {}).items():
            counts[name] = counts.get(name, 0) + number
    if not counts:
        return ""

    in_flight = sum(counts.get(name, 0) for name in
                    ("RUNNING", "QUEUED", "PENDING", "COMPLETED_MARKER"))
    if in_flight:
        return ""

    total = sum(counts.values())
    done = counts.get("DONE", 0)
    if done == total:
        return (
            "<div class='card' style='border-color:var(--good);"
            "margin-bottom:8px'><span class='good'>finished -- all %d case(s) "
            "passed</span><div class='l'>Nothing here needs a person. The "
            "results are ready to use.</div></div>" % total)
    bad = total - done
    return (
        "<div class='card' style='border-color:var(--bad);margin-bottom:8px'>"
        "<span class='bad'>finished, with %d of %d case(s) unresolved</span>"
        "<div class='l'>Nothing is running any more, so these will not "
        "improve on their own.</div></div>" % (bad, total))


def _lsf_banner(state: Dict[str, Any]) -> str:
    lsf = state.get("lsf") or {}
    if lsf.get("available"):
        return ""
    return (
        "<div class='card' style='border-color:var(--warn);margin-bottom:8px'>"
        "<span class='warn'>LSF data unavailable: %s</span>"
        "<div class='doc'>LOST and SUSPENDED detection is disabled; only "
        "markers and logs are used.</div></div>"
        % esc(lsf.get("note") or "reason unknown")
    )


def _daemon_banner(state: Dict[str, Any]) -> str:
    """Say plainly when nothing is watching this run any more.

    Everything below the banner is a snapshot from whenever the daemon last
    looked. Without saying so, a page from three hours ago is indistinguishable
    from a page from ten seconds ago.
    """
    daemon = state.get("daemon") or {}
    if daemon.get("running") is False:
        return (
            "<div class='card' style='border-color:var(--warn);"
            "margin-bottom:8px'><span class='warn'>the daemon has stopped"
            "</span><div class='l'>Nothing is watching this run. Anything "
            "below is from %s. Jobs already sent to LSF carry on regardless; "
            "start the daemon again to resume monitoring.</div></div>"
            % esc(timestamp(daemon.get("stopped_at"))))
    if not daemon.get("last_error"):
        return ""
    return (
        "<div class='card' style='border-color:var(--bad);margin-bottom:8px'>"
        "<span class='bad'>the last scan failed</span><pre>%s</pre></div>"
        % esc(daemon["last_error"])
    )


def _issue_summary(state: Dict[str, Any], run_id: str) -> str:
    """Group by id. When 200 cases hit the same problem, an engineer needs to
    see "NETLIST_MISSING x 200", not 200 identical lines.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for issue in state.get("issues") or []:
        grouped.setdefault(issue["id"], []).append(issue)
    if not grouped:
        return "<div class='empty'>no problems found</div>"

    rows = []
    for issue_id, group in sorted(
        grouped.items(),
        key=lambda kv: (_SEVERITY_RANK.get(kv[1][0]["severity"], 9),
                        -len(kv[1]), kv[0]),
    ):
        first = group[0]
        targets = sorted({(i.get("case_id") or i.get("index_key") or "-")
                          for i in group})
        shown = ", ".join(esc(t) for t in targets[:6])
        if len(targets) > 6:
            shown += " <span class='muted'>... (+%d)</span>" % (len(targets) - 6)
        rows.append([
            severity_pill(first["severity"]),
            esc(issue_id),
            esc(len(group)),
            esc(first.get("title") or ""),
            shown,
        ])
    return table(["severity", "issue id", "count", "description", "targets"],
                 rows, numeric=[2])


def _anomaly_cell(index: Dict[str, Any]) -> str:
    anomalies = index.get("anomalies") or {}
    parts = []
    if anomalies.get("unknown_markers"):
        parts.append("<span class='bad'>unknown markers %d</span>"
                     % len(anomalies["unknown_markers"]))
    if anomalies.get("unresolved_logs"):
        parts.append("<span class='warn'>orphan logs %d</span>"
                     % len(anomalies["unresolved_logs"]))
    if anomalies.get("unexpected_dirs"):
        parts.append("<span class='warn'>non-case dirs %d</span>"
                     % len(anomalies["unexpected_dirs"]))
    if anomalies.get("unmatched_entries"):
        parts.append("<span class='muted'>unclassified %d</span>"
                     % len(anomalies["unmatched_entries"]))
    return " ".join(parts) or "-"


# ---------------------------------------------------------------------------
# Index detail
# ---------------------------------------------------------------------------

def render_index(state: Dict[str, Any], index: Dict[str, Any],
                 refresh: int, show: str = "") -> str:
    run_id = state.get("run_id", "?")
    index_key = index.get("index_key", "?")
    counts = index.get("counts") or {}
    all_cases = index.get("cases") or []
    shown = _matching(all_cases, show)

    body = cards([(state_name, count,
                   "alert" if state_name in ("FAILED", "LOST", "STALLED") else "")
                  for state_name, count in sorted(counts.items())])
    body += rerun_link(state, index)

    rows = []
    for case in shown:
        issue_ids = sorted({i["id"] for i in case.get("issues") or []})
        rows.append([
            "<a href='%s'>%s</a>" % (
                esc(_q("run", run_id, "index", index_key,
                       "case", case["case_id"])),
                esc(case["case_id"])),
            state_pill(case["state"]),
            _lsf_cell(case),
            esc(duration(case.get("in_state_sec"))),
            _silent_cell(case),
            esc(size(case.get("log_size"))),
            ", ".join(esc(i) for i in issue_ids) or "-",
            "<span class='muted'>%s</span>" % esc(case.get("note") or ""),
        ])

    body += "<h2>cases</h2>"
    body += _filter_bar(run_id, index_key, all_cases, show)
    body += table(
        ["case", "state", "LSF", "in state", "log quiet", "log size",
         "issues", "note"],
        rows, numeric=[3, 4, 5],
        empty=("no case matches this filter"
               if show else "this index has no case"))

    body += _anomaly_section(index)

    return page("%s / %s" % (run_id, index_key), body, refresh=refresh,
                crumbs=[("/", "all runs"), (_q("run", run_id), run_id),
                        (_q("run", run_id, "index", index_key), index_key)])


def _matching(cases: Sequence[Dict[str, Any]], show: str
              ) -> List[Dict[str, Any]]:
    """The cases a filter selects. Unknown filter values select everything.

    An unrecognised value showing everything, rather than nothing, is
    deliberate: a hand-edited or stale URL should not make a case table look
    empty, which reads exactly like "there is nothing wrong".
    """
    if not show or show == "all":
        return list(cases)
    if show == "attention":
        return [c for c in cases if c.get("state") in ATTENTION_STATES]
    if show in _STATE_ORDER or show in {c.get("state") for c in cases}:
        return [c for c in cases if c.get("state") == show]
    return list(cases)


#: The order filter chips appear in: the states worth looking at first.
_STATE_ORDER = ("FAILED", "LOST", "STALLED", "SUSPENDED", "UNKNOWN",
                "RUNNING", "QUEUED", "PENDING", "COMPLETED_MARKER", "DONE")


def _filter_bar(run_id: str, index_key: str,
                cases: Sequence[Dict[str, Any]], show: str) -> str:
    """Links, not JavaScript, so a filtered table is a URL somebody can send.

    An index of three hundred cases where four are wrong is a table nobody
    reads; the four are the whole content of the page. Filtering with plain
    links keeps that shareable and keeps the auto refresh honest -- the page
    reloads into the same filter rather than dropping back to everything.
    """
    counts: Dict[str, int] = {}
    for case in cases:
        state = case.get("state") or "UNKNOWN"
        counts[state] = counts.get(state, 0) + 1
    attention = sum(counts.get(s, 0) for s in ATTENTION_STATES)

    base = _q("run", run_id, "index", index_key)
    options: List[Tuple[str, str, int, bool]] = [
        ("all", "all", len(cases), False),
    ]
    if attention:
        options.append(("attention", "needs a person", attention, True))
    for state in _STATE_ORDER:
        if counts.get(state):
            options.append((state, state, counts[state],
                            state in ATTENTION_STATES))
    for state in sorted(counts):
        if state not in _STATE_ORDER:
            options.append((state, state, counts[state], False))

    current = show or "all"
    chips = []
    for value, label, count, alert in options:
        selected = (current == value)
        href = base if value == "all" else "%s?show=%s" % (
            base, urllib.parse.quote(value))
        klass = "pill %s" % ("bad" if alert else "muted")
        if selected:
            chips.append("<span class='%s' style='font-weight:600;"
                         "text-decoration:underline'>%s %d</span>"
                         % (klass, esc(label), count))
        else:
            chips.append("<a class='%s' href='%s'>%s %d</a>"
                         % (klass, esc(href), esc(label), count))
    return ("<p style='display:flex;gap:6px;flex-wrap:wrap;margin:0 0 8px'>"
            "%s</p>" % "".join(chips))


def rerun_link(state: Dict[str, Any], index: Dict[str, Any]) -> str:
    """Offer a rerun where the problem is visible, not on a separate page.

    Only when something is actually wrong: a button that is always there
    invites a rerun of work that did not need one, and a rerun is the one
    operation that moves directories.

    The scope is the directory Arcx ran in -- one source folder, not the whole
    wave -- because that is the directory this index's cases live in. Since
    each folder has its own Arcx parent, its own launch record and its own
    lock, rerunning one leaves the rest of the wave alone.
    """
    if not index.get("attention"):
        return ""
    run_folder = index.get("run_folder") or ""
    wave_dir = os.path.dirname(run_folder.rstrip("/"))
    if not wave_dir:
        return ""
    run_id = state.get("run_id", "")
    return (
        "<p><a class='pill bad' href=\"/rerun?wave_dir=%s&amp;run_id=%s"
        "&amp;back=%s\">rerun this folder...</a> "
        "<span class='muted'>shows what would be moved aside before "
        "anything happens</span></p>"
        % (urllib.parse.quote(wave_dir), urllib.parse.quote(run_id),
           urllib.parse.quote("/run/%s/index/%s"
                              % (run_id, index.get("index_key", ""))))
    )


def _lsf_cell(case: Dict[str, Any]) -> str:
    """One column's worth of the same distinction."""
    if case.get("lsf_job_matched") and case.get("lsf_state"):
        return esc(case["lsf_state"])
    if case.get("lsf_job_id"):
        return "<span class='warn'>gone</span>"
    return "<span class='muted'>not matched</span>"


def _silent_cell(case: Dict[str, Any]) -> str:
    """Quiet time escalates visually as it grows: the system states the
    number, the judgement stays human.
    """
    silent = case.get("silent_sec") or 0
    text = esc(duration(silent))
    if silent >= 28800:
        return "<span class='bad'>%s</span>" % text
    if silent >= 14400:
        return "<span class='warn'>%s</span>" % text
    return text


def _anomaly_section(index: Dict[str, Any]) -> str:
    anomalies = index.get("anomalies") or {}
    rows = []
    for item in anomalies.get("unknown_markers") or []:
        rows.append(["<span class='bad'>unknown marker</span>",
                     esc(item.get("file")),
                     esc("only queue/run/complete are known; case=%s"
                         % item.get("case_id"))])
    for name in anomalies.get("unresolved_logs") or []:
        rows.append(["<span class='warn'>log unmapped</span>", esc(name),
                     "its cmd_file is missing or unparseable"])
    for name in anomalies.get("unexpected_dirs") or []:
        rows.append(["<span class='warn'>dir is not a case</span>", esc(name),
                     "no marker and no cmd_file names it; not counted"])
    for name in anomalies.get("unmatched_entries") or []:
        rows.append(["<span class='muted'>unclassified</span>", esc(name),
                     "matches no known convention"])
    if not rows:
        return ""
    return "<h2>scan anomalies</h2>" + table(
        ["kind", "name", "description"], rows)


# ---------------------------------------------------------------------------
# Case detail
# ---------------------------------------------------------------------------

def render_case(state: Dict[str, Any], index: Dict[str, Any],
                case: Dict[str, Any], refresh: int,
                log: Optional[Dict[str, Any]] = None,
                files: Sequence[Dict[str, Any]] = ()) -> str:
    run_id = state.get("run_id", "?")
    index_key = index.get("index_key", "?")
    case_id = case["case_id"]

    body = cards([
        ("state", case["state"],
         "alert" if case["state"] in ("FAILED", "LOST", "STALLED") else ""),
        ("in state", duration(case.get("in_state_sec")), ""),
        ("log quiet", duration(case.get("silent_sec")), ""),
        ("log size", size(case.get("log_size")), ""),
    ])

    facts = [
        ("structural state", case.get("base_state") or "-"),
        ("reason", case.get("note") or "-"),
        ("LSF", _lsf_fact(case)),
        ("case run dir", case.get("case_dir") or "-"),
        ("cmd_file exec path", case.get("exec_path") or "-"),
        ("log", case.get("log_path") or "-"),
        ("markers consistent",
         "no" if case.get("marker_inconsistent") else "yes"),
    ]
    body += "<h2>details</h2>" + table(
        ["field", "value"],
        [[esc(k), "<span class='muted'>%s</span>" % esc(v)] for k, v in facts])

    back = _q("run", run_id, "index", index_key, "case", case_id)
    body += "<h2>QA issues</h2>" + _case_issues(case, back)
    body += _log_section(log, case.get("log_path") or "", back)
    body += _files_section(files, back, case.get("case_dir") or "")

    return page("%s / %s" % (index_key, case_id), body, refresh=refresh,
                crumbs=[("/", "all runs"), (_q("run", run_id), run_id),
                        (_q("run", run_id, "index", index_key), index_key),
                        (_q("run", run_id, "index", index_key,
                            "case", case_id), case_id)])


def _lsf_fact(case: Dict[str, Any]) -> str:
    """What LSF says about this case, in words that mean what they say.

    Three different situations used to render as the same "- (job -)":
    a live job, a job that has gone, and a case no job was ever matched to.
    The third is the common one and the least alarming -- Arcx runs only so
    many cases at a time within an index, so a case waiting its turn has no
    LSF job yet -- and it must not read like the second.
    """
    job_id = case.get("lsf_job_id")
    state = case.get("lsf_state")
    if case.get("lsf_job_matched") and job_id:
        return "%s (job %s)" % (state or "?", job_id)
    if job_id:
        since = case.get("lsf_missing_since")
        gone = (" for %s" % duration(time.time() - since)) if since else ""
        return "job %s is no longer listed by bjobs%s" % (job_id, gone)
    return ("no LSF job has been matched to this case "
            "(it may still be waiting its turn inside Arcx)")


def _case_issues(case: Dict[str, Any], back: str = "/") -> str:
    issues = sorted(case.get("issues") or [],
                    key=lambda i: _SEVERITY_RANK.get(i["severity"], 9))
    if not issues:
        return "<div class='empty'>no problems found</div>"

    case_dir = case.get("case_dir") or ""
    blocks = []
    for issue in issues:
        evidence = ""
        if issue.get("evidence"):
            import json

            evidence = "<pre>%s</pre>" % esc(
                json.dumps(issue["evidence"], ensure_ascii=False, indent=2))
        blocks.append(
            "<div class='card' style='margin-bottom:10px'>"
            "<div>%s <strong>%s</strong> - %s</div>"
            "<div class='doc'>%s</div>%s%s</div>"
            % (severity_pill(issue["severity"]), esc(issue["id"]),
               esc(issue.get("message") or ""),
               esc(issue.get("doc") or ""), evidence,
               _evidence_links(issue, case_dir, back))
        )
    return "".join(blocks)


def _evidence_paths(evidence: Any) -> List[str]:
    """Every file an issue's evidence names, in the order it names them.

    A verdict like "the first line has no QuickCap wording" is only worth
    something if the next click is that first line. The evidence already
    carries the paths; this is what turns them from text into somewhere to go.
    """
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("path", "file", "log_path") and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(evidence)
    out = []
    for item in found:
        if item and item not in out:
            out.append(item)
    return out


def _evidence_links(issue: Dict[str, Any], case_dir: str, back: str) -> str:
    paths = _evidence_paths(issue.get("evidence"))
    if not paths or not case_dir:
        return ""
    links = []
    for relpath in paths[:8]:
        full = relpath if os.path.isabs(relpath) else os.path.join(
            case_dir, relpath)
        links.append("<a href='%s'>%s</a>"
                     % (esc(view_url(full, back=back)), esc(relpath)))
    return ("<div class='doc'>open: %s</div>"
            % " &middot; ".join(links))


# ---------------------------------------------------------------------------
# Looking at a file
# ---------------------------------------------------------------------------

def view_url(path: str, mode: str = "tail", lines: int = 200,
             back: str = "/") -> str:
    return "/view?%s" % urllib.parse.urlencode(
        {"path": path, "mode": mode, "lines": lines, "back": back})


def _log_section(log: Optional[Dict[str, Any]], log_path: str,
                 back: str) -> str:
    """The end of the log, on the page, without a trip to a terminal.

    This is the most repeated action in the whole review -- a case reads
    FAILED, and the next thing anybody does is tail its log -- and until now
    the tool answered it with a path to copy.
    """
    if not log_path:
        return "<h2>log</h2><div class='empty'>this case has no log yet</div>"
    header = ("<p class='muted'>%s &middot; "
              "<a href='%s'>last 500</a> &middot; "
              "<a href='%s'>first 200</a></p>"
              % (esc(log_path),
                 esc(view_url(log_path, "tail", 500, back)),
                 esc(view_url(log_path, "head", 200, back))))
    if log is None:
        return "<h2>log</h2>" + header
    if log.get("error"):
        return ("<h2>log</h2>%s<div class='empty'>could not read it: %s</div>"
                % (header, esc(log["error"])))
    if not log.get("text"):
        return ("<h2>log</h2>%s<div class='empty'>the log is empty</div>"
                % header)
    note = ""
    if log.get("truncated"):
        note = ("<div class='doc'>showing the last %d line(s); there is more "
                "above</div>" % log.get("lines_shown", 0))
    return "<h2>log</h2>%s%s<pre>%s</pre>" % (header, note, esc(log["text"]))


def _files_section(files: Sequence[Dict[str, Any]], back: str,
                   case_dir: str = "") -> str:
    """Listed even when empty, because empty is the finding.

    A case that carries a .complete marker and produced no file at all is the
    false success this whole system exists to catch, and "produced nothing" is
    the plainest way to show it.
    """
    if not files and not case_dir:
        return ""
    rows = []
    for entry in files:
        rows.append([
            "<a href='%s'>%s</a>" % (
                esc(view_url(entry["path"], back=back)), esc(entry["name"])),
            esc(size(entry.get("size"))),
        ])
    return "<h2>files this case produced</h2>" + table(
        ["file", "size"], rows, numeric=[1],
        empty="this case run dir contains no file at all")


def render_file_view(view: Dict[str, Any], back: str = "/") -> str:
    """One file, bounded. Never the whole thing: these are netlists."""
    path = view.get("path") or ""
    name = os.path.basename(path.rstrip("/")) or path
    mode = view.get("mode") or "tail"
    lines = view.get("lines_shown") or 0
    asked = view.get("lines_asked") or lines or 200

    body = ("<p><a href='%s'>&larr; back</a></p>" % esc(back or "/"))
    if view.get("error"):
        body += ("<div class='card alert'><div>could not read this file</div>"
                 "<div class='doc'>%s</div><div class='doc'>%s</div></div>"
                 % (esc(path), esc(view["error"])))
        return page(name, body, crumbs=[("/", "all runs")])

    body += cards([
        ("size", size(view.get("size")), ""),
        ("modified", timestamp(view.get("mtime")), ""),
        ("showing", "%s %d line(s)" % (mode, lines), ""),
    ])
    choices = []
    for label, choice_mode, count in (("last 100", "tail", 100),
                                      ("last 500", "tail", 500),
                                      ("last 2000", "tail", 2000),
                                      ("first 200", "head", 200),
                                      ("first 2000", "head", 2000)):
        choices.append("<a class='pill muted' href='%s'>%s</a>"
                       % (esc(view_url(path, choice_mode, count, back)),
                          esc(label)))
    body += ("<p style='display:flex;gap:6px;flex-wrap:wrap'>%s</p>"
             % "".join(choices))
    body += "<p class='muted'>%s</p>" % esc(path)
    if view.get("truncated"):
        body += ("<div class='doc'>this is a window, not the whole file; "
                 "there is more %s</div>"
                 % ("above" if mode == "tail" else "below"))
    body += "<pre>%s</pre>" % esc(view.get("text") or "(empty)")
    return page(name, body, crumbs=[
        ("/", "all runs"), (back or "/", "back"),
        (view_url(path, mode, asked, back), name)])
