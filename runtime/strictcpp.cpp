// Address/RTTI registry for the archived AST backend. Unlike native/runtime.cpp this
// implementation has no companion SSA/shadow IDs through user pointers or per-byte
// initialization state. Its main-file checks remain explicitly experimental.
#include "strictcpp.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#if defined(__GNUG__)
#include <cxxabi.h>
#endif

namespace __strictcpp {
namespace {
// One registered root and its known typed subregions. Dead entries are retained for
// expired-object diagnostics, although address-only access recovery cannot distinguish
// all allocation-reuse cases handled by the native shadow-ID design.
struct Object {
  Id id;
  std::vector<Region> regions;
  bool heap;
  bool array;
  bool alive;
  Source source;
};
// A mutex serializes registry operations and ID assignment. It does not keep the lock
// held through a later user memory instruction and therefore does not establish race safety.
struct Registry {
  std::mutex mutex;
  std::vector<Object> objects;
  Id next = 1;
};
// Keep metadata available to guards during static destruction. The legacy runtime
// uses ordinary C++ allocation here; native runtime allocation-recursion protection is separate.
Registry& registry() {
  // Intentionally process-lifetime: global guards can run during teardown.
  static Registry* state = new Registry;
  return *state;
}
// Demangle RTTI names where the host GNU-compatible ABI supports it. Diagnostics still
// work with the original name if demangling is unavailable or fails.
std::string readable(const char* name) {
  if (!name) return "<unknown>";
#if defined(__GNUG__)
  int status = 0;
  char* demangled = abi::__cxa_demangle(name, nullptr, nullptr, &status);
  if (demangled) {
    std::string result(demangled);
    std::free(demangled);
    return result;
  }
#endif
  return name;
}
// Use subtraction-based range checks after checking the lower bound. A region may
// contain the entire requested access, whereas touches below also recognizes one-past.
bool contains(const Region& r, std::uintptr_t address, std::size_t size) {
  if (address < r.begin) return false;
  const auto offset = address - r.begin;
  const auto bytes = r.count * r.element.size;
  return offset <= bytes && size <= bytes - offset;
}
// Keep a candidate for an out-of-bounds diagnostic when an access starts at a known
// boundary but cannot fit its complete requested width.
bool touches(const Region& r, std::uintptr_t address) {
  return address >= r.begin && address - r.begin <= r.count * r.element.size;
}
} // namespace

// Emit original-source and registration locations, flush the diagnostic, then stop
// with exit 86. Avoid executing user teardown against storage already diagnosed invalid.
[[noreturn]] void fail(const char* reason, std::uintptr_t address,
                       const char* expected, const char* actual, Source s,
                       Source origin) {
  std::fprintf(stderr, "%s:%d: strictcpp: %s at %p\n", s.file, s.line,
               reason, reinterpret_cast<void*>(address));
  if (expected)
    std::fprintf(stderr, "  requested: %s; stored: %s\n",
                 readable(expected).c_str(), readable(actual).c_str());
  if (origin.file)
    std::fprintf(stderr, "  object registered at %s:%d\n", origin.file, origin.line);
  std::fflush(stderr);
  std::_Exit(86);
}

// Deduplicate repeated exposure of the same literal/local-static root while holding
// the registry lock. The root region is always first in Builder output.
Id register_object(std::vector<Region> regions, bool heap, bool array, Source s) {
  auto& state = registry();
  std::lock_guard<std::mutex> lock(state.mutex);
  const auto& root = regions.front();
  for (auto it = state.objects.rbegin(); it != state.objects.rend(); ++it) {
    const auto& old = it->regions.front();
    if (it->alive && old.begin == root.begin && old.element.identity == root.element.identity &&
        old.count == root.count) {
      // Repeated string literal exposure and local static registration.
      return it->id;
    }
  }
  const Id id = state.next++;
  state.objects.push_back({id, std::move(regions), heap, array, true, s});
  return id;
}

// Record lifetime expiration without erasing origin/type information needed by later
// diagnostics. Guard destruction and supported delete wrappers call this by registration ID.
void retire(Id id) {
  auto& state = registry();
  std::lock_guard<std::mutex> lock(state.mutex);
  for (auto it = state.objects.rbegin(); it != state.objects.rend(); ++it) {
    if (it->id == id) { it->alive = false; return; }
  }
}

// Hosted main arguments originate outside source instrumentation. Register argv,
// its sentinel slot and terminated strings as an explicit external-entry contract.
void arguments(int argc, char** argv, Source s) {
  if (!argv || argc < 0) return;
  register_block(argv, static_cast<std::size_t>(argc) + 1, false, true, s);
  for (int i = 0; i < argc; ++i)
    if (argv[i]) register_block(argv[i], std::strlen(argv[i]) + 1, false, true, s);
}

// Check address/alignment and choose the most specific live typed region. Matching
// smaller regions lets mutable subobjects override broader const enclosing regions.
// Unknown storage is accepted only under the explicit weaker AST option.
void check(std::uintptr_t address, Type requested, bool write, Source s,
           bool allow_unknown) {
  if (!address) fail("null-access", address, requested.name, nullptr, s);
  if (address % requested.alignment)
    fail("misaligned-access", address, requested.name, nullptr, s);
  auto& state = registry();
  std::lock_guard<std::mutex> lock(state.mutex);
  // Search newest registrations first, retaining separate candidates for type mismatch,
  // expired storage and boundary failures. A compatible region wins before diagnostics.
  const Object* candidate = nullptr;
  const Region* mismatch = nullptr;
  const Object* expired = nullptr;
  const Object* boundary = nullptr;
  const Object* matched_object = nullptr;
  const Region* matched_region = nullptr;
  for (auto it = state.objects.rbegin(); it != state.objects.rend(); ++it) {
    if (!it->alive) {
      if (!expired && contains(it->regions.front(), address, requested.size))
        expired = &*it;
      continue;
    }
    for (const auto& r : it->regions) {
      if (!contains(r, address, requested.size)) {
        if (touches(r, address)) boundary = &*it;
        continue;
      }
      const auto offset = address - r.begin;
      if (requested.bytes ||
          (r.element.key == requested.key && offset % r.element.size == 0)) {
        if (!matched_region || r.count * r.element.size < matched_region->count * matched_region->element.size) {
          matched_region = &r;
          matched_object = &*it;
        }
        continue;
      }
      // Prefer the most specific subobject for a useful diagnostic.
      if (!mismatch || r.count * r.element.size < mismatch->count * mismatch->element.size) {
        candidate = &*it;
        mismatch = &r;
      }
    }
  }
  // A matching access key is insufficient for writes: the selected region must also be
  // writable. Deletion below instead uses exact identity and heap/array classification.
  if (matched_region) {
    if (write && matched_region->element.constant)
      fail("write-to-const", address, requested.name, matched_region->element.name,
           s, matched_object->source);
    return;
  }
  if (mismatch)
    fail("type-mismatch", address, requested.name, mismatch->element.name,
         s, candidate->source);
  if (boundary)
    fail("out-of-bounds", address, requested.name, nullptr, s, boundary->source);
  if (expired)
    fail("expired-object", address, requested.name, nullptr, s, expired->source);
  if (!allow_unknown)
    fail("untracked-access", address, requested.name, nullptr, s);
}

// Validate scalar/array family and exact deletion identity before calling the original
// delete. A registered virtual base subobject may legitimately have a nonzero address
// relative to its complete object, so that case is matched through described regions.
Id prepare_delete(std::uintptr_t address, Type requested, bool array,
                  bool virtual_destructor, Source s) {
  auto& state = registry();
  std::lock_guard<std::mutex> lock(state.mutex);
  for (auto it = state.objects.rbegin(); it != state.objects.rend(); ++it) {
    const auto& root = it->regions.front();
    bool base = false;
    if (!array && virtual_destructor) {
      for (const auto& region : it->regions)
        if (region.begin == address && region.element.identity == requested.identity)
          base = true;
    }
    if (root.begin != address && !base) continue;
    if (!it->alive)
      fail("double-delete", address, requested.name, root.element.name, s, it->source);
    if (!it->heap)
      fail("delete-non-heap", address, requested.name, root.element.name, s, it->source);
    if (array != it->array)
      fail("delete-kind-mismatch", address, requested.name, root.element.name, s, it->source);
    if (root.element.identity != requested.identity && !base)
      fail("delete-type-mismatch", address, requested.name, root.element.name, s, it->source);
    return it->id;
  }
  fail("untracked-delete", address, requested.name, nullptr, s);
}
} // namespace __strictcpp
