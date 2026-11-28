# Claude review workflow

Release: v2026.11.0.

## Purpose

Claude reviews a PR that Codex or agy built, in GitHub Actions. The `post` job writes a marker comment. The `cross-review` gate reads a code review marker. A security review marker is advisory. See [Gate checks](checks.md).

## Install

1. Copy [`claude-review.yml`](claude-review.yml) to `.github/workflows/claude-review.yml`. Keep this path. The gates accept a marker only from an `issue_comment` run of the workflow at this path on the default branch.
2. Set the SHA of the gates action in its `gate` job to the SHA of the other pins of the project.
3. Copy [`gates/claude_review.py`](../gates/claude_review.py) of that SHA to `tools/claude_review.py`.
4. Make the environment `claude-review`, with the secret `CLAUDE_CODE_OAUTH_TOKEN`.

## Request a review

A writer posts a PR comment. The first line must be one of these two texts:

- code review: `@claude review this PR. Use the Review guidelines in AGENTS.md.`
- security review: `@claude do a security review of this PR. Use the Review guidelines in AGENTS.md.`

The `gate` job compares only the first line. It ignores the letter case and the length of each run of white space. The next lines can hold other text.

## Example

```bash
gh pr comment 123 --body "@claude review this PR. Use the Review guidelines in AGENTS.md.
Head: 1a2b3c4"
```

## Jobs

1. `gate`: no environment and no secret. It approves the request, or it stops it. For a fork PR, it needs the stage 2 owner approval of the current head and base.
2. `review`: Claude reviews the diff from the merge base of the base branch and the head. The comment text never goes into the prompt.
3. `post`: posts the review with a marker. The marker names the head, the merge base and the finding count.

## Result

The `post` job posts the review. The first count line is `Findings: N`. If `N` is `0` for the current head, the `cross-review` check of a Codex-built or agy-built PR passes.

## Limits of the requests

- One review of each kind for each head and merge base.
- At most 3 reviews of each kind for each PR. The `review-cap-lifted` owner label lifts this cap.
- Requests on one PR run one at a time.

## Failure texts

The `post` job posts the reason when the `gate` job stops a request.

| Text | Cause | Action |
|---|---|---|
| `A trusted Claude <kind> review of head <SHA> on the current merge base is on the PR. …` | A review of this kind for this head exists. | Read that review. Push a fix before you ask again. |
| `The PR has <N> trusted Claude <kind> reviews. The cap is 3 for each PR. …` | The cap is reached. | Examine the approach, then report to the owner. |
| `Stage 2 needs an owner approval of head <SHA> on base <branch> before a review. …` | A fork PR has no owner approval of the current head. | The owner reads the diff and approves. |
| `The gate cannot get the merge base of head <SHA> and base <branch>.` | The API gave no merge base. | Run the failed job again. |
| `The claude-review check runs only for an issue_comment event on a PR.` | The workflow ran for a different event. | Use the template triggers. |
| `findings=unknown` in the marker | The count line does not agree with the review text. | A new request for the same head does not start. Fix the findings, push, and ask again for the new head. If the review has no finding, the owner reviews the change and adds `cross-reviewed`. See [Known limits](known-limits.md#count-of-a-model-review). |
