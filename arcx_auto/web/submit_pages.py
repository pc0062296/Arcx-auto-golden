"""The pages somebody actually operates: building a submission, and reruns.

Everything before this file renders a view of what happened. These render
**forms**, which means for the first time the browser can ask for something --
and the whole design of that is one sentence: the browser only ever posts an
intent, and the daemon is still the only writer.

Three rules the layout follows:

**A destructive button never appears without what it will do.** The rerun
confirmation lists the exact directories that will be moved aside, read from
the plan that will actually run, not from a description of it.

**Checks are shown before the button, not after the failure.** The preflight
page is the gate: FATAL means the submit button is not rendered at all, rather
than rendered and refused, because a button you are allowed to press and then
told off for is worse than no button.

**Plain forms.** No framework, no fetch, no build step -- a POST and a redirect,
the way the web worked before it needed a toolchain. The small amount of inline
JavaScript is convenience only (select all, live counts); every action works
with it disabled.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from arcx_auto.web.html import CSS, cards, esc, page, severity_pill, table

_SELECT_JS = """
function arcxToggleAll(box) {
  var form = box.form;
  var boxes = form.querySelectorAll("input[name='index_keys']");
  for (var i = 0; i < boxes.length; i++) { boxes[i].checked = box.checked; }
  arcxCount(form);
}
function arcxCount(form) {
  var boxes = form.querySelectorAll("input[name='index_keys']:checked");
  var out = form.querySelector(".arcx-count");
  if (out) { out.textContent = boxes.length + " selected"; }
}
"""


def render_new(drafts: Sequence[Any], error: str = "") -> str:
    """Start a submission, or carry on with one already begun."""
    rows = []
    for draft in drafts:
        rows.append([
            '<a href="/submit/%s">%s</a>' % (esc(draft.id), esc(draft.run_id)),
            str(len(draft.groups)),
            str(draft.total_indices),
            esc(_when(draft.updated_at)),
        ])

    existing = (table(["draft", "groups", "indices", "updated"], rows)
                if rows else
                "<div class='empty'>no drafts in progress</div>")

    return _page("new submission", """
%s
<h2>start a submission</h2>
<form method="post" action="/submit/new">
  <p class='doc'>A submission is made of groups. One group is a dir_map, an
  arcx.cfg, and the indices you tick. Add another group if some indices need a
  different cfg.</p>
  <button type="submit">new submission</button>
</form>
<h2>in progress</h2>
%s
""" % (_error(error), existing))


def render_draft(draft: Any, error: str = "", notice: str = "") -> str:
    """The submission being built: its groups, and how to add one."""
    group_rows = []
    for position, group in enumerate(draft.groups, start=1):
        group_rows.append([
            str(position),
            esc(group.name),
            "<span class='muted'>%s</span>" % esc(group.dir_map),
            "<span class='muted'>%s</span>" % esc(group.arcx_cfg),
            str(len(group.index_keys)),
            esc(", ".join(group.index_keys[:12])
                + (" +%d" % (len(group.index_keys) - 12)
                   if len(group.index_keys) > 12 else "")),
            '<form method="post" action="/submit/%s/drop" style="margin:0">'
            '<input type="hidden" name="group" value="%d">'
            '<button type="submit" class="link">remove</button></form>'
            % (esc(draft.id), position - 1),
        ])

    groups = (table(
        ["#", "group", "dir_map", "arcx.cfg", "indices", "which", ""],
        group_rows)
        if group_rows else
        "<div class='empty'>no group yet -- add one below</div>")

    ready = ""
    if draft.groups:
        ready = """
<h2>check and submit</h2>
<form method="post" action="/submit/%s/check">
  <label>run id <input name="run_id" value="%s" size="30"></label>
  <label>slot cap <input name="max_slots" value="%s" size="6"></label>
  <label>batching
    <select name="mode">
      <option value="auto"%s>auto (split by slot cap)</option>
      <option value="off"%s>off (one wave)</option>
    </select>
  </label>
  <button type="submit">run the pre-submission checks</button>
  <p class='doc'>Nothing is created or submitted by this. It plans the waves
  and runs every check, and shows you the result.</p>
