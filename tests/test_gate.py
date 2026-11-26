"""The shared gates pass and fail as the project standard says. The tests use a fake GitHub API and no network."""
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gates'))
import gate  # noqa: E402
from claude_review import comment_body  # noqa: E402

REPO = 'owner/project'
HEAD = 'a' * 40
OLD = 'b' * 40
# The merge base of the base branch and HEAD. A Claude marker of the tests names it.
BASE = 'c' * 40
# The tip of the base branch, and two SHAs of the gates action.
TIP = 'f' * 40
MERGE = '9' * 40
PIN_OLD, PIN_NEW = 'd' * 40, 'e' * 40
TEMPLATE = (ROOT / '.github/pull_request_template.md').read_text()
FILLED = 'Closes #5\n\n## Testing\n\n`make check`: 10 tests pass.\n\n## Risk\n\nLow. Level 0.\n\n## How to revert\n\nRevert the commit.\n'
CONFIG = {'code': ['src/*', 'tests/*'], 'security_sources': ['src/*.py'], 'not_security': ['*/test_*.py'],
          'boundaries': ['tools/*', 'src/auth/*'], 'level1': ['tools/*', 'SPEC.md'], 'generated': ['gen/*'], 'docstring_only': []}
# The owner turns on the docstring check for these paths.
DOC_CONFIG = CONFIG | {'docstring_only': ['src/*', 'tools/*', '.github/workflows/*']}
CLEAN_RECEIPT = ('<!-- codex-review-receipt:v1 -->\n\n### 🛡️ Codex Security Review\n\nSecurity review completed. '
                 'No security issues were found in this pull request.\n\n**Reviewed commit:** `%s`\n')
OPEN_RECEIPT = '### 🛡️ Codex Security Review\n\nSecurity review completed. 1 issue was found.\n\n**Reviewed commit:** `%s`\n'


def state(commit=HEAD, status='completed', findings=False, unresolved=None, done=None):
    data = {'code_review': {'commit_sha': commit, 'has_any_findings': findings,
                            'has_unresolved_findings': findings if unresolved is None else unresolved, 'status': status},
            'security_review': {'commit_sha': None, 'has_any_findings': None, 'has_unresolved_findings': None, 'status': None}}
    row = '' if done is None else ('\n| 📝 **Code Review** | ✅ **Completed** <relative-time datetime="%s">%s</relative-time> | `%s` | Manual request |'
                                   % (done, done, (commit or '')[:7]))
    return '<!-- codex-pull-request-review-summary -->\n<!-- codex-review-state:v1 %s -->\n<details>%s' % (json.dumps(data), row)


def comment(login, body, at='2026-11-07T10:00:00Z', updated=None):
    return {'user': {'login': login}, 'body': body, 'created_at': at, 'updated_at': updated or at}


RUNS = iter(range(1000, 10 ** 6))
# A run of the Claude workflow that the gates trust. It covers each comment time of the tests.
TRUSTED_RUN = {'path': gate.CLAUDE_WORKFLOW, 'event': 'issue_comment', 'head_branch': 'main', 'status': 'completed',
               'run_started_at': '2026-11-07T00:00:00Z', 'updated_at': '2026-11-07T23:00:00Z'}


def review_body(status, head, text, kind=None, run=None, base=BASE):
    """Return a Claude comment. Each comment gets its own run ID, as each run of the workflow posts one comment."""
    return comment_body(status, head, text, kind, run or str(next(RUNS)), base)


class Fake:
    """The GitHub API of one PR."""

    def __init__(self, files=('README.md',), labels=(), head=HEAD, ref='claude/7-sample', fork=False, body=FILLED,
                 comments=(), issues=None, order=None, threads=0, compare=None, base_changed='', complete=True,
                 admins=('mohanraj00',), actors=None, objects=None, labeled=None, now='2026-11-07T13:00:00Z', unlabeled=None,
                 runs=None, reviews=(), merge_base=BASE, contents=None, on_main=(), author='mohanraj00'):
        self.repo, self.number = REPO, 7
        self.labeled = {x: ('mohanraj00', '2026-11-07T09:00:00Z') for x in labels} | (labeled or {})
        self.clock = gate.stamp(now)
        self.unlabeled = unlabeled or {}
        self.slept = 0
        self.state = {'labels': [{'name': x} for x in labels], 'body': body, 'user': {'login': author},
                      'head': {'sha': head, 'ref': ref, 'repo': {'full_name': 'someone/project' if fork else REPO}},
                      'base': {'ref': 'main', 'sha': TIP, 'repo': {'default_branch': 'main'}}, 'merge_commit_sha': MERGE}
        self.contents = contents or {}
        self.mains = set(on_main)
        self.tree = {}
        self.modes = {}
        self.file_list = [{'path': x, 'previous': None, 'status': 'modified', 'lines': 10} if isinstance(x, str) else x for x in files]
        self.complete = complete
        self.comment_list = list(comments)
        self.issues = issues if issues is not None else [{'number': 5, 'open': True, 'repo': REPO, 'labels': ['accepted']}]
        self.order = order or {}
        self.threads = threads
        self.compares = compare or {}
        self.changed = base_changed
        self.admins = set(admins)
        self.actors = actors if actors is not None else {5: 'mohanraj00'}
        self.objects = objects if objects is not None else [HEAD, OLD]
        self.posted, self.removed = [], []
        self.on_sleep = None
        self.runs = runs
        self.review_list = list(reviews)
        self.merge_bases = []
        self.current_base = merge_base

    def is_admin(self, login):
        return login in self.admins

    def reviews(self):
        return self.review_list

    def last_labeled(self, issue, label, kind='labeled'):
        if kind == 'unlabeled':
            return self.unlabeled.get((issue, label))
        if issue == self.number:
            return self.labeled.get(label)
        return (self.actors[issue], '2026-11-07T09:00:00Z') if issue in self.actors else None

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.slept += seconds
        if self.on_sleep:
            self.on_sleep()
        self.clock = gate.datetime.fromtimestamp(self.clock.timestamp() + seconds, gate.timezone.utc)

    def resolve(self, commit):
        found = [x for x in self.objects if x.startswith(commit)]
        return found[0] if len(found) == 1 else None

    def pr(self):
        return self.state

    def files(self):
        return self.file_list, self.complete

    def comments(self):
        return self.comment_list

    def base_changed_at(self):
        return self.changed

    def linked_issues(self):
        return self.issues

    def label_order(self, issue):
        return self.order.get(issue, [])

    def open_codex_threads(self):
        return self.threads

    def compare(self, old, new):
        return self.compares[old]

    def merge_base(self, base, head):
        self.merge_bases.append((base, head))
        return self.current_base

    def content(self, repo, path, ref):
        # The test merge has the files of the head in these tests.
        data = self.contents.get((repo, path, HEAD if ref == MERGE else ref))
        if isinstance(data, Exception):
            raise data
        return (gate.blob_sha(data), data) if data is not None else None

    def on_main(self, repo, commit):
        return repo == gate.UPSTREAM and commit in self.mains

    def mode(self, ref, path):
        return self.modes.get((ref, path), '100644')

    def parents(self, commit):
        return [TIP, HEAD] if commit == MERGE else []

    def tree_files(self, ref):
        self.tree_ref = ref
        if isinstance(self.tree, Exception):
            raise self.tree
        return self.tree

    def workflow_run(self, run_id):
        return dict(TRUSTED_RUN) if self.runs is None else self.runs.get(run_id)

    def post_comment(self, body):
        self.posted.append(body)
        self.comment_list.append(comment(gate.BOT, body, self.clock.strftime('%Y-%m-%dT%H:%M:%SZ')))

    def remove_label(self, label):
        self.removed.append(label)


def approval(commit=HEAD, login='mohanraj00', state='APPROVED', at='2026-11-07T11:00:00Z', number=1):
    return {'id': number, 'user': {'login': login}, 'state': state, 'commit_id': commit, 'submitted_at': at}


APPROVED = [approval()]


def event(action='opened', label=None, head=HEAD, sender='mohanraj00'):
    found = {'action': action, 'repository': {'full_name': REPO}, 'sender': {'login': sender},
             'pull_request': {'number': 7, 'head': {'sha': head}, 'base': {'ref': 'main'}}}
    if label:
        found['label'] = {'name': label}
    return found


def check(name, fake, ev=None, config=CONFIG):
    passed, lines, outputs = gate.run(name, fake, ev or event(), config, TEMPLATE)
    return passed, '\n'.join(lines), outputs


# More than gate.PENDING seconds after each labeled time of these tests.
LATE = '2026-11-07T18:00:00Z'


def record(label, head=HEAD, base='main'):
    return comment(gate.BOT, gate.record_body(label, head, base))


class ConfigTest(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'gates.json')
            path.write_text(data if isinstance(data, str) else json.dumps(data))
            return gate.load_config(path)

    def test_a_valid_config_gives_each_key(self):
        config = self.load({'version': 1, 'boundaries': ['tools/*']})
        self.assertEqual(config['boundaries'], ['tools/*'])
        self.assertEqual(config['code'], [])

    def test_each_bad_config_fails_closed(self):
        for data in ('{broken', '[]', {'version': 2}, {'boundaries': []}, {'version': 1, 'extra': []},
                     {'version': 1, 'level1': 'SPEC.md'}, {'version': 1, 'level1': ['']}, {'version': 1, 'code': [1]}):
            with self.subTest(data=data), self.assertRaises(gate.GateError):
                self.load(data)
        with self.assertRaises(gate.GateError):
            gate.load_config('/nonexistent/gates.json')

    def test_the_config_of_this_repository_is_valid(self):
        config = gate.load_config(ROOT / '.github/gates.json')
        self.assertEqual(gate.classify(config, ['gates/gate.py'])['level1'], ['gates/gate.py'])


class ClassifyTest(unittest.TestCase):
    def test_the_shared_security_categories(self):
        for path in ('.github/workflows/ci.yml', 'docs/STANDARD.md', 'AGENTS.md', 'sub/dir/CLAUDE.md', 'x/.claude/settings.json',
                     '.codex/config.toml', 'go.sum', 'web/package-lock.json', 'requirements-dev.txt', 'skills/a/SKILL.md'):
            with self.subTest(path=path):
                self.assertEqual(gate.classify(CONFIG, [path])['security'], [path])

    def test_the_project_paths(self):
        result = gate.classify(CONFIG, ['src/a.py', 'src/test_a.py', 'tools/gate.py', 'README.md', 'SPEC.md'])
        self.assertEqual(result['security'], ['src/a.py', 'tools/gate.py'])
        self.assertEqual(result['level1'], ['tools/gate.py', 'SPEC.md'])
        self.assertFalse(result['docs_only'])

    def test_the_shared_level1_paths(self):
        for path in ('CONSTITUTION.md', 'docs/STANDARD.md', '.github/CODEOWNERS', 'a/AGENTS.md', '.agents/x', 'go.mod'):
            with self.subTest(path=path):
                self.assertEqual(gate.classify(CONFIG, [path])['level1'], [path])
        self.assertEqual(gate.classify(CONFIG, ['docs/guide.md', 'src/a.py'])['level1'], [])

    def test_docs_only(self):
        self.assertTrue(gate.classify(CONFIG, ['README.md', 'docs/guide.md'])['docs_only'])
        for paths in (['README.md', 'src/a.go'], ['AGENTS.md'], ['SPEC.md'], []):
            with self.subTest(paths=paths):
                self.assertFalse(gate.classify(CONFIG, paths)['docs_only'])

    def test_classify_output(self):
        self.assertEqual(check('classify', Fake(files=['README.md']))[2], {'docs_only': 'true', 'code': 'false'})
        self.assertEqual(check('classify', Fake(files=['src/a.go']))[2], {'docs_only': 'false', 'code': 'true'})
        self.assertEqual(check('classify', Fake(files=['README.md'], complete=False))[2], {'docs_only': 'false', 'code': 'true'})
        moved = {'path': 'docs/a.md', 'previous': 'src/a.go', 'lines': 1}
        self.assertEqual(check('classify', Fake(files=[moved]))[2]['code'], 'true')


