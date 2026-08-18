"""The local web server. Standard library http.server, bound to 127.0.0.1.

Why not FastAPI or Flask: this is a single user, read only dashboard listening
on loopback. The standard library is enough, and installing packages on an air
gapped network is real friction. Zero dependencies also means "copy the source
across and it runs".

Security properties:
  * bound to 127.0.0.1 by default, so it serves nobody else and needs no auth
  * every route is a GET; there are no write endpoints (write actions will be
    posted through commands/ files)
  * it only reads state.json under the state root, and the path comes from
    looking the run_id up in the existing runs rather than from concatenation,
    so there is no room for path traversal
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from arcx_auto.adapters.store import RunStore
from arcx_auto.web import pages


@dataclass
class WebOptions:
    state_root: str
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_sec: int = 30


class _Handler(BaseHTTPRequestHandler):
    options: WebOptions = WebOptions(state_root="~/.arcx-auto")
    server_version = "arcx-auto"

    # -- Routing -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        path = urllib.parse.urlparse(self.path).path
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p]

        try:
            if not parts:
                return self._home()
            if parts[0] == "api" and len(parts) == 3 and parts[1] == "state":
                return self._api_state(parts[2])
            if parts[0] == "healthz":
                return self._send_json({"ok": True})
            if parts[0] == "run":
                return self._run_routes(parts[1:])
        except Exception as exc:  # noqa: BLE001 - a bad request must not take
            # the whole server down
            return self._error(500, "internal error: %s" % exc)

        self._error(404, "no such page")

    def _run_routes(self, parts: List[str]) -> None:
        if not parts:
            return self._error(404, "missing run id")
        run_id = parts[0]
        state = self._load_state(run_id)
        if state is None:
            return self._error(404, "no such run: %s" % run_id)

        if len(parts) == 1:
            return self._html(pages.render_run(state, self.options.refresh_sec))

        if len(parts) >= 3 and parts[1] == "index":
            index = _find(state.get("indexes") or [], "index_key", parts[2])
            if index is None:
                return self._error(404, "no such index: %s" % parts[2])

            if len(parts) == 3:
                return self._html(
                    pages.render_index(state, index, self.options.refresh_sec))

            if len(parts) == 5 and parts[3] == "case":
                case = _find(index.get("cases") or [], "case_id", parts[4])
                if case is None:
                    return self._error(404, "no such case: %s" % parts[4])
                return self._html(pages.render_case(
                    state, index, case, self.options.refresh_sec))

        self._error(404, "no such page")

    def _home(self) -> None:
        states = []
        for run_id in RunStore.list_runs(self.options.state_root):
            state = self._load_state(run_id)
            if state:
                state.setdefault("run_id", run_id)
                states.append(state)
        self._html(pages.render_home(states, self.options.refresh_sec))

    def _api_state(self, run_id: str) -> None:
        state = self._load_state(run_id)
        if state is None:
            return self._error(404, "no such run: %s" % run_id)
        self._send_json(state)

    # -- Data ----------------------------------------------------------

    def _load_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Accept only run ids that actually exist under the state root.

        Listing the existing runs and matching, rather than concatenating user
        input into a path, removes path traversal at the root.
        """
        if run_id not in RunStore.list_runs(self.options.state_root):
            return None
        state = RunStore(self.options.state_root, run_id).read_state()
        return state or None

    # -- Responses -----------------------------------------------------

    def _html(self, body: str, code: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: Any, code: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2,
                             default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, code: int, message: str) -> None:
        body = pages.page(
            "%d" % code,
            "<h2>%d</h2><div class='empty'>%s</div>"
            "<p><a href='/'>back to the overview</a></p>"
            % (code, pages.esc(message)),
        )
        self._html(body, code=code)

    def log_message(self, fmt: str, *args: Any) -> None:
        """The default logs every request to stderr; with a 30 second refresh
        that is just noise.
        """
        return


def _find(items: List[Dict[str, Any]], key: str,
          value: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if item.get(key) == value:
            return item
    return None


def serve(options: WebOptions, ready: Optional[Any] = None) -> None:
    """Start the server (blocking). ``ready`` is a threading.Event for tests."""
    _Handler.options = options
    httpd = ThreadingHTTPServer((options.host, options.port), _Handler)
    httpd.daemon_threads = True
    if ready is not None:
        ready.port = httpd.server_address[1]
        ready.httpd = httpd
        ready.set()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
