# Strict profile v1

`--sc-strict` prioritizes rejection of unsupported cases over legacy-code
compatibility. It accepts a deliberately small source subset, runs the native
checks, rejects reported coverage gaps and foreign calls, and links the runtime.
It never silently switches to the AST backend or permits untracked accesses.

```sh
./bin/strict++ --sc-strict -O2 --sc-report coverage.json program.cpp -o program
```

The implementation is an explicit allowlist, not a list of known bad features.
The source gate runs before AST instrumentation. Unlisted declaration and
statement/expression kinds, object types, casts and operators cause a compiler
error at the source location. Headers and expanded macros pass through the same
gate. Pragmas, including `_Pragma` expansions, and identification directives are
rejected; Clang's own predefines buffer is exempt using its physical file ID.
Line directives cannot confer that exemption.

## Supported source

- C++17 or GNU++17, one source translation unit, currently macOS arm64 only.
- `bool`, `int`, `unsigned int`, `long`, `unsigned long`, `long long`, and
  `unsigned long long`; const qualification and fixed arrays of these types.
- Scalar function parameters/returns, up to eight parameters, direct calls and
  an acyclic call graph. `void` returns are supported. `main` takes no arguments.
- Small automatic objects and constant-initialized static data with internal
  linkage. Externally visible data is rejected to avoid runtime symbol interposition.
- Blocks, declarations, returns, `if`, `while`, `do`, `for`, `break`, `continue`,
  conditional expressions, scalar assignment and comparisons, and selected
  bitwise/logical operations.
- Temporary pointer expressions formed from supported objects: address-taking,
  dereference, array decay/indexing, checked pointer offsets, equality/null
  comparisons, and pointer selection. Pointer/reference objects and interfaces
  that store or pass pointers are rejected.
- Unsigned arithmetic and standalone unsigned increment/decrement. Signed
  arithmetic must be a proven constant expression, except division/remainder by
  a proven positive constant divisor. Unsigned division/remainder requires a
  nonzero constant divisor; shifts require an unsigned result and a constant
  count within the type width. Nested assignment/increment side effects are rejected.
- Selected integral, boolean, null-pointer and qualification conversions, plus
  `sizeof`/`alignof` on supported object types and aliases for supported types.

Each array is limited to 1024 bytes. Each function is limited to 1024 bytes of
declared local objects plus a conservative tracking allowance per declaration.
The translation unit is limited to 4096 statement/expression nodes. These are
implementation limits, not a proof about every native stack or resource use.

Everything not admitted above is rejected, including classes, unions, enums,
character/floating/vector types, stored pointers/references, allocation,
exceptions, assembly, builtins, indirect calls, recursion, TLS, arbitrary
attributes, namespaces, dynamic static initialization and foreign interfaces.
Some harmless declarations and unevaluated expressions are also rejected.
Most standard-library headers are therefore outside this initial profile.

Bounds, null and initialization violations in admitted constructs still trap
at runtime with exit 86. Successful compilation does not mean every input or
execution will avoid a trap.

## Driver configuration

The strict driver permits a source-to-executable build with `-o`, include paths
and macro definitions/removals (`-I/-D/-U`), warning controls, `-g/-g0`, the six
tested optimization levels (`-O0/-O1/-O2/-O3/-Os/-Oz`), and
`-fno-rtti/-fno-exceptions`. Defaults select C++17 and the host toolchain.

Unknown flags, cross-target options, compiler/pass/plugin forwarding, raw
linker inputs, prebuilt objects/libraries, separate compilation, assembly output,
preprocessing-only output, PCH/modules/LTO and multiple source units are rejected.
Source, output and report paths must be distinct. Toolchain/SDK installation and
the host build environment remain trusted components.
Newline/carriage-return arguments are rejected so macro options cannot inject
directives into Clang's trusted predefines buffer.

The driver requires a unique strict-gate acknowledgment, regular instrumentation
reports, zero known/deferred gaps, no foreign calls, and successful linking.
The JSON report records `mode: strict`, `profile: strict-v1`, the final build
status, and each translation unit's source policy. Frontend rejections include
the diagnostics. An early invalid driver configuration can fail before a report
is created.

## Limits of the claim

This closes the admission-policy problem where an unknown source construct
could pass merely because it did not increment a gap counter. It does not prove
the compiler, runtime or this initial profile correct, and it does not implement
full C++ safety. Resource exhaustion, all C++ undefined behavior, compiler defects
and every interaction among admitted operations are not comprehensively modeled.
The broad backend retains executable counterexamples; strict tests additionally
verify that those witnesses are rejected.

The regression suite is evidence for the implemented behavior. Expanding this
profile requires adding an explicit rule with positive and negative evidence;
removing a rejection alone is insufficient. See [SAFETY.md](SAFETY.md) for the
intended full contract and the remaining implementation work.