class SecurityTest(unittest.TestCase):
    def test_no_listed_path_passes(self):
        passed, message, _ = check('security-review', Fake(files=['README.md', 'src/test_a.py']))
        self.assertTrue(passed)
        self.assertIn('No listed path changed.', message)

    def test_a_listed_path_needs_a_review(self):
        passed, message, _ = check('security-review', Fake(files=['src/a.py']))
        self.assertFalse(passed)
        self.assertIn('Listed paths: src/a.py', message)
        self.assertIn('@codex security review', message)

    def test_a_move_out_of_a_listed_path_counts(self):
        moved = {'path': 'docs/a.py', 'previous': 'src/auth/a.py', 'lines': 1}
        self.assertFalse(check('security-review', Fake(files=[moved]))[0])

    def test_a_clean_codex_receipt_of_the_head_passes(self):
        fake = Fake(files=['src/a.py'], comments=[comment(gate.CODEX, CLEAN_RECEIPT % HEAD[:10])])
        passed, message, _ = check('security-review', fake)
        self.assertTrue(passed, message)

    def test_a_receipt_from_another_user_does_not_count(self):
        for login in ('mohanraj00', 'chatgpt-codex-connector', gate.BOT):
            with self.subTest(login=login):
                self.assertFalse(check('security-review', Fake(files=['src/a.py'], comments=[comment(login, CLEAN_RECEIPT % HEAD[:10])]))[0])

    def test_the_latest_receipt_decides(self):
        comments = [comment(gate.CODEX, CLEAN_RECEIPT % HEAD[:10], '2026-11-07T10:00:00Z'),
                    comment(gate.CODEX, OPEN_RECEIPT % HEAD[:10], '2026-11-07T11:00:00Z')]
        passed, message, _ = check('security-review', Fake(files=['src/a.py'], comments=comments))
        self.assertFalse(passed)
        self.assertIn('open findings', message)

    def test_a_receipt_before_the_base_change_does_not_count(self):
        fake = Fake(files=['src/a.py'], comments=[comment(gate.CODEX, CLEAN_RECEIPT % HEAD[:10])], base_changed='2026-11-07T11:00:00Z')
        self.assertFalse(check('security-review', fake)[0])

    def test_a_changed_receipt_format_fails_closed(self):
        body = '### Codex Security Review\n\nSecurity review completed. No security issues were found in this pull request.\n\nCommit: %s' % HEAD
        passed, message, _ = check('security-review', Fake(files=['src/a.py'], comments=[comment(gate.CODEX, body)]))
        self.assertFalse(passed)
        self.assertIn('The receipt format changed.', message)

    def test_a_clean_review_stays_valid_if_the_new_commits_touch_no_listed_path(self):
        comments = [comment(gate.CODEX, CLEAN_RECEIPT % OLD[:10])]
        fake = Fake(files=['src/a.py'], comments=comments, compare={OLD: (True, ['README.md'], True)})
        passed, message, _ = check('security-review', fake)
        self.assertTrue(passed, message)
        self.assertIn('the later commits touch no listed path', message)
        for result in ((True, ['src/b.py'], True), (False, ['README.md'], True), (True, ['README.md'], False)):
            with self.subTest(result=result):
                fake = Fake(files=['src/a.py'], comments=comments, compare={OLD: result})
                self.assertFalse(check('security-review', fake)[0])

    def test_the_summary_tells_if_a_review_is_necessary(self):
        cases = [
            ('not needed', Fake(files=['README.md']), 'Security review: not needed. No listed path changed.'),
            ('listed paths', Fake(files=['src/a.py', 'tools/x.sh', '.github/workflows/ci.yml', 'go.mod', 'AGENTS.md']),
             'Security review: needed. The PR changes: src/a.py (project security source), tools/x.sh (project boundary), '
             '.github/workflows/ci.yml (GitHub config), go.mod (dependency manifest), AGENTS.md (agent instructions).'),
            ('still valid', Fake(files=['src/a.py'], comments=[comment(gate.CODEX, CLEAN_RECEIPT % OLD[:10])],
                                 compare={OLD: (True, ['README.md'], True)}),
             'Security review: the clean review is still valid. The commits after %s touch no listed path.' % OLD[:12]),
            ('new commits', Fake(files=['src/a.py'], comments=[comment(gate.CODEX, CLEAN_RECEIPT % OLD[:10])],
                                 compare={OLD: (True, ['README.md', 'src/b.py'], True)}),
             'Security review: needed. The commits after the clean review of %s change: src/b.py (project security source).' % OLD[:12]),
            ('head', Fake(files=['src/a.py'], comments=[comment(gate.CODEX, CLEAN_RECEIPT % HEAD[:10])]),
             'Security review: the clean review of head %s is valid.' % HEAD[:12]),
        ]
        for title, fake, expected in cases:
            with self.subTest(title):
                self.assertEqual(check('security-review', fake)[2], {'summary': expected})

    def test_main_writes_the_summary_to_the_job_summary_and_the_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.github').mkdir()
            (root / '.github/gates.json').write_text(json.dumps(dict(CONFIG, version=1)))
            (root / 'event.json').write_text(json.dumps(event()))
            env = {'GITHUB_EVENT_PATH': str(root / 'event.json'), 'GITHUB_WORKSPACE': folder, 'GITHUB_TOKEN': 't',
                   'GITHUB_OUTPUT': str(root / 'out'), 'GITHUB_STEP_SUMMARY': str(root / 'summary')}
            fake = Fake(files=['src/[x](y).py'])
            with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub', return_value=fake), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate.main(['security-review']), 1)
            self.assertEqual((root / 'out').read_text(),
                             'summary=Security review: needed. The PR changes: src/[x](y).py (project security source).\n')
            self.assertEqual((root / 'summary').read_text(),
                             'Security review: needed. The PR changes: src/\\[x\\]\\(y\\).py \\(project security source\\).\n')

    def test_security_needed_forces_a_review_of_the_head(self):
        fake = Fake(files=['README.md'], labels=['security-needed'])
        self.assertFalse(check('security-review', fake)[0])
        fake = Fake(files=['README.md'], labels=['security-needed'], comments=[comment(gate.CODEX, CLEAN_RECEIPT % OLD[:10])],
                    compare={OLD: (True, ['README.md'], True)})
        self.assertFalse(check('security-review', fake)[0])
        fake = Fake(files=['README.md'], labels=['security-needed'], comments=[comment(gate.CODEX, CLEAN_RECEIPT % HEAD[:10])])
        self.assertTrue(check('security-review', fake)[0])

    def test_a_long_file_list_is_security_relevant(self):
        passed, message, _ = check('security-review', Fake(files=['README.md'], complete=False))
        self.assertFalse(passed)
        self.assertIn('more files than GitHub lists', message)

    def test_the_label_needs_an_approval_record(self):
        fake = Fake(files=['src/a.py'], labels=['security-reviewed'], comments=[record('security-reviewed')])
        self.assertTrue(check('security-review', fake)[0])
        for comments in ([], [record('security-reviewed', base='dev')], [record('cross-reviewed')],
                         [comment('mohanraj00', gate.record_body('security-reviewed', HEAD, 'main'))]):
            with self.subTest(comments=comments):
                # LATE is after the pending time, so the run of the labeled event is not in the queue now.
                fake = Fake(files=['src/a.py'], labels=['security-reviewed'], comments=comments, now=LATE)
                passed, message, _ = check('security-review', fake)
                self.assertFalse(passed)
                self.assertEqual(fake.removed, ['security-reviewed'])

    def test_the_labeled_event_binds_the_head_of_the_event(self):
        fake = Fake(files=['src/a.py'], labels=['security-reviewed'])
        self.assertTrue(check('security-review', fake, event('labeled', 'security-reviewed'))[0])
        self.assertEqual(fake.posted, [gate.record_body('security-reviewed', HEAD, 'main')])
        fake = Fake(files=['src/a.py'], labels=['security-reviewed'], compare={OLD: (True, ['src/a.py'], True)})
        self.assertFalse(check('security-review', fake, event('labeled', 'security-reviewed', head=OLD))[0])
        self.assertEqual(fake.removed, ['security-reviewed'])
        fake = Fake(files=['src/a.py'], labels=['security-reviewed'])
        check('security-review', fake, event('labeled', 'cross-reviewed'))
        self.assertEqual(fake.posted, [])

    def test_an_unreadable_newer_receipt_stops_an_older_clean_receipt(self):
        comments = [comment(gate.CODEX, CLEAN_RECEIPT % HEAD[:10], '2026-11-07T10:00:00Z'),
                    comment(gate.CODEX, '### Codex Security Review\n\n1 issue.\n\nCommit: x', '2026-11-07T11:00:00Z')]
        passed, message, _ = check('security-review', Fake(files=['src/a.py'], comments=comments))
        self.assertFalse(passed)
        self.assertIn('The receipt format changed.', message)

    def test_an_old_record_does_not_count_after_the_label_is_added_again(self):
        for actor, at in (('triage-user', '2026-11-07T11:00:00Z'), ('mohanraj00', '2026-11-07T11:00:00Z')):
            with self.subTest(actor=actor):
                fake = Fake(files=['src/a.py'], labels=['security-reviewed'], comments=[record('security-reviewed')],
                            labeled={'security-reviewed': (actor, at)}, now=LATE)
                self.assertFalse(check('security-review', fake)[0])
                self.assertEqual(fake.removed, ['security-reviewed'])
                fake = Fake(ref='agy/7', issues=[], labels=['cross-reviewed'], comments=[record('cross-reviewed')],
                            labeled={'cross-reviewed': (actor, at)}, now=LATE)
                self.assertFalse(check('cross-review', fake)[0])

    def test_a_run_waits_for_the_record_of_a_recent_label_and_does_not_remove_it(self):
        recent = {'cross-reviewed': ('mohanraj00', '2026-11-07T12:59:00Z')}
        fake = Fake(ref='agy/7', issues=[], labels=['cross-reviewed'], labeled=recent)
        self.assertFalse(check('cross-review', fake)[0])
        self.assertEqual((fake.slept, fake.removed), (gate.WAIT, []))
        fake = Fake(ref='agy/7', issues=[], labels=['cross-reviewed'], labeled=recent)
        fake.on_sleep = lambda: fake.comment_list.append(comment(gate.BOT, gate.record_body('cross-reviewed', HEAD, 'main'), '2026-11-07T13:00:05Z'))
        self.assertTrue(check('cross-review', fake)[0])
        self.assertEqual((fake.slept, fake.removed), (10, []))

    def test_a_codex_result_with_findings_stops_the_claude_advisory_line(self):
        comments = [comment(gate.CODEX, OPEN_RECEIPT % HEAD[:10], '2026-11-07T10:00:00Z'),
                    comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'security'), '2026-11-07T11:00:00Z')]
        passed, message, _ = check('security-review', Fake(files=['src/a.py'], comments=comments))
        self.assertFalse(passed)
        self.assertNotIn('advisory', message)
        comments[0] = comment(gate.CODEX, '### Codex Security Review\n\nCommit: x', '2026-11-07T10:00:00Z')
        passed, message, _ = check('security-review', Fake(files=['src/a.py'], comments=comments))
        self.assertFalse(passed)  # The Claude result is advisory, also after a malformed receipt.
        self.assertIn('advisory', message)

    def test_only_an_admin_can_clear_security_needed(self):
        removed = {(7, 'security-needed'): ('triage-user', '2026-11-07T11:00:00Z')}
        self.assertFalse(check('security-review', Fake(files=['README.md'], unlabeled=removed))[0])
        removed = {(7, 'security-needed'): ('mohanraj00', '2026-11-07T11:00:00Z')}
        self.assertTrue(check('security-review', Fake(files=['README.md'], unlabeled=removed))[0])

    def test_a_label_from_a_user_who_is_not_an_admin_makes_no_record(self):
        for sender in ('triage-user', ''):
            with self.subTest(sender=sender):
                # The labeled event of the PR names the same user as the event, and the run is recent.
                fake = Fake(files=['src/a.py'], labels=['security-reviewed'], labeled={'security-reviewed': (sender, '2026-11-07T12:59:00Z')})
                self.assertFalse(check('security-review', fake, event('labeled', 'security-reviewed', sender=sender))[0])
                self.assertEqual((fake.posted, fake.removed), ([], ['security-reviewed']))
        fake = Fake(ref='agy/7', issues=[], labels=['cross-reviewed'])
        self.assertFalse(check('cross-review', fake, event('labeled', 'cross-reviewed', sender='triage-user'))[0])
        self.assertEqual(fake.posted, [])

    def test_a_receipt_prefix_must_name_one_full_commit(self):
        # A different commit with the same 10 hex digits as the reviewed commit does not match.
        twin = HEAD[:10] + 'c' * 30
        receipt = [comment(gate.CODEX, CLEAN_RECEIPT % HEAD[:10])]
        self.assertFalse(check('security-review', Fake(files=['src/a.py'], head=twin, comments=receipt, objects=[HEAD, twin]))[0])
        self.assertFalse(check('security-review', Fake(files=['src/a.py'], comments=receipt, objects=[HEAD, twin]))[0])
        self.assertFalse(check('security-review', Fake(files=['src/a.py'], comments=receipt, objects=[]))[0])
        fake = Fake(files=['src/a.py'], head=twin, comments=receipt, objects=[HEAD],
                    compare={HEAD: (True, ['src/b.py'], True)})
        self.assertFalse(check('security-review', fake)[0])

    def test_a_clean_claude_security_review_is_advisory(self):
        body = review_body('success', HEAD, 'Findings: 0\nNo defect.', 'security')
        passed, message, _ = check('security-review', Fake(files=['src/a.py'], comments=[comment(gate.BOT, body)]))
        self.assertFalse(passed)
        self.assertIn('advisory', message)
        self.assertIn('security-reviewed', message)
        # The same PR passes after the owner adds the label, which writes an approval record.
        fake = Fake(files=['src/a.py'], labels=['security-reviewed'], comments=[comment(gate.BOT, body), record('security-reviewed')])
        self.assertTrue(check('security-review', fake)[0])
        for body, login in ((review_body('success', HEAD, 'Findings: 1\nA.', 'security'), gate.BOT),
                            (review_body('success', HEAD, 'Findings: 0', 'code'), gate.BOT),
                            (review_body('success', HEAD, 'Findings: 0', 'security'), 'mohanraj00')):
            with self.subTest(body=body, login=login):
                passed, message, _ = check('security-review', Fake(files=['src/a.py'], comments=[comment(login, body)]))
                self.assertFalse(passed)
                self.assertNotIn('advisory', message)


def pinned(sha, note=b' # pin'):
    return b'jobs:\n  gate:\n    steps:\n      - uses: mohanraj00/.github/gates@' + sha.encode() + note + b'\n'


COPY_FILES = ['.github/workflows/gates.yml', 'docs/STANDARD.md', 'tools/claude_review.py', gate.CLAUDE_WORKFLOW]


def copies(**change):
    """Return the contents of a PR that changes the pin from PIN_OLD to PIN_NEW and copies the files of UPSTREAM."""
    up = gate.UPSTREAM
    found = {(REPO, gate.PIN_FILE, TIP): pinned(PIN_OLD), (REPO, gate.PIN_FILE, HEAD): pinned(PIN_NEW),
             (up, 'docs/STANDARD.md', PIN_NEW): b'# Project standard\n',
             (up, 'gates/claude_review.py', PIN_NEW): b'print(1)\n',
             (up, 'docs/claude-review.yml', PIN_NEW): pinned('0' * 40, b' # Replace with the SHA.'),
             (REPO, 'docs/STANDARD.md', HEAD): b'# Project standard\n',
             (REPO, 'tools/claude_review.py', HEAD): b'print(1)\n',
             (REPO, gate.CLAUDE_WORKFLOW, HEAD): pinned(PIN_NEW, b' # Replace with the SHA.')}
    found.update({(REPO, path, HEAD): data for path, data in change.items()})
    return found


