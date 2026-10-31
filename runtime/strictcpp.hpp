// Template support for the archived source-rewriting backend only. The native LLVM
// backend uses native/abi.h and a separate runtime. These wrappers preserve ordinary
// C++ types/value categories but do not provide the native backend's shadow provenance.
#pragma once

// Compiler-injected implementation details. User programs do not include this.
#include <cstddef>
#include <atomic>
#include <cstdint>
#include <limits>
#include <memory>
#include <type_traits>
#include <typeindex>
#include <typeinfo>
#include <utility>
#include <vector>

// This opt-in is injected only by the AST driver's explicitly weaker compatibility
// option. It permits unknown storage; it must never be presented as strict enforcement.
#ifndef STRICTCPP_ALLOW_UNTRACKED
#define STRICTCPP_ALLOW_UNTRACKED 0
#endif

namespace __strictcpp {

// Original user location embedded in generated wrappers, independent of temporary
// instrumented filenames used during compilation.
struct Source {
  const char* file;
  int line;
};

// key permits selected access aliases; identity retains the more precise RTTI type
// needed for deletion/layout identity. Read compatibility does not imply delete compatibility.
struct Type {
  std::type_index key;
  std::type_index identity;
  const char* name;
  std::size_t size;
  std::size_t alignment;
  bool bytes;
  bool constant;
};

// [conv.qual] "similar" types may differ in cv-qualification at successive
// pointer/array levels. Access through those types is permitted by [basic.lval].
template <class T> struct Similar;
template <class T> struct SimilarImpl { using type = T; };
template <class T> struct SimilarImpl<T*> {
  using type = typename Similar<T>::type*;
};
template <class T, std::size_t N> struct SimilarImpl<T[N]> {
  using type = typename Similar<T>::type[N];
};
template <class T, class C> struct SimilarImpl<T C::*> {
  using type = typename Similar<T>::type C::*;
};
template <class T> struct Similar : SimilarImpl<std::remove_cv_t<T>> {};

// Build a descriptor without changing T in user expressions. Qualifier normalization
// and corresponding integer aliases apply to access keys, while size/alignment, byte
// access status and top-level constness remain separate checks.
template <class T> Type type() {
  using U = std::remove_cv_t<T>;
  // C++ permits accessing an integer through its corresponding signed or
  // unsigned type. Do not equate unrelated types merely because sizes match.
  const auto identity = std::type_index(typeid(typename Similar<U>::type));
  auto key = identity;
  if constexpr (std::is_integral_v<U> && !std::is_same_v<U, bool> &&
                !std::is_same_v<U, char> && !std::is_same_v<U, wchar_t> &&
                !std::is_same_v<U, char16_t> && !std::is_same_v<U, char32_t>
#ifdef __cpp_char8_t
                && !std::is_same_v<U, char8_t>
#endif
  ) {
    key = std::type_index(typeid(std::make_unsigned_t<U>));
  }
  return {key, identity, typeid(U).name(), sizeof(T), alignof(T),
          std::is_same_v<U, char> || std::is_same_v<U, unsigned char> ||
              std::is_same_v<U, std::byte>,
          std::is_const_v<T>};
}

// One typed repeated subregion of a registered object. Builders include the whole
// object and selected array/field/base regions so checking can choose a specific match.
struct Region {
  std::uintptr_t begin;
  std::size_t count;
  Type element;
};

class Builder;
// Prefer an ADL-visible compiler-generated friend when a record has one; the long
// fallback below is a no-op for types whose subobject description is unavailable.
template <class T>
auto describe(T* p, Builder& b, int)
    -> decltype(__strictcpp_describe(p, b), void()) {
  __strictcpp_describe(p, b); // ADL finds compiler-generated friends.
}
template <class T> void describe(T*, Builder&, long) {}

// Recursively register known object structure. Generated friends allow private field
// and base addresses to be described without requiring new user syntax or public members.
class Builder {
public:
  std::vector<Region> regions;

