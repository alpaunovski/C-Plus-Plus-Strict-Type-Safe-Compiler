#!/usr/bin/env python3
"""Check publishable source, Markdown targets and historical audit consistency.

This lightweight CI job needs only Python and Git. It deliberately does not build
LLVM plugins, execute C++ fixtures or interpret passing audit counts as a proof.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

from export_audit import validate_results

ROOT = Path(__file__).resolve().parents[1]


def source_files():
    """Inspect tracked files and new source files, excluding generated local trees."""
    result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            cwd=ROOT, check=True, capture_output=True)
    names = sorted(set(result.stdout.decode().split('\0')) - {''})
    paths = [ROOT / name for name in names]
    for path in paths:
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ('build', '.strictcpp', '.venv'):
            raise ValueError(f'generated/local artifact included in source: {relative}')
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'expected an ordinary source file: {relative}')
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError(f'oversized source artifact: {relative}')
    return paths


def check_ignored_paths():
    """Catch accidental ignore-rule regressions before large/local files are added."""
    probes = ['build/native/runtime.o', '.strictcpp/toolchains/example.tar.xz',
              '.venv/pyvenv.cfg', '.env', '.env.local', '.DS_Store', '__pycache__/x.pyc']
    result = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin'], cwd=ROOT,
                            input='\n'.join(probes) + '\n', text=True, capture_output=True)
    if result.returncode != 0 or set(result.stdout.splitlines()) != set(probes):
        raise ValueError('generated tooling, local environments or outputs lost their ignore rules')


def check_markdown(path):
    """Check checkout-local file targets without crawling the web or generated build/."""
    contents = path.read_text()
    # Compiler-command examples can contain Markdown-like C++/shell syntax. Only
    # links outside fenced code are navigation that must resolve in a fresh clone.
    contents = re.sub(r'(?ms)^```[^\n]*\n.*?^```[^\n]*$', '', contents)
    for target in re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', contents):
        target = target.strip().strip('<>')
        if target.startswith(('#', 'http://', 'https://', 'mailto:')):
            continue
        relative = unquote(target.split('#', 1)[0])
        destination = (path.parent / relative).resolve()
        if not destination.is_relative_to(ROOT):
            raise ValueError(f'{path.relative_to(ROOT)}: link leaves the repository: {target}')
        if not destination.exists() or destination.is_relative_to(ROOT / 'build'):
            raise ValueError(f'{path.relative_to(ROOT)}: target unavailable in a clone: {target}')


def check_snapshot(folder):
    """Independently reconcile every published cell, count and content digest."""
    count = 0
    for path in sorted(folder.glob('*.summary.json')):
        summary = json.loads(path.read_text())
        results_path = folder / summary['results_file']
        if results_path.parent.resolve() != folder.resolve():
            raise ValueError('snapshot result path must remain in its own directory')
        rows = [json.loads(line) for line in results_path.read_text().splitlines()]
        validate_results(rows, planned=summary['planned'], completed=summary['completed'],
                         counts=summary['counts'])
        names = {case['name'] for case in summary['fixtures']} | {'dialect-smoke'}
        if any(row['case'] not in names for row in rows):
            raise ValueError(f'{path}: result refers to a fixture absent from the frozen catalog')
        # Only portable keys belong in the public evidence. New raw fields must
        # be reviewed explicitly rather than copied from future runner metadata.
        if any(key in row for row in rows for key in ('source', 'logs', 'diagnostic_excerpt', 'coverage')):
            raise ValueError(f'{path}: machine-local evidence leaked into the snapshot')
        count += len(rows)
    manifest = folder / 'SHA256SUMS'
    for line in manifest.read_text().splitlines():
        digest, name = line.split('  ', 1)
        if Path(name).name != name or len(digest) != 64:
            raise ValueError(f'{manifest}: malformed content manifest')
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'{manifest}: content digest mismatch for {name}')
    return count


def main():
    """Fail on an actual publication defect and print a compact verification result."""
    required = ('README.md', 'LICENSE', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md',
                'docs/PUBLISHING.md', '.github/workflows/ci.yml', '.github/workflows/audit.yml')
    for name in required:
        if not (ROOT / name).is_file():
            raise ValueError(f'missing publication file: {name}')
    paths = source_files()
    check_ignored_paths()
    python_files = [path for path in paths if path.suffix == '.py' or path == ROOT / 'bin/strict++']
    for path in python_files:
        ast.parse(path.read_text(), filename=str(path))
    markdown = [path for path in paths if path.suffix == '.md']
    for path in markdown:
        check_markdown(path)
    for path in (ROOT / '.github/workflows').glob('*.yml'):
        references = re.findall(r'^\s*- uses:\s+(\S+)', path.read_text(), re.M)
        if not references or any(not re.fullmatch(r'actions/[\w-]+@[0-9a-f]{40}', item) for item in references):
            raise ValueError(f'{path}: actions must reference an immutable full commit SHA')
    snapshots = [path.parent for path in (ROOT / 'docs/audits').glob('*/SHA256SUMS')]
    if not snapshots:
        raise ValueError('no portable historical audit evidence found')
    cells = sum(check_snapshot(folder) for folder in snapshots)
    if not (ROOT / 'bin/strict++').stat().st_mode & 0o111:
        raise ValueError('bin/strict++ lost its executable mode')
    size = sum(path.stat().st_size for path in paths)
    print(f'Checked {len(paths)} source files ({size:,} bytes), {len(python_files)} Python files, '
          f'{len(markdown)} Markdown files and {cells:,} historical audit cells.')
    print('Local documentation targets, ignore rules, action pins and audit counts/digests are valid.')


if __name__ == '__main__':
    main()
