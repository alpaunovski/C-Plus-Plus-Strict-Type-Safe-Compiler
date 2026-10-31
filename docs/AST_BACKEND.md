This document describes the retained AST bootstrap backend. Use `--sc-backend ast`; the native backend is now the default. See the root README for current behavior.

# Strict C++ — experimental Clang-backed compiler

A working prototype of type-access enforcement for **unchanged C++ source**.
It uses Clang to parse and type-check the program, instruments a temporary copy
using Clang's JSON AST, then compiles and links native code with a checking
runtime. The original source is never rewritten. No annotations, replacement
pointer types, or new C++ syntax are required.

**This is a prototype, not a complete strictly type-safe C++ implementation.**
It checks supported operations in input `.cpp` files. Included header bodies,
uninstrumented libraries, and some language constructs remain unchecked. It can
also stop safe programs when storage has not been registered. Every normal
compilation prints its coverage scope; a JSON report lists known skipped
constructs. Do not use this prototype as a security boundary.

## Run it

Requires Python 3.10+, Clang with `-ast-dump=json`, and a C++17+ toolchain. There
are no Python package dependencies and no LLVM rebuild is necessary. Tested
locally with Apple Clang 21 on macOS arm64.

```sh
./bin/strict++ --sc-backend ast -O2 examples/valid.cpp -o build-valid
./build-valid
# value=42 array=2 heap=7

./bin/strict++ --sc-backend ast -O2 examples/type_confusion.cpp -o build-confusion
./build-confusion
# .../type_confusion.cpp:8: strictcpp: type-mismatch at ...
#   requested: float; stored: int
#   object registered at .../type_confusion.cpp:4
# exits with status 86 before the instrumented load
```

Or run `make demo` and `make test`.

Ordinary compiler options such as `-std=c++20`, `-O2`, `-g`, `-I`, `-D`, `-o`,
and `-c` are forwarded to Clang. C++17 is the default. For example:

```sh
./bin/strict++ --sc-backend ast -c first.cpp -o first.o
./bin/strict++ --sc-backend ast -c second.cpp -o second.o
./bin/strict++ --sc-backend ast first.o second.o -o program
# Or compile and link multiple source files in one invocation:
./bin/strict++ --sc-backend ast first.cpp second.cpp -o program
```

Link instrumented objects through this driver to include exactly one shared
runtime registry. Plain `clang++` can also link them if `runtime/strictcpp.cpp`
is included explicitly. Object and pointer layouts are unchanged; instrumented
objects reference additional runtime functions.

## What works

| Feature | Implemented behavior |
| --- | --- |
| `T*` → `void*` → `T*` | Works without source changes |
| Wrong-type pointer reads and writes | Instrumented access exits before loading/storing |
| `reinterpret_cast<T&>` and C-style reference casts | Checks the referred-to storage |
| Automatic, global, local-static, and parameter objects | Compiler-generated storage registration |
| Built-in `*p`, `p[i]`, `i[p]`, and explicit `p->member` | Type/access checks, preserving lvalue behavior |
| Arrays and multidimensional arrays | Element type metadata and registered-storage bounds checks |
| Typed `new`, `new[]`, `delete`, `delete[]` | Allocation metadata, deletion validation and retirement |
| Array bound side effects | Evaluated once |
| Struct/class fields, including private fields | Generated hidden friends describe subobjects; layouts stay unchanged |
| Inheritance | Base-subobject metadata; scalar deletion through a virtual base destructor |
| Signed/unsigned corresponding integer access | Permitted by the aliasing rules; deletion still requires the proper type |
| Similar pointer/array types | Permit compatible cv-qualification differences at successive levels |
| `char`, `unsigned char`, `std::byte` views | Permitted object-representation access |
| Null, alignment, invalid deletion | Runtime diagnostics |
| Const storage | Checked at instrumented stores where write context is known, including `mutable` fields |
| Scope unwinding and deletion | Basic expiration detection, without a pointer-provenance guarantee |
| Constant evaluation and unevaluated operands | Preserve ordinary constant pointer checks and `sizeof`/`noexcept` behavior |
| Multiple translation units | One registry shared across separately compiled objects |

Checks are inserted before normal Clang optimization. Integration tests verify
that simple type-confusion cases are stopped at `-O0`, `-O2`, and `-O3`.

