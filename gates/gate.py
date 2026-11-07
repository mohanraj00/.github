#!/usr/bin/env python3
"""The shared PR gates: cross-review, security-review and acceptance, and the classify step of ci-ok.

The caller workflow runs this file from the base branch of the project, through the composite action in this folder.
It never checks out or runs PR code. It reads the PR from the GitHub API and the project config from the base checkout.
It uses the standard library only, and it calls no host other than the GitHub API.
"""
import argparse
from datetime import datetime, timezone
from fnmatch import fnmatch
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
from claude_review import FAILED, MARKER as CLAUDE_MARKER  # noqa: E402

API = 'https://api.github.com'
# GitHub lists at most 3000 files for a PR. A longer list is security relevant and level 1.
PAGES = 30
# The compare API lists at most 300 files.
COMPARE_FILES = 300
FORK_LINES = 400
CHECKS = ['cross-review', 'security-review', 'acceptance', 'classify']

# The Codex GitHub integration posts as this app. A user cannot post as an app.
CODEX = 'chatgpt-codex-connector[bot]'
# The workflow token posts as this user. A fork PR cannot post as this user, but a writer can. Thus the gates trust writers.
BOT = 'github-actions[bot]'
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

    def is_admin(self, login):
        """Only a user with the admin role can approve. A triage or write user can add labels, but not approve."""
        data = self.call('GET', '/repos/%s/collaborators/%s/permission' % (self.repo, urllib.parse.quote(login, safe='')))
        return data.get('permission') == 'admin'

    def last_labeled(self, issue, label):
        """Return (login, time) of the last labeled event of the label on the issue or PR, or None."""
        events = self.all_pages('/repos/%s/issues/%d/events' % (self.repo, issue))
        events.sort(key=lambda x: (x['created_at'], x['id']))
        found = [x for x in events if x.get('event') == 'labeled' and (x.get('label') or {}).get('name') == label]
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

    def compare(self, old, new):
        """Return (old is an ancestor of new, changed paths, complete)."""
        data = self.call('GET', '/repos/%s/compare/%s...%s' % (self.repo, old, new))
        files = data.get('files') or []
        paths = [p for x in files for p in [x['filename']] + ([x['previous_filename']] if x.get('previous_filename') else [])]
        return data.get('status') in ('ahead', 'identical'), paths, len(files) < COMPARE_FILES

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


def claude_results(comments, changed, kind):
    """Return [(head, findings)] oldest first from the Claude workflow comments of this kind after the last base change."""
    results = []
    for item in comments:
        if (item.get('user') or {}).get('login') != BOT or not after(item, changed):
            continue
        body = item.get('body') or ''
        for found_kind, head, findings in CLAUDE_MARKER.findall(body):
            if found_kind == kind:
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
            if review['has_any_findings'] is False:
                return True, lines + ['The Codex code review of head %s found no issues.' % head[:12]], drop
            if review['has_any_findings'] is True:
                left = gh.open_codex_threads()
                if not left:
                    return True, lines + ['The Codex code review of head %s has findings, and each Codex thread is resolved.' % head[:12]], drop
                lines.append('The Codex code review of head %s has %d unresolved threads. Fix each finding, or write the reason and resolve the thread.' % (head[:12], left))
            else:
                lines.append('The Codex code review of head %s gives no findings value.' % head[:12])
        else:
            lines.append('No completed Codex code review of head %s. Post "@codex review" with "Head: %s" on the next line.' % (head[:12], head[:7]))
    elif builder in ('codex', 'agy'):
        lines.append('Builder: %s, from %s. The code reviewer is the Claude workflow.' % (builder, source))
        results = [findings for commit, findings in claude_results(comments, changed, 'code') if commit == head]
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


def security_review(gh, config, pr, paths, complete, comments, changed):
    labels = {x['name'] for x in pr['labels']}
    head, base = pr['head']['sha'], pr['base']['ref']
    forced = 'security-needed' in labels
    listed = classify(config, paths)['security']
    if complete and not listed and not forced:
        return True, ['No listed path changed.'], []
    lines = []
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
    claude = claude_results(comments, changed, 'security')
    if claude and claude[-1][1] == '0' and covers(claude[-1][0]):
        return True, lines + ['The Claude security review of %s found no security issues.' % name(claude[-1][0])], []
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


def acceptance(gh, config, pr, files, complete, template):
    labels = {x['name'] for x in pr['labels']}
    head, base = pr['head']['sha'], pr['base']['ref']
    fork = (pr['head'].get('repo') or {}).get('full_name') != gh.repo
    paths = [p for x in files for p in [x['path']] + ([x['previous']] if x['previous'] else [])]
    level1 = classify(config, paths)['level1']
    lines, problems, drop = [], [], []
    if fork:
        lines.append('This PR comes from the fork %s. Stage 1 of "Fork PRs" applies.' % show((pr['head'].get('repo') or {}).get('full_name') or 'that GitHub deleted'))
        issues = gh.linked_issues()
        accepted = [x for x in issues if x['open'] and x['repo'] == gh.repo and 'accepted' in x['labels'] and 'decision' not in x['labels']]
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
        if label_state(gh, 'ready-for-review', labels, (head, base))[1]:
            drop.append('ready-for-review')
            lines.append('A push after the owner review removes the ready-for-review label.')
    owner = []
    if level1:
        owner.append('level 1 paths changed: ' + ', '.join(show(x) for x in level1))
    if not complete:
        owner.append('the PR has more files than GitHub lists')
    if 'owner-needed' in labels:
        owner.append('the PR has the owner-needed label')
    if owner:
        problems.append('This PR is level 1 (%s). The owner merges this PR with the admin bypass, so auto-merge cannot merge it.' % '; '.join(owner))
    if problems:
        return False, lines + problems, drop
    return True, lines + ['Level 0: no level 1 path, no owner-needed label%s.' % (', and the fork checks pass' if fork else '')], drop


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
        config = load_config(workspace / '.github' / 'gates.json')
        if 'pull_request' not in event:
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
