# Changelog

Each release is a tag on `main`. A project repins to the commit of the tag. See [Repin to a release](docs/repin.md).

## v2026.11.0 (2026-11-27)

Commit: `05c2fba15c81b4d3a35ebad71cb5b14dbda050d3`.

- The gates read the Claude count from the first count line (#25).
- The `review-cap-lifted` label lifts the Claude review cap (#41).
- The queue keeps an owner label while it delays its record (#42).
- The security review skips a docstring-only change (#43).
- The "Added level 1 paths" table in `docs/STANDARD.md` has a row for mugham (#24).
- The agent config variants and their letter cases are level 1. `CHANGELOG.md` is not a shared level 1 path (#23).

Added repin steps:

1. Copy `gates/claude_review.py` of the new SHA to `tools/claude_review.py`. This release changes that file (#25).
2. Read the changes in `docs/STANDARD.md`. The standard is now Version 7.
3. If a release step of the project copies `CHANGELOG.md` into the release notes, add `CHANGELOG.md` to `level1` of `.github/gates.json`. See [Shared categories](docs/adopt.md#shared-categories).
