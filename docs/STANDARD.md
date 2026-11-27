# Project standard

Version: 7

## Documents

| File | Owns |
|---|---|
| `CONSTITUTION.md` | Purpose, boundaries, principles, evidence, contributions, amendment and a version log |
| `docs/STANDARD.md` | Build, review, approval and CI rules. It is an exact copy with a version number. |
| `AGENTS.md` | Layout, the check command and the review guidelines of the project |
| `CLAUDE.md`, `GEMINI.md` | Imports only |
| `.github/pull_request_template.md` | The evidence of a PR. It is an exact copy, with the sections `Testing`, `Risk` and `How to revert`. |

Each constitution has the same sections and the same governance block. For nookku, koodam and groundgate, move the existing text from their README, `SPEC.md` and `AGENTS.md`. Do not write new policy.

A harness loads instructions only from its checkout. Thus, the rule text must be a local copy.

## Builds

1. Each change starts from an issue.
2. The harness adds `agent:claude`, `agent:codex` or `agent:agy` to the issue. The first label wins.
3. The branch name shows the harness and the issue.
4. `make check` runs the local checks in each repository.
5. The author fills the PR template. `Testing` gives each command and its result, not only a statement that the tests pass.

The label `agent:claude-code` in nookku becomes `agent:claude`. Open branches keep their names until they merge.

## Threat model

The gates protect against these actors:

- a fork contributor with no write access;
- text in a diff or in a file that steers a model reviewer.

The gates do not protect against an actor with write access. That actor uses the owner login and has the admin bypass, so a gate cannot stop it. The rules for agents control that actor.

## Reviews

| Builder | Code reviewer | Security reviewer | Security fallback |
|---|---|---|---|
| Claude Code | Codex app | Codex security review | agy, with a security prompt |
| Codex | Claude workflow | Codex security review | Claude workflow, with a security prompt |
| agy | Claude workflow | Codex security review | Claude workflow, with a security prompt |
| No harness | Codex app | Codex security review | Claude workflow, with a security prompt |

- If the code reviewer is not available, agy does the review. If agy built the change or is not available, the owner does a manual review.
- The local agy review is `gates/cross_review.py` of `mohanraj00/.github` at the gate pin of the project. A project does not keep its own copy. Before the review, the tool sends one short prompt to agy with a time limit. If that call fails, times out or asks for a sign-in, the tool stops and sends no diff.
- Ask one time for each head commit, and at most 3 times for each PR. The `gate` job of the Claude workflow stops a second Claude review of one kind for a head, and a Claude review after 3 of one kind.
- The `cross-review` check fails if a PR has more than 3 `@codex review` requests from the owner login. The text is "Codex review cap reached. Examine the approach and report to the owner."
- At the cap, stop the requests. Examine the approach, then report to the owner. The owner decides if more reviews are necessary. To permit more requests, the owner adds the `review-cap-lifted` label. The label lifts the cap of `@codex review` requests and the cap of 3 Claude reviews of each kind. The gate records the label for the current head and base, as for the other owner labels. The rule of one request for each head commit still applies.
- When a finding shows a class of problem, fix all instances of the class in the same commit.
- Do not make a commit only for a nit. Close the thread with a reason.
- If a finding is about code that the PR did not change, file an issue. It does not block the PR.
- If the author rejects a P0, P1 or security finding, this is a reviewer disagreement.
- A reviewer gives a file and a line for each finding. The summary starts with the number of findings.
- A reviewer states what it ran. If a tool of the reviewer fails, the result is not clean.
- Fix each finding, or write the reason in the PR.
- A code review is valid only for the current head and base.
- The fallback starts if Codex reports a limit, or gives no result in 30 minutes. The 30 minutes is an estimate.
- CI cannot verify a local agy result. For a local agy security result, the owner adds the `security-reviewed` label.
- A Claude workflow security result is advisory. The `security-review` gate does not accept it. The owner reads the review, then adds the `security-reviewed` label. This is the same rule as for a local agy security result.

The gates are the files of `mohanraj00/.github` and their copies in each project. Use this stop rule for a finding in the gates:

- Fix a finding in the gates now if an actor of the threat model can reach it, and its severity is P0 or P1.
- Otherwise, add it to "Known limits" in `README.md` of `mohanraj00/.github`, close the finding with a reason, and do not change code.
- Collect the other fixes of the gates in one release each month. A project repins one time for each release.
- On the day of each monthly gates release, run `python3 tools/review_cost.py <owner/name> <first day> <last day>` from `mohanraj00/.github` for each project. Use the dates of the month before the release, and post the tables on the release PR. The script reads the `review-cost` line that each passed gate writes. A PR that ran an older gates SHA has no line.
- A reviewer disagreement about the threat model goes to the owner.

