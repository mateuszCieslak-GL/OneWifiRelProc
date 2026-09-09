#!/usr/bin/env python3
#
# If not stated otherwise in this file or this component's LICENSE file the
# following copyright and licenses apply:
#
# Copyright 2026 RDK Management
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Diff-scoped source-coverage gate.

OneWifi carries two independent, hand-maintained build descriptions: the
autotools tree (`configure.ac` + the `Makefile.am` files, `*_SOURCES = ...`) and
the standalone `build/linux/{bpi,rpi}/makefile` that CI and the bpi/rpi yocto
recipes actually compile. They list the same files by hand, so they drift — and
the drift that bites is a real source that autotools knows about but no CI leg
compiles: CI stays green while silently skipping the file.

This flags exactly that, diff-scoped to the files a PR ADDS: a `.c/.cpp/.cc` that
appears in some `*_SOURCES` but in neither leg's compile list. It is purely
textual — the OneWifi sources are enumerated explicitly in both legs (no wildcard
covers `source/`), so no compile database or build is needed.

ADVISORY by design, not a gate. "In a Makefile.am but not in a leg" is usually an
INTENTIONAL platform choice (XB-only / DML / feature-gated code the bpi & rpi legs
deliberately skip), not a forgotten file — so a hard gate would false-block
legitimate platform-specific additions. Two guards keep the signal honest:
  * diff scope — only files the PR adds (a new file in a _SOURCES was put there by
    this PR, so it can't be pre-existing intentional drift the PR didn't touch);
  * directory scope — only a file whose directory ALREADY has a leg-compiled
    sibling (a new file next to compiled code is a likely omission; a new file in
    a dir no leg ever compiles is a platform/aux area, left alone).

Advisory only — it never gates (there is no reliable textual way to tell an
intentional platform exclusion from a forgotten file, so blocking a merge on it
would false-fail legitimate XB-only additions). It emits a ::warning per finding
and, when there is a finding, writes a markdown section to OUT for the trusted
stage-2 job to fold into the one ci-summary sticky comment. Always exits 0.

Usage:  source_coverage.py <added-files.txt>
        <added-files.txt> = the PR's added paths, one repo-relative path per line
        (non-source lines are ignored). Reads Makefile.am + the two leg makefiles
        from REPO_ROOT (default '.'); writes the sticky section to OUT (default
        ci-out/source-coverage.md) only when there is a finding.
