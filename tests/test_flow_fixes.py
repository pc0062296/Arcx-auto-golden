"""Problems found by running the tool for real, and the friction around them.

Every test here comes from somebody using the thing rather than from reading
the code, which is why most of them are about what the person sees rather than
about what is computed.
"""

import os
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

from arcx_auto.adapters.fs import FsAdapter
from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import Settings
from arcx_auto.services.browse import classify, list_dir, suggest
from arcx_auto.services.commands import DONE, PENDING, CommandQueue
from arcx_auto.web import pages
from arcx_auto.web.server import WebOptions, serve
from tests.fixtures.fake_run import (
    CaseSpec,
    make_arcx_cfg,
    make_dir_map,
    make_index_run_folder,
    make_index_source,
)


# ---------------------------------------------------------------------------
# 3. Only <index>_run directories are index run folders
# ---------------------------------------------------------------------------

class IndexRunFolderTest(unittest.TestCase):
    """Arcx names them <index path basename>_run.

    Listing every directory instead swept up whatever else lived in the wave
    and then reported QA failures against folders that were never cases.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wave = os.path.join(self.tmp.name, "wave_001")
        make_index_run_folder(self.wave, "1000", [CaseSpec("NTN_1")])
        for junk in ("logs", "scratch", "QC_Cc"):
            os.makedirs(os.path.join(self.wave, junk), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_run_folders_are_listed(self):
        found = FsAdapter().list_index_run_folders(self.wave)
        self.assertEqual([key for key, _path in found], ["1000"])

    def test_unrelated_directories_are_not_scanned_at_all(self):
        """They used to be scanned and then fail QA. Not being a case is not
        a defect, so it must not produce one.
        """
        found = dict(FsAdapter().list_index_run_folders(self.wave))
        self.assertNotIn("logs", found)
        self.assertNotIn("scratch", found)

    def test_the_key_is_the_name_without_run(self):
        found = FsAdapter().list_index_run_folders(self.wave)
        self.assertEqual(found[0][0], "1000")
        self.assertTrue(found[0][1].endswith("1000_run"))

    def test_the_manifest_maps_a_basename_back_to_its_dir_map_key(self):
        """The folder is named after the index *path*, which is not the key:
        "1000" may point at /proj/foo/index1000, giving index1000_run.
        """
        import json

        wave = os.path.join(self.tmp.name, "wave_002")
        make_index_run_folder(wave, "index1000", [CaseSpec("NTN_1")])
        meta = os.path.join(wave, ".arcx_auto")
        os.makedirs(meta, exist_ok=True)
        with open(os.path.join(meta, "manifest.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"indexes": [{"index_key": "1000",
                                    "path": "/proj/foo/index1000"}]}, handle)

        found = FsAdapter().list_index_run_folders(wave)
        self.assertEqual([key for key, _p in found], ["1000"])

    def test_scanning_one_folder_directly_still_names_it_right(self):
        folder = os.path.join(self.wave, "1000_run")
        self.assertEqual(FsAdapter().index_key_for(folder), "1000")


# ---------------------------------------------------------------------------
# 2 and 6. Defaults that stopped people working
# ---------------------------------------------------------------------------

class DefaultsTest(unittest.TestCase):
    def test_an_unreadable_special_cfg_does_not_block_selection(self):
        """The sizing is what is unknown, not the work. Refusing to let the
        index be selected helps nobody.
        """
        from arcx_auto.adapters.arcx import ArcxAdapter

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "idx")
            os.makedirs(path)
            open(os.path.join(path, "a.gds"), "w").close()
            spec = ArcxAdapter().build_index_spec("1000", path)

        self.assertTrue(spec.usable)
        self.assertEqual(spec.cpu_per_case, 4)
        self.assertTrue(spec.cpu_estimated)

    def test_the_quota_threshold_matches_a_real_cluster(self):
        self.assertEqual(Settings().gate.quota_threshold, 10000)


# ---------------------------------------------------------------------------
# 4. Report QA waits for the whole index
# ---------------------------------------------------------------------------

class ReportQaTimingTest(unittest.TestCase):
    """Arcx writes the reports only once every case has finished.

    Checking them earlier reports missing report directories on a run that is
    doing nothing wrong.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()

    def tearDown(self):
        self.tmp.cleanup()

    def _issue_ids(self, cases):
        from arcx_auto.services.monitor import MonitorService

        folder = make_index_run_folder(self.tmp.name, "1000", cases,
                                       report_dirs=())
        make_arcx_cfg(os.path.join(folder, "zmwu.cfg"))
        result = MonitorService(self.settings).scan(
            run_folders=[folder], use_lsf=False)
        return {i.id for i in result.all_issues()}

    def test_missing_reports_are_not_reported_while_cases_run(self):
        ids = self._issue_ids([CaseSpec("NTN_1", "complete", artifacts="full"),
                               CaseSpec("NDIO_1", "running", artifacts="none")])
        self.assertNotIn("REPORT_DIR_MISSING", ids)

    def test_missing_reports_are_reported_once_everything_finished(self):
        ids = self._issue_ids([CaseSpec("NTN_1", "complete", artifacts="full")])
        self.assertIn("REPORT_DIR_MISSING", ids)

    def test_unfinished_cases_are_still_reported_while_they_run(self):
        """"Some are unfinished" is true *now*, so it cannot wait for the end
        -- it would never fire at all.
        """
        ids = self._issue_ids([CaseSpec("NTN_1", "complete", artifacts="full"),
                               CaseSpec("NDIO_1", "running", artifacts="none")])
        self.assertIn("INDEX_INCOMPLETE", ids)


