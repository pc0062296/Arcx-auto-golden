"""HTML generation. Pure functions: data in, string out.

No template engine and no frontend build chain -- installing things on an air
gapped network is friction, and this dashboard is not complex enough to justify
a dependency. The CSS is inlined and nothing is fetched externally.
"""

from __future__ import annotations

import html
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Colour semantics: warm for things needing attention, blue for in flight,
# green for finished
_STATE_CLASS = {
    "FAILED": "bad", "LOST": "bad", "STALLED": "bad",
    "SUSPENDED": "warn", "UNKNOWN": "warn", "COMPLETED_MARKER": "warn",
    "RUNNING": "busy", "QUEUED": "busy", "PENDING": "muted",
    "DONE": "good",
}
_SEVERITY_CLASS = {
    "FATAL": "bad", "UNKNOWN": "warn", "WARN": "warn", "INFO": "muted",
}

CSS = """
:root {
  --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e6e9ef; --dim:#8b93a7;
  --good:#3fb950; --bad:#f85149; --warn:#d29922; --busy:#58a6ff;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#f6f7f9; --panel:#fff; --line:#e1e4e8; --fg:#1f2328; --dim:#656d76; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5
  ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
a { color:var(--busy); text-decoration:none; }
a:hover { text-decoration:underline; }
header { padding:14px 20px; border-bottom:1px solid var(--line);
  display:flex; gap:16px; align-items:baseline; flex-wrap:wrap; }
header h1 { margin:0; font-size:16px; font-weight:600; }
header .meta { color:var(--dim); font-size:12px; }
main { padding:20px; max-width:1400px; }
h2 { font-size:14px; margin:24px 0 10px; font-weight:600;
  color:var(--dim); text-transform:uppercase; letter-spacing:.06em; }
h2:first-child { margin-top:0; }
.cards { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:8px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:12px 16px; min-width:110px; }
.card .n { font-size:24px; font-weight:600; }
.card .l { color:var(--dim); font-size:12px; }
.card.alert { border-color:var(--bad); }
.card.alert .n { color:var(--bad); }
table { border-collapse:collapse; width:100%; background:var(--panel);
  border:1px solid var(--line); border-radius:8px; overflow:hidden; }
th,td { text-align:left; padding:7px 12px; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { color:var(--dim); font-weight:600; font-size:12px;
  text-transform:uppercase; letter-spacing:.04em; }
tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; }
.good { color:var(--good); } .bad { color:var(--bad); }
.warn { color:var(--warn); } .busy { color:var(--busy); } .muted { color:var(--dim); }
.pill { display:inline-block; padding:1px 8px; border-radius:99px;
  font-size:12px; border:1px solid currentColor; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:6px;
  padding:10px 12px; overflow-x:auto; margin:6px 0; font-size:12px; }
.empty { color:var(--dim); padding:12px; }
.doc { color:var(--dim); font-size:12px; white-space:pre-wrap; margin:4px 0 0; }
.bar { display:flex; height:6px; border-radius:99px; overflow:hidden;
  background:var(--line); min-width:120px; }
.bar span { display:block; }
.bar.big { height:14px; border-radius:6px; }
.legend { display:flex; gap:14px; flex-wrap:wrap; margin:6px 0 0;
  font-size:12px; color:var(--dim); }
.legend b { font-weight:600; }
.key { display:inline-block; width:9px; height:9px; border-radius:2px;
  margin-right:5px; vertical-align:baseline; }
button.link { background:none; border:none; padding:0; font:inherit;
  color:var(--busy); cursor:pointer; }
button.link:hover { text-decoration:underline; }
"""

