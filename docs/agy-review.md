# Local agy review

Release: v2026.11.0.

## Purpose

agy reviews a change on your computer. Use it only when the GitHub review by Codex or Claude is not available. See the review table in the [standard](STANDARD.md#reviews).

The tool is [`gates/cross_review.py`](../gates/cross_review.py) at the gate pin of the project. A project does not keep its own copy.

## Before you start

- Install agy, and sign in.
- Get a checkout of this repository at the gate pin of the project.
- If you give `--work`, use a new, empty directory outside the project checkout.

## Command

Run this from the root of the project checkout:

```bash
python3 -B <gates checkout>/gates/cross_review.py --base origin/main --work <new directory>
```

| Option | Default | Meaning |
|---|---|---|
| `--base` | `origin/main` | The base revision of the diff |
| `--security` | `auto` | `auto` adds a security review if a listed path changed. `yes` or `no` sets it. |
| `--timeout` | `1800` | The time limit of one review, in seconds |
| `--login-timeout` | `60` | The time limit of the login test call, in seconds |
| `--work` | a new temporary directory | The work directory for the full output |

## Example

```bash
git clone https://github.com/mohanraj00/.github.git ../dot-github
git -C ../dot-github checkout 05c2fba15c81b4d3a35ebad71cb5b14dbda050d3
git fetch origin main
python3 -B ../dot-github/gates/cross_review.py --base origin/main --work "$(mktemp -d)"
```

## Result

The tool prints one report for each kind. Each report starts with `Review (agy <version>):` or `Security review (agy <version>):`, then the base and the head. The last line is `Full output: <work directory>`. The exit status is `0` if agy gave each review and `HEAD` did not change, else `1`.

The tool reads `.github/gates.json` of the base revision to select the security review. CI cannot verify a local result. For a local security result, the owner adds the `security-reviewed` label. For a local code result, the owner adds the `cross-reviewed` label.

## Safety

- Before the review, the tool sends one short prompt to agy with a time limit. If that call fails, times out or asks for a sign-in, the tool stops and sends no diff.
- agy gets only a short list of environment variables, and it runs with `--sandbox` in an empty directory.
- If the agy login is not in the token file, for example in the macOS keychain, agy runs with your real HOME. The tool prints a line when this occurs. See [Known limits](known-limits.md#local-agy-review-without-a-token-file).

## Failure texts

| Text | Cause | Action |
|---|---|---|
| `agy is not installed. Get a manual review.` | No `agy` command | Install agy, or ask the owner for a manual review. |
| `agy has no working login: … Sign in to agy, then run this again.` | The login test failed. | Sign in to agy. |
| `use a work directory outside the checkout: …` | `--work` is in the project checkout. | Use a different directory. |
| `work directory is not empty: …` | `--work` holds files. | Use a new directory. |
| `The diff was cut at 90000 bytes. Get a manual review of the rest.` | The diff is too large. | Get a manual review of the rest. |
| `agy did not give a review (…). Get a manual review.` | agy timed out or failed. | Get a manual review. |
| `HEAD changed during the review. This report covers <SHA> only.` | A commit occurred during the review. | Run the review again. |
