"""Operating the tool from a browser: groups, the queue, and the rerun button.

This is the first code in the project that lets something outside the system
ask it to do work, so most of these tests are about the boundary rather than
the feature: what a malformed request does, what a cross-site post does, and
what happens when the thing somebody was shown is no longer true by the time
the daemon gets to it.

The rule underneath all of it: **the browser posts an intent and the daemon
does the work.** Two writers on one run folder is the failure this whole system
exists to avoid.
"""

import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from arcx_auto.config.settings import Settings
from arcx_auto.domain.enums import PlanMode
from arcx_auto.domain.models import IndexSpec, SubmitGroup
from arcx_auto.services import workspaces
from arcx_auto.services.commands import DONE, PENDING, RUNNING, CommandQueue
from arcx_auto.services.drafts import Draft, DraftGroup, DraftStore
from arcx_auto.services.executor import CommandExecutor, IntentError
from arcx_auto.services.wave_planner import plan_groups
from arcx_auto.web.server import WebOptions, serve
from tests.fixtures.fake_run import make_arcx_cfg, make_dir_map, make_index_source


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def spec(key, gds=2, cpu=4, folder=None):
    return IndexSpec(index_key=key, path="/src/%s/%s" % (folder or key, key),
                     gds_count=gds, cpu_per_case=cpu)


class PlanGroupsTest(unittest.TestCase):
    """A group is what a person selected; a wave is what may go at once."""

    def test_each_wave_carries_its_own_cfg(self):
        """Two groups can use different cfgs, and a wave is one Arcx command
        against one cfg -- so the pair has to travel with the wave.
        """
        plan = plan_groups([
            (SubmitGroup("a", "/map_a", "/cfg_a", ("1000",)), [spec("1000")]),
            (SubmitGroup("b", "/map_b", "/cfg_b", ("1001",)), [spec("1001")]),
        ], max_slots_per_wave=100)
        self.assertEqual([w.arcx_cfg for w in plan.waves], ["/cfg_a", "/cfg_b"])
        self.assertEqual([w.dir_map for w in plan.waves], ["/map_a", "/map_b"])
        self.assertEqual([w.group for w in plan.waves], ["a", "b"])

    def test_wave_numbering_is_global(self):
        """wave_001 is whatever goes out first, whichever group it came from:
        the gate releases one wave at a time and the number is that order.
        """
        plan = plan_groups([
            (SubmitGroup("a", "/m", "/c", ()), [spec("1000"), spec("1001")]),
            (SubmitGroup("b", "/m", "/c", ()), [spec("1002")]),
        ], max_slots_per_wave=8)
        self.assertEqual([w.name for w in plan.waves],
                         ["wave_001", "wave_002", "wave_003"])

    def test_a_group_is_still_split_by_the_slot_cap(self):
        """Somebody ticking fifty indices has not thought about the queue. The
        person says what belongs together; the planner says how much goes at
        once.
        """
        plan = plan_groups([
            (SubmitGroup("a", "/m", "/c", ()),
             [spec("100%d" % i) for i in range(4)]),
        ], max_slots_per_wave=8)
        self.assertEqual(len(plan.waves), 4)   # 8 slots each, cap 8

    def test_warnings_name_the_group_they_came_from(self):
        plan = plan_groups([
            (SubmitGroup("mygroup", "/m", "/c", ()), [spec("1000", gds=0)]),
        ], max_slots_per_wave=100)
        self.assertTrue(any("mygroup" in w for w in plan.warnings))


# ---------------------------------------------------------------------------
# The command queue
# ---------------------------------------------------------------------------

