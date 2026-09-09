#!/usr/bin/env python3
# Copyright 2026 RDK Management — Apache-2.0 (see complexity_delta.py header).
"""Unit tests for complexity_delta.py: lizard CSV parsing (CCN/name columns, commas in
long_name, max over duplicate names), the loud/soft delta rule (crossed threshold vs
grew-and-floor, precedence, new functions), render_md, and main()'s write/skip paths."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import complexity_delta as c  # noqa: E402


def _row(nloc, ccn, name, long_name):
    # NLOC,CCN,token,PARAM,length,location,file,name,long_name,start,end
    return f'{nloc},{ccn},80,2,10,"{name}@1-9@/x.c","/x.c","{name}","{long_name}",1,9'


class ParseLizardCsv(unittest.TestCase):
    def test_ccn_name_and_comma_in_longname(self):
        text = "\n".join([
            _row(5, 7, "foo", "foo( int a, int b)"),   # comma inside quoted long_name
            _row(3, 2, "bar", "bar()"),
        ])
        self.assertEqual(c.parse_lizard_csv(text), {"foo": 7, "bar": 2})

    def test_takes_max_for_duplicate_names(self):
        text = "\n".join([_row(1, 4, "f", "f()"), _row(1, 9, "f", "f(int)")])
        self.assertEqual(c.parse_lizard_csv(text), {"f": 9})

    def test_skips_malformed_rows(self):
        text = "not,an,int,ccn,row,here,at,all\n" + _row(1, 3, "g", "g()")
        self.assertEqual(c.parse_lizard_csv(text), {"g": 3})


class FindDeltas(unittest.TestCase):
    def test_crossed_is_loud(self):
        loud, soft = c.find_deltas({"f": 26}, {"f": 33}, 30, 5, 20)
        self.assertEqual(loud, [("f", 26, 33)])
        self.assertEqual(soft, [])

    def test_new_function_crossing_is_loud(self):
        loud, soft = c.find_deltas({}, {"f": 31}, 30, 5, 20)
        self.assertEqual(loud, [("f", None, 31)])   # None base -> rendered "new", not 0->31

    def test_grew_above_floor_is_soft(self):
        loud, soft = c.find_deltas({"g": 20}, {"g": 26}, 30, 5, 20)  # +6, head>=20, no cross
        self.assertEqual(soft, [("g", 20, 26)])
        self.assertEqual(loud, [])

    def test_grew_but_below_floor_ignored(self):
        # +7 growth but head 9 < floor 20 -> the "trivial 2->9" case we deliberately drop.
        self.assertEqual(c.find_deltas({"h": 2}, {"h": 9}, 30, 5, 20), ([], []))

    def test_small_growth_ignored(self):
        self.assertEqual(c.find_deltas({"h": 22}, {"h": 24}, 30, 5, 20), ([], []))  # +2 < 5

    def test_loud_precedence_over_soft(self):
        # base 10 -> head 30: crosses AND would satisfy soft; must be LOUD only.
        loud, soft = c.find_deltas({"f": 10}, {"f": 30}, 30, 5, 20)
        self.assertEqual(loud, [("f", 10, 30)])
        self.assertEqual(soft, [])


class RenderMd(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(c.render_md([], []), "")

    def test_loud_and_soft(self):
        md = c.render_md([("f", 26, 33)], [("g", 20, 26)])
        self.assertIn("### 🧮 Complexity", md)
        self.assertIn("crossed CCN", md)
        self.assertIn("`f`", md)
        self.assertIn("`g` 20->26", md)
        self.assertIn("please reconsider", md)

    def test_new_function_labelled_not_zero_arrow(self):
        md = c.render_md([("f", None, 33)], [("g", None, 22)])
        self.assertIn("`f` is a new function at CCN 33", md)
        self.assertIn("`g` new at 22", md)
        self.assertNotIn("0->", md)          # never render a bogus 0->N for a new function


class MainEndToEnd(unittest.TestCase):
    def setUp(self):
        self._saved = (c.analyze_source, c.git_show, c.OUT, c.BASE)
        c.git_show = lambda sha, path: "base content"

    def tearDown(self):
        c.analyze_source, c.git_show, c.OUT, c.BASE = self._saved

    def _changed(self, body="source/x.c\n"):
        fd, p = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        return p

    def test_writes_sticky_on_loud(self):
        # on-disk (head) -> 33; base (text != None) -> 26  => crossed 30
        c.analyze_source = lambda path, text=None: {"f": 33} if text is None else {"f": 26}
        c.BASE = "deadbeef"
        c.OUT = tempfile.mkstemp(suffix=".md")[1]
        rc = c.main(["complexity_delta.py", self._changed()])
        self.assertEqual(rc, 0)                       # advisory: never fails
        with open(c.OUT, encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("crossed CCN 30", md)
        self.assertIn("`f`", md)

    def test_no_sticky_when_clean(self):
        # head == base, no growth -> nothing written
        c.analyze_source = lambda path, text=None: {"f": 12}
        c.BASE = "deadbeef"
        c.OUT = os.path.join(tempfile.mkdtemp(), "complexity.md")
        rc = c.main(["complexity_delta.py", self._changed()])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(c.OUT))       # no finding -> no sticky file


if __name__ == "__main__":
    unittest.main()
