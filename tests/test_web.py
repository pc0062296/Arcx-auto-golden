"""Web UI: routing, rendering, and the hard read-only property."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from arcx_auto.config.settings import Settings
from arcx_auto.daemon import Daemon, DaemonOptions
from arcx_auto.web import WebOptions, serve
from arcx_auto.web import pages
from tests.fixtures.fake_run import build_demo


class _Ready(threading.Event):
    port = 0
    httpd = None


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        demo = build_demo(os.path.join(cls.tmp.name, "demo"))
        cls.demo = demo
        cls.state_root = os.path.join(cls.tmp.name, "state")

        settings = Settings()
        settings.state_root = cls.state_root
        settings.export.shared_root = os.path.join(cls.tmp.name, "shared")
        Daemon(
            DaemonOptions(run_id="demo", wave_dirs=[demo["wave_dir"]],
                          use_lsf=False, once=True),
            settings=settings,
        ).run()

        cls.ready = _Ready()
        cls.thread = threading.Thread(
            target=serve,
            args=(WebOptions(state_root=cls.state_root, port=0, refresh_sec=0),
                  cls.ready),
            daemon=True,
        )
        cls.thread.start()
        cls.ready.wait(10)
        cls.base = "http://127.0.0.1:%d" % cls.ready.port

    @classmethod
    def tearDownClass(cls):
        if cls.ready.httpd is not None:
            cls.ready.httpd.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def status_of(self, path):
        try:
            code, _ = self.get(path)
            return code
        except urllib.error.HTTPError as exc:
            return exc.code

    # -- Routing -------------------------------------------------------

    def test_home_lists_runs(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("demo", body)
        self.assertIn("needs your decision", body)

    def test_run_page(self):
        code, body = self.get("/run/demo")
        self.assertEqual(code, 200)
        self.assertIn("1000", body)
        self.assertIn("1001", body)

    def test_index_page_lists_cases(self):
        code, body = self.get("/run/demo/index/1000")
        self.assertEqual(code, 200)
        for case_id in ("NDIO_1", "PDIO_1", "NTN_1"):
            self.assertIn(case_id, body)

    def test_case_page_shows_issues_and_evidence(self):
        code, body = self.get("/run/demo/index/1001/case/NMOS_1")
        self.assertEqual(code, 200)
        self.assertIn("NETLIST_MISSING", body)
        # The real paths from the evidence have to be visible: a verdict
        # without evidence cannot be checked
        self.assertIn("blocking_naming_qcap", body)

    def test_case_page_shows_check_docstring(self):
        """A check's docstring is what the UI shows."""
        _code, body = self.get("/run/demo/index/1001/case/NMOS_1")
        self.assertIn("false success", body)

    def test_api_returns_raw_state(self):
        code, body = self.get("/api/state/demo")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["run_id"], "demo")
        self.assertIn("totals", data)

    def test_healthz(self):
        code, body = self.get("/healthz")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    # -- Error handling ------------------------------------------------

    def test_unknown_run_is_404(self):
        self.assertEqual(self.status_of("/run/nope"), 404)

    def test_unknown_index_is_404(self):
        self.assertEqual(self.status_of("/run/demo/index/9999"), 404)

    def test_unknown_case_is_404(self):
        self.assertEqual(self.status_of("/run/demo/index/1000/case/NOPE"), 404)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.status_of("/nothing"), 404)

    def test_path_traversal_rejected(self):
        """run ids are looked up in the existing runs, never concatenated
        into a path.
        """
        for attack in ("/run/..%2f..%2fetc", "/api/state/..%2f..%2fetc",
                       "/run/%2e%2e%2f%2e%2e%2fetc/index/x"):
            self.assertEqual(self.status_of(attack), 404, attack)

    # -- Read only -----------------------------------------------------

    def test_no_write_endpoints(self):
        """The UI is read only. Write actions are posted to the daemon
        through commands/ (architecture decision 1: the daemon is the single
        writer).
        """
        request = urllib.request.Request(
            self.base + "/run/demo", method="POST", data=b"x")
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("POST should not be accepted")
        except urllib.error.HTTPError as exc:
            self.assertIn(exc.code, (400, 404, 405, 501))

    # -- Looking at a file ---------------------------------------------

    def _log_path(self, index_key, case_id):
        _code, body = self.get("/api/state/demo")
        state = json.loads(body)
        for index in state["indexes"]:
            if index["index_key"] != index_key:
                continue
            for case in index["cases"]:
                if case["case_id"] == case_id:
                    return case.get("log_path")
        return None

    def test_case_page_shows_the_end_of_the_log(self):
        """The most repeated action in the whole review, answered on the page.

        Before this the page gave a path and the engineer went to a terminal
        to tail it -- once per failure, every time.
        """
        _code, body = self.get("/run/demo/index/1000/case/PDIO_1")
        self.assertIn("<h2>log</h2>", body)
        self.assertIn("last 500", body)

    def test_case_page_lists_the_files_the_case_produced(self):
        _code, body = self.get("/run/demo/index/1001/case/PMOS_10")
        self.assertIn("files this case produced", body)

    def test_view_serves_a_file_inside_the_run(self):
        log = self._log_path("1000", "PDIO_1")
        self.assertTrue(log)
        code, body = self.get("/view?" + urllib.parse.urlencode(
            {"path": log, "mode": "tail", "lines": "20"}))
        self.assertEqual(code, 200)
        self.assertIn("tail", body)

    def test_view_refuses_a_file_outside_the_run_directories(self):
        """The check that separates "reads a log" from "reads anything".

        The process runs as the person using it, so this is not a privilege
        boundary -- it is a promise about what this tool will open when asked,
        and it has to hold before anyone points it at a shared disk.
        """
        for attack in ("/etc/passwd", os.path.join(self.tmp.name, "..", "etc"),
                       "~/.ssh/id_rsa"):
            code = self.status_of("/view?" + urllib.parse.urlencode(
                {"path": attack}))
            self.assertEqual(code, 403, attack)

    def test_view_refuses_a_symlink_pointing_out_of_the_run(self):
        outside = os.path.join(self.tmp.name, "outside_secret")
        with open(outside, "w") as handle:
            handle.write("not for the viewer")
        link = os.path.join(self.demo["wave_dir"], "innocent.log")
        if not os.path.exists(link):
            os.symlink(outside, link)
        self.addCleanup(lambda: os.path.exists(link) and os.unlink(link))
        code = self.status_of("/view?" + urllib.parse.urlencode(
            {"path": link}))
        self.assertEqual(code, 403)

    def test_view_of_a_missing_file_is_a_page_not_a_crash(self):
        missing = os.path.join(self.demo["wave_dir"], "no_such_file.log")
        code, body = self.get("/view?" + urllib.parse.urlencode(
            {"path": missing}))
        self.assertEqual(code, 200)
        self.assertIn("could not read this file", body)

    def test_view_without_a_path_is_a_bad_request(self):
        self.assertEqual(self.status_of("/view"), 400)

    # -- Filtering the case table --------------------------------------

    def test_index_filter_narrows_the_table(self):
        _code, everything = self.get("/run/demo/index/1001")
        self.assertIn("PMOS_10", everything)
        _code, only_bad = self.get("/run/demo/index/1001?show=attention")
        self.assertIn("needs a person", only_bad)
        self.assertNotIn(">PMOS_10<", only_bad)

    def test_index_filter_chips_are_plain_links(self):
        """No JavaScript: a filtered table is a URL somebody can send."""
        _code, body = self.get("/run/demo/index/1001")
        self.assertIn("?show=attention", body)

    def test_serving_does_not_touch_run_folder(self):
        before = _tree(self.demo["wave_dir"])
        for path in ("/", "/run/demo", "/run/demo/index/1001",
                     "/run/demo/index/1001/case/NMOS_1"):
            self.get(path)
        self.assertEqual(_tree(self.demo["wave_dir"]), before)