class GroupFolderFlagTest(unittest.TestCase):
    """The switch travels: draft -> command -> SubmitGroup -> planner."""

    def test_a_group_keeps_folders_together_by_default(self):
        self.assertTrue(SubmitGroup("g", "/m", "/c").keep_folders_together)
        self.assertTrue(DraftGroup("g", "/m", "/c").keep_folders_together)

    def test_the_flag_survives_being_written_and_read_back(self):
        group = DraftGroup("g", "/m", "/c", ["1000"],
                           keep_folders_together=False)
        draft = Draft(id="d1", groups=[group])
        again = Draft.from_dict(draft.as_dict())
        self.assertFalse(again.groups[0].keep_folders_together)

    def test_an_older_command_without_the_flag_keeps_folders_together(self):
        """A queued command written before the flag existed should get the
        behaviour the settings describe, not silently the other one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.state_root = tmp
            executor = CommandExecutor(settings)
            dir_map = make_dir_map(
                os.path.join(tmp, "dir_map"),
                {"1000": make_index_source(os.path.join(tmp, "src"), "1000",
                                           gds_count=1)})
            cfg = make_arcx_cfg(os.path.join(tmp, "arcx.cfg"))
            groups = executor._read_groups({"groups": [
                {"name": "g", "dir_map": dir_map, "arcx_cfg": cfg,
                 "index_keys": ["1000"]}]})
        self.assertTrue(groups[0].keep_folders_together)

    def test_each_group_decides_for_itself(self):
        """Only the person who made a selection knows whether its directory
        structure means anything.
        """
        loose = SubmitGroup("loose", "/m", "/c", keep_folders_together=False)
        kept = SubmitGroup("kept", "/m", "/c")
        # One index in its own folder, then two sharing one. 8 slots each,
        # cap 20: the cut lands in a different place depending on the flag.
        plan = plan_groups([
            (loose, [spec("a", folder="one"), spec("b", folder="two"),
                     spec("c", folder="two")]),
            (kept, [spec("d", folder="one"), spec("e", folder="two"),
                    spec("f", folder="two")]),
        ], max_slots_per_wave=20)
        by_group = {}
        for wave in plan.waves:
            by_group.setdefault(wave.group, []).append(wave.index_keys)
        self.assertEqual(by_group["loose"], [("a", "b"), ("c",)])
        self.assertEqual(by_group["kept"], [("d",), ("e", "f")])


class CommandQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = CommandQueue(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_command_round_trips(self):
        self.queue.submit("rerun", {"wave_dir": "/w"})
        command = self.queue.claim_next()
        self.assertEqual(command.kind, "rerun")
        self.assertEqual(command.payload["wave_dir"], "/w")

    def test_an_unknown_kind_is_refused_when_it_is_written(self):
        """Refusing here rather than at execution means a malformed queue
        cannot reach the executor at all.
        """
        with self.assertRaises(ValueError):
            self.queue.submit("rm_rf", {})

    def test_claiming_is_exclusive(self):
        """Two daemons racing: one os.replace wins, the other gets nothing."""
        self.queue.submit("rerun", {})
        first = self.queue.claim_next()
        second = self.queue.claim_next()
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_a_claimed_command_leaves_pending(self):
        self.queue.submit("rerun", {})
        self.queue.claim_next()
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(len(self.queue.list(RUNNING)), 1)

    def test_completing_moves_it_to_done_with_the_outcome(self):
        self.queue.submit("rerun", {})
        command = self.queue.claim_next()
        self.queue.complete(command, ok=False, error="it did not work")
        done = self.queue.list(DONE)
        self.assertEqual(len(done), 1)
        self.assertFalse(done[0].ok)
        self.assertIn("did not work", done[0].error)

    def test_an_interrupted_command_is_never_retried(self):
        """A submit that died half way may already have created directories
        and sent jobs. Repeating it would double-submit, so it is retired
        visibly instead.
        """
        self.queue.submit("submit", {})
        self.queue.claim_next()
        retired = self.queue.requeue_stale(older_than_sec=-1)
        self.assertEqual(len(retired), 1)
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertIn("interrupted", self.queue.list(DONE)[0].error)

    def test_an_unreadable_command_file_does_not_block_the_queue(self):
        self.queue.ensure()
        with open(os.path.join(self.queue.dir_for(PENDING), "1-bad.json"),
                  "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.queue.submit("rerun", {})
        command = self.queue.claim_next()
        self.assertIsNotNone(command, "the bad file stopped the good one")
        self.assertEqual(command.kind, "rerun")


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

class DraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DraftStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_draft_survives_a_reload(self):
        """A tool that loses a selection because somebody hit refresh is a
        tool people stop using.
        """
        draft = self.store.create()
        draft.groups.append(DraftGroup("g1", "/m", "/c", ["1000", "1001"]))
        self.store.save(draft)

        again = DraftStore(self.tmp.name).load(draft.id)
        self.assertEqual(len(again.groups), 1)
        self.assertEqual(again.groups[0].index_keys, ["1000", "1001"])

    def test_a_draft_id_cannot_escape_the_drafts_directory(self):
        path = self.store.path_for("../../etc/passwd")
        self.assertTrue(os.path.dirname(path).endswith("drafts"))
        self.assertNotIn("..", path)

    def test_an_unknown_draft_is_none_not_a_crash(self):
        self.assertIsNone(self.store.load("deadbeef"))


# ---------------------------------------------------------------------------
# The executor: validating an intent it did not write
# ---------------------------------------------------------------------------

class IntentValidationTest(unittest.TestCase):
    """A command file is the one input this system did not write itself.

    By the time it reaches WorkspaceBuilder it decides where directories get
    created, so every path is resolved and checked before anything is touched.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.state_root = os.path.join(self.tmp.name, "state")
        # run_root defaults to a relative path, which would put wave
        # directories in whatever directory the suite happens to run from.
        self.settings.run_root = os.path.join(self.tmp.name, "runs")
        self.executor = CommandExecutor(self.settings)
        self.dir_map = make_dir_map(
            os.path.join(self.tmp.name, "dir_map"),
            {"1000": make_index_source(os.path.join(self.tmp.name, "src"),
                                       "1000", gds_count=2)})
        self.cfg = make_arcx_cfg(os.path.join(self.tmp.name, "arcx.cfg"))

    def tearDown(self):
        self.tmp.cleanup()

    def _groups(self, **overrides):
        entry = {"name": "g1", "dir_map": self.dir_map,
                 "arcx_cfg": self.cfg, "index_keys": ["1000"]}
        entry.update(overrides)
        return {"groups": [entry]}

    def test_a_valid_intent_is_accepted(self):
        groups = self.executor._read_groups(self._groups())
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].index_keys, ("1000",))

    def test_a_missing_dir_map_is_refused(self):
        with self.assertRaises(IntentError) as caught:
            self.executor._read_groups(self._groups(dir_map="/nope/dir_map"))
        self.assertIn("dir_map", str(caught.exception))

    def test_a_missing_cfg_is_refused(self):
        with self.assertRaises(IntentError):
            self.executor._read_groups(self._groups(arcx_cfg="/nope/a.cfg"))

    def test_no_groups_is_refused(self):
        with self.assertRaises(IntentError):
            self.executor._read_groups({"groups": []})

    def test_a_group_with_no_index_is_refused(self):
        with self.assertRaises(IntentError):
            self.executor._read_groups(self._groups(index_keys=[]))

    def test_an_index_not_in_the_dir_map_is_refused(self):
        groups = self.executor._read_groups(self._groups(index_keys=["9999"]))
        with self.assertRaises(IntentError) as caught:
            self.executor._build_specs(groups)
        self.assertIn("9999", str(caught.exception))

    def test_a_command_that_is_not_an_object_is_refused(self):
        with self.assertRaises(IntentError):
            self.executor._read_groups({"groups": ["just a string"]})

    def test_a_bad_command_fails_without_touching_anything(self):
        queue = CommandQueue(self.settings.expanded_state_root())
        queue.submit("submit", {"run_id": "r", "groups": [
            {"name": "g", "dir_map": "/nope", "arcx_cfg": "/nope",
             "index_keys": ["1"]}]})
        command = queue.claim_next()
        self.executor.run_one(command)
        done = queue.list(DONE)[0]
        self.assertFalse(done.ok)
        self.assertFalse(os.path.exists(self.settings.expanded_run_root()))


