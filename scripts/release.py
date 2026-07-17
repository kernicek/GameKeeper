#!/usr/bin/env python3
"""Create and push a CalVer release tag (YYYY.MM.DD[.N]).

Pushing the tag triggers .github/workflows/deploy.yml, which builds and pushes
ghcr.io/kernicek/gamekeeper to GHCR. Tag the commit you want deployed — this
tags HEAD.

Usage:
    python scripts/release.py                 # tag HEAD as today's date, auto-suffixed
    python scripts/release.py -m "notes"      # annotated tag with a message
    python scripts/release.py -n              # dry run: print the tag, create nothing
    python scripts/release.py 2026.07.09      # use an explicit tag instead of today
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys


def git(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        check=check,
        text=True,
        capture_output=True,
    )


def tag_exists(tag: str) -> bool:
    return git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def check_migration_conflicts(root: str) -> None:
    # `makemigrations --check` walks Django's actual migration graph, so a
    # merge migration that resolves two same-numbered leaves (a normal,
    # permanent artifact of concurrent branches) is correctly seen as fine —
    # unlike a naive check of migration filenames for duplicate numbers.
    result = subprocess.run(
        [sys.executable, "manage.py", "makemigrations", "--check", "--dry-run"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 and "Conflicting migrations detected" in result.stderr:
        die(result.stderr.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create and push a CalVer release tag (YYYY.MM.DD[.N]).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("tag", nargs="?", help="explicit tag to use instead of today's date")
    parser.add_argument("-m", "--message", help="create an annotated tag with this message")
    parser.add_argument("-n", "--dry-run", action="store_true", help="print the tag, create nothing")
    args = parser.parse_args()

    # Run from the repo root so relative paths and status checks behave.
    root = git("rev-parse", "--show-toplevel").stdout.strip()

    # Refuse to release a dirty tree — the tag would point at a commit that
    # doesn't match what's on disk.
    if git("status", "--porcelain").stdout.strip():
        die("working tree is dirty — commit or stash before tagging.")

    # Unmerged migration heads (e.g. two branches both adding "0039_...")
    # would leave production with divergent migration graphs.
    check_migration_conflicts(root)

    # Compare against every tag that already exists remotely.
    git("fetch", "--tags", "--quiet")

    if args.tag:
        tag = args.tag
    else:
        today = datetime.date.today().strftime("%Y.%m.%d")
        # First release of the day is bare (2026.07.09); later ones get .1, .2, …
        if not tag_exists(today):
            tag = today
        else:
            n = 1
            while tag_exists(f"{today}.{n}"):
                n += 1
            tag = f"{today}.{n}"

    if tag_exists(tag):
        die(f"tag {tag} already exists.")

    commit = git("rev-parse", "--short", "HEAD").stdout.strip()
    subject = git("log", "-1", "--format=%s").stdout.strip()
    print(f"tag:    {tag}")
    print(f"commit: {commit} ({subject})")

    if args.dry_run:
        print("(dry run — nothing created)")
        return

    if args.message:
        git("tag", "-a", tag, "-m", args.message)
    else:
        git("tag", tag)

    git("push", "origin", tag)
    print(f"pushed {tag} — watch the deploy: https://github.com/kernicek/GameKeeper/actions")


if __name__ == "__main__":
    main()
