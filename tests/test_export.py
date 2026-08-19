"""Phase 5: publishing to the shared disk.

This is what makes a single-user local tool useful to a team: nobody else runs
a daemon, nobody else needs an account, they open a file off a mounted share.

Most of these tests are about the ways a shared mount misbehaves. A share is
the least reliable thing this system touches -- it can vanish, fill up, or go
read-only at any moment -- and none of that may stop monitoring.
"""

import json
import os
import tempfile
import unittest

from arcx_auto.adapters.store import RunStore
from arcx_auto.config.settings import Settings
from arcx_auto.services.exporter import Exporter
from arcx_auto.web.export_page import render_export, render_shared_index


def _state(run_id="demo", updated_at=1000.0, cases=(), issues=(),
           index_key="1000"):
    """A state.json payload of the shape the daemon writes."""
    counts = {}
    case_rows = []
    for case_id, state in cases:
        counts[state] = counts.get(state, 0) + 1
        case_rows.append({
            "case_id": case_id, "state": state, "base_state": state,
            "note": "because reasons", "in_state_sec": 60.0,
            "silent_sec": 30.0, "issues": [],
        })
    attention = sum(
        n for s, n in counts.items()
        if s in ("FAILED", "LOST", "STALLED", "SUSPENDED", "UNKNOWN"))
    return {
        "schema_version": 1,
        "run_id": run_id,
        "updated_at": updated_at,
        "lsf": {"available": True, "note": None, "job_count": 0},
        "daemon": {},
        "indexes": [{
            "index_key": index_key,
            "run_folder": "/runs/%s/%s" % (run_id, index_key),
            "updated_at": updated_at,
            "counts": counts,
            "attention": attention,
            "cases": case_rows,
            "anomalies": {},
        }],
        "issues": list(issues),
    }


class ExporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.state_root = os.path.join(self.tmp.name, "state")
        self.settings.export.shared_root = os.path.join(self.tmp.name, "shared")
        self.exporter = Exporter(self.settings, user="zmwu", host="lsfbox")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_run(self, run_id, **kwargs):
        store = RunStore(self.settings.expanded_state_root(), run_id)
        store.ensure()
        store.write_state(_state(run_id=run_id, **kwargs))

    def _read(self, name):
        with open(os.path.join(self.exporter.user_dir, name),
                  encoding="utf-8") as handle:
            return handle.read()

    # -- the happy path ------------------------------------------------

    def test_writes_the_three_files(self):
        self._write_run("demo", cases=[("NTN_1", "DONE")])
        result = self.exporter.export_now(force=True)
        self.assertTrue(result.ok, result.errors)
        for name in ("status.json", "status.html", "updated_at"):
            self.assertTrue(
                os.path.isfile(os.path.join(self.exporter.user_dir, name)),
                name)

    def test_status_json_carries_the_runs(self):
        self._write_run("demo", cases=[("NTN_1", "DONE"), ("NDIO_1", "FAILED")])
        self.exporter.export_now(force=True)
        data = json.loads(self._read("status.json"))
        self.assertEqual(data["user"], "zmwu")
        self.assertEqual(data["host"], "lsfbox")
        self.assertEqual(data["totals"]["cases"], 2)
        self.assertEqual(data["totals"]["attention"], 1)

    def test_the_page_names_the_case_needing_attention(self):
        self._write_run("demo", cases=[("NTN_1", "DONE"), ("NDIO_1", "FAILED")])
        self.exporter.export_now(force=True)
        html = self._read("status.html")
        self.assertIn("NDIO_1", html)
        self.assertIn("needs attention", html)

    def test_updated_at_is_readable_by_a_shell(self):
        """A plain timestamp, so `cat updated_at` answers "is this stale"
        without parsing anything.
        """
        self._write_run("demo")
        self.exporter.export_now(force=True)
        text = self._read("updated_at").strip()
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_history_keeps_finished_runs(self):
        self._write_run("old", cases=[("NTN_1", "DONE")])
        self._write_run("live", cases=[("NDIO_1", "RUNNING")])
        self.exporter.export_now(force=True)
        html = self._read("status.html")
        self.assertIn("finished runs", html)
        self.assertIn("old", html)

    def test_the_history_limit_is_honoured(self):
        self.settings.export.history_limit = 2
        for i in range(5):
            self._write_run("run%d" % i, cases=[("NTN_1", "DONE")])
        self.exporter.export_now(force=True)
        data = json.loads(self._read("status.json"))
        self.assertEqual(len(data["runs"]), 2)

    # -- the shared index ----------------------------------------------

    def test_the_root_page_lists_every_user(self):
        self._write_run("demo", cases=[("NTN_1", "DONE")])
        self.exporter.export_now(force=True)
        # A second user, as their own daemon would have left them
        other = os.path.join(self.exporter.shared_root, "alice")
        os.makedirs(other)
        with open(os.path.join(other, "status.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"user": "alice", "host": "box2", "generated_at": 1.0,
                       "totals": {"runs": 1, "cases": 3, "done": 3,
                                  "attention": 0}}, handle)

        self.exporter.refresh_shared_index()
        with open(os.path.join(self.exporter.shared_root, "index.html"),
                  encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("alice", html)
        self.assertIn("zmwu", html)

    def test_a_damaged_status_json_does_not_break_the_root_page(self):
        """One user's broken file must not take the page away from everyone."""
        self._write_run("demo")
        self.exporter.export_now(force=True)
        broken = os.path.join(self.exporter.shared_root, "bob")
        os.makedirs(broken)
        with open(os.path.join(broken, "status.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{ this is not json")

        self.assertEqual(self.exporter.refresh_shared_index(), [])
        self.assertTrue(os.path.isfile(
            os.path.join(self.exporter.shared_root, "index.html")))

    # -- the ways a share misbehaves -----------------------------------

    def test_an_unwritable_share_is_reported_not_raised(self):
        """The share can be unmounted or read-only. Monitoring continues."""
        self.settings.export.shared_root = "/proc/definitely/not/writable"
        exporter = Exporter(self.settings, user="zmwu")
        result = exporter.export_now(force=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.errors)

    def test_a_failed_export_does_not_count_as_done(self):
        """Otherwise a share that is briefly unwritable silently pushes the
        next attempt a whole interval into the future.
        """
        self.settings.export.shared_root = "/proc/definitely/not/writable"
        exporter = Exporter(self.settings, user="zmwu")
        exporter.export_now(force=True)
        self.assertTrue(exporter.due())

    def test_nothing_to_export_is_still_a_valid_export(self):
        """A user with no runs yet gets an empty page, not an error."""
        result = self.exporter.export_now(force=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.runs, 0)
        self.assertIn("nothing needs a person", self._read("status.html"))

    # -- the interval --------------------------------------------------

    def test_the_interval_is_respected(self):
        self._write_run("demo")
        self.assertTrue(self.exporter.due(now=1000.0))
        self.exporter.export_now(now=1000.0)
        self.assertFalse(self.exporter.due(now=1030.0))
        self.assertTrue(
            self.exporter.due(now=1000.0 + self.settings.export.interval_sec))

    def test_force_ignores_the_interval(self):
        self._write_run("demo")
        self.exporter.export_now(now=1000.0)
        result = self.exporter.export_now(now=1001.0, force=True)
        self.assertFalse(result.skipped)

    def test_a_skipped_export_writes_nothing(self):
        self._write_run("demo")
        self.exporter.export_now(now=1000.0)
        os.remove(os.path.join(self.exporter.user_dir, "status.html"))
        result = self.exporter.export_now(now=1001.0)
        self.assertTrue(result.skipped)
        self.assertFalse(os.path.exists(
            os.path.join(self.exporter.user_dir, "status.html")))

    # -- atomicity -----------------------------------------------------

    def test_no_temporary_files_are_left_behind(self):
        """Readers see the whole file or the previous one, never a fragment."""
        self._write_run("demo")
        self.exporter.export_now(force=True)
        self.exporter.export_now(force=True)
        leftovers = [n for n in os.listdir(self.exporter.user_dir)
                     if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])


class ExportPageTest(unittest.TestCase):
    """The page is read by people who cannot run the tool. What it says has to
    stand on its own.
    """

    def _payload(self, runs):
        return {"user": "zmwu", "host": "lsfbox", "generated_at": 1000.0,
                "totals": {"runs": len(runs), "cases": 0, "done": 0,
                           "attention": 0},
                "runs": runs}

    def test_a_healthy_run_says_so_plainly(self):
        html = render_export(self._payload(
            [_state(cases=[("NTN_1", "RUNNING")])]))
        self.assertIn("nothing needs a person", html)

    def test_problems_come_before_healthy_detail(self):
        """Somebody opening this asks one question: is anything wrong. Making
        them scroll past healthy runs to find out is how a status page stops
        being read.
        """
        html = render_export(self._payload(
            [_state(cases=[("NDIO_1", "FAILED"), ("NTN_1", "RUNNING")])]))
        self.assertLess(html.index("needs attention"), html.index("run demo"))

    def test_issues_are_grouped_rather_than_repeated(self):
        issues = [
            {"id": "NETLIST_NO_SIGNATURE", "severity": "FATAL",
             "title": "netlist does not look extracted", "case_id": "C%d" % i}
            for i in range(50)
        ]
        html = render_export(self._payload(
            [_state(cases=[("C0", "FAILED")], issues=issues)]))
        self.assertEqual(html.count("NETLIST_NO_SIGNATURE"), 1)
        self.assertIn("and 42 more", html)

    def test_a_stale_page_says_it_is_stale(self):
        """A page frozen hours ago looks exactly like a healthy one, which is
        the most misleading thing a status display can do.
        """
        html = render_export(self._payload(
            [_state(updated_at=1000.0, cases=[("NTN_1", "RUNNING")])]))
        self.assertIn("ago", html)
        self.assertIn("class='bad'", html)

    def test_lsf_being_unavailable_is_stated(self):
        state = _state(cases=[("NTN_1", "RUNNING")])
        state["lsf"] = {"available": False, "note": "bjobs timed out"}
        html = render_export(self._payload([state]))
        self.assertIn("bjobs timed out", html)

    def test_the_page_is_self_contained(self):
        """No server, so nothing may be fetched: no links out, no assets."""
        html = render_export(self._payload(
            [_state(cases=[("NTN_1", "DONE")])]))
        self.assertNotIn("<script", html)
        self.assertNotIn("http://", html)
        self.assertNotIn('href="/run/', html)
        self.assertIn("<style>", html)

    def test_html_is_escaped(self):
        state = _state(cases=[("<img src=x>", "FAILED")])
        html = render_export(self._payload([state]))
        self.assertNotIn("<img src=x>", html)
        self.assertIn("&lt;img", html)

    def test_the_root_page_survives_a_user_with_no_totals(self):
        html = render_shared_index([{"user": "alice"}], generated_at=1000.0)
        self.assertIn("alice", html)

    def test_an_empty_share_says_so(self):
        self.assertIn("nobody has exported yet",
                      render_shared_index([], generated_at=1000.0))


if __name__ == "__main__":
    unittest.main()