# ---------------------------------------------------------------------------
# The web boundary
# ---------------------------------------------------------------------------

class _Ready(threading.Event):
    port = 0


class WebActionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings()
        cls.settings.state_root = os.path.join(cls.tmp.name, "state")
        cls.settings.run_root = os.path.join(cls.tmp.name, "runs")
        cls.settings.export.shared_root = os.path.join(cls.tmp.name, "shared")

        cls.dir_map = make_dir_map(
            os.path.join(cls.tmp.name, "dir_map"),
            {"1000": make_index_source(os.path.join(cls.tmp.name, "src"),
                                       "1000", gds_count=2)})
        cls.cfg = make_arcx_cfg(os.path.join(cls.tmp.name, "arcx.cfg"))

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

    def _post(self, path, data, origin=None):
        payload = urllib.parse.urlencode(data, doseq=True).encode()
        headers = {"Origin": origin} if origin is not None else {}
        return urllib.request.urlopen(urllib.request.Request(
            self.base + path, data=payload, headers=headers), timeout=10)

    def _get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=10).read().decode()

    # -- the boundary --------------------------------------------------

    def test_a_cross_site_post_is_refused(self):
        """Loopback stops the network reaching us; it does not stop a page open
        in the same browser from posting here. Without this an open tab could
        trigger a rerun that moves directories.
        """
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/submit/new", {}, origin="http://evil.example")
        self.assertEqual(caught.exception.code, 403)

    def test_a_same_origin_post_is_accepted(self):
        response = self._post("/submit/new", {}, origin=self.base)
        self.assertEqual(response.status, 200)

    def test_a_post_with_no_origin_is_accepted(self):
        """Some browsers omit Origin for same-origin form posts, and refusing
        those would break the UI for the person it is meant to serve.
        """
        response = self._post("/submit/new", {})
        self.assertEqual(response.status, 200)

    # -- the flow ------------------------------------------------------

    def _new_draft(self):
        url = self._post("/submit/new", {}).geturl()
        return url.rstrip("/").split("/")[-1].split("?")[0]

    def test_the_whole_selection_flow(self):
        draft_id = self._new_draft()

        browse = self._post("/submit/%s/browse" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg}).read().decode()
        self.assertIn("1000", browse)
        self.assertIn("slots", browse)

        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "g1"})

        page = self._get("/submit/%s" % draft_id)
        self.assertIn("g1", page)

    def test_two_groups_can_use_different_cfgs(self):
        other_cfg = make_arcx_cfg(os.path.join(self.tmp.name, "other.cfg"))
        draft_id = self._new_draft()
        for name, cfg in (("g1", self.cfg), ("g2", other_cfg)):
            self._post("/submit/%s/add" % draft_id, {
                "dir_map": self.dir_map, "arcx_cfg": cfg,
                "index_keys": ["1000"], "name": name})
        page = self._get("/submit/%s" % draft_id)
        self.assertIn("other.cfg", page)
        self.assertIn("g2", page)

    def test_the_folder_switch_is_per_group_and_shown(self):
        draft_id = self._new_draft()
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "g1"})
        page = self._get("/submit/%s" % draft_id)
        self.assertIn("one wave per folder", page)

        self._post("/submit/%s/folders" % draft_id, {"group": "0"})
        draft = DraftStore(self.settings.expanded_state_root()).load(draft_id)
        self.assertFalse(draft.groups[0].keep_folders_together)
        self.assertIn("split anywhere", self._get("/submit/%s" % draft_id))

        self._post("/submit/%s/folders" % draft_id, {"group": "0"})
        draft = DraftStore(self.settings.expanded_state_root()).load(draft_id)
        self.assertTrue(draft.groups[0].keep_folders_together)

    def test_the_folder_switch_needs_a_real_group(self):
        draft_id = self._new_draft()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/submit/%s/folders" % draft_id, {"group": "7"})
        self.assertEqual(caught.exception.code, 404)

    def test_a_group_can_be_removed(self):
        draft_id = self._new_draft()
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "doomed"})
        self._post("/submit/%s/drop" % draft_id, {"group": "0"})
        self.assertNotIn("doomed", self._get("/submit/%s" % draft_id))

    def test_a_missing_dir_map_says_so_instead_of_crashing(self):
        draft_id = self._new_draft()
        body = self._post("/submit/%s/browse" % draft_id, {
            "dir_map": "/nope/dir_map", "arcx_cfg": self.cfg}).read().decode()
        self.assertIn("not found", body)

    def test_ticking_nothing_is_not_an_empty_group(self):
        draft_id = self._new_draft()
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "name": "zzz_no_ticks"})
        page = self._get("/submit/%s" % draft_id)
        self.assertNotIn("zzz_no_ticks", page)
        self.assertIn("no group yet", page)

    # -- the gate ------------------------------------------------------

    def test_a_fatal_check_removes_the_submit_button(self):
        """A button you are allowed to press and then told off for is worse
        than no button. LSF is not reachable in this test, which is a FATAL
        preflight, so the button must not be rendered.
        """
        draft_id = self._new_draft()
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "g1"})
        body = self._post("/submit/%s/check" % draft_id, {
            "run_id": "r1", "mode": "auto"}).read().decode()
        self.assertIn("blocked", body)
        self.assertNotIn('action="/submit/%s/go"' % draft_id, body)

    def test_checking_creates_nothing(self):
        draft_id = self._new_draft()
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "g1"})
        self._post("/submit/%s/check" % draft_id, {"run_id": "r2"})
        self.assertFalse(
            os.path.exists(os.path.join(self.settings.expanded_run_root(), "r2")))

    # -- queueing ------------------------------------------------------

    def test_submitting_queues_an_intent_and_does_not_act(self):
        draft_id = self._new_draft()
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "g1"})
        self._post("/submit/%s/go" % draft_id, {"confirm": draft_id})

        queue = CommandQueue(self.settings.expanded_state_root())
        pending = queue.list(PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, "submit")
        # Nothing was created: the daemon has not run.
        self.assertFalse(os.path.exists(self.settings.expanded_run_root()))

    def test_a_mismatched_confirmation_is_refused(self):
        draft_id = self._new_draft()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/submit/%s/go" % draft_id, {"confirm": "something else"})
        self.assertEqual(caught.exception.code, 400)

    def test_the_queue_page_says_when_nothing_is_consuming_it(self):
        draft_id = self._new_draft()
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "g1"})
        self._post("/submit/%s/go" % draft_id, {"confirm": draft_id})
        self.assertIn("no daemon is running", self._get("/commands"))


