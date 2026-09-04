#!/usr/bin/env python3
# Copyright 2026 RDK Management — Apache-2.0 (see build_attribution.py header).
"""Unit tests for build_attribution.py: the tier-1 classifier (pr / environment /
ambiguous / sibling-mesh / link error), make-target parsing, DB matching, and the
three render verdict shapes. Tier-2 recompile-on-base is exercised end-to-end on a
scratch PR at the runner (documented in the plan), not here."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import build_attribution as b  # noqa: E402


class ClassifyPath(unittest.TestCase):
    def test_pr_when_changed_onewifi_file(self):
        self.assertEqual(
            b.classify_path("OneWifi/source/foo.c", {"source/foo.c"}, False), "pr")

    def test_sibling_hal_is_environment(self):
        self.assertEqual(
            b.classify_path("rdk-wifi-hal/x.c", {"source/foo.c"}, False), "environment")

    def test_sibling_mesh_under_checkout_root_is_environment(self):
        # The summary sed leaves mesh (not in its alternation) as
        # OneWifi/easymesh_project/<sib>/…, which must NOT read as a pr path.
        self.assertEqual(
            b.classify_path("OneWifi/easymesh_project/unified-wifi-mesh/src/x.c",
                            {"source/foo.c"}, False), "environment")

    def test_sibling_downgraded_when_pr_changed_header(self):
        self.assertEqual(
            b.classify_path("rdk-wifi-hal/x.c", {"include/a.h"}, True), "ambiguous")

    def test_onewifi_file_not_changed_is_ambiguous(self):
        self.assertEqual(
            b.classify_path("OneWifi/source/other.c", {"source/foo.c"}, False), "ambiguous")

    def test_absolute_toolchain_path_is_environment(self):
        self.assertEqual(
            b.classify_path("/usr/include/stdio.h", {"source/foo.c"}, False), "environment")


class ParseMake(unittest.TestCase):
    def test_targets_deduped_and_o_only(self):
        log = (
            "make: *** [makefile:412: source/foo.o] Error 1\n"
            "make[1]: *** [Makefile:88: bar.o] Error 2\n"
            "*** [source/foo.o] Error 1\n"      # dup of first target
            "make: *** [all] Error 2\n"         # not a TU
        )
        self.assertEqual(b.parse_make_targets(log), ["source/foo.o", "bar.o"])

    def test_plain_target(self):
        self.assertEqual(b.parse_make_targets("*** [foo.o] Error 1\n"), ["foo.o"])


DB = [{"file": "/w/OneWifi/source/foo.c", "directory": "/w/OneWifi",
       "arguments": ["cc", "-c", "source/foo.c", "-o", "source/foo.o", "-Wall"]}]


class DbMatch(unittest.TestCase):
    def test_object_lookup_matches_on_output_not_source(self):
        # make reports the .o target; the entry must be found by its -o output.
        e = b.entry_for_object(DB, "source/foo.o")
        self.assertIsNotNone(e)
        self.assertEqual(e["file"], "/w/OneWifi/source/foo.c")

    def test_object_lookup_boundary(self):
        # bare basename matches at the '/' boundary (source/foo.o endswith /foo.o) —
        # make normally reports the full target, so exact lookups dominate anyway.
        self.assertIsNotNone(b.entry_for_object(DB, "foo.o"))
        # a non-boundary suffix must NOT match (would recompile the wrong TU).
        self.assertIsNone(b.entry_for_object(DB, "notfoo.o"))
        self.assertIsNone(b.entry_for_object([], "x.o"))

    def test_db_args_strips_c_and_o(self):
        cwd, args = b.db_args(DB, "source/foo.c")
        self.assertEqual(cwd, "/w/OneWifi")
        self.assertNotIn("-c", args)
        self.assertNotIn("-o", args)
        self.assertIn("-Wall", args)


class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc


class JudgeTu(unittest.TestCase):
    """The three tier-2 outcomes, driven through the run_cmd seam. This is the
    test that catches both the .o-lookup bug and the deleted-source misread."""
    def setUp(self):
        self._saved = b.run_cmd
        self.calls = []

    def tearDown(self):
        b.run_cmd = self._saved

    def _stub(self, rc):
        def f(cmd, cwd):
            self.calls.append((cmd, cwd))
            return _FakeProc(rc)
        return f

    def test_source_missing_after_revert_is_pr(self):
        # PR ADDED the file: git apply -R removed it -> failure is the PR's.
        b.run_cmd = self._stub(0)   # must not be consulted
        v = b.judge_tu("/no/such/dir", ["cc", "x.c"], "x.c")
        self.assertEqual(v, "pr")
        self.assertEqual(self.calls, [])   # never compiled a missing file

    def test_fails_on_base_is_environment(self):
        import tempfile
        d = tempfile.mkdtemp()
        open(os.path.join(d, "a.c"), "w").close()
        b.run_cmd = self._stub(1)
        self.assertEqual(b.judge_tu(d, ["cc", "a.c"], "a.c"), "environment")

    def test_compiles_on_base_is_pr(self):
        import tempfile
        d = tempfile.mkdtemp()
        open(os.path.join(d, "a.c"), "w").close()
        b.run_cmd = self._stub(0)
        self.assertEqual(b.judge_tu(d, ["cc", "a.c"], "a.c"), "pr")


class Tier1LinkErrors(unittest.TestCase):
    def test_link_symbol_in_diff_is_pr(self):
        errs = ["undefined reference to `my_new_symbol'"]
        rows = b.tier1(errs, set(), False, "+int my_new_symbol(void){return 0;}\n")
        self.assertEqual(rows[0][1], "pr")

    def test_link_symbol_absent_is_environment_likely(self):
        rows = b.tier1(["undefined reference to `foreign_sym'"], set(), False, "unrelated diff")
        self.assertEqual(rows[0][1], "environment-likely")


class Render(unittest.TestCase):
    def test_not_caused_by_pr(self):
        md, notice = b.render([], {"a.o": "environment", "b.o": "environment"}, "",
                              {"rdk-wifi-hal"})
        self.assertIn("not caused by this PR", md)
        self.assertIn("not caused by this PR", notice)

    def test_introduced_by_pr(self):
        md, notice = b.render([], {"a.o": "pr"}, "", {"OneWifi"})
        self.assertIn("introduced by this PR", md)

    def test_inconclusive_with_table(self):
        rows = [("rdk-wifi-hal/x.c", "environment"), ("OneWifi/source/foo.c", "pr")]
        md, notice = b.render(rows, {}, "", set())
        self.assertIn("inconclusive", md)
        self.assertIn("| path / symbol | heuristic |", md)

    def test_restore_warning_is_inconclusive(self):
        md, _ = b.render([], {"a.o": "environment"}, "WARNING: could not restore", set())
        self.assertIn("inconclusive", md)

    def test_unknown_tu_caveat_when_not_caused(self):
        md, _ = b.render([], {"a.o": "environment", "b.o": "unknown"}, "", {"rdk-wifi-hal"})
        self.assertIn("not caused by this PR", md)
        self.assertIn("not in the compile DB", md)   # honest about the unjudged TU


if __name__ == "__main__":
    unittest.main()
