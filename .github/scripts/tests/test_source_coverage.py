#!/usr/bin/env python3
# Copyright 2026 RDK Management — Apache-2.0 (see source_coverage.py header).
"""Unit tests for source_coverage.py: Makefile.am _SOURCES parsing (top_srcdir,
bare-relative, continuations, conditionals, += ), leg-makefile extraction (only
$(ONE_WIFI_HOME) entries, sibling trees ignored), the drift set, and main()'s
exit codes / annotations on a scratch repo tree."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import source_coverage as sc  # noqa: E402


class NormalizeAmToken(unittest.TestCase):
    def test_top_srcdir(self):
        self.assertEqual(
            sc.normalize_am_token("$(top_srcdir)/source/db/wifi_db.c", "source/core"),
            "source/db/wifi_db.c",
        )

    def test_bare_relative_to_am_dir(self):
        self.assertEqual(
            sc.normalize_am_token("wifi_mgr.c", "source/core"),
            "source/core/wifi_mgr.c",
        )

    def test_unresolved_variable_skipped(self):
        self.assertIsNone(sc.normalize_am_token("$(GEN_DIR)/foo.c", "source"))

    def test_at_substitution_skipped(self):
        self.assertIsNone(sc.normalize_am_token("@THING@.c", "source"))

    def test_non_source_skipped(self):
        self.assertIsNone(sc.normalize_am_token("wifi_mgr.h", "source/core"))


class ParseMakefileAmSources(unittest.TestCase):
    def _write(self, root, rel, body):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_continuations_conditionals_and_append(self):
        with tempfile.TemporaryDirectory() as root:
            self._write(
                root,
                "source/core/Makefile.am",
                "OneWifi_SOURCES = wifi_mgr.c wifi_ctrl.c \\\n"
                "    $(top_srcdir)/source/db/wifi_db.c\n"
                "if FEATURE_X\n"
                "OneWifi_SOURCES += $(top_srcdir)/source/apps/x.c\n"
                "endif\n"
                "libwifi_la_SOURCES = helper.c\n"
                "OneWifi_CFLAGS = -Wall  # not a source line\n",
            )
            got = sc.parse_makefile_am_sources(root, ["source/core/Makefile.am"])
            self.assertEqual(
                got,
                {
                    "source/core/wifi_mgr.c",
                    "source/core/wifi_ctrl.c",
                    "source/db/wifi_db.c",
                    "source/apps/x.c",       # conditional file still counts
                    "source/core/helper.c",  # bare name in a different _SOURCES var
                },
            )


class ParseLegCompiled(unittest.TestCase):
    def test_only_onewifi_prefix_captured(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "build/linux/bpi"), exist_ok=True)
            with open(os.path.join(root, "build/linux/bpi/makefile"), "w") as fh:
                fh.write(
                    "SRCS = $(ONE_WIFI_HOME)/source/core/wifi_mgr.c \\\n"
                    "    $(ONE_WIFI_HOME)/lib/log/log_journal.c \\\n"
                    # sibling repo — same 'source/…' suffix, must be ignored:
                    "    $(WIFI_CCSP_COMMON_LIB)/source/cosa/foo.c \\\n"
                    "    $(wildcard $(WIFI_RDK_HAL)/src/*.c)\n"
                    "INCLUDES = -I$(ONE_WIFI_HOME)/source/core  # not a .c\n"
                )
            got = sc.parse_leg_compiled(root, ["build/linux/bpi/makefile",
                                               "build/linux/rpi/makefile"])
            self.assertEqual(
                got, {"source/core/wifi_mgr.c", "lib/log/log_journal.c"}
            )


class FindAmFilesTrackedOnly(unittest.TestCase):
    def test_untracked_vendored_makefile_am_excluded(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as root:
            def git(*a):
                subprocess.run(["git", "-C", root, *a], check=True,
                               capture_output=True)
            git("init", "-q")
            git("config", "user.email", "t@t")
            git("config", "user.name", "t")
            os.makedirs(os.path.join(root, "source/core"))
            with open(os.path.join(root, "source/core/Makefile.am"), "w") as fh:
                fh.write("OneWifi_SOURCES = a.c\n")
            git("add", "source/core/Makefile.am")
            git("commit", "-q", "-m", "x")
            # a clone `make setup` drops into the tree — untracked, must be skipped:
            os.makedirs(os.path.join(root, "unified-wifi-mesh"))
            with open(os.path.join(root, "unified-wifi-mesh/Makefile.am"), "w") as fh:
                fh.write("Mesh_SOURCES = m.c\n")
            self.assertEqual(sc.find_am_files(root), ["source/core/Makefile.am"])


class FindDrift(unittest.TestCase):
    def setUp(self):
        # source/core has a compiled sibling (have.c); source/x does not.
        self.am = {"source/core/new.c", "source/core/have.c", "source/x/y.c"}
        self.leg = {"source/core/have.c"}

    def test_in_am_not_in_leg_is_flagged(self):
        self.assertEqual(
            sc.find_drift(["source/core/new.c"], self.am, self.leg),
            ["source/core/new.c"],
        )

    def test_in_both_is_clean(self):
        self.assertEqual(sc.find_drift(["source/core/have.c"], self.am, self.leg), [])

    def test_not_in_any_sources_is_clean(self):
        # Not in a _SOURCES → not a known source → not our concern (Design Y).
        self.assertEqual(sc.find_drift(["source/core/scratch.c"], self.am, self.leg), [])

    def test_header_ignored(self):
        self.assertEqual(sc.find_drift(["source/core/new.h"], self.am, self.leg), [])

    def test_dir_without_compiled_sibling_skipped(self):
        # source/x/y.c is in a _SOURCES and uncompiled, but no leg compiles
        # anything in source/x → a platform/aux area, left alone (not flagged).
        self.assertEqual(sc.find_drift(["source/x/y.c"], self.am, self.leg), [])


class MainEndToEnd(unittest.TestCase):
    def _repo(self, root):
        os.makedirs(os.path.join(root, "source/core"), exist_ok=True)
        os.makedirs(os.path.join(root, "build/linux/bpi"), exist_ok=True)
        os.makedirs(os.path.join(root, "build/linux/rpi"), exist_ok=True)
        with open(os.path.join(root, "source/core/Makefile.am"), "w") as fh:
            fh.write("OneWifi_SOURCES = wifi_mgr.c newfeature.c\n")
        # bpi compiles wifi_mgr.c but NOT newfeature.c -> drift on newfeature.c
        for leg in ("bpi", "rpi"):
            with open(os.path.join(root, f"build/linux/{leg}/makefile"), "w") as fh:
                fh.write("SRCS = $(ONE_WIFI_HOME)/source/core/wifi_mgr.c\n")
        with open(os.path.join(root, "added.txt"), "w") as fh:
            fh.write("source/core/newfeature.c\ndocs/README.md\n")

    def _run(self, root):
        os.environ["REPO_ROOT"] = root
        os.environ["OUT"] = os.path.join(root, "sc.md")
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = sc.main(["source_coverage.py", os.path.join(root, "added.txt")])
        finally:
            os.environ.pop("REPO_ROOT", None)
            os.environ.pop("OUT", None)
        return rc, buf.getvalue()

    def _out(self, root):
        p = os.path.join(root, "sc.md")
        return open(p, encoding="utf-8").read() if os.path.isfile(p) else None

    def test_advisory_warns_and_writes_sticky(self):
        with tempfile.TemporaryDirectory() as root:
            self._repo(root)
            rc, out = self._run(root)
            self.assertEqual(rc, 0)  # advisory: never reddens
            self.assertIn("::warning file=source/core/newfeature.c", out)
            self.assertNotIn("wifi_mgr.c", out)  # compiled -> not flagged
            md = self._out(root)
            self.assertIsNotNone(md)  # sticky section written
            self.assertIn("### 🧭 Source coverage", md)
            self.assertIn("`source/core/newfeature.c`", md)

    def test_clean_writes_no_sticky(self):
        with tempfile.TemporaryDirectory() as root:
            self._repo(root)
            # add newfeature.c to both legs -> no drift
            for leg in ("bpi", "rpi"):
                with open(os.path.join(root, f"build/linux/{leg}/makefile"), "a") as fh:
                    fh.write("SRCS += $(ONE_WIFI_HOME)/source/core/newfeature.c\n")
            rc, out = self._run(root)
            self.assertEqual(rc, 0)
            self.assertIn("source-coverage: OK", out)
            self.assertIsNone(self._out(root))  # no sticky section when clean


if __name__ == "__main__":
    unittest.main()
