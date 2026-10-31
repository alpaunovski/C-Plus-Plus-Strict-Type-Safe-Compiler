# Standards and safety audit

This report records a local differential audit of the experimental compiler.
The audit exposes missed checks and rejection of valid programs. Passing the
ordinary regression suite does not establish the proposed full safety guarantee.

## Completed verification at a glance

| Verification | Result |
| --- | --- |
| Ordinary integration suite | 185 tests: 183 pass, two documented expected failures; exit 0. |
| Main differential matrix | 3,858 cells; exit 1 because failed expectations are retained. |
| Boundary/strict-interaction supplement | 528 cells; exit 1 because boundary failures remain. |
| ASan/UBSan checking-runtime examples | All nine pass; exit 0. |

The combined audit records **3,178 passes, 266 required policy rejections,
328 new failure cells, 608 known-failure cells and six unavailable-mode cells**.
No reference-control failure, compiler crash, timeout or source modification
was observed. The six unavailable cells request `c++2d`/`gnu++2d`; all canonical
mode spellings were available in the ordinary compilers. Detailed tables and
reproductions follow below.

## Scope and interpretation

The main catalog contains 102 fixtures: 59 defined positive programs, 31
deliberate safety violations, and 12 strict-admission rejection cases. Backend
and minimum-language-version filters determine which fixtures are applicable.
A supplement adds five cases for foreign callbacks, variadic arguments,
nonlocal jumps and arm64 assembly, plus 24 strict-profile interaction cases.
Dialect smoke probes are additional
matrix cells rather than additional catalog fixtures.

The canonical flags are `-std=c++98`, `c++03`, `c++11`, `c++14`, `c++17`,
`c++20`, `c++23`, `c++26`, and the eight corresponding `gnu++` spellings.
Historical `0x`, `1y`, `1z`, `2a`, `2b`, `2c` and speculative `2d` aliases are
probed for both dialect families. C++26 is the installed compiler's development
mode, not a claim of complete C++26 implementation or conformance.

Native mode admits all eight canonical language versions. The archived AST
backend requires C++17 or newer; strict admission accepts only the exact
`c++17` and `gnu++17` spellings. Other backend modes are checked for deliberate
rejection, rather than incorrectly counted as compatibility successes.

Every applicable compatibility fixture runs at `-O0` and `-O2`. Seven core
fixtures also run at `-O1`, `-O3`, `-Os` and `-Oz` in ISO C++17 and C++23, for
both compatibility backends. Every strict fixture runs at all six optimization
levels in both supported strict dialects. The boundary supplement uses `-O0`
and `-O2` across the applicable canonical modes; its additional strict cases use
all six levels in both supported strict dialects. Atomic builtins and inline assembly
are explicitly extension probes, including when selected with an ISO mode flag.

The existing integration tests additionally exercise exception cleanup,
allocation replacement, placement construction, address reuse, provenance,
union switching and copying, templates, headers, multiple translation units,
driver/report integrity and unsupported strict configurations. These are
targeted regressions; the audit is neither an official ISO conformance suite
nor an exhaustive test of all C++ programs or combinations of features.

## How results are judged

Each case is compiled first with the corresponding ordinary Clang and then
through `strict++` using the same standard and optimization flags. Defined
positive controls must compile and return zero before the compiler under test
can be assessed. Their instrumented executions must return zero and match the
control's stdout. Deliberately unsafe controls are compiled and never executed.
Strict-policy controls are also compile-only.

Compatibility negatives write `strictcpp-matrix-armed` immediately before the
intended unsafe operation. A checker stop during valid setup is a failure.
After the marker, a passing safety probe requires exit code 86 and a diagnostic
explicitly permitted by that fixture. Successful unsafe execution, an OS signal,
an unrelated diagnostic, a compiler crash and a timeout are separate failures.
The strict runtime probes omit the foreign arming call because foreign calls
are outside that profile; their small setups have separate positive coverage.

Result categories are intentionally distinct:

| Category | Meaning |
| --- | --- |
| `pass` | The defined-program or intended-runtime-check expectation succeeded. |
| `policy_rejection` | The required explicit strict/backend admission rejection occurred. |
| `known_failure` | A previously documented unsupported behavior actually failed this probe. It is not a pass. |
| `failure` | A newly exposed or otherwise unmarked expectation failed. |
| `reference_failure` | The control compiler/program did not satisfy the fixture, so a target defect cannot be inferred from this cell. |
| `toolchain_unavailable` | The requested dialect spelling was rejected by both the ordinary compiler and the target. |

