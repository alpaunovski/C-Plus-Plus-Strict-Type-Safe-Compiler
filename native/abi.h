// Shared compiler/runtime interface for the native backend. This is a C++ header with
// C linkage on entry points, not a header intended for user programs or a C compiler.
// The LLVM pass constructs these layouts/signatures manually: update both sides together.
#pragma once
#include <cstdint>

// Stable C ABI between the LLVM pass and the uninstrumented runtime.
// Keep layouts synchronized with NativePass::descriptor().
struct ScType;
// A repeated child layout: offset is in bytes from the enclosing element, count is
// its repetition count, and type supplies the stride/recursive layout for each child.
struct ScField {
  std::uint64_t offset;
  std::uint64_t count;
  const ScType* type;
};
// Immutable descriptors are emitted as module globals. Runtime records retain these
// pointers, so unloading instrumented modules is outside the current lifetime contract.
struct ScType {
  // Normalized access/record identity, often an RTTI-mangled record name. This vocabulary
  // is intentionally incomplete; it is not a complete serialization of C++ QualType.
  const char* name;
  // Size of one described element, not necessarily the complete registered allocation.
  // The register call supplies the actual storage extent separately.
  std::uint64_t size;
  // Bitmask below; const may combine with array, pointer, record or byte categories.
  std::uint64_t flags;
  // Number of ScField entries; a null fields pointer is used when this count is zero.
  std::uint64_t fields_count;
  const ScField* fields;
};
// SC_BYTES permits supported representation accesses; SC_RAW marks raw backing storage.
// SC_UNION requires active-member state. These flags do not imply that every C++ rule
// for the corresponding category has been implemented.
enum : std::uint64_t {
  SC_BYTES = 1, SC_POINTER = 2, SC_RECORD = 4, SC_UNION = 8,
  SC_CONST = 16, SC_RAW = 32, SC_ARRAY = 64
};
// All entry points are noexcept, matching the pass-generated nounwind calls. IDs use
// zero for missing authority; real object/frame/capability generations share a monotonic
// runtime ID source. Runtime helper names are implementation-reserved, not escape hatches.
extern "C" {
// Begin an exact-function activation and return its frame ID.
std::uint64_t __sc_enter(void* function) noexcept;
// End a frame and supply its scalar result ID; aggregate return pieces are separate.
void __sc_leave(std::uint64_t frame, std::uint64_t result) noexcept;
// Discard abandoned child frames/pending calls while preserving the surviving frame.
void __sc_unwind(std::uint64_t frame) noexcept;
// Register (address, byte extent, descriptor, storage kind, file, line). Storage kinds:
// 0 stack, 1 scalar new, 2 global, 3 calloc, 4 array new, 5 malloc/aligned C allocation.
std::uint64_t __sc_register(void*, std::uint64_t, const ScType*, std::uint64_t,
                            const char*, std::uint32_t) noexcept;
// Retire an object ID at a recognized storage lifetime end.
void __sc_end(void*, std::uint64_t) noexcept;
// Recover a live-address ID only at explicit compiler-selected boundaries.
std::uint64_t __sc_resolve(void*) noexcept;
// Derive a bounded capability for (pointer, inherited ID, byte extent, source location).
std::uint64_t __sc_narrow(void*, std::uint64_t, std::uint64_t, const char*, std::uint32_t) noexcept;
// Derive a selected typed/mutable view while retaining the backing object identity.
std::uint64_t __sc_view(void*, std::uint64_t, const ScType*, const char*, std::uint32_t) noexcept;
// Frontend identity hooks preserve semantic events in raw IR. The pass supplies the
// actual placement/downcast operations; these stubs alone perform no enforcement.
void* __sc_front_placement(void*, std::uint64_t, const char*) noexcept;
void* __sc_front_downcast(void*, std::uint64_t, const char*, std::uint64_t) noexcept;
// Validate a derived extent using its source type name and nonzero base adjustment.
std::uint64_t __sc_downcast(void*, std::uint64_t, std::uint64_t, const char*, std::uint64_t, const char*, std::uint32_t) noexcept;
// Start a supported typed lifetime in raw backing storage and return its fresh ID.
std::uint64_t __sc_placement(void*, std::uint64_t, std::uint64_t, const ScType*, const char*, std::uint32_t) noexcept;
// Validate (pointer, ID, width, access name, container name, container offset, flags,
// file, line). flags: write=1, byte/aggregate=2, read-modify-write=4; upper 32 bits
// contain required alignment. Selected checks also update initialization/shadow metadata.
void __sc_access_v2(void*, std::uint64_t, std::uint64_t, const char*, const char*,
                    std::uint64_t, std::uint64_t, const char*, std::uint32_t) noexcept;
// Record (slot address, actual stored bits, transported ID) after the user store.
void __sc_shadow_store(void*, std::uint64_t, std::uint64_t) noexcept;
// Record a static initializer eligible for later explicit live-address resolution.
void __sc_shadow_static(void*, std::uint64_t) noexcept;
// Recover an ID only when the slot and actual loaded bits match its shadow entry.
std::uint64_t __sc_shadow_load(void*, std::uint64_t) noexcept;
// Push a pending call with its exact target and machine argument count.
void __sc_call_begin(void*, std::uint64_t) noexcept;
// Supply one scalar argument ID, indexed by lowered machine argument position.
void __sc_call_arg(std::uint64_t, std::uint64_t) noexcept;
// Supply an aggregate argument piece ID, indexed by argument and piece position.
void __sc_call_arg_slot(std::uint64_t, std::uint64_t, std::uint64_t) noexcept;
// Read a scalar argument ID at entry; raw bits support the limited foreign-entry rule.
std::uint64_t __sc_argument(std::uint64_t, void*) noexcept;
// Read an aggregate argument piece; missing/foreign pieces retain ID zero.
std::uint64_t __sc_argument_slot(std::uint64_t, std::uint64_t) noexcept;
// Consume the matching pending call and expose its scalar/aggregate return metadata.
std::uint64_t __sc_call_end(void*) noexcept;
// Publish one independent aggregate return-piece ID from the callee.
void __sc_return_slot(std::uint64_t, std::uint64_t) noexcept;
// Recover one return piece after the caller has consumed call_end.
std::uint64_t __sc_returned_slot(std::uint64_t) noexcept;
// Validate a deallocation family and retire storage unless kind includes bit 0x100.
// The low family byte is 0 for C free, 1 for scalar delete, 2 for array delete.
void __sc_release(void*, std::uint64_t, std::uint64_t, const char*, std::uint32_t) noexcept;
// Keep this exact object live while a replaceable C++ deallocation callback executes.
void __sc_custom_delete_begin(void*, std::uint64_t, std::uint64_t, const char*, std::uint32_t) noexcept;
// Retire the paired TLS-recorded root after that callback returns.
void __sc_custom_delete_end(void*, std::uint64_t) noexcept;
// Validate realloc before its actual call; reject unsupported non-null zero-size cases.
void __sc_realloc_guard(void*, std::uint64_t, std::uint64_t, const char*, std::uint32_t) noexcept;
// Finish realloc with a fresh ID and preserved prefix metadata; a failed result is zero.
std::uint64_t __sc_reallocated(void*, void*, std::uint64_t, const char*, std::uint32_t) noexcept;
// Transfer supported initialization/type/shadow/union metadata for memcpy/memmove.
// Both source and destination carry independent provenance IDs and a checked byte count.
void __sc_copy(void*, std::uint64_t, void*, std::uint64_t, std::uint64_t,
               const char*, std::uint32_t) noexcept;
// Mark byte-filled storage initialized and invalidate any overlapping pointer shadows.
void __sc_fill(void*, std::uint64_t, std::uint64_t, const char*, std::uint32_t) noexcept;
// Seed hosted argc/argv storage, strings and the null-sentinel pointer shadow.
void __sc_main_args(int, char**) noexcept;
// Compute current + index * stride with checked signed arithmetic before a GEP.
std::int64_t __sc_offset(std::int64_t, std::int64_t, std::uint64_t, const char*, std::uint32_t) noexcept;
// Validate base + signed offset as address formation, allowing one-past but not access.
void __sc_gep(void*, std::uint64_t, std::int64_t, const char*, std::uint32_t) noexcept;
// Register an address with its lowered IR function type and calling convention.
void __sc_function(void*, const char*) noexcept;
// Validate an indirect target against that lowered signature before invocation.
void __sc_indirect(void*, const char*, const char*, std::uint32_t) noexcept;
// Register a preserved source signature for instrumented free/static definitions.
void __sc_function_source(void*, const char*) noexcept;
// Compare a source call signature where a checked definition supplied one.
void __sc_source_call(void*, const char*, const char*, std::uint32_t) noexcept;
// Identity stubs for source call/access/member events; the LLVM pass adds enforcement.
void* __sc_front_call_target(void*, const char*) noexcept;
void* __sc_front_access(void*, std::uint64_t, const char*, std::uint64_t) noexcept;
void* __sc_front_union(void*, std::uint64_t, const char*, std::uint64_t, std::uint64_t) noexcept;
// Select (union address, inherited ID, union width, type name, one-based member,
// activation mode, file, line). Modes 0/1/2 select, activate/reset, and initialize/preserve.
// The resulting capability captures member generation independently from pointer bits.
std::uint64_t __sc_union_select(void*, std::uint64_t, std::uint64_t, const char*,
                                std::uint64_t, std::uint64_t, const char*, std::uint32_t) noexcept;
}
