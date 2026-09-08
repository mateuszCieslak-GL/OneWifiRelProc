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
"""Diff-scoped cyclomatic-complexity delta nudge — ADVISORY, never gates.

For each .c/.cpp the PR changed, compare per-function CCN (via lizard) between the PR
base and head:
  * LOUD  -- a function that CROSSED the threshold (base < LOUD or new, head >= LOUD).
             Emphatic, named. Rare (~2% of develop commits at LOUD=30) and high-signal.
  * SOFT  -- a function that grew by >= SOFT_DELTA and ends at >= SOFT_FLOOR without
             crossing LOUD. A one-line list. The floor keeps trivial 2->7 growth out
             (pure ">=+5" fires on ~34% of commits; ">=+5 & head>=20" on ~10%).

Never a gate: high CCN is a maintainability/test-cost signal, not a crash class. Writes a
sticky markdown section to OUT only when something fires; always exits 0 (a lizard or git
hiccup yields no finding, never a failure). Base content comes from `git show BASE:<file>`,
head from the working tree.

Env: REPO_ROOT ('.'), OUT ('ci-out/complexity.md'), BASE (diff base ref, required for the
     base side), LOUD (30), SOFT_DELTA (5), SOFT_FLOOR (20), LIZARD ('lizard' -- the CLI to
     shell out to; the script does not import lizard, so a pipx-installed CLI is fine).
Usage: complexity_delta.py <changed-files.txt>   (one repo-relative .c/.cpp per line)
"""
import csv
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.environ.get("REPO_ROOT", ".")
OUT = os.environ.get("OUT", "ci-out/complexity.md")
BASE = os.environ.get("BASE", "").strip()
LIZARD = os.environ.get("LIZARD", "lizard")
SRC_EXT = (".c", ".cpp", ".cc", ".cxx")


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


LOUD = _int_env("LOUD", 30)
SOFT_DELTA = _int_env("SOFT_DELTA", 5)
SOFT_FLOOR = _int_env("SOFT_FLOOR", 20)


def parse_lizard_csv(text):
    """lizard --csv rows -> {function name: max CCN}. Columns are
    NLOC,CCN,token,PARAM,length,location,file,name,long_name,... -- CCN is col 1, name col
    7. long_name (col 8) can contain commas, so parse with the csv module, not split(',')."""
    out = {}
    for row in csv.reader(text.splitlines()):
        if len(row) < 8:
            continue
        try:
            ccn = int(row[1])
        except ValueError:
            continue  # a header or malformed row
        name = row[7]
        out[name] = max(out.get(name, 0), ccn)
    return out


def analyze_source(path, text=None):
    """{name: ccn} for one source. With `text`, analyze that content (a base version from
    `git show`); else the on-disk file at `path`. Returns {} on any lizard failure --
    advisory: a tool hiccup must never break the nudge. `path` sets the temp file's
    extension so lizard picks the right language reader."""
    tmp = None
    try:
        target = path
        if text is not None:
            ext = os.path.splitext(path)[1] or ".c"
            fd, tmp = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            target = tmp
        r = subprocess.run([LIZARD, "--csv", target],
                           capture_output=True, text=True, timeout=120)
        return parse_lizard_csv(r.stdout)
    except (OSError, subprocess.SubprocessError):
        return {}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def git_show(sha, path):
    """Base version text via `git show sha:path`, or None (file new / not in base)."""
    if not sha:
        return None
    r = subprocess.run(["git", "-C", REPO_ROOT, "show", f"{sha}:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def find_deltas(base_map, head_map, loud, soft_delta, soft_floor):
    """(loud, soft) as lists of (name, base_ccn, head_ccn). loud = crossed `loud` (base <
    loud or new, head >= loud); soft = grew >= soft_delta AND head >= soft_floor AND not
    loud. base_ccn is None for a function NEW in this PR (rendered as "new", not "0->N");
    the growth arithmetic still treats a new function as grown-from-0 (matches the backtest)."""
    loud_hits, soft_hits = [], []
    for name, hc in head_map.items():
        bc = base_map.get(name)          # None -> the function is new in this PR
        prev = bc if bc is not None else 0
        if prev < loud and hc >= loud:
            loud_hits.append((name, bc, hc))
            continue
        if hc - prev >= soft_delta and hc >= soft_floor:
            soft_hits.append((name, bc, hc))
    return sorted(loud_hits, key=lambda t: t[0]), sorted(soft_hits, key=lambda t: t[0])


def render_md(loud, soft):
    """The ci-summary sticky section (its own ### heading), or '' when nothing fired."""
    if not loud and not soft:
        return ""
    lines = ["### 🧮 Complexity", ""]
    if soft:
        names = ", ".join(f"`{n}` new at {h}" if b is None else f"`{n}` {b}->{h}"
                          for n, b, h in soft)
        lines.append(f"{len(soft)} function(s) this PR touched grew by >= {SOFT_DELTA} in "
                     f"cyclomatic complexity (now >= {SOFT_FLOOR}): {names}.")
        lines.append("")
    for n, b, h in loud:
        where = f"is a new function at CCN {h}" if b is None else f"crossed CCN {LOUD} ({b}->{h})"
        lines.append(f"- ⚠️ `{n}` {where} — please reconsider; a function this complex "
                     f"is hard to test and a defect magnet.")
    lines.append("")
    return "\n".join(lines)


def main(argv):
    changed = []
    if len(argv) > 1 and argv[1]:
        try:
            with open(argv[1], encoding="utf-8") as fh:
                changed = [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            changed = []

    all_loud, all_soft = [], []
    for path in changed:
        if not path.endswith(SRC_EXT):
            continue
        head_map = analyze_source(os.path.join(REPO_ROOT, path))
        if not head_map:
            continue  # file gone / lizard failed -> nothing to say
        base_text = git_show(BASE, path)
        base_map = analyze_source(path, text=base_text) if base_text is not None else {}
        loud, soft = find_deltas(base_map, head_map, LOUD, SOFT_DELTA, SOFT_FLOOR)
        all_loud += loud
        all_soft += soft

    all_loud = sorted(set(all_loud))
    all_soft = sorted(set(all_soft))
    if not all_loud and not all_soft:
        print(f"complexity: no touched function crossed CCN {LOUD} or grew "
              f">= {SOFT_DELTA} (ending >= {SOFT_FLOOR}).")
        return 0

    md = render_md(all_loud, all_soft)
    try:
        os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write(md)
    except OSError as exc:
        print(f"complexity: could not write {OUT}: {exc}")
    # Unresolvable annotations for the loud crossings (Files tab); soft stays sticky-only.
    for n, b, h in all_loud:
        print(f"::warning::complexity: {n} crossed CCN {LOUD} ({b}->{h}) — please reconsider")
    print(f"complexity: {len(all_loud)} crossed {LOUD}, {len(all_soft)} grew "
          f">= {SOFT_DELTA} (advisory).")
    return 0  # advisory: never fails the build


if __name__ == "__main__":
    sys.exit(main(sys.argv))