class CopyCheckTest(unittest.TestCase):
    def run_check(self, files=COPY_FILES, contents=None, on_main=(PIN_NEW,)):
        fake = Fake(files=files, contents=copies() if contents is None else contents, on_main=on_main)
        return check('security-review', fake)

    def test_a_valid_pin_change_and_exact_copies_pass_with_no_review(self):
        passed, message, _ = self.run_check()
        self.assertTrue(passed, message)
        self.assertIn('No review is necessary.', message)
        self.assertIn('.github/workflows/gates.yml: the pin changes to eeeeeeeeeeee', message)
        for path in COPY_FILES[1:]:
            self.assertIn(path + ': an exact copy of', message)

    def test_one_changed_byte_in_a_copy_needs_a_review(self):
        passed, message, _ = self.run_check(contents=copies(**{'tools/claude_review.py': b'print(2)\n'}))
        self.assertFalse(passed)
        self.assertIn('tools/claude_review.py is not an exact copy', message)
        self.assertIn('@codex security review', message)

    def test_the_workflow_copy_must_name_the_new_pin(self):
        contents = copies(**{gate.CLAUDE_WORKFLOW: pinned(PIN_OLD, b' # Replace with the SHA.')})
        passed, message, _ = self.run_check(contents=contents)
        self.assertFalse(passed)
        self.assertIn('.github/workflows/claude-review.yml is not an exact copy', message)

    def test_a_pin_that_is_not_on_main_needs_a_review(self):
        passed, message, _ = self.run_check(on_main=())
        self.assertFalse(passed)
        self.assertIn('The pin eeeeeeeeeeee is not on main of mohanraj00/.github.', message)

    def test_a_pin_line_with_another_change_needs_a_review(self):
        passed, message, _ = self.run_check(contents=copies(**{gate.PIN_FILE: pinned(PIN_NEW, b' # other')}))
        self.assertFalse(passed)
        self.assertIn('.github/workflows/gates.yml is not a copy or a pin change.', message)

    def test_a_copy_and_one_other_listed_path_need_a_review(self):
        passed, message, _ = self.run_check(files=['docs/STANDARD.md', 'src/a.py'])
        self.assertFalse(passed)
        self.assertIn('src/a.py is not a copy or a pin change.', message)

    def test_an_api_error_needs_a_review(self):
        error = urllib.error.HTTPError('https://api.github.com/x', 500, 'error', {}, io.BytesIO())
        passed, message, _ = self.run_check(contents=copies(**{'docs/STANDARD.md': error}))
        self.assertFalse(passed)
        self.assertIn('The copy check failed closed (HTTPError', message)
        self.assertIn('@codex security review', message)

    def test_a_review_still_passes_when_the_copy_check_fails(self):
        fake = Fake(files=['src/a.py'], comments=[comment(gate.CODEX, CLEAN_RECEIPT % HEAD[:10])])
        passed, message, _ = check('security-review', fake)
        self.assertTrue(passed, message)
        self.assertIn('Copy check: ', message)

    def test_security_needed_skips_the_copy_check(self):
        fake = Fake(files=COPY_FILES, labels=['security-needed'], contents=copies(), on_main=(PIN_NEW,))
        passed, message, _ = check('security-review', fake)
        self.assertFalse(passed)
        self.assertNotIn('No review is necessary.', message)

    def test_the_blob_sha_is_the_git_value(self):
        self.assertEqual(gate.blob_sha(b''), 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')


STORE = 'src/auth/store.py'
STORE_OLD = b'''#!/usr/bin/env python3
"""The store."""
import os  # noqa: F401


class Store:
    """The rows."""

    def rows(self, n):
        """Return the rows."""
        return self.data[:n]
'''
STORE_DOCS = STORE_OLD.replace(b'Return the rows.', b'Return the rows of the open snapshot.\n\n        The rows are a list.')
TOKENS = 'src/auth/store.py changes a byte outside the docstrings, or adds or removes a docstring.'


class DocstringCheckTest(unittest.TestCase):
    def run_check(self, new=STORE_DOCS, old=STORE_OLD, files=(STORE,), tree=None, labels=(), path=STORE, modes=None):
        contents = {(REPO, path, TIP): old, (REPO, path, HEAD): new}
        fake = Fake(files=files, contents=contents, labels=labels)
        fake.modes = modes or {}
        fake.tree = {path: new, 'tools/run.py': b'import argparse\n'} if tree is None else tree
        return check('security-review', fake, config=DOC_CONFIG)

    def test_the_check_is_off_with_no_docstring_only_paths(self):
        contents = {(REPO, STORE, TIP): STORE_OLD, (REPO, STORE, HEAD): STORE_DOCS}
        fake = Fake(files=(STORE,), contents=contents)
        fake.tree = {STORE: STORE_DOCS}
        passed, message, _ = check('security-review', fake)
        self.assertFalse(passed)
        self.assertNotIn('Docstring check', message)
        self.assertIsNone(fake.__dict__.get('tree_ref'))
        self.assertIn('@codex security review', message)

    def test_a_listed_path_outside_docstring_only_needs_a_review(self):
        contents = {(REPO, STORE, TIP): STORE_OLD, (REPO, STORE, HEAD): STORE_DOCS}
        fake = Fake(files=(STORE,), contents=contents)
        fake.tree = {STORE: STORE_DOCS}
        passed, message, _ = check('security-review', fake, config=CONFIG | {'docstring_only': ['src/api/*']})
        self.assertFalse(passed)
        self.assertIn('Docstring check: src/auth/store.py is not in "docstring_only" of the project config.', message)
        self.assertIsNone(fake.__dict__.get('tree_ref'))

    def test_a_docstring_only_change_passes_with_no_review(self):
        passed, message, output = self.run_check()
        self.assertTrue(passed, message)
        self.assertIn('Each listed path changes only the text of docstrings. No review is necessary.', message)
        self.assertIn('src/auth/store.py: only the text of docstrings changes.', message)
        self.assertEqual(output, {'summary': 'Security review: not needed. Only docstrings changed in a listed path.'})

    def test_a_docstring_after_code_on_its_line_passes(self):
        old = 'def f(x="é"): "Doc."\n'.encode()
        passed, message, _ = self.run_check(old=old, new=old.replace(b'Doc.', b'Doc two.'))
        self.assertTrue(passed, message)

    def test_a_comment_or_line_change_needs_a_review(self):
        old = (b'# /// script\n# dependencies = ["safe-package"]\n# ///\nimport subprocess\n\n'
               b'def f(command):\n    # nosemgrep\n    """Run."""\n    return subprocess.run(command, shell=True)  # Run it.\n')
        cases = {'comment on code': old.replace(b'# Run it.', b'# Run the command.'),
                 'nosemgrep on code': old.replace(b'# Run it.', b'# Run it. nosemgrep'),
                 'own line comment': old.replace(b'    # nosemgrep\n', b'    # The command.\n'),
                 'added comment': old.replace(b'import subprocess', b'# A comment.\nimport subprocess'),
                 'script metadata': old.replace(b'safe-package', b'other-package'),
                 'start line': b'#!/usr/bin/env python3\n' + old,
                 'removed docstring': old.replace(b'    """Run."""\n', b''),
                 'added docstring': b'"""New."""\n' + old,
                 'removed blank line': old.replace(b'subprocess\n\n', b'subprocess\n'),
                 'new line break': old.replace(b'(command, shell=True)', b'(command,\n                          shell=True)'),
                 'moved comment': old.replace(b'  # Run it.', b'').replace(b'    # nosemgrep', b'    # nosemgrep  # Run it.'),
                 'backslash line': old.replace(b'return subprocess', b'return \\\n        subprocess'),
                 'new space': old.replace(b'(command, shell', b'(command,  shell'),
                 'crlf': old.replace(b'\n', b'\r\n')}
        for name, new in cases.items():
            with self.subTest(name):
                passed, message, _ = self.run_check(old=old, new=new)
                self.assertFalse(passed)
                self.assertIn('Docstring check: ' + TOKENS, message)

    def test_a_mode_change_or_no_change_needs_a_review(self):
        passed, message, _ = self.run_check(new=STORE_OLD)
        self.assertFalse(passed)
        self.assertIn('Docstring check: src/auth/store.py changes no byte, so the change is not a docstring change.', message)
        for modes in ({(MERGE, STORE): '100755'}, {(TIP, STORE): None}, {(TIP, STORE): '120000', (MERGE, STORE): '120000'}):
            with self.subTest(modes=modes):
                passed, message, _ = self.run_check(modes=modes)
                self.assertFalse(passed)
                self.assertIn('Docstring check: src/auth/store.py changes its file mode, or it is not a regular file.', message)

    def test_a_listed_file_that_runs_as_another_program_needs_a_review(self):
        bare_old, bare_new = STORE_OLD.split(b'\n', 1)[1], STORE_DOCS.split(b'\n', 1)[1]
        executable = {(TIP, STORE): '100755', (MERGE, STORE): '100755'}
        cases = {'shell line': (b'#!/bin/sh\n' + bare_old, b'#!/bin/sh\n' + bare_new, {}),
                 'executable with no line': (bare_old, bare_new, executable),
                 'env split': (b"#!/usr/bin/env -S sh -c 'sh \"$1\"' python\n" + bare_old,
                               b"#!/usr/bin/env -S sh -c 'sh \"$1\"' python\n" + bare_new, executable),
                 'python in a path': (b'#!/bin/sh /opt/python\n' + bare_old, b'#!/bin/sh /opt/python\n' + bare_new, {}),
                 'command flag': (b'#!/usr/bin/python3 -c\n' + bare_old, b'#!/usr/bin/python3 -c\n' + bare_new, {})}
        for name, (old, new, modes) in cases.items():
            with self.subTest(name):
                passed, message, _ = self.run_check(old=old, new=new, modes=modes)
                self.assertFalse(passed)
                self.assertIn('Docstring check: src/auth/store.py can run as a program that is not Python.', message)
        for line in (b'#!/usr/bin/env python3\n', b'#! /usr/bin/python3.12 -u\n', b'#!/usr/local/bin/python\n'):
            with self.subTest(line=line):
                passed, message, _ = self.run_check(old=line + bare_old, new=line + bare_new, modes=executable)
                self.assertTrue(passed, message)

    def test_a_source_encoding_that_is_not_utf8_needs_a_review(self):
        for cookie in (b'# -*- coding: unicode_escape -*-\n', b'# -*- coding: latin-1 -*-\n'):
            with self.subTest(cookie=cookie):
                passed, message, _ = self.run_check(old=cookie + STORE_OLD, new=cookie + STORE_DOCS)
                self.assertFalse(passed)
                self.assertIn('src/auth/store.py does not parse at the base or the head (ValueError)', message)

    def test_a_bom_change_needs_a_review(self):
        passed, message, _ = self.run_check(new=b'\xef\xbb\xbf' + STORE_DOCS)
        self.assertFalse(passed)
        self.assertIn('Docstring check: ' + TOKENS, message)

    def test_a_code_change_with_a_docstring_change_needs_a_review(self):
        passed, message, _ = self.run_check(new=STORE_DOCS.replace(b'[:n]', b'[:n + 1]'))
        self.assertFalse(passed)
        self.assertIn('Docstring check: src/auth/store.py changes code, not only docstrings.', message)
        self.assertIn('@codex security review', message)

    def test_a_string_that_is_not_a_docstring_needs_a_review(self):
        cases = {'a second string': STORE_OLD.replace(b'"""Return the rows."""', b'"""Return the rows."""\n        "x"'),
                 'an f-string': STORE_OLD.replace(b'"""Return the rows."""', b'f"{self.data}"'),
                 'a bytes docstring': STORE_OLD.replace(b'"""Return the rows."""', b'b"Return the rows."'),
                 'a string value': STORE_OLD.replace(b'[:n]', b'[:n] or "Return the rows."')}
        for name, new in cases.items():
            with self.subTest(name):
                passed, message, _ = self.run_check(new=new)
                self.assertFalse(passed)
                self.assertIn('changes code', message)

    def test_a_parse_error_needs_a_review(self):
        for old, new in ((STORE_OLD, STORE_DOCS + b'def (\n'), (STORE_OLD + b'\x00', STORE_DOCS), (STORE_OLD, b'\tx\n  y\n')):
            with self.subTest(new=new[-6:]):
                passed, message, _ = self.run_check(old=old, new=new)
                self.assertFalse(passed)
                self.assertIn('src/auth/store.py does not parse at the base or the head', message)

    def test_an_added_deleted_or_renamed_file_needs_a_review(self):
        cases = {'added': [{'path': STORE, 'previous': None, 'status': 'added', 'lines': 10}],
                 'removed': [{'path': STORE, 'previous': None, 'status': 'removed', 'lines': 10}],
                 'renamed': [{'path': STORE, 'previous': 'src/auth/old.py', 'status': 'renamed', 'lines': 10}]}
        for name, files in cases.items():
            with self.subTest(name):
                passed, message, _ = self.run_check(files=files)
                self.assertFalse(passed)
                self.assertIn('is not a modified file with the same path.', message)

    def test_a_missing_file_needs_a_review(self):
        fake = Fake(files=[STORE], contents={(REPO, STORE, HEAD): STORE_DOCS})
        passed, message, _ = check('security-review', fake, config=DOC_CONFIG)
        self.assertFalse(passed)
        self.assertIn('Cannot read src/auth/store.py at the base or the head.', message)

    def test_a_listed_file_that_is_not_python_needs_a_review(self):
        passed, message, _ = self.run_check(path='tools/run.sh', files=['tools/run.sh'], old=b'# a\necho 1\n', new=b'# b\necho 1\n')
        self.assertFalse(passed)
        self.assertIn('Docstring check: tools/run.sh is not a .py file.', message)

    def test_one_python_and_one_other_listed_file_need_a_review(self):
        passed, message, _ = self.run_check(files=[STORE, '.github/workflows/ci.yml'])
        self.assertFalse(passed)
        self.assertIn('.github/workflows/ci.yml is not a .py file.', message)

    def test_a_tree_that_reads_a_docstring_needs_a_review(self):
        cases = {
            'own __doc__': ({STORE: STORE_DOCS + b'print(__doc__)\n'}, 'src/auth/store.py reads its own __doc__'),
            'object __doc__': ({'src/cli.py': b'from src.auth import store\nprint(store.Store.rows.__doc__)\n'},
                               'src/cli.py reads __doc__ of an object'),
            'getattr': ({'src/cli.py': b'getattr(x, "__doc__")\n'}, 'src/cli.py names __doc__ in a string'),
            'import': ({'src/cli.py': b'from src.auth.store import __doc__ as text\n'}, 'src/cli.py imports __doc__ of a module'),
            'inspect': ({'src/cli.py': b'from inspect import getdoc\n'}, 'src/cli.py names getdoc'),
            'source': ({'src/cli.py': b'getsource(f)\n'}, 'src/cli.py names getsource'),
            'ast reader': ({'src/cli.py': b'get_docstring(tree)\n'}, 'src/cli.py names get_docstring'),
            'loader': ({'src/cli.py': b'__loader__.get_source(name)\n'}, 'src/cli.py names get_source'),
            'own file': ({STORE: STORE_DOCS + b'open(__file__)\n'}, 'src/auth/store.py names its own source file'),
            'module file': ({'src/cli.py': b'Path(store.__file__).read_text()\n'},
                            'src/cli.py reads __file__ of an object'),
            'spec origin': ({'src/cli.py': b'import importlib.util\n'}, 'src/cli.py names importlib'),
            'origin': ({'src/cli.py': b'open(spec.origin)\n'}, 'src/cli.py reads origin of an object'),
            'getattr file': ({'src/cli.py': b'open(getattr(store, "__file__"))\n'}, 'src/cli.py names __file__ in a string'),
            'abs file': ({'src/cli.py': b'getabsfile(store)\n'}, 'src/cli.py names getabsfile'),
            'inspect': ({'src/cli.py': b'import inspect\n'}, 'src/cli.py names inspect'),
            'exec': ({'src/cli.py': b'exec(text)\n'}, 'src/cli.py names exec'),
            'grammar': ({'src/parse.py': b'import ply.yacc\n'}, 'src/parse.py names ply'),
            'txt reader': ({'tools/reader.txt': b'from src.auth import store\nrun(store.__doc__)\n'},
                           'tools/reader.txt reads __doc__ of an object'),
            'md reader': ({'docs/run.md': b'print(getdoc(x))\n'}, 'docs/run.md names getdoc'),
            'config reader': ({'.github/gates.json': b'run(store.__doc__)\n'}, '.github/gates.json reads __doc__ of an object'),
            'config path': ({'tox.ini': b'x = "src/auth/store.py"\n'}, 'tox.ini names the file store.py'),
            'txt shell reader': ({'tools/reader.txt': b"python -c 'from src.auth import store; exec(store.__doc__)'\n"},
                                 'tools/reader.txt names a docstring reader'),
            'md path': ({'docs/run.md': b'Run `sh < src/auth/store.py`.\n'}, 'docs/run.md names the file store.py'),
            'pytest doctest': ({'tools/test.py': b'import pytest\npytest.main(["--doctest-modules", "src"])\n'},
                               'tools/test.py names a docstring reader in a string'),
            'bytes doctest': ({'tools/test.py': b'run([b"--doctest-glob"])\n'}, 'tools/test.py names a docstring reader in a string'),
            'class pattern': ({'src/cli.py': b'match store:\n    case object(__doc__=text):\n        run(text)\n'},
                              'src/cli.py reads __doc__ in a pattern or a keyword'),
            'pattern file': ({'src/cli.py': b'match store:\n    case object(__file__=path):\n        run(path)\n'},
                             'src/cli.py reads __file__ in a pattern or a keyword'),
            'keyword': ({'src/cli.py': b'run(__doc__=1)\n'}, 'src/cli.py reads __doc__ in a pattern or a keyword'),
            'sphinx conf': ({'docs/conf.py': b'extensions = ["sphinx.ext.autodoc"]\n'}, 'docs/conf.py names a docstring reader in a string'),
            'sphinx import': ({'docs/conf.py': b'import sphinx\n'}, 'docs/conf.py names sphinx'),
            'automodule': ({'docs/index.rst': b'.. automodule:: src.auth.store\n'}, 'docs/index.rst names a docstring reader'),
            'sphinx build': ({'.github/workflows/docs.yml': b'run: sphinx-build docs out\n'},
                             '.github/workflows/docs.yml names a docstring reader'),
            'mkdocstrings': ({'mkdocs.yml': b'plugins:\n  - mkdocstrings\n'}, 'mkdocs.yml names a docstring reader'),
            'command path': ({'tools/run.py': b'run("cat src/auth/store.py | sh", shell=True)\n'}, 'tools/run.py names the file store.py'),
            'bytecode': ({'tools/run.py': b'run(deploy.__code__, shell=True)\n'}, 'tools/run.py reads __code__ of an object'),
            'code consts': ({'tools/run.py': b'run(code.co_consts[0])\n'}, 'tools/run.py reads co_consts of an object'),
            'dis': ({'tools/run.py': b'import dis\n'}, 'tools/run.py names dis'),
            'help': ({'tools/run.py': b'help(store)\n'}, 'tools/run.py names help'),
            'vars': ({'tools/run.py': b'run(list(vars(store).values())[1], shell=True)\n'}, 'tools/run.py names vars'),
            'module dict': ({'tools/run.py': b'run(store.__dict__)\n'}, 'tools/run.py reads __dict__ of an object'),
            'dir': ({'tools/run.py': b'[getattr(store, x) for x in dir(store)]\n'}, 'tools/run.py names dir'),
            'shell vars': ({'bin/run.sh': b"sh -c \"$(python3 -c 'import s; print(list(vars(s).values())[1])')\"\n"},
                           'bin/run.sh names a docstring reader'),
            'shell dict': ({'bin/run.sh': b"python3 -c 'print(s.__dict__)'\n"}, 'bin/run.sh names a docstring reader'),
            'shell code': ({'bin/run.sh': b"python3 -c 'exec(f.__code__.co_consts[0])'\n"}, 'bin/run.sh names a docstring reader'),
            'function globals': ({'tools/run.py': b'run(list(store.marker.__globals__.values())[1], shell=True)\n'},
                                 'tools/run.py reads __globals__ of an object'),
            'frame globals': ({'tools/run.py': b'import sys\nrun(sys._getframe(0).f_globals)\n'}, 'tools/run.py reads f_globals of an object'),
            'gc': ({'tools/run.py': b'import gc\n'}, 'tools/run.py names gc'),
            'shell globals': ({'bin/run.sh': b"python3 -c 'print(f.__globals__)'\n"}, 'bin/run.sh names a docstring reader'),
            'shell py': ({'tools/run.py': b'#!/bin/sh\nx = "python3 -c print(s.__doc__)"\n'}, 'tools/run.py names a docstring reader in a string'),
            'shell origin': ({'bin/run.sh': b'python -c "import auth.store as s; print(s.__file__)"\n'},
                             'bin/run.sh names a docstring reader'),
            'file path': ({'src/cli.py': b'Path("src/auth/store.py").read_text()\n'}, 'src/cli.py names the file store.py'),
            'click': ({'src/cli.py': b'import click.testing\n'}, 'src/cli.py names click'),
            'from click': ({'src/cli.py': b'from click import command\n'}, 'src/cli.py names click'),
            'doctest config': ({'pyproject.toml': b'addopts = "--doctest-modules"\n'}, 'pyproject.toml names a docstring reader in a string'),
            'script reader': ({'bin/run': b'#!/usr/bin/env python3\nget_docstring(t)\n'}, 'bin/run names get_docstring'),
            'pyw reader': ({'src/app.pyw': b'get_docstring(t)\n'}, 'src/app.pyw names get_docstring'),
            'shell reader': ({'bin/run.sh': b'python -c "import ast; ast.get_docstring(t)"\n'}, 'bin/run.sh names a docstring reader'),
            'shell path': ({'bin/run.sh': b'python - < src/auth/store.py\n'}, 'bin/run.sh names the file store.py'),
            'broken file': ({'tests/bad.py': b'def (\n'}, 'tests/bad.py does not parse at the head (SyntaxError)'),
        }
        for name, (tree, expected) in cases.items():
            with self.subTest(name):
                own = tree.get(STORE, STORE_DOCS)[len(STORE_DOCS):]
                passed, message, _ = self.run_check(old=STORE_OLD + own,
                                                    new=tree.get(STORE, STORE_DOCS), tree={STORE: STORE_DOCS} | tree)
                self.assertFalse(passed)
                self.assertIn(expected + ', so a docstring can be code.', message)

    def test_a_bytes_path_with_a_non_ascii_name_needs_a_review(self):
        path = 'src/auth/\u00e9.py'
        tree = {path: STORE_DOCS, 'tools/run.py': b'run(b"sh src/auth/\\xc3\\xa9.py")\n'}
        passed, message, _ = self.run_check(path=path, files=(path,), tree=tree)
        self.assertFalse(passed)
        self.assertIn('tools/run.py names the file \\u00e9.py', message)
        for encoding in ('utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be', 'utf-16', 'utf-32'):
            with self.subTest(encoding=encoding):
                tree = {path: STORE_DOCS, 'tools/run.txt': 'cat src/auth/\u00e9.py | sh\n'.encode(encoding)}
                passed, message, _ = self.run_check(path=path, files=(path,), tree=tree)
                self.assertFalse(passed)
                self.assertIn('tools/run.txt names the file \\u00e9.py', message)

    def test_a_file_name_in_another_case_or_form_needs_a_review(self):
        cases = {'python upper': ('tools/run.py', b'open("src/auth/STORE.PY").read()\n'),
                 'shell upper': ('bin/run.sh', b'cat SRC/AUTH/Store.py | sh\n'),
                 'nfd': ('bin/run.sh', 'cat src/auth/e\u0301.py | sh\n'.encode())}
        for name, (reader, data) in cases.items():
            with self.subTest(name):
                path = 'src/auth/\u00e9.py' if name == 'nfd' else STORE
                passed, message, _ = self.run_check(path=path, files=(path,), tree={path: STORE_DOCS, reader: data})
                self.assertFalse(passed)
                self.assertIn('Docstring check: %s names the file' % reader, message)

    def test_a_link_or_a_copy_of_a_listed_file_needs_a_review(self):
        passed, message, _ = self.run_check(tree={STORE: STORE_DOCS, 'payload': STORE_DOCS})
        self.assertFalse(passed)
        self.assertIn('Docstring check: payload has the bytes of src/auth/store.py, so a docstring can be code.', message)

    def test_a_utf16_reader_needs_a_review(self):
        script = 'python -c "from src.auth import store; exec(store.__doc__)"\r\n'
        cases = {'bom': script.encode('utf-16'), 'no bom': script.encode('utf-16-le'),
                 'path': '& sh src/auth/store.py\r\n'.encode('utf-16')}
        for name, data in cases.items():
            with self.subTest(name):
                passed, message, _ = self.run_check(tree={STORE: STORE_DOCS, 'tools/run.ps1': data})
                self.assertFalse(passed)
                self.assertIn('Docstring check: tools/run.ps1 names', message)

    def test_an_archive_needs_a_review(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('__main__.py', 'from src.auth import store\nexec(store.__doc__)\n' * 20)
        cases = {'reader.pyz': b'#!/usr/bin/env python3\n' + data.getvalue(), 'reader.zip': data.getvalue(),
                 'reader.gz': gzip.compress(b'exec(store.__doc__)\n')}
        for path, body in cases.items():
            with self.subTest(path):
                passed, message, _ = self.run_check(tree={STORE: STORE_DOCS, path: body})
                self.assertFalse(passed)
                self.assertIn('Docstring check: %s is an archive or a compressed file' % path, message)

    def test_a_reader_of_its_own_docstring_in_an_unchanged_file_is_valid(self):
        tree = {STORE: STORE_DOCS, 'tools/claude_review.py': b'"""Help."""\nimport argparse\nargparse.ArgumentParser(description=__doc__)\n',
                'tests/conftest.py': b'from pathlib import Path\nROOT = Path(__file__).parent\n',
                '.github/gates.json': b'{"version": 1, "boundaries": ["src/auth/store.py"]}\n',
                '.github/CODEOWNERS': b'src/auth/store.py @owner\n',
                'README.md': b'The store keeps the rows.\n'}
        passed, message, _ = self.run_check(tree=tree)
        self.assertTrue(passed, message)

    def test_a_tree_read_error_needs_a_review(self):
        for error in (gate.GateError('The tree of aaaaaaaaaaaa has more than 20000 files.'), gate.tarfile.ReadError('bad')):
            with self.subTest(error=type(error).__name__):
                passed, message, _ = self.run_check(tree=error)
                self.assertFalse(passed)
                self.assertIn('The docstring check failed closed (%s' % type(error).__name__, message)

    def test_the_check_reads_the_test_merge_commit(self):
        contents = {(REPO, STORE, TIP): STORE_OLD, (REPO, STORE, HEAD): STORE_DOCS}
        fake = Fake(files=[STORE], contents=contents)
        fake.tree = {STORE: STORE_DOCS, 'src/base_only.py': b'from src.auth.store import __doc__\n'}
        passed, message, _ = check('security-review', fake, config=DOC_CONFIG)
        self.assertFalse(passed)
        self.assertEqual(fake.tree_ref, MERGE)
        self.assertIn('src/base_only.py imports __doc__ of a module', message)

    def test_a_missing_or_stale_test_merge_needs_a_review(self):
        for merge in (None, '', 'abc', OLD):
            with self.subTest(merge=merge):
                contents = {(REPO, STORE, TIP): STORE_OLD, (REPO, STORE, HEAD): STORE_DOCS}
                fake = Fake(files=[STORE], contents=contents)
                fake.state['merge_commit_sha'] = merge
                passed, message, _ = check('security-review', fake, config=DOC_CONFIG)
                self.assertFalse(passed)
                self.assertIn('Docstring check: GitHub has no test merge commit of the base ffffffffffff and the head aaaaaaaaaaaa.', message)

    def test_security_needed_skips_the_docstring_check(self):
        passed, message, _ = self.run_check(labels=['security-needed'])
        self.assertFalse(passed)
        self.assertNotIn('Docstring check', message)
        self.assertNotIn('No review is necessary.', message)

    def test_the_docstring_check_does_not_change_the_code_review(self):
        contents = {(REPO, STORE, TIP): STORE_OLD, (REPO, STORE, HEAD): STORE_DOCS}
        fake = Fake(files=[STORE], contents=contents)
        fake.tree = {STORE: STORE_DOCS}
        passed, message, _ = check('cross-review', fake)
        self.assertFalse(passed)


def tarball(files, top='owner-project-aaaaaaa', links=None):
    """Return the bytes of a tarball like the one of the GitHub API, with a top folder and a link."""
    data = io.BytesIO()
    with gate.tarfile.open(fileobj=data, mode='w:gz') as archive:
        folder = gate.tarfile.TarInfo(top)
        folder.type = gate.tarfile.DIRTYPE
        archive.addfile(folder)
        for path, body in files.items():
            info = gate.tarfile.TarInfo(top + '/' + path)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        for path, target in (links or {}).items():
            link = gate.tarfile.TarInfo(top + '/' + path)
            link.type, link.linkname = gate.tarfile.SYMTYPE, target
            archive.addfile(link)
    return data.getvalue()


def git_tree(data):
    """Return the Git tree of the GitHub API for the tarball."""
    found = []
    with gate.tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for x in archive:
            if '/' not in x.name:
                continue
            item = {'path': x.name.split('/', 1)[-1], 'type': 'tree' if x.isdir() else 'blob', 'mode': '120000' if x.issym() else '100644'}
            if x.issym():
                item['sha'] = gate.blob_sha(x.linkname.encode())
            elif x.isfile():
                item['sha'] = gate.blob_sha(archive.extractfile(x).read())
            found.append(item)
    return {'truncated': False, 'tree': found}


class TreeFilesTest(unittest.TestCase):
    def read(self, data, tree=None):
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = lambda size: data[:size]
        if tree is None:
            tree = git_tree(data)
        with mock.patch.object(gate.urllib.request, 'urlopen', return_value=response) as opened, \
                mock.patch.object(gate.GitHub, 'call', return_value=tree) as call:
            found = gate.GitHub(REPO, 7, 'token').tree_files(HEAD)
        call.assert_called_once_with('GET', '/repos/owner/project/git/trees/%s?recursive=1' % HEAD)
        self.assertEqual(opened.call_args[0][0].full_url, 'https://api.github.com/repos/owner/project/tarball/' + HEAD)
        return found

    def test_the_mode_comes_from_the_tree_of_the_folder(self):
        tree = {'tree': [{'path': 'store.py', 'type': 'blob', 'mode': '100755'}, {'path': 'x', 'type': 'tree', 'mode': '040000'}]}
        with mock.patch.object(gate.GitHub, 'call', return_value=tree) as call:
            github = gate.GitHub(REPO, 7, 'token')
            self.assertEqual(github.mode(HEAD, 'src/auth/store.py'), '100755')
            self.assertIsNone(github.mode(HEAD, 'src/auth/x'))
        self.assertEqual(call.call_args[0][1], '/repos/%s/git/trees/%s:src/auth' % (REPO, HEAD))
        with mock.patch.object(gate.GitHub, 'call', return_value={'tree': [], 'truncated': True}):
            with self.assertRaises(gate.GateError):
                gate.GitHub(REPO, 7, 'token').mode(HEAD, 'store.py')

    def test_the_tree_has_each_file_without_the_top_folder(self):
        self.assertEqual(self.read(tarball({'src/a.py': b'x = 1\n', 'README.md': b'# A\n'})),
                         {'src/a.py': b'x = 1\n', 'README.md': b'# A\n'})

    def test_a_link_gets_the_content_of_its_target(self):
        found = self.read(tarball({'shared/reader.txt': b'from a import __doc__\n'},
                                  links={'reader.py': 'shared/reader.txt', 'two.py': 'reader.py', 'gone.md': 'shared/gone.md'}))
        self.assertEqual(found['reader.py'], b'from a import __doc__\n')
        self.assertEqual(found['two.py'], b'from a import __doc__\n')
        self.assertNotIn('gone.md', found)

    def test_a_python_link_with_no_target_file_fails_closed(self):
        for target in ('loop.py', 'missing.py'):
            with self.subTest(target=target), self.assertRaisesRegex(gate.GateError, 'has no target file'):
                self.read(tarball({'shared/a.txt': b'x\n'}, links={'loop.py': target}))

    def test_a_link_to_a_folder_or_out_of_the_tree_fails_closed(self):
        for path, target in (('payload', 'src/auth'), ('payload', '.'), ('deep/payload', '../src'), ('payload', '/etc'),
                             ('payload.py', '../outside.py'), ('root', '..')):
            with self.subTest(target=target), self.assertRaisesRegex(gate.GateError, 'goes to a folder or out of the tree'):
                self.read(tarball({'src/auth/store.py': b'x\n'}, links={path: target}))

    def test_a_hard_link_fails_closed(self):
        data = io.BytesIO()
        with gate.tarfile.open(fileobj=data, mode='w:gz') as archive:
            link = gate.tarfile.TarInfo('top/a.py')
            link.type, link.linkname = gate.tarfile.LNKTYPE, 'top/b.py'
            archive.addfile(link)
        with self.assertRaisesRegex(gate.GateError, 'hard link'):
            self.read(data.getvalue())

    def test_a_tree_that_does_not_agree_with_the_tarball_fails_closed(self):
        data = tarball({'src/a.py': b'x = 1\n'})
        blob = {'path': 'src/a.py', 'type': 'blob', 'mode': '100644', 'sha': gate.blob_sha(b'x = 1\n')}
        cases = {'export-ignore': ({'truncated': False, 'tree': [blob, {'path': 'reader.py', 'type': 'blob'}]}, 'does not have reader.py'),
                 'submodule': ({'truncated': False, 'tree': [blob, {'path': 'lib', 'type': 'commit'}]}, 'has a submodule'),
                 'truncated': ({'truncated': True, 'tree': [blob]}, 'did not give the full Git tree'),
                 'no list': ({'truncated': False}, 'did not give the full Git tree'),
                 'export-subst': ({'truncated': False, 'tree': [blob | {'mode': '100644', 'sha': gate.blob_sha(b'x = $Format:%B$\n')}]},
                                  'changes src/a.py, for example by export-subst'),
                 'file as link': ({'truncated': False, 'tree': [blob | {'mode': '120000', 'sha': gate.blob_sha(b'x = 1\n')}]},
                                  'gives src/a.py as a different kind of file')}
        for name, (tree, expected) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(gate.GateError, expected):
                self.read(data, tree)

    def test_parents_reads_the_commit(self):
        with mock.patch.object(gate.GitHub, 'call', return_value={'parents': [{'sha': TIP}, {'sha': HEAD}]}) as call:
            self.assertEqual(gate.GitHub(REPO, 7, 'token').parents(MERGE), [TIP, HEAD])
        call.assert_called_once_with('GET', '/repos/owner/project/commits/' + MERGE)

    def test_a_large_tree_fails_closed(self):
        cases = {'TARBALL_BYTES': 'larger than 10 bytes', 'TREE_BYTES': 'larger than 10 bytes', 'TREE_FILES': 'more than 2 files'}
        for name, expected in cases.items():
            with self.subTest(name), mock.patch.object(gate, name, 2 if name == 'TREE_FILES' else 10):
                with self.assertRaisesRegex(gate.GateError, expected):
                    self.read(tarball({'a.py': b'x = 1\n', 'b.py': b'y = 2\n'}))


CHECKOUT_OLD, CHECKOUT_NEW = '1' * 40, '2' * 40
WORKFLOW = '.github/workflows/ci.yml'


def workflow(sha, version, extra=b''):
    return (b'jobs:\n  test:\n    steps:\n      - uses: actions/checkout@' + sha.encode() + b' # ' + version
            + b'\n        with:\n          persist-credentials: false\n      - uses: actions/setup-python@' + sha.encode()
            + b' # ' + version + b'\n' + extra)


def bump(**change):
    """Return the contents of a Dependabot PR that bumps two uses lines in one workflow and one in a composite action."""
    found = {(REPO, WORKFLOW, TIP): workflow(CHECKOUT_OLD, b'v6.0.0'), (REPO, WORKFLOW, HEAD): workflow(CHECKOUT_NEW, b'v7.0.1'),
             (REPO, 'gates/action.yml', TIP): b'runs:\n  steps:\n    - uses: a/b/c@' + CHECKOUT_OLD.encode() + b' # 1.2\n',
             (REPO, 'gates/action.yml', HEAD): b'runs:\n  steps:\n    - uses: a/b/c@' + CHECKOUT_NEW.encode() + b' # 1.3\n'}
    found.update({(REPO, path, HEAD): data for path, data in change.items()})
    return found


class BumpCheckTest(unittest.TestCase):
    def run_check(self, files=(WORKFLOW, 'gates/action.yml'), contents=None, author=gate.DEPENDABOT, labels=()):
        fake = Fake(files=files, contents=bump() if contents is None else contents, author=author, labels=labels)
        return check('security-review', fake)

    def test_a_grouped_action_bump_passes_with_no_review(self):
        passed, message, _ = self.run_check()
        self.assertTrue(passed, message)
        self.assertIn('The PR is an action bump of dependabot[bot]. No review is necessary.', message)
        self.assertIn('.github/workflows/ci.yml line 4: actions/checkout gets a new SHA and version.', message)
        self.assertIn('.github/workflows/ci.yml line 7: actions/setup-python gets a new SHA and version.', message)
        self.assertIn('gates/action.yml line 3: a/b/c gets a new SHA and version.', message)

    def test_the_bump_stays_level_1(self):
        fake = Fake(files=[WORKFLOW], contents=bump(), author=gate.DEPENDABOT)
        passed, message, _ = check('acceptance', fake)
        self.assertFalse(passed)
        self.assertIn('This PR is level 1', message)

    def test_a_bump_from_another_author_needs_a_review(self):
        passed, message, _ = self.run_check(author='mohanraj00')
        self.assertFalse(passed)
        self.assertNotIn('Bump check', message)
        self.assertIn('@codex security review', message)

    def test_a_bump_with_any_other_change_needs_a_review(self):
        name = WORKFLOW + ' line %d is not only a new SHA and version of a uses line.'
        cases = {
            'another line': (bump(**{WORKFLOW: workflow(CHECKOUT_NEW, b'v7.0.1').replace(b'false', b'true')}), name % 6),
            'another action': (bump(**{WORKFLOW: workflow(CHECKOUT_NEW, b'v7.0.1').replace(b'actions/checkout', b'evil/checkout')}), name % 4),
            'a short SHA': (bump(**{WORKFLOW: workflow(CHECKOUT_NEW[:39], b'v7.0.1')}), name % 4),
            'a branch': (bump(**{WORKFLOW: workflow(CHECKOUT_OLD, b'v6.0.0').replace(CHECKOUT_OLD.encode() + b' # v6.0.0', b'main', 1)}), name % 4),
            'a comment change only': (bump(**{WORKFLOW: workflow(CHECKOUT_OLD, b'v7.0.1')}), name % 4),
            'a text comment': (bump(**{WORKFLOW: workflow(CHECKOUT_NEW, b'run curl')}), name % 4),
            'a new indent': (bump(**{WORKFLOW: workflow(CHECKOUT_NEW, b'v7.0.1').replace(b'      - uses', b'     - uses', 1)}), name % 4),
            'an added line': (bump(**{WORKFLOW: workflow(CHECKOUT_NEW, b'v7.0.1', b'      - run: id\n')}), WORKFLOW + ' adds or removes lines.'),
        }
        for case, (contents, reason) in cases.items():
            with self.subTest(case=case):
                passed, message, _ = self.run_check(contents=contents)
                self.assertFalse(passed)
                self.assertIn('Bump check: ' + reason, message)
                self.assertIn('@codex security review', message)

    def test_a_bump_with_another_file_needs_a_review(self):
        contents = bump(**{'src/a.py': b'print(2)\n'})
        contents[(REPO, 'src/a.py', TIP)] = b'print(1)\n'
        passed, message, _ = self.run_check(files=(WORKFLOW, 'src/a.py'), contents=contents)
        self.assertFalse(passed)
        self.assertIn('Bump check: src/a.py line 1 is not only a new SHA and version of a uses line.', message)
        for item, reason in (({'path': WORKFLOW, 'previous': 'old.yml', 'status': 'renamed', 'lines': 2}, 'is not a modified file'),
                             ({'path': '.github/workflows/new.yml', 'previous': None, 'status': 'added', 'lines': 2}, 'is not a modified file')):
            with self.subTest(item=item):
                passed, message, _ = self.run_check(files=(item,))
                self.assertFalse(passed)
                self.assertIn(reason, message)

    def test_security_needed_and_an_api_error_need_a_review(self):
        passed, message, _ = self.run_check(labels=['security-needed'])
        self.assertFalse(passed)
        self.assertNotIn('No review is necessary.', message)
        error = urllib.error.HTTPError('https://api.github.com/x', 500, 'error', {}, io.BytesIO())
        passed, message, _ = self.run_check(contents=bump(**{WORKFLOW: error}))
        error.close()
        self.assertFalse(passed)
        self.assertIn('The bump check failed closed (HTTPError', message)


class CrossReviewTest(unittest.TestCase):
    def test_the_builder(self):
        issues = [{'number': 5, 'open': True, 'repo': REPO, 'labels': ['bug', 'agent:codex', 'agent:claude']}]
        fake = Fake(issues=issues, order={5: ['bug', 'agent:codex', 'agent:claude']}, ref='claude/7')
        self.assertEqual(gate.builder_of(fake, fake.state), ('codex', 'the label agent:codex of issue #5'))  # The first label wins.
        issues = [{'number': 5, 'open': True, 'repo': REPO, 'labels': ['agent:claude']}]
        fake = Fake(issues=issues, order={5: ['agent:codex', 'agent:claude']})
        self.assertEqual(gate.builder_of(fake, fake.state)[0], 'claude')  # agent:codex was removed
        for ref, builder in (('codex/7-x', 'codex'), ('agy/7', 'agy'), ('claude/7', 'claude'), ('feature/7', None), ('claudex/7', None)):
            with self.subTest(ref=ref):
                fake = Fake(ref=ref, issues=[])
                self.assertEqual(gate.builder_of(fake, fake.state)[0], builder)

    def test_a_claude_pr_needs_a_clean_codex_review_of_the_head(self):
        self.assertTrue(check('cross-review', Fake(comments=[comment(gate.CODEX, state())]))[0])
        for body in (state(commit=OLD), state(status='in_progress'), state(commit=None, status=None, findings=None)):
            with self.subTest(body=body):
                self.assertFalse(check('cross-review', Fake(comments=[comment(gate.CODEX, body)]))[0])

    def test_a_pr_with_no_harness_needs_the_codex_review(self):
        fake = Fake(ref='feature/7', issues=[], comments=[comment(gate.CODEX, state())])
        passed, message, _ = check('cross-review', fake)
        self.assertTrue(passed)
        self.assertIn('No harness', message)

    def test_only_the_codex_app_counts(self):
        for login in ('mohanraj00', gate.BOT, 'chatgpt-codex-connector'):
            with self.subTest(login=login):
                self.assertFalse(check('cross-review', Fake(comments=[comment(login, state())]))[0])

    def test_the_findings_value_alone_does_not_decide(self):
        # has_unresolved_findings stays true after the threads are resolved, so the gate reads the threads.
        fake = Fake(comments=[comment(gate.CODEX, state(findings=True, unresolved=True))], threads=0)
        self.assertTrue(check('cross-review', fake)[0])
        fake = Fake(comments=[comment(gate.CODEX, state(findings=True, unresolved=False))], threads=2)
        passed, message, _ = check('cross-review', fake)
        self.assertFalse(passed)
        self.assertIn('2 unresolved threads', message)

    def test_a_null_findings_value_reads_the_threads(self):
        # Seen on aavanam#57: a clean Codex review gave has_any_findings null.
        fake = Fake(comments=[comment(gate.CODEX, state(findings=None))], threads=0)
        passed, message, _ = check('cross-review', fake)
        self.assertTrue(passed)
        self.assertIn('gives no findings value, and no Codex thread is open', message)
        fake = Fake(comments=[comment(gate.CODEX, state(findings=None))], threads=1)
        passed, message, _ = check('cross-review', fake)
        self.assertFalse(passed)
        self.assertIn('1 unresolved threads', message)

    def test_a_changed_state_format_fails_closed(self):
        for body in ('<!-- codex-review-state:v1 {broken} -->', '<!-- codex-review-state:v1 {"code": {}} -->',
                     '<!-- codex-review-state:v1 {"code_review": {"commit_sha": 5, "status": "completed"}} -->',
                     state().replace('false', '"no"')):
            with self.subTest(body=body):
                passed, message, _ = check('cross-review', Fake(comments=[comment(gate.CODEX, body)]))
                self.assertFalse(passed)
                self.assertIn('The format changed.', message)

    def test_the_state_before_the_base_change_does_not_count(self):
        fake = Fake(comments=[comment(gate.CODEX, state())], base_changed='2026-11-07T11:00:00Z')
        self.assertFalse(check('cross-review', fake)[0])
        late = '2026-11-07T12:00:00Z'
        fake = Fake(comments=[comment(gate.CODEX, state(done='2026-11-07T11:30:00.123Z'), updated=late)], base_changed='2026-11-07T11:00:00Z')
        self.assertTrue(check('cross-review', fake)[0])
        # A later security review changes the summary, but the code review is from before the base change.
        fake = Fake(comments=[comment(gate.CODEX, state(done='2026-11-07T10:30:00.123Z'), updated=late)], base_changed='2026-11-07T11:00:00Z')
        self.assertFalse(check('cross-review', fake)[0])
        fake = Fake(comments=[comment(gate.CODEX, state(), updated=late)], base_changed='2026-11-07T11:00:00Z')
        passed, message, _ = check('cross-review', fake)
        self.assertFalse(passed)
        self.assertIn('The format changed.', message)

    def test_an_unreadable_newer_state_stops_an_older_clean_state(self):
        comments = [comment(gate.CODEX, state(), '2026-11-07T10:00:00Z'),
                    comment(gate.CODEX, '<!-- codex-review-state:v1 {broken} -->', '2026-11-07T11:00:00Z')]
        self.assertFalse(check('cross-review', Fake(comments=comments))[0])

    def test_a_failed_claude_review_stops_an_older_clean_review_of_the_head(self):
        comments = [comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'code'), '2026-11-07T10:00:00Z'),
                    comment(gate.BOT, review_body('failure', HEAD, ''), '2026-11-07T11:00:00Z')]
        self.assertFalse(check('cross-review', Fake(ref='codex/7', issues=[], comments=comments))[0])
        comments = [comments[1], comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'code'), '2026-11-07T12:00:00Z')]
        self.assertTrue(check('cross-review', Fake(ref='codex/7', issues=[], comments=comments))[0])  # A new clean review passes.

    def test_a_codex_or_agy_pr_needs_a_clean_claude_review_of_the_head(self):
        clean = comment(gate.BOT, review_body('success', HEAD, 'Findings: 0\nNo defect.', 'code'))
        for ref in ('codex/7', 'agy/7'):
            with self.subTest(ref=ref):
                self.assertTrue(check('cross-review', Fake(ref=ref, issues=[], comments=[clean]))[0])
                self.assertFalse(check('cross-review', Fake(ref=ref, issues=[], comments=[comment(gate.CODEX, state())]))[0])
        for body, login in ((review_body('success', HEAD, 'Findings: 2\nA.', 'code'), gate.BOT),
                            (review_body('success', HEAD, 'No count.', 'code'), gate.BOT),
                            (review_body('success', OLD, 'Findings: 0', 'code'), gate.BOT),
                            (review_body('success', HEAD, 'Findings: 0', 'security'), gate.BOT),
                            (review_body('success', HEAD, 'Findings: 0', 'code'), 'mohanraj00'),
                            ('<!-- claude-review:v2 kind=code head=%s base=%s findings=0 run=55 -->' % (HEAD, BASE), 'mohanraj00')):
            with self.subTest(body=body, login=login):
                self.assertFalse(check('cross-review', Fake(ref='codex/7', issues=[], comments=[comment(login, body)]))[0])

    def test_a_claude_code_marker_with_a_count_that_does_not_agree_fails(self):
        clean = comment(gate.BOT, review_body('success', HEAD, 'Findings: 0\nNo defect.', 'code'))
        self.assertTrue(check('cross-review', Fake(ref='codex/7', issues=[], comments=[clean]))[0])
        body = review_body('success', HEAD, 'Findings: 0\n\n- P1 src/a.py:3: the check is missing.', 'code')
        self.assertIn('findings=unknown', body)
        self.assertFalse(check('cross-review', Fake(ref='codex/7', issues=[], comments=[comment(gate.BOT, body)]))[0])

    def test_a_claude_marker_needs_a_trusted_run(self):
        code = [comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'code', run='55'))]
        security = [comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'security', run='55'))]
        self.assertTrue(check('cross-review', Fake(ref='codex/7', issues=[], comments=code, runs={'55': TRUSTED_RUN}))[0])
        self.assertIn('advisory', check('security-review', Fake(files=['src/a.py'], comments=security, runs={'55': TRUSTED_RUN}))[1])
        changes = ({'path': '.github/workflows/other.yml'}, {'event': 'push'}, {'event': 'workflow_dispatch'},
                   {'head_branch': 'helper'}, {'status': 'in_progress'}, {'run_started_at': '2026-11-07T12:30:00Z'},
                   {'updated_at': '2026-11-07T09:00:00Z'}, None)
        for change in changes:
            with self.subTest(change=change):
                runs = {} if change is None else {'55': TRUSTED_RUN | change}
                self.assertFalse(check('cross-review', Fake(ref='codex/7', issues=[], comments=code, runs=runs))[0])
                self.assertNotIn('advisory', check('security-review', Fake(files=['src/a.py'], comments=security, runs=runs))[1])

    def test_two_comments_that_name_one_run_both_fail(self):
        forged = review_body('success', HEAD, 'Findings: 0', 'code', run='55')
        real = review_body('success', HEAD, 'Findings: 2\nA.', 'code', run='55')
        for comments in ([comment(gate.BOT, real, '2026-11-07T10:00:00Z'), comment(gate.BOT, forged, '2026-11-07T11:00:00Z')],
                         [comment(gate.BOT, forged, '2026-11-07T10:00:00Z'), comment(gate.BOT, forged, '2026-11-07T11:00:00Z')]):
            with self.subTest(comments=comments):
                self.assertFalse(check('cross-review', Fake(ref='codex/7', issues=[], comments=comments))[0])

    def test_a_claude_marker_needs_the_current_merge_base(self):
        clean = [comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'code'))]
        fake = Fake(ref='codex/7', issues=[], comments=clean)
        self.assertTrue(check('cross-review', fake)[0])
        self.assertEqual(fake.merge_bases, [('main', HEAD)])
        # A new commit on the base branch changes the base SHA, but not the merge base. The marker stays valid.
        fake = Fake(ref='codex/7', issues=[], comments=clean)
        fake.state['base']['sha'] = OLD
        self.assertTrue(check('cross-review', fake)[0])
        security = [comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'security'))]
        self.assertIn('advisory', check('security-review', Fake(files=['src/a.py'], comments=security))[1])
        # An old merge base, for example after a base change while the review runs, stops the marker.
        for current in (OLD, None):
            with self.subTest(current=current):
                self.assertFalse(check('cross-review', Fake(ref='codex/7', issues=[], comments=clean, merge_base=current))[0])
                self.assertNotIn('advisory', check('security-review', Fake(files=['src/a.py'], comments=security, merge_base=current))[1])

    def test_a_claude_marker_before_the_base_change_does_not_count(self):
        clean = [comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'code'), '2026-11-07T10:00:00Z')]
        self.assertFalse(check('cross-review', Fake(ref='codex/7', issues=[], comments=clean, base_changed='2026-11-07T11:00:00Z'))[0])
        security = [comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'security'), '2026-11-07T10:00:00Z')]
        message = check('security-review', Fake(files=['src/a.py'], comments=security, base_changed='2026-11-07T11:00:00Z'))[1]
        self.assertNotIn('advisory', message)

    def test_a_v1_claude_marker_does_not_count(self):
        old = '<!-- claude-review:v1 kind=code head=%s findings=0 run=55 -->\nClaude code review of head %s. Findings: 0.' % (HEAD, HEAD)
        self.assertFalse(check('cross-review', Fake(ref='codex/7', issues=[], comments=[comment(gate.BOT, old)],
                                                    runs={'55': TRUSTED_RUN}))[0])

    def test_a_claude_pr_does_not_pass_with_a_claude_review(self):
        clean = comment(gate.BOT, review_body('success', HEAD, 'Findings: 0', 'code'))
        self.assertFalse(check('cross-review', Fake(comments=[clean]))[0])

    def test_the_cross_reviewed_label_needs_an_approval_record(self):
        fake = Fake(ref='agy/7', issues=[], labels=['cross-reviewed'], comments=[record('cross-reviewed')])
        self.assertTrue(check('cross-review', fake)[0])
        fake = Fake(ref='agy/7', issues=[], labels=['cross-reviewed'], comments=[record('cross-reviewed', head=OLD)])
        passed, message, _ = check('cross-review', fake)
        self.assertFalse(passed)
        self.assertEqual(fake.removed, ['cross-reviewed'])
        self.assertIn('no approval record for head', message)


    def requests(self, count, login='mohanraj00'):
        return [comment(login, '@codex review\nHead: %s' % HEAD[:7], '2026-11-07T0%d:00:00Z' % i) for i in range(count)]

    def test_a_fourth_codex_request_fails_cross_review(self):
        clean = comment(gate.CODEX, state())
        self.assertTrue(check('cross-review', Fake(comments=self.requests(3) + [clean]))[0])
        passed, message, _ = check('cross-review', Fake(comments=self.requests(4) + [clean]))
        self.assertFalse(passed)
        self.assertIn('The PR has 4 "@codex review" requests. The cap is 3 for each PR.', message)
        self.assertTrue(message.endswith('Codex review cap reached. Examine the approach and report to the owner.'))

    def test_only_a_code_review_request_from_an_admin_counts(self):
        clean = comment(gate.CODEX, state())
        others = (self.requests(4, login='someone') + [comment('mohanraj00', '@codex security review')] * 4
                  + [comment('mohanraj00', 'Ask @codex review later.')] * 4)
        self.assertTrue(check('cross-review', Fake(comments=self.requests(3) + others + [clean]))[0])
        upper = [comment('mohanraj00', '@Codex Review')]
        self.assertFalse(check('cross-review', Fake(comments=self.requests(3) + upper + [clean]))[0])

    def test_the_owner_label_lifts_the_cap_for_one_head(self):
        clean = comment(gate.CODEX, state())
        comments = self.requests(5) + [clean, record(gate.CAP_LABEL)]
        passed, message, _ = check('cross-review', Fake(labels=[gate.CAP_LABEL], comments=comments))
        self.assertTrue(passed, message)
        self.assertIn('The review-cap-lifted label applies to head aaaaaaaaaaaa', message)
        fake = Fake(labels=[gate.CAP_LABEL], comments=self.requests(5) + [clean, record(gate.CAP_LABEL, head=OLD)])
        passed, message, _ = check('cross-review', fake)
        self.assertFalse(passed)
        self.assertEqual(fake.removed, [gate.CAP_LABEL])
        self.assertIn('Codex review cap reached.', message)

    def test_an_admin_label_posts_the_cap_record(self):
        fake = Fake(labels=[gate.CAP_LABEL], comments=self.requests(4) + [comment(gate.CODEX, state())])
        passed, message, _ = check('cross-review', fake, event('labeled', label=gate.CAP_LABEL))
        self.assertTrue(passed, message)
        self.assertEqual(fake.posted, [gate.record_body(gate.CAP_LABEL, HEAD, 'main')])
        fake = Fake(labels=[gate.CAP_LABEL], comments=self.requests(4) + [comment(gate.CODEX, state())])
        self.assertFalse(check('cross-review', fake, event('labeled', label=gate.CAP_LABEL, sender='someone'))[0])
        self.assertEqual(fake.posted, [])

    def test_a_delayed_record_run_does_not_cancel_the_owner_label(self):
        # The owner adds the label at 18:00:53. The queue delays the run of the labeled event by 40 minutes.
        labeled = {gate.CAP_LABEL: ('mohanraj00', '2026-11-07T18:00:53Z')}
        comments = self.requests(4) + [comment(gate.CODEX, state())]
        for now in ('2026-11-07T18:14:10Z', '2026-11-07T18:40:00Z'):
            with self.subTest(now=now):
                fake = Fake(labels=[gate.CAP_LABEL], comments=list(comments), labeled=labeled, now=now)
                passed, message, _ = check('cross-review', fake)
                self.assertFalse(passed)  # A pending label is not an approval.
                self.assertEqual((fake.removed, fake.slept), ([], 0))
                self.assertIn('The review-cap-lifted label has no approval record yet.', message)
        fake = Fake(labels=[gate.CAP_LABEL], comments=list(comments), labeled=labeled, now='2026-11-07T18:41:00Z')
        passed, message, _ = check('cross-review', fake, event('labeled', label=gate.CAP_LABEL))
        self.assertTrue(passed, message)
        self.assertEqual((fake.posted, fake.removed), ([gate.record_body(gate.CAP_LABEL, HEAD, 'main')], []))

    def test_a_pending_label_has_a_fixed_upper_limit(self):
        labeled = {gate.CAP_LABEL: ('mohanraj00', '2026-11-07T09:00:00Z')}
        comments = self.requests(4) + [comment(gate.CODEX, state())]
        for now, removed in (('2026-11-07T14:59:59Z', []), ('2026-11-07T15:00:00Z', [gate.CAP_LABEL])):
            with self.subTest(now=now):
                fake = Fake(labels=[gate.CAP_LABEL], comments=list(comments), labeled=labeled, now=now)
                self.assertFalse(check('cross-review', fake)[0])
                self.assertEqual(fake.removed, removed)

    def test_a_delayed_label_from_a_user_who_is_not_an_admin_is_refused(self):
        labeled = {gate.CAP_LABEL: ('triage-user', '2026-11-07T12:30:00Z')}
        fake = Fake(labels=[gate.CAP_LABEL], comments=self.requests(4) + [comment(gate.CODEX, state())], labeled=labeled)
        self.assertFalse(check('cross-review', fake)[0])
        self.assertEqual(fake.removed, [gate.CAP_LABEL])
        fake = Fake(labels=[gate.CAP_LABEL], comments=self.requests(4) + [comment(gate.CODEX, state())], labeled=labeled)
        self.assertFalse(check('cross-review', fake, event('labeled', label=gate.CAP_LABEL, sender='triage-user'))[0])
        self.assertEqual((fake.posted, fake.removed), ([], [gate.CAP_LABEL]))

    def test_a_newer_record_for_a_different_head_or_base_makes_the_label_stale(self):
        labeled = {gate.CAP_LABEL: ('mohanraj00', '2026-11-07T12:30:00Z')}
        for other in (record(gate.CAP_LABEL, head=OLD), record(gate.CAP_LABEL, base='dev')):
            with self.subTest(record=other['body']):
                fake = Fake(labels=[gate.CAP_LABEL], comments=self.requests(4) + [comment(gate.CODEX, state()),
                            other | {'created_at': '2026-11-07T12:31:00Z'}], labeled=labeled)
                passed, message, _ = check('cross-review', fake)
                self.assertFalse(passed)
                self.assertEqual(fake.removed, [gate.CAP_LABEL])
                self.assertIn('no approval record for head', message)

    def test_the_cross_reviewed_label_still_passes_at_the_cap(self):
        fake = Fake(labels=['cross-reviewed'], comments=self.requests(4) + [record('cross-reviewed')])
        passed, message, _ = check('cross-review', fake)
        self.assertTrue(passed, message)


