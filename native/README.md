# Native backend design

The native backend is the current implementation path. It is an experimental
compiler, not an established sound implementation of all C++ safety rules.

## Pipeline

1. The pinned Clang frontend parses the original translation unit. A plugin
   runs before CodeGen and preserves selected semantic events that otherwise
   disappear from IR: concrete placement construction, pointer downcasts,
   mutable-field views, signed-character accesses, scalar union member events,
   and free/static-function signatures.
   With `--sc-strict`, a separate allowlist checks the original AST and
   preprocessor directives before instrumentation. Unsupported nodes, types and
   operations reject the build; see `docs/STRICT.md`.
2. Clang emits unoptimized bitcode with standalone debug information and TBAA.
   The frontend uses O1 to retain TBAA; O0 inputs undefine `__OPTIMIZE__`.
   This is not a perfect reproduction of every compiler-option/macro behavior.
3. `opt` loads the native pass. The pass runs before optimization and is followed
   by the verifier, requested default optimization pipeline, and verifier again.
4. The matching Clang generates a native object. Linking adds the separately
   compiled, uninstrumented runtime exactly once per executable.

The runtime is a trusted component, as are the compiler/toolchain and explicit
foreign boundaries. It is built without the program's flags/macros. Its
containers allocate via malloc/free rather than the program's replaceable
global new/delete, preventing instrumentation recursion.

User replacement allocators' returned-storage contracts are not fully verified.
Their definitions count as coverage gaps even though their emitted accesses are
instrumented. Correct metadata currently assumes these low-level contracts hold.

## Metadata

Object records have monotonically assigned IDs, address/extent, storage family,
declared type layout when available, initialized-byte state, lifetime state,
and source location. Storage-generation IDs are not inferred from addresses.
Expired records are retained so a saved ID cannot become valid merely because
the same address is allocated again.

Selected scalar union states record a member index and generation per union
address/type within a storage object. Member capabilities retain that identity
through narrowing and shadow transport. Address formation can precede member
activation, while checked value accesses validate the captured member and
generation. This is not complete union semantics; union descriptors still
increase the conservative coverage-gap count.

Capabilities add subobject bounds and selected typed views to an existing
object ID. Narrowing does not create a new allocation. Metadata can follow
selected transparent replacement links; arbitrary replacement rules are not
implemented.

Pointer/integer shadow entries associate a storage address with its actual
bits and ID. Loads recover a matching entry's ID; ordinary missing shadow
entries do not resolve an arbitrary address to a fresh allocation. Static
pointer initializers use a distinct lazy-resolution marker so cross-TU global
initialization order can resolve static roots after registration.

The pass transports IDs through eligible scalar and aggregate pieces, LLVM
PHIs/selects, selected casts/arithmetic, and native call boundaries. Aggregate
and wide-integer pieces have independent transported IDs. This is necessary to
avoid reinterpreting a stale pointer returned inside a struct as a fresh pointer
after address reuse.

TLS call records identify the exact called function and carry argument/return
IDs without changing the native calling convention. Foreign callbacks do not
consume the wrong pending native call record. They use a limited, explicitly
trusted entry recovery rule. Exceptions trim abandoned call frames and retire
their automatic objects.

## Checks

Loads/stores validate allocation identity, liveness, bounds, selected alignment
and type rules, and selected const/initialization rules before the operation.
Record TBAA paths are checked against recorded subobject layouts, preventing
same-layout unrelated records from passing merely because both fields are int.

GEP offsets are accumulated with checked signed arithmetic before pointer
formation checks. `inbounds` is removed to avoid allowing an invalid offset to
become LLVM poison before the check. Selected field/row GEPs narrow bounds.
This is not complete C++ pointer-arithmetic semantics.

Indirect calls validate the lowered IR signature. Frontend annotations preserve
additional source signatures for instrumented free/static functions, including
pointee types that opaque LLVM pointers erase. Member-function pointer and
foreign-function coverage is incomplete; indirect calls remain a reported gap
for conservative coverage rejection.

## Allocation and lifetime

The pass recognizes ordinary typed allocation, array allocation, C allocation,
reallocation, memory-copy/fill operations, LLVM lifetime markers, and selected
exception storage allocation. Family checks distinguish scalar new, array new,
and C allocations. User delete and standard sized-delete forwarding keep an
identity alive through the deallocator callback, then retire it.

Concrete placement hooks create typed children of raw storage. Ending the
owner's lifetime retires its children. Limited transparent same-type replacement
can forward old identities to the replacement. Existing typed storage without
supported backing information is rejected rather than silently reinterpreted.

## Known gaps and evidence

`tests/test_native.py` contains executable positive/negative regressions.
`tests/test_semantics.py` exercises source distinctions and union state.
`tests/test_known_limits.py` deliberately exposes remaining counterexamples.
The root README lists broader incomplete areas. A passing regression suite or
zero reported gaps is not a soundness proof.

Important unfinished work includes exact source-level access types in all
cases, union/explicit-destruction semantics, complete pointer information flow,
virtual/member-function behavior, foreign ABI contracts, atomic/concurrent
lifetime enforcement, and broad compatibility/performance validation.
