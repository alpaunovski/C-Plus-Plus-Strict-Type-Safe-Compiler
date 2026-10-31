// Pipeline invariant: inspect the frontend's original IR before ordinary optimization,
// then leave well-formed instrumented IR for the requested optimization level. Runtime
// calls carry hidden metadata; user pointer bits and public function ABIs stay native.
// Native, pre-optimization instrumentation. No user-source rewriting.
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/BinaryFormat/Dwarf.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/DebugInfo.h"
#include "llvm/Analysis/ValueTracking.h"
#include "llvm/IR/DebugProgramInstruction.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/IR/IntrinsicInst.h"
#include "llvm/IR/GetElementPtrTypeIterator.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/FormatVariadic.h"
#include "llvm/Transforms/Utils/BasicBlockUtils.h"
#include "llvm/Transforms/Utils/ModuleUtils.h"
#include <map>
#include <string>
#include <set>

using namespace llvm;
namespace {
// One instance owns module-level descriptor/signature caches and per-function SSA
// transport state. Coverage gaps are conservative observations, not a proof that every
// unreported language feature is enforced.
class NativePass : public PassInfoMixin<NativePass> {
  Module* M = nullptr;
  LLVMContext* C = nullptr;
  const DataLayout* DL = nullptr;
  Type *P = nullptr, *I64 = nullptr, *I32 = nullptr, *Void = nullptr;
  StructType *Descriptor = nullptr, *Field = nullptr;
  // Descriptors are interned before their children are built so recursive debug types
  // can refer to the same placeholder global instead of recursing forever.
  DenseMap<DIType*, GlobalVariable*> Types;
  DenseMap<Type*, GlobalVariable*> IRTypes;
  std::map<std::string, Constant*> Strings;
  std::map<std::string, DIType*> NamedTypes;
  std::set<std::string> Foreign;
  DenseMap<Function*, std::string> SourceSignatures;
  DenseMap<GlobalVariable*, std::string> UnionInitializers;
  // Per-function IDs travel beside original SSA values. AggregateIds holds independent
  // pieces; AggregateSources remembers a load address for selected bulk metadata copies.
  DenseMap<Value*, Value*> Tokens;
  DenseMap<Value*, Value*> AggregateSources;
  DenseMap<Value*, SmallVector<Value*, 4>> AggregateIds;
  // Describe one eligible ABI piece by aggregate extraction path, byte offset and bit
  // shift within wide integers. Target DataLayout determines layout rather than source spelling.
  struct Piece { SmallVector<unsigned, 4> path; std::uint64_t offset; unsigned shift; Type* type; };
  DenseMap<Type*, SmallVector<Piece, 4>> Pieces;
  // Alloca identities use mutable hidden slots because lifetime.start can register a
  // new generation when control flow reuses the same stack allocation.
  DenseMap<AllocaInst*, AllocaInst*> TokenSlots;
  DenseMap<AllocaInst*, DIType*> Variables;
  Value* Frame = nullptr;
  std::uint64_t Accesses = 0, Allocations = 0, Functions = 0, Gaps = 0;

