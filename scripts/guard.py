#!/usr/bin/env python3
"""Fail when repo-bound text carries a term that must not become public.

One implementation, three modes, so that what CI enforces and what you can run locally are
the same code and the same regex engine. A shell version would have meant maintaining two,
and would not have run on Windows without a bash on PATH.

    python scripts/guard.py            working tree, tracked paths, unpushed commits
    python scripts/guard.py --staged   only what is staged (what the pre-commit hook runs)
    python scripts/guard.py --ci       for GitHub Actions: redacted output, pattern from env

WHERE THE TERM LIST LIVES

Locally, in this clone's git config -- never in a tracked file:

    git config --local guard.pattern "internalname|some person|XY-[0-9]+"

.git/config is not committed, so the list stays on your machine. In CI it comes from the
FORBIDDEN_PATTERN repository secret. Set the two to the same value.

A public workflow or script that spelled out the terms would publish exactly what it exists
to keep out of the repo, in a file search engines index. That is why this file contains no
terms, and why --ci prints neither what matched nor, when a filename is what matched, where.

Keep the pattern to plain alternation, character classes and \\b. Those mean the same thing
to Python's re as to grep -E, so a pattern that behaves one way locally cannot behave
another way in CI.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ZERO_SHA = "0" * 40


def git(*args: str) -> str:
    """Run git and return stdout, or "" if the command failed.

    Failure is routine here, not exceptional: a range can name a commit that no longer
    exists after a force-push, and an upstream ref may simply not be configured.
    """
    try:
        done = subprocess.run(("git", *args), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)
    except OSError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def tracked_files() -> list[str]:
    out = git("ls-files", "-z")
    return [p for p in out.split("\0") if p]


def read_blob(rev_path: str) -> str:
    """Contents of a path at a revision, as text. Binary comes back as replacement chars,
    which cannot produce a false positive for an alphanumeric term."""
    return git("show", rev_path)


def matches(rx: re.Pattern[str], text: str) -> list[tuple[int, str]]:
    return [(n, line) for n, line in enumerate(text.splitlines(), 1) if rx.search(line)]


def commit_range() -> list[str]:
    """The commits this event introduces.

    Merge commits are included, unlike the sign-off check: a squash or merge commit carries
    the pull request title, which a person wrote.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    head = os.environ.get("GITHUB_SHA", "HEAD")
    if event == "pull_request":
        base, tip = os.environ.get("BASE_SHA", ""), os.environ.get("HEAD_SHA", "")
        if base and tip:
            return git("rev-list", f"{base}..{tip}").split()
    before = os.environ.get("PUSH_BEFORE", "")
    # `rev-parse --verify --quiet` and not `cat-file -e`: cat-file prints nothing on success,
    # so its empty output is indistinguishable from the empty string this helper returns on
    # failure. rev-parse echoes the sha, which is a signal that can actually be read.
    if (before and before != ZERO_SHA
            and git("rev-parse", "--verify", "--quiet", f"{before}^{{commit}}").strip()):
        return git("rev-list", f"{before}..{head}").split()
    # First push to a branch, or a force-push: the previous tip is unknown or gone. Check
    # the tip alone rather than the entire history, which would fail forever on old commits
    # nobody can now rewrite.
    return git("rev-list", "--max-count=1", head).split()


def unpushed_commits() -> list[str]:
    """Local commits not yet on the upstream -- the ones an amend or rebase can still fix."""
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").strip()
    if not upstream:
        upstream = "origin/main"
    if not git("rev-parse", "--verify", "--quiet", f"{upstream}^{{commit}}").strip():
        return []
    return git("rev-list", f"{upstream}..HEAD").split()


def commit_text(sha: str) -> str:
    return git("log", "-1", "--format=%s%n%b%n%an%n%ae%n%cn%n%ce", sha)


# --------------------------------------------------------------------------------------
# CI mode: nothing that matched is ever printed.
# --------------------------------------------------------------------------------------

