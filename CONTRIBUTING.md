# Contributing to Strict C++

Strict C++ is a research compiler with known missed checks and false positives.
Read the [safety contract](docs/SAFETY.md), [strict profile](docs/STRICT.md) and
[audit report](docs/TEST_REPORT.md) before changing the admitted language or
describing a new guarantee. Contributions are provided under the [MIT license](LICENSE).

## Set up and verify

The validated platform is macOS arm64 with LLVM 21.1.8. Install Apple's command
line developer tools and Python 3.10 or newer, then run:

```sh
python3 native/bootstrap.py
python3 scripts/check_repository.py
make test
make demo strict-demo
make verify-runtime
```

The bootstrap downloads about 1.4 GiB and extracts about 7.8 GiB of local LLVM
tooling into the ignored `.strictcpp/` directory. An existing matching LLVM
developer distribution can instead be selected with `STRICTCPP_LLVM`; see the
[setup instructions](README.md#requirements-and-toolchain).

For the same compiler selection as CI, use the pinned LLVM tools for the AST
backend as well as the native plugins, and Apple's compatible sanitizer compiler:

```sh
export STRICTCPP_LLVM="$PWD/.strictcpp/toolchains/LLVM-21.1.8-macOS-ARM64"
export PATH="$STRICTCPP_LLVM/bin:$PATH"
export STRICTCPP_SANITIZER_CXX="$(xcrun --find clang++)"
export SDKROOT="$(xcrun --show-sdk-path)"
make test
make verify-runtime
```

The full differential audits are slower and currently return nonzero because
they preserve unresolved failures. Run each command separately and serially:

```sh
make test-standards
make test-boundaries
```

The native build cache is not concurrent. Do not run native compiler suites
against the same checkout at the same time. The Python/documentation checks
need neither LLVM nor a macOS host. No third-party Python dependencies are
required for the compiler, test runners or repository checks.

## Changes and evidence

Keep changes focused on one behavior. Explain the triggering source program,
the behavior before and after, and the checks you ran. A compiler/runtime
change should include a minimized regression for its meaningful behavior.
Preserve existing fixture strings unless the source change itself is needed.
Do not apply a formatter to generated C++ fixtures as a side effect.

When extending strict admission, describe the explicit source and driver rules,
show defined positive cases and rejected or trapped negative cases, and update
the profile documentation. A zero known-gap count is insufficient evidence for
admitting a previously unsupported construct. Keep source type distinctions,
evaluation order, provenance and lifetime behavior in view across optimization.

Known failures must remain visible. Do not add an expected-failure annotation
merely to make CI green. If a documented witness is fixed, remove its annotation
deliberately and retain the now-passing regression. The standalone audits count
known failures as failures and must continue to do so.

The checked-in [audit snapshot](docs/audits/2026-10-31/README.md) is historical
evidence, not a rolling baseline. Put a new audit in a new dated directory when
publishing changed results. Preserve the complete local output when investigating
a failure; do not commit toolchains, executables, raw temporary trees or build logs.

## Issues and pull requests

For a bug, include a small unchanged C++ source, exact `strict++` command,
backend/profile, compiler version, platform, expected result and actual diagnostic.
Distinguish missed checks from valid-code rejection and deliberate strict-policy
rejection. For a feature, include a concrete source example and the evidence
needed to model it safely.

Use the issue and pull request templates. See [SECURITY.md](SECURITY.md) for
reporting sensitive problems. Maintainers review changes without a guaranteed
response time; this project does not currently offer production support.
