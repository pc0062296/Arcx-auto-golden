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
            if parts[0] == "pick" and len(parts) == 3:
                return self._pick_page(parts[1], parts[2])
            if parts[0] == "view":
                return self._view_page()
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
            if len(parts) == 3 and parts[0] == "pick":
                return self._pick_set(parts[1], parts[2], form)
            if parts == ["commands", "cancel"]:
                self._queue().cancel(_first(form, "id"))
                return self._redirect("/commands")
            if parts == ["submit", "discard"]:
                self._drafts().delete(_first(form, "id"))
                return self._redirect("/submit")
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
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            return self._html(pages.render_run(
                state, self.options.refresh_sec,
                view=(query.get("view") or [""])[0],
                show=(query.get("show") or [""])[0],
                sort=(query.get("sort") or [""])[0],
                direction=(query.get("dir") or [""])[0]))

        if len(parts) >= 3 and parts[1] == "index":
            index = _find(state.get("indexes") or [], "index_key", parts[2])
            if index is None:
                return self._error(404, "no such index: %s" % parts[2])

            if len(parts) == 3:
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                return self._html(pages.render_index(
                    state, index, self.options.refresh_sec,
                    show=(query.get("show") or [""])[0]))

            if len(parts) == 5 and parts[3] == "case":
                case = _find(index.get("cases") or [], "case_id", parts[4])
                if case is None:
                    return self._error(404, "no such case: %s" % parts[4])
                log, files = self._case_files(case)
                return self._html(pages.render_case(
                    state, index, case, self.options.refresh_sec,
                    log=log, files=files))

        self._error(404, "no such page")

    def _home(self) -> None:
        states = []
        for run_id in RunStore.list_runs(self.options.state_root):
            state = self._load_state(run_id)
            if state:
                state.setdefault("run_id", run_id)
                states.append(state)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self._html(pages.render_home(
            states, self.options.refresh_sec,
            workspaces=self._workspaces(),
            sort=(query.get("sort") or [""])[0],
            direction=(query.get("dir") or [""])[0]))

    def _api_state(self, run_id: str) -> None:
        state = self._load_state(run_id)
        if state is None:
            return self._error(404, "no such run: %s" % run_id)
        self._send_json(state)

    # -- Looking at a file ---------------------------------------------

    def _view_roots(self) -> List[str]:
        """Where the viewer is allowed to read.

        The configured run_root, plus the wave directory of every run the
        daemon has actually recorded. The second half matters because a run
        may have been started with an explicit wave dir outside run_root, and
        a viewer that refuses to open the log of a run it is displaying is
        worse than useless. Both halves come from configuration or from the
        daemon's own state -- never from the request.
        """
        roots: List[str] = []
        settings = self.options.resolved_settings()
        run_root = settings.expanded_run_root()
        if run_root:
            roots.append(run_root)
        for run_id in RunStore.list_runs(self.options.state_root):
            state = self._load_state(run_id)
            for index in (state or {}).get("indexes") or []:
                folder = (index.get("run_folder") or "").rstrip("/")
                if folder:
                    roots.append(os.path.dirname(folder))
        out: List[str] = []
        for root in roots:
            if root and root not in out:
                out.append(root)
        return out

    def _view_page(self) -> None:
        from arcx_auto.services.fileview import read_view, within

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        path = (query.get("path") or [""])[0]
        back = (query.get("back") or ["/"])[0]
        mode = (query.get("mode") or ["tail"])[0]
        mode = mode if mode in ("tail", "head") else "tail"
        try:
            lines = int((query.get("lines") or ["200"])[0])
        except ValueError:
            lines = 200
        lines = max(1, min(lines, 5000))

        if not path:
            return self._error(400, "no file given")
        if not back.startswith("/"):
            # An absolute URL here would turn a "back" link into an open
            # redirect. Only somewhere on this server is a valid destination.
            back = "/"
        if not within(path, self._view_roots()):
            return self._error(
                403,
                "this file is outside the run directories, so it is not "
                "shown here: %s" % path)
        view = read_view(path, mode=mode, lines=lines)
        self._html(pages.render_file_view(view.as_dict(), back=back))

    def _case_files(self, case: Dict[str, Any]):
        """The tail of the log and the files the case produced.

        Read here rather than in the page functions: the UI modules render
        what they are handed and import no service (architecture decision 1).
        """
        from arcx_auto.services.fileview import list_case_files, read_view

        log = None
        log_path = case.get("log_path")
        if log_path:
            # A tighter byte budget than the full viewer gets: one log line
            # can be megabytes, and this one is only the preview.
            log = read_view(log_path, mode="tail", lines=40,
                            max_bytes=32 * 1024).as_dict()
        case_dir = case.get("case_dir")
        files = []
        if case_dir:
            files = [entry.as_dict() for entry in list_case_files(case_dir)]
        return log, files

    # -- Submission flow -----------------------------------------------

    def _drafts(self):
        from arcx_auto.services.drafts import DraftStore

        return DraftStore(self.options.state_root)

    def _queue(self):
        from arcx_auto.services.commands import CommandQueue

        return CommandQueue(self.options.state_root)

    def _workspaces(self):
        from arcx_auto.services import workspaces

        return workspaces.list_workspaces(self.options.state_root)

    def _draft_run_root(self, draft) -> str:
        """Which run_root this draft submits into, deciding it if nobody has.

        A workspace is a directory somebody is working in, and the web server
        is started in exactly one of them. Defaulting to the server's own
        run_root is right only while there is one workspace; the moment there
        are two it silently sends the work to the wrong disk. So: if exactly
        one daemon is alive, that is the answer and nobody is asked. If
        several are, the draft page asks, and the answer is kept on the draft.
        """
        if draft.run_root:
            return draft.run_root
        from arcx_auto.services import workspaces

        alive = workspaces.live_workspaces(self.options.state_root)
        mine = self.options.resolved_settings().expanded_run_root()
        if len(alive) == 1:
            return alive[0].run_root
        if any(w.run_root == mine for w in alive):
            # `arcx-auto start` in one project directory and bare daemons in
            # the others: the UI's own root is one of the workspaces, and it
            # is the one the person is looking at.
            return mine
        # Several workspaces and none of them ours. Rather than picking one --
        # the wrong guess sends the work to another project's disk -- leave it
        # on a root nobody is watching, which the page says out loud and asks
        # about. A default that looks plausible and is wrong is worse than one
        # that is obviously unfinished.
        return mine

    def _submit_get(self, parts: List[str]) -> None:
        store = self._drafts()
        if not parts:
            return self._html(submit_pages.render_new(store.list()))

        draft = store.load(parts[0])
        if draft is None:
            return self._error(404, "no such draft")
        if len(parts) == 2 and parts[1] == "pending":
            return self._submit_pending(draft)
        if len(parts) == 2 and parts[1] == "auto":
            return self._auto_pick(draft)
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query)
        return self._html(submit_pages.render_draft(
            draft,
            error=(query.get("error") or [""])[0],
            notice=(query.get("notice") or [""])[0],
            workspaces=self._workspaces(),
            run_root=self._draft_run_root(draft)))

    def _submit_new(self) -> None:
        """Start a submission already pointed at a workspace.

        The picker opening in the directory the daemon is working in is worth
        more than it sounds: with one workspace per project, the alternative
        is clicking up out of the web server's own directory and back down
        again on every single submission.
        """
        from arcx_auto.services import workspaces

        store = self._drafts()
        draft = store.create()
        draft.run_root = self._draft_run_root(draft)
        found = workspaces.find(self.options.state_root, draft.run_root)
        if found is not None and os.path.isdir(found.cwd):
            draft.last_dir = found.cwd
        store.save(draft)
        self._redirect("/submit/%s" % draft.id)

    def _submit_action(self, draft_id: str, action: str,
                       form: Dict[str, List[str]]) -> None:
        store = self._drafts()
        draft = store.load(draft_id)
        if draft is None:
            return self._error(404, "no such draft")

        if action == "browse":
            return self._submit_browse(draft, form)
        if action == "pending":
            return self._submit_pending(draft)
        if action == "add":
            return self._submit_add(store, draft, form)
        if action == "drop":
            return self._submit_drop(store, draft, form)
        if action == "folders":
            return self._submit_folders(store, draft, form)
        if action == "workspace":
            return self._submit_workspace(store, draft, form)
        if action == "autoplan":
            return self._auto_plan(draft, form)
        if action == "autoadd":
            return self._auto_add(store, draft, form)
        if action == "check":
            return self._submit_check(store, draft, form)
        if action == "go":
            return self._submit_go(store, draft, form)
        self._error(404, "no such action")

    def _pick_page(self, draft_id: str, field: str) -> None:
        """Click through the filesystem for one of the two files."""
        from arcx_auto.services.browse import list_dir, suggest
        from arcx_auto.web import submit_pages as sp

        store = self._drafts()
        draft = store.load(draft_id)
        if draft is None:
            return self._error(404, "no such draft")
        if field not in ("dir_map", "arcx_cfg"):
            return self._error(404, "nothing to pick there")

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        path = (query.get("path") or [""])[0]
        if not path:
            # Reopen where they were. Starting at home every time makes the
            # picker slower than the text box it replaced.
            path = draft.last_dir or self._draft_run_root(draft)
            if not os.path.isdir(os.path.expanduser(path)):
                # A workspace whose run_root does not exist yet is normal --
                # nothing has been submitted into it. The directory it lives
                # in does exist, and that is where the files are.
                path = os.path.dirname(os.path.expanduser(path).rstrip("/"))
            if not os.path.isdir(os.path.expanduser(path or "")):
                path = os.path.expanduser("~")

        listing = list_dir(path)
        if not listing.error:
            draft.last_dir = listing.path
            store.save(draft)

        kind = "dir_map" if field == "dir_map" else "arcx_cfg"
        self._html(sp.render_file_picker(
            listing, field, draft.id,
            suggestions=suggest(listing.path, kind),
            current=draft.pending))

    def _pick_set(self, draft_id: str, field: str,
                  form: Dict[str, List[str]]) -> None:
        """Record one chosen file, then ask for the other or read the dir_map.

        Held on the draft rather than passed through the URL, so choosing the
        second file cannot lose the first.
        """
        store = self._drafts()
        draft = store.load(draft_id)
        if draft is None:
            return self._error(404, "no such draft")
        if field not in ("dir_map", "arcx_cfg"):
            return self._error(404, "nothing to pick there")

        value = os.path.expanduser(_first(form, "value"))
        if not os.path.isfile(value):
            return self._redirect("/pick/%s/%s?error=1" % (draft.id, field))

        draft.pending[field] = value
        draft.last_dir = os.path.dirname(value)
        store.save(draft)

        other = "arcx_cfg" if field == "dir_map" else "dir_map"
        if not draft.pending.get(other):
            return self._redirect("/pick/%s/%s" % (draft.id, other))
        return self._redirect("/submit/%s/pending" % draft.id)

    def _submit_pending(self, draft) -> None:
        """Both files chosen: go straight on to ticking indices."""
        form = {"dir_map": [draft.pending.get("dir_map", "")],
                "arcx_cfg": [draft.pending.get("arcx_cfg", "")]}
        return self._submit_browse(draft, form)

    # -- Auto grouping -------------------------------------------------

    def _auto_pick(self, draft) -> None:
        """Choose the directory to group from."""
        from arcx_auto.services.browse import list_dir

        settings = self.options.resolved_settings()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        path = (query.get("path") or [""])[0]
        if not path:
            path = draft.last_dir or self._draft_run_root(draft)
            if not os.path.isdir(os.path.expanduser(path)):
                path = os.path.dirname(os.path.expanduser(path).rstrip("/"))
            if not os.path.isdir(os.path.expanduser(path or "")):
                path = os.path.expanduser("~")

        listing = list_dir(path)
        if not listing.error:
            store = self._drafts()
            draft.last_dir = listing.path
            store.save(draft)
        has_dir_map = os.path.isfile(os.path.join(
            listing.path, settings.auto_group.dir_map_name))
        self._html(submit_pages.render_auto_pick(
            listing, draft.id, has_dir_map=has_dir_map))

    def _auto_plan(self, draft, form: Dict[str, List[str]]) -> None:
        """Read one directory and show what it would be grouped into."""
        from arcx_auto.services.autogroup import scan_directory

        directory = os.path.expanduser(_first(form, "directory"))
        plan = scan_directory(directory, self.options.resolved_settings())
        if not plan.ok:
            from arcx_auto.services.browse import list_dir

            listing = list_dir(directory)
            return self._html(submit_pages.render_auto_pick(
                listing, draft.id, has_dir_map=False, error=plan.error))
        self._html(submit_pages.render_auto_plan(draft, plan))

    def _auto_add(self, store, draft, form: Dict[str, List[str]]) -> None:
        """Turn the proposal, as edited, into groups on the draft.

        The directory is read again rather than trusted from the form: what
        comes back is a set of index keys and cfg paths, and both have to be
        ones this directory actually offers. A cfg path taken at face value
        would let a form field name any file on the disk as the cfg a wave
        runs against.
        """
        from arcx_auto.services.autogroup import scan_directory
        from arcx_auto.services.drafts import DraftGroup

        directory = os.path.expanduser(_first(form, "directory"))
        plan = scan_directory(directory, self.options.resolved_settings())
        if not plan.ok:
            return self._html(submit_pages.render_draft(
                draft, error=plan.error,
                workspaces=self._workspaces(),
                run_root=self._draft_run_root(draft)))

        known_cfgs = {c.path: c for c in plan.cfgs}
        known_keys = {a.index_key for a in plan.assignments}
        chosen = [k for k in (form.get("index_keys") or []) if k in known_keys]

        by_cfg: Dict[str, List[str]] = {}
        for key in chosen:
            cfg = _first(form, "cfg_%s" % key)
            if cfg not in known_cfgs:
                continue        # "skip", or something this directory has not
            by_cfg.setdefault(cfg, []).append(key)

        if not by_cfg:
            return self._html(submit_pages.render_auto_plan(
                draft, plan,
                error="nothing was selected, or every selected index was set "
                      "to skip"))

        added = 0
        for cfg in (c.path for c in plan.cfgs):
            keys = by_cfg.get(cfg)
            if not keys:
                continue
            draft.groups.append(DraftGroup(
                name=known_cfgs[cfg].suffix,
                dir_map=plan.dir_map,
                arcx_cfg=cfg,
                index_keys=keys,
            ))
            added += 1
        draft.pending = {}
        store.save(draft)
        self._redirect("/submit/%s?notice=%s" % (
            draft.id, urllib.parse.quote(
                "added %d group(s), %d index/indices"
                % (added, len(chosen)))))

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
        draft.pending = {}
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

    def _submit_workspace(self, store, draft,
                          form: Dict[str, List[str]]) -> None:
        """Send this submission to a different workspace."""
        from arcx_auto.services import workspaces

        chosen = os.path.abspath(os.path.expanduser(_first(form, "run_root")))
        if not chosen:
            return self._error(400, "no workspace given")
        known = {w.run_root for w in self._workspaces()}
        known.add(self.options.resolved_settings().expanded_run_root())
        if chosen not in known:
            # Only somewhere a daemon has actually registered. A free-text
            # run_root would let the UI create wave directories anywhere on
            # the disk, which is not a decision a form field should carry.
            return self._error(400, "not a known workspace: %s" % chosen)
        draft.run_root = chosen
        found = workspaces.find(self.options.state_root, chosen)
        if found is not None and os.path.isdir(found.cwd):
            draft.last_dir = found.cwd
        store.save(draft)
        self._redirect("/submit/%s?notice=%s" % (
            draft.id, urllib.parse.quote("workspace set to %s" % chosen)))

    def _submit_folders(self, store, draft,
                        form: Dict[str, List[str]]) -> None:
        """Turn folder grouping on or off for one group.

        Before the checks are run, because it changes what the waves are --
        which is the thing the checks page exists to show.
        """
        try:
            position = int(_first(form, "group"))
        except ValueError:
            return self._error(400, "which group?")
        if not 0 <= position < len(draft.groups):
            return self._error(404, "no such group")
        group = draft.groups[position]
        group.keep_folders_together = not group.keep_folders_together
        store.save(draft)
        self._redirect("/submit/%s?notice=%s" % (
            draft.id, urllib.parse.quote(
                "%s: folders are %s"
                % (group.name,
                   "kept together" if group.keep_folders_together
                   else "allowed to split across waves"))))

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

        from arcx_auto.services import workspaces

        run_root = self._draft_run_root(draft)
        found = workspaces.find(self.options.state_root, run_root)
        self._html(submit_pages.render_preflight(
            draft, plan, cfg_result, pre_result, run_dir,
            workspace=found, run_root=run_root))

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
        run_root = self._draft_run_root(draft)
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
            # The workspace travels with the request. Whichever daemon claims
            # it, the waves land in the run_root the person was looking at
            # when they pressed the button.
            "run_root": self._draft_run_root(draft),
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
