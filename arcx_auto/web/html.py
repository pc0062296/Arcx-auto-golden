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
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, refresh: int = 0,
         crumbs: Sequence[Tuple[str, str]] = (), meta: str = "") -> str:
    refresh_tag = ('<meta http-equiv="refresh" content="%d">' % refresh
                   if refresh > 0 else "")
    trail = " / ".join(
        '<a href="%s">%s</a>' % (esc(href), esc(text)) for href, text in crumbs)
    return (
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "%s<title>%s</title><style>%s</style></head><body>"
        "<header><h1>%s</h1><span class='meta'>%s</span>"
        "<span class='meta'>%s</span></header><main>%s</main></body></html>"
        % (refresh_tag, esc(title), CSS, trail or esc(title), esc(meta),
           esc("updated " + time.strftime("%H:%M:%S")), body)
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
          numeric: Sequence[int] = (), empty: str = "no data") -> str:
    """Row cells are already HTML; the caller is responsible for esc()."""
    if not rows:
        return "<div class='empty'>%s</div>" % esc(empty)
    head = "".join(
        "<th%s>%s</th>" % (" class='num'" if i in numeric else "", esc(h))
        for i, h in enumerate(headers))
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


def progress_bar(counts: Dict[str, int]) -> str:
    """A proportional bar of the state mix."""
    total = sum(counts.values())
    if not total:
        return ""
    order = [("DONE", "var(--good)"), ("COMPLETED_MARKER", "var(--warn)"),
             ("RUNNING", "var(--busy)"), ("QUEUED", "var(--busy)"),
             ("PENDING", "var(--line)"), ("STALLED", "var(--bad)"),
             ("SUSPENDED", "var(--warn)"), ("LOST", "var(--bad)"),
             ("FAILED", "var(--bad)"), ("UNKNOWN", "var(--warn)")]
    segments = []
    for state, color in order:
        count = counts.get(state, 0)
        if count:
            segments.append(
                "<span style='width:%.2f%%;background:%s' title='%s %d'></span>"
                % (100.0 * count / total, color, esc(state), count))
    return "<div class='bar'>%s</div>" % "".join(segments)


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