The review guidelines in `AGENTS.md` are short. They state what a failure looks like in the project, the paths to skip and the limit for nits. If a finding occurs again, add a check, a test or a line in these guidelines.

A Codex-built PR gets a Codex security review as an exception to the rule that a harness does not review its own change. The exception is limited: Claude Code still does the code review, and the security review is a separate run.

## Review requests

The agents use the owner login. An agent posts each review request in a PR comment.

- A code review request is `@codex review`, with `Head: <short SHA>` on the next line. A security review request is a separate comment with the exact text `@codex security review`.
- A Claude request is a comment with one of these two texts on its first line:
  - code review: `@claude review this PR. Use the Review guidelines in AGENTS.md.`
  - security review: `@claude do a security review of this PR. Use the Review guidelines in AGENTS.md.`
- The gate job of the Claude workflow compares only the first line. It ignores the letter case, the white space at the two ends, and the length of each run of white space. The next lines can hold other text, for example `Head: <short SHA>`. A first line with other words, or with a word more or less, does not start a review.
- For a fork PR, ask for a review only after the owner approval of stage 2.

## Security review trigger

The gate decides from the changed paths. If no listed path changed, the check passes and an agent does not ask for a review.

The shared categories are:

- workflows, release steps and gate tools;
- agent instructions and settings, and `docs/STANDARD.md`;
- dependency manifests and lock files;
- credentials, logins and RBAC;
- named boundaries for untrusted input, commands, network and file paths.

The gates compare a path with the shared lists and with the lists of the project with no regard to the letter case. A file system can ignore the letter case, and then a harness also loads `agents.md`.

Each project names its boundary files. It does not list all of its source. A code reviewer can report that a security review is necessary. Then the author adds the `security-needed` label, and the gate requires the review.

After a clean security review, a new push needs a new review only if the new commits touch a listed path.

Before you ask for a security review, read the `Security review:` line in the job summary of the `security-review` check. If the line says "not needed" or "valid", do not ask. If the line is not there, the project pins an older gates SHA.

The security review of `mohanraj00/.github` covers the copies of its files in each project. Thus these listed changes need no review:

- an exact copy of a file of `mohanraj00/.github` at the pinned SHA;
- a change of only the SHA in a pin line of the gates action, if the new SHA is on `main` of `mohanraj00/.github`;
- an action bump PR of Dependabot. The PR author is `dependabot[bot]`. Each changed line is a `uses:` line, where only the 40-character SHA and its version comment change. No other file or line changes.

If the owner lists a `.py` path in `docstring_only` of `.github/gates.json`, a change of only the text of its existing docstrings also needs no review, because the code does not change. The owner lists a path only if no tool of the project reads its docstrings. Each file keeps its path and its file mode. Its `ast` tree without the docstrings does not change, and no byte outside the docstrings changes. No file of the head tree reads a docstring or a listed source file.

The gate proves each one. Each other listed change still needs a review. The PR stays level 1.

## Docs-only PRs

A docs-only PR skips the project jobs, and `ci-ok` still passes. The project check always runs. Do not use `paths-ignore`, because a required check can then stay pending.

These `.md` files are not docs:

- agent instructions, `CONSTITUTION.md`, `docs/STANDARD.md` and skill files;
- `SPEC.md` and contract files;
- `CHANGELOG.md`, in a project that has it in `level1`;
- docs that a project test runs.

## Approvals

| Level | Applies to | Who approves | Control |
|---|---|---|---|
| 0, routine | Other PRs | No person. The author agent merges. | Required checks |
| 1, owner | The paths below, a reviewer disagreement, and the `owner-needed` label | Owner | The owner merges. An agent does not merge. |
| 2, outward | Tag, release, package, visibility, outreach | Owner, one approval for each step | Tag ruleset, `pypi` environment |

Filing a sanitized issue needs no approval.

- A reviewer can report that a change needs the owner, also for a path that is not on the list. Then the author adds the `owner-needed` label.
- An agent uses the owner login, so GitHub cannot tell an agent from the owner. Thus, an agent never approves a PR and never merges a level 1 PR.
- The owner merges a level 1 PR with the admin bypass, because GitHub does not let the PR author approve it.

Level 1 paths in all projects:

- `CONSTITUTION.md`, `docs/STANDARD.md`;
- `AGENTS.md`, `CLAUDE.md` and `GEMINI.md` in each directory, with their `.local.md` and `.override.md` forms;
- `.github/`, and in each directory `.agents/`, `.claude/`, `.codex/` and `.mcp.json`;
- the gate tools;
- dependency manifests and lock files.

Added level 1 paths:

