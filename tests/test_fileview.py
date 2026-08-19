"""Reading a run's own files: confinement, bounds, and what the page shows.

The confinement tests are the point of this file. This is the first endpoint
that takes a path from the request, and the difference between "reads a log"
and "reads anything on the machine" is one missing check.
"""

import os
import tempfile
import unittest

from arcx_auto.services.fileview import (
    DEFAULT_MAX_BYTES,
    list_case_files,
    read_view,
    within,
)
from arcx_auto.web import pages


def write(path, text="x", mode="w"):
    with open(path, mode) as handle:
        handle.write(text)
    return path


class WithinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "runs")
        os.makedirs(os.path.join(self.root, "wave_001"))
        self.addCleanup(self.tmp.cleanup)

    def test_a_file_inside_the_root_is_allowed(self):
        target = os.path.join(self.root, "wave_001", "log")
        write(target, "hi")
        self.assertTrue(within(target, [self.root]))

    def test_the_root_itself_is_allowed(self):
        self.assertTrue(within(self.root, [self.root]))

    def test_a_file_outside_the_root_is_refused(self):
        outside = os.path.join(self.tmp.name, "secret")
        write(outside, "no")
        self.assertFalse(within(outside, [self.root]))

    def test_dot_dot_does_not_escape(self):
        attack = os.path.join(self.root, "..", "secret")
        self.assertFalse(within(attack, [self.root]))

    def test_a_symlink_planted_inside_does_not_escape(self):
        """The reason realpath comes first.

        Anyone who can write into a run folder can drop a symlink there. A
        check on the literal path would see something under the root and say
        yes.
        """
        outside = os.path.join(self.tmp.name, "secret")
        write(outside, "no")
        link = os.path.join(self.root, "wave_001", "innocent.log")
        os.symlink(outside, link)
        self.assertFalse(within(link, [self.root]))

    def test_a_prefix_that_is_not_a_directory_boundary_is_refused(self):
        """<root>_other must not pass just because it starts with <root>."""
        sibling = self.root + "_other"
        os.makedirs(sibling)
        target = os.path.join(sibling, "log")
        write(target, "no")
        self.assertFalse(within(target, [self.root]))

    def test_no_roots_denies_everything(self):
        """Fails closed. A misconfigured server shows nothing, not anything."""
        target = os.path.join(self.root, "wave_001", "log")
        write(target, "hi")
        self.assertFalse(within(target, []))
        self.assertFalse(within(target, [""]))


class ReadViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "run.log")
        with open(self.path, "w") as handle:
            for number in range(1, 1001):
                handle.write("line %d\n" % number)

    def test_tail_shows_the_end(self):
        view = read_view(self.path, mode="tail", lines=10)
        self.assertTrue(view.ok)
        self.assertIn("line 1000", view.text)
        self.assertNotIn("line 990\n", view.text)
        self.assertEqual(view.lines_shown, 10)
        self.assertTrue(view.truncated)

    def test_head_shows_the_start(self):
        view = read_view(self.path, mode="head", lines=10)
        self.assertIn("line 1", view.text)
        self.assertNotIn("line 999", view.text)
        self.assertTrue(view.truncated)

    def test_a_short_file_is_not_truncated(self):
        path = os.path.join(self.tmp.name, "short.log")
        write(path, "only\ntwo\n")
        view = read_view(path, mode="tail", lines=100)
        self.assertFalse(view.truncated)
        self.assertEqual(view.text, "only\ntwo")

    def test_an_empty_file_reads_cleanly(self):
        path = write(os.path.join(self.tmp.name, "empty.log"), "")
        view = read_view(path)
        self.assertTrue(view.ok)
        self.assertEqual(view.text, "")
        self.assertEqual(view.lines_shown, 0)

    def test_a_missing_file_reports_the_error_rather_than_raising(self):
        view = read_view(os.path.join(self.tmp.name, "nope"))
        self.assertFalse(view.ok)
        self.assertIn("No such file", view.error)

    def test_a_directory_is_not_a_file(self):
        view = read_view(self.tmp.name)
        self.assertFalse(view.ok)
        self.assertIn("directory", view.error)

    def test_a_huge_file_is_never_read_whole(self):
        """A netlist is routinely hundreds of megabytes. Reading one to show
        the last twenty lines would take the server down with it.
        """
        path = os.path.join(self.tmp.name, "big.spf")
        with open(path, "wb") as handle:
            handle.write(b"x" * (2 * DEFAULT_MAX_BYTES))
            handle.write(b"\nlast line\n")
        view = read_view(path, mode="tail", lines=5)
        self.assertIn("last line", view.text)
        self.assertLessEqual(len(view.text), DEFAULT_MAX_BYTES + 4096)
        self.assertTrue(view.truncated)

    def test_bytes_that_are_not_utf8_do_not_break_the_page(self):
        path = os.path.join(self.tmp.name, "binary.log")
        write(path, b"before\n\xff\xfe\nafter\n", "wb")
        view = read_view(path)
        self.assertTrue(view.ok)
        self.assertIn("after", view.text)


class ListCaseFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = os.path.join(self.tmp.name, "NMOS_1")
        deep = os.path.join(self.case, "blk_qcap", "work_qcap")
        os.makedirs(deep)
        write(os.path.join(deep, "blk.spf"), "* QuickCap\n")
        write(os.path.join(self.case, "top.log"), "hi\n")
        os.makedirs(os.path.join(self.case, ".hidden"))
        write(os.path.join(self.case, ".hidden", "junk"), "x")

    def test_files_are_flattened_with_relative_names(self):
        names = [e.name for e in list_case_files(self.case)]
        self.assertIn("top.log", names)
        self.assertIn(os.path.join("blk_qcap", "work_qcap", "blk.spf"), names)

    def test_dot_directories_are_skipped(self):
        names = [e.name for e in list_case_files(self.case)]
        self.assertFalse([n for n in names if n.startswith(".")])

    def test_the_count_is_capped(self):
        """An intermediate database can hold thousands of files, and a page
        listing all of them is no more useful than none.
        """
        many = os.path.join(self.case, "db")
        os.makedirs(many)
        for number in range(50):
            write(os.path.join(many, "f%d" % number), "x")
        self.assertEqual(len(list_case_files(self.case, limit=10)), 10)

    def test_depth_is_capped(self):
        path = self.case
        for level in range(8):
            path = os.path.join(path, "d%d" % level)
        os.makedirs(path)
        write(os.path.join(path, "deep.txt"), "x")
        names = [e.name for e in list_case_files(self.case, max_depth=2)]
        self.assertNotIn("deep.txt", [os.path.basename(n) for n in names])

    def test_a_missing_case_dir_is_empty_not_an_error(self):
        self.assertEqual(list_case_files(os.path.join(self.tmp.name, "no")), [])


class EvidenceLinkTest(unittest.TestCase):
    """The paths an issue names should be somewhere to go, not text to copy."""

    def test_paths_are_pulled_out_of_nested_evidence(self):
        evidence = {"netlists": [{"path": "blk_qcap/work_qcap/blk.spf",
                                  "first_line": "* nothing"}],
                    "found": ["a", "b"]}
        self.assertEqual(pages._evidence_paths(evidence),
                         ["blk_qcap/work_qcap/blk.spf"])

    def test_a_top_level_path_counts_too(self):
        self.assertEqual(pages._evidence_paths({"path": "/runs/x/NMOS_1"}),
                         ["/runs/x/NMOS_1"])

    def test_no_paths_means_no_links(self):
        self.assertEqual(pages._evidence_paths({"silent_sec": 900}), [])

    def test_a_relative_path_is_resolved_against_the_case_dir(self):
        html = pages._evidence_links(
            {"evidence": {"netlists": [{"path": "blk/net.spf"}]}},
            "/runs/w/1001_run/NMOS_1", back="/run/x")
        self.assertIn("%2Fruns%2Fw%2F1001_run%2FNMOS_1%2Fblk%2Fnet.spf", html)

    def test_without_a_case_dir_nothing_is_linked(self):
        html = pages._evidence_links(
            {"evidence": {"path": "blk/net.spf"}}, "", back="/")
        self.assertEqual(html, "")


class FilterTest(unittest.TestCase):
    """Feature: an index of 300 cases where 4 are wrong is a table nobody
    reads. The 4 are the content of the page.
    """

    CASES = [
        {"case_id": "A", "state": "DONE"},
        {"case_id": "B", "state": "FAILED"},
        {"case_id": "C", "state": "RUNNING"},
        {"case_id": "D", "state": "LOST"},
    ]

    def ids(self, show):
        return [c["case_id"] for c in pages._matching(self.CASES, show)]

    def test_no_filter_shows_everything(self):
        self.assertEqual(self.ids(""), ["A", "B", "C", "D"])
        self.assertEqual(self.ids("all"), ["A", "B", "C", "D"])

    def test_one_state(self):
        self.assertEqual(self.ids("FAILED"), ["B"])

    def test_attention_gathers_every_state_that_needs_a_person(self):
        self.assertEqual(self.ids("attention"), ["B", "D"])

    def test_an_unknown_filter_shows_everything_not_nothing(self):
        """A stale or hand-edited URL must not make a case table look empty.
        Empty reads exactly like "nothing is wrong", which is the one wrong
        answer this whole tool exists to prevent.
        """
        self.assertEqual(self.ids("NONSENSE"), ["A", "B", "C", "D"])


if __name__ == "__main__":
    unittest.main()
