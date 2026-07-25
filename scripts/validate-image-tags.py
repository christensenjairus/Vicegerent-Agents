#!/usr/bin/env python3
"""Keep every images/<name>/ build in lockstep with the tag the cluster deploys.

Two checks, both grounded in the same rule from AGENTS.md: our images are pulled
IfNotPresent on a static tag, so a rebuild that reuses a tag is never redeployed.

  static (default)  every deployed reference to an image we build carries exactly
                    the TAG its Makefile currently defaults to, and every image we
                    build is referenced somewhere.
  --since <ref>     additionally, any change to an image's build context since
                    <ref> came with a change to that image's TAG line.

The --since half needs a git base, so scripts/validate.sh runs the static half
and .gitlab-ci.yml's validate:image-tag-bump job adds the diff half against the
merge request's base SHA.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A path that cannot change what lands in the image, so it needs no TAG bump.
CONTENT_EXEMPT = {"README.md"}

MAKE_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:?]?=\s*(.*?)\s*$", re.MULTILINE)
MAKE_VAR_REF = re.compile(r"\$\((\w+)\)")


def make_vars(text: str) -> dict[str, str]:
    """Assignments from a Makefile, with $(VAR) references resolved in-file."""
    raw = {m.group(1): m.group(2) for m in MAKE_ASSIGN.finditer(text)}
    resolved: dict[str, str] = {}

    def expand(name: str, seen: frozenset[str] = frozenset()) -> str:
        if name in resolved:
            return resolved[name]
        if name in seen or name not in raw:
            return ""
        value = MAKE_VAR_REF.sub(lambda m: expand(m.group(1), seen | {name}), raw[name])
        resolved[name] = value
        return value

    for name in raw:
        expand(name)
    return resolved


def built_images() -> dict[str, tuple[str, str]]:
    """{image dir name: (full image ref, tag)} for every image with a Makefile."""
    out: dict[str, tuple[str, str]] = {}
    for makefile in sorted((ROOT / "images").glob("*/Makefile")):
        v = make_vars(makefile.read_text())
        image, tag = v.get("IMAGE", ""), v.get("TAG", "")
        if not image or not tag:
            sys.exit(f"{makefile.relative_to(ROOT)}: needs both IMAGE and TAG")
        out[makefile.parent.name] = (image, tag)
    return out


def tracked_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [ROOT / p for p in listing.split("\0") if p]


def deployed_tags(image: str, haystacks: dict[Path, str]) -> list[tuple[Path, int, str]]:
    """Every (file, line, tag) that pins this image, across the three ref shapes.

    Inline `<image>:<tag>`; a Helm-style `repository: <image>` / `tag:` pair; and
    agentgateway's split `registry:` / `repository:` / `tag:` triple. A mention
    with no tag at all (renovate.json's matchPackageNames) is not a deployment.
    """
    registry, _, repository = image.rpartition("/")
    patterns = [
        re.compile(rf"{re.escape(image)}:(?P<tag>[A-Za-z0-9][^\s\"',\]]*)"),
        re.compile(rf"repository:\s*{re.escape(image)}\s*\n\s*tag:\s*(?P<tag>\S+)"),
        re.compile(
            rf"registry:\s*{re.escape(registry)}\s*\n"
            rf"\s*repository:\s*{re.escape(repository)}\s*\n"
            rf"\s*tag:\s*(?P<tag>\S+)"
        ),
    ]
    found = []
    for path, text in haystacks.items():
        for pattern in patterns:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                found.append((path, line, m.group("tag")))
    return found


def check_static(images: dict[str, tuple[str, str]]) -> list[str]:
    haystacks = {}
    for path in tracked_files():
        if path.parts[-1] == "Makefile" or "/images/" in f"/{path.relative_to(ROOT)}":
            continue
        try:
            haystacks[path] = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue

    errors = []
    for name, (image, tag) in images.items():
        refs = deployed_tags(image, haystacks)
        if not refs:
            errors.append(
                f"{image} is built by images/{name}/ but nothing outside images/ deploys it -- "
                f"either wire it up or delete the image"
            )
            continue
        for path, line, found in refs:
            if found != tag:
                errors.append(
                    f"{path.relative_to(ROOT)}:{line} pins {image}:{found} but "
                    f"images/{name}/Makefile defaults TAG to {tag}"
                )
    return errors


def check_bumped(images: dict[str, tuple[str, str]], since: str) -> list[str]:
    # Diffed against the working tree, not HEAD, so this catches a bump that is
    # still uncommitted -- the state you are in when you run it by hand. Diffing
    # from the merge base keeps a hand-run `--since origin/main` on a branch
    # behind main from picking up main-side image changes; CI passes the merge
    # request's base SHA, which IS the merge base, so there it changes nothing.
    base = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", since, "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    changed = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", base],
        capture_output=True, text=True, check=True,
    ).stdout.split()

    errors = []
    for name in images:
        prefix = f"images/{name}/"
        content = [
            p for p in changed
            if p.startswith(prefix) and Path(p).name not in CONTENT_EXEMPT
        ]
        if not content:
            continue
        before = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{since}:{prefix}Makefile"],
            capture_output=True, text=True,
        )
        if before.returncode != 0:  # new image, so its tag is new by definition
            continue
        old = make_vars(before.stdout).get("TAG", "")
        if old == images[name][1]:
            errors.append(
                f"images/{name}/ changed ({', '.join(sorted(content)[:4])}"
                f"{', ...' if len(content) > 4 else ''}) but TAG is still {old} -- "
                f"the cluster pulls IfNotPresent, so this rebuild would never be deployed"
            )
    return errors


def main() -> int:
    since = None
    args = sys.argv[1:]
    if args[:1] == ["--since"]:
        if len(args) != 2:
            sys.exit("usage: validate-image-tags.py [--since <git-ref>]")
        since = args[1]
    elif args:
        sys.exit("usage: validate-image-tags.py [--since <git-ref>]")

    images = built_images()
    errors = check_static(images)
    if since:
        errors += check_bumped(images, since)

    if errors:
        for e in errors:
            print(f"FAIL - {e}", file=sys.stderr)
        return 1
    scope = f"deployed tags in sync + bumped since {since}" if since else "deployed tags in sync"
    print(f"OK - {len(images)} built images: {scope}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
