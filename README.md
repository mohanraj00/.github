# .github
Shared project standard and CI gates

## Shared gates

The composite action in [`gates/`](gates/action.yml) holds the gates `cross-review`, `security-review` and `acceptance` of [the standard](docs/STANDARD.md), and the `claude-review` check of the Claude review workflow. The tools are next to `action.yml`, so the workflow and the tools come from the same SHA. The tools use the Python standard library only, and they call only the GitHub API.

### Adopt the gates in a project

Use two PRs. The gates read `.github/gates.json` from the base branch, so the config must be on the base branch before the adoption PR.

1. Merge a PR that adds only `.github/gates.json` with the project rules. The keys are below. Because the gate reads this file from the base branch, a PR cannot change the config of its own gate.
2. Open the adoption PR. Copy [`docs/gates-caller.yml`](docs/gates-caller.yml) to `.github/workflows/gates.yml`. Replace the SHA of the gates action with a full SHA of this repository on `main`. Do not use a tag or a branch.
3. Copy [`docs/gates-rerun.yml`](docs/gates-rerun.yml) to `.github/workflows/gates-rerun.yml`.
4. Put the project jobs in the skeleton [`docs/ci-ok.yml`](docs/ci-ok.yml), and use the same SHA.
5. If the project has the Claude review workflow, copy [`gates/claude_review.py`](gates/claude_review.py) to `tools/claude_review.py`. Use [`docs/claude-review.yml`](docs/claude-review.yml) as the workflow, at the path `.github/workflows/claude-review.yml`. Replace the SHA of the gates action in its `gate` job with the SHA of step 2. Its post job gives the `--kind` argument, and its diff starts at the merge base of the base branch and the head. The marker names this merge base. The gates accept a marker only if its run is an `issue_comment` run of this workflow on the default branch. Thus a workflow on a branch cannot make a marker that the gates accept. When you change the SHA of the gates action, copy `gates/claude_review.py` of the same SHA. The gates do not accept a `v1` marker, because it names no merge base.
6. Remove the old gate workflows and tools of the project, for example `pr-policy.yml` and `tools/pr_policy.py`. The gates also read the approval records of `pr_policy.py`.
7. Merge the adoption PR. The checks `cross-review`, `security-review` and `acceptance` do not run on the adoption PR, because `pull_request_target` uses the workflow of the base branch. Only `ci-ok` runs on it.
8. After the merge, in the main ruleset, require the 4 checks `ci-ok`, `cross-review`, `security-review` and `acceptance`. Do not require the 3 gate checks before the merge, because they cannot pass on the adoption PR.

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

- `cross-review` finds the builder from the first `agent:*` label of the linked issue, then from the branch prefix. A Claude-built PR, or a PR with no harness, needs a completed Codex code review of the current head. A Codex-built or agy-built PR needs a Claude workflow review of the current head with `Findings: 0`. The marker of the review must name the current merge base of the base branch and the head. A new commit on the base branch does not change the merge base, so the review stays valid. A base change of the PR stops the review. The `cross-reviewed` label with an approval record is the fallback for an agy or owner review.
- `security-review` passes if no listed path changed. Otherwise it needs a clean Codex security receipt, or the `security-reviewed` label with an approval record. A clean Claude security review is advisory. The gate writes a line about it, and the owner adds the `security-reviewed` label after the owner reads the review. A Codex security receipt with open findings stops the pass. After a clean review, a new push needs a new review only if the new commits touch a listed path. The `security-needed` label forces a review of the current head.
- The copy check of `security-review` reads the old pin from `.github/workflows/gates.yml` of the base branch. A listed path passes with no review if it is an exact copy of its file in this repository at the new pin, by the Git blob SHA. The copy paths are the paths of "Adopt the gates in a project". The copy of `claude-review.yml` can differ from the template only in the SHA of the gates action, and only with the new pin. A listed path also passes if it changes only the SHA in its `mohanraj00/.github/gates@<SHA>` lines to the new pin. The compare API must show the new pin on `main` of this repository. If each listed path is proved, the check passes and writes each proved path. If the API gives an error, the copy check fails closed, and the PR needs a review.
- `claude-review` is the `gate` job of the Claude review workflow. It runs before the `review` job, with no environment and no secret. For a fork PR, it requires the stage 2 owner approval of the current head and base, with the same code as `acceptance`. It stops a request if a trusted marker of the same kind names the current head and merge base, or if the PR has 3 trusted markers of the same kind. The post job then posts the reason, and the job log has it. On a pass, it gives the head, the base and the merge base to the `review` job. The `review` job fails if the PR head, base or merge base changed after the gate. The workflow has a concurrency group for each PR, so the requests on one PR run one at a time. A new pending request replaces an older pending request, and the older request posts no comment.
- `acceptance` applies stage 1 and stage 2 of "Fork PRs" to a fork PR. Stage 2 needs an owner approval of the current head and base: an admin review with the state `APPROVED` for the head, or the `ready-for-review` label with its approval record. The latest review of each user decides, and a dismissed review does not count. For each PR, it fails if a level 1 path changed or if the PR has the `owner-needed` label. Then the owner merges the PR with the admin bypass, and auto-merge cannot merge it. A push removes the `ready-for-review` label from a fork PR.