class AcceptanceTest(unittest.TestCase):
    def test_a_level0_pr_passes(self):
        passed, message, _ = check('acceptance', Fake(files=['src/a.py', 'README.md']))
        self.assertTrue(passed, message)

    def test_a_level1_pr_fails_for_the_owner(self):
        for fake in (Fake(files=['.github/workflows/ci.yml']), Fake(files=['tools/gate.py']), Fake(files=['SPEC.md']),
                     Fake(labels=['owner-needed']), Fake(complete=False)):
            with self.subTest(files=fake.file_list, labels=fake.state['labels']):
                passed, message, _ = check('acceptance', fake)
                self.assertFalse(passed)
                self.assertIn('The owner merges this PR with the admin bypass', message)

    def test_only_an_admin_can_clear_owner_needed_or_decision(self):
        passed, message, _ = check('acceptance', Fake(unlabeled={(7, 'owner-needed'): ('triage-user', '2026-11-07T11:00:00Z')}))
        self.assertFalse(passed)
        self.assertIn('owner-needed', message)
        self.assertTrue(check('acceptance', Fake(unlabeled={(7, 'owner-needed'): ('mohanraj00', '2026-11-07T11:00:00Z')}))[0])
        self.assertFalse(check('acceptance', Fake(fork=True, unlabeled={(5, 'decision'): ('triage-user', '2026-11-07T11:00:00Z')}))[0])
        self.assertTrue(check('acceptance', Fake(fork=True, reviews=APPROVED, unlabeled={(5, 'decision'): ('mohanraj00', '2026-11-07T11:00:00Z')}))[0])

    def test_a_fork_pr_that_follows_stage1_passes(self):
        passed, message, _ = check('acceptance', Fake(fork=True, reviews=APPROVED))
        self.assertTrue(passed, message)
        self.assertIn('fork checks and the owner approval pass', message)

    def test_a_fork_pr_needs_an_accepted_issue(self):
        for issues in ([], [{'number': 5, 'open': False, 'repo': REPO, 'labels': ['accepted']}],
                       [{'number': 5, 'open': True, 'repo': REPO, 'labels': ['accepted', 'decision']}],
                       [{'number': 5, 'open': True, 'repo': REPO, 'labels': []}],
                       [{'number': 5, 'open': True, 'repo': 'other/repo', 'labels': ['accepted']}]):
            with self.subTest(issues=issues):
                passed, message, _ = check('acceptance', Fake(fork=True, issues=issues))
                self.assertFalse(passed)
                self.assertIn('label accepted', message)

    def test_the_accepted_issue_label_needs_an_admin(self):
        for actors in ({5: 'triage-user'}, {}):
            with self.subTest(actors=actors):
                passed, message, _ = check('acceptance', Fake(fork=True, actors=actors))
                self.assertFalse(passed)
                self.assertIn('An admin must add the accepted label.', message)

    def test_a_fork_pr_has_a_line_limit_without_generated_files(self):
        big = {'path': 'src/a.py', 'previous': None, 'lines': 401}
        self.assertFalse(check('acceptance', Fake(fork=True, files=[big]))[0])
        generated = {'path': 'gen/a.py', 'previous': None, 'lines': 5000}
        self.assertTrue(check('acceptance', Fake(fork=True, files=[generated, 'src/a.py'], reviews=APPROVED))[0])

    def test_a_fork_pr_follows_the_template(self):
        for body in (None, '', TEMPLATE, FILLED.replace('Low. Level 0.', ''), FILLED.replace('## Risk', '## Risks')):
            with self.subTest(body=body):
                passed, message, _ = check('acceptance', Fake(fork=True, body=body))
                self.assertFalse(passed)
                self.assertIn('Fill these sections of the PR template', message)

    def test_a_fork_pr_with_a_protected_path_needs_the_owner(self):
        fake = Fake(fork=True, files=['tools/gate.py'])
        passed, message, _ = check('acceptance', fake)
        self.assertFalse(passed)
        self.assertIn('protected paths', message)
        fake = Fake(fork=True, files=['tools/gate.py'], labels=['accepted'], comments=[record('accepted')])
        passed, message, _ = check('acceptance', fake)
        self.assertNotIn('protected paths', message)
        self.assertIn('level 1', message)  # The owner still merges it.

    def test_a_push_removes_the_ready_for_review_label_of_a_fork_pr(self):
        fake = Fake(fork=True, labels=['ready-for-review'], comments=[record('ready-for-review', head=OLD)])
        passed, message, _ = check('acceptance', fake, event('synchronize'))
        self.assertFalse(passed)  # A push after the approval makes the gate fail again.
        self.assertIn('Stage 2 needs an owner approval', message)
        self.assertEqual(fake.removed, ['ready-for-review'])
        fake = Fake(fork=True, labels=['ready-for-review'], comments=[record('ready-for-review')])
        check('acceptance', fake, event('synchronize'))
        self.assertEqual(fake.removed, [])
        fake = Fake(fork=True, labels=['ready-for-review'])
        check('acceptance', fake, event('labeled', 'ready-for-review'))
        self.assertEqual((len(fake.posted), fake.removed), (1, []))
        fake = Fake(labels=['ready-for-review'])
        check('acceptance', fake, event('synchronize'))
        self.assertEqual(fake.removed, [])  # The label applies to fork PRs only.

    def test_a_fork_pr_needs_an_owner_approval_of_the_head(self):
        passed, message, _ = check('acceptance', Fake(fork=True))
        self.assertFalse(passed)
        self.assertIn('Stage 2 needs an owner approval of head %s on base main' % HEAD[:12], message)
        self.assertIn('gh run rerun', message)
        self.assertTrue(check('acceptance', Fake(fork=True, reviews=APPROVED))[0])
        fake = Fake(fork=True, labels=['ready-for-review'], comments=[record('ready-for-review')])
        self.assertTrue(check('acceptance', fake)[0])
        for reviews in ([approval(OLD)], [approval(login='writer-user')], [approval(state='DISMISSED')],
                        [approval(state='COMMENTED')], [approval(), approval(state='CHANGES_REQUESTED', at='2026-11-07T12:00:00Z', number=2)],
                        [approval(at='2026-11-07T09:00:00Z')]):
            with self.subTest(reviews=reviews):
                passed, message, _ = check('acceptance', Fake(fork=True, reviews=reviews, base_changed='2026-11-07T10:00:00Z'))
                self.assertFalse(passed)
                self.assertIn('Stage 2 needs an owner approval', message)
        # A comment review after the approval does not change it, as in GitHub.
        later = [approval(), approval(state='COMMENTED', at='2026-11-07T12:00:00Z', number=2)]
        self.assertTrue(check('acceptance', Fake(fork=True, reviews=later))[0])
        self.assertTrue(check('acceptance', Fake(fork=True, reviews=APPROVED, base_changed='2026-11-07T10:00:00Z'))[0])

    def test_a_pr_that_is_not_from_a_fork_needs_no_approval(self):
        passed, message, _ = check('acceptance', Fake())
        self.assertTrue(passed, message)
        self.assertNotIn('Stage 2', message)


