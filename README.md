# .github

Shared project standard and CI gates. Release: v2026.11.0.

This repository gives each project of the owner:

- [the project standard](docs/STANDARD.md): the build, review, approval and CI rules;
- a composite GitHub Action, [`gates/`](gates/action.yml), with the required checks `cross-review`, `security-review` and `acceptance`, the `classify` step of `ci-ok`, and the `gate` job of the Claude review workflow;
- workflow templates in `docs/*.yml`, and a [constitution template](docs/constitution-template.md).

The maintainers of a project use it to adopt the gates and to repin them to each monthly release. The gates use the Python standard library only, and they call only the GitHub API.

## Quickstart

You need `git`, `make` and Python 3.12 or later. CI uses the Python 3 of `ubuntu-24.04`. The quickstart uses no token and no network after the clone.

1. Clone the repository:

   ```bash
   git clone https://github.com/mohanraj00/.github.git dot-github
   ```

2. Go into the checkout:

   ```bash
   cd dot-github
   ```

3. Run the tests. The last two lines are `Ran <N> tests in <T>s` and `OK`.

   ```bash
   make check
   ```

4. Classify some changed paths with the config of this repository. This is the same function that the gates use.

   ```bash
   python3 -B -c 'import sys; sys.path.insert(0, "gates"); import gate; print(gate.classify(gate.load_config(".github/gates.json"), sys.argv[1:]))' README.md gates/gate.py AGENTS.md
   ```

   The result is:

   ```text
   {'security': ['gates/gate.py', 'AGENTS.md'], 'level1': ['gates/gate.py', 'AGENTS.md'], 'code': ['gates/gate.py'], 'docs_only': False}
   ```

   `gates/gate.py` and `AGENTS.md` are level 1 and need a security review. If you give only `README.md`, the result has `'docs_only': True`.

## Next docs

| Task | Page |
|---|---|
| Add the gates to a project | [Adopt the gates](docs/adopt.md) |
| Move a project to a new release | [Repin to a release](docs/repin.md) |
| Ask Claude to review a PR | [Claude review workflow](docs/claude-review-workflow.md) |
| Review a PR with agy on your computer | [Local agy review](docs/agy-review.md) |
| Read a gate result or a failure text | [Gate checks](docs/checks.md) |
| Find a known limit of the gates | [Known limits](docs/known-limits.md) |
| Read the changes of each release | [CHANGELOG.md](CHANGELOG.md) |

Each project constitution uses the sections and the exact governance block in [`docs/constitution-template.md`](docs/constitution-template.md). The block has version markers, so a check can compare it with the template.

## Known limits

The known limits of the gates are in [docs/known-limits.md](docs/known-limits.md).
