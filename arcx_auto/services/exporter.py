"""Publish this user's status to the shared disk.

This is what makes a single-user local tool useful to a team. Nobody else runs
the daemon, nobody else needs an account, and nothing here is a service: one
user's daemon writes files, and everybody else opens them off a mounted share.

Three rules, all of them consequences of "somebody may be reading right now":

  * **Every write is tmp -> os.replace.** Half an HTML file renders as a blank
    page rather than as an error, which is the worst way to fail.
  * **Derived data only.** The truth is the run folders and ~/.arcx-auto. This
    whole tree can be deleted and the next export rebuilds it, so nothing here
    is ever recovered from, only overwritten.
  * **An export failure is never fatal.** The share can be unmounted, full, or
    read-only, and none of that is a reason to stop monitoring. Errors are
    collected and reported, never raised.

Layout (architecture 9.6):

    <shared_root>/
      index.html            every user, built from the status.json files
      <user>/
        status.json         the full data
        status.html         one self-contained page
        updated_at          a plain timestamp, for `cat` and for scripts
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import ExportSettings, Settings
from arcx_auto.util.atomic import atomic_write_json, atomic_write_text, read_json
from arcx_auto.web.export_page import render_export, render_shared_index

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ExportResult:
    """What one export produced, and anything that went wrong doing it."""

    ok: bool = True
    path: str = ""
    runs: int = 0
    users: int = 0
    errors: tuple = ()
    skipped: bool = False          # too soon since the last one

    @property
    def summary(self) -> str:
        if self.skipped:
            return "skipped; not due yet"
        if not self.ok:
            return "failed: %s" % "; ".join(self.errors)
        return "exported %d run(s) to %s" % (self.runs, self.path)


class Exporter:
    """Writes the shared-disk view of one user's runs."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        user: Optional[str] = None,
        host: Optional[str] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.export: ExportSettings = self.settings.export
        self.user = user or _current_user()
        self.host = host or socket.gethostname()
        self._last_export_at = 0.0

    # ------------------------------------------------------------------

    @property
    def shared_root(self) -> str:
        return os.path.abspath(os.path.expanduser(self.export.shared_root))

    @property
    def user_dir(self) -> str:
        return os.path.join(self.shared_root, self.user)

    def due(self, now: Optional[float] = None) -> bool:
        """Whether enough time has passed since the last export.

        The export interval is deliberately independent of the daemon tick: the
        daemon polls every 30 seconds while cases are running, and rewriting
        files on a shared NFS mount that often is rude to everybody mounting it.
        """
        now = now if now is not None else time.time()
        return (now - self._last_export_at) >= self.export.interval_sec

    def export_now(
        self,
        now: Optional[float] = None,
        force: bool = False,
    ) -> ExportResult:
        """Read every run this user has and publish them.

        Runs are read from the state root rather than taken from the caller, so
        one daemon publishes the user's whole picture -- including runs it is
        not itself monitoring, and finished ones, which is what makes the
        history section possible.
        """
        now = now if now is not None else time.time()
        if not force and not self.due(now):
            return ExportResult(skipped=True, path=self.user_dir)

        payload = self.build_payload(now)
        errors: List[str] = []

        try:
            os.makedirs(self.user_dir, exist_ok=True)
            _chmod(self.user_dir, self.export.dir_mode)
        except OSError as exc:
            return ExportResult(ok=False, path=self.user_dir,
                                errors=("cannot create %s: %s"
                                        % (self.user_dir, exc),))

        mode = self.export.file_mode
        for name, writer in (
            ("status.json", lambda p: atomic_write_json(p, payload, mode)),
            ("status.html",
             lambda p: atomic_write_text(p, render_export(payload), mode)),
            ("updated_at",
             lambda p: atomic_write_text(
                 p, time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(now)) + "\n", mode)),
        ):
            try:
                writer(os.path.join(self.user_dir, name))
            except OSError as exc:
                errors.append("%s: %s" % (name, exc))

        index_errors = self.refresh_shared_index(now)
        errors.extend(index_errors)

        # Only a fully successful export counts as one, or a share that is
        # briefly unwritable would silently push the next attempt an interval
        # into the future.
        if not errors:
            self._last_export_at = now

        return ExportResult(
            ok=not errors,
            path=self.user_dir,
            runs=len(payload.get("runs") or []),
            errors=tuple(errors),
        )

    # ------------------------------------------------------------------

    def build_payload(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Everything about this user, as plain data.

        status.json is a published interface: somebody will parse it in a shell
        script long before they ask us to add a feature, so it is the same
        shape as the daemon's state.json with a wrapper around it rather than a
        second format to learn.
        """
        now = now if now is not None else time.time()
        state_root = self.settings.expanded_state_root()

        runs: List[Dict[str, Any]] = []
        for run_id in RunStore.list_runs(state_root)[:self.export.history_limit]:
            state = RunStore(state_root, run_id).read_state()
            if not state:
                continue
            state.setdefault("run_id", run_id)
            runs.append(state)

        return {
            "schema_version": SCHEMA_VERSION,
            "user": self.user,
            "host": self.host,
            "generated_at": now,
            "totals": _totals(runs),
            "runs": runs,
        }

    def refresh_shared_index(self, now: Optional[float] = None) -> List[str]:
        """Rebuild the root page from every user's status.json.

        Any user's daemon may write it. Each user owns only their own
        directory, so this file is the single point they overlap -- and since
        it is derived, a lost race costs nothing: the next export rewrites it.
        A share where we cannot write the root is still perfectly usable by
        opening a user's own page, so failing here is reported, not raised.
        """
        now = now if now is not None else time.time()
        users: List[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(self.shared_root))
        except OSError as exc:
            return ["cannot list %s: %s" % (self.shared_root, exc)]

        for name in names:
            status = os.path.join(self.shared_root, name, "status.json")
            data = read_json(status, default=None)
            if not isinstance(data, dict):
                continue
            users.append({
                "user": data.get("user") or name,
                "host": data.get("host"),
                "generated_at": data.get("generated_at"),
                "totals": data.get("totals") or {},
            })

        try:
            atomic_write_text(
                os.path.join(self.shared_root, "index.html"),
                render_shared_index(users, generated_at=now),
                self.export.file_mode,
            )
        except OSError as exc:
            return ["index.html: %s" % exc]
        return []


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _totals(runs: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Totals across every run, so the root page needs no second pass."""
    cases = done = attention = 0
    for run in runs:
        for index in run.get("indexes") or []:
            counts = index.get("counts") or {}
            cases += sum(counts.values())
            done += counts.get("DONE", 0)
            attention += index.get("attention") or 0
    return {"runs": len(runs), "cases": cases, "done": done,
            "attention": attention}


def _chmod(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # A share that will not take a chmod is still a share we can write to.
        pass


def _current_user() -> str:
    for key in ("USER", "LOGNAME", "USERNAME"):
        value = os.environ.get(key)
        if value:
            return value
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:                      # pragma: no cover - very unusual
        return "unknown"