When a user with the admin role adds `accepted`, `ready-for-review`, `security-reviewed` or `cross-reviewed` to a PR, the gate posts an approval record for that head and base. A label from a triage or write user gets no record. For a fork PR, an admin must add the `accepted` label of the linked issue. A label without a record for the current head and base is not valid, and the gate removes it.

If the API gives an error, or a receipt has an unknown format, the gate fails. It does not pass.

Only an admin can clear the blocking labels `owner-needed`, `security-needed` and the `decision` label of a linked issue. If another user removes one of them, the gate still applies it.

### Known limits

This section holds each known limit of the gates. A finding that the stop rule of the standard does not fix also goes here, with its reason. The threat model of the standard excludes an actor with write access.

#### Owner login

The agents use the owner login. Thus GitHub cannot tell if the owner or an agent added a label. The labels `accepted`, `security-reviewed`, `cross-reviewed` and `ready-for-review` depend on the rule that agents never add them.

#### Count of a model review

The gates trust the finding count of a model review. The Codex security receipt also comes from a model that reads the diff, and the gate accepts it. The marker gives `findings=unknown` if the text of a Claude review does not agree with the count on its first line:

- `INCOMPLETE REVIEW` is on a line below the first line.
- A later `Findings: N` line gives a different count.
- The count is 0, and a later line starts with a severity tag or a `path:line` reference.
- The count is 0, and a severity tag P0 to P3 is at any position in a later line. Thus a clean review that writes "no P1 defect" also gives `findings=unknown`.

The check reads the text in NFKC form with no format characters. Thus a full-width or a zero-width character cannot hide a signal. This check blocks only a count that does not agree with the text. It does not block prose that describes a defect with no severity tag and no `path:line` reference. It does not block a model that omits a finding. A prompt injection in the diff can cause these two results.

#### Events that do not start the gates

A change to the labels of a linked issue does not start the gates. Edit the PR, or run the failed jobs again.

A review does not start the gates. For a fork PR, the `pull_request_review` event runs the workflow file of the PR with a read-only token, so the gates do not use it. After you approve a fork PR with the Approve button, run the failed `acceptance` job again, for example with `gh run rerun <run-id> --failed`. The `ready-for-review` label starts the gates, so it needs no rerun.

GitHub Actions has no event for a resolved review thread. If you resolve the last Codex thread without a push, run the failed `cross-review` job again, for example with `gh run rerun <run-id> --failed`.

#### Base change during a Claude review (#16)

If the base of a PR changes while a Claude review runs, the run does not stop. It uses the OAuth token to the end and posts a review of the old diff. Its marker counts toward the cap of 3. The gates accept the marker only if it names the current merge base. A user with write access can do this on each PR, and the threat model excludes that actor. A fork author can also change the base, but a fork run starts only after the owner approval of the head and base. Thus a fork author can make the one run of an approval useless, but cannot make a marker that a gate accepts.

#### Burst of Claude review requests (#19, item 2)

The `gate` job and the concurrency group limit the Claude runs for each PR. A user with write access can still post requests on many PRs in a short time. Each run uses the OAuth token for up to 30 minutes. The threat model excludes that actor, so the gates do not limit the runs across PRs.

#### Some pin lines unchanged in the copy check (#22, P2)

If a PR changes only some of the pin lines in `gates.yml`, the copy check uses the SHA of the changed lines as the new pin. The unchanged lines keep the old SHA, so some gates run at the old SHA after the merge. The next copy check then finds two pins on the base branch, and it fails closed. The stop rule does not fix this now, because its severity is P2. The PR is level 1, so the owner reads the pin lines before the merge.

#### Pin to an older commit of main (#22, P3)

The copy check accepts each new pin that is on `main` of this repository, also a commit before the old pin. Thus a PR can move a project to an older release of the gates with no security review. The stop rule does not fix this now, because its severity is P3. The PR is level 1, so the owner decides each pin change before the merge.

## Constitution template

Each project constitution uses the sections and the exact governance block in [`docs/constitution-template.md`](docs/constitution-template.md). The block has version markers, so a check can compare it with the template.