"""

import os
import re
import subprocess
import sys

TOP_SRCDIR = "$(top_srcdir)/"
SRC_EXT = (".c", ".cpp", ".cc")

# OneWifi's own sources in the leg makefiles are ALWAYS $(ONE_WIFI_HOME)/source|lib/…
# (verified: 64 such .c per leg, no wildcard under source/). Requiring that exact
# prefix ignores sibling-repo trees ($(WIFI_CCSP_COMMON_LIB)/source/…, hostap, HAL)
# whose paths would otherwise collide on a bare `source/…` suffix.
LEG_SRC_RE = re.compile(
    r"\$\(ONE_WIFI_HOME\)/((?:source|lib)/[A-Za-z0-9_./-]+\.(?:c|cpp|cc))\b"
)
SOURCES_RE = re.compile(r"^\s*[A-Za-z0-9_]+_SOURCES\s*\+?=(.*)$")


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _join_continuations(text):
    """Fold Make/automake line continuations (`\\` at EOL) into single lines."""
    return re.sub(r"\\\n", " ", text)


def normalize_am_token(tok, am_dir):
    """A Makefile.am _SOURCES token → repo-relative source path, or None.

    Handles `$(top_srcdir)/source/db/wifi_db.c` (→ source/db/wifi_db.c) and a bare
    `wifi_mgr.c` (→ <am_dir>/wifi_mgr.c). Tokens with any other unresolved make
    variable or automake substitution are skipped (can't place them textually)."""
    if tok.startswith(TOP_SRCDIR):
        tok = tok[len(TOP_SRCDIR):]
    if "$(" in tok or "@" in tok:
        return None
    if not tok.endswith(SRC_EXT):
        return None
    if "/" in tok:
        return os.path.normpath(tok)
    return os.path.normpath(os.path.join(am_dir, tok))


def parse_makefile_am_sources(root, am_paths):
    """Set of repo-relative .c/.cpp/.cc paths named in any *_SOURCES across the
    given Makefile.am files. Automake conditionals (`if X`) are intentionally
    ignored: a file under any conditional is still a source autotools knows."""
    srcs = set()
    for am in am_paths:
        am_dir = os.path.dirname(am)
        text = _join_continuations(_read(os.path.join(root, am)))
        for line in text.splitlines():
            m = SOURCES_RE.match(line)
            if not m:
                continue
            for tok in m.group(1).split():
                p = normalize_am_token(tok, am_dir)
                if p:
                    srcs.add(p)
    return srcs


def parse_leg_compiled(root, mk_paths):
    """Set of repo-relative OneWifi .c/.cpp/.cc paths compiled by the given
    build/linux leg makefiles (only $(ONE_WIFI_HOME)/source|lib/… entries)."""
    compiled = set()
    for mk in mk_paths:
        full = os.path.join(root, mk)
        if not os.path.isfile(full):
            continue
        for m in LEG_SRC_RE.finditer(_read(full)):
            compiled.add(os.path.normpath(m.group(1)))
    return compiled


def find_am_files(root):
    """Repo-relative paths of every Makefile.am under root.

    Prefer `git ls-files` (TRACKED files only): on the runner `make setup` clones
    dependencies (unified-wifi-mesh, submodules, …) INTO the tree, and their
    Makefile.am must not leak into the source set — same reasoning as the tidy
    step's `^rdk-wifi-libhostap/` skip. Fall back to an os.walk for a non-git tree
    (the unit-test tmp dirs)."""
    try:
        res = subprocess.run(
            ["git", "-C", root, "ls-files", "-z"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return [p for p in res.stdout.split("\0")
                if p and os.path.basename(p) == "Makefile.am"]
    except (OSError, subprocess.SubprocessError):
        walked = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            if "Makefile.am" in filenames:
                walked.append(os.path.relpath(
                    os.path.join(dirpath, "Makefile.am"), root))
        return walked


def find_drift(added, am_set, leg_set):
    """PR-added sources that autotools lists but no leg compiles, restricted to
    directories a leg already compiles from (see the module docstring). Sorted."""
    leg_dirs = {os.path.dirname(f) for f in leg_set}
    out = []
    for f in added:
        f = os.path.normpath(f.strip())
        if not f or not f.endswith(SRC_EXT):
            continue
        if f in am_set and f not in leg_set and os.path.dirname(f) in leg_dirs:
            out.append(f)
    return sorted(set(out))


def render_md(findings):
    """The ci-summary sticky section (its own ### heading), or '' when clean.

    A PR issue-comment sticky is unresolvable and updates in place by marker, so
    the nudge is visible to the maintainer and never duplicates on a re-push."""
    if not findings:
        return ""
    lines = [
        "### 🧭 Source coverage",
        "",
        "This PR adds source file(s) that appear in a `Makefile.am` but that "
        "neither the **bpi** nor **rpi** leg compiles. If a file should build on "
        "bpi/rpi, add it to `build/linux/bpi/makefile` (and `rpi/makefile`); if it "
        "is platform-specific (XB-only / feature-gated), no action is needed.",
        "",
    ]
    lines += [f"- `{f}`" for f in findings]
    lines.append("")
    return "\n".join(lines)


def main(argv):
    root = os.environ.get("REPO_ROOT", ".")
    out_path = os.environ.get("OUT", "ci-out/source-coverage.md")
    added = []
    if len(argv) > 1 and argv[1]:
        try:
            added = _read(argv[1]).splitlines()
        except OSError:
            added = []

    am_set = parse_makefile_am_sources(root, find_am_files(root))
    leg_set = parse_leg_compiled(
        root, ["build/linux/bpi/makefile", "build/linux/rpi/makefile"]
    )
    findings = find_drift(added, am_set, leg_set)

    if not findings:
        print("source-coverage: OK — every added source that autotools lists is "
              "compiled by a CI leg (or is not a tracked source).")
        return 0

    # ::warning annotations: unresolvable, and point at the exact file in the
    # Files tab (they are check annotations, not review comments, so they add no
    # clutter to the resolvable-comment threads).
    for f in findings:
        print(f"::warning file={f},line=1::{f} is in a Makefile.am _SOURCES but no "
              f"CI leg compiles it. If it should build on bpi/rpi add it to "
              f"build/linux/bpi/makefile (and rpi); if platform-specific "
              f"(XB-only / feature-gated) this is expected.")
    # The sticky section, written for the trusted stage-2 job to cat into the
    # one ci-summary comment (only written when there is a finding).
    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(render_md(findings))
    except OSError as exc:
        print(f"source-coverage: could not write {out_path}: {exc}")
    print(f"source-coverage: {len(findings)} added source(s) not in any "
          f"build-check makefile (advisory):")
    for f in findings:
        print(f"  - {f}")
    return 0  # advisory: never fails the build


if __name__ == "__main__":
    sys.exit(main(sys.argv))
