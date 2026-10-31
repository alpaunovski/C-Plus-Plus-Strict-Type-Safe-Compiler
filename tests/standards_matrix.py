#!/usr/bin/env python3
"""Run a reproducible dialect/optimization/edge-case differential compiler audit.

This intentionally lives outside unittest's test_*.py discovery: a complete
matrix takes longer than the ordinary regression suite and preserves failures
instead of converting documented limitations into passing expectations.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from native.build import build
from standards_cases import ALL_CASES, ARMED, CORE, Case


@dataclass(frozen=True)
class Dialect:
    flag: str
    year: int
    alias: bool = False


@dataclass(frozen=True)
class Job:
    profile: str
    dialect: Dialect
    level: str
    case: Case
    policy_rejection: bool = False


YEARS = [('98', 1998), ('03', 2003), ('11', 2011), ('14', 2014),
         ('17', 2017), ('20', 2020), ('23', 2023), ('26', 2026)]
ALIASES = [('0x', 2011), ('1y', 2014), ('1z', 2017), ('2a', 2020),
           ('2b', 2023), ('2c', 2026), ('2d', 2029)]
CANONICAL = [Dialect(prefix + suffix, year) for prefix in ('c++', 'gnu++') for suffix, year in YEARS]
ALIAS_DIALECTS = [Dialect(prefix + suffix, year, True) for prefix in ('c++', 'gnu++') for suffix, year in ALIASES]
LEVELS = ('O0', 'O1', 'O2', 'O3', 'Os', 'Oz')
CRASH = re.compile(r'PLEASE submit a bug report|Stack dump:|LLVM ERROR:|Assertion .* failed|internal compiler error|clang frontend command failed', re.I)
POLICY = re.compile(r'strict profile rejects|strict-v1 rejects|strict-v1 requires|requires -std=c\+\+17|unsequenced modifications|non-void function')


def command(argv, folder, label, *, timeout, environment, input_text=None):
    """Record complete bounded subprocess evidence without using shell evaluation."""
    start = time.monotonic()
    (folder / (label + '.command.json')).write_text(json.dumps(argv, indent=2) + '\n')
    try:
        result = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                                timeout=timeout, env=environment)
        data = {'returncode': result.returncode, 'timeout': False,
                'seconds': round(time.monotonic() - start, 4)}
        stdout, stderr = result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        data = {'returncode': None, 'timeout': True,
                'seconds': round(time.monotonic() - start, 4)}
        stdout = error.stdout or ''
        stderr = error.stderr or ''
        if isinstance(stdout, bytes): stdout = stdout.decode(errors='replace')
        if isinstance(stderr, bytes): stderr = stderr.decode(errors='replace')
    (folder / (label + '.stdout')).write_text(stdout)
    (folder / (label + '.stderr')).write_text(stderr)
    return {**data, 'stdout': stdout, 'stderr': stderr}


def probe_dialects(compilers, sysroot, folder, environment):
    """Query each actual frontend, retaining feature macros and rejected spellings."""
    probes = {}
    wanted = ('__cplusplus', '__cpp_constexpr', '__cpp_concepts', '__cpp_consteval',
              '__cpp_if_consteval', '__cpp_explicit_this_parameter', '__cpp_pack_indexing')
    for profile, compiler in compilers.items():
        probes[profile] = {}
        for dialect in CANONICAL + ALIAS_DIALECTS:
            destination = folder / profile / dialect.flag.replace('+', 'p')
            destination.mkdir(parents=True, exist_ok=True)
            result = command([compiler, *sysroot, '-std=' + dialect.flag, '-dM', '-E', '-x', 'c++', '-'],
                             destination, 'probe', timeout=30, environment=environment, input_text='')
            macros = {line.split()[1]: ' '.join(line.split()[2:])
                      for line in result['stdout'].splitlines() if line.startswith('#define ')}
            probes[profile][dialect.flag] = {
                'accepted': result['returncode'] == 0 and not result['timeout'],
                'features': {name: macros.get(name) for name in wanted},
                'diagnostic': result['stderr'][:2000],
            }
    return probes


def policy_rejects(profile, dialect):
    """Distinguish deliberate language policy from a compiler compatibility failure."""
    return ((profile == 'ast' and dialect.year < 2017) or
            (profile == 'strict' and dialect.flag not in ('c++17', 'gnu++17')))


def make_jobs(args, probes):
    """Cross applicable fixtures with modes, then add alias and optimization probes."""
    profiles = args.profile or ['native', 'ast', 'strict']
    dialects = [dialect for dialect in CANONICAL if not args.standard or dialect.flag in args.standard]
    levels = args.optimization or ['O0', 'O2']
    cases = [case for case in ALL_CASES if not args.case or case.name in args.case]
    jobs = []
    smoke = Case('dialect-smoke', 'int main(){return 0;}\n', 'dialect')
    for profile in profiles:
        compiler_profile = 'ast' if profile == 'ast' else 'native'
        for dialect in dialects:
            jobs.append(Job(profile, dialect, 'O0', smoke, policy_rejects(profile, dialect)))
            if policy_rejects(profile, dialect) or not probes[compiler_profile][dialect.flag]['accepted']:
                continue
            for case in cases:
                if profile not in case.profiles or dialect.year < case.since:
                    continue
                selected = LEVELS if profile == 'strict' and not args.optimization else levels
                for level in selected:
                    jobs.append(Job(profile, dialect, level, case))
        if not args.no_aliases and not args.standard:
            for dialect in ALIAS_DIALECTS:
                jobs.append(Job(profile, dialect, 'O0', smoke, policy_rejects(profile, dialect)))
                if (profile == 'strict' or policy_rejects(profile, dialect) or
                        not probes[compiler_profile][dialect.flag]['accepted']):
                    continue
                for case in CORE:
                    if case.name in ('scalar-address', 'wrong-type-read'):
                        jobs.append(Job(profile, dialect, 'O0', case))
    # Full-dialect O0/O2 coverage is complemented by focused optimizer stress,
    # avoiding an unnecessary sixfold expansion of every expensive library case.
    if not args.optimization and not args.case:
        selected_cases = {'scalar-address', 'aggregate-two-pointer-return', 'wrong-type-read',
                          'one-past-read', 'deleted-pointer', 'expired-stack', 'signed-char-alias'}
        for profile in profiles:
            if profile == 'strict': continue
            for dialect in dialects:
                if dialect.flag not in ('c++17', 'c++23'): continue
                for case in cases:
                    if case.name in selected_cases:
                        for level in ('O1', 'O3', 'Os', 'Oz'):
                            jobs.append(Job(profile, dialect, level, case))
    unique = {}
    for job in jobs:
        unique[job.profile, job.dialect.flag, job.level, job.case.name] = job
    return list(unique.values())


def run_job(job, compilers, sysroot, probes, destination, environment):
    """Compile a control before judging the target, and never run an unsafe control."""
    relative = Path(job.profile) / job.dialect.flag.replace('+', 'p') / job.level / job.case.name
    folder = destination / relative
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / 'tmp'
    temporary.mkdir(exist_ok=True)
    env = dict(environment, TMPDIR=str(temporary))
    source = folder / job.case.filename
    source.write_text(job.case.source)
    before = source.read_bytes()
    for name, contents in job.case.files:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    reference = folder / 'reference'
    target = folder / 'target'
    # A second run of the same output directory must not confuse stale binaries
    # with output produced by a newly rejected compilation.
    reference.unlink(missing_ok=True)
    target.unlink(missing_ok=True)
    report = folder / 'coverage.json'
    report.unlink(missing_ok=True)
    compiler_profile = 'ast' if job.profile == 'ast' else 'native'
    row = {'profile': job.profile, 'standard': job.dialect.flag, 'year': job.dialect.year,
           'optimization': job.level, 'case': job.case.name, 'category': job.case.category,
           'expectation': 'policy-rejection' if job.policy_rejection else job.case.expectation,
           'source': str((relative / job.case.filename).as_posix()),
           'logs': relative.as_posix(), 'known_limitation': job.profile in job.case.known}

    def finish(status, detail):
        row.update(status=status, detail=detail)
        if source.read_bytes() != before:
            row.update(status='failure', detail='source-modified')
        (folder / 'result.json').write_text(json.dumps(row, indent=2) + '\n')
        return row

    def deviation(detail):
        return finish('known_failure' if row['known_limitation'] else 'failure', detail)

    reference_dialect = probes[compiler_profile][job.dialect.flag]
    if reference_dialect['accepted']:
        control = command([compilers[compiler_profile], *sysroot, '-std=' + job.dialect.flag,
                           '-' + job.level, str(source), '-o', str(reference)], folder, 'reference-compile',
                          timeout=60, environment=env)
        row['reference_compile'] = {key: control[key] for key in ('returncode', 'timeout', 'seconds')}
        if control['timeout'] or control['returncode'] != 0:
            row['diagnostic_excerpt'] = control['stderr'][-2500:]
            return finish('reference_failure', 'control-compile-timeout' if control['timeout'] else 'control-compile-rejected')
        if job.case.expectation == 'valid' and not job.policy_rejection:
            control_run = command([str(reference)], folder, 'reference-run', timeout=10, environment=env)
            row['reference_run'] = {key: control_run[key] for key in ('returncode', 'timeout', 'seconds')}
            if control_run['timeout'] or control_run['returncode'] != 0:
                row['diagnostic_excerpt'] = control_run['stderr'][-2500:]
                return finish('reference_failure', 'control-did-not-complete-successfully')
        else:
            control_run = None
    else:
        control_run = None

    options = ['--sc-backend', 'ast'] if job.profile == 'ast' else []
    if job.profile == 'strict': options.append('--sc-strict')
    argv = [str(ROOT / 'bin/strict++'), '--sc-quiet', *options,
            '-std=' + job.dialect.flag, '-' + job.level, '--sc-report', str(report),
            '--sc-emit-dir', str(folder / 'ir'), str(source), '-o', str(target)]
    built = command(argv, folder, 'target-compile', timeout=60, environment=env)
    row['target_compile'] = {key: built[key] for key in ('returncode', 'timeout', 'seconds')}
    if report.exists():
        try:
            evidence = json.loads(report.read_text())
            row['coverage'] = [{'counts': unit.get('counts'), 'foreign_symbols': unit.get('foreign_symbols'),
                                'limitations': unit.get('limitations')}
                               for unit in evidence.get('translation_units', [])]
        except ValueError:
            return deviation('malformed-coverage-report')
    if built['timeout']:
        return deviation('compiler-timeout')
    if built['returncode'] is not None and (built['returncode'] < 0 or CRASH.search(built['stderr'])):
        row['diagnostic_excerpt'] = built['stderr'][-3000:]
        return deviation('compiler-crash')
    if not reference_dialect['accepted']:
        if built['returncode'] != 0 and not target.exists():
            return finish('toolchain_unavailable', 'dialect-not-accepted-by-reference-or-target')
        return finish('failure', 'unsupported-dialect-was-not-enforced')
    if job.policy_rejection or job.case.expectation == 'policy':
        if built['returncode'] != 0 and not target.exists() and POLICY.search(built['stderr']):
            return finish('policy_rejection', 'deliberate-admission-rejection')
        row['diagnostic_excerpt'] = built['stderr'][-2500:]
        return finish('failure', 'policy-was-not-enforced' if built['returncode'] == 0 else 'unexpected-policy-diagnostic')
    if built['returncode'] != 0:
        row['diagnostic_excerpt'] = built['stderr'][-3000:]
        return deviation('instrumented-compile-rejected')
    run = command([str(target)], folder, 'target-run', timeout=10, environment=env)
    row['target_run'] = {key: run[key] for key in ('returncode', 'timeout', 'seconds')}
    row['observed_diagnostic'] = re.findall(r'strictcpp: ([a-z][a-z-]+)', run['stderr'])
    if run['timeout']:
        return deviation('execution-timeout')
    if job.case.expectation == 'valid':
        if run['returncode'] == 0 and run['stdout'] == control_run['stdout']:
            return finish('pass', 'defined-program-matches-reference')
        row['diagnostic_excerpt'] = run['stderr'][-3000:]
        return deviation('valid-program-runtime-rejection' if run['returncode'] == 86 else 'valid-program-result-mismatch')
    if job.case.armed and ARMED not in run['stderr']:
        row['diagnostic_excerpt'] = run['stderr'][-3000:]
        return deviation('failure-before-unsafe-action')
    if run['returncode'] == 86 and any('strictcpp: ' + name in run['stderr'] for name in job.case.diagnostics):
        return finish('pass', 'intended-safety-check-fired')
    row['diagnostic_excerpt'] = run['stderr'][-3000:]
    if run['returncode'] == 86:
        return deviation('wrong-safety-diagnostic')
    return deviation('unsafe-action-not-diagnosed' if run['returncode'] is not None and run['returncode'] >= 0
                     else 'unsafe-action-reached-os-signal')


def write_summary(destination, metadata, rows, total, elapsed):
    """Checkpoint readable/structured evidence, retaining non-passing outcomes."""
    counts = dict(Counter(row['status'] for row in rows))
    data = {'metadata': metadata, 'planned': total, 'completed': len(rows),
            'seconds': round(elapsed, 3), 'counts': counts, 'results': rows}
    (destination / 'results.json').write_text(json.dumps(data, indent=2) + '\n')
    lines = ['# Standards and edge-case audit', '',
             'This is differential regression evidence, not a C++ conformance suite or safety proof.', '',
             f'- Completed: {len(rows)} / {total} matrix cells.',
             f'- Elapsed: {elapsed:.1f} seconds.',
             '- Undefined-behavior controls were compiled, never executed.',
             '- Known failures and reference failures are not counted as passes.',
             '- Negative compatibility probes arm immediately before the unsafe operation.', '',
             '## Toolchain', '', '```json', json.dumps(metadata['tools'], indent=2), '```', '',
             '## Outcomes', '', '| Outcome | Cells |', '| --- | ---: |']
    lines.extend(f'| {status} | {count} |' for status, count in sorted(counts.items()))
    grouped = defaultdict(Counter)
    for row in rows: grouped[row['profile'], row['standard']][row['status']] += 1
    lines += ['', '## Per profile and standard', '',
              '| Profile | Mode | Pass | Policy rejection | Known failure | Failure | Reference failure | Unavailable |',
              '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for (profile, standard), group in sorted(grouped.items()):
        values = [group[key] for key in ('pass', 'policy_rejection', 'known_failure', 'failure', 'reference_failure', 'toolchain_unavailable')]
        lines.append('| ' + ' | '.join([profile, standard, *map(str, values)]) + ' |')
    failures = [row for row in rows if row['status'] in ('known_failure', 'failure', 'reference_failure')]
    lines += ['', '## Distinct non-passing findings', '',
              'All cells, source files, commands, streams and coverage evidence are retained in this directory.', '',
              '| Profile | Case | Classification | Detail | Cells | First reproduction |',
              '| --- | --- | --- | --- | ---: | --- |']
    findings = defaultdict(list)
    for row in failures: findings[row['profile'], row['case'], row['status'], row['detail']].append(row)
    for (profile, case, status, detail), group in sorted(findings.items()):
        first = group[0]
        label = first['standard'] + ' / ' + first['optimization']
        path = first['logs'] + '/result.json'
        lines.append(f'| {profile} | {case} | {status} | {detail} | {len(group)} | [{label}]({path}) |')
    (destination / 'REPORT.md').write_text('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/standards-matrix')
    parser.add_argument('--profile', action='append', choices=('native', 'ast', 'strict'))
    parser.add_argument('--standard', action='append', choices=[dialect.flag for dialect in CANONICAL])
    parser.add_argument('--optimization', action='append', choices=LEVELS)
    parser.add_argument('--case', action='append', choices=[case.name for case in ALL_CASES])
    parser.add_argument('--no-aliases', action='store_true')
    parser.add_argument('--list-cases', action='store_true')
    args = parser.parse_args()
    if args.list_cases:
        for case in ALL_CASES: print(case.name, case.since, case.category, case.expectation, ','.join(case.profiles))
        return 0
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    llvm, _, _, sysroot = build()
    compilers = {'native': str(llvm / 'bin/clang++'), 'ast': shutil.which('clang++') or 'clang++'}
    tools = {name: subprocess.check_output([compiler, '--version'], text=True).splitlines()[0]
             for name, compiler in compilers.items()}
    tools.update(host=platform.platform(), machine=platform.machine(), python=sys.version.split()[0],
                 llvm_prefix=str(llvm), sysroot=sysroot)
    fingerprint = hashlib.sha256()
    for directory in ('strictcpp', 'native'):
        for path in sorted((ROOT / directory).glob('*')):
            if path.is_file() and path.suffix in ('.py', '.cpp', '.h'):
                fingerprint.update(str(path.relative_to(ROOT)).encode() + path.read_bytes())
    for path in (Path(__file__), Path(__file__).with_name('standards_cases.py')):
        fingerprint.update(path.read_bytes())
    environment = os.environ.copy()
    probes = probe_dialects(compilers, sysroot, destination / 'dialects', environment)
    (destination / 'dialects.json').write_text(json.dumps(probes, indent=2) + '\n')
    jobs = make_jobs(args, probes)
    metadata = {'started_utc': datetime.now(timezone.utc).isoformat(), 'tools': tools,
                'source_fingerprint': fingerprint.hexdigest(),
                'selection': {key: value for key, value in vars(args).items() if key != 'output'}}
    (destination / 'catalog.json').write_text(json.dumps([asdict(case) for case in ALL_CASES], indent=2) + '\n')
    rows = []
    start = time.monotonic()
    last_progress = start
    print(f'Running {len(jobs)} matrix cells; artifacts: {destination}', flush=True)
    with (destination / 'results.jsonl').open('w') as journal:
        for number, job in enumerate(jobs, 1):
            row = run_job(job, compilers, sysroot, probes, destination, environment)
            rows.append(row)
            journal.write(json.dumps(row) + '\n')
            journal.flush()
            now = time.monotonic()
            if number % 25 == 0 or now - last_progress > 15 or number == len(jobs):
                print(f'{number}/{len(jobs)} {dict(Counter(item["status"] for item in rows))} '
                      f'last={job.profile}/{job.dialect.flag}/{job.case.name}', flush=True)
                write_summary(destination, metadata, rows, len(jobs), now - start)
                last_progress = now
    write_summary(destination, metadata, rows, len(jobs), time.monotonic() - start)
    print(f'Report: {destination / "REPORT.md"}', flush=True)
    # A known limitation is evidence against completeness, not a passing cell.
    return int(any(row['status'] in ('failure', 'known_failure', 'reference_failure') for row in rows))


if __name__ == '__main__':
    raise SystemExit(main())
