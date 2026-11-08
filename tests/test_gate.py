"""The shared gates pass and fail as the project standard says. The tests use a fake GitHub API and no network."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gates'))
import gate  # noqa: E402
from claude_review import comment_body  # noqa: E402

REPO = 'owner/project'
HEAD = 'a' * 40
OLD = 'b' * 40
# The merge base of the base branch and HEAD. A Claude marker of the tests names it.
BASE = 'c' * 40
TEMPLATE = (ROOT / '.github/pull_request_template.md').read_text()
FILLED = 'Closes #5\n\n## Testing\n\n`make check`: 10 tests pass.\n\n## Risk\n\nLow. Level 0.\n\n## How to revert\n\nRevert the commit.\n'
CONFIG = {'code': ['src/*', 'tests/*'], 'security_sources': ['src/*.py'], 'not_security': ['*/test_*.py'],
          'boundaries': ['tools/*', 'src/auth/*'], 'level1': ['tools/*', 'SPEC.md'], 'generated': ['gen/*']}
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
                 runs=None, reviews=(), merge_base=BASE):
        self.repo, self.number = REPO, 7
        self.labeled = {x: ('mohanraj00', '2026-11-07T09:00:00Z') for x in labels} | (labeled or {})
        self.clock = gate.stamp(now)
        self.unlabeled = unlabeled or {}
        self.slept = 0
        self.state = {'labels': [{'name': x} for x in labels], 'body': body,
                      'head': {'sha': head, 'ref': ref, 'repo': {'full_name': 'someone/project' if fork else REPO}},
                      'base': {'ref': 'main', 'repo': {'default_branch': 'main'}}}
        self.file_list = [{'path': x, 'previous': None, 'lines': 10} if isinstance(x, str) else x for x in files]
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

    def workflow_run(self, run_id):
        return dict(TRUSTED_RUN) if self.runs is None else self.runs.get(run_id)

    def post_comment(self, body):
        self.posted.append(body)
        self.comment_list.append(comment(gate.BOT, body, '2026-11-07T12:00:00Z'))

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


def check(name, fake, ev=None):
    passed, lines, outputs = gate.run(name, fake, ev or event(), CONFIG, TEMPLATE)
    return passed, '\n'.join(lines), outputs


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
                fake = Fake(files=['src/a.py'], labels=['security-reviewed'], comments=comments)
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
                            labeled={'security-reviewed': (actor, at)})
                self.assertFalse(check('security-review', fake)[0])
                self.assertEqual(fake.removed, ['security-reviewed'])
                fake = Fake(ref='agy/7', issues=[], labels=['cross-reviewed'], comments=[record('cross-reviewed')],
                            labeled={'cross-reviewed': (actor, at)})
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
                fake = Fake(files=['src/a.py'], labels=['security-reviewed'])
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


if __name__ == '__main__':
    unittest.main()