</form>
""" % (esc(draft.id), esc(draft.run_id),
       esc(str(draft.max_slots or "")),
       " selected" if draft.mode == "auto" else "",
       " selected" if draft.mode == "off" else "")

    return _page("submission %s" % draft.run_id, """
%s%s
<h2>groups</h2>
%s
<h2>add a group</h2>
<form method="post" action="/submit/%s/browse">
  <p><label>dir_map <input name="dir_map" size="70" placeholder="/path/to/dir_map" required></label></p>
  <p><label>arcx.cfg <input name="arcx_cfg" size="70" placeholder="/path/to/arcx.cfg" required></label></p>
  <button type="submit">read the dir_map</button>
  <p class='doc'>Both files are read, not copied. You pick the indices on the
  next page.</p>
</form>
%s
""" % (_error(error), _notice(notice), groups, esc(draft.id), ready))


def render_browse(draft: Any, dir_map_path: str, arcx_cfg: str,
                  entries: Sequence[Dict[str, Any]],
                  warnings: Sequence[str] = (),
                  error: str = "") -> str:
    """Tick the indices for one group.

    Each row shows what the index will cost -- GDS count and slot demand --
    because the slot cap only means something next to the number it caps.
    """
    rows = []
    for entry in entries:
        key = entry["index_key"]
        disabled = " disabled" if entry.get("error") else ""
        note = (("<span class='bad'>%s</span>" % esc(entry["error"]))
                if entry.get("error") else
                "<span class='muted'>%s</span>" % esc(entry.get("path", "")))
        rows.append([
            "<input type='checkbox' name='index_keys' value='%s'%s "
            "onchange='arcxCount(this.form)'>" % (esc(key), disabled),
            esc(key),
            str(entry.get("gds_count") or 0),
            str(entry.get("cpu_per_case") or 0),
            str(entry.get("slots") or 0),
            note,
        ])

    warn = ""
    if warnings:
        warn = "<h3>while reading</h3><ul class='doc'>%s</ul>" % "".join(
            "<li>%s</li>" % esc(w) for w in warnings)

    return _page("select indices", """
%s
<h2>%s</h2>
<p class='doc'>dir_map: %s<br>arcx.cfg: %s</p>
%s
<form method="post" action="/submit/%s/add">
  <input type="hidden" name="dir_map" value="%s">
  <input type="hidden" name="arcx_cfg" value="%s">
  <p>
    <label><input type="checkbox" onchange="arcxToggleAll(this)"> select all</label>
    &nbsp;<span class="arcx-count muted">0 selected</span>
  </p>
  %s
  <p><label>group name <input name="name" value="%s" size="24"></label></p>
  <button type="submit">add this group</button>
</form>
<p><a href="/submit/%s">back to the submission</a></p>
""" % (_error(error), esc(os.path.basename(dir_map_path)),
       esc(dir_map_path), esc(arcx_cfg), warn, esc(draft.id),
       esc(dir_map_path), esc(arcx_cfg),
       table(["", "index", "GDS", "cpu/case", "slots", "path"], rows)
       if rows else "<div class='empty'>this dir_map has no usable index</div>",
       esc("group_%d" % (len(draft.groups) + 1)), esc(draft.id)))


def render_preflight(draft: Any, plan: Any, cfg_result: Any,
                     preflight_result: Any, run_dir: str) -> str:
    """The gate. A FATAL means the submit button is not rendered at all."""
    issues = list(getattr(cfg_result, "issues", ()) or ()) + \
        list(getattr(preflight_result, "issues", ()) or ())
    fatal = [i for i in issues if i.severity.value == "FATAL"]
    blocking = [i for i in issues if i.blocks_success]

    wave_rows = []
    for wave in plan.waves:
        wave_rows.append([
            esc(wave.name),
            esc(wave.group or "-"),
            str(len(wave.indices)),
            str(wave.total_cases),
            str(wave.total_slots),
            esc(", ".join(wave.index_keys[:10])),
        ])

    issue_rows = []
    for issue in sorted(issues, key=lambda i: _rank(i.severity.value)):
        issue_rows.append([
            severity_pill(issue.severity.value),
            esc(issue.id),
            esc(issue.message),
        ])

    if fatal:
        action = (
            "<div class='card alert'><div class='n bad'>blocked</div>"
            "<div class='l'>%d check(s) must pass before this can be "
            "submitted</div></div>"
            "<p><a href='/submit/%s'>go back and fix it</a></p>"
            % (len(fatal), esc(draft.id)))
    else:
        action = """