def _index(key, group="", folder="", attention=0, done=1, cfg=""):
    counts = {"DONE": done}
    if attention:
        counts["FAILED"] = attention
    return {"index_key": key, "run_folder": "/runs/w/b/%s_run" % key,
            "group": group, "folder": folder, "cfg": cfg,
            "counts": counts, "attention": attention, "cases": [],
            "anomalies": {}}


class IndexGroupingTest(unittest.TestCase):
    """One submission carries several cfgs and several source folders.

    Flattened into one table that structure is invisible, and "which corner
    was this" becomes a question about a path.
    """

    def render(self, indexes):
        return pages._index_sections("r1", indexes)

    def test_a_run_without_the_metadata_keeps_the_flat_table(self):
        """Runs made before the batch layout carry none of it, and must not
        end up inside a container called "".
        """
        body = self.render([_index("1000"), _index("1001")])
        self.assertNotIn("<details", body)
        self.assertIn("1000", body)

    def test_cfgs_become_the_outer_level(self):
        body = self.render([
            _index("1000", group="typical", folder="/p/corner_v2g/typ/blkA"),
            _index("1002", group="cworst", folder="/p/corner_v2g/cw/blkA"),
        ])
        self.assertIn("typical", body)
        self.assertIn("cworst", body)
        self.assertEqual(body.count("<summary"), 4)   # 2 cfgs, 1 folder each

    def test_folders_are_the_inner_level(self):
        body = self.render([
            _index("1000", group="typical", folder="/p/corner_v2g/typ/blkA"),
            _index("1001", group="typical", folder="/p/corner_v2g/typ/blkB"),
        ])
        self.assertIn("typ/blkA", body)
        self.assertIn("typ/blkB", body)
        self.assertEqual(body.count("<summary"), 3)   # 1 cfg, 2 folders

    def test_the_index_table_is_unchanged_inside(self):
        body = self.render([
            _index("1000", group="typical", folder="/p/a/blkA")])
        self.assertIn("/run/r1/index/1000", body)
        for column in ("progress", "cases", "done", "attention"):
            self.assertIn(column, body)

    def test_a_container_with_something_wrong_is_open(self):
        body = self.render([
            _index("1000", group="typical", folder="/p/a/blkA"),
            _index("1002", group="cworst", folder="/p/a/blkB", attention=2),
        ])
        opened = [block for block in body.split("<details")
                  if block.startswith(" open")]
        self.assertTrue(any("cworst" in block for block in opened))
        self.assertFalse(any("typical" in block and "cworst" not in block
                             for block in opened))

    def test_the_only_container_is_open(self):
        """Nothing is gained by making somebody click once to see everything
        there is.
        """
        body = self.render([_index("1000", group="typical", folder="/p/a/b")])
        self.assertNotIn("<details>", body)
        self.assertIn("<details open", body)

    def test_the_closed_line_says_enough_to_skip_it(self):
        body = self.render([
            _index("1000", group="typical", folder="/p/a/blkA", done=7),
            _index("1002", group="cworst", folder="/p/a/blkB", attention=2),
        ])
        self.assertIn("1 index, 7 case(s)", body)
        self.assertIn("2 need a person", body)

    def test_an_index_with_no_folder_recorded_still_appears(self):
        body = self.render([_index("1000", group="typical"),
                            _index("1001", group="typical",
                                   folder="/p/a/blkA")])
        self.assertIn("no source folder recorded", body)
        self.assertIn("1000", body)

    def test_the_cfg_name_stands_in_for_a_missing_group_name(self):
        body = self.render([_index("1000", cfg="chipA_cbt.cfg",
                                   folder="/p/a/blkA")])
        self.assertIn("chipA_cbt.cfg", body)


