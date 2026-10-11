#!/usr/bin/env python3
"""Render Gradle JUnit XML into a summary and report (render); post them to the job summary, commit and PR (publish)."""
import glob
import json
import os
import re
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PRUNE = {'.git', '.gradle', '.gtnh-workflows'}
MAX_SUMMARY = 45000  # ASCII chars; under the job summary limit
MAX_NAME = 200
MAX_LINE = 300  # chars
MAX_LINES = 10  # per failure
MAX_FAILURES = 5  # shown with output
MAX_LISTED = 10  # GitHub shows at most 10 error annotations per step
MARKER = '<!-- gtnh-test-results -->'
STATUS_CONTEXT = 'Tests'
SLOW_FLOOR = 1.0  # seconds
SLOW_FACTOR = 10  # x median
OUTCOMES = ('failed', 'passed', 'skipped')
HEADING = '### Test results: {}'
OMITTED = '{} more not shown; see the report.'


def report_dir():
    return Path(os.environ['RUNNER_TEMP'], 'test-report')


def find_reports(root):
    """Yield (task key, project dir, xml path) for <project>/build/test-results/<task>/TEST-*.xml."""
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in PRUNE)
        path = Path(current)
        if path.parent.name != 'test-results' or path.parent.parent.name != 'build':
            continue
        project = path.parents[2]
        key = ':'.join(project.relative_to(root).parts) + ':' + path.name
        for name in sorted(files):
            if name.startswith('TEST-') and name.endswith('.xml'):
                yield key, project, path / name


def one_line(text):
    return ' '.join(text.split())[:MAX_NAME]


def parse(path):
    root = ET.parse(path).getroot()
    started = {case: suite.get('timestamp', '') for suite in root.iter('testsuite') for case in suite.findall('testcase')}
    cases = []
    for case in root.iter('testcase'):
        outcome, detail = 'passed', ''
        for tag, result in (('skipped', 'skipped'), ('failure', 'failed'), ('error', 'failed')):
            node = case.find(tag)
            if node is not None:
                outcome = result
                detail = (node.text or '').strip() or node.get('message', '')
        try:
            seconds = round(float(case.get('time', 0)), 3)
        except ValueError:
            seconds = 0.0
        cases.append(dict(cls=one_line(case.get('classname', '')), name=one_line(case.get('name', '')),
                          outcome=outcome, seconds=seconds if 0 < seconds < 1e9 else 0.0,
                          detail=detail.replace('\r\n', '\n').replace('\r', '\n'), started=started.get(case, '')))
    return cases


def count(tests):
    return {o: sum(t['outcome'] == o for t in tests) for o in OUTCOMES}


def total_seconds(tests):
    return sum(t['seconds'] for t in tests)


class Results:
    def __init__(self, root):
        self.tests, self.errors = [], []
        for key, project, path in find_reports(root):
            try:
                self.tests += [dict(case, task=key, project=project) for case in parse(path)]
            except (ET.ParseError, OSError, LookupError, ValueError) as exc:
                self.errors.append(f'{path.relative_to(root)}: {exc}')
        # Run order: task, suite timestamp, then file and in-file position (stable sort).
        self.tests.sort(key=lambda t: (t['task'], t['started']))
        self.ran = [t for t in self.tests if t['outcome'] != 'skipped']
        typical = statistics.median(t['seconds'] for t in self.ran) if self.ran else 0
        self.cutoff = round(max(SLOW_FLOOR, SLOW_FACTOR * typical), 3)
        for t in self.tests:
            t['slow'] = t['outcome'] != 'skipped' and t['seconds'] >= self.cutoff
        self.slow = [t for t in self.tests if t['slow']]
        self.failures = [t for t in self.tests if t['outcome'] == 'failed']
        self.by_task = {}
        for t in self.tests:
            self.by_task.setdefault(t['task'], []).append(t)
        self.broken = bool(self.errors or self.failures)
        counts = count(self.tests)
        parts = [f'{counts[o]} {o}' for o in OUTCOMES if counts[o] or o == 'passed']
        if self.errors:
            parts.insert(0, f'{len(self.errors)} unreadable')
        self.title = ', '.join(parts)


def el(tag, *children, **attrs):
    """Element from strings and elements in order. Untrusted text goes in only as a string child; markup() escapes it."""
    node = ET.Element(tag, attrs)
    last = None
    for child in children:
        if not isinstance(child, str):
            node.append(child)
            last = child
        elif last is None:
            node.text = (node.text or '') + child
        else:
            last.tail = (last.tail or '') + child
    return node


def markup(node):
    # ASCII with numeric references: writing cannot fail on the locale encoding or on unencodable file names.
    return ET.tostring(node, encoding='ascii', method='html').decode()


def code(text):
    return el('code', ' '.join(text.split()))


