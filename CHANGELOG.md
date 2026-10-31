# Changelog

## 0.4.0 — initial public source snapshot (unreleased)

- Experimental Clang frontend plugin, LLVM instrumentation pass and checking
  runtime for unchanged C++ source.
- Explicit `strict-v1` source, preprocessor and driver admission policy for a
  small C++17/GNU++17 subset on macOS arm64.
- Archived JSON-AST backend for studying the earlier source-rewriting approach.
- Detailed architecture, safety limits, examples and compiler diagnostics.
- A 185-test regression suite and nine sanitizer-runtime examples.
- A historical 4,386-cell standards/edge-case audit. It retains 328 new failure
  results and 608 known-failure results; this version is not a full safety
  implementation. See [the report](docs/TEST_REPORT.md) for exact scope.
- GitHub CI, manual differential audits, contribution templates, portable audit
  evidence and the MIT license.

This entry describes the prepared source snapshot. No published GitHub release
or tag is implied; deployment and publication dates are recorded when they occur.
