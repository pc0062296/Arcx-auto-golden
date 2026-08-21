"""Workspaces: one run_root per project directory, and who owns which.

The point of these tests is the multi-directory case. One workspace works by
accident -- everything defaults to the only run_root there is. Two is where a
submission can silently land on the wrong disk.
"""

import os
import tempfile
import time
import unittest

from arcx_auto.config.settings import Settings
from arcx_auto.daemon import Daemon, DaemonOptions
from arcx_auto.services import workspaces


class WorkspaceIdTest(unittest.TestCase):
    def test_the_same_root_always_gets_the_same_id(self):
        """Stable across restarts: the daemon's run id used to be a
        timestamp, so restarting created a new run page and left the old one
        looking abandoned.
        """
        first = workspaces.workspace_id("/proj/chipA/arcx_runs")
        second = workspaces.workspace_id("/proj/chipA/arcx_runs/")
        self.assertEqual(first, second)

    def test_two_directories_get_two_ids(self):
        self.assertNotEqual(workspaces.workspace_id("/proj/a/arcx_runs"),
                            workspaces.workspace_id("/proj/b/arcx_runs"))

    def test_directories_with_the_same_name_do_not_collide(self):
        """Half the projects on a disk have a subdirectory called work."""
        self.assertNotEqual(workspaces.workspace_id("/proj/a/work/arcx_runs"),
                            workspaces.workspace_id("/proj/b/work/arcx_runs"))

    def test_the_id_is_readable(self):
        self.assertTrue(
            workspaces.workspace_id("/proj/chipA/arcx_runs").startswith(
                "chipA-"))

    def test_a_relative_root_resolves_before_it_is_named(self):
        here = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            relative = workspaces.workspace_id("./arcx_runs")
        finally:
            os.chdir(here)
        self.assertEqual(
            relative,
            workspaces.workspace_id(
                os.path.join(tempfile.gettempdir(), "arcx_runs")))


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")

    def test_a_registered_workspace_can_be_found_again(self):
        workspaces.register(self.state, "chipA-1", "/proj/chipA/arcx_runs")
        found = workspaces.find(self.state, "/proj/chipA/arcx_runs")
        self.assertIsNotNone(found)
        self.assertEqual(found.run_id, "chipA-1")
        self.assertTrue(found.alive())

    def test_several_workspaces_coexist(self):
        workspaces.register(self.state, "a", "/proj/a/arcx_runs")
        workspaces.register(self.state, "b", "/proj/b/arcx_runs")
        roots = {w.run_root for w in workspaces.list_workspaces(self.state)}
        self.assertEqual(roots, {"/proj/a/arcx_runs", "/proj/b/arcx_runs"})

    def test_a_stale_file_is_not_alive(self):
        """A daemon killed with -9 leaves its file behind. The UI must not
        keep offering a workspace nobody is serving.
        """
        old = time.time() - 10 * workspaces.STALE_AFTER_SEC
        workspaces.register(self.state, "a", "/proj/a/arcx_runs", now=old)
        self.assertEqual(workspaces.live_workspaces(self.state), [])
        self.assertEqual(len(workspaces.list_workspaces(self.state)), 1)

    def test_a_clean_stop_says_so_rather_than_going_stale(self):
        """Otherwise it stays 'alive' for fifteen minutes after shutdown."""
        entry = workspaces.register(self.state, "a", "/proj/a/arcx_runs")
        workspaces.mark_stopped(self.state, entry)
        self.assertEqual(workspaces.live_workspaces(self.state), [])
        self.assertFalse(workspaces.list_workspaces(self.state)[0].alive())

    def test_a_heartbeat_keeps_the_start_time(self):
        started = time.time() - 3600
        entry = workspaces.register(self.state, "a", "/proj/a/arcx_runs",
                                    now=started)
        again = workspaces.heartbeat(self.state, entry)
        self.assertEqual(again.started_at, started)
        self.assertGreater(again.updated_at, started)

    def test_a_run_id_cannot_escape_the_registry_directory(self):
        workspaces.register(self.state, "../../escape", "/proj/x/arcx_runs")
        listed = os.listdir(workspaces.registry_dir(self.state))
        self.assertEqual(listed, ["______escape.json"])

    def test_an_unreadable_entry_is_skipped_not_fatal(self):
        os.makedirs(workspaces.registry_dir(self.state), exist_ok=True)
        with open(os.path.join(workspaces.registry_dir(self.state),
                               "junk.json"), "w") as handle:
            handle.write("not json")
        workspaces.register(self.state, "a", "/proj/a/arcx_runs")
        self.assertEqual(len(workspaces.list_workspaces(self.state)), 1)


class DaemonRegistersTest(unittest.TestCase):
    """The daemon is the only thing that knows where it is."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings()
        self.settings.state_root = os.path.join(self.tmp.name, "state")
        self.settings.run_root = os.path.join(self.tmp.name, "runs")
        self.settings.export.enabled = False

    def run_once(self, run_id="ws-1"):
        Daemon(DaemonOptions(run_id=run_id, use_lsf=False, once=True),
               settings=self.settings).run()

    def test_a_daemon_registers_the_root_it_owns(self):
        self.run_once()
        found = workspaces.find(self.settings.expanded_state_root(),
                                self.settings.expanded_run_root())
        self.assertIsNotNone(found)
        self.assertEqual(found.run_id, "ws-1")
        self.assertEqual(found.pid, os.getpid())

    def test_a_daemon_that_finishes_says_it_has_stopped(self):
        self.run_once()
        found = workspaces.find(self.settings.expanded_state_root(),
                                self.settings.expanded_run_root())
        self.assertFalse(found.alive())


if __name__ == "__main__":
    unittest.main()