#: Keeping the page still while it refreshes.
#:
#: A monitoring page has to update itself -- that is what it is for. But a
#: plain meta refresh throws away everything the reader was doing: the scroll
#: position, and every section they had opened. Reviewing a run means opening
#: a few things and reading, and being yanked back to the top every thirty
#: seconds makes that impossible.
#:
#: So the reload is driven from here instead, and the two pieces of state that
#: belong to the reader -- where they were, and what they had open -- are
#: saved before it and put back after. There is also a switch: sometimes the
#: honest answer is to stop refreshing until they are done.
#:
#: sessionStorage can throw (private windows, blocked site data), so every
#: access is wrapped. Failing means the page refreshes the old way, which is
#: the behaviour this replaces, not a broken page.
_KEEP_PLACE_JS = """
(function () {
  var SECONDS = %d;
  var PLACE = "arcx:place:" + location.pathname + location.search;
  var PAUSED = "arcx:paused";
  var timer = null;

  function store() { try { return window.sessionStorage; } catch (e) { return null; } }
  function get(key) { var s = store(); try { return s && s.getItem(key); } catch (e) { return null; } }
  function set(key, value) { var s = store(); try { s && s.setItem(key, value); } catch (e) {} }

  function sections() { return document.querySelectorAll("details[data-key]"); }

  function save() {
    var open = {}, all = sections();
    for (var i = 0; i < all.length; i++) {
      open[all[i].getAttribute("data-key")] = all[i].open ? 1 : 0;
    }
    set(PLACE, JSON.stringify({ y: window.pageYOffset || 0, open: open }));
  }

  function restore() {
    var raw = get(PLACE);
    if (!raw) { return; }
    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    var open = data.open || {}, all = sections();
    for (var i = 0; i < all.length; i++) {
      var key = all[i].getAttribute("data-key");
      // Only sections that existed when we saved: anything new keeps
      // whatever the server decided, which is usually "open, because
      // something in here needs a person".
      if (open[key] !== undefined) { all[i].open = !!open[key]; }
    }
    if (data.y) { window.scrollTo(0, data.y); }
  }

  function paused() { return get(PAUSED) === "1"; }

  function label() {
    var button = document.getElementById("arcx-refresh");
    if (button) {
      button.textContent = paused()
        ? "auto refresh: off" : "auto refresh: " + SECONDS + "s";
    }
  }

  function schedule() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (paused() || SECONDS <= 0) { return; }
    timer = setTimeout(function () { save(); location.reload(); }, SECONDS * 1000);
  }

  function wire() {
    restore();
    label();
    schedule();
    var all = sections();
    for (var i = 0; i < all.length; i++) {
      all[i].addEventListener("toggle", save);
    }
    var button = document.getElementById("arcx-refresh");
    if (button) {
      button.addEventListener("click", function () {
        set(PAUSED, paused() ? "0" : "1");
        label();
        schedule();
      });
    }
    var now = document.getElementById("arcx-refresh-now");
    if (now) {
      now.addEventListener("click", function () { save(); location.reload(); });
    }
    window.addEventListener("beforeunload", save);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, refresh: int = 0,
         crumbs: Sequence[Tuple[str, str]] = (), meta: str = "") -> str:
    """One page of the served UI.

    Every page carries the same two links. The monitoring pages and the
    submission pages were built at different times and were not connected at
    all: somebody opening the dashboard had no way to reach the thing that
    starts a run, short of typing the URL. A tool whose main action is
    unreachable from its front page does not have that action.
    """
    # Without JavaScript there is no way to put the reader back where they
    # were, so the old behaviour is the fallback rather than no refresh at all.
    refresh_tag = ('<noscript><meta http-equiv="refresh" content="%d">'
                   '</noscript>' % refresh if refresh > 0 else "")
    script = ("<script>%s</script>" % (_KEEP_PLACE_JS % refresh)
              if refresh > 0 else "")
    controls = ""
    if refresh > 0:
        controls = (
            "<span class='meta'><button class='link' id='arcx-refresh'>"
            "auto refresh: %ds</button></span>"
            "<span class='meta'><button class='link' id='arcx-refresh-now'>"
            "refresh now</button></span>" % refresh)
    trail = " / ".join(
        '<a href="%s">%s</a>' % (esc(href), esc(text)) for href, text in crumbs)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "%s<title>%s</title><style>%s</style></head><body>"
        "<header><h1>%s</h1>"
        "<span class='meta'><a href='/submit'>new submission</a></span>"
        "<span class='meta'><a href='/commands'>queue</a></span>"
        "%s"
        "<span class='meta'>%s</span>"
        "<span class='meta'>%s</span></header><main>%s</main>%s</body></html>"
        % (refresh_tag, esc(title), CSS, trail or esc(title), controls,
           esc(meta), esc("updated " + time.strftime("%H:%M:%S")), body,
           script)
    )


def cards(items: Sequence[Tuple[str, Any, str]]) -> str:
    """A row of stat cards, each (label, value, css class)."""
    if not items:
        return ""
    html_parts = []
    for label, value, klass in items:
        html_parts.append(
            "<div class='card %s'><div class='n %s'>%s</div>"
            "<div class='l'>%s</div></div>"
            % (esc(klass), esc(klass if klass != "alert" else "bad"),
               esc(value), esc(label))
        )
    return "<div class='cards'>%s</div>" % "".join(html_parts)


def table(headers: Sequence[str], rows: Sequence[Sequence[str]],
          numeric: Sequence[int] = (), empty: str = "no data",
          header_html: Sequence[str] = ()) -> str:
    """Row cells are already HTML; the caller is responsible for esc().

    ``header_html`` replaces the escaped headers with markup the caller has
    built -- a column heading that is a sort link, and nothing else so far.
    It must line up one-to-one with ``headers``, which stays the plain-text
    version.
    """
    if not rows:
        return "<div class='empty'>%s</div>" % esc(empty)
    labels = list(header_html) if header_html else [esc(h) for h in headers]
    head = "".join(
        "<th%s>%s</th>" % (" class='num'" if i in numeric else "", label)
        for i, label in enumerate(labels))
    body = []
    for row in rows:
        cells = "".join(
            "<td%s>%s</td>" % (" class='num'" if i in numeric else "", cell)
            for i, cell in enumerate(row))
        body.append("<tr>%s</tr>" % cells)
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
        head, "".join(body))


def state_pill(state: str) -> str:
    return "<span class='pill %s'>%s</span>" % (
        _STATE_CLASS.get(state, "muted"), esc(state))


def severity_pill(severity: str) -> str:
    return "<span class='pill %s'>%s</span>" % (
        _SEVERITY_CLASS.get(severity, "muted"), esc(severity))


#: The order states appear in a progress bar, and their colour. Finished
#: first, then in flight, then the ones that need somebody -- so the bar reads
#: left to right as "how much is done" without anybody decoding it.
STATE_COLORS: Tuple[Tuple[str, str], ...] = (
    ("DONE", "var(--good)"), ("COMPLETED_MARKER", "var(--warn)"),
    ("RUNNING", "var(--busy)"), ("QUEUED", "var(--busy)"),
    ("PENDING", "var(--line)"), ("STALLED", "var(--bad)"),
    ("SUSPENDED", "var(--warn)"), ("LOST", "var(--bad)"),
    ("FAILED", "var(--bad)"), ("UNKNOWN", "var(--warn)"),
)


def progress_bar(counts: Dict[str, int], big: bool = False) -> str:
    """A proportional bar of the state mix."""
    total = sum(counts.values())
    if not total:
        return ""
    segments = []
    for state, color in STATE_COLORS:
        count = counts.get(state, 0)
        if count:
            segments.append(
                "<span style='width:%.2f%%;background:%s' title='%s %d'></span>"
                % (100.0 * count / total, color, esc(state), count))
    return "<div class='bar%s'>%s</div>" % (" big" if big else "",
                                            "".join(segments))


def progress_legend(counts: Dict[str, int]) -> str:
    """The numbers behind the bar.

    A bar shows proportion and hides quantity: "nearly done" reads the same
    whether two cases are left or two hundred. Both are wanted, so both are
    shown, in the same order and the same colours as the bar.
    """
    total = sum(counts.values())
    if not total:
        return ""
    parts = []
    for state, color in STATE_COLORS:
        count = counts.get(state, 0)
        if count:
            parts.append(
                "<span><span class='key' style='background:%s'></span>"
                "%s <b>%d</b></span>" % (color, esc(state), count))
    for state in sorted(counts):
        if state not in dict(STATE_COLORS) and counts[state]:
            parts.append("<span>%s <b>%d</b></span>"
                         % (esc(state), counts[state]))
    parts.append("<span>total <b>%d</b></span>" % total)
    return "<div class='legend'>%s</div>" % "".join(parts)


def duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%ds" % int(seconds)
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd%02dh" % (seconds // 86400, (seconds % 86400) // 3600)


def size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "-"
    value = float(num_bytes)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return "%.0f%s" % (value, unit)
        value /= 1024.0
    return "%.0fG" % value


def timestamp(value: Optional[float]) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
