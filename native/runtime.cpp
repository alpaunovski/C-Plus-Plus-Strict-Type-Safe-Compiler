// Trusted, uninstrumented runtime called by generated IR. Metadata stays outside
// user storage, preserving native pointer representation and calling conventions.
// This is an experimental object model: locking metadata is not a guarantee against
// a concurrent free between validation and the subsequent machine access.
#include "abi.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>
#include <new>
#include <limits>

namespace {
// The target program may replace global operator new/delete. The checking
// runtime must never allocate through those instrumented user functions.
// All internal containers bypass replaceable global new/delete. Otherwise a user
// allocator instrumented by this compiler could reenter the checker while the checker
// itself allocates metadata. Exhaustion aborts rather than crossing the noexcept C ABI.
template <class T> struct RawAllocator {
  using value_type = T;
  RawAllocator() = default;
  template <class U> RawAllocator(const RawAllocator<U>&) {}
  T* allocate(std::size_t n) {
    if (n > std::numeric_limits<std::size_t>::max() / sizeof(T)) std::abort();
    auto* p = static_cast<T*>(std::malloc(n * sizeof(T)));
    if (!p) std::abort();
    return p;
  }
  void deallocate(T* p, std::size_t) { std::free(p); }
  template <class U> bool operator==(const RawAllocator<U>&) const { return true; }
  template <class U> bool operator!=(const RawAllocator<U>&) const { return false; }
};
template <class T> using Vector = std::vector<T, RawAllocator<T>>;
using String = std::basic_string<char, std::char_traits<char>, RawAllocator<char>>;
template <class K, class V> using Map = std::map<K, V, std::less<K>, RawAllocator<std::pair<const K, V>>>;
template <class K, class V> using HashMap = std::unordered_map<K, V, std::hash<K>, std::equal_to<K>, RawAllocator<std::pair<const K, V>>>;
using Id = std::uint64_t;
using Address = std::uintptr_t;
// Raw storage remembers selected exact-address/width typed stores. These slots are
// a limited effective-type model, not complete C++ implicit object creation.
struct Slot { Address start; std::uint64_t size; String type; };
// Active member identity is separate from type and address: two int members of one
// union still have different lifetimes. Generation changes invalidate saved capabilities.
struct UnionState { Address start; std::uint64_t size; String type; Id member, generation; };
// A saved capability captures a member generation. Comparing only the current type
// would revive old pointers after switching away and back to the same union member.
struct UnionGuard { Address start; String type; Id member, generation; };
// A storage generation owns lifetime, allocation-family and initialization state.
// Retired records remain as tombstones; a reused address must never reuse an expired ID.
struct Object {
  // frame associates automatic storage with its instrumented activation. Non-stack
  // storage normally has frame zero and is not retired merely because a function returns.
  Id id, frame;
  Address start;
  // kind: 0 stack, 1 scalar new, 2 global, 3 calloc, 4 array new, 5 C allocation.
  // The full extent belongs to storage; capabilities may grant narrower subobject bounds.
  std::uint64_t size, kind;
  const ScType* type;
  // Liveness is independent of initialized bytes. The following vector records one
  // initialization byte per storage byte; selected reads must find the entire range initialized.
  bool alive;
  Vector<unsigned char> initialized;
  Vector<Slot> slots;
  const char* file;
  std::uint32_t line;
  // Placement-created children share backing storage but own typed lifetimes. Retiring
  // the backing owner recursively retires its children.
  Id parent = 0;
  Vector<UnionState> unions = {};
};
// Keys are addresses of pointer/integer slots, not pointee addresses. Recovery also
// requires matching actual bits, so a partial overwrite cannot retain old pointer authority.
struct Shadow { std::uint64_t bits; Id id; bool lazy_static = false; };
// A capability narrows authority within an object and can carry a mutable view or
// union guards. It neither allocates user storage nor changes pointer bits.
struct Capability { Id object; Address start; std::uint64_t size; const ScType* view = nullptr;
                    Address view_start = 0; Vector<UnionGuard> unions = {}; };
// A recursive mutex allows runtime helpers to call one another while manipulating
// this registry. It is released before the user instruction executes, leaving the
// documented concurrent-lifetime check/use gap.
struct State {
  std::recursive_mutex mutex;
  Id next = 1;
  HashMap<Id, Object> objects;
  // Ordered lookup is used at explicit address-resolution boundaries. Ordinary shadow
  // loads must not use it to invent missing provenance.
  Map<Address, Id> live;
  Map<Address, Shadow> shadow;
  HashMap<Id, Capability> capabilities;
  // Only supported transparent replacements may forward identities. A new allocation
  // at an old address is never sufficient to create an entry here.
  HashMap<Id, Id> replacements;
  HashMap<void*, String> functions;
  HashMap<void*, String> source_functions;
};
// Allocate once through malloc/placement new and retain the registry through teardown.
// This avoids both user allocation recursion and global-destruction ordering dependencies.
State& state() {
  static auto* s = [] {
    void* memory = std::malloc(sizeof(State));
    if (!memory) std::abort();
    return ::new(memory) State;
  }();
  return *s;
}
// Thread-local call transport preserves the public ABI. Each exact-target call carries
// scalar and aggregate-piece IDs; entered distinguishes instrumented from foreign callees.
struct Pending { void* target; Id owner; Vector<Id> args; Id result = 0; bool entered = false;
                 Vector<Id> results = {}; Vector<Vector<Id>> fields = {}; };
// The pending-call boundary at entry lets exception cleanup remove abandoned children
// without consuming metadata belonging to an unrelated callback.
struct Frame { Id id; void* function; std::size_t pending; };
// Calls and return pieces belong to the executing thread, while storage records are
// process-wide. A thread must never consume another thread's pending argument metadata.
thread_local Vector<Pending> pending;
thread_local Vector<Frame> frames;
thread_local Vector<Id> returned_slots;
thread_local Vector<Id> custom_deletions;

// Terminate before the checked operation. _Exit avoids running user destructors or
// atexit handlers that could revisit invalid state. Exit 86 identifies checker failures;
// compiler errors, resource exhaustion and OS signals have different statuses.
[[noreturn]] void error(const char* reason, Address address, const char* requested,
                        const Object* object, const char* file, std::uint32_t line) {
  std::fprintf(stderr, "%s:%u: strictcpp: %s at %p\n", file ? file : "<native>", line,
               reason, reinterpret_cast<void*>(address));
  if (requested) std::fprintf(stderr, "  requested: %s\n", requested);
  if (object)
    std::fprintf(stderr, "  object #%llu (%s, %llu bytes) registered at %s:%u\n",
                 static_cast<unsigned long long>(object->id),
                 object->type ? object->type->name : "raw storage",
                 static_cast<unsigned long long>(object->size),
                 object->file ? object->file : "<native>", object->line);
  std::fflush(stderr);
  std::_Exit(86);
}
// Find the nearest preceding live start address. This is explicit boundary recovery,
// not a replacement for the transported ID required by ordinary checked accesses.
Object* lookup(Address address) {
  auto& s = state();
  auto it = s.live.upper_bound(address);
  if (it == s.live.begin()) return nullptr;
  auto& o = s.objects.at(std::prev(it)->second);
  return address >= o.start && address - o.start < o.size ? &o : nullptr;
}
// Resolve object/capability identity, follow recorded replacements, then check liveness
// and both allocation/capability bounds. Callers hold the registry lock. size zero allows
// one-past address formation; actual accesses provide the complete width.
Object& require(Address address, Id id, std::uint64_t size, const char* file,
                std::uint32_t line, bool check_union = true) {
  auto& s = state();
  if (!address) error("null-access", address, nullptr, nullptr, file, line);
  if (!id) error("missing-provenance", address, nullptr, nullptr, file, line);
  const auto cap = s.capabilities.find(id);
  Id root = cap == s.capabilities.end() ? id : cap->second.object;
  for (unsigned n = 0; n < 100 && s.replacements.count(root); ++n) root = s.replacements.at(root);
  auto it = s.objects.find(root);
  if (it == s.objects.end()) error("unknown-provenance", address, nullptr, nullptr, file, line);
  auto& o = it->second;
  if (!o.alive) error("expired-object", address, nullptr, &o, file, line);
  if (cap != s.capabilities.end()) {
    const auto& bounds = cap->second;
    if (address < bounds.start || address - bounds.start > bounds.size || size > bounds.size - (address - bounds.start))
      error("subobject-out-of-bounds", address, nullptr, &o, file, line);
    // Selecting an inactive address is distinct from accessing its value. Value access
    // must validate both the current member index and the captured lifetime generation.
    if (check_union) for (const auto& guard : bounds.unions) {
      auto active = std::find_if(o.unions.begin(), o.unions.end(), [&](const UnionState& u) {
        return u.start == guard.start && u.type == guard.type;
      });
      if (active == o.unions.end() || active->member != guard.member)
        error("union-inactive-member", address, guard.type.c_str(), &o, file, line);
      if (active->generation != guard.generation)
        error("expired-union-member", address, guard.type.c_str(), &o, file, line);
    }
  }
  if (address < o.start || address - o.start > o.size || size > o.size - (address - o.start))
    error("out-of-bounds", address, nullptr, &o, file, line);
  return o;
}
// Remove all eight-byte shadow slots touched by a write, including slots starting up
// to seven bytes before it. Partial stores must destroy the authority of the old bits.
void forget_shadow(Address address, std::uint64_t size) {
  auto& shadow = state().shadow;
  auto it = shadow.lower_bound(address >= 7 ? address - 7 : 0);
  while (it != shadow.end() && it->first < address + size) it = shadow.erase(it);
}
// Keep the ID as an expired tombstone, end placement children, remove live-address
// resolution and forget stored pointer metadata. Saved IDs remain detectably expired.
void retire(Object& object) {
  if (!object.alive) return;
  object.alive = false;
  for (auto& entry : state().objects)
    if (entry.second.parent == object.id && entry.second.alive) retire(entry.second);
  auto& s = state();
  auto it = s.live.find(object.start);
  if (it != s.live.end() && it->second == object.id) s.live.erase(it);
  forget_shadow(object.start, object.size);
}
// Normalize corresponding integer aliases and available TBAA pointer labels.
// Character-pointee normalization is deliberately broad and is a documented pointer-
// storage hole; strict admission rejects stored pointer objects.
String canonical(String name) {
  if (name == "unsigned char") return "signed char";
  if (name == "unsigned int") return "int";
  if (name == "unsigned long") return "long";
  if (name == "unsigned long long") return "long long";
  if (name == "unsigned short") return "short";
  if (name == "unsigned __int128") return "__int128";
  if (name.size() >= 3 && name[0] == 'p') {
    const auto space = name.find(' ');
    if (space != String::npos) {
      const auto base = name.substr(space + 1);
      if (base == "char" || base == "signed char" || base == "unsigned char")
        return name.substr(0, space + 1) + "omnipotent char";
    }
  }
  return name;
}
// Compare the limited normalized vocabulary. any pointer is a fallback for erased
// pointee information, not a complete source-language type-equivalence relation.
bool compatible(const char* actual, const char* wanted) {
  const auto a = canonical(actual), b = canonical(wanted);
  if (a == b) return true;
  if (a == "any pointer" && (b == "any pointer" || b.rfind("p", 0) == 0)) return true;
  if ((a == "any pointer" || a.rfind("p", 0) == 0) && b == "any pointer") return true;
  return false;
}
// Return 0 for mismatch, 1 for writable matching storage, 2 for const storage.
// Descend declared array/record layout while inheriting constness. Beyond 0/1/2,
// -1 reports unknown/inactive union membership. Extent and depth checks bound traversal
// of incomplete or recursive descriptors.
int match(const ScType* type, std::uint64_t offset, std::uint64_t size,
          const char* expected, bool inherited_const = false, unsigned depth = 0,
          const Object* object = nullptr, Address type_start = 0) {
  if (!type || !type->size || depth > 80 || offset > type->size || size > type->size - offset) return 0;
  bool constant = inherited_const || (type->flags & SC_CONST);
  int result = 0;
  if (!offset && size == type->size && compatible(type->name, expected)) result = constant ? 2 : 1;
  if (result) return result;
  std::uint64_t active_member = 0;
  if (type->flags & SC_UNION) {
    if (object) for (const auto& u : object->unions)
      if (u.start == type_start && u.type == type->name) { active_member = u.member; break; }
    if (!active_member) return -1;
  }
  for (std::uint64_t i = 0; i < type->fields_count; ++i) {
    if ((type->flags & SC_UNION) && i + 1 != active_member) continue;
    const auto& field = type->fields[i];
    if (!field.type || !field.type->size || offset < field.offset) continue;
    const auto relative = offset - field.offset;
    if (relative / field.type->size >= field.count) continue;
    const int nested = match(field.type, relative % field.type->size, size, expected,
                             constant, depth + 1, object,
                             type_start + field.offset + (relative / field.type->size) * field.type->size);
    if (nested == -1) return -1;
    if (nested) result = nested;
  }
  if (!result && (type->flags & SC_UNION)) return -1;
  return result;
}
// Validate the enclosing record path, not merely its final scalar field. Otherwise
// unrelated same-layout records could match because each happens to contain an int.
bool container_match(const ScType* type, std::uint64_t offset, const char* name, unsigned depth = 0) {
  if (!type || !type->size || depth > 80 || offset >= type->size) return false;
  if (!offset && std::strcmp(type->name, name) == 0) return true;
  for (std::uint64_t i = 0; i < type->fields_count; ++i) {
    const auto& field = type->fields[i];
    if (!field.type || !field.type->size || offset < field.offset) continue;
    const auto relative = offset - field.offset;
    if (relative / field.type->size < field.count &&
        container_match(field.type, relative % field.type->size, name, depth + 1)) return true;
  }
  return false;
}
// A write to a placement child also initializes the corresponding backing bytes.
// Initialization alone does not establish the remaining type or lifetime properties.
void mark_initialized(Object& o, Address address, std::uint64_t size) {
  if (!size) return;
  std::fill_n(o.initialized.data() + (address - o.start), size, 1);
  if (o.parent) mark_initialized(state().objects.at(o.parent), address, size);
}
}

