# Known limits

Release: v2026.11.0.

This page holds each known limit of the gates. A finding that the stop rule of the [standard](STANDARD.md#reviews) does not fix also goes here, with its reason. The threat model of the standard excludes an actor with write access.

## Owner login

The agents use the owner login. Thus GitHub cannot tell if the owner or an agent added a label. The labels `accepted`, `security-reviewed`, `cross-reviewed`, `review-cap-lifted` and `ready-for-review` depend on the rule that agents never add them.

## Count of a model review

The gates trust the finding count of a model review. The Codex security receipt also comes from a model that reads the diff, and the gate accepts it. The count line of a Claude review is the first line that is exactly `Findings: N`, with no indent. The model can write a progress line before it. The marker gives `findings=unknown` if no line is a count line, or if the text does not agree with the count:

- A line before the count line has a character that is not plain prose. The permitted characters are letters, digits, spaces and `.,:;'’()/?!%+@-`. An apostrophe is permitted only in a word, for example `I'll`. A double quote is not permitted. Markdown and HTML have many forms of quoted text, for example code fences, code spans, quote lines and HTML tags. A list of forms to refuse can miss a form, so the gate permits only these characters. A backtick, `<`, `>`, `~`, `[` or `|` before the count line thus gives `findings=unknown`.
- A line before the count line starts with a list marker (`-`, `+`, or a number and `.` or `)`). The count line can then be a lazy continuation of that list item.
- The last text line before the count line does not end with `.`, `!` or `?`. A line that ends with a colon or with no mark can introduce the count line as a quote.
- `INCOMPLETE REVIEW` is on a different line.
- A different `Findings: N` line gives a different count.
- The count is 0, and a different line starts with a severity tag or a `path:line` reference.
- The count is 0, and a severity tag P0 to P3 is at any position in a different line. Thus a clean review that writes "no P1 defect" also gives `findings=unknown`.

The check reads the text in NFKC form with no format characters. Thus a full-width or a zero-width character cannot hide a signal. This check blocks only a count that does not agree with the text. It does not block prose that describes a defect with no severity tag and no `path:line` reference. It does not block a model that omits a finding. The rules for the lines before the count line refuse quoted text, but they do not block a model that writes a false count line. A prompt injection in the diff can cause these three results. A count on the first line has the same limit. A count line that the model repeats from the diff is a different limit, see [Count line that a diff can supply](#count-line-that-a-diff-can-supply-59-p2).

## Count line that a diff can supply (#59, P2)

The count line can come after prose. Thus a model can repeat a count line from the diff of a fork PR, and the marker can then show `findings=0` when the model did not make that count. Before #25, the count had to be on the first line, so this text gave `findings=unknown`. Issue #59 tracks the fix: the count must come from the first line or from a field of structured output, not from a line after prose. The fix comes in a later release.

## Base change during a Claude review (#16)

If the base of a PR changes while a Claude review runs, the run does not stop. It uses the OAuth token to the end and posts a review of the old diff. Its marker counts toward the cap of 3. The gates accept the marker only if it names the current merge base. A user with write access can do this on each PR, and the threat model excludes that actor. A fork author can also change the base, but a fork run starts only after the owner approval of the head and base. Thus a fork author can make the one run of an approval useless, but cannot make a marker that a gate accepts.

## Burst of Claude review requests (#19, item 2)

The `gate` job and the concurrency group limit the Claude runs for each PR. A user with write access can still post requests on many PRs in a short time. Each run uses the OAuth token for up to 30 minutes. The threat model excludes that actor, so the gates do not limit the runs across PRs.

## Some pin lines unchanged in the copy check (#22, P2)

If a PR changes only some of the pin lines in `gates.yml`, the copy check uses the SHA of the changed lines as the new pin. The unchanged lines keep the old SHA, so some gates run at the old SHA after the merge. The next copy check then finds two pins on the base branch, and it fails closed. The stop rule does not fix this now, because its severity is P2. The PR is level 1, so the owner reads the pin lines before the merge.

## Pin to an older commit of main (#22, P3)

The copy check accepts each new pin that is on `main` of this repository, also a commit before the old pin. Thus a PR can move a project to an older release of the gates with no security review. The stop rule does not fix this now, because its severity is P3. The PR is level 1, so the owner decides each pin change before the merge.

## Codex request after a pass (#29, P2)

A request comment does not start the gates, and `gates-rerun` runs only a failed job again. Thus if `cross-review` passed for a head, a fourth `@codex review` request for the same head does not change the check. After a push, the gates run again and read the count, so the next head fails at the cap. The stop rule does not fix this now, because its severity is P2 and only the owner login counts as a requester. The standard also allows one request for each head.

## SHA of an action bump (#28)

The bump check does not prove that the new SHA is a release of the named action. GitHub also shows a commit of a fork under the name of the parent repository. Only an actor with write access can push to a Dependabot branch, and the threat model excludes that actor. The PR is level 1, so the owner reads each SHA and its release before the merge.

## Base of an action bump (#28)

The bump check and the copy check read the old file at the tip of the base branch, not at the merge base. If a different PR changes a workflow on the base branch after Dependabot opens its PR, the bump check fails. The gate then needs a security review. This failure is safe. To prove the bump again, comment `@dependabot rebase` on the PR.

## Readers that the docstring check does not find (#43)

The owner decision in `docstring_only` is the control. The reader scan is a second layer, and it cannot be complete: a Python file can read a docstring in more ways than a list of names can find. Thus add a path to `docstring_only` only if no tool, docs build, test or script of the project reads its docstrings. The docstring check finds the usual readers of a docstring or a source file by name. It does not find code that makes the name `__doc__`, a reader name or the path of a listed file at run time. For example, `repr(module)`, `str(module)` and an f-string of a module give the path of its source file at run time, and the scan cannot block `str`. An uncaught exception prints the path of its source file with no reader name, and a launcher can read that path at run time. A glob of only `*`, for example `cd src/auth && sh *`, also does not count, because `a * b` is a product in most languages. It also does not read installed packages, for example a metaclass or a docs tool of a library that the project uses. A change to a file that is not listed needs no security review, so a reader in such a file is not in the threat model of this check. The check reads the base branch when it runs. If a reader merges into the base branch after the check and before the merge of this PR, the check does not see it. The PR is level 1 if it changes a level 1 path, and then the owner reads the change before the merge. The `cross-review` gate does not change, and the PR stays level 1 if it changes a level 1 path.

## Agent files that are not on the shared list (#23)

The shared list holds the instruction and config names of the harnesses that the standard names. It does not hold the files of a different harness, or a file that an instruction file imports. If a project has one of these files, add its path to `level1` and `boundaries` of `.github/gates.json`. A fork PR that changes one of these paths still needs the owner approval of stage 2.

## Comments that name @claude (#38)

GitHub expressions have no regular expressions. Thus the Claude workflow starts its `gate` job for each comment of a writer that contains `@claude`. The `gate` job reads the first line, and for other text it sets no kind, so the `review` and `post` jobs skip. Such a comment uses the concurrency group of the requests. Thus a comment of a writer that names `@claude` can replace a pending request, and that request then posts no comment. Only an actor with write access can do this, and the threat model excludes that actor. Ask again if a request gets no comment.

## Local agy review without a token file (#38)

If the agy login is not in the token file, for example in the macOS keychain, `gates/cross_review.py` cannot run agy in a new HOME. It runs agy with the real HOME and an empty allow list for the session. The global MCP servers and other settings of the user can then load. The prompt tells agy not to use tools, and agy runs with `--sandbox` in an empty directory. The tool prints a line when this occurs.

## Repin keeps the old Claude workflow (#60)

The steps in [repin.md](repin.md) do not copy `docs/claude-review.yml`. Thus a project that repins keeps its old Claude workflow. The old workflow compares the full comment body, so a request with a `Head:` line starts no review. Issue #60 tracks the added repin step.

Until the fix, do this step in each repin of a project that has the Claude review workflow:

1. Copy `docs/claude-review.yml` of the new SHA to `.github/workflows/claude-review.yml`.
2. Keep only the project values, for example the SHA of the gates action.
