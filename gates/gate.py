#!/usr/bin/env python3
"""The shared PR gates: cross-review, security-review and acceptance, the classify step of ci-ok, and the gate job of the Claude review.

The caller workflow runs this file from the base branch of the project, through the composite action in this folder.
It never checks out or runs PR code. It reads the PR from the GitHub API and the project config from the base checkout.
It uses the standard library only, and it calls no host other than the GitHub API.
"""
import argparse
import ast
import base64
import codecs
from datetime import datetime, timezone
from fnmatch import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import tarfile
import time
import tokenize
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_review import FAILED, MARKER as CLAUDE_MARKER, REQUESTS as CLAUDE_REQUESTS, request_kind as claude_request_kind  # noqa: E402

API = 'https://api.github.com'
# GitHub lists at most 3000 files for a PR. A longer list is security relevant and level 1.
PAGES = 30
# The compare API lists at most 300 files.
COMPARE_FILES = 300
FORK_LINES = 400
CHECKS = ['cross-review', 'security-review', 'acceptance', 'classify', 'claude-review']
# The standard allows at most this number of reviews of one kind for each PR. The review-cap-lifted label lifts it for one head and base.
CLAUDE_CAP = 3
# The cap of "@codex review" requests from an admin on one PR. The review-cap-lifted label lifts it for one head and base.
CODEX_CAP = 3
CODEX_REQUEST = re.compile(r'\s*@codex review\s*(?:\n|$)', re.I)
CAP_LABEL = 'review-cap-lifted'
SECURITY_REQUEST = re.compile(r'\s*@codex security review\s*$', re.I)
# The title of the notice that holds the review cost line. tools/review_cost.py reads this notice.
COST = 'review-cost'
COST_CHECKS = ['cross-review', 'security-review', 'acceptance']

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
          CLAUDE_WORKFLOW: 'docs/claude-review.yml', '.github/dependabot.yml': 'docs/dependabot.yml'}
# An action bump of Dependabot changes only the SHA and the version comment of a uses line.
DEPENDABOT = 'dependabot[bot]'
USES = re.compile(rb'(\s*(?:-\s+)?uses:\s+)([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@([0-9a-f]{40}) # (v?[0-9][0-9A-Za-z.+-]*)(\r?\n)?')
PIN_FILE = '.github/workflows/gates.yml'
# A listed .py file that changes only the text of its docstrings needs no security review. The gate reads the head tree from
# the tarball and stops at these limits. A larger tree needs a review.
TARBALL_BYTES = 64 * 1024 * 1024
TREE_BYTES = 256 * 1024 * 1024
TREE_FILES = 20000
# A docstring is code if a file of the head tree reads it. A Python file that names one of these reads docstrings or
# source files, runs text as code, makes the help text of a command or a grammar from the docstrings, or publishes them
# as docs. The names are
# modules and functions, so a new function of the module "inspect" or "ast" also counts.
DOC_NAMES = {'getdoc', 'get_docstring', 'getcomments', 'getsource', 'getsourcelines', 'findsource', 'getsourcefile', 'getfile',
             'getabsfile', 'getmodule', 'get_source', 'get_data', '__loader__', 'linecache', 'pydoc', 'doctest', 'click',
             'typer', 'fire', 'docopt', 'ply', 'find_spec', 'get_filename', 'importlib', 'pkgutil', 'inspect', 'ast',
             'tokenize', 'runpy', 'zipimport', 'exec', 'eval', 'compile', '__import__', 'sphinx', 'pdoc', 'mkdocstrings',
             'griffe', 'dis', 'marshal', 'help', 'vars', 'globals', 'locals', 'dir', 'getmembers', 'getmembers_static',
             '__getattribute__', '__getstate__', '__reduce__', '__reduce_ex__', 'copyreg', '_getframe', 'currentframe',
             'gc', 'get_referents', 'get_referrers', 'ctypes', 'traceback', 'extract_stack', 'walk_stack',
             # A metaclass gets the namespace of a class, and the namespace holds __doc__.
             '__prepare__', 'add_metaclass', 'with_metaclass', 'new_class', 'prepare_class',
             # A captured warning gives the source file of the code that gives it.
             'catch_warnings', 'showwarning', 'formatwarning', 'captureWarnings', 'WarningMessage',
             # These tools find or read the source file of a module by its name.
             'modulefinder', 'ModuleFinder', 'find_module', 'find_loader', 'imp', 'pyclbr', 'py_compile', 'compileall',
             'pkg_resources', 'meta_path', 'path_hooks', 'path_importer_cache', 'FileFinder', 'SourceFileLoader',
             'tabnanny', 'symtable', 'builtins',
             # A hook of the interpreter gets each event, for example the path of each source file that an import
             # opens.
             'addaudithook', 'audit', 'settrace', 'setprofile', 'monitoring', 'register_callback', 'call_tracing',
             '_current_frames', 'meta_path',
             # A decoder can unpack a reader from a text file, for example "base64 -d run.txt | sh".
             'base64', 'binascii', 'zlib', 'gzip', 'bz2', 'lzma', 'zstd', 'compression', 'zipfile', 'tarfile',
             'unpack_archive', 'pickle', 'shelve', 'fromhex', 'codecs', 'rot13', 'rot_13', 'uu', 'quopri', 'gunzip',
             'zcat', 'bzcat', 'xzcat', 'unxz', 'unzstd', 'zstdcat', 'xxd', 'unzip', 'uudecode', 'base32', 'basenc',
             'openssl', 'tar', 'bzip2', 'xz', '7z', 'cpio',
             # This tuple of functools holds the string '__doc__'.
             'WRAPPER_ASSIGNMENTS',
             # The bytecode cache holds a copy of each docstring.
             'pycache_prefix', 'cache_from_source', 'source_from_cache', 'cache_tag',
             # These standard APIs return a docstring: unittest gives the docstring of a test, cmd.Cmd prints the
             # docstring of a command, and XML-RPC introspection returns the docstring of a method.
             'shortDescription', '_testMethodDoc', 'getDescription', 'do_help', 'system_methodHelp',
             'register_introspection_functions', 'DocXMLRPCServer', 'DocXMLRPCRequestHandler', 'idlelib',
             # A test runner prints the docstring of each test in verbose mode, for example "python -m unittest -v".
             'unittest', 'TextTestRunner', 'TestProgram', 'nose', 'nose2', 'green', 'trial', 'testtools', 'stestr',
             'subunit',
             # functools copies the docstring of a function to its wrapper, and a __setattr__ or a __set__ method can
             # get the copy.
             'wraps', 'update_wrapper', 'cached_property', '__setattr__', '__set__',
             # A debugger, a tracer or a report tool finds a module by its dotted name and prints its source lines.
             'trace', 'tracemalloc', 'pdb', 'bdb', 'ipdb', 'pudb', 'trepan', 'IPython', 'ultratb', 'rich',
             'stackprinter', 'better_exceptions', 'pytest', 'coverage'}
