# Historical audit snapshot — 2026-10-31

These files preserve the completed local audit summarized in
[TEST_REPORT.md](../../TEST_REPORT.md). They are historical observations of the
recorded compiler/toolchain, not a safety proof or an automatically updated baseline.

| Suite | Summary, feature macros and frozen source fixtures | Complete portable outcome rows |
| --- | --- | --- |
| Main, 3,858 cells | [main.summary.json](main.summary.json) | [main.results.jsonl](main.results.jsonl) |
| Supplement, 528 cells | [supplement.summary.json](supplement.summary.json) | [supplement.results.jsonl](supplement.results.jsonl) |

Across both suites there are 3,178 passes, 266 deliberate policy rejections,
328 newly exposed failure results, 608 known-failure results and six unavailable
alias probes. Every result row survives the export. Known failures are not passes.

The JSONL files contain one object per cell, identified by `profile`, `standard`,
`optimization` and `case`. They retain expected/actual classification, observed
checker diagnostic names, and compile/run exit codes and timeout facts. Frozen
fixture sources, minimum language versions, profile eligibility and expected
diagnostics are included in each summary. `dialect-smoke` is the runner's
additional `int main(){return 0;}` probe rather than a catalog fixture.

The export intentionally omits absolute artifact paths, argv, raw stdout/stderr,
coverage trees, temporary files, IR and executables. The richer local records
remain under `build/standards-matrix/` and `build/standards-supplement/` on the
original machine; they are not included in this repository. A new full run
recreates those richer artifacts using the checked-in runners. No assertion
that the public snapshot contains every raw diagnostic is intended.

The [regression transcript](regressions.txt) and
[sanitizer transcript](runtime-sanitizers.txt) record the companion runs.
The [SHA-256 manifest](SHA256SUMS) covers the four exported JSON/JSONL files.
`python3 scripts/check_repository.py` reconciles their counts and verifies the
manifest. The summaries also retain the original runners' source fingerprints;
their scope and limits are described in the main report.

To create a different dated snapshot after completing both audit suites:

```sh
python3 scripts/export_audit.py --main build/standards-matrix \
  --supplement build/standards-supplement --output docs/audits/YYYY-MM-DD
```

Choose the actual new run date instead of `YYYY-MM-DD`. This exporter refuses
incomplete or count-inconsistent runs. Review the resulting evidence and
documentation together; do not overwrite these historical results when changing
the compiler. The implementation and the fixture catalog were unchanged during
the original audit; subsequent publication metadata does not change its outcomes.
