#!/usr/bin/env python3
"""Audit foreign/control-transfer boundaries and strict subset interactions.

Run separately so newly added investigative cases do not change an already
running matrix's catalog. Both reports retain their own exact source catalog,
commands, compiler controls, fingerprints and non-passing results.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from standards_cases import Case, valid, violation
from standards_matrix import (CANONICAL, LEVELS, Job, ROOT, command, probe_dialects,
                              run_job, write_summary)
from native.build import build


CASES = [
    # The original function contains only trivial automatic objects. Thus the
    # jump itself is legal; the deliberately stale read is the safety violation.
    violation('longjmp-expired-stack', 'if(setjmp(environment)==0)leave();',
              'return *saved;', 'expired-object',
              declarations='#include <csetjmp>\nstatic std::jmp_buf environment;static int* saved;void leave(){int n=7;saved=&n;std::longjmp(environment,1);}',
              category='nonlocal-control', known=('native', 'ast')),
    valid('variadic-scalar', 'return first(1,7)!=7;',
          declarations='#include <cstdarg>\nint first(int count,...){std::va_list args;va_start(args,count);int n=va_arg(args,int);va_end(args);return n;}',
          category='varargs', known=('native',)),
    valid('qsort-scalar-callback', 'int values[3]={3,1,2};std::qsort(values,3,sizeof(int),compare);return values[0]!=1||values[2]!=3;',
          declarations='#include <cstdlib>\nint compare(const void* a,const void* b){int x=*static_cast<const int*>(a);int y=*static_cast<const int*>(b);return (x>y)-(x<y);}',
          category='foreign-callback', known=('native', 'ast')),
    valid('qsort-pointer-callback', 'int a=3,b=1,c=2;int* values[3]={&a,&b,&c};std::qsort(values,3,sizeof(int*),compare);return *values[0]!=1||*values[2]!=3;',
          declarations='#include <cstdlib>\nint compare(const void* a,const void* b){int x=**static_cast<int* const*>(a);int y=**static_cast<int* const*>(b);return (x>y)-(x<y);}',
          category='foreign-shadow-copy', known=('native', 'ast')),
    # Assembly can modify memory without an LLVM store carrying an access type.
    # Use a valid local address so the probe does not depend on an OS fault.
    violation('assembly-const-store', 'const int n=7;int value=9;',
              'asm volatile("str %w1, [%0]"::"r"(&n),"r"(value):"memory");return 0;',
              'write-to-const', category='assembly-extension', known=('native', 'ast')),
]


# Focus on interactions inside the admitted subset, where an unnoticed operation
# would be more serious than an already rejected foreign-language boundary.
# These cases have no arming foreign call: the strict gate would reject it.
STRICT_CASES = [
    valid('strict-bool-roundtrip', 'bool n=true;return *&n!=true;', profiles=('strict',)),
    valid('strict-all-integer-widths', 'long a=7;long long b=7;unsigned long c=7;unsigned long long d=7;return *&a!=7||*&b!=7||*&c!=7u||*&d!=7u;', profiles=('strict',)),
    valid('strict-partial-initializer', 'unsigned a[3]={7};return a[0]!=7u||a[1]!=0u||a[2]!=0u;', profiles=('strict',)),
    valid('strict-unevaluated-uninitialized', 'unsigned n;return sizeof(n)!=4u;', profiles=('strict',)),
    valid('strict-short-circuit-uninitialized', 'bool n;bool take=false;return take&&n;', profiles=('strict',)),
    valid('strict-static-zero', 'return read()!=0u;', declarations='static unsigned n;unsigned read(){return *&n;}', profiles=('strict',)),
    valid('strict-array-alias', 'Values a={3,7};return a[1]!=7u;', declarations='typedef unsigned Values[2];', profiles=('strict',)),
    valid('strict-scalar-one-past', 'unsigned n=7;return (&n+1u)==&n;', profiles=('strict',)),
    valid('strict-defined-division-shift', 'unsigned n=16u;n/=2u;n<<=1u;long value=-7;return n!=16u||value/2!=-3;', profiles=('strict',)),
]

# Declaring the permitted diagnostic per case prevents a different earlier trap
# from silently satisfying the intended dynamic initialization/bounds check.
for name, source, diagnostic in [
    ('bool-uninitialized', 'int main(){bool n;return n;}', 'uninitialized-read'),
    ('long-long-uninitialized', 'int main(){long long n;return n!=0;}', 'uninitialized-read'),
    ('partially-written-array', 'int main(){unsigned a[2];a[0]=7u;return a[1];}', 'uninitialized-read'),
    ('negative-array-index', 'int main(){unsigned a[2]{};return a[-1];}', 'out-of-bounds'),
    ('pointer-offset-overflow', 'int main(){unsigned a[2]{};unsigned long n=1ul<<62;return a[n];}', 'index-overflow'),
    ('outside-pointer-formation', 'int main(){unsigned a[2]{};return (a+3u)==nullptr;}', 'out-of-bounds'),
    ('scalar-one-past-read', 'int main(){unsigned n=7;return *(&n+1u);}', 'out-of-bounds'),
    ('conditional-null-read', 'int main(){unsigned n=7;bool take=false;return *(take?&n:static_cast<unsigned*>(nullptr));}', 'null-access'),
]:
    STRICT_CASES.append(Case('strict-' + name, source, 'strict-interaction', since=2017,
                             expectation='violation', diagnostics=(diagnostic,), profiles=('strict',)))

# Legal grammar must still be refused when arithmetic or interfaces are outside
# the narrow proof rules. The reference compiler only compiles these controls.
for name, source in [
    ('negative-divisor', 'int main(){int n=-7;return n/-1;}'),
    ('shift-type-width', 'int main(){unsigned n=1;return n<<32u;}'),
    ('volatile-object', 'int main(){volatile unsigned n=7;return n;}'),
    ('variadic-interface', 'unsigned f(unsigned n,...){return n;}int main(){return f(0u,7u);}'),
    ('assembly-boundary', 'int main(){asm volatile("");return 0;}'),
    ('signed-overflow-constant', 'int main(){int n=2147483647;return n+1;}'),
    ('pointer-difference', 'int main(){unsigned a[2]{};return &a[1]-&a[0];}'),
]:
    STRICT_CASES.append(Case('strict-reject-' + name, source, 'strict-interaction-admission',
                             since=2017, expectation='policy', profiles=('strict',)))


def main():
    """Run isolated cells serially, preserving every failed expectation as evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/standards-supplement')
    parser.add_argument('--case', action='append', choices=[case.name for case in CASES + STRICT_CASES])
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    llvm, _, _, sysroot = build()
    compilers = {'native': str(llvm / 'bin/clang++'), 'ast': shutil.which('clang++') or 'clang++'}
    environment = os.environ.copy()
    tools = {}
    for profile, compiler in compilers.items():
        probe = command([compiler, '--version'], destination, profile + '-version',
                        timeout=30, environment=environment)
        tools[profile] = probe['stdout'].splitlines()[0]
    tools.update(llvm_prefix=str(llvm), sysroot=sysroot)
    probes = probe_dialects(compilers, sysroot, destination / 'dialects', environment)
    (destination / 'dialects.json').write_text(json.dumps(probes, indent=2) + '\n')
    cases = [case for case in CASES + STRICT_CASES if not args.case or case.name in args.case]
    # The parent matrix already checks all unsupported-mode rejections and alias
    # spellings. Here only applicable canonical modes need boundary executions.
    jobs = [Job(profile, dialect, level, case)
            for profile in compilers for dialect in CANONICAL
            if probes[profile][dialect.flag]['accepted'] and (profile != 'ast' or dialect.year >= 2017)
            for case in cases if profile in case.profiles for level in ('O0', 'O2')]
    jobs.extend(Job('strict', dialect, level, case)
                for dialect in CANONICAL if dialect.flag in ('c++17', 'gnu++17')
                for case in cases if 'strict' in case.profiles for level in LEVELS)
    fingerprint = hashlib.sha256()
    for directory in ('strictcpp', 'native'):
        for path in sorted((ROOT / directory).glob('*')):
            if path.is_file() and path.suffix in ('.py', '.h', '.cpp'):
                fingerprint.update(path.read_bytes())
    for filename in ('standards_matrix.py', 'standards_cases.py', 'standards_supplement.py'):
        fingerprint.update(Path(__file__).with_name(filename).read_bytes())
    metadata = {'started_utc': datetime.now(timezone.utc).isoformat(), 'tools': tools,
                'source_fingerprint': fingerprint.hexdigest(), 'selection': {'suite': 'supplement'}}
    (destination / 'catalog.json').write_text(json.dumps([asdict(case) for case in cases], indent=2) + '\n')
    rows = []
    start = time.monotonic()
    print(f'Running {len(jobs)} supplemental cells; artifacts: {destination}', flush=True)
    with (destination / 'results.jsonl').open('w') as journal:
        for number, job in enumerate(jobs, 1):
            row = run_job(job, compilers, sysroot, probes, destination, environment)
            rows.append(row)
            journal.write(json.dumps(row) + '\n')
            journal.flush()
            if number % 20 == 0 or number == len(jobs):
                print(f'{number}/{len(jobs)} {dict(Counter(item["status"] for item in rows))}', flush=True)
                write_summary(destination, metadata, rows, len(jobs), time.monotonic() - start)
    write_summary(destination, metadata, rows, len(jobs), time.monotonic() - start)
    return int(any(row['status'] in ('failure', 'known_failure', 'reference_failure') for row in rows))


if __name__ == '__main__':
    raise SystemExit(main())