  // Keep ABI scalar constants uniformly 64-bit; the runtime and pass reject targets
  // whose pointer width/endianness do not match the current representation.
  ConstantInt* num(std::uint64_t n) { return ConstantInt::get(cast<IntegerType>(I64), n); }
  // Intern module-lifetime strings used by descriptors and diagnostics. Runtime records
  // retain these pointers, so dynamic unloading is outside the supported lifetime model.
  Constant* text(StringRef value) {
    auto found = Strings.find(value.str());
    if (found != Strings.end()) return found->second;
    auto* init = ConstantDataArray::getString(*C, value);
    auto* global = new GlobalVariable(*M, init->getType(), true, GlobalValue::PrivateLinkage, init, "__sc.str");
    global->setUnnamedAddr(GlobalValue::UnnamedAddr::Global);
    Strings[value.str()] = global;
    return global;
  }
  // Declare the runtime entry point using the argument types actually emitted here.
  // These signatures must match native/abi.h; LLVM cannot validate the external C++ ABI for us.
  FunctionCallee api(StringRef name, Type* result, ArrayRef<Type*> args) {
    return M->getOrInsertFunction(name, FunctionType::get(result, args, false));
  }
  // Inserted helpers are noexcept/nounwind by contract. They must not turn a user EH
  // edge into an unmodeled runtime exception or call back through instrumented allocation.
  CallInst* call(IRBuilder<>& b, StringRef name, Type* result, ArrayRef<Value*> args) {
    SmallVector<Type*> types;
    for (auto* a : args) types.push_back(a->getType());
    auto* c = b.CreateCall(api(name, result, types), args);
    c->setDoesNotThrow();
    return c;
  }
  // Convert pointer bits or integer widths to the ABI transport width without changing
  // the original program value. This helper transports bits; it does not grant provenance.
  Value* integer(IRBuilder<>& b, Value* value) {
    if (value->getType()->isPointerTy()) return b.CreatePtrToInt(value, I64);
    return b.CreateZExtOrTrunc(value, I64);
  }
  // Prefer original debug file/line locations, including header and macro expansion
  // locations. Fall back to the translation-unit filename when CodeGen supplies no location.
  SmallVector<Value*> location(Instruction* at) {
    std::string file = M->getSourceFileName();
    unsigned line = 0;
    if (auto loc = at->getDebugLoc()) {
      auto* f = loc->getFile();
      if (f) {
        file = f->getFilename().str();
        if (!file.empty() && file[0] != '/' && !f->getDirectory().empty())
          file = (f->getDirectory() + "/" + file).str();
      }
      line = loc.getLine();
    }
    return {text(file), ConstantInt::get(I32, line)};
  }
  // Peel aliases/qualifiers to discover layout while allowing descriptor() to retain
  // const information separately. Pointer/reference layers remain meaningful type structure.
  DIType* strip(DIType* t) {
    while (auto* d = dyn_cast_or_null<DIDerivedType>(t)) {
      auto tag = d->getTag();
      if (tag != dwarf::DW_TAG_typedef && tag != dwarf::DW_TAG_const_type &&
          tag != dwarf::DW_TAG_volatile_type && tag != dwarf::DW_TAG_restrict_type &&
          tag != dwarf::DW_TAG_atomic_type) break;
      t = d->getBaseType();
    }
    return t;
  }
  // Build the limited shared debug/TBAA type vocabulary. Record identifiers preserve
  // source identity beyond field layout; unsigned integer aliases are normalized deliberately.
  // This representation still loses source distinctions documented by known-limit tests.
  std::string typeName(DIType* original, unsigned depth = 0) {
    if (depth > 32) return "any pointer";
    auto* t = strip(original);
    if (!t) return "void";
    if (auto* d = dyn_cast<DIDerivedType>(t)) {
      if (d->getTag() == dwarf::DW_TAG_pointer_type || d->getTag() == dwarf::DW_TAG_reference_type ||
          d->getTag() == dwarf::DW_TAG_rvalue_reference_type) {
        unsigned levels = 1;
        auto* target = strip(d->getBaseType());
        while (auto* next = dyn_cast_or_null<DIDerivedType>(target)) {
          if (next->getTag() != dwarf::DW_TAG_pointer_type) break;
          ++levels;
          target = strip(next->getBaseType());
        }
        if (!target || isa<DISubroutineType>(target)) return "any pointer";
        return "p" + std::to_string(levels) + " " + typeName(target, depth + 1);
      }
    }
    if (auto* composite = dyn_cast<DICompositeType>(t)) {
      if (!composite->getIdentifier().empty()) return composite->getIdentifier().str();
    }
    auto name = t->getName().str();
    if (name == "unsigned int") name = "int";
    if (name == "unsigned long") name = "long";
    if (name == "unsigned long long") name = "long long";
    if (name == "unsigned short") name = "short";
    return name.empty() ? "aggregate" : name;
  }
  // Emit a module-owned field array and complete an already interned descriptor global.
  // Its five-field layout must remain byte-for-byte compatible with ScType in native/abi.h.
  Constant* finishDescriptor(GlobalVariable* global, StringRef name, std::uint64_t size,
                            std::uint64_t flags, ArrayRef<Constant*> fields) {
    Constant* list = ConstantPointerNull::get(cast<PointerType>(P));
    if (!fields.empty()) {
      auto* array = ArrayType::get(Field, fields.size());
      list = new GlobalVariable(*M, array, true, GlobalValue::PrivateLinkage,
                                ConstantArray::get(array, fields), "__sc.fields");
    }
    global->setInitializer(ConstantStruct::get(Descriptor, {text(name), num(size), num(flags), num(fields.size()), list}));
    return global;
  }
  // Translate available debug layout into recursive runtime descriptors. Unsupported
  // bitfields/virtual layouts and union semantics increase gaps rather than being described
  // as fully checked. A null descriptor intentionally represents unknown/raw storage.
  Constant* descriptor(DIType* original) {
    if (!original) return ConstantPointerNull::get(cast<PointerType>(P));
    auto found = Types.find(original);
    if (found != Types.end()) return found->second;
    auto* global = new GlobalVariable(*M, Descriptor, true, GlobalValue::PrivateLinkage,
                                      Constant::getNullValue(Descriptor), "__sc.type");
    Types[original] = global;
    std::uint64_t flags = 0;
    for (auto* q = original; q && q != strip(original); ) {
      auto* d = dyn_cast<DIDerivedType>(q);
      if (!d) break;
      if (d->getTag() == dwarf::DW_TAG_const_type) flags |= 16;
      q = d->getBaseType();
    }
    auto* t = strip(original);
    std::uint64_t size = t ? (t->getSizeInBits() + 7) / 8 : 0;
    SmallVector<Constant*> fields;
    if (auto* d = dyn_cast_or_null<DIDerivedType>(t)) {
      if (d->getTag() == dwarf::DW_TAG_pointer_type || d->getTag() == dwarf::DW_TAG_reference_type ||
          d->getTag() == dwarf::DW_TAG_rvalue_reference_type) flags |= 2;
    }
    if (auto* record = dyn_cast_or_null<DICompositeType>(t)) {
      if (record->getTag() == dwarf::DW_TAG_array_type) {
        flags |= 64;
        auto* base = record->getBaseType();
        std::uint64_t count = base && base->getSizeInBits() ? record->getSizeInBits() / base->getSizeInBits() : 0;
        if (count) fields.push_back(ConstantStruct::get(Field, {num(0), num(count), descriptor(base)}));
      } else {
        flags |= record->getTag() == dwarf::DW_TAG_union_type ? 8 : 4;
        if (flags & 8) ++Gaps;
        for (auto* e : record->getElements()) {
          auto* member = dyn_cast<DIDerivedType>(e);
          if (!member || (member->getTag() != dwarf::DW_TAG_member && member->getTag() != dwarf::DW_TAG_inheritance) ||
              member->isStaticMember()) continue;
          if (member->isBitField() || member->getFlags() & DINode::FlagVirtual) { ++Gaps; continue; }
          fields.push_back(ConstantStruct::get(Field, {num(member->getOffsetInBits() / 8), num(1), descriptor(member->getBaseType())}));
        }
      }
    }
    const auto name = typeName(original);
    if (name == "char" || name == "unsigned char" || name == "std::byte") flags |= 1;
    return finishDescriptor(global, name, size, flags, fields);
  }
  // Fallback for compiler-generated globals without source debug types. LLVM layout is
  // useful for extents and aggregate pieces, but cannot reconstruct every C++ source identity.
  Constant* irDescriptor(Type* type) {
    auto found = IRTypes.find(type);
    if (found != IRTypes.end()) return found->second;
    auto* global = new GlobalVariable(*M, Descriptor, true, GlobalValue::PrivateLinkage,
                                      Constant::getNullValue(Descriptor), "__sc.irtype");
    IRTypes[type] = global;
    std::uint64_t flags = 0;
    SmallVector<Constant*> fields;
    std::string name = primitive(type);
    if (auto* array = dyn_cast<ArrayType>(type)) {
      flags |= 64;
      fields.push_back(ConstantStruct::get(Field, {num(0), num(array->getNumElements()), irDescriptor(array->getElementType())}));
    } else if (auto* record = dyn_cast<StructType>(type)) {
      flags |= 4;
      if (!record->isOpaque()) {
        const auto* layout = DL->getStructLayout(record);
        for (unsigned i = 0; i < record->getNumElements(); ++i)
          fields.push_back(ConstantStruct::get(Field, {num(layout->getElementOffset(i)), num(1), irDescriptor(record->getElementType(i))}));
      }
    }
    if (type->isPointerTy()) flags |= 2;
    if (type->isIntegerTy(8)) flags |= 1;
    return finishDescriptor(global, name, type->isSized() ? DL->getTypeAllocSize(type).getFixedValue() : 0, flags, fields);
  }
  // Name lowered scalar shapes only when richer metadata is absent. In particular i8
  // cannot distinguish signed char, unsigned char and char, motivating frontend access hooks.
  std::string primitive(Type* type) {
    if (type->isPointerTy()) return "any pointer";
    if (type->isFloatTy()) return "float";
    if (type->isDoubleTy()) return "double";
    if (type->isIntegerTy(1)) return "bool";
    if (type->isIntegerTy(8)) return "char";
    if (type->isIntegerTy(16)) return "short";
    if (type->isIntegerTy(32)) return "int";
    if (type->isIntegerTy(64)) return "long";
    if (type->isIntegerTy(128)) return "__int128";
    return "aggregate";
  }
  // Use TBAA access identity when available. Omnipotent-char and vtable labels need
  // special fallback handling; these labels must not be mistaken for exact source types.
  std::string accessName(Instruction* at, Type* value) {
    if (auto* tag = at->getMetadata(LLVMContext::MD_tbaa)) {
      if (tag->getNumOperands() >= 2)
        if (auto* access = dyn_cast_or_null<MDNode>(tag->getOperand(1)))
          if (access->getNumOperands())
            if (auto* name = dyn_cast_or_null<MDString>(access->getOperand(0))) {
              if (name->getString() == "vtable pointer") return "any pointer";
              if (name->getString() != "omnipotent char") return name->getString().str();
            }
    }
    return primitive(value);
  }
  // Recover only already transported authority. Unknown SSA values get zero, while
  // explicit global/constant boundaries may resolve known storage. Arbitrary missing IDs
  // must not be repaired from addresses, which would revive stale allocations.
  Value* token(Value* value, IRBuilder<>& b) {
    if (auto* alloca = dyn_cast<AllocaInst>(value)) {
      auto it = TokenSlots.find(alloca);
      if (it != TokenSlots.end()) return b.CreateLoad(I64, it->second, "sc.id");
    }
    auto known = Tokens.find(value);
    if (known != Tokens.end()) return known->second;
    auto wide = AggregateIds.find(value);
    if (wide != AggregateIds.end() && !wide->second.empty() && value->getType()->isIntegerTy()) return wide->second.front();
    if (auto* constant = dyn_cast<Constant>(value)) {
      if (constant->isNullValue() || isa<UndefValue>(constant) || isa<PoisonValue>(constant)) return num(0);
      if (auto* expression = dyn_cast<ConstantExpr>(constant)) {
        if (expression->isCast() || expression->getOpcode() == Instruction::GetElementPtr)
          return token(expression->getOperand(0), b);
      }
      if (isa<GlobalValue>(value)) return call(b, "__sc_resolve", I64, {value});
    }
    return num(0);
  }
  // Insert the check before the actual load/store, carrying width, alignment, access
  // name and enclosing-record path. Aggregate/representation access uses a deliberately
  // limited byte mode; this is not a complete effective-type or const implementation.
  void access(Instruction* at, Value* pointer, Type* valueType, bool write, IRBuilder<>& b) {
    if (!valueType->isSized() || DL->getTypeStoreSize(valueType).isScalable()) { ++Gaps; return; }
    std::string name = accessName(at, valueType);
    StringRef baseName;
    std::uint64_t baseOffset = 0;
    if (auto* tag = at->getMetadata(LLVMContext::MD_tbaa)) {
      if (tag->getNumOperands() >= 3) {
        if (auto* base = dyn_cast_or_null<MDNode>(tag->getOperand(0)))
          if (base->getNumOperands())
            if (auto* label = dyn_cast_or_null<MDString>(base->getOperand(0))) baseName = label->getString();
        if (auto* offset = mdconst::dyn_extract<ConstantInt>(tag->getOperand(2))) baseOffset = offset->getZExtValue();
      }
    }
    std::uint64_t flags = (write ? 1 : 0) | (((valueType->isIntegerTy(8) && name == "char") || valueType->isAggregateType()) ? 2 : 0);
    if (auto* load = dyn_cast<LoadInst>(at)) flags |= load->getAlign().value() << 32;
    if (auto* store = dyn_cast<StoreInst>(at)) flags |= store->getAlign().value() << 32;
    auto loc = location(at);
    call(b, "__sc_access_v2", Void, {pointer, token(pointer, b), num(DL->getTypeStoreSize(valueType).getFixedValue()),
                                   text(name), text(baseName), num(baseOffset), num(flags), loc[0], loc[1]});
    ++Accesses;
  }
  // Pointers and pointer-width integers can carry one scalar ID. Other aggregate/wide
  // forms need independent pieces so ABI coercion does not merge distinct embedded pointers.
  bool shadowType(Type* type) { return type->isPointerTy() || type->isIntegerTy(64); }
  // Walk target struct/array layout and eligible wide-integer chunks. Cap large arrays
  // instead of generating unbounded per-element transport; unhandled forms remain reported gaps.
  void collectPieces(Type* type, SmallVector<unsigned, 4> path, std::uint64_t offset,
                     SmallVectorImpl<Piece>& output) {
    if (auto* record = dyn_cast<StructType>(type)) {
      if (record->isOpaque()) return;
      auto* layout = DL->getStructLayout(record);
      for (unsigned i = 0; i < record->getNumElements(); ++i) {
        auto child = path; child.push_back(i);
        collectPieces(record->getElementType(i), child, offset + layout->getElementOffset(i), output);
      }
    } else if (auto* array = dyn_cast<ArrayType>(type)) {
      if (array->getNumElements() > 4096) { ++Gaps; return; }
      auto stride = DL->getTypeAllocSize(array->getElementType()).getFixedValue();
      for (unsigned i = 0; i < array->getNumElements(); ++i) {
        auto child = path; child.push_back(i);
        collectPieces(array->getElementType(), child, offset + i * stride, output);
      }
    } else if (shadowType(type)) output.push_back({path, offset, 0, type});
    else if (type->isIntegerTy() && type->getIntegerBitWidth() > 64)
      for (unsigned bit = 0; bit + 64 <= type->getIntegerBitWidth(); bit += 64)
        output.push_back({path, offset + bit / 8, bit, type});
  }
  // Cache decomposition by LLVM type because every load/store/call of an aggregate
  // must agree on piece ordering and offsets.
  const SmallVector<Piece, 4>& pieces(Type* type) {
    auto found = Pieces.find(type);
    if (found != Pieces.end()) return found->second;
    SmallVector<Piece, 4> result;
    collectPieces(type, {}, 0, result);
    return Pieces.try_emplace(type, std::move(result)).first->second;
  }
  // Extract one lowered component and normalize its bits for shadow matching. The
  // associated ID remains separate so equal addresses never imply equal object generations.
  Value* pieceBits(Value* value, const Piece& piece, IRBuilder<>& b) {
    if (!piece.path.empty()) value = b.CreateExtractValue(value, piece.path);
    if (piece.shift) value = b.CreateLShr(value, piece.shift);
    return integer(b, value);
  }
  // Collect a complete piece vector, retaining zeros for unavailable identities. Constants
  // can inherit explicit known roots, but undefined/poison values cannot create authority.
  SmallVector<Value*, 4> ids(Value* value, IRBuilder<>& b) {
    auto known = AggregateIds.find(value);
    if (known != AggregateIds.end()) return known->second;
    if (shadowType(value->getType())) return {token(value, b)};
    SmallVector<Value*, 4> result;
    for (const auto& piece : pieces(value->getType())) {
      Value* id = num(0);
      if (isa<Constant>(value) && !isa<UndefValue>(value) && !isa<PoisonValue>(value)) {
        Value* part = value;
        if (!piece.path.empty()) part = b.CreateExtractValue(value, piece.path);
        if (!piece.shift) id = token(part, b);
      }
      result.push_back(id);
    }
    return result;
  }
  // Store scalar and aggregate transport in their corresponding maps. Every later use
  // must apply the same decomposition rather than inferring new IDs from the result bits.
  void setIds(Value* value, SmallVector<Value*, 4> result) {
    if (shadowType(value->getType()) && !result.empty()) Tokens[value] = result.front();
    else AggregateIds[value] = std::move(result);
  }
  // Lowered type plus calling convention is the first indirect-call contract. Opaque
  // pointers require the additional frontend source-signature contract where available.
  std::string signature(FunctionType* type, unsigned convention) {
    std::string result = std::to_string(convention) + ":";
    raw_string_ostream out(result);
    type->print(out);
    return result;
  }
  // Recognize the current built-in allocation vocabulary and Itanium new spellings.
  // Recognition is a compiler contract heuristic, not verification of arbitrary allocator bodies.
  bool allocation(StringRef name) {
    return name == "malloc" || name == "calloc" || name == "aligned_alloc" || name == "__cxa_allocate_exception" ||
           name.starts_with("_Znwm") || name.starts_with("_Znam") || name.starts_with("_Znwj") || name.starts_with("_Znaj");
  }
  // Pair C free and recognized scalar/array delete families. Custom allocation protocols
  // or different ABIs need explicit models instead of extending this by name guesswork alone.
  bool deallocation(StringRef name) {
    return name == "free" || name.starts_with("_ZdlPv") || name.starts_with("_ZdaPv");
  }
  // Snapshot original instructions before injecting helpers so checker code is not
  // instrumented recursively. Reset function-local maps while retaining module descriptor
  // and function registries across definitions.
  void function(Function& f) {
    if (f.isDeclaration() || f.getName().starts_with("__sc_")) return;
    ++Functions;
    // Their accesses are instrumented, but arbitrary allocator implementations
    // still provide a trusted returned-storage contract.
    if (allocation(f.getName()) || deallocation(f.getName())) ++Gaps;
    Tokens.clear(); TokenSlots.clear(); Variables.clear(); AggregateSources.clear(); AggregateIds.clear();
    SmallVector<Instruction*> original;
    for (auto& i : instructions(f)) {
      original.push_back(&i);
      for (auto& record : i.getDbgRecordRange())
        if (auto* variable = dyn_cast<DbgVariableRecord>(&record))
          if (variable->isDbgDeclare() && variable->getExpression()->getNumElements() == 0)
            if (auto* address = dyn_cast_or_null<AllocaInst>(variable->getAddress()->stripPointerCasts()))
              Variables[address] = variable->getVariable()->getType();
    }
    // Create companion PHIs before visiting users. Their incoming IDs are connected only
    // after all original definitions have been processed, avoiding traversal-order dependence.
    // Make token PHIs first; their incoming values are wired after all blocks.
    for (auto* i : original)
      if (auto* phi = dyn_cast<PHINode>(i)) {
        SmallVector<Value*, 4> result;
        for (unsigned n = 0; n < pieces(phi->getType()).size(); ++n)
          result.push_back(PHINode::Create(I64, phi->getNumIncomingValues(), "sc.phi", phi->getIterator()));
        setIds(phi, std::move(result));
      }
    // Enter runtime bookkeeping before original instructions and establish incoming IDs.
    // Arguments retain the public ABI; hidden scalar/aggregate transport is recovered separately.
    IRBuilder<> entry(&*f.getEntryBlock().getFirstInsertionPt());
    Frame = call(entry, "__sc_enter", I64, {&f});
    if (f.getName() == "main" && f.arg_size() >= 2) {
      auto* argc = f.getArg(0); auto* argv = f.getArg(1);
      if (argc->getType() == I32 && argv->getType()->isPointerTy()) call(entry, "__sc_main_args", Void, {argc, argv});
    }
    for (auto& argument : f.args()) {
      if (shadowType(argument.getType())) {
        Value* raw = argument.getType()->isPointerTy() ? static_cast<Value*>(&argument) : entry.CreateIntToPtr(&argument, P);
        Tokens[&argument] = call(entry, "__sc_argument", I64, {num(argument.getArgNo()), raw});
      } else {
        SmallVector<Value*, 4> result;
        for (unsigned n = 0; n < pieces(argument.getType()).size(); ++n)
          result.push_back(call(entry, "__sc_argument_slot", I64, {num(argument.getArgNo()), num(n)}));
        setIds(&argument, std::move(result));
      }
    }
    // Preallocate mutable ID slots at entry. Actual registration can still occur later at
    // lifetime.start, so lexical reuse receives the correct current generation.
    for (auto* i : original) if (auto* alloca = dyn_cast<AllocaInst>(i)) {
      auto* slot = entry.CreateAlloca(I64, nullptr, "sc.slot");
      entry.CreateStore(num(0), slot);
      TokenSlots[alloca] = slot;
    }
    // Recognized lifetime markers are authoritative start sites; allocas without such
    // markers register at allocation. Avoid registering both and immediately expiring a live ID.
    SmallVector<AllocaInst*> starts;
    for (auto* i : original) if (auto* intrinsic = dyn_cast<IntrinsicInst>(i))
      if (intrinsic->getIntrinsicID() == Intrinsic::lifetime_start)
        if (auto* a = dyn_cast<AllocaInst>(intrinsic->getArgOperand(1)->stripPointerCasts())) starts.push_back(a);
    for (auto* i : original) {
      if (auto* alloca = dyn_cast<AllocaInst>(i)) {
        if (!llvm::is_contained(starts, alloca)) {
          IRBuilder<> b(alloca->getNextNode());
          registerStack(alloca, b, alloca);
        }
        continue;
      }
      IRBuilder<> before(i);
      // Check before loading user bytes, then recover shadow IDs from the actual loaded
      // bits. An earlier initialization failure must not become an unchecked undefined value.
      if (auto* load = dyn_cast<LoadInst>(i)) {
        access(i, load->getPointerOperand(), load->getType(), false, before);
        IRBuilder<> after(load->getNextNode());
        SmallVector<Value*, 4> result;
        for (const auto& piece : pieces(load->getType())) {
          auto* address = after.CreateGEP(Type::getInt8Ty(*C), load->getPointerOperand(), num(piece.offset));
          result.push_back(call(after, "__sc_shadow_load", I64, {address, pieceBits(load, piece, after)}));
        }
        setIds(load, std::move(result));
        if (load->getType()->isAggregateType()) AggregateSources[load] = load->getPointerOperand();
      // The pre-store check invalidates old shadows. After the actual store, publish the
      // new bits with the IDs transported from its source value.
      } else if (auto* store = dyn_cast<StoreInst>(i)) {
        auto* value = store->getValueOperand();
        auto identifiers = ids(value, before);
        access(i, store->getPointerOperand(), value->getType(), true, before);
        IRBuilder<> after(store->getNextNode());
        unsigned chunk = 0;
        for (const auto& piece : pieces(value->getType())) {
          auto* address = after.CreateGEP(Type::getInt8Ty(*C), store->getPointerOperand(), num(piece.offset));
          call(after, "__sc_shadow_store", Void, {address, pieceBits(value, piece, after), identifiers[chunk++]});
        }
        if (value->getType()->isAggregateType()) {
          if (auto* source = AggregateSources.lookup(value)) {
            IRBuilder<> after(store->getNextNode());
            auto loc = location(store);
            call(after, "__sc_copy", Void, {store->getPointerOperand(), token(store->getPointerOperand(), after),
                 source, token(source, after), num(DL->getTypeStoreSize(value->getType()).getFixedValue()), loc[0], loc[1]});
          }
        }
      // Select only the pieces below the extracted aggregate path; copying one ID to all
      // fields would collapse independent pointer identities.
      } else if (auto* extract = dyn_cast<ExtractValueInst>(i)) {
        auto sourceIds = ids(extract->getAggregateOperand(), before);
        SmallVector<Value*, 4> result;
        const auto& structure = pieces(extract->getAggregateOperand()->getType());
        for (unsigned n = 0; n < structure.size(); ++n)
          if (structure[n].path.size() >= extract->getNumIndices() &&
              std::equal(extract->idx_begin(), extract->idx_end(), structure[n].path.begin())) result.push_back(sourceIds[n]);
        setIds(extract, std::move(result));
        if (auto* source = AggregateSources.lookup(extract->getAggregateOperand())) {
          IRBuilder<> after(extract->getNextNode());
          SmallVector<Value*> indices{ConstantInt::get(I32, 0)};
          for (auto n : extract->getIndices()) indices.push_back(ConstantInt::get(I32, n));
          auto* address = after.CreateGEP(extract->getAggregateOperand()->getType(), source, indices);
          if (extract->getType()->isAggregateType()) AggregateSources[extract] = address;
        }
      // Replace IDs for the inserted subtree while retaining the other original pieces.
      } else if (auto* insert = dyn_cast<InsertValueInst>(i)) {
        auto result = ids(insert->getAggregateOperand(), before);
        auto inserted = ids(insert->getInsertedValueOperand(), before);
        const auto& structure = pieces(insert->getType());
        unsigned next = 0;
        for (unsigned n = 0; n < structure.size(); ++n)
          if (structure[n].path.size() >= insert->getNumIndices() &&
              std::equal(insert->idx_begin(), insert->idx_end(), structure[n].path.begin())) result[n] = inserted[next++];
        setIds(insert, std::move(result));
      // Validate arithmetic before pointer formation. Strip inbounds so an invalid source
      // offset can reach diagnostics rather than immediately becoming poison from that flag.
      } else if (auto* gep = dyn_cast<GetElementPtrInst>(i)) {
        if (!gep->getPointerOperand()->getType()->isPointerTy() || gep->getType()->isVectorTy())
          report_fatal_error("Strict C++ does not support vector GEP instrumentation yet");
        gep->setIsInBounds(false); // Invalid offsets must reach checks, not become poison.
        Value* inherited = token(gep->getPointerOperand(), before);
        Value* offset = num(0);
        auto loc = location(gep);
        for (auto it = gep_type_begin(gep); it != gep_type_end(gep); ++it) {
          if (auto* record = it.getStructTypeOrNull()) {
            const auto field = cast<ConstantInt>(it.getOperand())->getZExtValue();
            offset = call(before, "__sc_offset", I64, {offset, num(1), num(DL->getStructLayout(record)->getElementOffset(field)), loc[0], loc[1]});
          } else {
            auto* index = before.CreateSExtOrTrunc(it.getOperand(), I64);
            offset = call(before, "__sc_offset", I64, {offset, index, num(DL->getTypeAllocSize(it.getIndexedType()).getFixedValue()), loc[0], loc[1]});
          }
        }
        call(before, "__sc_gep", Void, {gep->getPointerOperand(), inherited, offset, loc[0], loc[1]});
        bool member = false;
        for (auto it = gep_type_begin(gep); it != gep_type_end(gep); ++it)
          if (it.getStructTypeOrNull()) member = true;
        auto* resultType = gep->getResultElementType();
        // Array indexing preserves the containing array. Selecting a row or a
        // record member creates narrower authority, including through copies.
        if ((member || resultType->isArrayTy()) && resultType->isSized()) {
          IRBuilder<> after(gep->getNextNode());
          auto loc = location(gep);
          Tokens[gep] = call(after, "__sc_narrow", I64, {gep, inherited,
               num(DL->getTypeAllocSize(resultType).getFixedValue()), loc[0], loc[1]});
        } else Tokens[gep] = inherited;
      // Propagate available IDs through selected bit-preserving/pointer-integer forms.
      // This is intentionally incomplete for arbitrary encodings or unusual ABI coercions.
      } else if (auto* cast = dyn_cast<CastInst>(i)) {
        if (shadowType(cast->getType())) Tokens[cast] = token(cast->getOperand(0), before);
        else if (cast->getType()->isIntegerTy() && cast->getType()->getIntegerBitWidth() > 64) {
          SmallVector<Value*, 4> result(pieces(cast->getType()).size(), num(0));
          if (!result.empty()) result[0] = token(cast->getOperand(0), before);
          setIds(cast, std::move(result));
        }
      // Metadata must select the same branch as the user value, piece by piece.
      } else if (auto* select = dyn_cast<SelectInst>(i)) {
        auto left = ids(select->getTrueValue(), before), right = ids(select->getFalseValue(), before);
        SmallVector<Value*, 4> result;
        for (unsigned n = 0; n < left.size(); ++n) result.push_back(before.CreateSelect(select->getCondition(), left[n], right[n]));
        setIds(select, std::move(result));
      // Support selected pointer-width add/sub encodings by carrying a nonzero operand ID.
      // This is not a general provenance calculus for integer arithmetic.
      } else if (auto* binary = dyn_cast<BinaryOperator>(i)) {
        if (binary->getType()->isIntegerTy(64) && (binary->getOpcode() == Instruction::Add || binary->getOpcode() == Instruction::Sub)) {
          auto* left = token(binary->getOperand(0), before);
          auto* right = token(binary->getOperand(1), before);
          Tokens[binary] = before.CreateSelect(before.CreateICmpEQ(left, num(0)), right, left);
        }
      } else if (auto* intrinsic = dyn_cast<IntrinsicInst>(i)) {
        intrinsicInstruction(intrinsic, before);
      } else if (auto* cb = dyn_cast<CallBase>(i)) {
        callInstruction(cb, before);
      // Publish each return piece before leaving the activation and retiring automatic storage.
      } else if (auto* ret = dyn_cast<ReturnInst>(i)) {
        auto* value = ret->getReturnValue();
        if (value) {
          auto result = ids(value, before);
          for (unsigned n = 0; n < result.size(); ++n) call(before, "__sc_return_slot", Void, {num(n), result[n]});
        }
        call(before, "__sc_leave", Void, {Frame, value ? token(value, before) : num(0)});
      } else if (isa<ResumeInst>(i)) {
        call(before, "__sc_leave", Void, {Frame, num(0)});
      // After entering a landing pad, abandon child call/frame metadata that exception
      // control flow bypassed. The current catch/cleanup frame remains live.
      } else if (isa<LandingPadInst>(i)) {
        IRBuilder<> after(i->getNextNode());
        call(after, "__sc_unwind", Void, {Frame});
      } else if (isa<AtomicRMWInst>(i) || isa<AtomicCmpXchgInst>(i) || isa<IndirectBrInst>(i) || isa<VAArgInst>(i)) {
        ++Gaps;
      }
    }
    // All value definitions now have transport metadata. Wire companion PHIs from the
    // same predecessor edges as the original values to preserve identity across loops/branches.
    for (auto* i : original) if (auto* phi = dyn_cast<PHINode>(i)) {
      IRBuilder<> position(phi->getParent()->getTerminator());
      auto phiIds = ids(phi, position);
      for (unsigned n = 0; n < phi->getNumIncomingValues(); ++n) {
          auto* block = phi->getIncomingBlock(n);
          IRBuilder<> b(block->getTerminator());
          auto incoming = ids(phi->getIncomingValue(n), b);
          for (unsigned k = 0; k < phiIds.size(); ++k) cast<PHINode>(phiIds[k])->addIncoming(incoming[k], block);
      }
    }
    // Extra runtime calls invalidate purity/nosync summaries on definitions.
    f.setMemoryEffects(MemoryEffects::unknown());
    f.removeFnAttr(Attribute::NoSync);
    f.removeFnAttr(Attribute::NoFree);
    f.removeFnAttr(Attribute::Speculatable);
  }
  // Register the current alloca extent using its debug type when available, then update
  // the hidden mutable ID slot. Source type loss here remains a coverage limitation.
  void registerStack(AllocaInst* alloca, IRBuilder<>& b, Instruction* at) {
    auto loc = location(at);
    auto* size = b.CreateMul(integer(b, alloca->getArraySize()), num(DL->getTypeAllocSize(alloca->getAllocatedType()).getFixedValue()));
    auto* type = Variables.lookup(alloca);
    auto* id = call(b, "__sc_register", I64, {alloca, size, descriptor(type), num(0), loc[0], loc[1]});
    b.CreateStore(id, TokenSlots.lookup(alloca));
    ++Allocations;
  }
  // Model storage lifetime, bulk memory and recognized frontend annotations explicitly.
  // Other memory-affecting/stack/coroutine intrinsics must add gaps instead of silently
  // claiming that their effects were checked.
  void intrinsicInstruction(IntrinsicInst* intrinsic, IRBuilder<>& before) {
    auto loc = location(intrinsic);
    const auto id = intrinsic->getIntrinsicID();
    if (id == Intrinsic::lifetime_start) {
      if (auto* alloca = dyn_cast<AllocaInst>(intrinsic->getArgOperand(1)->stripPointerCasts())) {
        IRBuilder<> after(intrinsic->getNextNode());
        registerStack(alloca, after, intrinsic);
      }
    } else if (id == Intrinsic::lifetime_end) {
      auto* pointer = intrinsic->getArgOperand(1);
      call(before, "__sc_end", Void, {pointer, token(pointer, before)});
    } else if (auto* copy = dyn_cast<MemTransferInst>(intrinsic)) {
      call(before, "__sc_copy", Void, {copy->getDest(), token(copy->getDest(), before), copy->getSource(),
            token(copy->getSource(), before), integer(before, copy->getLength()), loc[0], loc[1]});
    } else if (auto* fill = dyn_cast<MemSetInst>(intrinsic)) {
      call(before, "__sc_fill", Void, {fill->getDest(), token(fill->getDest(), before), integer(before, fill->getLength()), loc[0], loc[1]});
    } else if (id == Intrinsic::var_annotation) {
      StringRef annotation;
      if (getConstantStringInfo(intrinsic->getArgOperand(1), annotation) && annotation.starts_with("strictcpp.union.init:"))
        unionInitializer(before, intrinsic->getArgOperand(0), annotation, false, loc);
    } else if (id == Intrinsic::ptr_annotation) {
      StringRef label;
      getConstantStringInfo(intrinsic->getArgOperand(1), label);
      auto* inherited = token(intrinsic->getArgOperand(0), before);
      if (label.consume_front("strictcpp.mutable:")) {
        auto found = NamedTypes.find(label.str());
        if (found != NamedTypes.end()) {
          IRBuilder<> after(intrinsic->getNextNode());
          Tokens[intrinsic] = call(after, "__sc_view", I64, {intrinsic, inherited, descriptor(found->second), loc[0], loc[1]});
        } else { Tokens[intrinsic] = inherited; ++Gaps; }
      } else Tokens[intrinsic] = inherited;
    } else if (id == Intrinsic::stackrestore || id == Intrinsic::stacksave || id == Intrinsic::assume ||
               intrinsic->mayReadOrWriteMemory() || intrinsic->getCalledFunction()->getName().starts_with("llvm.coro.")) ++Gaps;
  }
  // Decode the frontend annotation into the same runtime member-selection contract used
  // by source accesses. Global mode preserves static initialization; local mode starts a
  // fresh member before its generated initializer stores/copies.
  void unionInitializer(IRBuilder<>& b, Value* pointer, StringRef annotation, bool global, ArrayRef<Value*> loc) {
    if (!annotation.consume_front("strictcpp.union.init:")) return;
    auto memberPart = annotation.split(':');
    auto sizePart = memberPart.second.split(':');
    std::uint64_t member = 0, size = 0;
    if (memberPart.first.getAsInteger(10, member) || sizePart.first.getAsInteger(10, size) || sizePart.second.empty()) {
      ++Gaps; return;
    }
    call(b, "__sc_union_select", I64, {pointer, token(pointer, b), num(size), text(sizePart.second),
                                      num(member), num(global ? 2 : 1), loc[0], loc[1]});
  }
  // Handle semantic hook calls before generic native/foreign calls. Generic calls use
  // exact-target hidden transport; supported allocation/memory contracts add their own checks.
  // Declarations without a modeled contract are retained as foreign coverage boundaries.
  void callInstruction(CallBase* cb, IRBuilder<>& before) {
    if (cb->isInlineAsm()) { ++Gaps; return; }
    auto* target = cb->getCalledFunction();
    StringRef name = target ? target->getName() : StringRef();
    auto loc = location(cb);
    if (name == "__sc_front_access") {
      auto* pointer = cb->getArgOperand(0);
      auto* id = token(pointer, before);
      call(before, "__sc_access_v2", Void, {pointer, id, integer(before, cb->getArgOperand(1)),
          cb->getArgOperand(2), text(""), num(0), integer(before, cb->getArgOperand(3)), loc[0], loc[1]});
      Tokens[cb] = id;
      ++Accesses;
      return;
    }
    if (name == "__sc_front_union") {
      auto* pointer = cb->getArgOperand(0);
      auto* id = token(pointer, before);
      IRBuilder<> after(cb->getNextNode());
      Tokens[cb] = call(after, "__sc_union_select", I64, {cb, id, integer(after, cb->getArgOperand(1)),
          cb->getArgOperand(2), integer(after, cb->getArgOperand(3)), integer(after, cb->getArgOperand(4)), loc[0], loc[1]});
      return;
    }
    if (name == "__sc_front_call_target") {
      IRBuilder<> after(cb->getNextNode());
      call(after, "__sc_source_call", Void, {cb, cb->getArgOperand(1), loc[0], loc[1]});
      Tokens[cb] = token(cb->getArgOperand(0), before);
      return;
    }
    if (name == "__sc_front_downcast") {
      auto* id = token(cb->getArgOperand(0), before);
      IRBuilder<> after(cb->getNextNode());
      Tokens[cb] = call(after, "__sc_downcast", I64, {cb, id, integer(after, cb->getArgOperand(1)),
                   cb->getArgOperand(2), integer(after, cb->getArgOperand(3)), loc[0], loc[1]});
      return;
    }
    if (name == "__sc_front_placement") {
      auto* id = token(cb->getArgOperand(0), before);
      StringRef label;
      getConstantStringInfo(cb->getArgOperand(2), label);
      Constant* type = nullptr;
      auto found = NamedTypes.find(label.str());
      if (found != NamedTypes.end()) type = descriptor(found->second);
      else {
        auto* description = new GlobalVariable(*M, Descriptor, true, GlobalValue::PrivateLinkage,
            Constant::getNullValue(Descriptor), "__sc.placement.type");
        auto* size = dyn_cast<ConstantInt>(cb->getArgOperand(1));
        type = finishDescriptor(description, label, size ? size->getZExtValue() : 0, 0, {});
      }
      IRBuilder<> after(cb->getNextNode());
      Tokens[cb] = call(after, "__sc_placement", I64, {cb, id, integer(after, cb->getArgOperand(1)), type, loc[0], loc[1]});
      return;
    }
    // Helpers inserted by this implementation are not user operations to instrument.
    // Strict source admission separately forbids implementation-reserved identifiers.
    if (name.starts_with("__sc_")) return;
    if (target && target->isDeclaration() && !allocation(name) && !deallocation(name) &&
        name != "realloc" && name != "memcpy" && name != "memmove" && name != "memset")
      Foreign.insert(name.str());
    if (!target) {
      call(before, "__sc_indirect", Void, {cb->getCalledOperand(), text(signature(cb->getFunctionType(), cb->getCallingConv())), loc[0], loc[1]});
      ++Gaps; // Source pointee types can be erased by LLVM's opaque pointers.
    }
    // Do not retire C++ allocation identity until replaceable deallocation has returned.
    // The runtime keeps a TLS permission for nested free while that exact callback is active.
    // Standard sized delete can forward through an uninstrumented library to
    // a user replacement. Keep the identity alive until that call returns.
    const bool customDelete = deallocation(name) && name != "free";
    if (deallocation(name))
      call(before, customDelete ? "__sc_custom_delete_begin" : "__sc_release", Void,
          {cb->getArgOperand(0), token(cb->getArgOperand(0), before), num(name == "free" ? 0 : (name.starts_with("_Zda") ? 2 : 1)), loc[0], loc[1]});
    if (name == "realloc")
      call(before, "__sc_realloc_guard", Void, {cb->getArgOperand(0), token(cb->getArgOperand(0), before), integer(before, cb->getArgOperand(1)), loc[0], loc[1]});
    if ((name == "memcpy" || name == "memmove") && cb->arg_size() >= 3)
      call(before, "__sc_copy", Void, {cb->getArgOperand(0), token(cb->getArgOperand(0), before),
           cb->getArgOperand(1), token(cb->getArgOperand(1), before), integer(before, cb->getArgOperand(2)), loc[0], loc[1]});
    if (name == "memset" && cb->arg_size() >= 3)
      call(before, "__sc_fill", Void, {cb->getArgOperand(0), token(cb->getArgOperand(0), before), integer(before, cb->getArgOperand(2)), loc[0], loc[1]});
    call(before, "__sc_call_begin", Void, {cb->getCalledOperand(), num(cb->arg_size())});
    for (unsigned n = 0; n < cb->arg_size(); ++n)
      if (shadowType(cb->getArgOperand(n)->getType()))
        call(before, "__sc_call_arg", Void, {num(n), token(cb->getArgOperand(n), before)});
      else {
        auto fields = ids(cb->getArgOperand(n), before);
        for (unsigned k = 0; k < fields.size(); ++k) call(before, "__sc_call_arg_slot", Void, {num(n), num(k), fields[k]});
      }
    // Post-call metadata belongs on the normal edge of an invoke, not its unwind edge.
    // Splitting that edge gives returned IDs and realloc/delete completion a valid insertion point.
    Instruction* next = cb->getNextNode();
    if (auto* invoke = dyn_cast<InvokeInst>(cb)) {
      auto* edge = SplitEdge(invoke->getParent(), invoke->getNormalDest());
      next = edge->getTerminator();
    }
    if (!next) { ++Gaps; return; }
    IRBuilder<> after(next);
    auto* returned = call(after, "__sc_call_end", I64, {cb->getCalledOperand()});
    if (customDelete)
      call(after, "__sc_custom_delete_end", Void, {cb->getArgOperand(0), token(cb->getArgOperand(0), after)});
    if (cb->getType()->isPointerTy()) Tokens[cb] = returned;
    else if (shadowType(cb->getType())) Tokens[cb] = returned;
    else {
      SmallVector<Value*, 4> result;
      for (unsigned n = 0; n < pieces(cb->getType()).size(); ++n)
        result.push_back(call(after, "__sc_returned_slot", I64, {num(n)}));
      setIds(cb, std::move(result));
    }
    // Register successful allocation results after the real call. Array cookies/raw storage
    // lack a complete element model, so later construction/stores establish selected layouts.
    if (allocation(name)) {
      Value* size = cb->getArgOperand(0);
      if (name == "calloc") size = after.CreateMul(integer(after, size), integer(after, cb->getArgOperand(1)));
      if (name == "aligned_alloc") size = cb->getArgOperand(1);
      auto* dt = dyn_cast_or_null<DIType>(cb->getMetadata("heapallocsite"));
      // Array cookies and raw allocator calls have byte-oriented storage until
      // construction/stores establish element layouts.
      Constant* type = name.starts_with("_Zna") ? descriptor(nullptr) : descriptor(dt);
      const unsigned family = name == "calloc" ? 3 : (name == "malloc" || name == "aligned_alloc" ? 5 : (name.starts_with("_Zna") ? 4 : 1));
      Tokens[cb] = call(after, "__sc_register", I64, {cb, integer(after, size), type, num(family), loc[0], loc[1]});
      ++Allocations;
    } else if (name == "realloc") {
      Tokens[cb] = call(after, "__sc_reallocated", I64, {cb->getArgOperand(0), cb, integer(after, cb->getArgOperand(1)), loc[0], loc[1]});
    } else if (cb->getType()->isPointerTy() && target && target->isDeclaration()) {
      // Foreign pointer-return boundary: resolve only registered storage.
      Tokens[cb] = call(after, "__sc_resolve", I64, {cb});
    }
    cb->setMemoryEffects(MemoryEffects::unknown());
    cb->removeFnAttr(Attribute::NoSync);
    cb->removeFnAttr(Attribute::NoFree);
  }
public:
  // Build module type/function/global registries before transforming definitions. The
  // current runtime assumes 64-bit little-endian pointers and eight-byte shadow slots.
  PreservedAnalyses run(Module& module, ModuleAnalysisManager&) {
    M = &module; C = &M->getContext(); DL = &M->getDataLayout();
    if (!M->getModuleInlineAsm().empty()) ++Gaps;
    if (DL->getPointerSize() != 8 || !DL->isLittleEndian())
      report_fatal_error("Strict C++ native runtime currently requires a 64-bit little-endian target");
    P = PointerType::getUnqual(*C); I64 = Type::getInt64Ty(*C); I32 = Type::getInt32Ty(*C); Void = Type::getVoidTy(*C);
    Descriptor = StructType::create(*C, {P, I64, I64, I64, P}, "sc.type");
    Field = StructType::create(*C, {I64, I64, P}, "sc.field");
    // Gather named types once so frontend annotations can reconnect source-only type names
    // to concrete debug descriptors, including selected const/mutable views.
    DebugInfoFinder finder;
    finder.processModule(*M);
    for (auto* type : finder.types()) {
      const auto name = typeName(type);
      if (type == strip(type)) NamedTypes[name] = type;
      if (auto* derived = dyn_cast<DIDerivedType>(type))
        if (derived->getTag() == dwarf::DW_TAG_const_type) NamedTypes["const " + name] = type;
    }
    // Read semantic annotations emitted by the frontend. They are compiler-internal data;
    // user-provided similarly named attributes are not a supported way to grant authority.
    if (auto* annotations = M->getNamedGlobal("llvm.global.annotations")) {
      if (auto* array = dyn_cast_or_null<ConstantArray>(annotations->getInitializer()))
        for (const auto& operand : array->operands()) {
          auto* item = dyn_cast<ConstantStruct>(operand);
          if (!item || item->getNumOperands() < 2) continue;
          auto* function = dyn_cast<Function>(item->getOperand(0)->stripPointerCasts());
          StringRef annotation;
          if (function && getConstantStringInfo(item->getOperand(1), annotation) && annotation.consume_front("strictcpp.signature:"))
            SourceSignatures[function] = annotation.str();
          auto* global = dyn_cast<GlobalVariable>(item->getOperand(0)->stripPointerCasts());
          if (global && getConstantStringInfo(item->getOperand(1), annotation) && annotation.starts_with("strictcpp.union.init:"))
            UnionInitializers[global] = annotation.str();
        }
    }
    // Freeze original module members before adding descriptor strings, helper declarations
    // and the module constructor. LLVM metadata-only globals are not user storage to register.
    SmallVector<Function*> originals;
    for (auto& f : *M) originals.push_back(&f);
    SmallVector<GlobalVariable*> globals;
    for (auto& g : M->globals())
      if (g.hasInitializer() && !g.getName().starts_with("llvm.") && g.getSection() != "llvm.metadata") globals.push_back(&g);
    // Priority-zero registration normally precedes ordinary user global constructors.
    // Duplicate COMDAT roots are deduplicated by the runtime; arbitrary dynamic loading or
    // all static-initialization interactions are not part of the complete safety contract.
    auto* init = Function::Create(FunctionType::get(Void, false), GlobalValue::InternalLinkage, "__sc_module_init", *M);
    auto* block = BasicBlock::Create(*C, "entry", init);
    IRBuilder<> b(block);
    for (auto* f : originals)
      if (!f->isIntrinsic() && !f->getName().starts_with("__sc_")) {
        call(b, "__sc_function", Void, {f, text(signature(f->getFunctionType(), f->getCallingConv()))});
        auto source = SourceSignatures.find(f);
        if (source != SourceSignatures.end()) call(b, "__sc_function_source", Void, {f, text(source->second)});
      }
    for (auto* g : globals) {
      // Register storage before seeding pointer shadows. Cross-module static initializers
      // use lazy resolution markers because their pointed-to module may register later.
      SmallVector<DIGlobalVariableExpression*> debug;
      g->getDebugInfo(debug);
      auto* type = debug.empty() ? irDescriptor(g->getValueType()) : descriptor(debug.front()->getVariable()->getType());
      auto size = DL->getTypeAllocSize(g->getValueType()).getFixedValue();
      call(b, "__sc_register", I64, {g, num(size), type, num(2), text(M->getSourceFileName()), ConstantInt::get(I32, 0)});
      if (auto found = UnionInitializers.find(g); found != UnionInitializers.end())
        unionInitializer(b, g, found->second, true, {text(M->getSourceFileName()), ConstantInt::get(I32, 0)});
    }
    for (auto* g : globals) {
      for (const auto& piece : pieces(g->getValueType())) {
        Value* part = g->getInitializer();
        if (!piece.path.empty()) part = b.CreateExtractValue(part, piece.path);
        auto* address = b.CreateGEP(Type::getInt8Ty(*C), g, num(piece.offset));
        bool static_pointer = part->getType()->isPointerTy();
        if (auto* expression = dyn_cast<ConstantExpr>(part)) static_pointer |= expression->getOpcode() == Instruction::PtrToInt;
        if (static_pointer && !piece.shift)
          call(b, "__sc_shadow_static", Void, {address, pieceBits(g->getInitializer(), piece, b)});
        else call(b, "__sc_shadow_store", Void, {address, pieceBits(g->getInitializer(), piece, b), num(0)});
      }
    }
    b.CreateRetVoid();
    appendToGlobalCtors(*M, init, 0);
    for (auto* f : originals) function(*f);
    errs() << "strictcpp-native: " << Functions << " functions, " << Accesses << " accesses, "
           << Allocations << " object sites, " << Gaps << " known coverage gaps\n";
    json::Array foreign;
    for (const auto& symbol : Foreign) foreign.push_back(symbol);
    errs() << "strictcpp-native-foreign: " << formatv("{0}", json::Value(std::move(foreign))) << "\n";
    return PreservedAnalyses::none();
  }
  static bool isRequired() { return true; }
};
}
// Expose the new-pass-manager plugin entry point. isRequired above ensures the checker
// still runs for definitions that ordinary optimization would skip, including O0/optnone.
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "StrictCppNative", "0.4.0", [](PassBuilder& pb) {
    pb.registerPipelineParsingCallback([](StringRef name, ModulePassManager& pm, ArrayRef<PassBuilder::PipelineElement>) {
      if (name != "strictcpp") return false;
      pm.addPass(NativePass()); return true;
    });
  }};
}
