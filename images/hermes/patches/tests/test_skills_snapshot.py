"""Exercise the skills-snapshot script against the symlink-deletion disaster.

Run: python3 images/hermes/patches/tests/test_skills_snapshot.py

The script is baked into the image at /usr/local/bin/snapshot-skills.sh; this
tests the source under images/hermes/skills-scripts/.
"""
import os
import pathlib
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = str(REPO / "images" / "hermes" / "skills-scripts" / "snapshot-skills.sh")

ROOT = pathlib.Path("/tmp/snaptest")
SKILLS = ROOT / "skills"
GITDIR = ROOT / "snap.git"


def run(**extra):
    env = {**os.environ, "HOME": "/opt/data",
           "HERMES_SKILLS_DIR": str(SKILLS), "SKILLS_SNAPSHOT_GITDIR": str(GITDIR)}
    env.update(extra)
    return subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env)


def git(*a):
    return subprocess.run(
        ["git", f"--git-dir={GITDIR}", f"--work-tree={SKILLS}", *a],
        capture_output=True, text=True, env={**os.environ, "HOME": "/opt/data"},
    ).stdout.strip()


fails = []


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(label)


shutil.rmtree(ROOT, ignore_errors=True)
(SKILLS / "cat" / "unique").mkdir(parents=True)
(SKILLS / "cat" / "unique" / "SKILL.md").write_text("IRREPLACEABLE\n")
(SKILLS / "cat" / "unique" / "references").mkdir()
(SKILLS / "cat" / "unique" / "references" / "api.md").write_text("refdata\n")
(SKILLS / ".usage.json").write_text('{"a": 1}\n')
(SKILLS / ".usage.json.lock").write_text("")
(SKILLS / ".curator_state").write_text("state\n")
(SKILLS / ".bundled_manifest").write_text("manifest\n")
(SKILLS / ".hub" / "index-cache").mkdir(parents=True)
(SKILLS / ".hub" / "audit.log").write_text("log\n")
(SKILLS / ".archive" / "retired").mkdir(parents=True)
(SKILLS / ".archive" / "retired" / "SKILL.md").write_text("ARCHIVED BUT PRECIOUS\n")

r = run()
check("first run creates the repo", GITDIR.is_dir(), r.stderr.strip()[:60])
check("no .git artifact inside the skills tree",
      not (SKILLS / ".git").exists(),
      "top: " + " ".join(sorted(p.name for p in SKILLS.iterdir())))
tracked = git("ls-files").split("\n")
check("skill content tracked", "cat/unique/SKILL.md" in tracked)
check("support files tracked", "cat/unique/references/api.md" in tracked)
for transient in (".usage.json", ".usage.json.lock", ".curator_state",
                  ".bundled_manifest", ".hub/audit.log"):
    check(f"transient state excluded: {transient}",
          transient not in tracked, str(tracked))
check(".archive IS tracked (archived skills stay recoverable)",
      ".archive/retired/SKILL.md" in tracked, str(tracked))

# no-op run must not create an empty commit
before = git("rev-list", "--count", "HEAD")
run()
after = git("rev-list", "--count", "HEAD")
check("unchanged tree produces no new commit", before == after, f"{before} -> {after}")

# .usage.json churn alone must not commit
(SKILLS / ".usage.json").write_text('{"a": 2, "b": 3}\n')
(SKILLS / ".curator_state").write_text("changed\n")
(SKILLS / ".hub" / "audit.log").write_text("more log\n")
run()
check("usage churn alone produces no commit",
      git("rev-list", "--count", "HEAD") == before)

# THE DISASTER: rm -rf through a symlink
farm = ROOT / "farm"
farm.mkdir()
os.symlink(SKILLS / "cat" / "unique", farm / "unique")
subprocess.run(["bash", "-c", f"rm -rf {farm}/unique/"], capture_output=True)
check("disaster destroyed canonical content",
      not (SKILLS / "cat" / "unique" / "SKILL.md").exists())

check("snapshot still holds the content",
      git("show", "HEAD:cat/unique/SKILL.md") == "IRREPLACEABLE")

subprocess.run(["git", f"--git-dir={GITDIR}", f"--work-tree={SKILLS}", "checkout", "--", "."],
               capture_output=True, env={**os.environ, "HOME": "/opt/data"})
check("restore recovers SKILL.md",
      (SKILLS / "cat" / "unique" / "SKILL.md").read_text() == "IRREPLACEABLE\n")
check("restore recovers support files",
      (SKILLS / "cat" / "unique" / "references" / "api.md").read_text() == "refdata\n")

# a real edit must commit
(SKILLS / "cat" / "unique" / "SKILL.md").write_text("EDITED\n")
run()
check("real content change produces a commit",
      git("rev-list", "--count", "HEAD") != before,
      git("log", "-1", "--pretty=%s"))

# A global gitignore (core.excludesFile) would otherwise silently drop matching
# skill files from the snapshot, leaving a backup that looks healthy and is not.
gi = pathlib.Path("/tmp/_snap_globalignore")
gi.write_text("*.md\n")
gc = pathlib.Path("/tmp/_snap_gitconfig")
gc.write_text(f"[core]\n\texcludesFile = {gi}\n")
shutil.rmtree(ROOT, ignore_errors=True)
(SKILLS / "cat" / "u").mkdir(parents=True)
(SKILLS / "cat" / "u" / "SKILL.md").write_text("under global ignore\n")
run(GIT_CONFIG_GLOBAL=str(gc))
check("a global gitignore cannot silently drop skills",
      "cat/u/SKILL.md" in git("ls-files").split("\n"), git("ls-files"))
gi.unlink(missing_ok=True)
gc.unlink(missing_ok=True)

# missing skills dir must be a clean no-op
shutil.rmtree(ROOT, ignore_errors=True)
r = run()
check("missing skills dir exits cleanly", r.returncode == 0)
check("hook contract: stdout empty (JSON parser tolerates)", r.stdout.strip() == "",
      repr(r.stdout[:40]))

shutil.rmtree(ROOT, ignore_errors=True)
print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