Both audit scripts return nonzero if a `failure`, `known_failure` or
`reference_failure` occurs. Expected-failure annotations in the ordinary
unittest suite do not suppress failures in these audits.

Primary source bytes are checked for modification, stale target binaries are removed before
compiling, subprocesses have timeouts, and every cell retains its exact command
argument arrays, stdout, stderr, source, coverage evidence and generated IR.
The compatibility arming helper is a foreign call: its coverage reports are
not evidence of zero-gap strict admission.

## Reproduction and evidence

Run these commands serially; the compiler build helpers share a native cache:

```sh
python3 -m unittest discover -s tests -v
python3 tests/standards_matrix.py --output build/standards-matrix
python3 tests/standards_supplement.py --output build/standards-supplement
make verify-runtime
```

The audit commands currently return nonzero because failures are retained.
Execute subsequent commands separately instead of placing them behind `&&`.
For a focused reproduction, select a fixture in a new directory:

```sh
python3 tests/standards_matrix.py --profile native --standard c++17 \
  --optimization O2 --case delete-corresponding-unsigned --no-aliases \
  --output build/delete-type-reproduction
python3 tests/standards_supplement.py --case longjmp-expired-stack \
  --output build/longjmp-reproduction
```

The fixture definitions and runners are repository source artifacts:

- [Main fixture catalog](../tests/standards_cases.py).
- [Main audit runner](../tests/standards_matrix.py).
- [Boundary supplement](../tests/standards_supplement.py).
- [Portable main summary, macros and frozen fixtures](audits/2026-10-31/main.summary.json)
  and [all main outcome rows](audits/2026-10-31/main.results.jsonl).
- [Portable supplement summary and frozen fixtures](audits/2026-10-31/supplement.summary.json)
  and [all supplement outcome rows](audits/2026-10-31/supplement.results.jsonl).
- [Ordinary regression transcript](audits/2026-10-31/regressions.txt).
- [Runtime sanitizer transcript](audits/2026-10-31/runtime-sanitizers.txt).

The [historical snapshot](audits/2026-10-31/README.md) preserves every outcome,
fixture and original source fingerprint without machine-local paths or raw
artifact trees. The first-reproduction columns below identify the mode and
optimization to use with the named fixture. The JSONL files retain each cell's
exit/timeout facts and observed diagnostic names.

Complete generated reports, argv, raw streams, coverage, IR and executables
remain local under `build/standards-matrix/` and `build/standards-supplement/`;
they are ignored by Git. A fresh full run recreates this richer evidence. The
recorded source fingerprint covers the native/Python implementation inputs and
audit sources selected by each runner; it is a provenance aid, not a complete
binary attestation or formal proof.

## Completed main run

The 3,858-cell main run completed in 1,287.407 seconds (about 21.5 minutes).
It returned **exit 1**, retaining 328 newly exposed failure cells and 480
known-failure cells. There were **zero reference-control failures, compiler
crashes or timeouts**. Every applicable canonical mode was available.

### Environment

| Component | Recorded value |
| --- | --- |
| native | `clang version 21.1.8 (https://github.com/llvm/llvm-project 2078da43e25a4623cab2d0d60decddf709aaea28)` |
| ast | `Apple clang version 21.0.0 (clang-2100.4.1.1)` |
| host | `macOS-27.2-arm64-arm-64bit-Mach-O` |
| machine | `arm64` |
| python | `3.14.5` |

The macOS SDK was `/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk`.
Both compilers accepted 28 of the 30 probed dialect spellings. Only
`c++2d` and `gnu++2d` were unavailable. In `c++26` mode both reported
`__cplusplus=202400L`; feature availability was checked by compiling actual
fixtures, rather than inferred from macro presence alone.

The main-run source fingerprint was unchanged when rechecked:

```text
f17341f7afb1a0b3ced11fa5671ae67df03f26e528b591e99f8c54e3d5435d5d
```

### Outcome counts

| Profile | Total | Pass | Required policy rejection | New failure | Known failure | Reference failure | Unavailable |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| native | 2242 | 1748 | 0 | 248 | 244 | 0 | 2 |
| ast | 1334 | 1004 | 12 | 80 | 236 | 0 | 2 |
| strict | 282 | 110 | 170 | 0 | 0 | 0 | 2 |
| **Total** | 3858 | 2862 | 182 | 328 | 480 | 0 | 6 |

