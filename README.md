# .github
Shared project standard and CI gates

## Shared gates

The composite action in [`gates/`](gates/action.yml) holds the gates `cross-review`, `security-review` and `acceptance` of [the standard](docs/STANDARD.md). The tools are next to `action.yml`, so the workflow and the tools come from the same SHA. The tools use the Python standard library only, and they call only the GitHub API.

### Adopt the gates in a project

1. Copy [`docs/gates-caller.yml`](docs/gates-caller.yml) to `.github/workflows/gates.yml`. Replace the SHA of the gates action with a full SHA of this repository on `main`. Do not use a tag or a branch.
2. Copy [`docs/gates-rerun.yml`](docs/gates-rerun.yml) to `.github/workflows/gates-rerun.yml`.
3. Add `.github/gates.json` with the project rules. The keys are below. The gate reads this file from the base branch, so a PR cannot change the config of its own gate.
4. Put the project jobs in the skeleton [`docs/ci-ok.yml`](docs/ci-ok.yml), and use the same SHA.
5. If the project has the Claude review workflow, copy [`gates/claude_review.py`](gates/claude_review.py) to `tools/claude_review.py`. Use [`docs/claude-review.yml`](docs/claude-review.yml) as the workflow, at the path `.github/workflows/claude-review.yml`. Its post job gives the `--kind` argument, and its diff starts at the base branch of the PR. The gates accept a marker only if its run is an `issue_comment` run of this workflow on the default branch. Thus a workflow on a branch cannot make a marker that the gates accept.
6. In the main ruleset, require the 4 checks `ci-ok`, `cross-review`, `security-review` and `acceptance`.
7. Remove the old gate workflows and tools of the project, for example `pr-policy.yml` and `tools/pr_policy.py`. The gates also read the approval records of `pr_policy.py`.

The keys of `.github/gates.json` are lists of `fnmatch` patterns. A `*` also matches `/`.

| Key | Contents |
|---|---|
| `version` | `1` |
| `code` | The paths that run the project jobs. A path in a security or level 1 list also runs them. |
| `security_sources` | The executable sources that are security relevant |
| `not_security` | The paths of `security_sources` that are not security relevant, for example tests |
| `boundaries` | The gate tools and the named boundary files. These are always security relevant. |
| `level1` | The gate tools and the added level 1 paths of the project |
| `generated` | The generated files. The fork line limit does not count them. |

The action holds the shared categories: the workflows, the agent instructions and settings, `docs/STANDARD.md`, and the dependency manifests and lock files.

### What each gate does

- `cross-review` finds the builder from the first `agent:*` label of the linked issue, then from the branch prefix. A Claude-built PR, or a PR with no harness, needs a completed Codex code review of the current head. A Codex-built or agy-built PR needs a Claude workflow review of the current head with `Findings: 0`. The `cross-reviewed` label with an approval record is the fallback for an agy or owner review.
- `security-review` passes if no listed path changed. Otherwise it needs a clean Codex security receipt, or the `security-reviewed` label with an approval record. A clean Claude security review is advisory. The gate writes a line about it, and the owner adds the `security-reviewed` label after the owner reads the review. A Codex security receipt with open findings stops the pass. After a clean review, a new push needs a new review only if the new commits touch a listed path. The `security-needed` label forces a review of the current head.
- `acceptance` applies stage 1 and stage 2 of "Fork PRs" to a fork PR. Stage 2 needs an owner approval of the current head and base: an admin review with the state `APPROVED` for the head, or the `ready-for-review` label with its approval record. The latest review of each user decides, and a dismissed review does not count. For each PR, it fails if a level 1 path changed or if the PR has the `owner-needed` label. Then the owner merges the PR with the admin bypass, and auto-merge cannot merge it. A push removes the `ready-for-review` label from a fork PR.

When a user with the admin role adds `accepted`, `ready-for-review`, `security-reviewed` or `cross-reviewed` to a PR, the gate posts an approval record for that head and base. A label from a triage or write user gets no record. For a fork PR, an admin must add the `accepted` label of the linked issue. A label without a record for the current head and base is not valid, and the gate removes it.

If the API gives an error, or a receipt has an unknown format, the gate fails. It does not pass. A change to the labels of a linked issue does not start the gates. Edit the PR, or run the failed jobs again.

Only an admin can clear the blocking labels `owner-needed`, `security-needed` and the `decision` label of a linked issue. If another user removes one of them, the gate still applies it.

A review does not start the gates. For a fork PR, the `pull_request_review` event runs the workflow file of the PR with a read-only token, so the gates do not use it. After you approve a fork PR with the Approve button, run the failed `acceptance` job again, for example with `gh run rerun <run-id> --failed`. The `ready-for-review` label starts the gates, so it needs no rerun.

GitHub Actions has no event for a resolved review thread. If you resolve the last Codex thread without a push, run the failed `cross-review` job again, for example with `gh run rerun <run-id> --failed`.

### Known limits

The agents use the owner login. Thus GitHub cannot tell if the owner or an agent added a label. The labels `accepted`, `security-reviewed`, `cross-reviewed` and `ready-for-review` depend on the rule that agents never add them.

The gates trust the finding count of a model review. The Codex security receipt also comes from a model that reads the diff, and the gate accepts it. If the first line of a Claude review gives `Findings: 0` and a later line starts with a severity tag or a `path:line` reference, the marker gives `findings=unknown`. This check blocks only a count that does not agree with the text. It does not block a model that omits a finding.

## Constitution template

Each project constitution uses the sections and the exact governance block in [`docs/constitution-template.md`](docs/constitution-template.md). The block has version markers, so a check can compare it with the template.
