"""Page content generation. Pure functions: state.json contents -> HTML.

Deliberately imports no service: the UI renders what the daemon already
computed. That is what lets the UI be closed, reopened, crash or be replaced
entirely without disturbing the daemon (architecture decision 1).
"""

from __future__ import annotations

import os
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

def render_home(states: Sequence[Dict[str, Any]], refresh: int) -> str:
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
    body += "<h2>runs</h2>"
    body += table(
        ["run", "progress", "cases", "index", "attention", "issues",
         "updated", "daemon"],
        rows, numeric=[2, 3, 4, 5],
        empty="no run is being monitored yet; start one with arcx-auto daemon",
    )
    return page("Arcx Auto Golden", body, refresh=refresh)


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

    rows = []
    for index in state.get("indexes") or []:
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
    body += "<h2>index</h2>" + table(
        ["index", "progress", "cases", "done", "attention", "scan anomalies",
         "run folder"],
        rows, numeric=[2, 3, 4])

    return page("run %s" % run_id, body, refresh=refresh,
                crumbs=[("/", "all runs"), (_q("run", run_id), run_id)],
                meta="updated %s" % timestamp(state.get("updated_at")))


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
                 refresh: int) -> str:
    run_id = state.get("run_id", "?")
    index_key = index.get("index_key", "?")
    counts = index.get("counts") or {}

    body = cards([(state_name, count,
                   "alert" if state_name in ("FAILED", "LOST", "STALLED") else "")
                  for state_name, count in sorted(counts.items())])
    body += rerun_link(state, index)

    rows = []
    for case in index.get("cases") or []:
        issue_ids = sorted({i["id"] for i in case.get("issues") or []})
        rows.append([
            "<a href='%s'>%s</a>" % (
                esc(_q("run", run_id, "index", index_key,
                       "case", case["case_id"])),
                esc(case["case_id"])),
            state_pill(case["state"]),
            esc(case.get("lsf_state") or "-"),
            esc(duration(case.get("in_state_sec"))),
            _silent_cell(case),
            esc(size(case.get("log_size"))),
            ", ".join(esc(i) for i in issue_ids) or "-",
            "<span class='muted'>%s</span>" % esc(case.get("note") or ""),
        ])

    body += "<h2>cases</h2>" + table(
        ["case", "state", "LSF", "in state", "log quiet", "log size",
         "issues", "note"],
        rows, numeric=[3, 4, 5])

    body += _anomaly_section(index)

    return page("%s / %s" % (run_id, index_key), body, refresh=refresh,
                crumbs=[("/", "all runs"), (_q("run", run_id), run_id),
                        (_q("run", run_id, "index", index_key), index_key)])


def rerun_link(state: Dict[str, Any], index: Dict[str, Any]) -> str:
    """Offer a rerun where the problem is visible, not on a separate page.

    Only when something is actually wrong: a button that is always there
    invites a rerun of a wave that did not need one, and a rerun is the one
    operation that moves directories.
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
        "&amp;back=%s\">rerun this wave...</a> "
        "<span class='muted'>shows what would be moved aside before "
        "anything happens</span></p>"
        % (urllib.parse.quote(wave_dir), urllib.parse.quote(run_id),
           urllib.parse.quote("/run/%s/index/%s"
                              % (run_id, index.get("index_key", ""))))
    )


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
                case: Dict[str, Any], refresh: int) -> str:
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
        ("LSF", "%s (job %s)" % (case.get("lsf_state") or "-",
                                 case.get("lsf_job_id") or "-")),
        ("case run dir", case.get("case_dir") or "-"),
        ("cmd_file exec path", case.get("exec_path") or "-"),
        ("log", case.get("log_path") or "-"),
        ("markers consistent",
         "no" if case.get("marker_inconsistent") else "yes"),
    ]
    body += "<h2>details</h2>" + table(
        ["field", "value"],
        [[esc(k), "<span class='muted'>%s</span>" % esc(v)] for k, v in facts])

    body += "<h2>QA issues</h2>" + _case_issues(case)

    return page("%s / %s" % (index_key, case_id), body, refresh=refresh,
                crumbs=[("/", "all runs"), (_q("run", run_id), run_id),
                        (_q("run", run_id, "index", index_key), index_key),
                        (_q("run", run_id, "index", index_key,
                            "case", case_id), case_id)])


def _case_issues(case: Dict[str, Any]) -> str:
    issues = sorted(case.get("issues") or [],
                    key=lambda i: _SEVERITY_RANK.get(i["severity"], 9))
    if not issues:
        return "<div class='empty'>no problems found</div>"

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
            "<div class='doc'>%s</div>%s</div>"
            % (severity_pill(issue["severity"]), esc(issue["id"]),
               esc(issue.get("message") or ""),
               esc(issue.get("doc") or ""), evidence)
        )
    return "".join(blocks)