# The plain names of DOC_NAMES, with no leading or trailing underscore, for example dir. The rules also find such a name
# with other underscores, for example __dir__.
BARE_NAMES = {x for x in DOC_NAMES if x == x.strip('_')}
# These attributes give the source file of a module, for example "store.__file__" or "find_spec(name).origin", or the
# code object of a function, for example "f.__code__.co_consts[0]", which is the docstring, or the namespace of a
# module, which holds __doc__, for example "store.__dict__", "f.__globals__" or the globals of a frame. An attribute or a string
# with one of these names counts in each file.
ORIGIN_NAMES = {'__file__', '__spec__', '__cached__', '__path__', 'origin', '__code__', 'co_consts', 'co_code', 'gi_code',
                'cr_code', 'f_code', '__dict__', '__globals__', '__builtins__', 'f_globals', 'f_locals', 'f_builtins', 'f_back',
                'gi_frame', 'cr_frame', 'ag_frame', 'tb_frame', '__closure__', 'cell_contents', 'co_filename',
                '__traceback__', 'pathname', 'metaclass', '__class__'}
# A script gets the path of its own source file from the command line, for example "open(sys.argv[0])", or from a
# warning that it gives, for example "WarningMessage.filename". The names count only in a changed listed file, as a
# bare __file__ does.
SELF_NAMES = {'argv', 'orig_argv', 'filename', 'warnings'}
# In a file that is not Python and not a doc, these words can read a docstring or a source file, for example
# "pytest --doctest-modules". A file system can ignore case, for example "PYDOC src.auth.store", so the words match in
# each case.
DOC_WORDS = re.compile(rb'__doc__|getdoc|get_docstring|getcomments|getsource|get_source|findsource|getabsfile|linecache|pydoc'
                       rb'|doctest|__file__|__spec__|__code__|co_consts|find_spec|importlib|pkgutil|inspect|runpy'
                       # A docs generator publishes the docstrings, for example Sphinx autodoc or mkdocstrings.
                       rb'|sphinx|autodoc|automodule|autoclass|autofunction|automethod|autosummary|pdoc|mkdocstrings|griffe'
                       # A shell can run Python, for example python3 -c "vars(store)". Thus each name of the Python
                       # rules is also a word, at word boundaries, so the two lists cannot drift apart.
                       rb'|\b(?:' + b'|'.join(re.escape(x.encode()) for x in sorted(DOC_NAMES | ORIGIN_NAMES)) + rb')\b'
                       # A plain name with leading or trailing underscores is the same reader, for example __dir__.
                       rb'|\b_+(?:' + b'|'.join(re.escape(x.encode()) for x in sorted(BARE_NAMES)) + rb')_*\b'
                       rb'|\b(?:' + b'|'.join(re.escape(x.encode()) for x in sorted(BARE_NAMES)) + rb')_+\b',
                       re.IGNORECASE)
# The signatures of a zip file, at any place, and of other compressed files, at the start.
ZIP = re.compile(rb'PK\x03\x04|PK\x05\x06|PK\x06\x06')
COMPRESSED = (b'\x1f\x8b', b'BZh', b'\xfd7zXZ\x00', b'\x28\xb5\x2f\xfd', b"7z\xbc\xaf\x27\x1c")
# A .gitattributes file with one of these attributes changes the bytes that a checkout writes. A .lfsconfig file sets
# the LFS store.
CHECKOUT = re.compile(rb'filter|working-tree-encoding|ident|lfs', re.IGNORECASE)
# Python copies each docstring into the bytecode cache, for example __pycache__/store.cpython-314.pyc. An ignore file
# lists these paths, but no program runs it.
CACHE_WORDS = re.compile(rb'__pycache__|\.py[co]\b|pycache_prefix|cache_from_source|cache_tag', re.IGNORECASE)
IGNORE_FILES = {'.gitignore', '.dockerignore', '.hgignore', '.npmignore'}
# A file with one of these extensions, or with a #! line of Python, is Python. The #! line must run Python directly: a
# path to python, or /usr/bin/env and python, and one optional group of flags with no argument. A line such as
# '#!/usr/bin/env -S sh -c ... python' names python, but it runs a shell, so it does not match.
PYTHON = re.compile(rb'#! ?(?:/usr/bin/env +|(?:/[\w.+-]+)+/)python[0-9.]*(?: -[BdEIOPqsSuv]+)? *(?:\n|\Z)')
# Python and a shell run a file with each name, so the gate reads each doc and each config.
# These files name paths. The project config names its boundary files. If such a file parses as Python, it can have only
# literal values, for example JSON.
PATH_DATA = {'.github/gates.json', 'CODEOWNERS', '.github/CODEOWNERS', 'docs/CODEOWNERS', '.gitattributes', '.gitignore'}
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
# A run waits at most WAIT seconds for that record.
FRESH = 300
WAIT = 120
# The run of the labeled event posts the record, and the Actions queue can delay that run. Thus an admin label with no
# record newer than its labeled event is pending, and the gate does not remove it, for at most PENDING seconds.
# A pending label does not count as an approval. After PENDING seconds with no record, the label is stale.
PENDING = 6 * 3600