def trim(detail):
    # A blank line would end the surrounding HTML block in the markdown summary.
    lines = [cut for cut in (line[:MAX_LINE].rstrip() for line in detail.splitlines()) if cut]
    return '\n'.join(lines[:MAX_LINES] + ['...'] * (len(lines) > MAX_LINES))


def short(test):
    return test['cls'].rsplit('.', 1)[-1] + ' > ' + test['name']


def named(test):
    return code(test['task'][:MAX_NAME]), ' ', code(short(test))


def unreadable(error):
    return 'Unreadable report: ', code(error[:MAX_LINE])


def make_summary(results):
    """Markdown with untrusted text only inside HTML blocks, where GitHub parses no markdown. ASCII, so chars == bytes."""
    def row(label, group):
        c = count(group)
        numbers = [str(len(group)), str(c['passed']), str(c['skipped']),
                   el('b', str(c['failed'])) if c['failed'] else '0', f'{total_seconds(group):.1f}s']
        return markup(el('tr', el('td', label), *(el('td', n, align='right') for n in numbers)))

    out = [HEADING.format(results.title), '', '<table>',
           markup(el('tr', *(el('th', name) for name in ('Task', 'Tests', 'Passed', 'Skipped', 'Failed', 'Time'))))]
    total = [row(el('b', 'Total'), results.tests), '</table>', '']
    room = MAX_SUMMARY - sum(len(text) + 1 for text in out + total) - len(OMITTED) - 10
    omitted = max(0, len(results.failures) - MAX_FAILURES)

    def add(*lines):
        nonlocal room, omitted
        cost = sum(len(text) + 1 for text in lines)
        if cost > room:
            omitted += 1
        else:
            out.extend(lines)
            room -= cost

    for task, group in results.by_task.items():
        add(row(code(task), group))
    out += total
    for t in results.failures[:MAX_FAILURES]:
        add(markup(el('details', el('summary', el('b', 'Failed'), ' ', *named(t)),
                    '\n', el('pre', trim(t['detail'])), '\n')), '')
    for error in results.errors:
        add(markup(el('p', *unreadable(error))))
    if omitted:
        out += ['', OMITTED.format(omitted)]
    return '\n'.join(out)


def make_comment(results):
    entries = [named(t) for t in results.failures] + [unreadable(error) for error in results.errors]
    items = [el('li', *entry) for entry in entries[:MAX_LISTED]]
    if len(entries) > MAX_LISTED:
        items.append(el('li', f'and {len(entries) - MAX_LISTED} more'))
    return HEADING.format(results.title) + ('\n\n' + markup(el('ul', *items)) if items else '')


def source_location(test, root):
    """Best effort (path, line)."""
    if not test['cls']:
        return None, None
    outer = test['cls'].split('$', 1)[0]
    parts = [glob.escape(p) for p in outer.split('.')]
    found = sorted(test['project'].glob('/'.join(['src', '*', '*', *parts]) + '.*'))
    if not found:
        return None, None
    frame = re.search(re.escape(outer) + r'[\w$.]*\(' + re.escape(found[0].name) + r':(\d+)\)', test['detail'])
    return found[0].relative_to(root).as_posix(), frame.group(1) if frame else None


def command_escape(value, prop=False):
    value = str(value).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    return value.replace(':', '%3A').replace(',', '%2C') if prop else value


def annotate(results, root):
    for t in results.failures[:MAX_LISTED]:
        path, line = source_location(t, root)
        props = dict(title=short(t), file=path, line=line)
        joined = ','.join(f'{k}={command_escape(v, True)}' for k, v in props.items() if v)
        message = (t['detail'].strip().splitlines() or ['Test failed'])[0][:MAX_LINE]
        print(f'::error {joined}::{command_escape(message)}')


