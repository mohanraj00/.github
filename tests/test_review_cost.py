"""The review cost line of the gates and the table of tools/review_cost.py. The tests use fixed data and no network."""
import contextlib
import io
import json
import os
import tempfile
from unittest import mock
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gates'))
sys.path.insert(0, str(ROOT / 'tools'))
import gate  # noqa: E402
import review_cost  # noqa: E402
from test_gate import CONFIG, Fake, comment, event  # noqa: E402

REPO = 'owner/project'


class LineTest(unittest.TestCase):
    def test_the_line_format(self):
        comments = [comment('mohanraj00', '@codex review\nHead: aaaaaaa'), comment('mohanraj00', '@CODEX review'),
                    comment('mohanraj00', '@codex security review'), comment('someone', '@codex security review'),
                    comment('mohanraj00', gate.CLAUDE_REQUESTS['code']), comment('mohanraj00', 'Ask @codex review later.')]
        fake = Fake(comments=comments, now='2026-11-07T13:30:00Z')
        fake.state['created_at'] = '2026-11-07T09:00:00Z'
        line = gate.review_cost(fake, fake.pr(), comments)
        self.assertEqual(line, 'review-cost v1 codex=2 security=1 claude=1 hours=4.5')
        self.assertTrue(review_cost.LINE.match(line))

    def run_main(self, files):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.github').mkdir()
            (root / '.github/gates.json').write_text(json.dumps(dict(CONFIG, version=1)))
            (root / 'event.json').write_text(json.dumps(event()))
            env = {'GITHUB_EVENT_PATH': str(root / 'event.json'), 'GITHUB_WORKSPACE': folder, 'GITHUB_TOKEN': 't',
                   'GITHUB_STEP_SUMMARY': str(root / 'summary')}
            fake = Fake(files=files, comments=[comment('mohanraj00', '@codex security review')], now='2026-11-07T10:00:00Z')
            fake.state['created_at'] = '2026-11-07T09:00:00Z'
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), mock.patch.object(gate, 'GitHub', return_value=fake), contextlib.redirect_stdout(out):
                code = gate.main(['security-review'])
            summary = (root / 'summary').read_text() if (root / 'summary').exists() else ''
            return code, out.getvalue(), summary

    def test_a_passed_gate_writes_the_line_to_a_notice_and_the_job_summary(self):
        code, out, summary = self.run_main(['README.md'])
        self.assertEqual(code, 0)
        self.assertIn('::notice title=review-cost::review-cost v1 codex=0 security=1 claude=0 hours=1.0\n', out)
        self.assertEqual(summary, 'review-cost v1 codex=0 security=1 claude=0 hours=1.0\n')

    def test_a_failed_gate_writes_no_line(self):
        code, out, summary = self.run_main(['src/a.py'])
        self.assertEqual(code, 1)
        self.assertNotIn('review-cost', out + summary)


PRS = {
    1: ('h1', [{'id': 11, 'name': 'cross-review', 'conclusion': 'success', 'completed_at': '2026-11-02T10:00:00Z'},
               {'id': 12, 'name': 'security-review', 'conclusion': 'success', 'completed_at': '2026-11-02T12:00:00Z'},
               {'id': 13, 'name': 'acceptance', 'conclusion': 'failure', 'completed_at': '2026-11-02T13:00:00Z'},
               {'id': 14, 'name': 'test', 'conclusion': 'success', 'completed_at': '2026-11-02T14:00:00Z'}]),
    2: ('h2', [{'id': 21, 'name': 'acceptance', 'conclusion': 'success', 'completed_at': '2026-11-03T10:00:00Z'}]),
    3: ('h3', []),
}
NOTES = {
    11: [{'title': 'review-cost', 'message': 'review-cost v1 codex=1 security=0 claude=0 hours=1.0'}],
    12: [{'title': 'other', 'message': 'review-cost v1 codex=9 security=9 claude=9 hours=9.0'},
         {'title': 'review-cost', 'message': 'review-cost v1 codex=2 security=1 claude=0 hours=3.0'}],
    13: [{'title': 'review-cost', 'message': 'review-cost v1 codex=7 security=7 claude=7 hours=7.0'}],
    21: [{'title': 'review-cost', 'message': 'review-cost v1 codex=0 security=0 claude=2 hours=0.5'}],
}


def fetch(path):
    if path.startswith('/search/issues?'):
        assert 'merged%3A2026-11-01..2026-11-30' in path, path
        return {'incomplete_results': False, 'items': [{'number': 3}, {'number': 1}, {'number': 2}]}
    if path.startswith('/repos/%s/pulls/' % REPO):
        return {'head': {'sha': PRS[int(path.rsplit('/', 1)[1])][0]}}
    if '/check-runs?' in path:
        head = path.split('/commits/')[1].split('/')[0]
        return {'check_runs': next(runs for sha, runs in PRS.values() if sha == head)}
    if '/annotations?' in path:
        return NOTES.get(int(path.split('/check-runs/')[1].split('/')[0]), [])
    raise AssertionError(path)


class TableTest(unittest.TestCase):
    def test_the_table_for_a_date_range(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(review_cost.main([REPO, '2026-11-01', '2026-11-30'], fetch), 0)
        self.assertEqual(out.getvalue(), '\n'.join([
            '| PR | Codex code | Security | Claude | Hours |',
            '|---|---|---|---|---|',
            '| #1 | 2 | 1 | 0 | 3.0 |',
            '| #2 | 0 | 0 | 2 | 0.5 |',
            '| #3 | no line | no line | no line | no line |',
            '| Total (2 PRs with a line) | 2 | 1 | 2 | median 1.8 |',
        ]) + '\n')

    def test_bad_arguments_stop(self):
        for argv in (['owner', '2026-11-01', '2026-11-30'], [REPO, '2026-11-30', '2026-11-01'], [REPO, '2026-11', '2026-11-30']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                review_cost.main(argv, fetch)


if __name__ == '__main__':
    unittest.main()
