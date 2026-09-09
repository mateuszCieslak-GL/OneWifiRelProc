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
"""Red-build attribution — "is it me?" (Commit 5b, advisory, fail-open).

When Build Check goes red, answer whether THIS PR caused it or whether an
unpinned dependency drifted / develop is pre-existing-broken, so the failure is
actionable instead of a mystery. Two tiers, both fail-open (any mechanism error
-> exit 0 with an "attribution skipped" note; the build is already red from the
build step, this only annotates it):

  Tier 1 (every leg): a heuristic per error path. A sibling tree the PR cannot
  edit (rdk-wifi-hal, rdk-wifi-libhostap, unified-wifi-mesh, …) -> environment;
  an OneWifi file the PR actually changed -> pr; anything else -> ambiguous. A PR
  that changed any header downgrades "environment" to "ambiguous" (a header edit
  can break a sibling that includes it). Link errors: pr if the symbol appears in
  the PR diff, else environment-likely.

  Tier 2 (only where a compile DB exists — the bpi leg): the DEFINITIVE verdict.
  Identify the failing translation units from make's `*** [x.o] Error N` lines
  (NOT gcc's error paths — in the drift case the error is in a sibling header
  reached via an OneWifi .c, so error paths point at the header, not the TU).
  Match each .o to its compile_commands.json entry, then reversibly revert the PR
  (git apply -R of the PR diff) and recompile each failing TU on base sources: it
  still fails on base -> environment (definitive); it compiles on base -> pr
  (definitive). The tree is always restored (git apply forward + a porcelain
  assert); a restore mismatch downgrades to inconclusive.

effective_base() and db_args() are copied VERBATIM from gcc_diff_gate.py (the
shared-source rule: this file also ships standalone in the HAL). Keep them in sync.

Env:
  BASE          PR base sha (github.event.pull_request.base.sha).
  HAVE_DB       'true' only on a leg that produced compile_commands.json (bpi).
                Gate tier 2 on THIS, never on os.path.exists — a stale DB on a
                DB-less leg would recompile with the wrong flags and lie.
  REPO_DIR      dir the changed files + git history live in (default '.').
  ERRORS_FILE   normalized error lines from the build summary (default
                ci-out/build-errors.txt); tier-1 input.
  BUILD_LOG     make/compiler log (default build.log); tier-2 target parsing.
  OUT           markdown output path (default ci-out/build-attribution.md).
Always exits 0.
"""
import json
import os
import re
import subprocess
import sys

BASE = os.environ.get("BASE", "").strip()
HAVE_DB = os.environ.get("HAVE_DB", "").strip().lower() in ("true", "1", "yes", "on")
REPO_DIR = os.environ.get("REPO_DIR", ".").strip() or "."
ERRORS_FILE = os.environ.get("ERRORS_FILE", "ci-out/build-errors.txt").strip()
BUILD_LOG = os.environ.get("BUILD_LOG", "build.log").strip()
OUT = os.environ.get("OUT", "ci-out/build-attribution.md").strip()
DB = "compile_commands.json"

# Sibling trees the PR cannot edit. Two forms appear in normalized display paths:
# rdk-wifi-hal / rdk-wifi-libhostap are in the summary's path-strip alternation so
# they head the path directly; the others (mesh, halinterface, …) are NOT, so the
# strip leaves them under the checkout-root prefix "OneWifi/easymesh_project/<sib>/".
SIBLING_PREFIXES = ("rdk-wifi-hal/", "rdk-wifi-libhostap/", "OneWifi/easymesh_project/")
MAKE_TARGET_RE = re.compile(r"\*\*\* \[(?:.*[:\s])?([^\s\]]+\.o)\] Error \d+")
UNDEF_REF_RE = re.compile(r"undefined reference to [`']([^`']+)'")


