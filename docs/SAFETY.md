# Safety contract and implementation status

The current compiler does not establish a complete safety guarantee. The
default backend detects selected violations; `--sc-require-coverage` additionally
rejects reported unsupported operations. `--sc-strict` adds an explicit
source/preprocessor/driver allowlist for a small subset. None is a soundness proof.

## What a full guarantee would require

For every executed object access in an accepted program, the implementation
would have to enforce the permitted source access type, live object/member
identity, valid bounds and alignment, initialization rules and const rules.
Calls would need compatible source function types and valid object lifetimes.
Those properties would have to survive every supported optimization and ABI
lowering. Storage lifetime changes in another thread must not invalidate an
access between its check and execution.

The compiler/runtime, operating system and explicit external contracts form
the trusted foundation. Foreign binaries need verified contracts or matching
instrumentation. Arbitrary assembly, forged metadata calls, corrupted runtime
state and hostile hardware are not covered by the current implementation.

Unchanged syntax does not imply accepting unsafe executions. A conforming
implementation of the intended contract could reject a build, trap before an
unsafe operation, or use a checked boundary. Preserving every unsafe legacy
program's behavior would conflict with enforcing safety.

## Implemented in this revision

Source hooks retain signed-char accesses that LLVM's byte-alias metadata would
otherwise conflate. Reads, simple/compound assignments, and increment/decrement
operations carry source access information. Corresponding signed/unsigned
character accesses remain supported. Constant evaluation uses the original
typed expression; runtime checks do not introduce forbidden void-pointer casts
into the evaluated constant branch. The same correction covers indirect calls
and pointer downcasts.

Selected scalar union members have per-storage active-member state and
generation checks. Member identity is distinct from type and address. Simple
assignments activate supported members; an ordinary store through a pointer
does not activate an inactive member. Changing members invalidates captured
member capabilities. Source access hooks preserve single evaluation and C++17
assignment ordering. Simple local/global initializers and supported whole-union
copies transfer active-member information.

These checks follow the relevant [type-access rules](https://eel.is/c++draft/basic.lval)
and [union lifetime rules](https://eel.is/c++draft/class.union.general) for the
tested cases. They do not implement all exceptions and interactions in those
rules. In particular, record/array union members, common initial sequences,
all placement/implicit lifetime creation, complete replacement/launder rules,
and every ABI copy/coercion remain incomplete. Whole-union copies from compiler
constant data preserve initializer-selected destination state. Raw byte copies
without source union metadata cannot reconstruct a new active-member identity.
Union use therefore still counts as a coverage gap, including supported cases.

## Strongest available build policy

```sh
./bin/strict++ --sc-strict --sc-report coverage.json program.cpp -o program
```

This rejects reported native gaps, deferred source semantics, foreign call
targets, and prebuilt objects/libraries whose coverage cannot be validated.
Raw linker options and non-source inputs are rejected so they cannot introduce
unchecked code through another driver path. Alternate source-language/job flags
that the driver cannot reconstruct are rejected as well.
Pointer-object loads, construction, explicit destruction and dynamic casts are
conservatively deferred; safe code using them can be rejected. Missing frontend,
native-pass or foreign-boundary reports also stop compilation.

The [strict profile](STRICT.md) additionally rejects every source construct and
driver option outside its explicit allowlists. It requires a unique frontend
gate acknowledgment and disallows fallback to broader backends. Its accepted
subset is intentionally small. The older coverage-only flag checks what the
compiler knows to report without imposing this source whitelist.

Successful compilation cannot be described as full C++ safety certification.

## Remaining evidence against completeness

`tests/test_known_limits.py` retains executable expected failures for explicit
destruction and exact character-pointer storage types. The original union and
signed-char witnesses now pass as regular negative tests. Tests for successful
programs also protect constant evaluation, single evaluation and supported
union initialization/copy behavior.

The strict-profile suite verifies that these remaining witnesses are rejected
at compilation, and that admitted bounds/null/initialization errors still trap.

The broader work listed in the root README remains required: complete object
lifetime/source type tracking, complete provenance propagation, concurrent
access enforcement, foreign/exception runtime contracts, coverage validation
for build artifacts, and independent validation of compiler/runtime invariants.
The existing integration suite and runtime sanitizer checks are regression
evidence, not a proof of that contract.