def request(kind='code', body=None):
    """Return the issue_comment event of a Claude review request on the PR."""
    return {'action': 'created', 'repository': {'full_name': REPO}, 'sender': {'login': 'mohanraj00'},
            'issue': {'number': 7, 'pull_request': {'url': 'x'}}, 'comment': {'body': body or gate.CLAUDE_REQUESTS[kind]}}


def marker(head=HEAD, kind='code', base=BASE):
    return comment(gate.BOT, review_body('success', head, 'Findings: 0', kind, base=base))


class ClaudeRequestTest(unittest.TestCase):
    def test_a_same_repo_pr_runs(self):
        passed, message, _ = check('claude-review', Fake(), request())
        self.assertTrue(passed, message)
        self.assertIn('The Claude code review of head %s can start.' % HEAD[:12], message)
        self.assertNotIn('Stage 2', message)

    def test_a_passed_request_names_the_approved_head_and_base(self):
        # The review job compares its fetched head, base and merge base with these outputs.
        self.assertEqual(check('claude-review', Fake(), request())[2], {'head': HEAD, 'base': 'main', 'merge_base': BASE, 'kind': 'code'})
        self.assertEqual(check('claude-review', Fake(fork=True), request())[2], {})

    def test_a_request_without_a_merge_base_stops(self):
        passed, message, outputs = check('claude-review', Fake(merge_base=None), request())
        self.assertFalse(passed)
        self.assertIn('The gate cannot get the merge base of head %s and base main.' % HEAD[:12], message)
        self.assertEqual(outputs, {})

    def test_a_fork_pr_without_an_approval_stops(self):
        for fake in (Fake(fork=True), Fake(fork=True, reviews=[approval(OLD)]),
                     Fake(fork=True, labels=['ready-for-review'], comments=[record('ready-for-review', head=OLD)])):
            with self.subTest(reviews=fake.review_list, labels=fake.state['labels']):
                passed, message, _ = check('claude-review', fake, request())
                self.assertFalse(passed)
                self.assertIn('This PR comes from the fork someone/project.', message)
                self.assertIn('Stage 2 needs an owner approval of head %s on base main before a review.' % HEAD[:12], message)
                self.assertEqual(fake.removed, [])  # The gate job has read permissions only.

    def test_a_fork_pr_with_an_approval_runs(self):
        for fake in (Fake(fork=True, reviews=APPROVED),
                     Fake(fork=True, labels=['ready-for-review'], comments=[record('ready-for-review')])):
            with self.subTest(reviews=fake.review_list, labels=fake.state['labels']):
                passed, message, _ = check('claude-review', fake, request('security'))
                self.assertTrue(passed, message)
                self.assertIn('The Claude security review of head', message)

    def test_the_stage2_rule_is_the_rule_of_acceptance(self):
        for fake in (lambda: Fake(fork=True), lambda: Fake(fork=True, reviews=APPROVED),
                     lambda: Fake(fork=True, reviews=[approval(at='2026-11-07T09:00:00Z')], base_changed='2026-11-07T10:00:00Z'),
                     lambda: Fake(fork=True, labels=['ready-for-review'], comments=[record('ready-for-review')])):
            with self.subTest():
                self.assertEqual(check('claude-review', fake(), request())[0], check('acceptance', fake())[0])

    def test_a_second_request_for_one_head_stops(self):
        passed, message, _ = check('claude-review', Fake(comments=[marker()]), request())
        self.assertFalse(passed)
        self.assertIn('A trusted Claude code review of head %s on the current merge base is on the PR.' % HEAD[:12], message)
        # A review of a different kind, head or merge base, or a marker that the gates do not trust, does not stop the request.
        for fake in (Fake(comments=[marker(kind='security')]), Fake(comments=[marker(OLD)]),
                     Fake(comments=[marker(base='d' * 40)]), Fake(comments=[marker()], runs={}),
                     Fake(comments=[marker()], base_changed='2026-11-07T11:00:00Z'),
                     Fake(comments=[comment(gate.BOT, review_body('failure', HEAD, '', 'code'))]),
                     Fake(comments=[comment('writer-user', review_body('success', HEAD, 'Findings: 0', 'code'))])):
            with self.subTest(comments=fake.comment_list, runs=fake.runs):
                passed, message, _ = check('claude-review', fake, request())
                self.assertTrue(passed, message)

    def test_a_4th_request_stops(self):
        two = [marker(OLD), marker('d' * 40)]
        self.assertTrue(check('claude-review', Fake(comments=two), request())[0])
        passed, message, _ = check('claude-review', Fake(comments=two + [marker('e' * 40, base='f' * 40)]), request())
        self.assertFalse(passed)
        self.assertIn('The PR has 3 trusted Claude code reviews. The cap is 3 for each PR.', message)
        # The cap is for each kind.
        self.assertTrue(check('claude-review', Fake(comments=two + [marker('e' * 40)]), request('security'))[0])

    def test_the_owner_label_lifts_the_claude_cap_for_one_head(self):
        three = [marker(OLD), marker('d' * 40), marker('e' * 40)]
        for kind in ('code', 'security'):
            with self.subTest(kind=kind):
                markers = [marker(h, kind=kind) for h in (OLD, 'd' * 40, 'e' * 40)]
                fake = Fake(labels=[gate.CAP_LABEL], comments=markers + [record(gate.CAP_LABEL)])
                passed, message, outputs = check('claude-review', fake, request(kind))
                self.assertTrue(passed, message)
                self.assertIn('The review-cap-lifted label applies to head %s, so the cap of 3 reviews does not apply.' % HEAD[:12], message)
                self.assertEqual(outputs['head'], HEAD)
        # The label does not lift the rule of one review for each head and merge base.
        fake = Fake(labels=[gate.CAP_LABEL], comments=three + [marker(), record(gate.CAP_LABEL)])
        passed, message, _ = check('claude-review', fake, request())
        self.assertFalse(passed)
        self.assertIn('A trusted Claude code review of head %s on the current merge base is on the PR.' % HEAD[:12], message)
        # Without a record for the current head and base, or without an admin label, the cap applies.
        for fake in (Fake(comments=three + [record(gate.CAP_LABEL)]),
                     Fake(labels=[gate.CAP_LABEL], comments=three + [record(gate.CAP_LABEL, head=OLD)]),
                     Fake(labels=[gate.CAP_LABEL], comments=three + [record(gate.CAP_LABEL, base='dev')]),
                     Fake(labels=[gate.CAP_LABEL], comments=three + [record(gate.CAP_LABEL)],
                          labeled={gate.CAP_LABEL: ('triage-user', '2026-11-07T09:00:00Z')}),
                     Fake(labels=[gate.CAP_LABEL], comments=three + [comment('mohanraj00', gate.record_body(gate.CAP_LABEL, HEAD, 'main'))])):
            with self.subTest(labels=fake.state['labels'], comments=fake.comment_list[3:], labeled=fake.labeled):
                passed, message, _ = check('claude-review', fake, request())
                self.assertFalse(passed)
                self.assertIn('The PR has 3 trusted Claude code reviews. The cap is 3 for each PR.', message)
                self.assertEqual(fake.removed, [])  # The gate job has read permissions only.

    def test_the_request_text_selects_the_kind(self):
        self.assertIn('security review', check('claude-review', Fake(), request(body=gate.CLAUDE_REQUESTS['security'].upper()))[1])

    def test_the_first_line_selects_the_kind_and_a_later_line_does_not_stop_it(self):
        code, security = gate.CLAUDE_REQUESTS['code'], gate.CLAUDE_REQUESTS['security']
        for body, kind in ((code + '\nHead: abc1234', 'code'), (code + '\r\nHead: abc1234\r\n', 'code'),
                           ('  ' + code.replace(' ', '   ') + '  ', 'code'), (code.upper() + '\n\nmore text', 'code'),
                           ('\n' + security.replace(' ', '\t') + '\nHead: abc1234', 'security')):
            with self.subTest(body=body):
                passed, message, outputs = check('claude-review', Fake(), request(body=body))
                self.assertTrue(passed, message)
                self.assertEqual(outputs['kind'], kind)

    def test_other_text_is_not_a_request_and_passes_with_no_kind(self):
        code = gate.CLAUDE_REQUESTS['code']
        for body in ('@claude review this PR.', '@claude', 'Please ask: ' + code, 'Head: abc1234\n' + code, code + ' Extra words.',
                     code[:-1], '', None):
            with self.subTest(body=body):
                self.assertIsNone(gate.request_kind(request(body='x') | {'comment': {'body': body}}))
                passed, message, outputs = gate.run('claude-review', Fake(), request(body='x') | {'comment': {'body': body}}, None)
                self.assertEqual((passed, outputs), (True, {'kind': ''}))