def run_ci(rx: re.Pattern[str]) -> int:
    status = 0
    paths = tracked_files()

    for path in paths:
        hits = matches(rx, read_blob(f"HEAD:{path}") or "")
        if not hits:
            continue
        status = 1
        for line_no, _line in hits:
            if rx.search(path):
                # The PATH matches too, so it cannot appear anywhere in this log -- not in
                # the message, and not in the annotation's file= parameter, which is what
                # makes an error clickable. Losing the link is the right trade.
                print(f"::error::Forbidden term at line {line_no} of a tracked file whose "
                      "name also matches. Both withheld; run the guard locally to see them.")
            else:
                print(f"::error file={path},line={line_no}::"
                      f"Forbidden term in tracked content: {path}:{line_no}")

    # Counted, never listed: naming one would print the term.
    path_hits = sum(1 for p in paths if rx.search(p))
    if path_hits:
        status = 1
        print(f"::error::{path_hits} tracked file path(s) contain a forbidden term. Paths "
              "are withheld here because printing one would publish the term.")

    for sha in commit_range():
        if rx.search(commit_text(sha)):
            status = 1
            print(f"::error::Forbidden term in the message or authorship of {sha}. "
                  "Rewrite it before this can merge.")

    # The original leak reached the public through a pull request body -- text that no
    # amount of scanning a checkout would ever have caught.
    for field in ("PR_TITLE", "PR_BODY", "BRANCH_NAME"):
        value = os.environ.get(field, "")
        if value and rx.search(value):
            status = 1
            print(f"::error::Forbidden term in {field}. Edit it before this can merge.")

    if status:
        print()
        print("Content that must stay private has reached repo-bound text. The locations")
        print("are above; the terms themselves are withheld because this log is public.")
        print()
        print("To see what matched:  python scripts/guard.py")
        print("A fix in the working tree is not enough if the term is in a commit message.")
    else:
        print("No forbidden terms in tracked content, paths, commit messages or "
              "pull request text.")
    return status


# --------------------------------------------------------------------------------------
# Local modes: these DO print what matched. Your terminal is not a public log, and a
# report you cannot read is a report you learn to ignore.
# --------------------------------------------------------------------------------------

RULE = "-" * 60


def show(header: str, lines: list[str]) -> None:
    print(RULE)
    print(header)
    for line in lines:
        print(f"  {line}")


def run_staged(rx: re.Pattern[str]) -> int:
    status = 0
    staged = [p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR",
                             "-z").split("\0") if p]
    for path in staged:
        # The STAGED content, not the file on disk: they differ whenever something is staged
        # and then edited again, and it is the staged version that becomes the commit.
        hits = matches(rx, read_blob(f":{path}") or "")
        if hits:
            status = 1
            show(f"STAGED CONTENT  {path}", [f"{n}: {line.strip()}" for n, line in hits])
    bad_paths = [p for p in staged if rx.search(p)]
    if bad_paths:
        status = 1
        show("STAGED PATHS", bad_paths)
    return status


def run_worktree(rx: re.Pattern[str]) -> int:
    status = 0

    content: list[str] = []
    for path in tracked_files():
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                body = handle.read()
        except OSError:
            continue
        content += [f"{path}:{n}: {line.strip()}" for n, line in matches(rx, body)]
    if content:
        status = 1
        show("TRACKED CONTENT", content)

    bad_paths = [p for p in tracked_files() if rx.search(p)]
    if bad_paths:
        status = 1
        show("TRACKED PATHS", bad_paths)

    for sha in unpushed_commits():
        if rx.search(commit_text(sha)):
            status = 1
            show(f"COMMIT MESSAGE  {git('log', '-1', '--format=%h %s', sha).strip()}",
                 [line for line in commit_text(sha).splitlines() if rx.search(line)])

    # Not a leak yet, but one `git add -A` away from being one. Reported as a warning and
    # never as a failure, so the two are not confused.
    untracked = [p for p in git("ls-files", "--others", "--exclude-standard", "-z")
                 .split("\0") if p]
    warn: list[str] = []
    for path in untracked:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                if rx.search(handle.read()) or rx.search(path):
                    warn.append(path)
        except OSError:
            continue
    if warn:
        show("WARNING -- untracked (not committed, but not ignored either)", warn)

    return status


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else ""

    if mode == "--ci":
        pattern = os.environ.get("FORBIDDEN_PATTERN", "").strip()
        if not pattern:
            # Refuse rather than pass. A green tick with no term list is a lie, and the
            # empty pattern would match every line anyway.
            print("::error::FORBIDDEN_PATTERN is not set for this repository. Add it under "
                  "Settings > Secrets and variables > Actions.")
            return 1
    else:
        pattern = git("config", "--get", "guard.pattern").strip()
        if not pattern:
            # Not an error. A contributor without the list should not be blocked from
            # committing; the CI job is the backstop that cannot be skipped.
            print("guard: no guard.pattern configured for this clone, nothing to check.")
            print('guard: set one with  git config --local guard.pattern "..."')
            return 0

    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as err:
        where = "FORBIDDEN_PATTERN" if mode == "--ci" else "guard.pattern"
        print(f"{'::error::' if mode == '--ci' else 'guard: '}{where} is not a valid "
              f"regular expression: {err}")
        return 1

    if mode == "--ci":
        return run_ci(rx)
    elif mode == "--staged":
        status = run_staged(rx)
    else:
        status = run_worktree(rx)

    print(RULE)
    if status:
        print("guard: FAILED. The terms above must not reach this repository.")
        print("guard: a working-tree fix is not enough for a commit message -- amend.")
    else:
        print("guard: clean.")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