def run_git(args, **kw):
    """git in REPO_DIR. One seam so tests can stub every git call."""
    return subprocess.run(["git", "-C", REPO_DIR, *args],
                          capture_output=True, text=True, **kw)


def run_cmd(cmd, cwd):
    """Run one recompile. Seam so tests can drive rc without a real compiler."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def effective_base():
    """Diff base for attribution — HEAD^1 when it is the trustworthy base.

    Copied verbatim from gcc_diff_gate.py: on a pull_request checked out with no
    `ref:`, HEAD is the merge ref refs/pull/N/merge, HEAD^1 is the CURRENT base
    tip (which the frozen payload BASE can drift from on a re-run). Prefer HEAD^1
    when HEAD is a merge commit AND BASE is its ancestor; else fall back to BASE.
    """
    def git_rc(*args):
        return run_git(list(args)).returncode
    if git_rc("rev-parse", "--verify", "--quiet", "HEAD^2") != 0:
        return BASE
    if git_rc("merge-base", "--is-ancestor", BASE, "HEAD^1") != 0:
        return BASE
    return "HEAD^1"


def db_args(db, f):
    """arguments for the DB entry whose file/out is f (exact) or ends with /f, minus -c/-o.

    Copied verbatim from gcc_diff_gate.py. Path-boundary match so a root-level
    'foo.o' never matches an unrelated '/src/notfoo.o'.
    """
    entry = next((e for e in db if e["file"] == f or e["file"].endswith("/" + f)), None)
    if not entry:
        return None
    out, skip = [], False
    for a in entry.get("arguments", []):
        if skip:
            skip = False
            continue
        if a == "-o":
            skip = True
            continue
        if a == "-c":
            continue
        out.append(a)
    return entry["directory"], out


def entry_for_object(db, obj):
    """The DB entry whose -o OUTPUT is obj (path-boundary). make reports failed
    TARGETS (`source/foo.o`), not sources, so we cannot match on entry['file']
    (the .c) — match the compile command's -o argument instead.
    """
    for e in db:
        a = e.get("arguments", [])
        outs = [a[i + 1] for i in range(len(a) - 1) if a[i] == "-o"]
        if any(o == obj or o.endswith("/" + obj) for o in outs):
            return e
    return None


def judge_tu(cwd, args, source):
    """Verdict for one failing TU recompiled on BASE sources.

    Called with the PR reverted (git apply -R). If the source no longer exists,
    the PR ADDED (or renamed) it — reverting removed it, so the failure is the
    PR's: 'pr'. Otherwise recompile it: still fails on base -> 'environment'
    (reproduces without the PR); compiles on base -> 'pr' (the revert fixed it).
    """
    src = source if os.path.isabs(source) else os.path.join(cwd, source)
    if not os.path.exists(src):
        return "pr"
    r = run_cmd(args + ["-c", "-o", os.devnull], cwd)
    return "environment" if r.returncode != 0 else "pr"


def read_lines(path):
    try:
        with open(path) as fh:
            return [ln.rstrip("\n") for ln in fh if ln.strip()]
    except OSError:
        return []


def changed_files(base):
    r = run_git(["diff", "--name-only", base, "HEAD"])
    if r.returncode != 0:
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def classify_path(path, changed_set, pr_changed_header):
    """One error path -> 'pr' | 'environment' | 'ambiguous' (tier-1 heuristic).

    changed_set holds OneWifi-repo-relative paths (as `git diff --name-only`
    gives them). A first-party OneWifi display path is `OneWifi/<relpath>`, so
    strip the leading `OneWifi/` before the membership test — that also excludes
    the siblings, whose stripped form starts with `easymesh_project/`.
    """
    rel = path[len("OneWifi/"):] if path.startswith("OneWifi/") else None
    if rel is not None and not rel.startswith("easymesh_project/") and rel in changed_set:
        return "pr"
    is_sibling = (
        path.startswith(SIBLING_PREFIXES)
        or not path.startswith("OneWifi/")   # /usr/…, other absolute / unrecognized tree
    )
    if is_sibling:
        return "ambiguous" if pr_changed_header else "environment"
    return "ambiguous"   # an OneWifi file the PR did not change


def error_paths(err_lines):
    """The distinct source paths named at the head of each error line."""
    paths = []
    seen = set()
    for ln in err_lines:
        m = re.match(r"^(\S+?):\d+:\d+: (?:error|fatal error):", ln) or \
            re.match(r"^(\S+?):\d+: (?:error|fatal error):", ln)
        p = m.group(1) if m else None
        if p and p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


def link_symbols(err_lines):
    syms, seen = [], set()
    for ln in err_lines:
        m = UNDEF_REF_RE.search(ln)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            syms.append(m.group(1))
    return syms


def parse_make_targets(build_log_text):
    """The .o targets make reported as failed, de-duplicated in first-seen order.

    Only `.o]` targets — recursive `[all]/[install] Error N` lines are not TUs.
    Tolerates the GNU make 4 `[makefile:line: target]` prefix.
    """
    out, seen = [], set()
    for m in MAKE_TARGET_RE.finditer(build_log_text):
        t = m.group(1)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def tier1(err_lines, changed_set, pr_changed_header, diff_text):
    """Return (rows, counts). rows: list of (path_or_sym, class)."""
    rows = []
    for p in error_paths(err_lines):
        rows.append((p, classify_path(p, changed_set, pr_changed_header)))
    for sym in link_symbols(err_lines):
        cls = "pr" if re.search(r"\b" + re.escape(sym) + r"\b", diff_text) else "environment-likely"
        rows.append((f"undefined reference: {sym}", cls))
    return rows


def recompile_on_base(db, targets):
    """Tier 2: verdict per failing TU after reverting the PR. Returns
    (verdicts, note) where verdicts maps target -> 'pr'|'environment'|'unknown'.

    Reversible: snapshot porcelain, `git apply -R` the PR diff, recompile each TU
    on base, then `git apply` forward and assert the snapshot. Any mechanism issue
    raises to the caller's fail-open handler; the finally-restore still runs.
    """
    verdicts = {}
    resolved = []            # (target, cwd, args, source)
    for t in targets:
        entry = entry_for_object(db, t)
        info = db_args(db, entry["file"]) if entry else None
        if entry and info:
            cwd, args = info
            resolved.append((t, cwd, args, entry["file"]))
        else:
            verdicts[t] = "unknown"   # not in the DB — can't judge this TU
    print(f"::debug::tier2 targets={len(targets)} resolved={len(resolved)}", file=sys.stderr)
    if not resolved:
        return verdicts, "no failing TU matched the compile DB"

    snapshot = run_git(["status", "--porcelain"]).stdout
    patch = run_git(["diff", "--binary", DIFF_BASE, "HEAD"]).stdout
    if not patch.strip():
        return verdicts, "empty PR diff — nothing to revert"
    with open("/tmp/pr.patch", "w") as fh:
        fh.write(patch)

    if run_git(["apply", "-R", "/tmp/pr.patch"]).returncode != 0:
        # binary / rename edge git apply -R can't handle — tree untouched, skip.
        return verdicts, "could not revert the PR diff (git apply -R failed) — tier 2 skipped"

    restored_ok = True
    try:
        for t, cwd, args, source in resolved:
            verdicts[t] = judge_tu(cwd, args, source)
    finally:
        if run_git(["apply", "/tmp/pr.patch"]).returncode != 0:
            restored_ok = False
        elif run_git(["status", "--porcelain"]).stdout != snapshot:
            restored_ok = False
    if not restored_ok:
        return verdicts, "WARNING: could not cleanly restore the PR tree after tier 2"
    return verdicts, ""


DIFF_BASE = None   # set in main()


def render(rows, verdicts, restore_note, trees):
    """Compose the ≤20-line markdown block + a one-line ::notice::."""
    env_tus = sorted(t for t, v in verdicts.items() if v == "environment")
    pr_tus = sorted(t for t, v in verdicts.items() if v == "pr")
    unknown_tus = sorted(t for t, v in verdicts.items() if v == "unknown")
    md, notice = [], ""
    if verdicts and not restore_note.startswith("WARNING") and (env_tus or pr_tus) and not pr_tus:
        # Every judged TU reproduces on base sources.
        tlist = ", ".join(sorted(trees)) or "the failing tree(s)"
        caveat = (f" ({len(unknown_tus)} TU(s) not in the compile DB, not judged)"
                  if unknown_tus else "")
        md.append(f"🧭 **Red build — not caused by this PR**: {len(env_tus)} error(s) "
                  f"reproduce on base sources (dependency drift or pre-existing breakage){caveat}.")
        md.append(f"Trees: {tlist}. Action: check whether develop is red / which unpinned "
                  "input moved (unified-wifi-mesh, rdk-wifi-hal, halinterface, trower-base64); "
                  "needs a maintainer pin bump or an upstream fix, not a change to this PR.")
        notice = "Red build not caused by this PR (errors reproduce on base sources)."
    elif pr_tus and not env_tus:
        files = ", ".join(pr_tus)
        md.append(f"🧭 **Red build — introduced by this PR**: errors in {files} disappear "
                  "when this PR's changes are reverted.")
        notice = "Red build introduced by this PR (errors vanish when it is reverted)."
    else:
        md.append("🧭 Red build — inconclusive (mixed verdicts / no compile DB on this leg / "
                  "mechanism error).")
        notice = "Red build attribution inconclusive."
        if restore_note:
            md.append(f"_{restore_note}_")
        if rows:
            md.append("")
            md.append("| path / symbol | heuristic |")
            md.append("|---|---|")
            for p, cls in rows[:12]:
                md.append(f"| `{p}` | {cls} |")
    return "\n".join(md), notice


def main():
    global DIFF_BASE
    if not BASE:
        return 0
    DIFF_BASE = effective_base()
    err_lines = read_lines(ERRORS_FILE)
    changed = changed_files(DIFF_BASE)
    changed_set = set(changed)
    pr_changed_header = any(c.endswith((".h", ".hpp", ".hh")) for c in changed)
    diff_text = run_git(["diff", DIFF_BASE, "HEAD"]).stdout

    rows = tier1(err_lines, changed_set, pr_changed_header, diff_text)

    verdicts, note = {}, ""
    trees = set()
    for p, _cls in rows:
        if p.startswith("rdk-wifi-hal/"):
            trees.add("rdk-wifi-hal")
        elif p.startswith("rdk-wifi-libhostap/"):
            trees.add("rdk-wifi-libhostap")
        elif p.startswith("OneWifi/easymesh_project/"):
            trees.add(p.split("/")[2])   # e.g. unified-wifi-mesh
        elif p.startswith("OneWifi/"):
            trees.add("OneWifi")

    if HAVE_DB and os.path.exists(DB):
        try:
            db = json.load(open(DB))
            targets = parse_make_targets(_read_text(BUILD_LOG))
            verdicts, note = recompile_on_base(db, targets)
        except Exception as exc:   # fail-open: annotate, never red
            note = f"tier 2 skipped (mechanism error: {exc})"

    md, notice = render(rows, verdicts, note, trees)
    try:
        d = os.path.dirname(OUT)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(OUT, "w") as fh:
            fh.write(md + "\n")
    except OSError as exc:
        print(f"::warning::build attribution could not write {OUT}: {exc}", file=sys.stderr)
    if notice:
        print(f"::notice::{notice}", file=sys.stderr)
    print(md)
    return 0


def _read_text(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        import traceback
        print("🧭 Red build — attribution skipped (mechanism error).")
        print(f"::warning::build attribution mechanism error: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(0)
