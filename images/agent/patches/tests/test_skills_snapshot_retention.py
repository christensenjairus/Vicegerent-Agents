"""Retention behaviour for snapshot-skills.sh: prune past 7 days, keep the rest.

Ages commits by forcing GIT_AUTHOR_DATE/GIT_COMMITTER_DATE, then runs the real
script and asserts what survived. The critical property is not "commits got
dropped" but "the oldest recoverable state is still recoverable" -- retention
must bound growth without losing the ability to restore.

    python3 test_skills_snapshot_retention.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = str(REPO / "images" / "agent" / "skills-scripts" / "snapshot-skills.sh")

ROOT = pathlib.Path("/tmp/snap-retention-test")
SKILLS = ROOT / "skills"
GITDIR = ROOT / "snap.git"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(label)


def env(**extra):
    e = {
        **os.environ,
        "HOME": "/opt/data",
        "AGENT_SKILLS_DIR": str(SKILLS),
        "SKILLS_SNAPSHOT_GITDIR": str(GITDIR),
    }
    e.update(extra)
    return e


def git(*a, **kw):
    return subprocess.run(
        ["git", f"--git-dir={GITDIR}", f"--work-tree={SKILLS}", *a],
        capture_output=True, text=True, env=env(**kw.pop("extra", {})),
    ).stdout.strip()


def snapshot(content: str, days_ago: float = 0, **extra):
    """Write content and run the real script with a forced commit date."""
    (SKILLS / "cat" / "probe" / "SKILL.md").write_text(content)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+0000",
                       time.gmtime(time.time() - days_ago * 86400))
    e = env(GIT_AUTHOR_DATE=ts, GIT_COMMITTER_DATE=ts, **extra)
    return subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=e)


shutil.rmtree(ROOT, ignore_errors=True)
(SKILLS / "cat" / "probe").mkdir(parents=True)

# 6 commits well outside the window, then 4 inside it.
for i in range(6):
    snapshot(f"OLD-{i}\n", days_ago=30 - i * 0.1)

# Every one of those is outside the window, so retention correctly collapses
# them to a single baseline -- there is no in-window history to keep yet. The
# property that matters is that the newest content survived the collapse.
check("all-stale history collapses to one baseline",
      int(git("rev-list", "--count", "HEAD")) == 1,
      git("rev-list", "--count", "HEAD"))
check("collapse preserves the newest content",
      git("show", "HEAD:cat/probe/SKILL.md") == "OLD-5",
      git("show", "HEAD:cat/probe/SKILL.md"))

for i in range(4):
    snapshot(f"NEW-{i}\n", days_ago=3 - i * 0.5)

total = int(git("rev-list", "--count", "HEAD"))
check("in-window commits retained", total >= 4, f"{total} commits")
check("history stays bounded", total <= 6, f"{total} commits")

# The distinguishing case: a single prune run that must collapse pre-window
# history AND replay several in-window commits. Build the history with
# retention effectively off, then run one pruning snapshot over it.
shutil.rmtree(ROOT, ignore_errors=True)
(SKILLS / "cat" / "probe").mkdir(parents=True)
snapshot("ANCIENT\n", days_ago=30, SKILLS_SNAPSHOT_RETAIN_DAYS="3650")
for i in range(3):
    snapshot(f"WINDOW-{i}\n", days_ago=4 - i, SKILLS_SNAPSHOT_RETAIN_DAYS="3650")
check("setup: history built with retention off",
      int(git("rev-list", "--count", "HEAD")) == 4,
      git("rev-list", "--count", "HEAD"))

snapshot("WINDOW-3\n", days_ago=0)  # default 7d retention -> one prune pass

subjects = git("log", "--pretty=%s")
revs = [c for c in git("rev-list", "HEAD").split("\n") if c]
bodies = [git("show", f"{c}:cat/probe/SKILL.md") for c in revs]
# The ancient CONTENT is deliberately preserved (the baseline carries the full
# pre-window tree). What must be gone is the ancient COMMIT: the only commit
# still holding it is the synthetic baseline, not an original snapshot.
holders = [git("log", "-1", "--pretty=%s", c)
           for c, b in zip(revs, bodies) if b == "ANCIENT"]
check("ancient content survives only via the baseline",
      all("baseline" in h for h in holders), str(holders))
for i in range(4):
    check(f"in-window snapshot WINDOW-{i} individually recoverable",
          f"WINDOW-{i}" in bodies, str(bodies))
check("collapse left exactly one baseline", subjects.count("baseline") == 1, subjects)

# The point of the whole feature: content must still be restorable.
shutil.rmtree(SKILLS / "cat" / "probe")
subprocess.run(["git", f"--git-dir={GITDIR}", f"--work-tree={SKILLS}",
                "checkout", "--", "."], capture_output=True, env=env())
check("restore still works after pruning",
      (SKILLS / "cat" / "probe" / "SKILL.md").read_text() == "WINDOW-3\n",
      repr((SKILLS / "cat" / "probe" / "SKILL.md").read_text()))

# A baseline commit must exist and carry the FULL tree, not a diff -- otherwise
# pruning would silently destroy the pre-window state.
root = git("rev-list", "--max-parents=0", "HEAD")
check("history is rooted in a baseline commit",
      "baseline" in git("log", "-1", "--pretty=%s", root),
      git("log", "-1", "--pretty=%s", root))
check("baseline holds the complete tree (not an empty root)",
      git("ls-tree", "-r", "--name-only", root) != "",
      git("ls-tree", "-r", "--name-only", root))

# No commit older than the window (excluding the synthetic baseline, which is
# stamped now by construction) should survive.
window_start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 7 * 86400))
stale = [c for c in git("rev-list", f"--before={window_start}", "HEAD").split("\n") if c]
stale = [c for c in stale if c != root]
check("no pre-window commits survive", not stale, f"{len(stale)} stale")

# Retention must be configurable, and a huge window must prune nothing.
before = int(git("rev-list", "--count", "HEAD"))
snapshot("NEW-4\n", days_ago=0, SKILLS_SNAPSHOT_RETAIN_DAYS="3650")
after = int(git("rev-list", "--count", "HEAD"))
check("a large retention window prunes nothing", after == before + 1,
      f"{before} -> {after}")

# A repo that stops changing must STILL prune. Retention must run even when
# nothing changed; it previously sat behind the "nothing changed" early exit,
# so a quiet skills tree kept stale history forever.
shutil.rmtree(ROOT, ignore_errors=True)
(SKILLS / "cat" / "probe").mkdir(parents=True)
snapshot("QUIET-0\n", days_ago=30, SKILLS_SNAPSHOT_RETAIN_DAYS="3650")
snapshot("QUIET-1\n", days_ago=29, SKILLS_SNAPSHOT_RETAIN_DAYS="3650")
before = int(git("rev-list", "--count", "HEAD"))
r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env())
after = int(git("rev-list", "--count", "HEAD"))
check("retention runs even when nothing changed", after < before,
      f"{before} -> {after}")
check("no-change run exits cleanly", r.returncode == 0, r.stderr.strip()[:80])
check("no-change run reports no commit", "committed" not in r.stderr,
      repr(r.stderr.strip()[:60]))

# An empty/new repo must not blow up on the retention path.
shutil.rmtree(ROOT, ignore_errors=True)
(SKILLS / "cat" / "probe").mkdir(parents=True)
r = snapshot("FIRST\n", days_ago=0)
check("first-ever snapshot exits cleanly", r.returncode == 0, r.stderr.strip()[:80])
check("first-ever snapshot still commits",
      int(git("rev-list", "--count", "HEAD")) == 1)
check("hook contract: stdout stays empty", r.stdout.strip() == "", repr(r.stdout[:40]))

shutil.rmtree(ROOT, ignore_errors=True)
print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