<form method="post" action="/submit/%s/go">
  <input type="hidden" name="confirm" value="%s">
  <button type="submit" class="danger">submit %d wave(s)</button>
  <p class='doc'>This creates the wave directories and sends the jobs. The
  daemon does the work, so this page returns immediately and the run appears
  on the monitoring page.</p>
</form>
""" % (esc(draft.id), esc(draft.id), len(plan.waves))

    warn_note = ""
    if blocking and not fatal:
        warn_note = ("<p class='doc warn'>%d check(s) could not be confirmed. "
                     "They do not block, but read them first.</p>"
                     % len(blocking))

    return _page("pre-submission checks", """
<h2>pre-submission checks</h2>
%s
%s
<h2>what would be submitted</h2>
<p class='doc'>run id: %s<br>into: %s</p>
%s
<h2>%s</h2>
%s
""" % (
        cards([("waves", len(plan.waves), ""),
               ("cases", plan.total_cases, ""),
               ("slots", plan.total_slots, ""),
               ("fatal", len(fatal), "bad" if fatal else "good")]),
        warn_note,
        esc(draft.run_id), esc(run_dir),
        table(["wave", "group", "indices", "cases", "slots", "which"],
              wave_rows),
        "checks" if issue_rows else "checks: all clear",
        table(["severity", "id", "what"], issue_rows) if issue_rows
        else "<div class='empty good'>every check passed</div>",
    ) + action)


def render_rerun_confirm(run_id: str, wave_dir: str, plan: Any,
                         back: str = "/") -> str:
    """Never offer a destructive button without showing what it will touch.

    The list is read from the plan that will actually run, so what is on the
    screen is what happens -- and the ids are posted back with the request, so
    if the wave moves on in between, the executor refuses rather than acting on
    a different set.
    """
    rows = []
    for decision in plan.decisions:
        rows.append([
            esc(decision.case_id),
            esc(decision.index_key),
            "<span class='bad'>move aside and rerun</span>" if decision.delete
            else "<span class='good'>keep</span>",
            esc(decision.reason),
        ])

    if plan.blockers:
        body = ("<h2>this wave cannot be rerun</h2><ul class='doc'>%s</ul>"
                % "".join("<li>%s</li>" % esc(b) for b in plan.blockers))
        return _page("rerun blocked",
                     body + "<p><a href='%s'>back</a></p>" % esc(back))

    if not plan.to_delete:
        return _page("nothing to rerun",
                     "<h2>nothing needs rerunning</h2>"
                     "<div class='empty good'>every case in this wave is "
                     "complete.</div>"
                     "<p><a href='%s'>back</a></p>" % esc(back))

    hidden = "".join(
        '<input type="hidden" name="expect_delete" value="%s">'
        % esc(d.case_id) for d in plan.to_delete)

    warnings = ""
    if plan.warnings:
        warnings = "<ul class='doc'>%s</ul>" % "".join(
            "<li>%s</li>" % esc(w) for w in plan.warnings)

    return _page("confirm rerun", """
<h2>rerun %s</h2>
<p class='doc'>%s</p>
%s
%s
<h2>what will happen</h2>
<ol class='doc'>
  <li>stop the parent Arcx job</li>
  <li>drain every LSF job under this wave, and confirm it is quiet</li>
  <li>move the %d run dir(s) below into .arcx_auto/attempts/%d/ -- they are
      moved, not deleted, so the evidence survives</li>
  <li>resubmit with -keep_dir, so the finished cases are not redone</li>
</ol>
<form method="post" action="/rerun">
  <input type="hidden" name="run_id" value="%s">
  <input type="hidden" name="wave_dir" value="%s">
  %s
  <button type="submit" class="danger">yes, rerun %d case(s)</button>
  <a href="%s" class="cancel">cancel</a>
