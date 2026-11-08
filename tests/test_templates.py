"""The caller templates and the workflows of this repository keep the hardening rules of the project standard."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
CALLER = (ROOT / 'docs/gates-caller.yml').read_text()
RERUN = (ROOT / 'docs/gates-rerun.yml').read_text()
CI_OK = (ROOT / 'docs/ci-ok.yml').read_text()
WORKFLOWS = {p.relative_to(ROOT).as_posix(): p.read_text() for p in
             sorted((ROOT / '.github/workflows').glob('*.yml')) + sorted((ROOT / 'docs').glob('*.yml'))}


def jobs(text):
    """Return {job id: job text}."""
    body = text.split('\njobs:\n', 1)[1]
    parts = re.split(r'^  ([a-z][a-z-]*):\n', body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


class HardeningTest(unittest.TestCase):
    def test_each_workflow_has_no_default_permissions(self):
        for name, text in WORKFLOWS.items():
            with self.subTest(name=name):
                self.assertRegex(text, r'\npermissions: \{\}\n')
                self.assertNotRegex(text, r'\n *paths-ignore:')

    def test_each_job_has_a_timeout_and_its_own_permissions(self):
        for name, text in WORKFLOWS.items():
            for job, body in jobs(text).items():
                with self.subTest(name=name, job=job):
                    self.assertRegex(body, r'\n    timeout-minutes: \d+\n')
                    self.assertRegex(body, r'\n    permissions:')

    def test_each_action_has_a_full_sha_pin(self):
        for name, text in WORKFLOWS.items():
            for ref in re.findall(r'uses: (\S+)', text):
                with self.subTest(name=name, ref=ref):
                    self.assertTrue(ref == './gates' or re.fullmatch(r'[\w.-]+/[\w./-]+@[0-9a-f]{40}', ref), ref)

    def test_each_checkout_keeps_no_credentials(self):
        for name, text in WORKFLOWS.items():
            with self.subTest(name=name):
                self.assertEqual(text.count('uses: actions/checkout@'), text.count('persist-credentials: false'))


class CallerTest(unittest.TestCase):
    def test_the_caller_has_the_three_check_names(self):
        for text in (CALLER, (ROOT / '.github/workflows/gates.yml').read_text()):
            found = jobs(text)
            self.assertEqual(list(found), ['cross-review', 'security-review', 'acceptance'])
            for job, body in found.items():
                with self.subTest(job=job):
                    self.assertTrue(body.startswith('    name: %s\n' % job))
                    self.assertIn('check: %s\n' % job, body)
                    self.assertIn('ref: ${{ github.event.pull_request.base.sha }}', body)

    def test_the_caller_uses_only_pull_request_target(self):
        self.assertRegex(CALLER, r'\non:\n  pull_request_target:\n    types: \[[a-z_, ]+\]\npermissions')
        self.assertNotIn('github.event.pull_request.head', CALLER)
        self.assertIn('mohanraj00/.github/gates@', CALLER)

    def test_the_rerun_starts_only_for_the_reviewers(self):
        for login in ('chatgpt-codex-connector[bot]', 'github-actions[bot]'):
            self.assertIn("github.event.comment.user.login == '%s'" % login, RERUN)
        self.assertIn('workflows: [Claude review]', RERUN)
        self.assertIn('actions/workflows/gates.yml/runs', RERUN)
        self.assertNotIn('actions/checkout', RERUN)

    def test_the_rerun_skips_a_claude_run_that_was_skipped(self):
        condition = jobs(RERUN)['rerun'].split('if: >-\n', 1)[1].split('\n    runs-on:', 1)[0]
        condition = ' '.join(condition.split())
        self.assertIn("github.event_name == 'workflow_run' && github.event.workflow_run.conclusion != 'skipped'", condition)
        branches = condition.split(' || (github.event_name == ')
        self.assertEqual(len(branches), 2)
        self.assertNotIn("event_name == 'workflow_run'", branches[0])
        for login in ('chatgpt-codex-connector[bot]', 'github-actions[bot]'):
            self.assertIn("github.event.comment.user.login == '%s'" % login, branches[0])
        self.assertIn("github.event_name == 'issue_comment'", branches[0])

    def test_ci_ok_collects_the_jobs_and_accepts_a_skipped_job(self):
        found = jobs(CI_OK)
        self.assertIn("if: needs.changes.outputs.code == 'true'", found['test'])
        self.assertNotIn('needs:', found['check'])  # The project check always runs.
        self.assertIn('needs: [changes, check, test]', found['ci-ok'])
        self.assertIn('if: always()', found['ci-ok'])
        self.assertIn('("success", "skipped")', found['ci-ok'])
        self.assertIn('check: classify', found['changes'])


if __name__ == '__main__':
    unittest.main()
