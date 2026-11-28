# Repin to a release

Release: v2026.11.0.

## Purpose

Move the gates of a project to a new release of this repository. A release occurs one time each month. A project repins one time for each release. [CHANGELOG.md](../CHANGELOG.md) gives the changes and the added steps of each release.

## Steps

Each pin of the gates action in each workflow of the project uses the same SHA.

1. Get the full commit SHA of the release tag. Use the SHA, not the tag.
2. In `.github/workflows/gates.yml`, change the SHA of the gates action to this SHA.
3. In the `check: classify` step of `.github/workflows/ci.yml`, change the SHA of the gates action to the same SHA. This step selects the project jobs. If it keeps the old SHA, it uses the old path lists.
4. If the project has the Claude review workflow, change the SHA of the gates action in `.github/workflows/claude-review.yml` to the same SHA. Then copy `gates/claude_review.py` of that SHA to `tools/claude_review.py`.
5. Do the added steps of the release in [CHANGELOG.md](../CHANGELOG.md).
6. Open the PR. It is level 1, because it changes `.github/`. The owner merges it.

## Example

These commands repin a project to v2026.11.0. Run them from the root of the project checkout.

```bash
SHA=$(git ls-remote https://github.com/mohanraj00/.github.git 'refs/tags/v2026.11.0^{}' | cut -f1)
echo "$SHA"
sed -i.bak "s|mohanraj00/.github/gates@[0-9a-f]\{40\}|mohanraj00/.github/gates@$SHA|" .github/workflows/gates.yml .github/workflows/ci.yml .github/workflows/claude-review.yml
rm .github/workflows/*.bak
curl -fsSL "https://raw.githubusercontent.com/mohanraj00/.github/$SHA/gates/claude_review.py" -o tools/claude_review.py
grep -rn "mohanraj00/.github/gates@" .github/workflows
```

The tag is an annotated tag, so the `^{}` form gives the commit. If the project does not use the Claude review, remove `claude-review.yml` from the `sed` command and do not run `curl`.

## Result

`echo` shows `05c2fba15c81b4d3a35ebad71cb5b14dbda050d3`. `grep` shows each pin line with this SHA.

On the repin PR, the copy check of `security-review` proves each pin change and each exact copy. Then the PR needs no security review. The job summary shows `Security review: not needed. Each listed path is a proved copy or pin change.`

## Failure texts

| Text | Cause | Action |
|---|---|---|
| `Security review: needed. The PR changes: …` | A listed path is not a proved copy or pin change. For example, `tools/claude_review.py` is not an exact copy at the new SHA. | Copy the file again from the new SHA. If the change is intended, ask for a security review. |
| `The copy check failed closed (…)` | The base branch has two different pins, or the API gave an error. | Make each pin on the base branch the same. Then run the failed job again. |

A pin to a SHA that is not on `main` of this repository is not a proved pin change.
