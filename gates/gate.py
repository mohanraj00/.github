#!/usr/bin/env python3
"""The shared PR gates: cross-review, security-review and acceptance, the classify step of ci-ok, and the gate job of the Claude review.

The caller workflow runs this file from the base branch of the project, through the composite action in this folder.
It never checks out or runs PR code. It reads the PR from the GitHub API and the project config from the base checkout.
It uses the standard library only, and it calls no host other than the GitHub API.
"""
import argparse
import base64
from datetime import datetime, timezone
from fnmatch import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_review import FAILED, MARKER as CLAUDE_MARKER, REQUESTS as CLAUDE_REQUESTS  # noqa: E402

API = 'https://api.github.com'
# GitHub lists at most 3000 files for a PR. A longer list is security relevant and level 1.
PAGES = 30
# The compare API lists at most 300 files.
COMPARE_FILES = 300
FORK_LINES = 400
CHECKS = ['cross-review', 'security-review', 'acceptance', 'classify', 'claude-review']
# The standard allows at most this number of reviews of one kind for each PR.
CLAUDE_CAP = 3

# The Codex GitHub integration posts as this app. A user cannot post as an app.
CODEX = 'chatgpt-codex-connector[bot]'
# The workflow token posts as this user. A fork PR cannot post as this user, but a writer can. Thus the gates trust writers.
BOT = 'github-actions[bot]'
# A writer can post as BOT from a workflow on a branch. An issue_comment run always uses the workflow of the default branch,
# so the gates accept a Claude marker only from an issue_comment run of this workflow.
CLAUDE_WORKFLOW = '.github/workflows/claude-review.yml'
# A project keeps exact copies of these files of UPSTREAM, at the SHA that its gates action pins.
# The paths come from "Adopt the gates in a project" in README.md. The workflow copy differs only in the pin.
UPSTREAM = 'mohanraj00/.github'
COPIES = {'docs/STANDARD.md': 'docs/STANDARD.md', 'tools/claude_review.py': 'gates/claude_review.py',
          CLAUDE_WORKFLOW: 'docs/claude-review.yml'}
PIN_FILE = '.github/workflows/gates.yml'
PIN = re.compile(rb'(mohanraj00/\.github/gates@)([0-9a-f]{40})(?![0-9A-Za-z])')
# The pr-policy form is the record of the aavanam and entelos gates before this action.
RECORD = re.compile(r'<!-- (?:gates|pr-policy):approval label=(\S+) head=([0-9a-f]{40}) base=(\S+) -->')
STATE = re.compile(r'<!-- codex-review-state:v1 (.*?) -->')
# Codex names the commit with 10 hex digits. A shorter prefix is easier to match with a different commit.
REVIEWED = re.compile(r'\*\*Reviewed commit:\*\* `([0-9a-f]{10,40})`')
CLEAN = re.compile(r'^Security review completed\. No security issues were found in this pull request\.$', re.M)
BRANCH = re.compile(r'^(claude|codex|agy)/')
# The completion time of the code review in the Codex summary table. The summary changes in place, so its updated_at
# time can come from a later security review.
CLAUDE_FAILED = re.compile(re.escape(FAILED) + r'\n\nHead: ([0-9a-f]{40})$')
CODE_TIME = re.compile(r'\*\*Code Review\*\* \|[^\n]*?<relative-time datetime="([^"]+)">')
# A label that an admin added in the last FRESH seconds can have a record that a parallel run did not post yet.
# A run waits at most WAIT seconds for that record, and it does not remove that label.
FRESH = 300
WAIT = 120

# Each check binds an approval record to these labels when the owner adds the label.
BIND = {
    'acceptance': ('accepted', 'ready-for-review'),
    'security-review': ('security-reviewed',),
    'cross-review': ('cross-reviewed',),
    'classify': (),
    'claude-review': (),
}


def anywhere(patterns):
    """Return each pattern at the root and at any directory depth."""
    return [x for p in patterns for x in (p, '*/' + p)]


# fnmatch lets * match /, so a pattern that ends in /* also matches deeper paths.
MANIFESTS = ['go.mod', 'go.sum', 'go.work', 'go.work.sum', 'package.json', 'package-lock.json', 'npm-shrinkwrap.json',
             'pnpm-lock.yaml', 'yarn.lock', 'bun.lockb', 'pyproject.toml', 'uv.lock', 'poetry.lock', 'Pipfile',
             'Pipfile.lock', 'requirements*.txt', 'setup.py', 'setup.cfg', 'Cargo.toml', 'Cargo.lock', 'Gemfile',
             'Gemfile.lock', 'Chart.lock', 'flake.lock']
AGENT_FILES = ['AGENTS.md', 'CLAUDE.md', 'GEMINI.md', 'SKILL.md']
# The shared categories of the security review trigger. The project config adds its sources and boundary files.
SHARED_SECURITY = (['.github/*', 'CONSTITUTION.md', 'docs/STANDARD.md', 'docs/REVIEW.md', 'SECURITY.md']
                   + anywhere(AGENT_FILES) + anywhere(['.agents/*', '.claude/*', '.codex/*', '.gemini/*'])
                   + anywhere(MANIFESTS))