# Each check binds an approval record to these labels when the owner adds the label.
BIND = {
    'acceptance': ('accepted', 'ready-for-review'),
    'security-review': ('security-reviewed',),
    'cross-review': ('cross-reviewed', CAP_LABEL),
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
# The category of each shared security pattern, in the order of the check.
SHARED_CATEGORIES = [('GitHub config', ['.github/*']),
                     ('standard document', ['CONSTITUTION.md', 'docs/STANDARD.md', 'docs/REVIEW.md', 'SECURITY.md']),
                     ('agent instructions', anywhere(AGENT_FILES)),
                     ('agent config', anywhere(['.agents/*', '.claude/*', '.codex/*', '.gemini/*'])),
                     ('dependency manifest', anywhere(MANIFESTS))]
CONFIG_KEYS = ['code', 'security_sources', 'not_security', 'boundaries', 'level1', 'generated', 'docstring_only']


class GateError(Exception):
    """A format or data problem. The gate fails closed."""


def show(text):
    """Return text with control characters escaped, so that a file name cannot write a workflow command."""
    return json.dumps(str(text))[1:-1]


def markdown(text):
    """Return text with the Markdown characters escaped, so that a file name cannot add a link or a table to the job summary."""
    return re.sub(r'([\\`*_{}\[\]()<>#+!|~&-])', r'\\\1', text)


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


def category(config, path):
    """Return the category of a listed path, in the order of the classify check."""
    for name, patterns in SHARED_CATEGORIES:
        if match(path, patterns):
            return name
    return 'project boundary' if match(path, config['boundaries']) else 'project security source'


def described(config, paths):
    return ', '.join('%s (%s)' % (show(x), category(config, x)) for x in paths)


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
        return [{'path': x['filename'], 'previous': x.get('previous_filename'), 'status': x.get('status'),
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

    def mode(self, ref, path):
        """Return the Git mode of the file at the commit, for example 100644 or 100755, or None if it does not exist."""
        folder, name = posixpath.split(path)
        tree = self.call('GET', '/repos/%s/git/trees/%s' % (self.repo, urllib.parse.quote('%s:%s' % (ref, folder), safe='/:')))
        if not isinstance(tree, dict) or tree.get('truncated') or not isinstance(tree.get('tree'), list):
            raise GateError('The GitHub API did not give the Git tree of %s at %s.' % (show(folder or '.'), ref[:12]))
        return next((x.get('mode') for x in tree['tree'] if x.get('path') == name and x.get('type') == 'blob'), None)

    def parents(self, commit):
        """Return the parent SHAs of the commit, in order."""
        data = self.call('GET', '/repos/%s/commits/%s' % (self.repo, commit))
        return [x.get('sha') for x in (data or {}).get('parents') or []]

    def tree_files(self, ref):
        """Return {path: bytes} for each file of the tree at the commit. The tarball of the commit gives all files in one call."""
        request = urllib.request.Request(API + '/repos/%s/tarball/%s' % (self.repo, ref), headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(TARBALL_BYTES + 1)
        if len(data) > TARBALL_BYTES:
            raise GateError('The tarball of %s is larger than %d bytes.' % (ref[:12], TARBALL_BYTES))
        found, links, total = {}, {}, 0
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            # The limit counts each entry, also the folders and the links.
            for count, member in enumerate(archive, 1):
                if count > TREE_FILES:
                    raise GateError('The tree of %s has more than %d files.' % (ref[:12], TREE_FILES))
                if member.islnk():
                    raise GateError('The tree of %s has a hard link.' % ref[:12])
                if member.issym():
                    links[member.name.split('/', 1)[-1]] = member.linkname
                    continue
                if not member.isfile():
                    continue
                total += member.size
                if total > TREE_BYTES:
                    raise GateError('The tree of %s is larger than %d bytes.' % (ref[:12], TREE_BYTES))
                # The first part of each name is the top folder of the tarball.
                found[member.name.split('/', 1)[-1]] = archive.extractfile(member).read()
        raw = dict(found)
        # A file system can ignore the case of a name or change its Unicode form. Then two paths of the tree can be one
        # file in a checkout, so this fails closed.
        names = {}
        for path in list(found) + list(links):
            key = fold(path)
            if key in names:
                raise GateError('The paths %s and %s of the tree of %s can be one file in a checkout.' % (
                    show(names[key]), show(path), ref[:12]))
            names[key] = path
        # A link gets the content of its target in the tree, so the reader check reads it with the rules of the link path.
        # A link with no target file fails closed, because a file system can find the target in another case, for
        # example "payload -> SRC/AUTH/STORE.PY".
        for path in links:
            target, hops = path, 0
            while target in links and hops <= len(links):
                target, hops = posixpath.normpath(posixpath.join(posixpath.dirname(target), links[target])), hops + 1
            if target in found:
                found[path] = found[target]
            elif target in ('.', '/') or target.startswith(('../', '/')) or target == '..' or \
                    any(x.startswith(target + '/') for x in list(found) + list(links)):
                # A link to a folder gives a second name to each file in it, for example payload/store.py for
                # "payload -> src/auth". A link out of the tree can come back into it. Both fail closed.
                raise GateError('The link %s goes to a folder or out of the tree of %s.' % (show(path), ref[:12]))
            else:
                raise GateError('The link %s has no target file in the tree of %s.' % (show(path), ref[:12]))
        # The tarball omits a file with the export-ignore attribute, and it has no submodule. Thus the Git tree must
        # agree with the tarball, so that no file can hide a reader.
        tree = self.call('GET', '/repos/%s/git/trees/%s?recursive=1' % (self.repo, ref))
        if not isinstance(tree, dict) or tree.get('truncated') or not isinstance(tree.get('tree'), list):
            raise GateError('The GitHub API did not give the full Git tree of %s.' % ref[:12])
        if any(x.get('type') == 'commit' for x in tree['tree']):
            raise GateError('The tree of %s has a submodule.' % ref[:12])
        missing = sorted({x.get('path') for x in tree['tree'] if x.get('type') == 'blob'} - set(found) - set(links))
        if missing:
            raise GateError('The tarball of %s does not have %s, for example by export-ignore.' % (ref[:12], show(missing[0])))
        # The tarball can change the bytes of a file, for example by export-subst or an eol rule. Thus each file and link
        # of the tarball must have the blob SHA of the Git tree.
        for item in tree['tree']:
            path = item.get('path')
            if item.get('type') != 'blob':
                continue
            link = item.get('mode') == '120000'
            if link != (path in links):
                raise GateError('The tarball of %s gives %s as a different kind of file.' % (ref[:12], show(path)))
            if blob_sha(links[path].encode() if link else raw[path]) != item.get('sha'):
                raise GateError('The tarball of %s changes %s, for example by export-subst.' % (ref[:12], show(path)))
        return found

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
    pending is true if an admin added the label in the last PENDING seconds, and no record of the label for any head
    or base is newer than that labeled event. Then the run of the labeled event did not post its record yet, so do not
    remove the label. A record for a different head or base shows that the run posted its record, so the label is stale."""
    if label not in labels:
        return [], False
    last = gh.last_labeled(gh.number, label)
    if not last or not gh.is_admin(last[0] or ''):
        return [], False
    waited = 0
    while True:
        records = [(h, b) for name, h, b, t in read_records(gh.comments()) if name == label and t and stamp(t) >= stamp(last[1])]
        heads = [h for h, b in records if b == base]
        age = (gh.now() - stamp(last[1])).total_seconds()
        if records or age >= FRESH or waited >= WAIT:
            return heads, not records and age < PENDING
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


def codex_requests(gh, comments):
    """Return the number of "@codex review" requests on the PR from a user with the admin role.
    A request from another user does not count, so a fork contributor cannot use the cap to stop a PR."""
    admins = {}
    for item in comments:
        login = (item.get('user') or {}).get('login') or ''
        if login and CODEX_REQUEST.match(item.get('body') or '') and login not in admins:
            admins[login] = gh.is_admin(login)
    return sum(1 for item in comments if CODEX_REQUEST.match(item.get('body') or '')
               and admins.get((item.get('user') or {}).get('login') or ''))


def review_cost(gh, pr, comments):
    """Return the review cost line of the PR. Each count holds only the request comments of a user with the admin role,
    as the cap does. The hours go from the PR open time to this gate run."""
    admins = {}

    def count(test):
        found = [item for item in comments if test((item.get('body') or '').strip())]
        for item in found:
            login = (item.get('user') or {}).get('login') or ''
            if login and login not in admins:
                admins[login] = gh.is_admin(login)
        return sum(1 for item in found if admins.get((item.get('user') or {}).get('login') or ''))
    texts = {x.lower() for x in CLAUDE_REQUESTS.values()}
    hours = (gh.now() - stamp(pr['created_at'])).total_seconds() / 3600
    return '%s v1 codex=%d security=%d claude=%d hours=%.1f' % (
        COST, codex_requests(gh, comments), count(SECURITY_REQUEST.match), count(lambda x: x.lower() in texts), hours)


def cross_review(gh, pr, comments, changed):
    labels = {x['name'] for x in pr['labels']}
    head, base = pr['head']['sha'], pr['base']['ref']
    builder, source = builder_of(gh, pr)
    valid, stale = label_state(gh, 'cross-reviewed', labels, (head, base))
    lifted, cap_stale = label_state(gh, CAP_LABEL, labels, (head, base))
    lines, drop = [], (['cross-reviewed'] if stale else []) + ([CAP_LABEL] if cap_stale else [])
    if builder in (None, 'claude'):
        name = 'No harness' if builder is None else 'Claude Code'
        lines.append('Builder: %s, from %s. The code reviewer is the Codex app.' % (name, source))
        count = codex_requests(gh, comments)
        if count > CODEX_CAP and not lifted:
            if valid:
                return True, lines + ['The cross-reviewed label applies to head %s.' % head[:12]], drop
            lines.append('The PR has %d "@codex review" requests. The cap is %d for each PR.' % (count, CODEX_CAP))
            if cap_stale:
                lines.append('The %s label has no approval record for head %s on base %s. The owner adds the label again.' % (CAP_LABEL, head[:12], base))
            elif CAP_LABEL in labels:
                lines.append('The %s label has no approval record yet. The run of the labeled event posts it, and the queue can '
                             'delay that run. That run checks the PR again.' % CAP_LABEL)
            lines.append('Codex review cap reached. Examine the approach and report to the owner.')
            return False, lines, drop
        if count > CODEX_CAP:
            lines.append('The %s label applies to head %s, so the cap of %d requests does not apply.' % (CAP_LABEL, head[:12], CODEX_CAP))
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
        return True, lines + ['The cross-reviewed label applies to head %s.' % head[:12]], drop
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


def bump_line(a, b):
    """Return the action name if the two lines differ only in the SHA and the version comment of one uses line, else None."""
    old, new = USES.fullmatch(a), USES.fullmatch(b)
    if not old or not new or old.group(1, 2, 5) != new.group(1, 2, 5) or old.group(3) == new.group(3):
        return None
    return new.group(2).decode()


def bump_check(gh, pr, files):
    """Return (proved lines, None) if the PR is an action bump of Dependabot, else (None, reason).
    Each changed file keeps its lines, and each changed line changes only the SHA and the version comment of a uses line."""
    head, base = pr['head']['sha'], pr['base']['sha']
    proved = []
    for item in files:
        path = item['path']
        if item.get('status') != 'modified' or item.get('previous'):
            return None, '%s is not a modified file with the same path.' % show(path)
        before, after = gh.content(gh.repo, path, base), gh.content(gh.repo, path, head)
        if not before or not after:
            return None, 'Cannot read %s at the base or the head.' % show(path)
        old, new = before[1].splitlines(True), after[1].splitlines(True)
        if len(old) != len(new):
            return None, '%s adds or removes lines.' % show(path)
        for number, (a, b) in enumerate(zip(old, new), 1):
            if a != b:
                action = bump_line(a, b)
                if not action:
                    return None, '%s line %d is not only a new SHA and version of a uses line.' % (show(path), number)
                proved.append('%s line %d: %s gets a new SHA and version.' % (show(path), number, show(action)))
    if not proved:
        return None, 'The PR changes no uses line.'
    return proved, None


def code_tree(data):
    """Return the ast dump of the Python source without its docstrings. A docstring is the first statement of a module,
    class or function, if the statement is only a string constant. The tree has no comments."""
    tree = ast.parse(data)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                node.body = node.body[1:]
    return ast.dump(tree)


def docstring_starts(tree):
    """Return the start (line, column) of each docstring in the tree."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add((first.value.lineno, first.value.col_offset))
    return found


def text_without_docstrings(data):
    """Return the source encoding and the source text, with a mark in place of each docstring. Thus each byte outside
    the docstrings must stay the same: each comment, each line break, each backslash and each space. A tool can read a
    comment and its physical line, for example "# nosec" or a "# /// script" block, so no comment can move or change.
    The mark is a null character, and Python source cannot contain one."""
    docs = docstring_starts(ast.parse(data))
    encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
    # Strict UTF-8 gives one text for one byte string, so equal text is equal bytes. Another encoding, for example
    # unicode_escape, can give equal text for different bytes, and a tool can read the bytes. It fails closed.
    if encoding not in ('utf-8', 'utf-8-sig'):
        raise ValueError('the source encoding %s is not UTF-8' % encoding)
    text = data.decode(encoding)
    starts = [0]
    for line in re.split(r'(?<=\n)', text):
        starts.append(starts[-1] + len(line))
    spans = []
    for token in tokenize.tokenize(io.BytesIO(data).readline):
        # The ast column counts UTF-8 bytes. The token column counts characters.
        if token.type == tokenize.STRING and (token.start[0], len(token.line[:token.start[1]].encode())) in docs:
            start, end = starts[token.start[0] - 1] + token.start[1], starts[token.end[0] - 1] + token.end[1]
            # A line break other than \n can move the rows of tokenize. Then the span is wrong, and the check fails.
            if text[start:end] != token.string:
                raise ValueError('the docstring position is not clear')
            spans.append((start, end))
    for start, end in reversed(spans):
        text = text[:start] + '\0' + text[end:]
    return encoding, text


def literal_tree(tree):
    """Return True if the tree has only literal values, for example the tree of a JSON file."""
    return all(isinstance(node, (ast.Module, ast.Expr, ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Load,
                                 ast.UnaryOp, ast.USub)) or (isinstance(node, ast.Name) and node.id in ('true', 'false', 'null'))
               for node in ast.walk(tree))


def doc_reader(path, data, changed, files):
    """Return the reason if the file can read a docstring, else None. A bare __doc__ or __file__ names its own module
    only, so it counts only in a changed file. A string that contains the name of a changed file can open it. Python
    can run a file with each name, so each file that parses gets the Python rules. Each file that is not Python also
    gets the word rules, because a shell can run a doc or a config. Only a file of PATH_DATA can name a listed file."""
    # The gate cannot read a compressed file. Python runs a zip file, also after a #! line, for example a .pyz app.
    if ZIP.search(data) or data.startswith(COMPRESSED):
        return 'is an archive or a compressed file'
    # A file that is not text can hold a packed reader in a form that the gate cannot read, for example zlib data.
    if not is_text(data):
        return 'is not UTF-8, UTF-16 or UTF-32 text'
    # A checkout can write other bytes than the blob. A Git LFS pointer gets the object of the LFS store, and a filter,
    # a working-tree-encoding or an ident attribute changes the bytes. The gate reads only the blob.
    if data.startswith(b'version https://git-lfs.github.com/spec/'):
        return 'is a Git LFS pointer'
    if posixpath.basename(path) in ('.gitattributes', '.lfsconfig') and CHECKOUT.search(data):
        return 'changes the bytes of a checkout'
    # A .py file with a #! line of another program, for example #!/bin/sh, is not only Python.
    python = (path.endswith(('.py', '.pyw')) and not data.startswith(b'#!')) or PYTHON.match(data)
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
        if python:
            return 'does not parse at the head (%s)' % type(error).__name__
        tree = None
    if tree is not None and not (path in PATH_DATA and literal_tree(tree)):
        reason = python_reader(tree, changed, files)
        if reason:
            return reason
    if python:
        return None
    texts = text_forms(data)
    if any(DOC_WORDS.search(x) for x in texts):
        return 'names a docstring reader'
    if posixpath.basename(path) not in IGNORE_FILES and any(CACHE_WORDS.search(x) for x in texts):
        return 'names the bytecode cache'
    if path in PATH_DATA:
        return None
    name = file_name_in(files, texts)
    return 'names the file %s' % show(name) if name else None


def fold(text):
    """Return the text in one case and one Unicode form. A file system can ignore the case of a name, for example on
    Windows, or change its Unicode form, for example on macOS."""
    return unicodedata.normalize('NFKC', unicodedata.normalize('NFKC', text).casefold())


def file_name_in(files, forms):
    """Return the first name of files that is in one of the byte forms, in each case and Unicode form, else None."""
    texts = [fold(x.decode('utf-8', 'replace')) for x in forms]
    return next((x for x in sorted(files) if any(fold(x) in y for y in texts)), None)


def is_text(data):
    """Return True if the bytes are UTF-8 text, or UTF-16 or UTF-32 text with a BOM."""
    for bom, encoding in ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'), (codecs.BOM_UTF16_LE, 'utf-16'),
                          (codecs.BOM_UTF16_BE, 'utf-16'), (b'', 'utf-8')):
        if data.startswith(bom):
            try:
                data.decode(encoding)
                return True
            except UnicodeDecodeError:
                pass
    return False


def text_forms(data):
    """Return the forms of the file for the word rules, in UTF-8. A UTF-16 or UTF-32 script, for example a PowerShell
    script, has a NUL byte between its characters, and it can have no BOM. Thus the forms are the bytes, the bytes with
    no NUL byte, and the text in each UTF-16 and UTF-32 byte order. A file name that is not ASCII, for example é.py, is
    in UTF-8 only in the last forms."""
    forms = [data, data.replace(b'\0', b'')]
    if b'\0' in data or data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        for encoding in ('utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be'):
            forms.append(data.decode(encoding, 'replace').encode('utf-8', 'replace'))
    return forms


def metaclass_name(node):
    """Return True if the expression or the name is type or a metaclass, for example abc.ABCMeta."""
    name = node if isinstance(node, str) else node.id if isinstance(node, ast.Name) else \
        node.attr if isinstance(node, ast.Attribute) else ''
    return name == 'type' or name.endswith(('Meta', 'Metaclass', 'MetaClass', 'EnumType'))


def python_reader(tree, changed, files):
    """Return the reason if the Python tree can read a docstring, else None."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == '__doc__':
            return 'reads __doc__ of an object'
        if isinstance(node, ast.Constant) and node.value == '__doc__':
            return 'names __doc__ in a string'
        if isinstance(node, ast.ImportFrom) and any(x.name == '__doc__' for x in node.names):
            return 'imports __doc__ of a module'
        if isinstance(node, ast.Name) and node.id == '__doc__' and changed:
            return 'reads its own __doc__'
        # A class can put a descriptor in __doc__, for example "__doc__ = Recorder()", and it then gets a copied
        # docstring.
        if isinstance(node, ast.Name) and node.id == '__doc__' and isinstance(node.ctx, ast.Store):
            return 'sets __doc__'
        if isinstance(node, ast.Name) and node.id == '__file__' and changed:
            return 'names its own source file'
        if isinstance(node, ast.Attribute) and node.attr in ORIGIN_NAMES:
            return 'reads %s of an object' % node.attr
        # A metaclass gets the namespace of each class that uses it, also of a subclass with no metaclass keyword. A
        # metaclass is a subclass of type, for example "class Runner(type)", or the result of a call with three
        # arguments, for example 'type("Runner", (type,), {"__new__": run})'.
        if isinstance(node, ast.ClassDef) and any(metaclass_name(x) for x in node.bases):
            return 'defines a metaclass'
        if isinstance(node, ast.Call) and len(node.args) == 3 and metaclass_name(node.func):
            return 'makes a class with a metaclass'
        # A name can be a second name for a metaclass, for example "from builtins import type as factory" or
        # "factory = type(int)". Thus each use of type or of a metaclass name counts, and so does a base that is a call.
        if isinstance(node, ast.ClassDef) and any(isinstance(x, ast.Call) for x in node.bases):
            return 'has a base that is a call'
        found = [x for x in ([node] if isinstance(node, (ast.Name, ast.Attribute)) else
                             [y.name.split('.')[-1] for y in node.names] if isinstance(node, (ast.Import, ast.ImportFrom))
                             else []) if metaclass_name(x)]
        if found:
            return 'names a metaclass'
        # A class pattern reads an attribute with no ast.Attribute, for example "case object(__doc__=text)".
        found = ({'__doc__'} | ORIGIN_NAMES) & set(node.kwd_attrs if isinstance(node, ast.MatchClass) else
                                                    [node.arg] if isinstance(node, ast.keyword) else [])
        if found:
            return 'reads %s in a pattern or a keyword' % sorted(found)[0]
        if isinstance(node, ast.Constant) and node.value in ORIGIN_NAMES:
            return 'names %s in a string' % node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            # Compare bytes, with each name in UTF-8. A str value can have a lone surrogate.
            raw = node.value if isinstance(node.value, bytes) else node.value.encode('utf-8', 'surrogatepass')
            # A command string can name the file at each place, for example "cat src/auth/store.py | sh".
            name = file_name_in(files, [raw])
            if name:
                return 'names the file %s' % show(name)
            # A string can give a reader to a tool, for example pytest.main(["--doctest-modules"]).
            if CACHE_WORDS.search(raw):
                return 'names the bytecode cache in a string'
            if DOC_WORDS.search(raw):
                return 'names a docstring reader in a string'
        # A def also counts, for example "def __prepare__(name, bases)" of a metaclass.
        names = ([node.id] if isinstance(node, ast.Name) else [node.attr] if isinstance(node, ast.Attribute)
                 else [node.name] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 else [x.name for x in node.names] if isinstance(node, ast.Import)
                 else [x.name for x in node.names] + [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
        if changed and set(names) & SELF_NAMES:
            return 'names its own source file in %s' % sorted(set(names) & SELF_NAMES)[0]
        # A name with other leading or trailing underscores is the same reader, for example __dir__ for dir.
        found = {y for x in names for y in (x.split('.')[0], x.split('.')[0].strip('_'))} & (DOC_NAMES | BARE_NAMES)
        if found:
            return 'names %s' % sorted(found)[0]
        # A name that contains a reader word is also a reader, for example xdoctest or doctest_module. The rules above
        # check a bare __doc__ or __file__, which names its own module.
        word = next((x for x in names if x not in ('__doc__', '__file__') and DOC_WORDS.search(x.encode())), None)
        if word:
            return 'names %s' % word
    return None


def docstring_check(gh, pr, listed, files):
    """Return (proved lines, None) if each listed path is a .py file that changes only the text of its docstrings, else
    (None, reason). The file keeps its path. The tree of the base and the merge are equal after the docstrings go.
    No file of the merge tree reads a docstring. The merge is the test merge commit of GitHub, with the parents base
    and head, so the check also reads the files that the base branch has and the head does not have."""
    head, base, merge = pr['head']['sha'], pr['base']['sha'], pr.get('merge_commit_sha') or ''
    if not re.fullmatch(r'[0-9a-f]{40}', merge) or gh.parents(merge) != [base, head]:
        return None, 'GitHub has no test merge commit of the base %s and the head %s.' % (base[:12], head[:12])
    status = {x['path']: x for x in files}
    proved, merged = [], {}
    for path in listed:
        item = status.get(path)
        if not path.endswith('.py'):
            return None, '%s is not a .py file.' % show(path)
        if not item or item.get('status') != 'modified' or item.get('previous'):
            return None, '%s is not a modified file with the same path.' % show(path)
        before, after = gh.content(gh.repo, path, base), gh.content(gh.repo, path, merge)
        if not before or not after:
            return None, 'Cannot read %s at the base or the head.' % show(path)
        if before[1] == after[1]:
            return None, '%s changes no byte, so the change is not a docstring change.' % show(path)
        mode = gh.mode(base, path)
        # The contents API reads the target of a link, so a listed link can change its target with no byte change.
        if mode not in ('100644', '100755') or gh.mode(merge, path) != mode:
            return None, '%s changes its file mode, or it is not a regular file.' % show(path)
        # A #! line of another program, for example #!/bin/sh, runs the docstring as shell code. A shell runs an
        # executable file with no #! line. Thus the file must be Python when it runs.
        if (after[1].startswith(b'#!') or mode == '100755') and not PYTHON.match(after[1]):
            return None, '%s can run as a program that is not Python.' % show(path)
        try:
            same = code_tree(before[1]) == code_tree(after[1])
            text = text_without_docstrings(before[1]) == text_without_docstrings(after[1])
        except (SyntaxError, ValueError, RecursionError, MemoryError, tokenize.TokenError, LookupError) as error:
            return None, '%s does not parse at the base or the head (%s).' % (show(path), type(error).__name__)
        if not same:
            return None, '%s changes code, not only docstrings.' % show(path)
        if not text:
            return None, '%s changes a byte outside the docstrings, or adds or removes a docstring.' % show(path)
        proved.append('%s: only the text of docstrings changes.' % show(path))
        merged[path] = after[1]
    if not proved:
        return None, 'No listed path changed.'
    basenames = {posixpath.basename(x) for x in listed}
    for path, data in sorted(gh.tree_files(merge).items()):
        # The tarball gives a link the bytes of its target. A link or a copy of a listed file is a second name for it,
        # for example "sh payload" for "payload -> src/auth/store.py".
        alias = next((x for x in sorted(merged) if x != path and merged[x] == data), None)
        if alias:
            return None, '%s has the bytes of %s, so a docstring can be code.' % (show(path), show(alias))
        reason = doc_reader(path, data, path in listed, basenames)
        if reason:
            return None, '%s %s, so a docstring can be code.' % (show(path), reason)
    return proved, None


def security_review(gh, config, pr, paths, complete, comments, changed, files=()):
    """Return (passed, lines, drop, summary). The summary is one line that tells the agent if a security review is necessary."""
    labels = {x['name'] for x in pr['labels']}
    head, base = pr['head']['sha'], pr['base']['ref']
    forced = blocks(gh, gh.number, 'security-needed', labels)
    listed = classify(config, paths)['security']
    if complete and not listed and not forced:
        return True, ['No listed path changed.'], [], 'Security review: not needed. No listed path changed.'
    lines = []
    if complete and not forced and (pr.get('user') or {}).get('login') == DEPENDABOT:
        # An action bump of Dependabot needs no review. The PR stays level 1, so the owner reads the SHAs before the merge.
        try:
            proved, reason = bump_check(gh, pr, files)
        except (OSError, GateError, KeyError, TypeError, ValueError) as error:
            proved, reason = None, 'The bump check failed closed (%s: %s).' % (type(error).__name__, show(error))
        if proved:
            return (True, ['The PR is an action bump of %s. No review is necessary.' % DEPENDABOT] + proved, [],
                    'Security review: not needed. The PR is a proved action bump.')
        lines.append('Bump check: ' + reason)
    if complete and not forced:
        # An exact copy of the gate files and a pin change need no new review. The review of UPSTREAM covers them.
        try:
            proved, reason = copy_check(gh, pr, listed)
        except (OSError, GateError, KeyError, TypeError, ValueError) as error:
            proved, reason = None, 'The copy check failed closed (%s: %s).' % (type(error).__name__, show(error))
        if proved:
            return (True, ['Each listed path is a proved copy or pin change. No review is necessary.'] + proved, [],
                    'Security review: not needed. Each listed path is a proved copy or pin change.')
        lines.append('Copy check: ' + reason)
        # A change of only the docstring text in listed .py files needs no review. The code does not change. The owner
        # turns this on for each path in "docstring_only" of the project config, after the owner confirms that no tool
        # of the project reads those docstrings. The config comes from the base branch, so a PR cannot turn it on. The
        # proof below is a second layer: it finds the usual readers, but no list of names can find each reader.
        if config['docstring_only']:
            outside = next((x for x in listed if not match(x, config['docstring_only'])), None)
            if outside:
                proved, reason = None, '%s is not in "docstring_only" of the project config.' % show(outside)
            else:
                try:
                    proved, reason = docstring_check(gh, pr, listed, files)
                except (OSError, GateError, KeyError, TypeError, ValueError, tarfile.TarError) as error:
                    proved, reason = None, 'The docstring check failed closed (%s: %s).' % (type(error).__name__, show(error))
            if proved:
                return (True, ['Each listed path changes only the text of docstrings. No review is necessary.'] + proved, [],
                        'Security review: not needed. Only docstrings changed in a listed path.')
            lines.append('Docstring check: ' + reason)
    if not complete:
        lines.append('The PR has more files than GitHub lists, so the change is security relevant.')
    if listed:
        lines.append('Listed paths: ' + ', '.join(show(x) for x in listed))
    if forced:
        lines.append('The security-needed label requires a security review of the current head.')
    # For each clean review of an earlier commit: (full SHA, the listed paths of the later commits, or None if no compare is valid).
    seen = {}

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
        touched = classify(config, later)['security'] if ancestor and complete else None
        seen.setdefault(commit, (full, touched))
        return touched == []

    def name(commit):
        return 'head %s' % head[:12] if head.startswith(commit) else 'commit %s, and the later commits touch no listed path' % commit[:12]

    def summary(clean):
        """Tell the agent what the next security review must cover."""
        if clean and head.startswith(clean):
            return 'Security review: the clean review of head %s is valid.' % head[:12]
        if clean and clean in seen:
            full, touched = seen[clean]
            if touched == []:
                return 'Security review: the clean review is still valid. The commits after %s touch no listed path.' % full[:12]
            if touched:
                return 'Security review: needed. The commits after the clean review of %s change: %s.' % (full[:12], described(config, touched))
        if forced:
            return 'Security review: needed. The security-needed label requires a review of head %s.' % head[:12]
        if not complete:
            return 'Security review: needed. The PR has more files than GitHub lists.'
        return 'Security review: needed. The PR changes: %s.' % described(config, listed)

    results, errors = codex_security(comments, changed)
    lines += errors
    clean = results[-1][0] if results and results[-1][0] and results[-1][1] else None
    if clean and covers(clean):
        return True, lines + ['The Codex security review of %s found no security issues.' % name(clean)], [], summary(clean)
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
            return True, lines + ['The security-reviewed label applies to %s.' % name(bound[-1])], [], summary(bound[-1])
        lines.append('The security-reviewed label has no approval record that covers head %s on base %s. Review the current change, then add the label again.' % (head[:12], base))
        drop = [] if pending else ['security-reviewed']
        if not clean and bound:
            clean = bound[-1]
    if results and results[-1][0] and not results[-1][1]:
        lines.append('The latest Codex security review has open findings. Fix them and ask again, or write the answer on the PR and ask the owner to add the security-reviewed label.')
    lines.append('Post "@codex security review" as a separate comment. The check passes when the Codex security review of head %s finds no security issues, or when the owner adds the security-reviewed label.' % head[:12])
    return False, lines, drop, summary(clean)


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
    of each kind for each head and merge base, and at most CLAUDE_CAP reviews of each kind for each PR, unless the
    review-cap-lifted label has a record for the current head and base."""
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
        # The owner label lifts the cap for one head and base, with the same record rule as the Codex cap.
        # The gate job has read permissions only, so it does not remove a stale label. cross-review removes it.
        if label_state(gh, CAP_LABEL, labels, (head, base))[0]:
            lines.append('The %s label applies to head %s, so the cap of %d reviews does not apply.' % (CAP_LABEL, head[:12], CLAUDE_CAP))
        else:
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
    """Return the kind of the Claude request in the comment, or None. The first line decides: see claude_review.request_kind."""
    return claude_request_kind((event.get('comment') or {}).get('body'))


def run(check, gh, event, config, template=''):
    """Return (passed, lines, outputs). This function makes all of its network calls through gh."""
    if check == 'classify':
        files, complete = gh.files()
        paths = [p for x in files for p in [x['path']] + ([x['previous']] if x['previous'] else [])]
        docs = complete and classify(config, paths)['docs_only']
        return True, ['Docs-only change.' if docs else 'The project jobs run.'], {'docs_only': str(docs).lower(), 'code': str(not docs).lower()}
    outputs = {}
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
        kind = request_kind(event)
        if kind is None:
            # The workflow starts for each comment that names @claude. Other text passes with no kind, and the other jobs skip.
            return True, ['The comment is not a Claude review request.'], {'kind': ''}
        passed, lines, outputs = claude_request(gh, pr, comments, gh.base_changed_at(), kind)
        return passed, lines, ({**outputs, 'kind': kind} if passed else outputs)
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
            passed, lines, drop, summary = security_review(gh, config, pr, paths, complete, comments, changed, files)
            outputs = {'summary': summary}
    for name in drop:
        gh.remove_label(name)
    return passed, lines, outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=CHECKS)
    args = parser.parse_args(argv)
    outputs = {}
    request = ''
    try:
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
        workspace = Path(os.environ.get('GITHUB_WORKSPACE') or '.')
        # The gate job of the Claude review has no checkout, and it reads no project config.
        config = None if args.check == 'claude-review' else load_config(workspace / '.github' / 'gates.json')
        if args.check == 'claude-review':
            issue = event.get('issue') or {}
            if 'comment' not in event or not issue.get('pull_request'):
                raise GateError('The claude-review check runs only for an issue_comment event on a PR.')
            request = request_kind(event) or ''
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
            if passed and args.check in COST_CHECKS:
                # The cost line is a record only. A failure to make it does not change the result of the gate.
                try:
                    cost = review_cost(gh, gh.pr(), gh.comments())
                except (OSError, GateError, KeyError, TypeError, ValueError) as error:
                    print('::warning title=%s::No review cost line (%s: %s).' % (COST, type(error).__name__, show(error)))
                else:
                    print('::notice title=%s::%s' % (COST, cost))
                    if os.environ.get('GITHUB_STEP_SUMMARY'):
                        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as out:
                            out.write(cost + '\n')
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
        outputs = {'reason': ' '.join(lines).replace('\r', ' ').replace('\n', ' '), 'kind': request}
    if os.environ.get('GITHUB_OUTPUT') and outputs:
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as out:
            for key, value in outputs.items():
                out.write('%s=%s\n' % (key, value))
    if os.environ.get('GITHUB_STEP_SUMMARY') and outputs.get('summary'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as out:
            out.write(markdown(outputs['summary']) + '\n')
    print('\n'.join(lines))
    if outputs.get('summary'):
        print(outputs['summary'])
    if not passed:
        print('::error title=%s::%s' % (args.check, lines[-1]))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
