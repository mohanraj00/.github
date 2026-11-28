# Adopt the gates

Release: v2026.11.0.

## Purpose

Add the 4 required checks `ci-ok`, `cross-review`, `security-review` and `acceptance` to a project. The [standard](STANDARD.md) gives the rules. This page gives the steps.

## Steps

Use two PRs. The gates read `.github/gates.json` from the base branch, so the config must be on the base branch before the adoption PR. Thus a PR cannot change the config of its own gate.

1. Merge a PR that adds only `.github/gates.json`. See [Project config](#project-config).
2. Open the adoption PR. Copy [`gates-caller.yml`](gates-caller.yml) to `.github/workflows/gates.yml`.
3. Copy [`gates-rerun.yml`](gates-rerun.yml) to `.github/workflows/gates-rerun.yml`.
4. Copy the skeleton [`ci-ok.yml`](ci-ok.yml) to `.github/workflows/ci.yml`. Put the project jobs in it.
5. If the project uses the Claude review, do the steps in [Claude review workflow](claude-review-workflow.md).
6. Replace each `mohanraj00/.github/gates@0000…` pin with one full commit SHA of `main` of this repository. Do not use a tag or a branch. Each pin uses the same SHA.
7. Remove the old gate workflows and tools of the project, for example `tools/cross_review.py`, `pr-policy.yml` and `tools/pr_policy.py`. The gates also read the approval records of `pr_policy.py`.
8. Copy [`dependabot.yml`](dependabot.yml) to `.github/dependabot.yml`. Do not change it.
9. Merge the adoption PR. Only `ci-ok` runs on it, because `pull_request_target` uses the workflow of the base branch.
10. After the merge, in the main ruleset, require the 4 checks. Do not require them before the merge, because the 3 gate checks cannot pass on the adoption PR.

For the local agy review, the project copies nothing. See [Local agy review](agy-review.md).

## Example

This command sets the pin of step 6 in the 3 workflow files. `05c2fba15c81b4d3a35ebad71cb5b14dbda050d3` is the commit of release v2026.11.0. If the project does not use the Claude review, remove `claude-review.yml` from the command.

```bash
SHA=05c2fba15c81b4d3a35ebad71cb5b14dbda050d3
sed -i.bak "s|mohanraj00/.github/gates@[0-9a-f]\{40\}|mohanraj00/.github/gates@$SHA|" .github/workflows/gates.yml .github/workflows/ci.yml .github/workflows/claude-review.yml
rm .github/workflows/*.bak
grep -rn "mohanraj00/.github/gates@" .github/workflows
```

## Result

`grep` shows each pin line with the same SHA. After the adoption PR merges, the next PR shows the 4 checks.

## Project config

The keys of `.github/gates.json` are lists of `fnmatch` patterns. A `*` also matches `/`. This is the config of this repository:

```json
{
  "version": 1,
  "code": ["gates/*", "tests/*", "tools/*", "docs/*.yml", "Makefile"],
  "security_sources": [],
  "not_security": [],
  "boundaries": ["gates/*", "docs/*.yml"],
  "level1": ["gates/*", "docs/*.yml"],
  "generated": []
}
```

| Key | Contents |
|---|---|
| `version` | `1` |
| `code` | The paths that run the project jobs. A path in a security or level 1 list also runs them. |
| `security_sources` | The executable sources that are security relevant |
| `not_security` | The paths of `security_sources` that are not security relevant, for example tests |
| `boundaries` | The gate tools and the named boundary files. These are always security relevant. |
| `level1` | The gate tools and the added level 1 paths of the project |
| `generated` | The generated files. The fork line limit does not count them. |
| `docstring_only` | The `.py` paths for the docstring check of `security-review`. The default is no path, and then the check is off. Add a path only if no tool, docs build, test or script of the project reads its docstrings. |

## Shared categories

The action holds these lists. A project does not list these paths in `.github/gates.json`.

| Category | Paths | Level 1 | Security relevant |
|---|---|---|---|
| GitHub config | `.github/` | Yes | Yes |
| Standard documents | `CONSTITUTION.md`, `docs/STANDARD.md` | Yes | Yes |
| Standard documents | `docs/REVIEW.md`, `SECURITY.md` | No | Yes |
| Agent instructions | `AGENTS.md`, `CLAUDE.md` and `GEMINI.md`, with their `.local.md` and `.override.md` forms, in each directory | Yes | Yes |
| Agent instructions | `SKILL.md` in each directory | No | Yes |
| Agent config | `.agents/`, `.claude/`, `.codex/` and `.mcp.json` in each directory, also the bare directory name | Yes | Yes |
| Agent config | `.gemini/` in each directory | No | Yes |
| Dependency manifests and lock files | The usual names of Go, Node, Python, Rust, Ruby, Helm and Nix, in each directory | Yes | Yes |

- The gates compare a path with the shared lists, `code`, `security_sources`, `boundaries` and `level1` with no regard to the letter case and the Unicode form. A file system can ignore the letter case, and then a harness also loads `agents.md`.
- The lists `not_security`, `generated` and `docstring_only` remove a control. Thus the gates compare them with the exact letter case.
- `CHANGELOG.md` is not on the shared lists. If a release step of the project copies its text into the release notes, add `CHANGELOG.md` to `level1`. Do not add it to `boundaries`, because no tool runs it.
- An instruction file of a different harness, or a file that an instruction file imports, is not on the shared lists. Add its path to `level1` and `boundaries`.

## Failure texts

| Text | Cause | Action |
|---|---|---|
| `Cannot read the project config .github/gates.json on the base branch: …` | The base branch has no valid config. | Merge the config PR of step 1 first. |
| `The project config … must be a JSON object with "version": 1.` | The `version` key is missing or wrong. | Set `"version": 1`. |
| `The project config … has unknown keys: …` | A key is not in the table. | Remove the key, or correct its spelling. |
| `The key "…" of the project config must be a list of path patterns.` | A value is not a list of strings. | Use a list of non-empty strings. |