class AutoGroupWebTest(unittest.TestCase):
    """Auto grouping from the browser: propose, edit, add.

    The proposal is only worth having if it can be overridden, so half of
    these are about the override.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings()
        cls.settings.state_root = os.path.join(cls.tmp.name, "state")
        cls.settings.run_root = os.path.join(cls.tmp.name, "runs")
        cls.settings.export.shared_root = os.path.join(cls.tmp.name, "shared")
        cls.settings.auto_group.corner_aliases = {"Cbest_T": ["cbt"]}

        cls.work = os.path.join(cls.tmp.name, "work")
        os.makedirs(cls.work)
        make_arcx_cfg(os.path.join(cls.work, "chipA_typical.cfg"))
        make_arcx_cfg(os.path.join(cls.work, "chipA_cbt.cfg"))

        src = os.path.join(cls.tmp.name, "src")
        entries = {
            "1000": make_index_source(
                os.path.join(src, "corner_v2g", "Cbest_T"), "1000",
                gds_count=2),
            "1001": make_index_source(os.path.join(src, "plain"), "1001",
                                      gds_count=2),
            "1002": make_index_source(
                os.path.join(src, "corner_v2g", "Whot_T"), "1002",
                gds_count=2),
        }
        cls.entries = entries
        make_dir_map(os.path.join(cls.work, "dir_map"), entries)

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
        payload = urllib.parse.urlencode(data, doseq=True).encode()
        return urllib.request.urlopen(urllib.request.Request(
            self.base + path, data=payload), timeout=10)

    def _get(self, path):
        return urllib.request.urlopen(
            self.base + path, timeout=10).read().decode()

    def _new_draft(self):
        return self._post("/submit/new", {}).geturl().rstrip(
            "/").split("/")[-1].split("?")[0]

    def _plan_page(self, draft_id):
        return self._post("/submit/%s/autoplan" % draft_id,
                          {"directory": self.work}).read().decode()

    def test_the_proposal_names_every_index_and_its_reason(self):
        body = self._plan_page(self._new_draft())
        for key in ("1000", "1001", "1002"):
            self.assertIn(key, body)
        self.assertIn("chipA_cbt.cfg", body)
        self.assertIn("chipA_typical.cfg", body)
        self.assertIn("no cfg", body)          # 1002 has no Whot_T cfg

    def test_adding_the_proposal_creates_one_group_per_cfg(self):
        draft_id = self._new_draft()
        self._plan_page(draft_id)
        self._post("/submit/%s/autoadd" % draft_id, {
            "directory": self.work,
            "index_keys": ["1000", "1001"],
            "cfg_1000": os.path.join(self.work, "chipA_cbt.cfg"),
            "cfg_1001": os.path.join(self.work, "chipA_typical.cfg"),
        })
        draft = DraftStore(self.settings.expanded_state_root()).load(draft_id)
        self.assertEqual(len(draft.groups), 2)
        by_name = {g.name: g for g in draft.groups}
        self.assertEqual(by_name["cbt"].index_keys, ["1000"])
        self.assertEqual(by_name["typical"].index_keys, ["1001"])
        self.assertTrue(all(g.dir_map.endswith("dir_map")
                            for g in draft.groups))

    def test_the_proposal_can_be_overridden(self):
        """An index the tool left out can be put somewhere by hand, and one
        it proposed can be moved. The proposal is a suggestion.
        """
        draft_id = self._new_draft()
        self._plan_page(draft_id)
        self._post("/submit/%s/autoadd" % draft_id, {
            "directory": self.work,
            "index_keys": ["1002"],
            "cfg_1002": os.path.join(self.work, "chipA_typical.cfg"),
        })
        draft = DraftStore(self.settings.expanded_state_root()).load(draft_id)
        self.assertEqual([g.index_keys for g in draft.groups], [["1002"]])

    def test_a_cfg_outside_the_directory_is_refused(self):
        """The cfg a wave runs against cannot be whatever a form field says.

        The directory is read again on submit, and anything not in it is
        dropped rather than trusted.
        """
        draft_id = self._new_draft()
        self._plan_page(draft_id)
        body = self._post("/submit/%s/autoadd" % draft_id, {
            "directory": self.work,
            "index_keys": ["1000"],
            "cfg_1000": "/etc/passwd",
        }).read().decode()
        draft = DraftStore(self.settings.expanded_state_root()).load(draft_id)
        self.assertEqual(draft.groups, [])
        self.assertIn("skip", body)

    def test_an_unknown_index_key_is_dropped(self):
        draft_id = self._new_draft()
        self._plan_page(draft_id)
        self._post("/submit/%s/autoadd" % draft_id, {
            "directory": self.work,
            "index_keys": ["9999"],
            "cfg_9999": os.path.join(self.work, "chipA_typical.cfg"),
        })
        draft = DraftStore(self.settings.expanded_state_root()).load(draft_id)
        self.assertEqual(draft.groups, [])

    def test_a_directory_without_a_dir_map_says_so(self):
        draft_id = self._new_draft()
        body = self._post("/submit/%s/autoplan" % draft_id,
                          {"directory": self.tmp.name}).read().decode()
        self.assertIn("dir_map", body)

    def test_the_picker_offers_the_directory_that_has_a_dir_map(self):
        draft_id = self._new_draft()
        body = self._get("/submit/%s/auto?path=%s"
                         % (draft_id, urllib.parse.quote(self.work)))
        self.assertIn("group everything in this directory", body)

    def test_the_picker_says_when_there_is_no_dir_map_here(self):
        draft_id = self._new_draft()
        body = self._get("/submit/%s/auto?path=%s"
                         % (draft_id, urllib.parse.quote(self.tmp.name)))
        self.assertIn("No <code>dir_map</code> here", body)

    def test_grouping_creates_nothing_on_disk(self):
        before = sorted(os.listdir(self.work))
        draft_id = self._new_draft()
        self._plan_page(draft_id)
        self.assertEqual(sorted(os.listdir(self.work)), before)


class WorkspaceTargetingTest(unittest.TestCase):
    """Two directories, two daemons, one browser.

    With one workspace everything works by accident: every default is the only
    run_root there is. Two is where a submission can silently land on the
    wrong disk, so these tests are about which one it goes to.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings()
        cls.settings.state_root = os.path.join(cls.tmp.name, "state")
        # The web server's own run_root: a third directory, so a test that
        # passes by falling back to it is a test that failed.
        cls.settings.run_root = os.path.join(cls.tmp.name, "web_runs")
        cls.settings.export.shared_root = os.path.join(cls.tmp.name, "shared")

        cls.dir_map = make_dir_map(
            os.path.join(cls.tmp.name, "dir_map"),
            {"1000": make_index_source(os.path.join(cls.tmp.name, "src"),
                                       "1000", gds_count=2)})
        cls.cfg = make_arcx_cfg(os.path.join(cls.tmp.name, "arcx.cfg"))

        cls.project_a = os.path.join(cls.tmp.name, "projA")
        cls.project_b = os.path.join(cls.tmp.name, "projB")
        for path in (cls.project_a, cls.project_b):
            os.makedirs(path, exist_ok=True)
        cls.root_a = os.path.join(cls.project_a, "arcx_runs")
        cls.root_b = os.path.join(cls.project_b, "arcx_runs")

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

    def setUp(self):
        registry = workspaces.registry_dir(self.settings.expanded_state_root())
        if os.path.isdir(registry):
            for name in os.listdir(registry):
                os.unlink(os.path.join(registry, name))

    def _post(self, path, data):
        payload = urllib.parse.urlencode(data, doseq=True).encode()
        return urllib.request.urlopen(
            urllib.request.Request(self.base + path, data=payload), timeout=10)

    def _get(self, path):
        return urllib.request.urlopen(
            self.base + path, timeout=10).read().decode()

    def _new_draft(self):
        url = self._post("/submit/new", {}).geturl()
        return url.rstrip("/").split("/")[-1].split("?")[0]

    def _register(self, run_id, run_root):
        return workspaces.register(self.settings.expanded_state_root(),
                                   run_id, run_root)

    def _queued_payload(self, draft_id):
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "g1"})
        self._post("/submit/%s/go" % draft_id, {"confirm": draft_id})
        queue = CommandQueue(self.settings.expanded_state_root())
        pending = queue.list(PENDING)
        self.addCleanup(lambda: [queue.cancel(c.id) for c in queue.list(PENDING)])
        return pending[-1].payload

    def test_one_live_workspace_is_used_without_asking(self):
        self._register("a", self.root_a)
        payload = self._queued_payload(self._new_draft())
        self.assertEqual(payload["run_root"], self.root_a)

    def test_with_two_workspaces_the_page_shows_both(self):
        self._register("a", self.root_a)
        self._register("b", self.root_b)
        page = self._get("/submit/%s" % self._new_draft())
        self.assertIn(self.root_a, page)
        self.assertIn(self.root_b, page)
        self.assertIn("workspace", page)

    def test_a_workspace_can_be_chosen_and_the_choice_is_kept(self):
        self._register("a", self.root_a)
        self._register("b", self.root_b)
        draft_id = self._new_draft()
        self._post("/submit/%s/workspace" % draft_id,
                   {"run_root": self.root_b})
        payload = self._queued_payload(draft_id)
        self.assertEqual(payload["run_root"], self.root_b)

    def test_an_unregistered_run_root_is_refused(self):
        """The form must not be able to name any directory on the disk: it
        decides where wave directories get created.
        """
        self._register("a", self.root_a)
        draft_id = self._new_draft()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/submit/%s/workspace" % draft_id,
                       {"run_root": os.path.join(self.tmp.name, "elsewhere")})
        self.assertEqual(caught.exception.code, 400)

    def test_a_dead_workspace_is_not_chosen_silently(self):
        """One live and one stale is still one choice, and it is the live
        one -- but the dead one stays visible, because "the daemon I started
        here is gone" is something to see rather than to be protected from.
        """
        stale = time.time() - 10 * workspaces.STALE_AFTER_SEC
        workspaces.register(self.settings.expanded_state_root(), "old",
                            self.root_b, now=stale)
        self._register("a", self.root_a)
        payload = self._queued_payload(self._new_draft())
        self.assertEqual(payload["run_root"], self.root_a)
        self.assertIn(self.root_b, self._get("/"))

    def test_the_preflight_page_says_when_nobody_is_watching(self):
        stale = time.time() - 10 * workspaces.STALE_AFTER_SEC
        workspaces.register(self.settings.expanded_state_root(), "old",
                            self.root_b, now=stale)
        draft_id = self._new_draft()
        self._post("/submit/%s/add" % draft_id, {
            "dir_map": self.dir_map, "arcx_cfg": self.cfg,
            "index_keys": ["1000"], "name": "g1"})
        body = self._post("/submit/%s/check" % draft_id,
                          {"run_id": "r1"}).read().decode()
        self.assertIn("No daemon is watching", body)

    def test_the_home_page_lists_which_directory_each_daemon_owns(self):
        self._register("a", self.root_a)
        self._register("b", self.root_b)
        page = self._get("/")
        self.assertIn("workspaces", page)
        self.assertIn(self.root_a, page)
        self.assertIn(self.root_b, page)

    def test_the_ui_own_workspace_wins_when_it_is_one_of_them(self):
        """`arcx-auto start` in one directory and bare daemons in the others:
        the UI's own root is a workspace, and it is the one being looked at.
        """
        self._register("web", self.settings.expanded_run_root())
        self._register("a", self.root_a)
        self._register("b", self.root_b)
        payload = self._queued_payload(self._new_draft())
        self.assertEqual(payload["run_root"],
                         self.settings.expanded_run_root())

    def test_with_no_workspace_the_page_says_how_to_start_one(self):
        page = self._get("/submit/%s" % self._new_draft())
        self.assertIn("arcx-auto daemon", page)


class ReadOnlyWebTest(unittest.TestCase):
    """--read-only turns the UI back into a display."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ready = _Ready()
        threading.Thread(
            target=serve,
            args=(WebOptions(state_root=self.tmp.name, port=0, refresh_sec=0,
                             allow_actions=False),
                  self.ready),
            daemon=True).start()
        self.ready.wait(5)
        self.base = "http://127.0.0.1:%d" % self.ready.port

    def tearDown(self):
        httpd = getattr(self.ready, "httpd", None)
        if httpd is not None:
            httpd.shutdown()
        self.tmp.cleanup()

    def test_every_action_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(
                self.base + "/submit/new", data=b""), timeout=10)
        self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
