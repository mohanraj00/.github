# Gate checks

Release: v2026.11.0.

## Purpose

The gates action runs one check for each `check:` input. `cross-review`, `security-review` and `acceptance` are required checks of each project. `classify` is a step of `ci-ok`. `claude-review` is the `gate` job of the [Claude review workflow](claude-review-workflow.md).

The gates run from the base branch, so a PR cannot change its own gate. If the API gives an error, or a receipt has an unknown format, the check fails. It does not pass.

## Workflow step

```yaml
- uses: mohanraj00/.github/gates@<full SHA> # The same SHA in each workflow
  with:
    check: cross-review # or security-review, acceptance, classify, claude-review
```

The templates [`gates-caller.yml`](gates-caller.yml) and [`ci-ok.yml`](ci-ok.yml) hold the full jobs.

## Example: read a result

```bash
gh pr checks 123
gh run view <run-id> --log | grep -E "Builder:|Security review:|Level"
```

Each check writes its reason lines in the job log. `security-review` also writes its `Security review:` line in the job summary.

## classify

Selects the project jobs of `ci-ok`. It reads `.github/gates.json` from the base branch, so a PR cannot mark its own change as docs.

- Output `docs_only=true` and `code=false` if no changed path is on a security, level 1 or `code` list. The log line is `Docs-only change.`
- Else `docs_only=false` and `code=true`. The log line is `The project jobs run.`
- With no PR, for example a push to `main`, the project jobs run.

You can run the same function on your computer. See the [quickstart](../README.md#quickstart).

## cross-review

Requires a clean review of the current head from the other harness. The builder comes from the first `agent:*` label of the linked issue, then from the branch prefix.

| Builder | Passes when |
|---|---|
| Claude, or no harness | A completed Codex code review of the current head has no findings, or each Codex thread is resolved. |
| Codex or agy | The latest Claude code review of the current head gives `Findings: 0`, and its marker names the current merge base. |
| Any | The owner adds `cross-reviewed` after an agy or owner review of the current head. |

A new commit on the base branch does not change the merge base, so a Claude review stays valid. A base change of the PR stops it. The check fails if the owner login posted more than 3 `@codex review` requests, unless the `review-cap-lifted` label applies to the current head.

| Text | Action |
|---|---|
| `No completed Codex code review of head <SHA>. Post "@codex review" with "Head: <SHA>" on the next line.` | Ask for the review. The check runs again when the review arrives. |
| `The Codex code review of head <SHA> has <N> unresolved threads. …` | Fix each finding, or write the reason and resolve the thread. Then run the failed job again. |
| `Codex review cap reached. Examine the approach and report to the owner.` | Stop the requests. Report to the owner. |
| `No Claude code review of head <SHA>. …` | Post the Claude request. |
| `The latest Claude code review of head <SHA> gives <N> findings. Fix them and ask again.` | Fix the findings, push, and ask again. |
| `The cross-reviewed label has no approval record for head <SHA> on base <branch>. …` | The owner reviews the current change and adds the label again. |

## security-review

Requires a security review if a listed path changed. The listed paths are the [shared categories](adopt.md#shared-categories), `boundaries`, and `security_sources` without `not_security`.

The check passes with no review in these cases:

- No listed path changed.
- Copy check: each listed path is an exact copy of its file in this repository at the new pin, or it changes only the SHA of its pin lines. The new pin must be on `main` of this repository. The copy check reads the old pin from `gates.yml` of the base branch.
- Bump check: the PR author is `dependabot[bot]`, and each changed line is a `uses:` line where only the 40-character SHA and its version comment change.
- Docstring check: each listed path is in `docstring_only`, and only the text of its existing docstrings changes. The check is off by default.

Else the check needs a clean `@codex security review` of the current head, or the `security-reviewed` owner label. A clean Claude security review is advisory: the owner reads it, then adds the label. After a clean review, a new push needs a new review only if the new commits touch a listed path. The `security-needed` label forces a review of the current head and stops the bump check.

Read the `Security review:` line before you ask for a review:

| Line | Action |
|---|---|
| `Security review: not needed. …` | Do not ask. |
| `Security review: the clean review of head <SHA> is valid.` | Do not ask. |
| `Security review: the clean review is still valid. …` | Do not ask. |
| `Security review: needed. The PR changes: <paths>.` | Post `@codex security review` as a separate comment. |
| `Security review: needed. The security-needed label requires a review of head <SHA>.` | Post `@codex security review`. |

| Text | Action |
|---|---|
| `The latest Codex security review has open findings. …` | Fix them and ask again, or write the answer on the PR and ask the owner to add `security-reviewed`. |
| `The copy check failed closed (…)` | See [Repin to a release](repin.md). |
| `The bump check failed closed (…)` | Comment `@dependabot rebase` on the PR. |

## acceptance

Controls the approval level, outside PRs and the blocking labels.

- For each PR, it fails if a level 1 path changed or if the PR has the `owner-needed` label. Then the owner merges with the admin bypass, and auto-merge cannot merge it.
- For a fork PR, stage 1 needs a linked open issue with `accepted` and without `decision`, about 400 changed lines or fewer, no protected path without owner approval, and a filled PR template.
- For a fork PR, stage 2 needs an owner approval of the current head and base: an admin review `APPROVED`, or the `ready-for-review` label with its record. A push removes the label.

| Text | Action |
|---|---|
| `Level 0: no level 1 path, no owner-needed label…` | Pass. The author agent can merge. |
| `This PR is level 1 (<paths>). The owner merges this PR with the admin bypass, …` | Stop. The owner merges. |
| `Link an open issue that has the label accepted and not the label decision. …` | Link the issue. An admin adds `accepted`. |
| `The PR changes <N> lines without generated files. The limit is about <N>.` | Split the PR. |
| `Fill these sections of the PR template: …` | Fill the sections. |
| `Stage 2 needs an owner approval of head <SHA> on base <branch>. …` | The owner reads the diff, then approves. |

## Owner labels

When an admin adds `accepted`, `ready-for-review`, `security-reviewed`, `cross-reviewed` or `review-cap-lifted`, the gate posts an approval record for that head and base. A label without a record for the current head and base is not valid, and the gate removes it. A label from a triage or write user gets no record. The Actions queue can delay the record. Then the label is pending, and the gate keeps it.

Only an admin can clear `owner-needed`, `security-needed` and the `decision` label of a linked issue. If another user removes one of them, the gate still applies it. Agents never add an owner label.

## Run a check again

These events do not start the gates:

- a change to the labels of a linked issue;
- a review, also an approval with the Approve button;
- a resolved review thread.

After one of these events, run the failed job again:

```bash
gh run rerun <run-id> --failed
```

The `gates-rerun` workflow runs the failed gate jobs again when a Codex result arrives or a Claude review run completes.
