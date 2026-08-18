"""SnapshotStore and atomic writes.

The core property (architecture decision 2): a damaged cache must silently
start over and never stop monitoring -- the filesystem is the truth, and the
cache only accumulates stall timing.
"""

import json
import os
import tempfile
import unittest

from arcx_auto.adapters.store import SnapshotStore
from arcx_auto.domain.enums import CaseState, LsfState
from arcx_auto.domain.models import CaseSnapshot, IndexRunSnapshot
from arcx_auto.util.atomic import append_jsonl, atomic_write_json, read_json


def snapshot(key="1000", state=CaseState.RUNNING):
    return IndexRunSnapshot(
        index_key=key, run_folder="/run/%s" % key, updated_at=123.0,
        cases={
            "case1": CaseSnapshot(
                case_id="case1", state=state, entered_state_at=100.0,
                last_progress_at=110.0, last_progress_size=4096,
                lsf_job_id="42", lsf_state=LsfState.RUN,
            )
        },
    )


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load(self):
        store = SnapshotStore(self.path)
        store.save({"/run/1000": snapshot()})
        loaded = store.load()
        self.assertIn("/run/1000", loaded)
        case = loaded["/run/1000"].cases["case1"]
        self.assertEqual(case.state, CaseState.RUNNING)
        self.assertEqual(case.last_progress_size, 4096)
        self.assertEqual(case.lsf_state, LsfState.RUN)

    def test_missing_file_returns_empty(self):
        self.assertEqual(SnapshotStore(self.path).load(), {})

    def test_corrupt_file_returns_empty_not_exception(self):
        with open(self.path, "w") as handle:
            handle.write("{ this is not json")
        self.assertEqual(SnapshotStore(self.path).load(), {})

    def test_schema_mismatch_discards_cache(self):
        """A version mismatch discards everything: it is only a cache, so
        migration logic would be dead weight.
        """
        with open(self.path, "w") as handle:
            json.dump({"schema_version": 99, "index_runs": {"x": {}}}, handle)
        self.assertEqual(SnapshotStore(self.path).load(), {})

    def test_partial_corruption_skips_bad_entries(self):
        store = SnapshotStore(self.path)
        store.save({"/run/1000": snapshot()})
        raw = read_json(self.path)
        raw["index_runs"]["/run/bad"] = {"missing": "fields"}
        atomic_write_json(self.path, raw)
        loaded = store.load()
        self.assertIn("/run/1000", loaded)
        self.assertNotIn("/run/bad", loaded)


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_read(self):
        path = os.path.join(self.tmp.name, "a", "b", "x.json")
        atomic_write_json(path, {"k": [1, 2, 3]})
        self.assertEqual(read_json(path), {"k": [1, 2, 3]})

    def test_no_temp_files_left_behind(self):
        path = os.path.join(self.tmp.name, "x.json")
        atomic_write_json(path, {"a": 1})
        leftovers = [n for n in os.listdir(self.tmp.name) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_overwrite_replaces_content(self):
        path = os.path.join(self.tmp.name, "x.json")
        atomic_write_json(path, {"v": 1})
        atomic_write_json(path, {"v": 2})
        self.assertEqual(read_json(path), {"v": 2})

    def test_append_jsonl(self):
        path = os.path.join(self.tmp.name, "events.jsonl")
        append_jsonl(path, {"a": 1})
        append_jsonl(path, {"a": 2})
        with open(path, encoding="utf-8") as handle:
            lines = [json.loads(ln) for ln in handle if ln.strip()]
        self.assertEqual([r["a"] for r in lines], [1, 2])


if __name__ == "__main__":
    unittest.main()