# The level 1 paths in all projects. The project config adds its gate tools and its added level 1 paths.
SHARED_LEVEL1 = (['.github/*', 'CONSTITUTION.md', 'docs/STANDARD.md']
                 + anywhere(['AGENTS.md', 'CLAUDE.md', 'GEMINI.md']) + anywhere(['.agents/*', '.claude/*', '.codex/*'])
                 + anywhere(MANIFESTS))
CONFIG_KEYS = ['code', 'security_sources', 'not_security', 'boundaries', 'level1', 'generated']


class GateError(Exception):
    """A format or data problem. The gate fails closed."""


def show(text):
    """Return text with control characters escaped, so that a file name cannot write a workflow command."""
    return json.dumps(str(text))[1:-1]


def load_config(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as error:
        raise GateError('Cannot read the project config %s on the base branch: %s' % (path, error))
    if not isinstance(data, dict) or data.get('version') != 1:
        raise GateError('The project config %s must be a JSON object with "version": 1.' % path)
    unknown = sorted(set(data) - set(CONFIG_KEYS) - {'version'})
    if unknown:
        raise GateError('The project config %s has unknown keys: %s.' % (path, ', '.join(unknown)))
    config = {}
    for key in CONFIG_KEYS:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            raise GateError('The key "%s" of the project config must be a list of path patterns.' % key)
        config[key] = value
    return config


def match(path, patterns):
    return any(fnmatch(path, x) for x in patterns)


def classify(config, paths):
    """Return the listed paths of each kind. paths holds both ends of each move."""
    security = [x for x in paths if match(x, SHARED_SECURITY) or match(x, config['boundaries'])
                or (match(x, config['security_sources']) and not match(x, config['not_security']))]
    level1 = [x for x in paths if match(x, SHARED_LEVEL1) or match(x, config['level1'])]
    code = [x for x in paths if match(x, config['code'])]
    other = [x for x in paths if x not in security and x not in level1 and x not in code]
    return {'security': security, 'level1': level1, 'code': code,
            'docs_only': bool(paths) and len(other) == len(paths)}


class GitHub:
    """The GitHub API calls of the gates. Each error stops the gate."""

    def __init__(self, repo, number, token):
        self.repo, self.number, self.token = repo, number, token

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(API + path, method=method, data=data, headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b'null')

    def pages(self, path):
        """Return (items, complete)."""
        items = []
        for page in range(1, PAGES + 1):
            batch = self.call('GET', '%s%sper_page=100&page=%d' % (path, '&' if '?' in path else '?', page))
            if not isinstance(batch, list):
                raise GateError('The GitHub API did not give a list for %s.' % path)
            items += batch
            if len(batch) < 100:
                return items, True
        return items, False

    def all_pages(self, path):
        items, complete = self.pages(path)
        if not complete:
            raise GateError('The PR has more items than the gate reads at %s.' % path)
        return items

    def graphql(self, query, **variables):
        data = self.call('POST', '/graphql', {'query': query, 'variables': variables})
        if not isinstance(data, dict) or data.get('errors') or not isinstance(data.get('data'), dict):
            raise GateError('The GitHub GraphQL API gave an error: %s' % show((data or {}).get('errors')))
        return data['data']

    def pr(self):
        return self.call('GET', '/repos/%s/pulls/%d' % (self.repo, self.number))

    def files(self):
        """Return (files, complete). Each file is {path, previous, lines}."""
        items, complete = self.pages('/repos/%s/pulls/%d/files' % (self.repo, self.number))
        return [{'path': x['filename'], 'previous': x.get('previous_filename'),
                 'lines': x.get('additions', 0) + x.get('deletions', 0)} for x in items], complete

    def comments(self):
        return self.all_pages('/repos/%s/issues/%d/comments' % (self.repo, self.number))

    def base_changed_at(self):
        events = self.all_pages('/repos/%s/issues/%d/events' % (self.repo, self.number))
        return max([''] + [x['created_at'] for x in events if x.get('event') == 'base_ref_changed'])

    def linked_issues(self):
        owner, name = self.repo.split('/', 1)
        data = self.graphql('''query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    closingIssuesReferences(first: 10) { nodes { number state repository { nameWithOwner }
      labels(first: 100) { nodes { name } } } } } } }''', owner=owner, name=name, number=self.number)
        nodes = data['repository']['pullRequest']['closingIssuesReferences']['nodes']
        return [{'number': x['number'], 'open': x['state'] == 'OPEN', 'repo': x['repository']['nameWithOwner'],
                 'labels': [y['name'] for y in x['labels']['nodes']]} for x in nodes]

    def label_order(self, issue):
        """Return the label names of the issue in the order that they were added."""
        events = self.all_pages('/repos/%s/issues/%d/events' % (self.repo, issue))
        events.sort(key=lambda x: (x['created_at'], x['id']))
        return [x['label']['name'] for x in events if x.get('event') == 'labeled' and x.get('label')]

    def open_codex_threads(self):
        owner, name = self.repo.split('/', 1)
        data = self.graphql('''query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100) { pageInfo { hasNextPage }
      nodes { isResolved comments(first: 1) { nodes { author { login } } } } } } } }''',
                            owner=owner, name=name, number=self.number)
        threads = data['repository']['pullRequest']['reviewThreads']
        if threads['pageInfo']['hasNextPage']:
            raise GateError('The PR has more review threads than the gate reads.')
        return sum(1 for x in threads['nodes'] if not x['isResolved'] and x['comments']['nodes']
                   and (x['comments']['nodes'][0]['author'] or {}).get('login') in (CODEX, CODEX[:-5]))

    def reviews(self):
        return self.all_pages('/repos/%s/pulls/%d/reviews' % (self.repo, self.number))

    def is_admin(self, login):
        """Only a user with the admin role can approve. A triage or write user can add labels, but not approve."""
        data = self.call('GET', '/repos/%s/collaborators/%s/permission' % (self.repo, urllib.parse.quote(login, safe='')))
        return data.get('permission') == 'admin'

    def last_labeled(self, issue, label, kind='labeled'):
        """Return (login, time) of the last event of this kind (labeled or unlabeled) for the label on the issue or PR, or None."""
        events = self.all_pages('/repos/%s/issues/%d/events' % (self.repo, issue))
        events.sort(key=lambda x: (x['created_at'], x['id']))
        found = [x for x in events if x.get('event') == kind and (x.get('label') or {}).get('name') == label]
        return ((found[-1].get('actor') or {}).get('login'), found[-1]['created_at']) if found else None

    def now(self):
        return datetime.now(timezone.utc)

    def sleep(self, seconds):
        time.sleep(seconds)

    def resolve(self, commit):
        """Return the full SHA of a commit prefix, or None if the prefix is unknown or names more than one object."""
        try:
            data = self.call('GET', '/repos/%s/commits/%s' % (self.repo, commit))
        except urllib.error.HTTPError as error:
            if error.code in (404, 409, 422):
                return None
            raise
        sha = (data or {}).get('sha') or ''
        return sha if re.fullmatch(r'[0-9a-f]{40}', sha) and sha.startswith(commit) else None

    def workflow_run(self, run_id):
        """Return the workflow run, or None if it does not exist."""
        try:
            data = self.call('GET', '/repos/%s/actions/runs/%s' % (self.repo, run_id))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    def merge_base(self, base, head):
        """Return the current merge base of the base branch and the head, or None."""
        data = self.call('GET', '/repos/%s/compare/%s...%s' % (self.repo, urllib.parse.quote(base, safe='/'), head))
        sha = ((data or {}).get('merge_base_commit') or {}).get('sha') or ''
        return sha if re.fullmatch(r'[0-9a-f]{40}', sha) else None

    def compare(self, old, new):
        """Return (old is an ancestor of new, changed paths, complete)."""
        data = self.call('GET', '/repos/%s/compare/%s...%s' % (self.repo, old, new))
        files = data.get('files') or []
        paths = [p for x in files for p in [x['filename']] + ([x['previous_filename']] if x.get('previous_filename') else [])]
        return data.get('status') in ('ahead', 'identical'), paths, len(files) < COMPARE_FILES

    def content(self, repo, path, ref):
        """Return (blob SHA, bytes) of the file at the commit, or None if the file does not exist."""
        try:
            data = self.call('GET', '/repos/%s/contents/%s?ref=%s' % (repo, urllib.parse.quote(path), ref))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get('type') != 'file' or data.get('encoding') != 'base64':
            raise GateError('The GitHub API did not give the content of %s.' % show(path))
        return data['sha'], base64.b64decode(data['content'])

    def on_main(self, repo, commit):
        """Return True if the commit is on the main branch of the repository."""
        data = self.call('GET', '/repos/%s/compare/%s...main' % (repo, commit))
        return data.get('status') in ('ahead', 'identical')

    def post_comment(self, body):
        self.call('POST', '/repos/%s/issues/%d/comments' % (self.repo, self.number), {'body': body})

    def remove_label(self, label):
        try:
            self.call('DELETE', '/repos/%s/issues/%d/labels/%s' % (self.repo, self.number, urllib.parse.quote(label, safe='')))
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise


def record_body(label, head, base):
    return ('<!-- gates:approval label=%s head=%s base=%s -->\nThe `%s` label applies to head %s on base `%s` only. '
            'A new head or base needs the label again.' % (label, head, base, label, head[:12], base))


def read_records(comments):
    """Return [(label, head, base, time)] from the approval records of the workflow bot, oldest first."""
    return [(label, head, base, item.get('created_at') or '') for item in comments
            if (item.get('user') or {}).get('login') == BOT
            for label, head, base in RECORD.findall(item.get('body') or '')]


def stamp(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def after(item, changed, field='created_at'):
    return bool(item.get(field)) and (not changed or stamp(item[field]) > stamp(changed))


def bound_heads(gh, label, labels, base):
    """Return (heads, pending) for a label on the PR. heads holds the heads of its records on this base, oldest first.
    A record counts only if an admin added the label last, and only if the record is newer than that labeled event.
    Thus an old record does not count after a user removes the label and adds it again.
    pending is true if an admin added the label recently and its record is not posted yet. Then do not remove the label."""
    if label not in labels:
        return [], False
    last = gh.last_labeled(gh.number, label)
    if not last or not gh.is_admin(last[0] or ''):
        return [], False
    waited = 0
    while True:
        heads = [h for name, h, b, t in read_records(gh.comments()) if name == label and b == base and t and stamp(t) >= stamp(last[1])]
        fresh = (gh.now() - stamp(last[1])).total_seconds() < FRESH
        if heads or not fresh or waited >= WAIT:
            return heads, not heads and fresh
        gh.sleep(10)
        waited += 10


def blocks(gh, issue, label, labels):
    """Return true if a blocking label applies. Only an admin can clear it: after a removal by another user, it still applies."""
    if label in labels:
        return True
    removed = gh.last_labeled(issue, label, 'unlabeled')
    return bool(removed) and not gh.is_admin(removed[0] or '')


def label_state(gh, label, labels, current):
    """Return (valid, stale). The label is valid only with a record for the current head and base. A stale label is removed."""
    heads, pending = bound_heads(gh, label, labels, current[1])
    valid = current[0] in heads
    return valid, label in labels and not valid and not pending


def codex_security(comments, changed):
    """Return ([(commit, clean)] oldest first, [format errors]) from the Codex security receipts after the last base change."""
    results, errors = [], []
    for item in comments:
        body = item.get('body') or ''
        if (item.get('user') or {}).get('login') != CODEX or 'Codex Security Review' not in body or not after(item, changed):
            continue
        commit = REVIEWED.search(body)
        if commit:
            results.append((commit.group(1), bool(CLEAN.search(body))))
        else:
            # An unreadable receipt stays in the order, so an older clean receipt cannot pass after it.
            results.append((None, False))
            errors.append('A Codex security receipt has no "Reviewed commit" line. The receipt format changed.')
    return results, errors


def codex_code(comments, changed):
    """Return (the code_review data of the latest Codex summary after the last base change, or None, [format errors])."""
    latest, errors = None, []
    for item in comments:
        if (item.get('user') or {}).get('login') != CODEX or not after(item, changed, 'updated_at'):
            continue
        found = STATE.search(item.get('body') or '')
        if not found:
            continue
        try:
            data = json.loads(found.group(1))
            review = data['code_review']
            if (not isinstance(review, dict) or not isinstance(review.get('commit_sha'), (str, type(None)))
                    or not isinstance(review.get('status'), (str, type(None)))
                    or not isinstance(review.get('has_any_findings'), (bool, type(None)))):
                raise ValueError
            if changed and review.get('commit_sha'):
                # The code review must be newer than the base change, not only the summary.
                done = CODE_TIME.search(item['body'])
                if not done:
                    raise ValueError
                if stamp(done.group(1)) <= stamp(changed):
                    review = dict(review, commit_sha=None, status=None)
        except (ValueError, KeyError, TypeError):
            # An unreadable summary stays in the order, so an older summary cannot pass after it.
            review = None
            errors.append('The codex-review-state:v1 data or the code review time has an unknown format. The format changed.')
        if latest is None or stamp(item['updated_at']) >= stamp(latest[0]):
            latest = (item['updated_at'], review)
    return (latest[1] if latest else None), errors


def trusted_run(gh, pr, run_id, item):
    """Return True if a completed issue_comment run of the Claude workflow on the default branch posted the comment.
    The comment time must be in the time of the run."""
    run = gh.workflow_run(run_id)
    default = ((pr.get('base') or {}).get('repo') or {}).get('default_branch')
    if not run or not default or run.get('path') != CLAUDE_WORKFLOW or run.get('event') != 'issue_comment':
        return False
    if run.get('head_branch') != default or run.get('status') != 'completed':
        return False
    start, end, posted = run.get('run_started_at'), run.get('updated_at'), item.get('created_at')
    return bool(start and end and posted) and stamp(start) <= stamp(posted) <= stamp(end)


def run_claims(comments):
    """Return (the comments of the workflow bot, {run ID: the number of markers that name it})."""
    bot = [item for item in comments if (item.get('user') or {}).get('login') == BOT]
    claims = {}
    for item in bot:
        for marker in CLAUDE_MARKER.findall(item.get('body') or ''):
            claims[marker[4]] = claims.get(marker[4], 0) + 1
    return bot, claims


def claude_results(gh, pr, comments, changed, kind):
    """Return [(head, findings)] oldest first from the Claude workflow comments of this kind after the last base change.
    A marker counts only if its run is trusted, no other comment names the same run, and its base is the current merge base.
    The merge base does not change when a commit merges into the base branch, so an open PR keeps its review."""
    bot, claims = run_claims(comments)
    results, current = [], []
    for item in bot:
        if not after(item, changed):
            continue
        body = item.get('body') or ''
        for found_kind, head, base, findings, run in CLAUDE_MARKER.findall(body):
            if found_kind != kind or claims[run] != 1:
                continue
            if not current:
                current.append(gh.merge_base(pr['base']['ref'], pr['head']['sha']))
            if base == current[0] and trusted_run(gh, pr, run, item):
                results.append((head, findings))
        # A failed review names no kind. It stops an older clean review of the same head for each kind.
        failed = CLAUDE_FAILED.match(body)
        if failed:
            results.append((failed.group(1), 'unknown'))
    return results


def builder_of(gh, pr):
    """Return (builder or None, source). The first agent label of the linked issue wins, then the branch prefix."""
    for issue in gh.linked_issues():
        if issue['repo'] != gh.repo:
            continue
        present = set(issue['labels'])
        for name in gh.label_order(issue['number']):
            if name.startswith('agent:') and name in present:
                return name[6:], 'the label %s of issue #%d' % (name, issue['number'])
        break
    prefix = BRANCH.match(pr['head']['ref'] or '')
    if prefix:
        return prefix.group(1), 'the branch prefix %s/' % prefix.group(1)
    return None, 'no agent label and no harness branch prefix'


def cross_review(gh, pr, comments, changed):
    labels = {x['name'] for x in pr['labels']}
    head, base = pr['head']['sha'], pr['base']['ref']
    builder, source = builder_of(gh, pr)
    valid, stale = label_state(gh, 'cross-reviewed', labels, (head, base))
    lines, drop = [], ['cross-reviewed'] if stale else []
    if builder in (None, 'claude'):
        name = 'No harness' if builder is None else 'Claude Code'
        lines.append('Builder: %s, from %s. The code reviewer is the Codex app.' % (name, source))
        review, errors = codex_code(comments, changed)
        lines += errors
        if review and review['commit_sha'] == head and review['status'] == 'completed':
            found = review.get('has_any_findings')
            if found is False:
                return True, lines + ['The Codex code review of head %s found no issues.' % head[:12]], drop
            # A null findings value gets the same thread rule as true, so an open Codex thread always fails.
            left = gh.open_codex_threads()
            if not left:
                if found is True:
                    return True, lines + ['The Codex code review of head %s has findings, and each Codex thread is resolved.' % head[:12]], drop
                return True, lines + ['The Codex code review of head %s gives no findings value, and no Codex thread is open.' % head[:12]], drop
            lines.append('The Codex code review of head %s has %d unresolved threads. Fix each finding, or write the reason and resolve the thread.' % (head[:12], left))
        else:
            lines.append('No completed Codex code review of head %s. Post "@codex review" with "Head: %s" on the next line.' % (head[:12], head[:7]))
    elif builder in ('codex', 'agy'):
        lines.append('Builder: %s, from %s. The code reviewer is the Claude workflow.' % (builder, source))
        results = [findings for commit, findings in claude_results(gh, pr, comments, changed, 'code') if commit == head]
        if results and results[-1] == '0':
            return True, lines + ['The Claude code review of head %s found no issues.' % head[:12]], drop
        if results:
            lines.append('The latest Claude code review of head %s gives %s findings. Fix them and ask again.' % (head[:12], results[-1]))
        else:
            lines.append('No Claude code review of head %s. Ask for it with the request text in docs/REVIEW.md.' % head[:12])
    else:
        lines.append('Builder: unknown value "%s", from %s.' % (show(builder), source))
    if valid:
        return True, lines + ['The cross-reviewed label applies to head %s.' % head[:12]], []
    if stale:
        lines.append('The cross-reviewed label has no approval record for head %s on base %s. Review the current change, then add the label again.' % (head[:12], base))
    lines.append('The cross-review check needs a clean review of head %s from the other harness, or the cross-reviewed label for an agy or owner review.' % head[:12])
    return False, lines, drop


def blob_sha(data):
    """Return the Git blob SHA of the bytes."""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def pin_change(before, after):
    """Return the new SHA if the file changes only the SHA in its pin lines, else None. Each file is (blob SHA, bytes) or None."""
    if not before or not after:
        return None
    old, new = before[1].splitlines(True), after[1].splitlines(True)
    if len(old) != len(new):
        return None
    shas = set()
    for a, b in zip(old, new):
        if a != b:
            if PIN.sub(rb'\1', a) != PIN.sub(rb'\1', b):
                return None
            shas |= {x[1].decode() for x in PIN.findall(b)}
    return shas.pop() if len(shas) == 1 else None


def copy_check(gh, pr, listed):
    """Return (proved lines, None) if each listed path is a proved copy or a proved pin change, else (None, reason).
    The old pin comes from the base branch. The copies must agree with UPSTREAM at the new pin, by the Git blob SHA."""
    head, base = pr['head']['sha'], pr['base']['sha']
    found = gh.content(gh.repo, PIN_FILE, base)
    pins = {x[1].decode() for x in PIN.findall(found[1])} if found else set()
    if len(pins) != 1:
        return None, 'The base branch has no single pin of the gates action in %s.' % PIN_FILE
    changes = {}
    for path in listed:
        if path not in COPIES:
            changes[path] = pin_change(gh.content(gh.repo, path, base), gh.content(gh.repo, path, head))
            if not changes[path]:
                return None, '%s is not a copy or a pin change.' % show(path)
    pin = changes.get(PIN_FILE, pins.pop())
    if set(changes.values()) - {pin}:
        return None, 'The pin changes do not all name %s.' % pin[:12]
    if not gh.on_main(UPSTREAM, pin):
        return None, 'The pin %s is not on main of %s.' % (pin[:12], UPSTREAM)
    proved = ['%s: the pin changes to %s, on main of %s.' % (show(x), pin[:12], UPSTREAM) for x in changes]
    for path in listed:
        if path in COPIES:
            copy, source = gh.content(gh.repo, path, head), gh.content(UPSTREAM, COPIES[path], pin)
            if not copy or not source:
                return None, 'Cannot read %s or %s of %s at %s.' % (show(path), COPIES[path], UPSTREAM, pin[:12])
            expected = blob_sha(PIN.sub(rb'\g<1>' + pin.encode(), source[1])) if path == CLAUDE_WORKFLOW else source[0]
            if copy[0] != expected:
                return None, '%s is not an exact copy of %s of %s at %s.' % (show(path), COPIES[path], UPSTREAM, pin[:12])
            proved.append('%s: an exact copy of %s of %s at %s.' % (show(path), COPIES[path], UPSTREAM, pin[:12]))
    return proved, None


def security_review(gh, config, pr, paths, complete, comments, changed):
    labels = {x['name'] for x in pr['labels']}
    head, base = pr['head']['sha'], pr['base']['ref']
    forced = blocks(gh, gh.number, 'security-needed', labels)
    listed = classify(config, paths)['security']
    if complete and not listed and not forced:
        return True, ['No listed path changed.'], []
    lines = []
    if complete and not forced:
        # An exact copy of the gate files and a pin change need no new review. The review of UPSTREAM covers them.
        try:
            proved, reason = copy_check(gh, pr, listed)
        except (OSError, GateError, KeyError, TypeError, ValueError) as error:
            proved, reason = None, 'The copy check failed closed (%s: %s).' % (type(error).__name__, show(error))
        if proved:
            return True, ['Each listed path is a proved copy or pin change. No review is necessary.'] + proved, []
        lines.append('Copy check: ' + reason)
    if not complete:
        lines.append('The PR has more files than GitHub lists, so the change is security relevant.')
    if listed:
        lines.append('Listed paths: ' + ', '.join(show(x) for x in listed))
    if forced:
        lines.append('The security-needed label requires a security review of the current head.')

    def covers(commit):
        """A review of an earlier commit stays valid if the later commits touch no listed path.
        A Codex receipt names a prefix. The gate resolves it to one full SHA, so a commit with the same prefix does not match."""
        full = commit if len(commit) == 40 else gh.resolve(commit)
        if not full:
            return False
        if full == head:
            return True
        if forced:
            return False
        ancestor, later, complete = gh.compare(full, head)
        return ancestor and complete and not classify(config, later)['security']

    def name(commit):
        return 'head %s' % head[:12] if head.startswith(commit) else 'commit %s, and the later commits touch no listed path' % commit[:12]

    results, errors = codex_security(comments, changed)
    lines += errors
    if results and results[-1][0] and results[-1][1] and covers(results[-1][0]):
        commit = results[-1][0]
        return True, lines + ['The Codex security review of %s found no security issues.' % name(commit)], []
    # A Claude security result is advisory. It never passes the gate. The owner reads it and adds the security-reviewed label.
    # A Codex result with open findings stops the advisory line too.
    codex_open = bool(results) and bool(results[-1][0]) and not results[-1][1]
    claude = claude_results(gh, pr, comments, changed, 'security')
    if not codex_open and claude and claude[-1][1] == '0' and covers(claude[-1][0]):
        lines.append('The Claude security review of %s found no security issues. This result is advisory. '
                     'The owner adds the security-reviewed label after the owner reads the review.' % name(claude[-1][0]))
    drop = []
    if 'security-reviewed' in labels:
        bound, pending = bound_heads(gh, 'security-reviewed', labels, base)
        if bound and covers(bound[-1]):
            return True, lines + ['The security-reviewed label applies to %s.' % name(bound[-1])], []
        lines.append('The security-reviewed label has no approval record that covers head %s on base %s. Review the current change, then add the label again.' % (head[:12], base))
        drop = [] if pending else ['security-reviewed']
    if results and results[-1][0] and not results[-1][1]:
        lines.append('The latest Codex security review has open findings. Fix them and ask again, or write the answer on the PR and ask the owner to add the security-reviewed label.')
    lines.append('Post "@codex security review" as a separate comment. The check passes when the Codex security review of head %s finds no security issues, or when the owner adds the security-reviewed label.' % head[:12])
    return False, lines, drop


def sections(text):
    """Return {heading: text} for each "## " heading."""
    found, heading = {}, None
    for line in text.replace('\r\n', '\n').split('\n'):
        if line.startswith('## '):
            heading = line[3:].strip()
            found[heading] = []
        elif heading is not None:
            found[heading].append(line)
    return {k: '\n'.join(v).strip() for k, v in found.items()}


def template_errors(template, body):
    """Return the template sections that the PR description does not fill."""
    want, have = sections(template), sections(body or '')
    return [k for k, v in want.items() if not have.get(k) or have[k] == v]


def approved(gh, head):
    """Return True if an admin approved the head with a GitHub review after the last base change.
    The latest review of each user decides. A comment review does not change an approval, as in GitHub.
    A dismissed review has the state DISMISSED, so it does not count."""
    changed = gh.base_changed_at()
    latest = {}
    for item in sorted(gh.reviews(), key=lambda x: (x.get('submitted_at') or '', x.get('id') or 0)):
        login = (item.get('user') or {}).get('login')
        if login and item.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            latest[login] = item
    return any(x['state'] == 'APPROVED' and x.get('commit_id') == head and after(x, changed, 'submitted_at')
               and gh.is_admin(login) for login, x in latest.items())


def from_fork(gh, pr):
    return (pr['head'].get('repo') or {}).get('full_name') != gh.repo


def fork_line(pr):
    return 'This PR comes from the fork %s.' % show((pr['head'].get('repo') or {}).get('full_name') or 'that GitHub deleted')


def stage2(gh, labels, head, base):
    """Return (approved, stale) for stage 2 of "Fork PRs": the owner approval of the current head and base.
    An admin review with the state APPROVED for the head, or the ready-for-review label with its approval record, is
    an approval. stale is true if the label has no record for the current head and base, so the label must go."""
    ready, stale = label_state(gh, 'ready-for-review', labels, (head, base))
    return ready or approved(gh, head), stale


def acceptance(gh, config, pr, files, complete, template):
    labels = {x['name'] for x in pr['labels']}
    head, base = pr['head']['sha'], pr['base']['ref']
    fork = from_fork(gh, pr)
    paths = [p for x in files for p in [x['path']] + ([x['previous']] if x['previous'] else [])]
    level1 = classify(config, paths)['level1']
    lines, problems, drop = [], [], []
    if fork:
        lines.append(fork_line(pr) + ' Stage 1 of "Fork PRs" applies.')
        issues = gh.linked_issues()
        accepted = [x for x in issues if x['open'] and x['repo'] == gh.repo and 'accepted' in x['labels']
                    and not blocks(gh, x['number'], 'decision', set(x['labels']))]
        if not any(gh.is_admin((gh.last_labeled(x['number'], 'accepted') or ('', ''))[0] or '') for x in accepted):
            problems.append('Link an open issue that has the label accepted and not the label decision. An admin must add the accepted label.')
        lines_changed = sum(x['lines'] for x in files if not match(x['path'], config['generated']))
        if not complete:
            problems.append('The PR has more files than GitHub lists.')
        elif lines_changed > FORK_LINES:
            problems.append('The PR changes %d lines without generated files. The limit is about %d.' % (lines_changed, FORK_LINES))
        valid, stale = label_state(gh, 'accepted', labels, (head, base))
        if stale:
            drop.append('accepted')
        if (level1 or not complete) and not valid:
            problems.append('The PR changes protected paths. The owner must approve them with the accepted label on the PR.')
        missing = template_errors(template, pr.get('body'))
        if missing:
            problems.append('Fill these sections of the PR template: ' + ', '.join(missing) + '.')
        ready, stale = stage2(gh, labels, head, base)
        if stale:
            drop.append('ready-for-review')
            lines.append('A push after the owner review removes the ready-for-review label.')
        # Stage 2: the owner approves the current head. A review does not start this gate, so the fail text gives the rerun step.
        if not ready:
            problems.append('Stage 2 needs an owner approval of head %s on base %s. The owner reads the diff, then adds the '
                            'ready-for-review label, or approves the PR and runs the failed acceptance job again, for example '
                            'with gh run rerun <run-id> --failed.' % (head[:12], show(base)))
    owner = []
    if level1:
        owner.append('level 1 paths changed: ' + ', '.join(show(x) for x in level1))
    if not complete:
        owner.append('the PR has more files than GitHub lists')
    if blocks(gh, gh.number, 'owner-needed', labels):
        owner.append('the PR has the owner-needed label')
    if owner:
        problems.append('This PR is level 1 (%s). The owner merges this PR with the admin bypass, so auto-merge cannot merge it.' % '; '.join(owner))
    if problems:
        return False, lines + problems, drop
    return True, lines + ['Level 0: no level 1 path, no owner-needed label%s.' % (', and the fork checks and the owner approval pass' if fork else '')], drop


def claude_markers(gh, pr, comments, kind):
    """Return [(head, base, comment)] oldest first for each trusted Claude marker of this kind on the PR, for each base.
    A marker is trusted if no other comment names its run, and its run is trusted."""
    bot, claims = run_claims(comments)
    return [(head, base, item) for item in bot for found, head, base, _, run in CLAUDE_MARKER.findall(item.get('body') or '')
            if found == kind and claims[run] == 1 and trusted_run(gh, pr, run, item)]


def claude_request(gh, pr, comments, changed, kind):
    """The gate job of the Claude review. It runs before the review job, and it has no secret.
    For a fork PR, it needs the stage 2 approval of acceptance. It applies the request rules of the standard: one review
    of each kind for each head and merge base, and at most CLAUDE_CAP reviews of each kind for each PR."""
    labels = {x['name'] for x in pr['labels']}
    head, base = pr['head']['sha'], pr['base']['ref']
    lines, problems = [], []
    if from_fork(gh, pr):
        lines.append(fork_line(pr))
        if not stage2(gh, labels, head, base)[0]:
            problems.append('Stage 2 needs an owner approval of head %s on base %s before a review. The owner reads the diff, '
                            'then adds the ready-for-review label or approves the PR. Then ask for the review again.' % (head[:12], show(base)))
    markers = claude_markers(gh, pr, comments, kind)
    if len(markers) >= CLAUDE_CAP:
        problems.append('The PR has %d trusted Claude %s reviews. The cap is %d for each PR. Examine the approach, then '
                        'report to the owner.' % (len(markers), kind, CLAUDE_CAP))
    current = gh.merge_base(base, head)
    if not current:
        problems.append('The gate cannot get the merge base of head %s and base %s.' % (head[:12], show(base)))
    elif any(h == head and b == current and after(item, changed) for h, b, item in markers):
        problems.append('A trusted Claude %s review of head %s on the current merge base is on the PR. Ask one time for '
                        'each head.' % (kind, head[:12]))
    if problems:
        return False, lines + problems, {}
    # The review job reviews only this head on this base and merge base. Thus the approval binds the reviewed code.
    return True, lines + ['The Claude %s review of head %s can start.' % (kind, head[:12])], {
        'head': head, 'base': base, 'merge_base': current}


def request_kind(event):
    """Return the kind of the exact request text in the comment. GitHub compares the texts with no regard to letter case."""
    body = ((event.get('comment') or {}).get('body') or '').lower()
    kinds = [kind for kind, text in CLAUDE_REQUESTS.items() if text.lower() == body]
    if not kinds:
        raise GateError('The comment is not a Claude review request.')
    return kinds[0]


def run(check, gh, event, config, template=''):
    """Return (passed, lines, outputs). This function makes all of its network calls through gh."""
    if check == 'classify':
        files, complete = gh.files()
        paths = [p for x in files for p in [x['path']] + ([x['previous']] if x['previous'] else [])]
        docs = complete and classify(config, paths)['docs_only']
        return True, ['Docs-only change.' if docs else 'The project jobs run.'], {'docs_only': str(docs).lower(), 'code': str(not docs).lower()}
    label = (event.get('label') or {}).get('name')
    # Only an admin label makes a record. A label from another user has no record, so the gate removes it.
    sender = (event.get('sender') or {}).get('login') or ''
    if event.get('action') == 'labeled' and label in BIND[check] and sender and gh.is_admin(sender):
        # The labeled event holds the head and base at the time of the label, not at the time of this run.
        old = event['pull_request']
        head, base = old['head']['sha'], old['base']['ref']
        gh.post_comment(record_body(label, head, base))
    comments = gh.comments()
    pr = gh.pr()
    if check == 'claude-review':
        # The gate job has read permissions only, so it never drops a label.
        return claude_request(gh, pr, comments, gh.base_changed_at(), request_kind(event))
    if check == 'acceptance':
        files, complete = gh.files()
        passed, lines, drop = acceptance(gh, config, pr, files, complete, template)
    else:
        changed = gh.base_changed_at()
        if check == 'cross-review':
            passed, lines, drop = cross_review(gh, pr, comments, changed)
        else:
            files, complete = gh.files()
            paths = [p for x in files for p in [x['path']] + ([x['previous']] if x['previous'] else [])]
            passed, lines, drop = security_review(gh, config, pr, paths, complete, comments, changed)
    for name in drop:
        gh.remove_label(name)
    return passed, lines, {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=CHECKS)
    args = parser.parse_args(argv)
    outputs = {}
    try:
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        workspace = Path(os.environ.get('GITHUB_WORKSPACE') or '.')
        # The gate job of the Claude review has no checkout, and it reads no project config.
        config = None if args.check == 'claude-review' else load_config(workspace / '.github' / 'gates.json')
        if args.check == 'claude-review':
            issue = event.get('issue') or {}
            if 'comment' not in event or not issue.get('pull_request'):
                raise GateError('The claude-review check runs only for an issue_comment event on a PR.')
            gh = GitHub(event['repository']['full_name'], int(issue['number']), os.environ['GITHUB_TOKEN'])
            passed, lines, outputs = run(args.check, gh, event, None)
        elif 'pull_request' not in event:
            if args.check != 'classify':
                raise GateError('The %s check runs only for a pull_request_target event.' % args.check)
            passed, lines, outputs = True, ['No PR: the project jobs run.'], {'docs_only': 'false', 'code': 'true'}
        else:
            template = ''
            if args.check == 'acceptance':
                try:
                    template = (workspace / '.github' / 'pull_request_template.md').read_text(encoding='utf-8')
                except (OSError, UnicodeError) as error:
                    raise GateError('Cannot read the PR template on the base branch: %s' % error)
            gh = GitHub(event['repository']['full_name'], int(event['pull_request']['number']), os.environ['GITHUB_TOKEN'])
            passed, lines, outputs = run(args.check, gh, event, config, template)
    except urllib.error.HTTPError as error:
        passed, lines = False, ['The GitHub API gave HTTP %d for %s. The gate fails closed.' % (error.code, show(error.url))]
    except (urllib.error.URLError, OSError) as error:
        passed, lines = False, ['The GitHub API call failed: %s. The gate fails closed.' % show(error)]
    except GateError as error:
        passed, lines = False, [show(error) + ' The gate fails closed.']
    except (KeyError, TypeError, ValueError) as error:
        passed, lines = False, ['The gate got data in an unknown format (%s: %s). The gate fails closed.' % (type(error).__name__, show(error))]
    if args.check == 'claude-review' and not passed:
        # The post job of the Claude review posts this reason. An output value has one line.
        outputs = {'reason': ' '.join(lines).replace('\r', ' ').replace('\n', ' ')}
    if os.environ.get('GITHUB_OUTPUT') and outputs:
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as out:
            for key, value in outputs.items():
                out.write('%s=%s\n' % (key, value))
    print('\n'.join(lines))
    if not passed:
        print('::error title=%s::%s' % (args.check, lines[-1]))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