# ---------------------------------------------------------------------------
# 1 and 5. Stopping
# ---------------------------------------------------------------------------

class StoppingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.state_root = os.path.join(self.tmp.name, "state")
        self.settings.run_root = os.path.join(self.tmp.name, "runs")
        self.settings.export.shared_root = os.path.join(self.tmp.name, "shared")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stopped_daemon_records_that_it_stopped(self):
        """Otherwise its last snapshot stays on the page looking live, and the
        moment it froze at is exactly the moment it was healthy.
        """
        from arcx_auto.daemon.loop import Daemon, DaemonOptions

        Daemon(DaemonOptions(run_id="t", use_lsf=False, once=True),
               settings=self.settings).run()
        state = RunStore(self.settings.expanded_state_root(), "t").read_state()
        self.assertIs(state["daemon"]["running"], False)
        self.assertIsNotNone(state["daemon"].get("stopped_at"))

    def test_the_page_says_so_rather_than_looking_healthy(self):
        state = {"run_id": "r", "updated_at": 0, "totals": {}, "indexes": [],
                 "issues": [], "daemon": {"running": False, "stopped_at": 1.0}}
        html = pages.render_run(state, 0)
        self.assertIn("the daemon has stopped", html)
        self.assertIn("Nothing is watching this run", html)

    def test_a_running_daemon_is_not_reported_as_stopped(self):
        state = {"run_id": "r", "updated_at": 0, "totals": {}, "indexes": [],
                 "issues": [], "daemon": {"pid": 123}}
        self.assertNotIn("the daemon has stopped", pages.render_run(state, 0))

    def test_the_gate_wait_ends_when_the_daemon_is_asked_to_stop(self):
        """A submission can sit at the gate for hours. Sleeping through a stop
        request is what made the first Ctrl-C look like it did nothing.
        """
        from arcx_auto.services.submitter import Submitter

        stop = threading.Event()
        submitter = Submitter(self.settings, stop=stop)
        stop.set()
        # Would block for a minute if the event were not honoured.
        submitter.sleep(60.0)


# ---------------------------------------------------------------------------
# Picking files instead of typing paths
# ---------------------------------------------------------------------------

class BrowseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(os.path.join(self.root, "sub"))
        for name in ("dir_map", "zmwu.cfg", "notes.txt"):
            open(os.path.join(self.root, name), "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_directories_come_first(self):
        listing = list_dir(self.root)
        self.assertTrue(listing.entries[0].is_dir)

    def test_the_useful_files_are_labelled(self):
        kinds = {e.name: e.kind for e in list_dir(self.root).entries}
        self.assertEqual(kinds["dir_map"], "dir_map")
        self.assertEqual(kinds["zmwu.cfg"], "arcx_cfg")
        self.assertEqual(kinds["notes.txt"], "")

    def test_nothing_is_hidden_on_the_strength_of_a_guess(self):
        """A dir_map not called dir_map still has to be selectable."""
        names = {e.name for e in list_dir(self.root).entries}
        self.assertIn("notes.txt", names)

    def test_likely_files_are_offered(self):
        self.assertEqual([os.path.basename(p)
                          for p in suggest(self.root, "dir_map")], ["dir_map"])

    def test_an_unreadable_directory_is_navigation_not_failure(self):
        listing = list_dir(os.path.join(self.root, "nope"))
        self.assertIsNotNone(listing.error)
        self.assertIsNotNone(listing.parent)

    def test_a_file_path_says_it_is_not_a_directory(self):
        listing = list_dir(os.path.join(self.root, "dir_map"))
        self.assertEqual(listing.error, "not a directory")

    def test_crumbs_make_every_level_one_click(self):
        crumbs = list_dir(self.root).crumbs
        self.assertEqual(crumbs[0][1], "/")
        self.assertEqual(crumbs[-1][1], self.root)

    def test_hidden_files_stay_hidden_unless_asked_for(self):
        open(os.path.join(self.root, ".secret"), "w").close()
        self.assertNotIn(".secret",
                         {e.name for e in list_dir(self.root).entries})
        self.assertIn(".secret", {e.name for e in
                                  list_dir(self.root, show_hidden=True).entries})


class _Ready(threading.Event):
    port = 0


class PickerFlowTest(unittest.TestCase):
    """Two clicks instead of two absolute paths typed by hand."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings()
        cls.settings.state_root = os.path.join(cls.tmp.name, "state")
        cls.settings.run_root = os.path.join(cls.tmp.name, "runs")
        cls.settings.export.shared_root = os.path.join(cls.tmp.name, "shared")

        cls.proj = os.path.join(cls.tmp.name, "proj")
        os.makedirs(cls.proj)
        cls.dir_map = make_dir_map(
            os.path.join(cls.proj, "dir_map"),
            {"1000": make_index_source(cls.proj, "1000", gds_count=2)})
        cls.cfg = make_arcx_cfg(os.path.join(cls.proj, "arcx.cfg"))

        cls.ready = _Ready()
        threading.Thread(
            target=serve,
            args=(WebOptions(state_root=cls.settings.expanded_state_root(),
                             port=0, refresh_sec=0, settings=cls.settings),
                  cls.ready),
            daemon=True).start()
        cls.ready.wait(5)
        cls.base = "http://127.0.0.1:%d" % cls.ready.port

    @classmethod
    def tearDownClass(cls):
        httpd = getattr(cls.ready, "httpd", None)
        if httpd is not None:
            httpd.shutdown()
        cls.tmp.cleanup()

    def _post(self, path, data):
        return urllib.request.urlopen(urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(data, doseq=True).encode(),
            headers={"Origin": self.base}), timeout=10)

    def _get(self, path):
        return urllib.request.urlopen(self.base + path,
                                      timeout=10).read().decode()

    def _draft(self):
        return self._post("/submit/new", {}).geturl().rstrip(
            "/").split("/")[-1].split("?")[0]

    def test_the_listing_shows_the_files(self):
        draft = self._draft()
        page = self._get("/pick/%s/dir_map?path=%s"
                         % (draft, urllib.parse.quote(self.proj)))
        self.assertIn("dir_map", page)
        self.assertIn("arcx.cfg", page)
        self.assertIn("likely here", page)

    def test_picking_one_file_asks_for_the_other(self):
        draft = self._draft()
        landed = self._post("/pick/%s/dir_map" % draft,
                            {"value": self.dir_map}).geturl()
        self.assertTrue(landed.endswith("/pick/%s/arcx_cfg" % draft), landed)

    def test_picking_both_goes_straight_to_the_indices(self):
        draft = self._draft()
        self._post("/pick/%s/dir_map" % draft, {"value": self.dir_map})
        body = self._post("/pick/%s/arcx_cfg" % draft,
                          {"value": self.cfg}).read().decode()
        self.assertIn("select indices", body)
        self.assertIn("1000", body)

    def test_the_first_choice_is_not_lost_by_making_the_second(self):
        draft = self._draft()
        self._post("/pick/%s/dir_map" % draft, {"value": self.dir_map})
        body = self._post("/pick/%s/arcx_cfg" % draft,
                          {"value": self.cfg}).read().decode()
        self.assertIn(os.path.basename(self.dir_map), body)

    def test_a_path_that_is_not_a_file_is_refused(self):
        draft = self._draft()
        landed = self._post("/pick/%s/dir_map" % draft,
                            {"value": "/nope/at/all"}).geturl()
        self.assertIn("/pick/%s/dir_map" % draft, landed)

    def test_the_picker_reopens_where_it_was(self):
        draft = self._draft()
        self._get("/pick/%s/dir_map?path=%s"
                  % (draft, urllib.parse.quote(self.proj)))
        self.assertIn(self.proj, self._get("/pick/%s/dir_map" % draft))


# ---------------------------------------------------------------------------
# Friction found by walking the flow
# ---------------------------------------------------------------------------

class FlowPolishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, cases, run_id="r"):
        counts = {}
        rows = []
        for case_id, state in cases:
            counts[state] = counts.get(state, 0) + 1
            rows.append({"case_id": case_id, "state": state,
                         "in_state_sec": 1.0, "note": "why", "issues": []})
        attention = sum(n for s, n in counts.items()
                        if s in pages.ATTENTION_STATES)
        return {"run_id": run_id, "updated_at": 0, "daemon": {"pid": 1},
                "totals": {"attention": attention}, "issues": [],
                "indexes": [{"index_key": "1000", "counts": counts,
                             "attention": attention, "cases": rows,
                             "run_folder": "/runs/r/wave_001/1000_run",
                             "anomalies": {}}]}

    def test_the_home_page_names_what_needs_a_person(self):
        """A row of counts makes somebody derive the answer to the only
        question they came with.
        """
        html = pages.render_home([self._state([("NDIO_1", "FAILED")])], 0)
        self.assertIn("needs attention (1)", html)
        self.assertIn("NDIO_1", html)

    def test_the_home_page_says_so_when_nothing_does(self):
        html = pages.render_home([self._state([("NTN_1", "DONE")])], 0)
        self.assertIn("nothing needs a person", html)

    def test_a_finished_and_clean_run_says_it_is_ready(self):
        """Knowing it is over is the last step of the job, and a case table
        does not say that.
        """
        html = pages.render_run(self._state([("NTN_1", "DONE"),
                                             ("PTN_1", "DONE")]), 0)
        self.assertIn("all 2 case(s) passed", html)

    def test_a_finished_run_with_failures_does_not_claim_success(self):
        html = pages.render_run(self._state([("NTN_1", "DONE"),
                                             ("NDIO_1", "FAILED")]), 0)
        self.assertIn("1 of 2 case(s) unresolved", html)
        self.assertIn("will not improve on their own", html)

    def test_a_running_run_claims_nothing(self):
        html = pages.render_run(self._state([("NTN_1", "RUNNING")]), 0)
        self.assertNotIn("finished", html.split("<main>")[1][:400])

    def test_a_queued_request_can_be_withdrawn(self):
        queue = CommandQueue(self.tmp.name)
        command = queue.submit("submit", {"run_id": "r"})
        self.assertTrue(queue.cancel(command.id))
        self.assertEqual(queue.pending_count(), 0)
        self.assertIn("cancelled", queue.list(DONE)[0].error)

    def test_a_started_request_cannot_be_withdrawn(self):
        """Once the daemon has it, jobs may already be out, and "cancel" would
        be a promise this cannot keep.
        """
        queue = CommandQueue(self.tmp.name)
        command = queue.submit("submit", {"run_id": "r"})
        queue.claim_next()
        self.assertFalse(queue.cancel(command.id))


if __name__ == "__main__":
    unittest.main()


class SubmitLatencyTest(unittest.TestCase):
    """Pressing submit must not wait for the next scan.

    Scanning is expensive -- every run folder over NFS -- so it is paced by the
    poll interval, five minutes when nothing is running. Serving the queue is
    one listdir on a local directory. Tying them together meant pressing submit
    and watching nothing happen for up to five minutes.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.state_root = os.path.join(self.tmp.name, "state")
        self.settings.run_root = os.path.join(self.tmp.name, "runs")
        self.settings.export.shared_root = os.path.join(self.tmp.name, "shared")
        self.settings.export.enabled = False

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_queued_request_is_picked_up_between_scans(self):
        import time

        from arcx_auto.daemon.loop import Daemon, DaemonOptions

        # The idle scan interval is long on purpose; the queue must not be
        # behind it.
        self.settings.monitor.poll_idle_sec = 300.0
        self.settings.monitor.poll_active_sec = 300.0
        self.settings.monitor.command_poll_sec = 0.05

        daemon = Daemon(DaemonOptions(run_id="lat", use_lsf=False),
                        settings=self.settings)
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            queue = CommandQueue(self.settings.expanded_state_root())
            deadline = time.time() + 5.0
            while time.time() < deadline and daemon._tick < 1:
                time.sleep(0.02)

            queue.submit("submit", {"run_id": "x", "groups": [
                {"name": "g", "dir_map": "/nope", "arcx_cfg": "/nope",
                 "index_keys": ["1"]}]})

            deadline = time.time() + 5.0
            while time.time() < deadline and not queue.list(DONE):
                time.sleep(0.02)
        finally:
            daemon.stop()
            thread.join(timeout=5)

        self.assertTrue(
            queue.list(DONE),
            "the request was still waiting; it is tied to the scan interval "
            "again")

    def test_the_poll_interval_is_far_below_the_scan_interval(self):
        monitor = Settings().monitor
        self.assertLess(monitor.command_poll_sec, monitor.poll_active_sec)
        self.assertLessEqual(monitor.command_poll_sec, 5.0)