def make_html(results):
    def row(t, show_task=False):
        cell = el('td', el('b', t['name']), el('small', t['cls'] + (' - ' + t['task'] if show_task else '')))
        if t['outcome'] == 'failed':
            cell.append(el('details', el('pre', t['detail']), open=''))
        elif t['detail']:
            cell.append(el('small', t['detail'].splitlines()[0][:MAX_LINE], **{'class': 'skipped'}))
        seconds = f'{t["seconds"]:.3f}'
        attrs = {'data-o': t['outcome'], 'data-t': seconds}
        if t['slow']:
            attrs['data-slow'] = ''
        return el('tr', el('td', t['outcome'], **{'class': t['outcome']}), cell, el('td', seconds + 's'), **attrs)

    def table(tests, show_task=False):
        return el('table', el('tbody', *(row(t, show_task) for t in tests)))

    meta = ' - '.join(os.environ[k] for k in ('GITHUB_REPOSITORY', 'HEAD_SHA', 'GITHUB_RUN_ID') if os.environ.get(k))
    head = [el('p', results.title), el('p', el('small', meta))]
    head += [el('p', 'Unreadable report: ' + error, **{'class': 'failed'}) for error in results.errors]
    head.append(el('p', f'Slow: {results.cutoff:.3f}s or more ({len(results.slow)} of {len(results.ran)} executed tests, '
                        f'{total_seconds(results.slow):.1f}s of {total_seconds(results.ran):.1f}s)'))

    body = []
    if results.failures:
        body.append(el('section', el('h2', 'Failed'), table(results.failures, True), id='failed'))
    for task, group in results.by_task.items():
        rest = [t for t in group if t['outcome'] != 'failed']
        if rest:
            c = count(rest)
            slow = sum(t['slow'] for t in rest)
            stats = f' - {c["passed"]} passed, {c["skipped"]} skipped, {total_seconds(rest):.1f}s' + (f', {slow} slow' if slow else '')
            body.append(el('details', el('summary', el('b', task), stats), table(rest), **{'class': 'g'}))

    parts = {name: '\n'.join(map(markup, nodes)) for name, nodes in (('head', head), ('body', body))}
    template = Path(__file__).with_name('test_report.html').read_text()
    return re.sub(r'\{\{(head|body)\}\}', lambda m: parts[m.group(1)], template)


def render():
    root = Path.cwd()
    results = Results(root)
    if not (results.tests or results.errors):
        print('No test results found')
        return
    summary = make_summary(results)
    out = report_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.md').write_text(summary)
    (out / 'comment.md').write_text(make_comment(results))
    (out / 'report.html').write_text(make_html(results))
    (out / 'result.json').write_text(json.dumps(dict(title=results.title, broken=results.broken)))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'report={out / "report.html"}\n')
    annotate(results, root)
    print(results.title)


def gh(*args, body=None):
    data = None if body is None else json.dumps(body)
    done = subprocess.run(['gh', 'api', *args] + (['--input', '-'] if data else []), input=data, text=True,
                          capture_output=True)
    if done.returncode:
        raise RuntimeError(f'gh api {" ".join(args)}: {done.stderr.strip()}')
    return done.stdout


def publish():
    env = os.environ
    repo, sha = env['GITHUB_REPOSITORY'], env['HEAD_SHA']
    result = json.loads((report_dir() / 'result.json').read_text())
    title, broken = result['title'], result['broken']
    state = 'failure' if broken else 'success'
    run_url = f'{env["GITHUB_SERVER_URL"]}/{repo}/actions/runs/{env["GITHUB_RUN_ID"]}'
    links = f'[Run and logs]({run_url})'
    if env.get('REPORT_URL'):
        links += f' - [View report (all tests)]({env["REPORT_URL"]})'
    body, short_body = (f'{links}\n\n' + (report_dir() / name).read_text() for name in ('summary.md', 'comment.md'))
    problems = []

    def attempt(fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except (RuntimeError, ValueError, KeyError) as exc:
            problems.append(exc)

    if env['CAN_PUBLISH'] == 'true':
        attempt(gh, '-X', 'POST', f'repos/{repo}/statuses/{sha}', body=dict(
            state=state, context=STATUS_CONTEXT, description=title, target_url=run_url))
        pr = env.get('PR_NUMBER')
        if pr and env.get('DISABLE_COMMENT') != 'true':
            attempt(comment, repo, pr, sha, broken, f'{MARKER}\n{short_body}')
    with open(env['GITHUB_STEP_SUMMARY'], 'a') as step:
        step.write(body + '\n')

    for problem in problems:
        print(f'::warning title=Publishing test results::{command_escape(problem)}')
    if problems:
        sys.exit(1)


def comment(repo, pr, sha, broken, body):
    # A re-run of an old run must not overwrite a newer head's result.
    if json.loads(gh(f'repos/{repo}/pulls/{pr}'))['head']['sha'] != sha:
        print('PR head has moved on; not commenting')
        return
    mine = f'.[] | select(.user.login == "github-actions[bot]" and ((.body // "") | startswith("{MARKER}"))) | .id'
    existing = gh('--paginate', f'repos/{repo}/issues/{pr}/comments?per_page=100', '--jq', mine).split()
    if existing:
        gh('-X', 'PATCH', f'repos/{repo}/issues/comments/{existing[0]}', body=dict(body=body))
    elif broken:
        gh('-X', 'POST', f'repos/{repo}/issues/{pr}/comments', body=dict(body=body))


if __name__ == '__main__':
    # Annotations carry test names and paths; printing them must not depend on the locale.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    if sys.argv[1:] == ['render']:
        render()
    elif sys.argv[1:] == ['publish']:
        publish()
    else:
        sys.exit('usage: test_results.py render | publish')
