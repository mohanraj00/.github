"""Print one table of the review cost of the merged PRs of a repository in a date range.

The gates write a review cost line in a notice with the title review-cost when cross-review, security-review or
acceptance passes. This script reads the notice of the last passed gate run on the head of each merged PR.
It reads the token from GITHUB_TOKEN or GH_TOKEN, else from "gh auth token".

Example: python3 tools/review_cost.py mohanraj00/koodam 2026-11-01 2026-11-30
"""
import argparse
from datetime import date
import json
import os
import re
import statistics
import subprocess
import urllib.parse
import urllib.request

API = 'https://api.github.com'
CHECKS = ['cross-review', 'security-review', 'acceptance']
LINE = re.compile(r'^review-cost v1 codex=(\d+) security=(\d+) claude=(\d+) hours=(\d+(?:\.\d+)?)$')
FIELDS = ['codex', 'security', 'claude', 'hours']
# The search API gives at most 1000 results.
SEARCH_PAGES = 10


def token():
    found = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if found:
        return found
    return subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, check=True).stdout.strip()


def fetcher(secret):
    def fetch(path):
        request = urllib.request.Request(API + path, headers={
            'Authorization': 'Bearer ' + secret, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b'null')
    return fetch


def merged_prs(fetch, repo, since, until):
    """Return the numbers of the PRs merged from since to until, both days included, in number order."""
    query = urllib.parse.quote('repo:%s is:pr is:merged merged:%s..%s' % (repo, since, until))
    numbers = []
    for page in range(1, SEARCH_PAGES + 1):
        data = fetch('/search/issues?q=%s&per_page=100&page=%d' % (query, page))
        if data.get('incomplete_results'):
            raise SystemExit('The search API gave incomplete results. Run the script again.')
        numbers += [x['number'] for x in data['items']]
        if len(data['items']) < 100:
            return sorted(numbers)
    raise SystemExit('The date range has more than %d merged PRs. Use a shorter range.' % (100 * SEARCH_PAGES))


def cost(fetch, repo, number):
    """Return the values of the review cost line of the last passed gate run on the PR head, or None."""
    head = fetch('/repos/%s/pulls/%d' % (repo, number))['head']['sha']
    runs = fetch('/repos/%s/commits/%s/check-runs?per_page=100' % (repo, head))['check_runs']
    runs = [x for x in runs if x['name'] in CHECKS and x.get('conclusion') == 'success' and x.get('completed_at')]
    for item in sorted(runs, key=lambda x: x['completed_at'], reverse=True):
        for note in fetch('/repos/%s/check-runs/%d/annotations?per_page=100' % (repo, item['id'])):
            found = LINE.match(note.get('message') or '') if note.get('title') == 'review-cost' else None
            if found:
                return dict(zip(FIELDS, [int(x) for x in found.groups()[:3]] + [float(found.group(4))]))
    return None


def table(rows):
    """Return the table text. rows holds (number, values or None)."""
    lines = ['| PR | Codex code | Security | Claude | Hours |', '|---|---|---|---|---|']
    for number, values in rows:
        if values:
            lines.append('| #%d | %d | %d | %d | %.1f |' % ((number,) + tuple(values[x] for x in FIELDS)))
        else:
            lines.append('| #%d | no line | no line | no line | no line |' % number)
    found = [values for _, values in rows if values]
    if found:
        sums = [sum(x[key] for x in found) for key in FIELDS[:3]]
        lines.append('| Total (%d PRs with a line) | %d | %d | %d | median %.1f |' % (
            (len(found),) + tuple(sums) + (statistics.median(x['hours'] for x in found),)))
    return '\n'.join(lines)


def main(argv=None, fetch=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('repo', help='The repository, as owner/name.')
    parser.add_argument('since', type=date.fromisoformat, help='The first merge day, as YYYY-MM-DD.')
    parser.add_argument('until', type=date.fromisoformat, help='The last merge day, as YYYY-MM-DD.')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('The repository must be owner/name.')
    if args.since > args.until:
        parser.error('The first day must not be after the last day.')
    fetch = fetch or fetcher(token())
    numbers = merged_prs(fetch, args.repo, args.since.isoformat(), args.until.isoformat())
    print(table([(number, cost(fetch, args.repo, number)) for number in numbers]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
