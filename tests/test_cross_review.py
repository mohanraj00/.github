"""The local agy review checks the login with one short agy call, not with a token file."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'gates/cross_review.py'
# The stub answers like agy. The login test prompt is the only short prompt. The mode comes from the file "mode" next to the stub.
STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import sys
    from pathlib import Path
    here = Path(__file__).resolve().parent
    if sys.argv[1:] == ['--version']:
        print('9.9.9')
        raise SystemExit(0)
    prompt = sys.argv[sys.argv.index('-p') + 1]
    with open(here / 'calls.txt', 'a') as log:
        log.write(prompt.splitlines()[0][:40] + '\\n')
    mode = (here / 'mode').read_text().strip()
    if mode == 'hang':
        import time
        time.sleep(30)
    if mode == 'browser':
        print('Open this URL to sign in: https://accounts.google.com/o/oauth2/auth?x=1')
        raise SystemExit(0)
    if mode == 'fail':
        print('error: request failed', file=sys.stderr)
        raise SystemExit(3)
    print('ok' if len(prompt) < 100 else 'no findings')
''')


def git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@x'})


class CrossReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.bin, self.home, self.repo, self.work = tmp / 'bin', tmp / 'home', tmp / 'repo', tmp / 'work'
        for folder in (self.bin, self.home, self.repo / '.github'):
            folder.mkdir(parents=True)
        (self.bin / 'agy').write_text(STUB)
        (self.bin / 'agy').chmod(0o755)
        (self.repo / 'CONSTITUTION.md').write_text('Rules.\n')
        (self.repo / 'AGENTS.md').write_text('Guidelines.\n')
        (self.repo / '.github/gates.json').write_text('{"version": 1, "code": ["src/*"]}\n')
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'base')
        git(self.repo, 'checkout', '-q', '-b', 'topic')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/a.py').write_text('x = 1\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'change')

    def run_tool(self, mode, *extra):
        (self.bin / 'mode').write_text(mode)
        env = {'PATH': '%s:%s' % (self.bin, os.environ['PATH']), 'HOME': str(self.home)}
        return subprocess.run([sys.executable, '-B', str(TOOL), '--base', 'main', '--work', str(self.work), *extra],
                              cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        path = self.bin / 'calls.txt'
        return path.read_text().splitlines() if path.exists() else []

    def test_a_signed_in_agy_without_a_token_file_runs_the_review(self):
        self.assertFalse((self.home / '.gemini').exists())
        result = self.run_tool('ok')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('no findings', result.stdout)
        self.assertEqual(len(self.calls()), 2)  # The login test call, then the code review.
        self.assertTrue(self.calls()[1].startswith('Review this change'))

    def test_an_agy_that_asks_for_a_browser_sign_in_stops_before_the_review(self):
        result = self.run_tool('browser')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('agy has no working login', result.stderr)
        self.assertIn('asks for a sign-in', result.stderr)
        self.assertEqual(len(self.calls()), 1)  # Only the login test call. No diff went to agy.
        self.assertFalse(self.work.exists())

    def test_an_agy_that_fails_stops_before_the_review(self):
        result = self.run_tool('fail')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exit 3', result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_an_agy_that_does_not_answer_in_the_time_limit_stops_before_the_review(self):
        result = self.run_tool('hang', '--login-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did not finish in 1s', result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_a_security_path_adds_the_security_review(self):
        (self.repo / '.github/workflows').mkdir()
        (self.repo / '.github/workflows/ci.yml').write_text('name: ci\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'workflow')
        result = self.run_tool('ok')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Security-relevant paths: .github/workflows/ci.yml', result.stdout)
        self.assertEqual(len(self.calls()), 3)


if __name__ == '__main__':
    unittest.main()
