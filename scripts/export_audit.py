#!/usr/bin/env python3
"""Export complete local audit outcomes without publishing machine-local logs.

The main runners retain rich evidence under build/. This exporter copies a
fixed allowlist of portable fields and the frozen fixture catalog into a dated
documentation snapshot. Every outcome survives; known failures remain failures.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


# Explicit field lists prevent newly added raw paths, environment data or diagnostic
# excerpts from silently becoming part of a later public snapshot.
ROW_FIELDS = ('profile', 'standard', 'year', 'optimization', 'case', 'category',
              'expectation', 'known_limitation', 'status', 'detail', 'observed_diagnostic')
STAGES = ('reference_compile', 'reference_run', 'target_compile', 'target_run')
TOOL_FIELDS = ('native', 'ast', 'host', 'machine', 'python')
STATUSES = {'pass', 'policy_rejection', 'failure', 'known_failure',
            'reference_failure', 'toolchain_unavailable'}


def validate_results(rows, *, planned, completed, counts):
    """Refuse partial or internally inconsistent evidence instead of trimming it."""
    if completed != planned or completed != len(rows):
        raise ValueError('only a complete audit may be exported as a completed snapshot')
    actual = Counter(row['status'] for row in rows)
    if set(actual) - STATUSES or dict(actual) != counts:
        raise ValueError('recorded outcome counts do not match the individual cells')
    keys = [(row['profile'], row['standard'], row['optimization'], row['case']) for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError('duplicate profile/standard/optimization/fixture cell')


def export_suite(source, destination, name):
    """Keep exact status/exit information and fixtures; omit ephemeral raw evidence."""
    document = json.loads((source / 'results.json').read_text())
    validate_results(document['results'], planned=document['planned'],
                     completed=document['completed'], counts=document['counts'])
    rows = []
    for original in document['results']:
        row = {key: original[key] for key in ROW_FIELDS if key in original}
        # Stage exit/timeout facts substantiate the result without copying argv
        # containing absolute source paths, captured streams or environment data.
        for stage in STAGES:
            if stage in original:
                row[stage] = {key: original[stage][key] for key in ('returncode', 'timeout')}
        rows.append(row)
    probes = json.loads((source / 'dialects.json').read_text())
    dialects = {profile: {flag: {key: probe[key] for key in ('accepted', 'features')}
                          for flag, probe in modes.items()}
                for profile, modes in probes.items()}
    metadata = document['metadata']
    summary = {
        'schema_version': 1,
        'suite': name,
        'started_utc': metadata['started_utc'],
        'source_fingerprint': metadata['source_fingerprint'],
        'tools': {key: value for key, value in metadata['tools'].items() if key in TOOL_FIELDS},
        'planned': document['planned'],
        'completed': document['completed'],
        'seconds': document['seconds'],
        'counts': document['counts'],
        'results_file': name + '.results.jsonl',
        'fixtures': json.loads((source / 'catalog.json').read_text()),
        'dialects': dialects,
        'omitted': ['absolute artifact paths', 'command argument arrays', 'raw stdout/stderr',
                    'coverage reports', 'generated IR', 'executables', 'temporary directories'],
    }
    summary_path = destination / (name + '.summary.json')
    results_path = destination / summary['results_file']
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n')
    # One JSON object per line keeps all thousands of cells directly inspectable
    # without adding tens of thousands of formatting-only lines to repository diffs.
    results_path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    return [summary_path, results_path]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--main', type=Path, default=Path('build/standards-matrix'))
    parser.add_argument('--supplement', type=Path, default=Path('build/standards-supplement'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, source in (('main', args.main), ('supplement', args.supplement)):
        paths.extend(export_suite(source, args.output, name))
    # The manifest covers the exported data bytes only. Documentation can improve
    # independently; changing audit data requires an explicit manifest refresh.
    manifest = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
                       for path in paths)
    (args.output / 'SHA256SUMS').write_text(manifest)
    print(f'Exported both complete suites to {args.output}; failures are preserved.')


if __name__ == '__main__':
    main()