The 2,862 passes comprise 1818 defined-program matches and 1044 intended
safety-check stops. Policy rejections are additional successful enforcement
outcomes, kept separate from runtime/compatibility passes. Six unavailable
cells are the two `2d` spellings requested under each of three profiles.

### Canonical standards coverage

The following counts combine the ISO and GNU mode of each language version.
They exclude historical aliases and include the focused optimizer cells.
Failure columns include both new and known failures. The generated report
retains a separate row for every individual mode and profile.

| Version | Native pass | Native failed | AST pass | AST failed | AST policy rejection | Strict pass | Strict policy rejection |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| C++98 | 166 | 36 | 0 | 0 | 2 | 0 | 2 |
| C++03 | 166 | 36 | 0 | 0 | 2 | 0 | 2 |
| C++11 | 186 | 60 | 0 | 0 | 2 | 0 | 2 |
| C++14 | 198 | 60 | 0 | 0 | 2 | 0 | 2 |
| C++17 | 246 | 64 | 234 | 76 | 0 | 110 | 144 |
| C++20 | 234 | 76 | 230 | 80 | 0 | 0 | 2 |
| C++23 | 270 | 80 | 270 | 80 | 0 | 0 | 2 |
| C++26 | 246 | 80 | 246 | 80 | 0 | 0 | 2 |

### Distinct main-run failures

The 808 failed expectation cells correspond to 40 distinct backend/fixture pairs.
Repeated modes and optimization levels do not represent independent defects.
The table identifies the first mode/optimization for each observed classification;
full per-cell outcome facts are in the portable snapshot above.

