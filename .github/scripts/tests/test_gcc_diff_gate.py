#!/usr/bin/env python3
# Copyright 2026 RDK Management — Apache-2.0 (see gcc_diff_gate.py header).
"""Unit tests for gcc_diff_gate.py: build_inline (ADVISORY-only inline candidates,
column dedupe, dropped count), recompile_cmd (-Os insertion, bare -Wno-error, C-only
flags dropped on C++ TUs) and write_inline (envelope shape, skipped vs ok, no-op when
INLINE_JSON is unset)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gcc_diff_gate as g  # noqa: E402


class BuildInline(unittest.TestCase):
    def test_advisory_inlined(self):
        advis = ["source/b.c:3:1: warning: value computed is not used [-Wunused-value]"]
        inline, dropped = g.build_inline(advis)
        self.assertEqual(dropped, 0)
        self.assertEqual(inline[0]["path"], "source/b.c")
        self.assertEqual(inline[0]["line"], 3)
        self.assertIn("(advisory)", inline[0]["body"])
        self.assertIn("-Wunused-value", inline[0]["body"])
        self.assertTrue(all(c["side"] == "RIGHT" for c in inline))

    def test_gated_findings_are_not_inlined(self):
        # build_inline takes ONLY the advisory list now; gated findings are summary-only.
        # A gate-class line passed as advisory would still render, so the routing is
        # enforced at the call site (main passes `advis`), not here — assert the arity:
        with self.assertRaises(TypeError):
            g.build_inline(["source/a.c:10:5: warning: vla [-Wvla]"], [])

    def test_dedupes_across_columns(self):
        # Same finding at two columns (gcc macro expansion) -> one comment.
        advis = ["source/a.c:10:5: warning: unused var 'x' [-Wunused-variable]",
                 "source/a.c:10:9: warning: unused var 'x' [-Wunused-variable]"]
        inline, dropped = g.build_inline(advis)
        self.assertEqual(len(inline), 1)
        self.assertEqual(dropped, 0)

    def test_drops_unparsable(self):
        inline, dropped = g.build_inline(["no tag here"])
        self.assertEqual(inline, [])
        self.assertEqual(dropped, 1)


class RecompileCmd(unittest.TestCase):
    """recompile_cmd reads module globals (ALL_FLAGS/C_ONLY/OPT) at call time; set them
    explicitly since the test env leaves GATE/ADVISORY (hence ALL_FLAGS) empty."""

    def setUp(self):
        self._saved = (g.ALL_FLAGS, g.C_ONLY, g.OPT)
        g.ALL_FLAGS = ["-Wvla", "-Wint-conversion", "-Wunused-value"]
        g.C_ONLY = {"-Wint-conversion"}
        g.OPT = "-Os"

    def tearDown(self):
        g.ALL_FLAGS, g.C_ONLY, g.OPT = self._saved

    def test_c_tu_keeps_flags_strips_werror_inserts_opt(self):
        # DB args carry -Werror + a per-class promotion -Os would trip; both must be gone.
        args = ["gcc", "-Werror", "-Werror=maybe-uninitialized", "foo.c"]
        cmd = g.recompile_cmd(args, "source/foo.c")
        self.assertIn("-Os", cmd)                        # OPT inserted
        self.assertNotIn("-Werror", cmd)                 # bare -Werror stripped
        self.assertNotIn("-Werror=maybe-uninitialized", cmd)  # per-class promotion stripped
        self.assertIn("-Wint-conversion", cmd)           # C-only kept on a .c TU
        self.assertIn("-Wvla", cmd)
        # candidate demotion kept — covers gcc-14 default-error classes the strip misses
        self.assertIn("-Wno-error=int-conversion", cmd)
        self.assertEqual(cmd[-3:], ["-Wno-error=vla", "-Wno-error=int-conversion",
                                    "-Wno-error=unused-value"])
        self.assertIn(os.devnull, cmd)

    def test_cxx_tu_drops_c_only_flags(self):
        cmd = g.recompile_cmd(["g++", "-Werror=return-type", "foo.cpp"], "source/foo.cpp")
        self.assertNotIn("-Wint-conversion", cmd)        # C-only dropped on .cpp
        self.assertNotIn("-Wno-error=int-conversion", cmd)
        self.assertNotIn("-Werror=return-type", cmd)     # promotion stripped
        self.assertIn("-Wvla", cmd)                      # non-C-only kept
        self.assertIn("-Os", cmd)

    def test_no_opt_when_unset(self):
        g.OPT = ""
        cmd = g.recompile_cmd(["gcc", "-Werror=format", "foo.c"], "source/foo.c")
        self.assertNotIn("-Os", cmd)
        self.assertNotIn("-Werror=format", cmd)          # still strips promotions


class WriteInline(unittest.TestCase):
    def setUp(self):
        self._saved = g.INLINE_JSON

    def tearDown(self):
        g.INLINE_JSON = self._saved

    def _tmp(self):
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        return p

    def test_noop_when_unset(self):
        g.INLINE_JSON = ""
        # Must not raise and must write nothing.
        g.write_inline("ok", [{"path": "a.c", "line": 1, "side": "RIGHT", "body": "b"}])

    def test_ok_envelope(self):
        g.INLINE_JSON = self._tmp()
        g.write_inline("ok", [{"path": "a.c", "line": 1, "side": "RIGHT", "body": "b"}], dropped=2)
        with open(g.INLINE_JSON) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["source"], "gcc-gate")
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["dropped"], 2)
        self.assertEqual(len(doc["comments"]), 1)

    def test_skipped_envelope(self):
        g.INLINE_JSON = self._tmp()
        g.write_inline("skipped", [])
        with open(g.INLINE_JSON) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["status"], "skipped")
        self.assertEqual(doc["comments"], [])


if __name__ == "__main__":
    unittest.main()
