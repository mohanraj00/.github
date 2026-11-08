# Constitution template

Each project constitution uses the sections below, in this order. The governance block is an exact copy. The other sections hold the text of the project.

When you make a constitution, move the existing text of the project from its README, `SPEC.md`, `AGENTS.md` or old constitution into these sections. Do not write new policy. If a text fits no section, keep it in its old file.

## Sections

| Section | Contents |
|---|---|
| `# <Project> constitution` | The title. The next line is `Version <n>, <YYYY-MM-DD>.` |
| `## Purpose` | What the project is, and who uses it |
| `## Boundaries` | What the project owns, what its users own, and what is out of scope |
| `## Principles` | The rules that each change must keep |
| `## Evidence` | How the project proves a claim: specs, conformance, tests and benchmarks |
| `## Contributions` | What a contribution must not add, for example private material, and the license |
| `## Governance` | The exact governance block below |
| `## Amendment` | The exact amendment text below |
| `## Version log` | One entry for each version, the newest first |

A project can add `###` subsections inside a section, for example `### Consumers` under `## Purpose`. Do not add other `##` sections.

## Governance block

Copy the text between the markers into the `## Governance` section. Keep the markers.

<!-- governance:start v1 -->
This project follows the project standard in `docs/STANDARD.md`. That file is an exact copy from `mohanraj00/.github`. The standard owns the rules for builds, reviews, approvals and CI. A project exception to the standard is valid only when the standard records it.

The documents own these topics:

- `CONSTITUTION.md`: the purpose, the boundaries, the principles, the evidence rules, the contribution rules and the version log.
- `docs/STANDARD.md`: the builds, the reviews, the approvals and CI.
- `AGENTS.md`: the layout, the check command and the review guidelines.
- `CLAUDE.md` and `GEMINI.md`: imports only.

These rules apply to each change:

1. A harness does not review its own change. The Codex security review is the one exception, as the standard says.
2. The agents use the owner login. An agent never approves a PR and never merges a level 1 PR. An agent never adds the labels `accepted`, `security-reviewed`, `cross-reviewed` or `ready-for-review`.
3. The owner merges each level 1 PR.
4. Each tag, release, package publication, visibility change and outreach step needs a separate owner approval.
<!-- governance:end v1 -->

## Amendment text

Copy this text into the `## Amendment` section:

```markdown
Change this constitution when the owner changes direction or evidence changes a decision. A change is a level 1 PR. Record the reason in the change, and add an entry to the version log.

Change the governance block only with a new version of the template in `mohanraj00/.github`.
```

## Version log entry

Each entry has this form:

```markdown
### Version <n>, <YYYY-MM-DD>

<The change.> Reason: <the reason>.
```