| Profile | Fixture | Classification | Observed result | Cells | First reproduction |
| --- | --- | --- | --- | ---: | --- |
| ast | `atomic-one-past-rmw` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `bad-function-signature` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `c-allocation` | `known_failure` | valid-program-runtime-rejection (`untracked-access`) | 16 | `c++17/O0` |
| ast | `const-bulk-copy` | `failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `constexpr-local-array` | `known_failure` | valid-program-runtime-rejection (`untracked-access`) | 16 | `c++17/O0` |
| ast | `copy-outside-allocation` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `coroutine-suspend-resume` | `known_failure` | valid-program-runtime-rejection (`untracked-access`) | 12 | `c++20/O0` |
| ast | `expired-reference-member` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `explicit-destructor-lifetime` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `field-array-escape` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `fold-expression` | `failure` | instrumented-compile-rejected | 16 | `c++17/O0` |
| ast | `header-wrong-read` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `initializer-list` | `failure` | valid-program-runtime-rejection (`untracked-access`) | 16 | `c++17/O0` |
| ast | `macro-wrong-read` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `outside-pointer-formation` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `range-for` | `failure` | instrumented-compile-rejected | 16 | `c++17/O0` |
| ast | `scalar-union-inactive` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `thread-local-worker` | `failure` | valid-program-runtime-rejection (`untracked-access`) | 16 | `c++17/O0` |
| ast | `uninitialized-int` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `uninitialized-unsigned-character` | `known_failure` | unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| native | `atomic-one-past-rmw` | `known_failure` | unsafe-action-not-diagnosed | 24 | `c++11/O0` |
| native | `capturing-lambda` | `known_failure` | valid-program-runtime-rejection (`type-mismatch`) | 24 | `c++11/O0` |
| native | `character-pointer-storage-type` | `known_failure` | unsafe-action-not-diagnosed | 32 | `c++98/O0` |
| native | `common-initial-sequence` | `known_failure` | valid-program-runtime-rejection (`union-inactive-member`) | 32 | `c++98/O0` |
| native | `const-bulk-copy` | `failure` | unsafe-action-not-diagnosed | 32 | `c++98/O0` |
| native | `coroutine-suspend-resume` | `known_failure` | valid-program-runtime-rejection (`expired-object`) | 12 | `c++20/O0` |
| native | `delete-corresponding-unsigned` | `failure` | unsafe-action-not-diagnosed | 32 | `c++98/O0` |
| native | `expected` | `failure` | valid-program-runtime-rejection (`union-inactive-member`) | 8 | `c++23/O0` |
| native | `explicit-destructor-lifetime` | `known_failure` | unsafe-action-not-diagnosed | 32 | `c++98/O0` |
| native | `integer-xor-roundtrip` | `known_failure` | valid-program-runtime-rejection (`missing-provenance`) | 32 | `c++98/O0` |
| native | `range-for` | `failure` | valid-program-runtime-rejection (`type-mismatch`) | 24 | `c++11/O0` |
| native | `span` | `failure` | valid-program-runtime-rejection (`type-mismatch`) | 12 | `c++20/O0` |
| native | `structured-binding` | `failure` | valid-program-runtime-rejection (`type-mismatch`) | 16 | `c++17/O0` |
| native | `thread-local-worker` | `known_failure` | valid-program-runtime-rejection (`uninitialized-read`) | 24 | `c++11/O0` |
| native | `uninitialized-unsigned-character` | `failure` | unsafe-action-not-diagnosed | 32 | `c++98/O0` |
| native | `utf16-string-literal` | `failure` | valid-program-runtime-rejection (`type-mismatch`) | 24 | `c++11/O0` |
| native | `utf32-string-literal` | `failure` | valid-program-runtime-rejection (`type-mismatch`) | 24 | `c++11/O0` |
| native | `utf8-character-type` | `failure` | valid-program-runtime-rejection (`type-mismatch`) | 12 | `c++20/O0` |
| native | `virtual-base-subobject` | `known_failure` | valid-program-runtime-rejection (`type-mismatch`) | 32 | `c++98/O0` |
| native | `wide-string-literal` | `failure` | valid-program-runtime-rejection (`type-mismatch`) | 32 | `c++98/O0` |

## What the main failures demonstrate

### Newly exposed missed safety checks

These probes reach their arming marker and complete without the required
checker diagnostic. They are failures in experimental compatibility modes;
they do not establish an escape from strict admission.

- **Deletion type identity:** native mode permits deleting an `int` allocation
  through an `unsigned int*` in all 32 canonical mode/optimization combinations.
  The corresponding signed/unsigned aliasing exception permits an access through
  that type, but does not make the deletion types interchangeable. The positive
  aliasing fixture and negative deletion fixture are therefore separate tests.
  The archived AST backend diagnoses this deletion fixture correctly.
- **Unsigned-character initialization:** native mode fails to diagnose returning
  the value of an uninitialized `unsigned char` in all 32 combinations. Reading
  object representation through a byte view and consuming an uninitialized
  scalar value need different rules. This is an initialization contract failure;
  the C++26 distinction between erroneous and indeterminate values is not a
  claim that every dialect specifies identical behavior for this source.
- **Const writes through bulk memory:** copying an integer into a `const int`
  with `std::memcpy` escapes the intended `write-to-const` check in both backends:
  32 native and 16 AST cells. Scalar const-write fixtures do trap correctly.

Strict admission rejects the constructs used by these witnesses. That is
evidence of a restricted admission boundary, not evidence that the broader
native compiler enforces the full requested contract.

### Rejection of valid C++

The corresponding ordinary compiler builds and runs every one of these
positive controls successfully. The instrumented compiler changes the result:

- Native wide, UTF-16, UTF-32 and UTF-8 character literal accesses produce
  `type-mismatch`. A representative wide-literal diagnostic requests `wchar_t`
  from an object registered as a 12-byte `aggregate`.
- Native range-for and `std::span` examples report pointer type mismatches;
  one diagnostic requests `p1 int` from storage registered as `p1 aggregate`.
- Native structured binding reports a request for the `Pair` type from storage
  registered as `int`. Native `std::expected<int,int>` reports an inactive union
  member during a valid dereference.
- The archived AST backend produces invalid rewritten syntax for range-for and
  an unexpanded parameter pack in its injected guard for a fold expression.
  Initializer-list and thread-local examples stop with `untracked-access`.

Those diagnostic descriptions are observations, not proven root causes. The
reports retain original source and generated IR/rewritten files for subsequent
implementation work. Previously documented limitations also reproduce for
explicit destruction, exact pointer-storage identity, common initial sequences,
virtual bases, some lambda/coroutine/TLS behavior and integer-pointer provenance.

The focused optimizer sweep passes its selected core fixtures at all six tested
levels. This does not repair the failures in other fixtures or demonstrate
that every optimizer transformation preserves safety instrumentation.

## Completed supplement and combined results

The supplement completed all 528 cells in 133.323 seconds
(about 2.2 minutes) and returned **exit 1** because its documented boundary
failures remain unresolved. It recorded no new failure, reference failure,
compiler crash or timeout. The additional 24 strict fixtures produced
**204 passes and 84 required admission rejections**, with no failed expectation.

| Supplemental profile | Total | Pass | Policy rejection | Known failure | New failure |
| --- | ---: | ---: | ---: | ---: | ---: |
| native | 160 | 64 | 0 | 96 | 0 |
| ast | 80 | 48 | 0 | 32 | 0 |
| strict | 288 | 204 | 84 | 0 | 0 |

### Foreign and control-transfer boundary results

| Profile | Fixture | Result | Cells | First reproduction |
| --- | --- | --- | ---: | --- |
| ast | `assembly-const-store` | `known_failure`: unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `longjmp-expired-stack` | `known_failure`: unsafe-action-not-diagnosed | 16 | `c++17/O0` |
| ast | `qsort-pointer-callback` | `pass`: defined-program-matches-reference | 16 | `c++17/O0` |
| ast | `qsort-scalar-callback` | `pass`: defined-program-matches-reference | 16 | `c++17/O0` |
| ast | `variadic-scalar` | `pass`: defined-program-matches-reference | 16 | `c++17/O0` |
| native | `assembly-const-store` | `known_failure`: unsafe-action-not-diagnosed | 32 | `c++98/O0` |
| native | `longjmp-expired-stack` | `known_failure`: unsafe-action-not-diagnosed | 32 | `c++98/O0` |
| native | `qsort-pointer-callback` | `known_failure`: valid-program-runtime-rejection (`missing-provenance`) | 32 | `c++98/O0` |
| native | `qsort-scalar-callback` | `pass`: defined-program-matches-reference | 32 | `c++98/O0` |
| native | `variadic-scalar` | `pass`: defined-program-matches-reference | 32 | `c++98/O0` |

The `longjmp` probe jumps out of a frame containing only trivial objects;
the invalid operation is the subsequent read through the saved pointer.
Neither backend diagnoses that expired stack lifetime. The arm64 assembly
probe writes a valid local address whose source object is const, and also
escapes both checkers. Native pointer sorting through foreign `qsort`
rejects an otherwise valid program, while scalar sorting and the scalar
variadic example succeed. Those successes establish only these specific
callback/varargs examples, not general foreign-interface correctness.

### Combined audit totals

| Profile | Total | Pass | Policy rejection | New failure | Known failure | Reference failure | Unavailable |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| native | 2402 | 1812 | 0 | 248 | 340 | 0 | 2 |
| ast | 1414 | 1052 | 12 | 80 | 268 | 0 | 2 |
| strict | 570 | 314 | 254 | 0 | 0 | 0 | 2 |
| **Total** | 4386 | 3178 | 266 | 328 | 608 | 0 | 6 |

The strict profile totals **314 passes and 254 required policy rejections**.
Its remaining two cells request unavailable `2d` spellings. No strict
escape was observed in the exercised fixtures. Strict admission accepts
a small subset of C++17, so these results cannot establish full C++ safety
or compatibility with arbitrary existing C++ programs.

The combined audit covers 131 catalog fixtures, plus dialect smoke probes,
across 4,386 cells. The 936 failed expectation cells include repeated
language/optimization variants of 45 backend/fixture pairs; 16 of those
pairs are newly exposed failures. Known failures remain failures.

Compiler implementation files were unchanged during this testing task.
The new artifacts are test runners, fixtures, Make targets and documentation.
The discovered defects remain open; no passing-suite or full-guarantee
claim should be inferred from these measurements.

## Ordinary suite and runtime sanitizer results

The ordinary suite ran all 185 tests in 54.867 seconds and returned exit 0:
183 passed and the two documented experimental-mode witnesses remained expected
failures. The strict tests separately reject those witness constructs. The new
standalone audits are deliberately outside this ordinary suite's discovery.

`make verify-runtime` returned exit 0. It compiled the checking runtime with
AddressSanitizer and UndefinedBehaviorSanitizer, relinked checked bitcode, and
verified the following nine executions without a sanitizer report:

| Example | Expected and observed result |
| --- | --- |
| `valid` | Exit 0. |
| `heap_array` | Exit 0. |
| `type_confusion` | Exit 86, `type-mismatch`. |
| `vector_lifetime` | Exit 86, `expired-object`. |
| `replacement_allocator` | Exit 0. |
| `union_members` | Exit 0. |
| `union_lifetime` | Exit 86, `union-inactive-member`. |
| `character_alias` | Exit 86, `type-mismatch`. |
| `strict` | Strict admission followed by exit 0. |

This sanitizer run checks the trusted runtime implementation on these examples.
It does not sanitize the compiler plugins or prove that the source-language
checks recognize every unsafe operation. The expanded audit's missed checks
remain failures even though these nine runtime examples pass.
