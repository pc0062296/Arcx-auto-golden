"""The local web server. Standard library http.server, bound to 127.0.0.1.

Why not FastAPI or Flask: this is a single user, read only dashboard listening
on loopback. The standard library is enough, and installing packages on an air
gapped network is real friction. Zero dependencies also means "copy the source
across and it runs".

Security properties:
  * bound to 127.0.0.1 by default, so it serves nobody else and needs no auth
  * **the server never acts.** POST routes write an intent into the command
    queue and return; the daemon, already the single writer, does the work.
    Two writers on one run folder is the failure this whole system exists to
    avoid, and "the UI only writes when the daemon is not looking" is not an
    invariant anybody can keep
  * every POST checks the Origin header. Binding to loopback stops the network
    reaching us, but it does **not** stop a page open in the same browser from
    posting here -- any site can submit a form to 127.0.0.1. Without this check
    an open tab could trigger a rerun that deletes directories
  * it only reads state.json under the state root, and the path comes from
    looking the run_id up in the existing runs rather than from concatenation,
    so there is no room for path traversal
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import Settings
from arcx_auto.web import pages, submit_pages


@dataclass
class WebOptions:
    state_root: str
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_sec: int = 30
    #: Settings, needed once the UI can queue work. Loaded lazily so the
    #: read-only paths keep working with nothing configured.
    settings: Optional[Any] = None
    #: Allow POST routes. Turning this off makes the UI purely a display.
    allow_actions: bool = True

    def resolved_settings(self):
        if self.settings is None:
            self.settings = Settings()
        return self.settings


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
            if parts[0] == "submit":
                return self._submit_get(parts[1:])
            if parts[0] == "commands":
                return self._commands_page()
            if parts[0] == "rerun":
                return self._rerun_confirm()
        except Exception as exc:  # noqa: BLE001 - a bad request must not take
            # the whole server down
            return self._error(500, "internal error: %s" % exc)

        self._error(404, "no such page")

    # -- Actions -------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        """Every POST writes an intent and returns. None of them do the work.

        The Origin check is the one thing standing between an open browser tab
        on any site and a button here that deletes directories: loopback stops
        the network from reaching us, but not a page in the same browser from
        posting to us.
        """
        if not self.options.allow_actions:
            return self._error(403, "this server is running read only")
        if not self._origin_ok():
            return self._error(
                403,
                "this request did not come from this page. Actions are only "
                "accepted from the arcx-auto UI itself.")

        path = urllib.parse.urlparse(self.path).path
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p]
        form = self._read_form()

        try:
            if parts == ["submit", "new"]:
                return self._submit_new()
            if len(parts) == 3 and parts[0] == "submit":
                return self._submit_action(parts[1], parts[2], form)
            if parts == ["rerun"]:
                return self._rerun_go(form)
        except Exception as exc:  # noqa: BLE001 - see do_GET
            import traceback

            traceback.print_exc()
            return self._error(500, "internal error: %s" % exc)

        self._error(404, "no such action")

    def _origin_ok(self) -> bool:
        """Accept only same-origin form posts.

        A missing Origin is accepted: some browsers omit it for same-origin
        form submissions, and refusing those would break the UI for the person
        it is meant to serve. A *present and different* Origin is what a
        cross-site post looks like, and that is refused.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlparse(origin)
        except ValueError:
            return False
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return int(port) == int(self.server.server_address[1])

    def _read_form(self) -> Dict[str, List[str]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        # A form is small. A body far larger than any real one is somebody
        # doing something else, and reading it would just tie up the process.
        if length <= 0 or length > 1 << 20:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

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

    # -- Submission flow -----------------------------------------------

    def _drafts(self):
        from arcx_auto.services.drafts import DraftStore

        return DraftStore(self.options.state_root)

    def _queue(self):
        from arcx_auto.services.commands import CommandQueue

        return CommandQueue(self.options.state_root)

    def _submit_get(self, parts: List[str]) -> None:
        store = self._drafts()
        if not parts:
            return self._html(submit_pages.render_new(store.list()))

        draft = store.load(parts[0])
        if draft is None:
            return self._error(404, "no such draft")
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query)
        return self._html(submit_pages.render_draft(
            draft,
            error=(query.get("error") or [""])[0],
            notice=(query.get("notice") or [""])[0]))

    def _submit_new(self) -> None:
        draft = self._drafts().create()
        self._redirect("/submit/%s" % draft.id)

    def _submit_action(self, draft_id: str, action: str,
                       form: Dict[str, List[str]]) -> None:
        store = self._drafts()
        draft = store.load(draft_id)
        if draft is None:
            return self._error(404, "no such draft")

        if action == "browse":
            return self._submit_browse(draft, form)
        if action == "add":
            return self._submit_add(store, draft, form)
        if action == "drop":
            return self._submit_drop(store, draft, form)
        if action == "check":
            return self._submit_check(store, draft, form)
        if action == "go":
            return self._submit_go(store, draft, form)
        self._error(404, "no such action")

    def _submit_browse(self, draft, form: Dict[str, List[str]]) -> None:
        """Read a dir_map and show what each index would cost."""
        from arcx_auto.adapters.arcx import ArcxAdapter
        from arcx_auto.adapters.fs import FsAdapter

        dir_map = _first(form, "dir_map")
        arcx_cfg = _first(form, "arcx_cfg")
        settings = self.options.resolved_settings()

        for path, label in ((dir_map, "dir_map"), (arcx_cfg, "arcx.cfg")):
            if not os.path.isfile(os.path.expanduser(path)):
                return self._html(submit_pages.render_draft(
                    draft, error="%s not found: %s" % (label, path)))

        fs = FsAdapter(settings.layout)
        arcx = ArcxAdapter(settings.layout, settings.plan, fs=fs)
        parsed = arcx.parse_dir_map(os.path.expanduser(dir_map))

        entries = []
        for key, path in sorted(parsed.entries.items(),
                                key=lambda kv: _natural(kv[0])):
            spec = arcx.build_index_spec(key, path)
            entries.append({
                "index_key": key, "path": path,
                "gds_count": spec.gds_count,
                "cpu_per_case": spec.cpu_per_case,
                "slots": spec.slots,
                "error": spec.error or "",
            })

        self._html(submit_pages.render_browse(
            draft, os.path.expanduser(dir_map), os.path.expanduser(arcx_cfg),
            entries, warnings=parsed.warnings))

    def _submit_add(self, store, draft, form: Dict[str, List[str]]) -> None:
        from arcx_auto.services.drafts import DraftGroup

        keys = form.get("index_keys") or []
        if not keys:
            return self._redirect(
                "/submit/%s?error=%s" % (draft.id,
                                         urllib.parse.quote("no index ticked")))
        draft.groups.append(DraftGroup(
            name=_first(form, "name") or "group_%d" % (len(draft.groups) + 1),
            dir_map=os.path.expanduser(_first(form, "dir_map")),
            arcx_cfg=os.path.expanduser(_first(form, "arcx_cfg")),
            index_keys=list(keys),
        ))
        store.save(draft)
        self._redirect("/submit/%s?notice=%s" % (
            draft.id, urllib.parse.quote(
                "added %d index/indices" % len(keys))))

    def _submit_drop(self, store, draft, form: Dict[str, List[str]]) -> None:
        try:
            position = int(_first(form, "group"))
        except ValueError:
            return self._error(400, "which group?")
        if 0 <= position < len(draft.groups):
            draft.groups.pop(position)
            store.save(draft)
        self._redirect("/submit/%s" % draft.id)

    def _submit_check(self, store, draft, form: Dict[str, List[str]]) -> None:
        """Plan and check. Creates nothing and submits nothing."""
        draft.run_id = _first(form, "run_id") or draft.run_id
        draft.mode = _first(form, "mode") or "auto"
        raw_slots = _first(form, "max_slots")
        draft.max_slots = int(raw_slots) if raw_slots.isdigit() else None
        store.save(draft)

        try:
            plan, cfg_result, pre_result, run_dir = self._plan_draft(draft)
        except ValueError as exc:
            return self._html(submit_pages.render_draft(draft, error=str(exc)))

        self._html(submit_pages.render_preflight(
            draft, plan, cfg_result, pre_result, run_dir))

    def _plan_draft(self, draft):
        """Turn a draft into a plan plus its check results.

        The same code path the executor will take, so what the page shows is
        what would actually be submitted rather than a description of it.
        """
        from arcx_auto.domain.enums import PlanMode
        from arcx_auto.services.executor import CommandExecutor, IntentError
        from arcx_auto.services.qa import QaRunner
        from arcx_auto.services.wave_planner import plan_groups

        settings = self.options.resolved_settings()
        executor = CommandExecutor(settings)
        payload = {"groups": [g.as_dict() for g in draft.groups]}
        try:
            groups = executor._read_groups(payload)
            specs = executor._build_specs(groups)
        except IntentError as exc:
            raise ValueError(str(exc))

        plan = plan_groups(
            specs,
            max_slots_per_wave=draft.max_slots or settings.plan.max_slots_per_wave,
            mode=PlanMode.AUTO if draft.mode == "auto" else PlanMode.OFF,
        )
        run_root = settings.expanded_run_root()
        run_dir = os.path.join(run_root, draft.run_id)

        from arcx_auto.adapters.arcx_cfg import parse_arcx_cfg

        from arcx_auto.adapters.lsf import LsfAdapter

        qa = QaRunner(settings)
        cfg = parse_arcx_cfg(draft.groups[0].arcx_cfg) if draft.groups else None
        cfg_result = qa.run_config(cfg)
        # The LSF adapter has to be passed, or the page runs a *different*
        # preflight from the executor: it showed "every check passed" and the
        # submission was then refused for an unreachable bsub. A button you are
        # allowed to press and then told off for is worse than no button.
        pre_result = qa.run_preflight(plan, run_dir, arcx_config=cfg,
                                      lsf=LsfAdapter(settings.lsf),
                                      run_root=run_root)
        return plan, cfg_result, pre_result, run_dir

    def _submit_go(self, store, draft, form: Dict[str, List[str]]) -> None:
        if _first(form, "confirm") != draft.id:
            return self._error(400, "the confirmation did not match")
        command = self._queue().submit("submit", {
            "run_id": draft.run_id,
            "mode": draft.mode,
            "max_slots": draft.max_slots,
            "groups": [g.as_dict() for g in draft.groups],
        })
        store.delete(draft.id)
        self._html(submit_pages.render_queued(command))

    # -- Rerun ---------------------------------------------------------

    def _rerun_confirm(self) -> None:
        """Show exactly which directories a rerun would move aside."""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        wave_dir = (query.get("wave_dir") or [""])[0]
        run_id = (query.get("run_id") or [""])[0]
        back = (query.get("back") or ["/"])[0]
        if not os.path.isdir(wave_dir):
            return self._error(404, "no such wave directory: %s" % wave_dir)

        from arcx_auto.services.monitor import MonitorService
        from arcx_auto.services.rerun_planner import build_rerun_plan
        from arcx_auto.util.atomic import read_json

        settings = self.options.resolved_settings()
        result = MonitorService(settings).scan(wave_dirs=[wave_dir],
                                               use_lsf=False)
        meta = os.path.join(wave_dir, ".arcx_auto")
        plan = build_rerun_plan(
            wave_dir=wave_dir,
            wave_name=os.path.basename(wave_dir),
            snapshots=result.snapshots,
            qa_reports=result.qa_reports,
            launch=read_json(os.path.join(meta, "launch.json"), default={}) or {},
            manifest=read_json(os.path.join(meta, "manifest.json"),
                               default={}) or {},
        )
        self._html(submit_pages.render_rerun_confirm(
            run_id or os.path.basename(wave_dir), wave_dir, plan, back=back))

    def _rerun_go(self, form: Dict[str, List[str]]) -> None:
        wave_dir = _first(form, "wave_dir")
        if not os.path.isdir(wave_dir):
            return self._error(400, "no such wave directory")
        command = self._queue().submit("rerun", {
            "wave_dir": wave_dir,
            "run_id": _first(form, "run_id"),
            # What the page showed. If the wave moves on before the daemon
            # picks this up, the executor refuses rather than acting on a
            # different set of cases.
            "expect_delete": form.get("expect_delete") or [],
        })
        self._html(submit_pages.render_queued(command, back="/"))

    # -- Queue ---------------------------------------------------------

    def _commands_page(self) -> None:
        from arcx_auto.services.commands import DONE, PENDING, RUNNING

        queue = self._queue()
        pending = queue.list(PENDING)
        note = ""
        if pending:
            note = ("%d request(s) are waiting. If they stay here, no daemon "
                    "is running to pick them up." % len(pending))
        self._html(submit_pages.render_commands(
            pending, queue.list(RUNNING), queue.list(DONE, limit=25),
            daemon_note=note))

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

    def _redirect(self, location: str) -> None:
        """Post/redirect/get, so a refresh never repeats an action."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

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


def _first(form: Dict[str, List[str]], key: str) -> str:
    values = form.get(key) or []
    return values[0].strip() if values else ""


def _natural(text: str):
    """Sort index keys the way people read them: 2 before 10."""
    return tuple(int(p) if p.isdigit() else p
                 for p in re.split(r"(\d+)", text))


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