class PageRenderTest(unittest.TestCase):
    """Page rendering is pure, so it can be tested by feeding it data."""

    def test_empty_home(self):
        body = pages.render_home([], refresh=0)
        self.assertIn("no run is being monitored yet", body)

    def test_html_is_escaped(self):
        state = {
            "run_id": "<script>alert(1)</script>",
            "totals": {"states": {}, "cases": 0, "indexes": 0,
                       "attention": 0, "issues": 0},
            "updated_at": 0,
            "daemon": {},
        }
        body = pages.render_home([state], refresh=0)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_lsf_banner_when_unavailable(self):
        state = {
            "run_id": "r", "updated_at": 0, "daemon": {},
            "totals": {"states": {}, "cases": 0, "indexes": 0,
                       "attention": 0, "issues": 0, "severities": {}},
            "lsf": {"available": False, "note": "bjobs is gone"},
            "indexes": [], "issues": [],
        }
        body = pages.render_run(state, refresh=0)
        self.assertIn("LSF data unavailable", body)
        self.assertIn("bjobs is gone", body)

    def test_daemon_error_banner(self):
        state = {
            "run_id": "r", "updated_at": 0,
            "daemon": {"last_error": "Traceback: boom"},
            "totals": {"states": {}, "cases": 0, "indexes": 0,
                       "attention": 0, "issues": 0, "severities": {}},
            "lsf": {"available": True}, "indexes": [], "issues": [],
        }
        body = pages.render_run(state, refresh=0)
        self.assertIn("the last scan failed", body)

    def test_issue_summary_groups_by_id(self):
        """When 200 cases hit one problem, show the aggregate, not 200 lines."""
        issues = [
            {"id": "NETLIST_MISSING", "severity": "FATAL", "title": "missing",
             "case_id": "C%d" % i, "index_key": "1000"}
            for i in range(12)
        ]
        state = {
            "run_id": "r", "updated_at": 0, "daemon": {},
            "totals": {"states": {}, "cases": 12, "indexes": 1,
                       "attention": 12, "issues": 12,
                       "severities": {"FATAL": 12}},
            "lsf": {"available": True}, "indexes": [], "issues": issues,
        }
        body = pages.render_run(state, refresh=0)
        self.assertEqual(body.count("NETLIST_MISSING"), 1)
        self.assertIn("(+6)", body)

    def test_silent_time_escalates_visually(self):
        """The longer the quiet, the louder the marker: the system states the
        number, the judgement stays human.
        """
        self.assertIn("bad", pages._silent_cell({"silent_sec": 9 * 3600}))
        self.assertIn("warn", pages._silent_cell({"silent_sec": 5 * 3600}))
        self.assertNotIn("bad", pages._silent_cell({"silent_sec": 60}))

    def test_stale_daemon_is_flagged(self):
        """A daemon that dies quietly freezes the display at the last moment,
        looking perfectly healthy -- the most dangerous state of all.
        """
        import time as _time
        html = pages._daemon_health({"pid": 1}, _time.time() - 3600)
        self.assertIn("no update", html)


def _tree(root):
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            result[os.path.join(dirpath, name)] = None
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                result[path] = os.path.getsize(path)
            except OSError:
                result[path] = -1
    return result


if __name__ == "__main__":
    unittest.main()


class NavigationTest(unittest.TestCase):
    """The submission flow has to be reachable from the page people open.

    The monitoring pages and the submission pages were built at different
    times and were not linked at all, so the dashboard had no route to the
    thing that starts a run.
    """

    def test_every_served_page_offers_the_submission_flow(self):
        from arcx_auto.web import pages

        state = {"run_id": "r", "updated_at": 0, "daemon": {},
                 "totals": {}, "indexes": [], "issues": []}
        for html in (pages.render_home([state], 0),
                     pages.render_run(state, 0)):
            self.assertIn('href=\'/submit\'', html)
            self.assertIn('href=\'/commands\'', html)