class MainTest(unittest.TestCase):
    def main(self, check_name, error=None, files=None, ev=None):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.github').mkdir()
            (Path(folder) / '.github/gates.json').write_text(json.dumps({'version': 1, **CONFIG}))
            (Path(folder) / '.github/pull_request_template.md').write_text(TEMPLATE)
            Path(folder, 'event.json').write_text(json.dumps(ev or event()))
            env = {'GITHUB_EVENT_PATH': str(Path(folder, 'event.json')), 'GITHUB_WORKSPACE': folder, 'GITHUB_TOKEN': 'sample',
                   'GITHUB_OUTPUT': str(Path(folder, 'out'))}
            fake = Fake(files=files or ['src/a.py'])
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), mock.patch.object(gate, 'GitHub') as api:
                api.return_value = fake
                if error:
                    fake.comments = mock.Mock(side_effect=error)
                status = gate.main([check_name])
            output = Path(folder, 'out').read_text() if Path(folder, 'out').exists() else ''
        return status, out.getvalue(), output

    def test_an_api_error_fails_closed(self):
        for error in (urllib.error.HTTPError('https://api.github.com/x', 502, 'Bad Gateway', {}, None),
                      urllib.error.URLError('timeout'), gate.GateError('Bad data.'), KeyError('body')):
            with self.subTest(error=error):
                status, out, _ = self.main('security-review', error)
                self.assertEqual(status, 1)
                self.assertIn('fails closed', out)
                self.assertIn('::error title=security-review::', out)

    def test_a_file_name_cannot_write_a_workflow_command(self):
        status, out, _ = self.main('security-review', files=['src/a\n::warning::x.py'])
        self.assertEqual(status, 1)
        self.assertNotIn('\n::warning', out)

    def test_a_stopped_claude_request_writes_the_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'event.json').write_text(json.dumps(request()))
            env = {'GITHUB_EVENT_PATH': str(Path(folder, 'event.json')), 'GITHUB_WORKSPACE': folder, 'GITHUB_TOKEN': 'sample',
                   'GITHUB_OUTPUT': str(Path(folder, 'out'))}
            out = io.StringIO()
            # The folder has no .github/gates.json: the gate job has no checkout.
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), mock.patch.object(gate, 'GitHub') as api:
                api.return_value = Fake(fork=True)
                status = gate.main(['claude-review'])
            api.assert_called_once_with(REPO, 7, 'sample')
            output = Path(folder, 'out').read_text()
        self.assertEqual(status, 1)
        self.assertIn('::error title=claude-review::Stage 2 needs an owner approval', out.getvalue())
        self.assertTrue(output.startswith('reason=This PR comes from the fork someone/project. Stage 2 needs'))
        self.assertTrue(output.endswith('kind=code\n'))
        self.assertEqual(output.count('\n'), 2)
        self.assertEqual(self.main('claude-review')[0], 1)  # A pull_request_target event is not a request.

    def test_a_passed_claude_request_writes_the_head_and_base(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'event.json').write_text(json.dumps(request()))
            env = {'GITHUB_EVENT_PATH': str(Path(folder, 'event.json')), 'GITHUB_WORKSPACE': folder, 'GITHUB_TOKEN': 'sample',
                   'GITHUB_OUTPUT': str(Path(folder, 'out'))}
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), mock.patch.object(gate, 'GitHub') as api:
                api.return_value = Fake()
                status = gate.main(['claude-review'])
            output = Path(folder, 'out').read_text()
        self.assertEqual(status, 0)
        self.assertEqual(output, 'head=%s\nbase=main\nmerge_base=%s\nkind=code\n' % (HEAD, BASE))

    def test_classify_writes_the_outputs(self):
        self.assertEqual(self.main('classify', files=['README.md'])[2], 'docs_only=true\ncode=false\n')
        push = {'repository': {'full_name': REPO}}
        self.assertEqual(self.main('classify', ev=push)[2], 'docs_only=false\ncode=true\n')
        self.assertEqual(self.main('acceptance', ev=push)[0], 1)