| Project | Paths |
|---|---|
| koodam | `/api/`, `/config/rbac/`, `/internal/gateway/`, `/charts/` |
| aavanam | `SPEC.md`, `docs/contracts/`, `conformance/` |
| groundgate | `SPEC.md`, `conformance/` |
| nookku | `SPEC.md`, the relay path, the record format |
| entelos | `docs/contracts/`, the lifecycle specs (a first estimate) |
| mugham | `catalog/schema/`, `docs/contracts/` |

`CHANGELOG.md` is not a level 1 path in all projects. If a release step of a project reads `CHANGELOG.md`, the project adds it to `level1` in its `.github/gates.json`. groundgate and nookku add it when they pin the release with this rule.

## CI and GitHub settings

Each repository has the same 4 required checks:

| Check | Purpose | Source |
|---|---|---|
| `ci-ok` | Collects the jobs of the project, and accepts a skipped job | Project jobs, shared skeleton |
| `cross-review` | Requires a clean review of the current head from the other harness | Shared gate |
| `security-review` | Requires a security review if the trigger applies | Shared gate |
| `acceptance` | Controls outside PRs, a reviewer disagreement and the `owner-needed` label | Shared gate |

groundgate has a temporary exception: `outside-pr-paths` is a fifth required check. The check adds a control and removes none. The exception ends when groundgate pins the gates release that makes the agent config variants level 1 (mohanraj00/.github#23). Then groundgate adds `CHANGELOG.md` to `level1`, and removes the check, the required check setting and the `maintainer-approved` label. The `accepted` label of the PR and the owner approval of stage 2 do the work of that label. The next release of this standard removes this exception.

- The gates run from the base branch, so a PR cannot change its own gate.
- Package the gates as a composite action, so that the tools and the workflow cannot differ.
- Parse each workflow in CI. In a test, an invalid workflow passed the project check and both Codex reviews.
- Use SHA pins, `permissions: {}`, job timeouts and `persist-credentials: false`.
- Enable Dependabot for actions with an exact copy of `docs/dependabot.yml` of `mohanraj00/.github` at `.github/dependabot.yml`. It has one group for all GitHub Actions updates and a monthly schedule, so each project gets one bump PR each month. The owner merges the bump PR, because it is level 1.
- The Claude workflow uses the hardened aavanam form in each repository.
- The main ruleset has squash only, linear history and resolved threads. It dismisses a stale approval when a push occurs. Remove classic protection.
- The only bypass on `main` is the admin role, in PR-only mode. The owner uses it to merge a level 1 PR.
- Each repository uses the groundgate tag ruleset.
- Delete the branch after merge, and enable auto-merge after `cross-review` is required.
- The up-to-date rule is off. No project has an exception. In koodam, a CI check of the generated files replaces the rule (mohanraj00/koodam#329).
- After a revert, or a bug that passed all reviews, file an issue that names the layer that had to find it. Add a check, a test or a guideline line. If the reverts increase in a project, the owner can move all PRs of that project to level 1.

The gates read a Codex result with these rules:

- Accept a result only if the comment author is `chatgpt-codex-connector[bot]`, because any user can write the same text.
- Read `commit_sha` and `status` from the `codex-review-state:v1` data in the summary comment.
- Do not use `has_unresolved_findings` alone. The value stayed `true` in a test after the threads were resolved.
- For a security review, read the receipt text. The value of `has_any_findings` was `null` after a completed security review.
- If the receipt format changes, fail closed and use the fallback, because Codex calls its security review experimental.

## Fork PRs

Stage 1 is automatic and uses no credits. The `acceptance` check requires:

- a linked open issue with the label `accepted`, and without the label `decision`;
- about 400 changed lines or fewer, without generated files;
- no change to protected paths, unless the owner approved it;
- a PR description that follows the template.

Stage 2 is the owner approval of the current head and base. The owner reads the diff, then does one of these steps:

- Add the `ready-for-review` label. The gate writes an approval record for the head and base.
- Approve the PR with the GitHub Approve button. Then run the failed `acceptance` job again, because a review does not start the gates.

The `acceptance` check fails for a fork PR without this approval. No review starts before this. The `gate` job of the Claude workflow uses the same rule, so a Claude review of a fork PR does not start without this approval. The `review` job reviews only the head and base that the `gate` job approved.

- The approval allows one code review, and one security review if the trigger applies.
- A push removes the label, and an approval of an earlier commit does not count.
- Agents never add the label.
- CI needs approval for all outside contributors.

## Worktrees

- A worktree that an app makes stays at the location of the app.
- A worktree that a person or agent makes by hand goes to the `worktrees/` folder of the owner's local work root, as the global instructions say.
- Each repository ignores `.claude/worktrees`.
- The author removes the worktree after the merge.

The public standard does not name the local path. A public repository must not hold a personal path.

The current Claude app location is inside the checkout. There, a harness can load the instruction files of the parent checkout. The owner can change this location in the app settings.