  // Record this level before descending into arrays/records. Const/mutable behavior is
  // deduced from the actual pointer expression supplied by the generated friend.
  template <class T> void add(T* p, std::size_t count = 1) {
    regions.push_back({reinterpret_cast<std::uintptr_t>(p), count, type<T>()});
    if constexpr (std::is_array_v<T>) {
      using E = std::remove_extent_t<T>;
      add(reinterpret_cast<E*>(p), count * std::extent_v<T>);
    } else if constexpr (std::is_class_v<T>) {
      for (std::size_t i = 0; i < count; ++i)
        describe(p + i, *this, 0);
    }
  }
};

// Only registry/guard operations carry IDs in this backend. Ordinary user pointers
// remain raw addresses, which is one reason this backend cannot offer complete provenance.
using Id = std::uint64_t;
Id register_object(std::vector<Region>, bool heap, bool array, Source);
void retire(Id);
void check(std::uintptr_t, Type, bool write, Source, bool allow_unknown);
Id prepare_delete(std::uintptr_t, Type, bool array, bool virtual_destructor, Source);
void arguments(int argc, char** argv, Source);
[[noreturn]] void fail(const char* reason, std::uintptr_t, const char* expected,
                       const char* actual, Source, Source origin = {nullptr, 0});

// Build all known regions first, then register them as one storage lifetime. heap and
// array classify selected delete checks; this is not the native allocation-family protocol.
template <class T>
Id register_block(T* p, std::size_t count, bool heap, bool array, Source s) {
  Builder b;
  b.add(p, count);
  return register_object(std::move(b.regions), heap, array, s);
}

// RAII registration helper injected around supported declarations. For supported local
// objects the driver arranges declaration order so user destruction precedes guard retirement.
// Global/static and advanced lifetime behavior remain bootstrap limitations.
class Guard {
  std::atomic<Id> id_{0};

public:
  Guard() = default;
  Guard(const Guard&) = delete;
  Guard& operator=(const Guard&) = delete;
  template <class T> Guard(T& object, Source s) { bind(object, s); }
  // Late binding permits registration after initialization while keeping the guard's
  // scope/dtor order. Atomic publication plus registry deduplication supports local statics.
  template <class T> void bind(T& object, Source s) {
    if (!id_.load(std::memory_order_acquire)) {
      // Concurrent binding of one local-static guard receives the same
      // deduplicated registration ID from the mutex-protected registry.
      id_.store(register_block(std::addressof(object), 1, false, false, s),
                std::memory_order_release);
    }
  }
  ~Guard() { if (Id id = id_.load(std::memory_order_acquire)) retire(id); }
};

// Return the same pointer after a runtime-only check. The constant-evaluation branch
// must remain free of registry operations; function pointers use a different unsupported
// checking problem and are deliberately not treated as object storage.
template <class T>
constexpr T* pointer(T* p, Source s, bool write = false) {
  if constexpr (!std::is_function_v<T>) {
    if (!__builtin_is_constant_evaluated())
      check(reinterpret_cast<std::uintptr_t>(p), type<T>(), write, s,
            STRICTCPP_ALLOW_UNTRACKED);
  }
  return p;
}

// Validate the referred-to address while preserving the original lvalue/rvalue category
// through perfect forwarding; copying the object here would change program behavior.
template <class T>
constexpr T&& reference(T&& object, Source s, bool write = false) {
  pointer(std::addressof(object), s, write);
  return std::forward<T>(object);
}

// A member call sequences base evaluation before index evaluation in C++17.
// This preserves the evaluation order of built-in a[i], including i[a].
// Compute byte offsets with explicit magnitude/overflow checks before forming the
// resulting reference. Registry regions provide limited bounds/type checks; this is
// not complete source-array identity or native shadow-provenance tracking.
template <class T, class I>
constexpr T& index(T* p, I i, Source s, bool write) {
  if (__builtin_is_constant_evaluated()) return p[i];
  static_assert(std::is_integral_v<I> || std::is_enum_v<I>);
  if constexpr (std::is_enum_v<I>)
    return index(p, static_cast<std::underlying_type_t<I>>(i), s, write);
  const auto address = reinterpret_cast<std::uintptr_t>(p);
  std::uintmax_t magnitude = 0;
  bool negative = false;
  if constexpr (std::is_signed_v<I>) {
    negative = i < 0;
    magnitude = negative ? std::uintmax_t(-(i + 1)) + 1 : std::uintmax_t(i);
  } else {
    magnitude = std::uintmax_t(i);
  }
  const auto max = std::numeric_limits<std::uintptr_t>::max();
  if (magnitude > max / sizeof(T))
    fail("index-overflow", address, type<T>().name, nullptr, s);
  const auto offset = static_cast<std::uintptr_t>(magnitude * sizeof(T));
  if ((negative && offset > address) || (!negative && offset > max - address))
    fail("index-overflow", address, type<T>().name, nullptr, s);
  const auto target = negative ? address - offset : address + offset;
  check(target, type<T>(), write, s, STRICTCPP_ALLOW_UNTRACKED);
  return *reinterpret_cast<T*>(target);
}

// Stage evaluation as a C++17 member call: the base is evaluated before the index,
// matching built-in a[i] even when the index mutates the original pointer variable.
template <class P> struct Indexed {
  P p;
  Source s;
  bool write;
  template <class I> constexpr decltype(auto) at(I i) const { return index(p, i, s, write); }
};
// Decay and retain the already evaluated base value rather than evaluating the source
// pointer expression again inside at().
template <class P> constexpr auto indexed(P&& p, Source s, bool write) {
  return Indexed<std::decay_t<P>>{p, s, write};
}
// For commuted i[a], retain the first evaluated index and obtain the pointer as the
// second operand. Reversing this staging would silently change side-effect ordering.
template <class I> struct ReverseIndexed {
  I i;
  Source s;
  bool write;
  template <class P> constexpr decltype(auto) at(P p) const { return index(p, i, s, write); }
};
// Preserve the initial index value exactly once before evaluating the later array base.
template <class I> constexpr auto reverse_indexed(I i, Source s, bool write) {
  return ReverseIndexed<I>{i, s, write};
}

// Register a successful scalar allocation result without changing new's return type.
// Null results are left alone, preserving the underlying allocation behavior.
template <class T> T* allocated(T* p, Source s) {
  if (p) register_block(p, 1, true, false, s);
  return p;
}

// Evaluate the original bound once, pass it into the generated allocation lambda,
// and use that same count for registration. new T[n++] must not increment n twice.
template <class Allocate, class N>
auto allocated_array(Allocate allocate, N count, Source s) {
  auto* p = allocate(count);
  if (p) register_block(p, static_cast<std::size_t>(count), true, true, s);
  return p;
}

// Validate before destruction but retire only afterwards. User destructors may still
// legally read members while destroy(p) runs; premature retirement would reject them.
template <class T, class Delete>
void deleted(T* p, Delete destroy, bool array, Source s) {
  if (!p) return;
  const Id id = prepare_delete(reinterpret_cast<std::uintptr_t>(p), type<T>(),
                               array, std::has_virtual_destructor_v<T>, s);
  destroy(p);
  retire(id);
}

// String literal storage comes from the compiler, not a user declaration guard.
// Register it lazily at runtime and retain constexpr use without touching registry state.
template <class T, std::size_t N>
constexpr const T (&literal(const T (&a)[N], Source s))[N] {
  if (!__builtin_is_constant_evaluated()) {
    // Registration deduplicates immutable static literals by address and type.
    register_block(std::addressof(a), 1, false, false, s);
  }
  return a;
}

} // namespace __strictcpp