class MergeBaseTest(unittest.TestCase):
    def test_the_merge_base_comes_from_the_compare_api(self):
        api = gate.GitHub(REPO, 7, 'sample')
        for data, found in (({'merge_base_commit': {'sha': BASE}}, BASE), ({'merge_base_commit': {'sha': 'abc'}}, None),
                            ({'merge_base_commit': None}, None), ({}, None), (None, None)):
            with self.subTest(data=data), mock.patch.object(api, 'call', return_value=data) as call:
                self.assertEqual(api.merge_base('release/1 #x', HEAD), found)
                call.assert_called_once_with('GET', '/repos/%s/compare/release/1%%20%%23x...%s' % (REPO, HEAD))


    def test_the_content_comes_from_the_contents_api(self):
        api = gate.GitHub(REPO, 7, 'sample')
        data = {'type': 'file', 'encoding': 'base64', 'sha': 'x', 'content': 'cHJpbnQoMSkK\n'}
        with mock.patch.object(api, 'call', return_value=data) as call:
            self.assertEqual(api.content(gate.UPSTREAM, 'gates/claude_review.py', HEAD), ('x', b'print(1)\n'))
            call.assert_called_once_with('GET', '/repos/mohanraj00/.github/contents/gates/claude_review.py?ref=' + HEAD)
        for data in ({'type': 'dir'}, {'type': 'file', 'encoding': 'none'}, [], None):
            with self.subTest(data=data), mock.patch.object(api, 'call', return_value=data):
                self.assertRaises(gate.GateError, api.content, REPO, 'a', HEAD)
        missing = urllib.error.HTTPError('https://api.github.com/x', 404, 'missing', {}, io.BytesIO())
        with mock.patch.object(api, 'call', side_effect=missing):
            self.assertIsNone(api.content(REPO, 'a', HEAD))

    def test_a_commit_is_on_main_if_main_is_ahead_or_identical(self):
        api = gate.GitHub(REPO, 7, 'sample')
        for status, found in (('ahead', True), ('identical', True), ('behind', False), ('diverged', False)):
            with self.subTest(status=status), mock.patch.object(api, 'call', return_value={'status': status}) as call:
                self.assertEqual(api.on_main(gate.UPSTREAM, PIN_NEW), found)
                call.assert_called_once_with('GET', '/repos/mohanraj00/.github/compare/%s...main' % PIN_NEW)

if __name__ == '__main__':
    unittest.main()