</form>
""" % (esc(os.path.basename(wave_dir)), esc(wave_dir), warnings,
       table(["case", "index", "decision", "why"], rows),
       len(plan.to_delete), plan.attempt,
       esc(run_id), esc(wave_dir), hidden, len(plan.to_delete), esc(back)))


def render_queued(command: Any, back: str = "/") -> str:
    """What happened after a button: the request is queued, not finished."""
    return _page("queued", """
<h2>queued</h2>
<div class='card'><div class='n'>%s</div><div class='l'>%s</div></div>
<p class='doc'>The daemon picks this up on its next tick and does the work.
Submissions can wait at the gate for the LSF quota, so this is deliberately not
something your browser sits and waits for.</p>
<p><a href="/commands">watch the queue</a> &middot;
   <a href="%s">back</a></p>
""" % (esc(command.kind), esc(command.id), esc(back)))


def render_commands(pending: Sequence[Any], running: Sequence[Any],
                    done: Sequence[Any], daemon_note: str = "") -> str:
    """The queue, so a request that went nowhere is visible."""
    def rows_for(commands, show_result=False):
        rows = []
        for command in commands:
            outcome = ""
            if show_result:
                if command.ok:
                    outcome = "<span class='good'>ok</span>"
                elif command.ok is False:
                    outcome = "<span class='bad'>%s</span>" % esc(
                        (command.error or "failed").splitlines()[0][:120])
            rows.append([
                esc(command.kind),
                esc(command.id),
                esc(command.requested_by),
                esc(_when(command.created_at)),
                outcome,
            ])
        return rows

    note = ("<p class='doc bad'>%s</p>" % esc(daemon_note)) if daemon_note else ""

    return _page("command queue", """
%s
<h2>waiting (%d)</h2>
%s
<h2>running (%d)</h2>
%s
<h2>finished</h2>
%s
""" % (
        note,
        len(pending),
        table(["kind", "id", "by", "asked at", ""], rows_for(pending))
        if pending else "<div class='empty'>nothing waiting</div>",
        len(running),
        table(["kind", "id", "by", "asked at", ""], rows_for(running))
        if running else "<div class='empty'>nothing running</div>",
        table(["kind", "id", "by", "asked at", "result"],
              rows_for(done, show_result=True))
        if done else "<div class='empty'>nothing finished yet</div>",
    ))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_EXTRA_CSS = """
form { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; margin:8px 0; }
label { display:inline-block; margin:4px 12px 4px 0; color:var(--dim);
  font-size:13px; }
input[type=text], input:not([type]), select { background:var(--bg);
  color:var(--fg); border:1px solid var(--line); border-radius:5px;
  padding:5px 8px; font:13px ui-monospace,Menlo,Consolas,monospace; }
button { background:var(--busy); color:#fff; border:0; border-radius:6px;
  padding:7px 14px; font:13px inherit; cursor:pointer; }
button:hover { filter:brightness(1.1); }
button.danger { background:var(--bad); }
button.link { background:none; color:var(--busy); padding:0; }
a.cancel { margin-left:14px; color:var(--dim); }
ol.doc, ul.doc { margin:6px 0 6px 20px; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>%s%s</style>"
        "<script>%s</script></head><body>"
        "<header><h1><a href='/'>arcx-auto</a> / %s</h1>"
        "<span class='meta'><a href='/submit'>new submission</a></span>"
        "<span class='meta'><a href='/commands'>queue</a></span>"
        "</header><main>%s</main></body></html>"
        % (esc(title), CSS, _EXTRA_CSS, _SELECT_JS, esc(title), body)
    )


def _error(message: str) -> str:
    if not message:
        return ""
    return ("<div class='card alert'><div class='n bad'>error</div>"
            "<div class='l'>%s</div></div>" % esc(message))


def _notice(message: str) -> str:
    if not message:
        return ""
    return "<p class='doc good'>%s</p>" % esc(message)


def _rank(severity: str) -> int:
    return {"FATAL": 0, "UNKNOWN": 1, "WARN": 2, "INFO": 3}.get(severity, 4)


def _when(value: Optional[float]) -> str:
    import time

    if not value:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(value))