## Inspect compilation and coverage

```sh
./bin/strict++ --sc-backend ast --sc-emit-dir build/generated \
  --sc-report build/coverage.json examples/valid.cpp -o build/valid
```

The generated `.strict.cpp` file shows the exact instrumentation. Diagnostics
and runtime failures refer to the original input path and line.

`--sc-require-main-coverage` rejects known skipped AST constructs in the input
main file. **It does not certify header/library coverage or prove soundness.**
Use it with `--sc-report` to make the current backend's gaps visible to a build.

Unknown storage fails closed at instrumented accesses. For experimentation,
`--sc-allow-untracked` allows those accesses while retaining checks on known
storage. That option weakens checking; it is not a safety mode.

Select another compiler with `--sc-clang /path/to/clang++` or `STRICTCPP_CLANG`.
Use `--sc-help` for options. Response files are supported using POSIX quoting.

## Current limits

- **Header and library coverage:** Only input main files are transformed.
  Standard-library internals, user headers, macros expanded into memory
  operations, inline assembly, implicit range-for machinery, and overloaded
  operators may bypass checks. A `std::vector` use-after-reallocation is not
  comprehensively detected. Inference across arbitrary library APIs is absent.
- **Object lifetime:** Placement construction, active union members, custom
  allocation APIs, `malloc`/`realloc`, byte-copy lifetime creation, and storage
  reuse are not modeled. Nothrow/placement `new` is reported as uncovered.
  Checked accesses to objects allocated by unchecked libraries normally fail
  as untracked. String literals are exposed when they decay to pointers.
- **Pointer identity:** The runtime tracks addresses and object registrations,
  not provenance carried with pointer values. It cannot distinguish a stale
  pointer from a valid pointer after an address is reused. Bounds checks use
  registered storage extents, so crossing a nested array's row boundary into
  the next row can escape detection. Arbitrary pointer arithmetic is not
  separately instrumented.
- **Source-rewriting coverage:** Control-statement initializers and declaration
  bodies, local/anonymous record layouts, reference members, bitfields,
  construction-time explicit `this`, and constexpr-function local storage have
  limitations recorded in reports. Implicit unqualified field accesses are
  left to ordinary Clang typing. Const-write detection is contextual and does
  not cover every reference escape or method call. Function-pointer targets
  and indirect calls are not checked.
- **Compatibility:** C++17+ only. Modules, PCH, LTO, OpenMP, cross-compilation,
  unusual frontend flags, and arbitrary existing build systems are not validated.
  RTTI and exceptions are required. Source instrumentation can affect template
  overload contexts and diagnostics; this is not a drop-in production toolchain.
- **Cost:** Registration and checks use a mutex and linear registry scans;
  expired records are retained. There is no performance claim. This version
  prioritizes inspectable behavior and integration tests over speed.

The useful initial guarantee is deliberately local: **when a supported access
is instrumented and its object's type metadata is accurate, an incompatible
typed access is stopped before that load/store.** This does not extend to all
operations in an arbitrary C++ program.

## Implementation and next steps

- `strictcpp/driver.py`: compiler invocation, AST extraction, temporary source,
  reports, separate compilation, and runtime linking.
- `strictcpp/instrument.py`: semantic AST selection and composable range edits.
- `runtime/strictcpp.hpp`: injected templates, object descriptions, checked
  operations, and allocation helpers.
- `runtime/strictcpp.cpp`: shared type/object registry and runtime diagnostics.
- `tests/test_compiler.py`: end-to-end positive and negative native programs.

The bootstrap needs no LLVM development package, which makes it executable
with the installed Apple Clang. To grow it into the compiler discussed, the
next backend should move instrumentation into Clang CodeGen and LLVM passes:
cover header/template instantiations and macro expansions, model lifetime
transitions, retain per-pointer provenance, establish explicit contracts at
foreign interfaces, then optimize metadata/checks. That work should preserve
the unchanged-source goal while replacing this backend's source-range limits.

Relevant upstream foundations: [Clang AST](https://clang.llvm.org/docs/IntroductionToTheClangAST.html),
[Clang tooling interfaces](https://clang.llvm.org/docs/Tooling.html), and
[TypeSanitizer](https://clang.llvm.org/docs/TypeSanitizer.html).
