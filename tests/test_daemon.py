"""Daemon: persistence, restart recovery, error isolation, single instance."""

import json
import os
import tempfile
import threading
import time
import unittest

from arcx_auto.adapters.arcx_cfg import parse_arcx_cfg
from arcx_auto.adapters.lock import FileLock, LockBusy
from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import Settings
from arcx_auto.daemon import Daemon, DaemonOptions
from arcx_auto.services.monitor import MonitorService
from tests.fixtures.fake_run import build_demo


def settings_with_root(root):
    settings = Settings()
    settings.state_root = root
    return settings


class DaemonRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.demo = build_demo(os.path.join(self.tmp.name, "demo"))
        self.state_root = os.path.join(self.tmp.name, "state")
        self.settings = settings_with_root(self.state_root)

    def tearDown(self):
        self.tmp.cleanup()

    def _daemon(self, run_id="t", **kwargs):
        options = DaemonOptions(
            run_id=run_id,
            wave_dirs=[self.demo["wave_dir"]],
            use_lsf=False,
            once=True,
            **kwargs,
        )
        return Daemon(options, settings=self.settings)

    def _state(self, run_id="t"):
        return RunStore(self.state_root, run_id).read_state()

    def test_single_tick_writes_all_files(self):
        self.assertEqual(self._daemon().run(), 0)
        run_dir = RunStore(self.state_root, "t").dir
        for name in ("manifest.json", "state.json", "events.jsonl",
                     "audit.jsonl"):
            self.assertTrue(os.path.exists(os.path.join(run_dir, name)), name)

    def test_state_has_totals_and_indexes(self):
        self._daemon().run()
        state = self._state()
        self.assertEqual(state["totals"]["indexes"], 2)
        self.assertGreater(state["totals"]["cases"], 0)
        self.assertEqual({i["index_key"] for i in state["indexes"]},
                         {"1000", "1001"})

    def test_state_detects_fake_success(self):
        """The demo contains cases with a .complete marker but no artifacts."""
        self._daemon().run()
        ids = {i["id"] for i in self._state()["issues"]}
        self.assertIn("NETLIST_MISSING", ids)

    def test_manifest_is_immutable(self):
        """The manifest records the original intent and must not be rewritten."""
        self._daemon().run()
        store = RunStore(self.state_root, "t")
        first = store.read_manifest()
        time.sleep(0.01)
        self._daemon().run()
        self.assertEqual(store.read_manifest()["created_at"],
                         first["created_at"])

    def test_audit_records_start_and_stop(self):
        self._daemon().run()
        path = RunStore(self.state_root, "t").audit_path
        with open(path, encoding="utf-8") as handle:
            actions = [json.loads(line)["action"] for line in handle]
        self.assertIn("daemon_start", actions)
        self.assertIn("daemon_stop", actions)

    def test_events_recorded_on_first_scan(self):
        self._daemon().run()
        path = RunStore(self.state_root, "t").events_path
        with open(path, encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle]
        self.assertTrue(events)
        self.assertIn("to_state", events[0])

    def test_restart_restores_previous_snapshots(self):
        """A restart must resume the previous verdicts, or stall timers reset
        to zero every time.
        """
        self._daemon().run()
        second = self._daemon()
        second.run()
        self.assertEqual(len(second.monitor.previous), 2)

    def test_works_without_any_prior_state(self):
        """Losing state.json must still rebuild from the run folders: the
        filesystem is the truth.
        """
        self._daemon().run()
        os.remove(RunStore(self.state_root, "t").state_path)
        self.assertEqual(self._daemon().run(), 0)
        self.assertEqual(self._state()["totals"]["indexes"], 2)

    def test_scan_failure_does_not_kill_daemon(self):
        """NFS hiccups and LSF timeouts are routine; one failed tick must not
        kill the monitoring.
        """
        class Exploding(MonitorService):
            def scan(self, *args, **kwargs):
                raise RuntimeError("simulated NFS hiccup")

        daemon = Daemon(
            DaemonOptions(run_id="boom", wave_dirs=[self.demo["wave_dir"]],
                          use_lsf=False, once=True),
            settings=self.settings,
            monitor=Exploding(self.settings),
        )
        self.assertEqual(daemon.run(), 0)
        state = self._state("boom")
        self.assertIn("simulated NFS hiccup", state["daemon"]["last_error"])

    def test_error_state_still_updates_timestamp(self):
        """A failed scan still updates state.json, or the UI shows stale data
        while looking healthy.
        """
        class Exploding(MonitorService):
            def scan(self, *args, **kwargs):
                raise RuntimeError("x")

        daemon = Daemon(
            DaemonOptions(run_id="boom2", wave_dirs=[self.demo["wave_dir"]],
                          use_lsf=False, once=True),
            settings=self.settings, monitor=Exploding(self.settings))
        daemon.run()
        self.assertGreater(self._state("boom2")["updated_at"], 0)

    def test_multiple_ticks(self):
        options = DaemonOptions(
            run_id="multi", wave_dirs=[self.demo["wave_dir"]],
            use_lsf=False, interval_sec=0.01, max_ticks=3)
        Daemon(options, settings=self.settings).run()
        self.assertEqual(self._state("multi")["daemon"]["tick"], 3)

    def test_second_daemon_is_refused(self):
        """The daemon is the single writer; two at once break that outright."""
        first = self._daemon(run_id="lock")
        first.store.ensure()
        lock = FileLock(first.store.dir + "/daemon.lock").acquire()
        try:
            self.assertEqual(self._daemon(run_id="lock").run(), 1)
        finally:
            lock.release()

    def test_arcx_cfg_auto_discovered_in_wave_dir(self):
        """Without --arcx-cfg it should be found in the wave directory."""
        daemon = self._daemon(run_id="cfg")
        daemon.run()
        ids = {i["id"] for i in self._state("cfg")["issues"]}
        # cfg found -> no "I do not know what to check" issue
        self.assertNotIn("CFG_EXPECTATION_UNAVAILABLE", ids)

    def test_missing_cfg_yields_unknown_issue(self):
        os.remove(os.path.join(self.demo["wave_dir"], "arcx.cfg"))
        self._daemon(run_id="nocfg").run()
        ids = {i["id"] for i in self._state("nocfg")["issues"]}
        self.assertIn("CFG_EXPECTATION_UNAVAILABLE", ids)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "a", "b.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_acquire_raises(self):
        with FileLock(self.path, "first"):
            with self.assertRaises(LockBusy):
                FileLock(self.path, "second").acquire()

    def test_released_lock_can_be_retaken(self):
        FileLock(self.path).acquire().release()
        FileLock(self.path).acquire().release()

    def test_holder_info_is_readable(self):
        with FileLock(self.path, "diagnostics"):
            with open(self.path, encoding="utf-8") as handle:
                info = json.load(handle)
            self.assertEqual(info["pid"], os.getpid())
            self.assertEqual(info["purpose"], "diagnostics")

    def test_creates_parent_directories(self):
        with FileLock(self.path):
            self.assertTrue(os.path.exists(self.path))


class RunStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_runs_orders_by_recency(self):
        for name in ("old", "new"):
            store = RunStore(self.tmp.name, name)
            store.ensure()
            store.write_state({"run_id": name})
            time.sleep(0.02)
        self.assertEqual(RunStore.list_runs(self.tmp.name)[0], "new")

    def test_list_runs_on_empty_root(self):
        self.assertEqual(RunStore.list_runs(self.tmp.name), [])

    def test_audit_enriches_records(self):
        store = RunStore(self.tmp.name, "r")
        store.ensure()
        store.append_audit({"action": "x", "reason": "y"})
        with open(store.audit_path, encoding="utf-8") as handle:
            record = json.loads(handle.readline())
        self.assertEqual(record["action"], "x")
        self.assertIn("ts", record)
        self.assertEqual(record["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()
