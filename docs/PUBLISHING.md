# Publishing this source on GitHub

The prepared project is **Strict C++ 0.4.0**, an experimental compiler source
snapshot licensed under MIT. It has known missed checks and compatibility
failures. Publish the current status and [audit report](TEST_REPORT.md) with the
source; do not describe this version as a complete or production safety compiler.

This guide describes the remaining GitHub steps. Preparing these files does
not create a remote repository, push source, create a release or run hosted CI.

## Prepared repository contents

- [README](../README.md), [MIT license](../LICENSE),
  [contribution guide](../CONTRIBUTING.md), [security policy](../SECURITY.md)
  and [changelog](../CHANGELOG.md).
- Immutable action references, read-only workflow permissions and a regression
  workflow for the validated native architecture.
- A separate manually dispatched differential audit that keeps failures visible
  and uploads available evidence after a failed test step.
- Bug/feature issue templates, a pull request template and action dependency updates.
- A [portable historical audit snapshot](audits/2026-10-31/README.md) with all
  4,386 outcome rows, frozen fixtures, feature macros and content digests.
- Ignore rules for local toolchains, builds, environments and native artifacts.

LLVM is a separately downloaded dependency. Its distribution and licenses stay
with that distribution; the project MIT license does not relicense LLVM.
No LLVM archive, extracted compiler, project executable or native plugin belongs
in this source repository.

## Verify the source snapshot

From the project root:

```sh
python3 scripts/check_repository.py
python3 native/bootstrap.py
make test
make demo strict-demo
make verify-runtime
```

The lightweight repository check validates Python syntax, checkout-local
documentation links, action commit references, generated-file ignore rules,
and every historical audit row/count/digest. It does not execute C++ or validate
GitHub workflow semantics. Workflow YAML and runner behavior require their own
checks and the first hosted run.

The main regression suite currently has two documented expected failures; the
separate audit intentionally returns nonzero while open failures remain. Run
the longer audit commands individually if reproducing the full evidence:

```sh
make test-standards
make test-boundaries
```

Keep test runs serial within one checkout. Retain generated evidence locally
under `build/`, and use `scripts/export_audit.py` only when intentionally creating
a new historical snapshot; do not overwrite the 2026-10-31 results with a new run.

## Initial commit and remote

The local repository currently starts on `main` without an initial commit.
If starting from an extracted source bundle instead, first run `git init -b main`
inside its top-level directory. The bundle contains source, not Git history.
Review the source that will enter the first commit:

```sh
git status --short
git add .
git update-index --chmod=+x bin/strict++
git diff --cached --stat
git diff --cached --check
git commit -m "Initial experimental Strict C++ compiler"
```

Create an **empty** GitHub repository named `strict-cpp` under the intended
account or organization. Choose public or private visibility. Leave GitHub's
README/license/gitignore initialization disabled because those files are already
included here. Then replace `YOUR_ACCOUNT` in the remote URL and push:

```sh
git remote add origin git@github.com:YOUR_ACCOUNT/strict-cpp.git
git push -u origin main
```

Alternatively, after the local initial commit, GitHub CLI can create and push a
public repository in one explicitly publishing command:

```sh
gh repo create strict-cpp --public --source=. --remote=origin --push \
  --description "Experimental C++ safety instrumentation using Clang/LLVM and a conservative strict C++17 subset"
```

Use `--private` instead of `--public` if that is the intended visibility. The
CLI command assumes no `origin` already exists. Authentication and the target
account are selected through the local GitHub CLI configuration.

Suggested topics are `cpp`, `clang`, `llvm`, `compiler`, `type-safety`,
`memory-safety` and `experimental`. The default branch is `main`.

## What Actions will run

The [CI workflow](../.github/workflows/ci.yml) runs on pushes, pull requests and
manual dispatch. It checks source/docs with Python 3.10 and 3.14 on Linux, then
builds and tests the compiler serially on a `macos-26` arm64 runner. That label's
architecture is documented in the [GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners);
the workflow also checks `uname -m` before downloading LLVM.

It caches the compressed 1.4 GiB LLVM archive, verifies the pinned SHA-256 on
every bootstrap, and rebuilds plugins/runtime inside that runner. The 7.8 GiB
extracted LLVM prefix and compiler build products are not cached. The pinned
LLVM tools also compile the AST tests. Sanitizer linking explicitly uses Apple's
compiler from `xcrun`, whose platform sanitizer runtime works on the tested host.
The [macOS 26 runner image](https://github.com/actions/runner-images/blob/main/images/macos/macos-26-arm64-Readme.md)
currently supplies Clang/LLVM 21, matching the emitted bitcode generation.
The workflow sets `SDKROOT` from `xcrun` so standalone LLVM can find platform headers.
Its AST compiler selection differs from the historical report's Apple Clang controls and
must not be described as an identical rerun of that historical environment.

The workflow uploads regression/sanitizer logs for 14 days. Its read-only token
does not publish releases or push changes. Actions are pinned to verified full
commit SHAs; Dependabot proposes updates through pull requests.

The [standards audit workflow](../.github/workflows/audit.yml) is manual because
it is slower and currently ends in failure for open defects. Start it from the
Actions tab after the first push. The supplement still runs when the main
matrix fails; available sources, commands, IR, reports and logs are retained
as an artifact for seven days. The workflow does not use `continue-on-error`
or rewrite failures into passing results. Its first cold run may download LLVM
when no regression-workflow cache is available.

Enable Actions for the repository and review the first hosted CI outcome before
requiring its checks on `main`. Hosted workflows have not run merely because
their YAML exists locally. Do not require the intentionally failing broad audit
as a passing regression gate while the reported defects remain unresolved.

Enable private vulnerability reporting in the repository's security settings
if the intended account supports it, so the path described in SECURITY.md is
available. Configure branch protection only after the actual check names and
permissions have been confirmed on GitHub.

## Optional first release

After the initial hosted checks and source review, a source release may be
tagged `v0.4.0`. Update the changelog's unreleased status when an actual release
is made. Mark the GitHub release as a **prerelease** and include:

- The experimental safety status and link to the strict-profile limitations.
- macOS arm64/LLVM 21.1.8 requirements and the large bootstrap download size.
- The regression, sanitizer and differential-audit results, including failures.
- Source setup commands and links to the frozen audit snapshot.

GitHub can generate source archives from the tag. This preparation does not
provide portable binary packages or claim support for untested platforms.

## Local preparation verification

The publication preparation was checked locally on macOS arm64 with the pinned
LLVM 21.1.8, explicit SDK selection, and Apple Clang 21 for sanitizer linking:

| Check | Observed result |
| --- | --- |
| `make check` | Source syntax, local documentation, ignore rules, action pins and all 4,386 historical audit cells are consistent. |
| `actionlint` 1.7.12 | Both workflows pass; ShellCheck integration was disabled. The official linter archive digest was verified before execution. |
| Workflow and Dependabot YAML parsing | All three YAML files parse successfully. |
| Python 3.10 syntax compatibility | All 21 Python source files parse using the minimum-version grammar. |
| Regression suite with pinned LLVM AST compiler and `SDKROOT` | 185 tests: 183 pass and two documented expected failures; exit 0. |
| Experimental and strict demos | Expected successful/trapped results; exit 0. |
| Apple ASan/UBSan runtime verification | All nine examples pass; exit 0. |
| Portable audit export | Reproduces all four data files and their digest manifest byte-for-byte; rejects incomplete or falsified-count inputs. |

The actual GitHub runner, cold toolchain bootstrap and hosted artifact upload
remain to be verified after the first push. These local checks do not close the
compiler defects documented in the historical audit.