// Start an activation and claim only a pending call whose exact target matches this
// function. An uninstrumented callback must not steal metadata from an outer native call.
extern "C" std::uint64_t __sc_enter(void* function) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  const Id id = s.next++;
  frames.push_back({id, function, pending.size()});
  if (!pending.empty() && pending.back().target == function && !pending.back().entered)
    pending.back().entered = true;
  return id;
}
// Retire automatic objects in abandoned child frames and trim pending calls. Preserve
// the surviving landing-pad frame so its catch block can continue normally.
extern "C" void __sc_unwind(std::uint64_t frame) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  while (!frames.empty() && frames.back().id != frame) {
    const Id child = frames.back().id;
    for (auto& entry : s.objects)
      if (entry.second.frame == child && entry.second.kind == 0) retire(entry.second);
    frames.pop_back();
  }
  if (!frames.empty() && pending.size() > frames.back().pending)
    pending.resize(frames.back().pending);
}
// End automatic storage in this activation, unwind residual children, and publish its
// scalar return ID. Aggregate return pieces use independent slot helpers below.
extern "C" void __sc_leave(std::uint64_t frame, std::uint64_t result) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  for (auto& entry : s.objects)
    if (entry.second.frame == frame && entry.second.kind == 0) retire(entry.second);
  __sc_unwind(frame);
  if (!frames.empty() && frames.back().id == frame) {
    auto function = frames.back().function;
    frames.pop_back();
    if (!pending.empty() && pending.back().target == function) pending.back().result = result;
  }
}
// Create a fresh storage generation, except duplicate COMDAT/linkonce global roots.
// Globals/calloc begin initialized; stack/new/malloc storage requires checked writes
// or transferred initialization. Address reuse retires the previous registration.
extern "C" std::uint64_t __sc_register(void* pointer, std::uint64_t size, const ScType* type,
                                       std::uint64_t kind, const char* file, std::uint32_t line) noexcept {
  if (!pointer) return 0;
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  Address address = reinterpret_cast<Address>(pointer);
  // Linkonce/COMDAT globals can be registered by several translation units.
  auto old = s.live.find(address);
  if (old != s.live.end()) {
    auto& object = s.objects.at(old->second);
    if (kind == 2 && object.kind == 2) return object.id;
    retire(object);
  }
  const Id id = s.next++;
  const Id frame = kind == 0 && !frames.empty() ? frames.back().id : 0;
  Object o{id, frame, address, size, kind, type, true,
           Vector<unsigned char>(size, kind == 2 || kind == 3), {}, file, line};
  s.objects.emplace(id, std::move(o));
  s.live[address] = id;
  return id;
}
// Retire the object ID emitted for a recognized LLVM lifetime marker. This helper
// expects an object ID from the tracked alloca slot, rather than resolving an address anew.
extern "C" void __sc_end(void*, std::uint64_t id) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  auto it = s.objects.find(id);
  if (it != s.objects.end()) retire(it->second);
}
// Recover identity only at compiler-selected boundaries such as globals, hosted main
// arguments and limited foreign returns. Resolving every missing shadow would turn
// stale addresses into fresh identities after allocation reuse.
extern "C" std::uint64_t __sc_resolve(void* pointer) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  auto* o = lookup(reinterpret_cast<Address>(pointer));
  return o ? o->id : 0;
}
// Restrict authority to a field or array-row extent while preserving views and union
// guards. Activity checks are deferred until value access so inactive member addresses
// can be formed without immediately pretending that their values are live.
extern "C" std::uint64_t __sc_narrow(void* pointer, std::uint64_t id, std::uint64_t size,
                                    const char* file, std::uint32_t line) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  auto& o = require(reinterpret_cast<Address>(pointer), id, size, file, line, false);
  const Id narrowed = s.next++;
  s.capabilities[narrowed] = {o.id, reinterpret_cast<Address>(pointer), size};
  if (auto it = s.capabilities.find(id); it != s.capabilities.end()) {
    s.capabilities[narrowed].view = it->second.view;
    s.capabilities[narrowed].view_start = it->second.view_start;
    s.capabilities[narrowed].unions = it->second.unions;
  }
  return narrowed;
}
// Supply the selected mutable-field type view without replacing the root identity.
// This does not grant write permission to every member of a const enclosing object.
extern "C" std::uint64_t __sc_view(void* pointer, std::uint64_t id, const ScType* type,
                                   const char* file, std::uint32_t line) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  if (!type) return id;
  const auto result = __sc_narrow(pointer, id, type->size, file, line);
  s.capabilities.at(result).view = type;
  s.capabilities.at(result).view_start = reinterpret_cast<Address>(pointer);
  return result;
}
// These identity stubs make frontend semantic events visible to the LLVM pass.
// The pass inserts checks/ID operations around them; the stubs alone do not enforce
// safety and are not a supported user API.
extern "C" void* __sc_front_placement(void* p, std::uint64_t, const char*) noexcept { return p; }
extern "C" void* __sc_front_downcast(void* p, std::uint64_t, const char*, std::uint64_t) noexcept { return p; }
extern "C" void* __sc_front_access(void* p, std::uint64_t, const char*, std::uint64_t) noexcept { return p; }
extern "C" void* __sc_front_union(void* p, std::uint64_t, const char*, std::uint64_t, std::uint64_t) noexcept { return p; }
// Capture a scalar member capability and optionally activate a supported member.
// Mode 1 resets value state for a new member; mode 2 preserves already supplied global
// initialization. Mode zero only selects the address.
extern "C" std::uint64_t __sc_union_select(void* pointer, std::uint64_t id, std::uint64_t size,
                 const char* type, std::uint64_t member, std::uint64_t activate,
                 const char* file, std::uint32_t line) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  const auto address = reinterpret_cast<Address>(pointer);
  auto& o = require(address, id, size, file, line, false);
  auto active = std::find_if(o.unions.begin(), o.unions.end(), [&](const UnionState& u) {
    return u.start == address && u.type == type;
  });
  if (active == o.unions.end()) {
    o.unions.push_back({address, size, type, 0, 0});
    active = std::prev(o.unions.end());
  }
  // Assignment to the member already active preserves its lifetime. Only a real member
  // switch gets a fresh generation and discards the previous value/shadow state.
  if (activate && active->member != member) {
    active->member = member;
    active->generation = s.next++;
    // A new member lifetime cannot inherit initialized-value or pointer state.
    if (activate == 1) {
      std::fill_n(o.initialized.data() + (address - o.start), size, 0);
      forget_shadow(address, size);
    }
  }
  const Id selected = s.next++;
  Capability cap{o.id, address, size};
  if (auto inherited = s.capabilities.find(id); inherited != s.capabilities.end()) cap = inherited->second;
  cap.start = address; cap.size = size;
  cap.unions.push_back({address, type, member, active->generation});
  s.capabilities[selected] = std::move(cap);
  return selected;
}
// Check that the declared root contains the requested derived object at the adjusted
// address, accounting for a nonzero base offset. Similar field layout is not sufficient
// reason to grant the wider derived extent.
extern "C" std::uint64_t __sc_downcast(void* pointer, std::uint64_t id, std::uint64_t size,
                                      const char* name, std::uint64_t offset, const char* file, std::uint32_t line) noexcept {
  if (!pointer) return 0;
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  const auto address = reinterpret_cast<Address>(pointer);
  auto& o = require(address, id, 0, file, line);
  if (address < offset || address - offset < o.start || !o.type || !o.type->size ||
      !container_match(o.type, (address - offset - o.start) % o.type->size, name))
    error("type-mismatch", address, name, &o, file, line);
  const auto target = address - offset;
  if (target - o.start > o.size || size > o.size - (target - o.start))
    error("out-of-bounds", target, name, &o, file, line);
  const Id result = s.next++;
  s.capabilities[result] = {o.id, target, size};
  if (auto inherited = s.capabilities.find(id); inherited != s.capabilities.end())
    s.capabilities[result].unions = inherited->second.unions;
  return result;
}
// Create a typed child inside supported raw/byte backing storage. End children at that
// start and forward old IDs only for the supported non-const same-type replacement.
// Arbitrary typed-storage reuse remains unsupported.
extern "C" std::uint64_t __sc_placement(void* pointer, std::uint64_t id, std::uint64_t size,
                                       const ScType* type, const char* file, std::uint32_t line) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  auto& destination = require(reinterpret_cast<Address>(pointer), id, size, file, line);
  auto raw = [](const ScType* t) {
    while (t && (t->flags & SC_ARRAY) && t->fields_count) t = t->fields[0].type;
    return !t || (t->flags & (SC_BYTES | SC_RAW));
  };
  const Id parent = destination.parent ? destination.parent : destination.id;
  auto& owner = s.objects.at(parent);
  if (!raw(owner.type)) error("unsupported-storage-reuse", reinterpret_cast<Address>(pointer), type ? type->name : nullptr, &owner, file, line);
  Id replace = 0;
  for (auto& entry : s.objects) {
    auto& child = entry.second;
    if (!child.alive || child.parent != parent || child.start != reinterpret_cast<Address>(pointer)) continue;
    if (child.type && type && !(child.type->flags & SC_CONST) && child.size == size &&
        std::strcmp(child.type->name, type->name) == 0) replace = child.id;
    retire(child);
  }
  const Id fresh = s.next++;
  const Id frame = owner.frame;
  const auto kind = owner.kind;
  Object object{fresh, frame, reinterpret_cast<Address>(pointer), size, kind, type, true,
                 Vector<unsigned char>(size, 0), {}, file, line, parent};
  s.objects.emplace(fresh, std::move(object));
  s.live[reinterpret_cast<Address>(pointer)] = fresh;
  if (replace) s.replacements[replace] = fresh;
  return fresh;
}
// Central load/store validation. flags: write=1, byte/aggregate=2, read-modify-write=4;
// alignment occupies the upper 32 bits. Enclosing TBAA record identity and final access
// type are checked separately before updating initialization/shadow state.
extern "C" void __sc_access_v2(void* pointer, std::uint64_t id, std::uint64_t size,
                            const char* expected, const char* base, std::uint64_t base_offset, std::uint64_t flags,
                            const char* file, std::uint32_t line) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  const Address address = reinterpret_cast<Address>(pointer);
  const auto alignment = flags >> 32;
  if (alignment && address % alignment)
    error("misaligned-access", address, expected, nullptr, file, line);
  auto& o = require(address, id, size, file, line);
  const bool write = flags & 1, bytes = flags & 2;
  int found = 0;
  const ScType* container = o.type;
  Address container_start = o.start;
  if (!container)
    if (auto* child = lookup(address); child && child->parent == o.id && child->type) {
      container = child->type;
      container_start = child->start;
    }
  // A mutable view or placement child may be more specific than its raw/const owner.
  // Container-path checks still retain the original root layout.
  const ScType* actual = o.type;
  Address actual_start = o.start;
  if (auto it = s.capabilities.find(id); it != s.capabilities.end() && it->second.view) {
    actual = it->second.view;
    actual_start = it->second.view_start;
  } else if (!actual) {
    if (auto* child = lookup(address); child && child->parent == o.id && child->type) {
      actual = child->type;
      actual_start = child->start;
    }
  }
  if (actual && actual->size) {
    const auto relative = address - actual_start;
    const auto container_relative = address - container_start;
    if (base && std::strncmp(base, "_ZTS", 4) == 0 && container && container->size &&
        (container_relative < base_offset || !container_match(container, (container_relative - base_offset) % container->size, base)))
      error("type-mismatch", address, base, &o, file, line);
    found = match(actual, relative % actual->size, size, expected, false, 0, &o,
                  actual_start + (relative / actual->size) * actual->size);
    if (found == -1 && !bytes) error("union-inactive-member", address, expected, &o, file, line);
    if (!found && !bytes) error("type-mismatch", address, expected, &o, file, line);
    if (write && ((found == 2) || ((actual->flags & SC_CONST) && !found))) {
      const auto begin = o.initialized.data() + (address - o.start);
      if (std::find(begin, begin + size, 0) == begin + size)
        error("write-to-const", address, expected, &o, file, line);
    }
  // Raw storage uses remembered exact-width typed slots where available. This is a
  // limited fallback rather than a full implementation of implicit lifetime/effective type.
  } else if (!bytes) {
    for (const auto& slot : o.slots)
      if (slot.start == address && slot.size == size) {
        found = compatible(slot.type.c_str(), expected);
        if (!found) error("type-mismatch", address, expected, &o, file, line);
        break;
      }
    if (write && !found) o.slots.push_back({address, size, expected});
  }
  // A compound operation must validate its old value before the resulting write is marked
  // initialized. Byte/aggregate representation operations follow separate limited rules.
  if ((!write || (flags & 4)) && !bytes) {
    const auto begin = o.initialized.data() + (address - o.start);
    if (std::find(begin, begin + size, 0) != begin + size)
      error("uninitialized-read", address, expected, &o, file, line);
  }
  if (write) {
    mark_initialized(o, address, size);
    forget_shadow(address, size);
  }
}
// Associate newly stored bits with the transported ID. Any later overlapping arbitrary
// write clears this entry; a load can recover it only if its actual bits still match.
extern "C" void __sc_shadow_store(void* storage, std::uint64_t bits, std::uint64_t id) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  s.shadow[reinterpret_cast<Address>(storage)] = {bits, id};
}
// Static pointer initializers get an explicit lazy-resolution marker for module
// registration order. Ordinary runtime stores never receive this recovery privilege.
extern "C" void __sc_shadow_static(void* storage, std::uint64_t bits) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  s.shadow[reinterpret_cast<Address>(storage)] = {bits, 0, true};
}
// A missing/mismatched ordinary shadow yields zero instead of looking up a live address.
// A subsequent non-null checked access then fails rather than acquiring unrelated authority.
extern "C" std::uint64_t __sc_shadow_load(void* storage, std::uint64_t bits) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  auto it = s.shadow.find(reinterpret_cast<Address>(storage));
  if (it == s.shadow.end() || it->second.bits != bits) return 0;
  if (it->second.lazy_static) {
    it->second.id = __sc_resolve(reinterpret_cast<void*>(static_cast<Address>(bits)));
    if (it->second.id) it->second.lazy_static = false;
  }
  return it->second.id;
}
// Push exact-target call metadata before invocation. Subsequent argument helpers fill
// this record while the machine-level calling convention remains unchanged.
extern "C" void __sc_call_begin(void* target, std::uint64_t count) noexcept {
  pending.push_back({target, frames.empty() ? 0 : frames.back().id, Vector<Id>(count)});
}
// Transport one scalar pointer/integer argument ID. Missing or short records retain
// zero metadata rather than writing past a foreign/incomplete transport boundary.
extern "C" void __sc_call_arg(std::uint64_t index, std::uint64_t id) noexcept {
  if (!pending.empty() && index < pending.back().args.size()) pending.back().args[index] = id;
}
// Keep each aggregate ABI piece independent. One ID for an entire struct would lose
// embedded pointer identities and could revive a stale pointer when addresses are reused.
extern "C" void __sc_call_arg_slot(std::uint64_t argument, std::uint64_t slot, std::uint64_t id) noexcept {
  if (pending.empty() || argument >= pending.back().args.size()) return;
  auto& fields = pending.back().fields;
  if (fields.size() <= argument) fields.resize(argument + 1);
  if (fields[argument].size() <= slot) fields[argument].resize(slot + 1);
  fields[argument][slot] = id;
}
// Consume a matching native call only for this exact function. Foreign entries and
// callbacks use limited live-address recovery and remain explicit trust boundaries.
extern "C" std::uint64_t __sc_argument(std::uint64_t index, void* raw) noexcept {
  if (!frames.empty() && !pending.empty() && pending.back().target == frames.back().function &&
      index < pending.back().args.size()) return pending.back().args[index];
  return __sc_resolve(raw); // Explicit foreign entry/callback boundary.
}
// Do not reconstruct missing aggregate pieces from raw addresses. An unrelated pending
// call or foreign entry cannot supply authority for these fields.
extern "C" std::uint64_t __sc_argument_slot(std::uint64_t argument, std::uint64_t slot) noexcept {
  if (frames.empty() || pending.empty() || pending.back().target != frames.back().function) return 0;
  const auto& fields = pending.back().fields;
  return argument < fields.size() && slot < fields[argument].size() ? fields[argument][slot] : 0;
}
// Pop this target's matching call and expose its returned pieces. A foreign callee
// that did not enter instrumentation cannot manufacture a transported scalar return ID.
extern "C" std::uint64_t __sc_call_end(void* target) noexcept {
  if (pending.empty() || pending.back().target != target) return 0;
  const Id result = pending.back().result;
  const bool entered = pending.back().entered;
  returned_slots = std::move(pending.back().results);
  pending.pop_back();
  return entered ? result : 0;
}
// Publish an aggregate result piece to the matching pending call. Piece ordering is
// defined by the pass's traversal of the selected target data layout.
extern "C" void __sc_return_slot(std::uint64_t slot, std::uint64_t id) noexcept {
  if (frames.empty() || pending.empty() || pending.back().target != frames.back().function) return;
  auto& results = pending.back().results;
  if (results.size() <= slot) results.resize(slot + 1);
  results[slot] = id;
}
// Read the piece vector preserved by call_end. Absent pieces remain zero so callers
// cannot silently recover missing identities from addresses.
extern "C" std::uint64_t __sc_returned_slot(std::uint64_t slot) noexcept {
  return slot < returned_slots.size() ? returned_slots[slot] : 0;
}
// Validate heap ownership, exact allocation start and deallocation family. Bit 0x100
// requests validation without immediate retirement for a delete callback or realloc.
extern "C" void __sc_release(void* pointer, std::uint64_t id, std::uint64_t kind,
                             const char* file, std::uint32_t line) noexcept {
  if (!pointer) return;
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  auto& o = require(reinterpret_cast<Address>(pointer), id, 0, file, line);
  if (!o.kind || o.kind == 2) error("delete-non-heap", o.start, nullptr, &o, file, line);
  if (o.start != reinterpret_cast<Address>(pointer)) error("interior-delete", reinterpret_cast<Address>(pointer), nullptr, &o, file, line);
  const auto family = kind & 0xff;
  bool compatible_family = family == 0 ? (o.kind == 3 || o.kind == 5) : (family == 1 ? o.kind == 1 : o.kind == 4);
  if (family == 0 && std::find(custom_deletions.begin(), custom_deletions.end(), o.id) != custom_deletions.end()) compatible_family = true;
  if (!compatible_family) error("delete-kind-mismatch", o.start, nullptr, &o, file, line);
  if (!(kind & 0x100)) retire(o);
}
// Keep identity live through a replaceable C++ delete call, including external sized-
// delete forwarding. A nested free is permitted only for this exact recorded root ID.
extern "C" void __sc_custom_delete_begin(void* pointer, std::uint64_t id, std::uint64_t family,
                                         const char* file, std::uint32_t line) noexcept {
  if (!pointer) { custom_deletions.push_back(0); return; }
  __sc_release(pointer, id, family | 0x100, file, line);
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  custom_deletions.push_back(require(reinterpret_cast<Address>(pointer), id, 0, file, line).id);
}
// Retire after the callback returns. Pair nested deletes using TLS IDs rather than
// looking up addresses that the callback may already have freed or reused.
extern "C" void __sc_custom_delete_end(void*, std::uint64_t) noexcept {
  if (custom_deletions.empty()) return;
  const Id id = custom_deletions.back(); custom_deletions.pop_back();
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  auto it = s.objects.find(id);
  if (it != s.objects.end()) retire(it->second);
}
// Validate before realloc can invalidate storage. A non-null realloc(p, 0) is explicitly
// rejected because its platform-dependent deallocation behavior is not modeled.
extern "C" void __sc_realloc_guard(void* pointer, std::uint64_t id, std::uint64_t size,
                                  const char* file, std::uint32_t line) noexcept {
  if (pointer && !size) error("unsupported-zero-realloc", reinterpret_cast<Address>(pointer), nullptr, nullptr, file, line);
  __sc_release(pointer, id, 0x100, file, line);
}
// Successful realloc gets a fresh ID even at the same address; transfer only the
// preserved prefix of initialization, typed slots and pointer shadows. Failure returns
// zero for the result while leaving the original registration live.
extern "C" std::uint64_t __sc_reallocated(void* old, void* fresh, std::uint64_t size,
                                         const char* file, std::uint32_t line) noexcept {
  if (!fresh) return 0;
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  Vector<unsigned char> initialized;
  Vector<Slot> slots;
  Vector<std::pair<Address, Shadow>> shadow;
  Address address = reinterpret_cast<Address>(old);
  if (auto* o = lookup(address)) {
    initialized = o->initialized;
    slots = o->slots;
    for (auto it = s.shadow.lower_bound(address); it != s.shadow.end() && it->first - address < o->size; ++it)
      shadow.push_back({it->first - address, it->second});
    retire(*o);
  }
  const Id id = __sc_register(fresh, size, nullptr, 5, file, line);
  auto& o = s.objects.at(id);
  std::copy_n(initialized.begin(), std::min<std::size_t>(size, initialized.size()), o.initialized.begin());
  for (auto slot : slots) {
    auto offset = slot.start - address;
    if (offset <= size && slot.size <= size - offset) {
      slot.start = o.start + offset;
      o.slots.push_back(std::move(slot));
    }
  }
  for (const auto& entry : shadow)
    if (entry.first <= size && sizeof(void*) <= size - entry.first)
      s.shadow[o.start + entry.first] = entry.second;
  return id;
}
// Validate both ranges and snapshot metadata before overlapping writes can destroy it.
// Initialization and complete shadow/typed-slot entries accompany the byte copy. The
// actual instruction executes separately, so this lock is not an atomic check/copy barrier.
extern "C" void __sc_copy(void* destination, std::uint64_t destination_id,
                          void* source, std::uint64_t source_id, std::uint64_t size,
                          const char* file, std::uint32_t line) noexcept {
  if (!size) return;
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  Address to = reinterpret_cast<Address>(destination), from = reinterpret_cast<Address>(source);
  auto& dst = require(to, destination_id, size, file, line);
  auto& src = require(from, source_id, size, file, line);
  Vector<unsigned char> initialized(src.initialized.data() + (from - src.start),
                                   src.initialized.data() + (from - src.start) + size);
  Vector<std::pair<Address, Shadow>> copy;
  for (auto it = s.shadow.lower_bound(from); it != s.shadow.end() && it->first - from < size; ++it)
    if (sizeof(void*) <= size - (it->first - from)) copy.push_back({it->first - from, it->second});
  forget_shadow(to, size);
  std::copy(initialized.begin(), initialized.end(), dst.initialized.data() + (to - dst.start));
  for (const auto& entry : copy) s.shadow[to + entry.first] = entry.second;
  const auto source_slots = src.slots;
  for (const auto& slot : source_slots)
    if (slot.start >= from && slot.start - from <= size && slot.size <= size - (slot.start - from))
      dst.slots.push_back({to + slot.start - from, slot.size, slot.type});
  if (to != from) {
    // Only a complete copied union carries its active-member state. Partial
    // byte copies preserve the destination's existing member lifetime.
    const auto source_unions = src.unions;
    for (const auto& u : source_unions) {
      if (u.start < from || u.start - from > size || u.size > size - (u.start - from)) continue;
      const auto start = to + (u.start - from);
      auto target = std::find_if(dst.unions.begin(), dst.unions.end(), [&](const UnionState& v) {
        return v.start == start && v.type == u.type;
      });
      // Copying into the member already active preserves that lifetime and its saved pointers.
      // Changing the active member requires a fresh generation.
      const Id generation = target != dst.unions.end() && target->member == u.member
                                ? target->generation : s.next++;
      UnionState copied{start, u.size, u.type, u.member, generation};
      if (target == dst.unions.end()) dst.unions.push_back(std::move(copied));
      else *target = std::move(copied);
    }
  }
}
// A byte fill initializes its destination and destroys overlapping pointer shadows.
// It cannot reconstruct pointer identities or model every const/union lifetime rule.
extern "C" void __sc_fill(void* destination, std::uint64_t id, std::uint64_t size,
                          const char* file, std::uint32_t line) noexcept {
  if (!size) return;
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  auto& o = require(reinterpret_cast<Address>(destination), id, size, file, line);
  mark_initialized(o, reinterpret_cast<Address>(destination), size);
  forget_shadow(reinterpret_cast<Address>(destination), size);
}
// Hosted main receives storage from outside instrumentation. Register the argv array,
// its null sentinel and terminated strings, then seed shadows for its pointer entries.
// This is a specific entry contract, not permission to resolve all unknown addresses.
extern "C" void __sc_main_args(int argc, char** argv) noexcept {
  static const ScType pointer{"any pointer", sizeof(void*), SC_POINTER, 0, nullptr};
  static const ScType character{"char", 1, SC_BYTES, 0, nullptr};
  __sc_register(argv, (static_cast<std::uint64_t>(argc) + 1) * sizeof(void*), &pointer, 2, "<argv>", 0);
  for (int i = 0; i < argc; ++i) {
    const auto id = __sc_register(argv[i], std::strlen(argv[i]) + 1, &character, 2, "<argv>", 0);
    __sc_shadow_store(argv + i, reinterpret_cast<Address>(argv[i]), id);
  }
  __sc_shadow_store(argv + argc, 0, 0);
}
// Accumulate GEP offsets with overflow intrinsics before pointer formation. Invalid
// arithmetic must reach diagnostics rather than first becoming unchecked LLVM poison.
extern "C" std::int64_t __sc_offset(std::int64_t previous, std::int64_t index,
                                    std::uint64_t scale, const char* file, std::uint32_t line) noexcept {
  std::int64_t step = 0, result = 0;
  if (scale > INT64_MAX || __builtin_mul_overflow(index, static_cast<std::int64_t>(scale), &step) ||
      __builtin_add_overflow(previous, step, &result))
    error("index-overflow", 0, nullptr, nullptr, file, line);
  return result;
}
// Check numeric address formation and one-past bounds. Null-plus-zero is a deliberate
// library-sentinel compatibility exception; a null dereference still fails. Union value
// activity is checked later, rather than during address selection.
extern "C" void __sc_gep(void* pointer, std::uint64_t id, std::int64_t offset,
                         const char* file, std::uint32_t line) noexcept {
  if (!pointer && !offset) return; // Null sentinel arithmetic used by libc++.
  const auto base = reinterpret_cast<Address>(pointer);
  const auto magnitude = offset < 0 ? std::uint64_t(-(offset + 1)) + 1 : std::uint64_t(offset);
  if ((offset < 0 && magnitude > base) || (offset >= 0 && magnitude > UINTPTR_MAX - base))
    error("index-overflow", base, nullptr, nullptr, file, line);
  const auto target = offset < 0 ? base - magnitude : base + magnitude;
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  require(target, id, 0, file, line, false);
}
// Register the lowered function signature/calling convention during module setup,
// before instrumented indirect calls can execute.
extern "C" void __sc_function(void* pointer, const char* signature) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  s.functions.emplace(pointer, signature);
}
// Reject null, unregistered and lowered-signature-incompatible calls before invocation.
// Opaque LLVM pointers erase pointee types, so source signatures complement this check.
extern "C" void __sc_indirect(void* pointer, const char* signature, const char* file, std::uint32_t line) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  const auto found = s.functions.find(pointer);
  if (!pointer) error("null-call", 0, signature, nullptr, file, line);
  if (found == s.functions.end()) error("unregistered-call-target", reinterpret_cast<Address>(pointer), signature, nullptr, file, line);
  if (found->second != signature) error("call-type-mismatch", reinterpret_cast<Address>(pointer), signature, nullptr, file, line);
}
// Register the frontend's normalized free/static-function type. Compatible noexcept
// conversions have already been normalized when that signature was built.
extern "C" void __sc_function_source(void* pointer, const char* signature) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  s.source_functions.emplace(pointer, signature);
}
// Compare source types only when a checked definition supplied one. Foreign declarations
// retain the separate IR-signature check and remain listed coverage boundaries.
extern "C" void __sc_source_call(void* pointer, const char* signature, const char* file, std::uint32_t line) noexcept {
  auto& s = state();
  std::lock_guard<std::recursive_mutex> lock(s.mutex);
  const auto found = s.source_functions.find(pointer);
  // Foreign function declarations retain the separate IR-signature check and
  // remain explicit trust boundaries in the coverage report.
  if (found != s.source_functions.end() && found->second != signature)
    error("call-type-mismatch", reinterpret_cast<Address>(pointer), signature, nullptr, file, line);
}
// Like the other frontend stubs, return identical pointer bits. Enforcement is added
// by the pass; this function alone is not an indirect-call safety check.
extern "C" void* __sc_front_call_target(void* p, const char*) noexcept { return p; }
