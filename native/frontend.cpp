// This plugin augments Clang's semantic AST before CodeGen. It preserves selected
// source facts that opaque LLVM pointers, TBAA or lowered construction would erase.
// No user text or language syntax is rewritten by the native backend.
// Preserve semantic lifetime events that LLVM IR otherwise erases.
#include "clang/AST/ASTConsumer.h"
#include "clang/AST/ASTContext.h"
#include "clang/AST/Attr.h"
#include "clang/AST/ExprCXX.h"
#include "clang/AST/Mangle.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/AST/RecordLayout.h"
#include "clang/Basic/Builtins.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendPluginRegistry.h"
#include "clang/Sema/Sema.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/Support/raw_ostream.h"
#include "strict_profile.h"
using namespace clang;
namespace {
// Visit concrete instantiations as well as written declarations. Seen sets/attributes
// make repeated top-level and translation-unit traversals idempotent; injected hooks
// must not be wrapped a second time.
class LifetimeVisitor : public RecursiveASTVisitor<LifetimeVisitor> {
  CompilerInstance& CI;
  ASTContext& C;
  // Synthetic C-linkage declarations are implicit compiler nodes, not user declarations.
  // Each hook returns unchanged pointer bits and is lowered to checks/ID operations by the pass.
  FunctionDecl* Hook = nullptr;
  FunctionDecl* ConstantEvaluation = nullptr;
  FunctionDecl* CastHook = nullptr;
  FunctionDecl* CallHook = nullptr;
  FunctionDecl* AccessHook = nullptr;
  FunctionDecl* UnionHook = nullptr;
  llvm::SmallPtrSet<CastExpr*, 32> Casts;
  llvm::SmallPtrSet<CallExpr*, 32> Calls;
  llvm::SmallPtrSet<CXXNewExpr*, 32> Seen;
  llvm::SmallPtrSet<Expr*, 32> AccessSites;
  llvm::SmallPtrSet<MemberExpr*, 32> UnionSites;
  llvm::SmallPtrSet<MemberExpr*, 32> UnionAssignments;
  llvm::SmallPtrSet<Expr*, 32> DeferredSites;
  // Hooks counts placement sites; Skipped records conservatively deferred semantic sites.
  // Neither is an exhaustive count of all C++ rules that would be required for soundness.
  unsigned Hooks = 0, Skipped = 0;
  // Construct a noexcept implicit declaration directly in ASTContext storage. This
  // matches the runtime nounwind ABI and preserves noexcept traits of wrapped operations.
  FunctionDecl* declareHook(llvm::StringRef name, llvm::ArrayRef<QualType> args) {
    auto* linkage = LinkageSpecDecl::Create(C, C.getTranslationUnitDecl(), {}, {}, LinkageSpecLanguageIDs::C, false);
    auto info = FunctionProtoType::ExtProtoInfo();
    info.ExceptionSpec.Type = EST_BasicNoexcept;
    auto type = C.getFunctionType(C.VoidPtrTy, args, info);
    auto* declaration = FunctionDecl::Create(C, linkage, {}, {}, &C.Idents.get(name), type,
                                             C.getTrivialTypeSourceInfo(type), SC_Extern);
    declaration->setImplicit(); declaration->setReferenced();
    llvm::SmallVector<ParmVarDecl*> parameters;
    for (auto t : args) parameters.push_back(ParmVarDecl::Create(C, declaration, {}, {}, nullptr, t,
                                    C.getTrivialTypeSourceInfo(t), SC_None, nullptr));
    declaration->setParams(parameters);
    linkage->addDecl(declaration); C.getTranslationUnitDecl()->addDecl(linkage);
    return declaration;
  }
  // Build the literal plus array-to-pointer conversion Clang CodeGen expects, keeping
  // source location information attached to the semantic event.
  Expr* label(llvm::StringRef value, SourceLocation loc) {
    auto array = C.getStringLiteralArrayType(C.CharTy.withConst(), value.size() + 1);
    auto* literal = StringLiteral::Create(C, value, StringLiteralKind::Ordinary, false, array, {loc});
    return ImplicitCastExpr::Create(C, C.getPointerType(C.CharTy.withConst()), CK_ArrayToPointerDecay,
                                    literal, nullptr, VK_PRValue, FPOptionsOverride());
  }
  // Use the target size type for semantic widths/member indexes; the current native
  // runtime separately rejects targets outside its 64-bit little-endian representation.
  Expr* number(std::uint64_t n, SourceLocation loc) {
    return IntegerLiteral::Create(C, llvm::APInt(C.getTypeSize(C.getSizeType()), n), C.getSizeType(), loc);
  }
  // During constant evaluation choose the original typed pointer expression. Cast the
  // void-returning hook only in the runtime branch: a void-pointer round trip in the
  // constant branch would invalidate otherwise legal pre-C++26 constant expressions.
  Expr* runtimeOnly(Expr* original, FunctionDecl* f, llvm::ArrayRef<Expr*> args, SourceLocation loc) {
    auto* invoke = CallExpr::Create(C, functionReference(f, loc), args, C.VoidPtrTy, VK_PRValue, loc, FPOptionsOverride());
    auto* typed = ImplicitCastExpr::Create(C, original->getType(), CK_BitCast, invoke, nullptr, VK_PRValue, FPOptionsOverride());
    if (!ConstantEvaluation) {
      auto& id = C.Idents.get("__builtin_is_constant_evaluated");
      ConstantEvaluation = cast<FunctionDecl>(CI.getSema().LazilyCreateBuiltin(&id,
          Builtin::BI__builtin_is_constant_evaluated, CI.getSema().getCurScope(), false, loc));
    }
    auto* condition = CallExpr::Create(C, functionReference(ConstantEvaluation, loc), {}, C.BoolTy, VK_PRValue, loc, FPOptionsOverride());
    return new (C) ConditionalOperator(condition, loc, original, loc, typed, original->getType(), VK_PRValue, OK_Ordinary);
  }
  // TBAA treats several character accesses alike, losing the distinction needed for
  // strict signed-char rules. Preserve the source lvalue type and read/write intent while
  // evaluating its address once on whichever conditional branch actually executes.
  Expr* signedCharacterAccess(Expr* original, unsigned flags) {
    if (original->getType()->isDependentType() ||
        !C.hasSameUnqualifiedType(original->getType(), C.SignedCharTy)) return original;
    if (original->refersToBitField()) { ++Skipped; return original; }
    auto loc = original->getBeginLoc();
    if (!AccessHook) AccessHook = declareHook("__sc_front_access", {C.VoidPtrTy, C.getSizeType(),
                                          C.getPointerType(C.CharTy.withConst()), C.getSizeType()});
    auto pointerType = C.getPointerType(original->getType());
    auto* address = UnaryOperator::Create(C, original, UO_AddrOf, pointerType, VK_PRValue, OK_Ordinary,
                                         loc, false, FPOptionsOverride());
    auto* raw = ImplicitCastExpr::Create(C, C.VoidPtrTy, CK_BitCast, address, nullptr, VK_PRValue, FPOptionsOverride());
    Expr* args[] = {raw, number(1, loc), label("signed char", loc), number(flags, loc)};
    auto* typed = runtimeOnly(address, AccessHook, args, loc);
    return UnaryOperator::Create(C, typed, UO_Deref, original->getType(), original->getValueKind(), OK_Ordinary,
                                 loc, false, FPOptionsOverride());
  }
  // Use declaration order, starting at one; zero means no active member in runtime state.
  // Only supported scalar member cases are hooked, and union use still reports coverage gaps.
  unsigned memberIndex(const FieldDecl* field) {
    unsigned index = 1;
    for (auto* candidate : field->getParent()->fields()) {
      if (candidate == field) return index;
      ++index;
    }
    return 0;
  }
  // Record/enum names use Clang RTTI mangling to match debug type identity in the pass.
  // Const is retained for selected typed views, while ordinary source spelling is canonicalized.
  std::string nameOf(QualType type) {
    std::string name;
    if (type->isRecordType() || type->isEnumeralType()) {
      auto mangler = std::unique_ptr<MangleContext>(C.createMangleContext());
      llvm::raw_string_ostream stream(name);
      mangler->mangleCXXRTTIName(type.getCanonicalType().getUnqualifiedType(), stream);
    } else name = type.getCanonicalType().getUnqualifiedType().getAsString();
    if (type.isConstQualified()) name = "const " + name;
    return name;
  }
  // Preserve source pointee types erased by opaque LLVM pointers. Removing the exception
  // specification allows a compatible noexcept function-to-pointer conversion to retain
  // the same checked signature.
  std::string sourceSignature(QualType type) {
    if (auto* prototype = type->getAs<FunctionProtoType>()) {
      auto info = prototype->getExtProtoInfo();
      info.ExceptionSpec.Type = EST_None;
      type = C.getFunctionType(prototype->getReturnType(), prototype->getParamTypes(), info);
    }
    auto mangler = std::unique_ptr<MangleContext>(C.createMangleContext());
    std::string signature;
    llvm::raw_string_ostream stream(signature);
    mangler->mangleCXXRTTIName(type.getCanonicalType(), stream);
    return signature;
  }
  // Create the ordinary function-reference decay expected by a synthesized CallExpr,
  // rather than using a user-visible declaration or reparsing injected source text.
  Expr* functionReference(FunctionDecl* f, SourceLocation loc) {
    auto* reference = DeclRefExpr::Create(C, {}, {}, f, false, loc, f->getType(), VK_LValue);
    return ImplicitCastExpr::Create(C, C.getPointerType(f->getType()), CK_FunctionToPointerDecay,
                                   reference, nullptr, VK_PRValue, FPOptionsOverride());
  }
  // Lazily declare the placement-construction marker. Its signature mirrors the runtime
  // identity stub; actual object registration happens only after the LLVM pass interprets it.
  FunctionDecl* hook() {
    if (Hook) return Hook;
    auto* linkage = LinkageSpecDecl::Create(C, C.getTranslationUnitDecl(), {}, {}, LinkageSpecLanguageIDs::C, false);
    QualType args[] = {C.VoidPtrTy, C.getSizeType(), C.getPointerType(C.CharTy.withConst())};
    auto info = FunctionProtoType::ExtProtoInfo();
      info.ExceptionSpec.Type = EST_BasicNoexcept;
      auto type = C.getFunctionType(C.VoidPtrTy, args, info);
    Hook = FunctionDecl::Create(C, linkage, {}, {}, &C.Idents.get("__sc_front_placement"), type,
                                C.getTrivialTypeSourceInfo(type), SC_Extern);
    Hook->setImplicit();
    Hook->setReferenced();
    llvm::SmallVector<ParmVarDecl*> parameters;
    for (auto t : args)
      parameters.push_back(ParmVarDecl::Create(C, Hook, {}, {}, nullptr, t,
                                              C.getTrivialTypeSourceInfo(t), SC_None, nullptr));
    Hook->setParams(parameters);
    linkage->addDecl(Hook);
    C.getTranslationUnitDecl()->addDecl(linkage);
    return Hook;
  }
public:
  LifetimeVisitor(CompilerInstance& ci) : CI(ci), C(ci.getASTContext()) {}
  bool shouldVisitTemplateInstantiations() const { return true; }
  // An lvalue-to-rvalue conversion is an actual value read. Stored pointer types are
  // conservatively deferred, while signed-character reads receive a precise source hook.
  bool VisitImplicitCastExpr(ImplicitCastExpr* expression) {
    if (expression->getCastKind() == CK_LValueToRValue && AccessSites.insert(expression).second) {
      // TBAA and debug types do not retain every source-level pointer-object
      // distinction (notably signedness and character pointees).
      if (expression->getType()->isPointerType()) ++Skipped;
      expression->setSubExpr(signedCharacterAccess(expression->getSubExpr(), 0));
    }
    return true;
  }
  // Record direct union assignments before recursively visiting their member expressions.
  // Simple assignment can activate supported members; compound assignment must read an
  // already live value and therefore cannot use that activation permission.
  bool VisitBinaryOperator(BinaryOperator* expression) {
    if (expression->getOpcode() == BO_Assign)
      if (auto* member = dyn_cast<MemberExpr>(expression->getLHS()->IgnoreParens())) UnionAssignments.insert(member);
    if (expression->isAssignmentOp() && AccessSites.insert(expression).second)
      expression->setLHS(signedCharacterAccess(expression->getLHS(), expression->isCompoundAssignmentOp() ? 5 : 1));
    return true;
  }
  // Increment/decrement reads and writes the old signed-character value, so the hook
  // requests initialization checking before recording the write.
  bool VisitUnaryOperator(UnaryOperator* expression) {
    if (expression->isIncrementDecrementOp() && AccessSites.insert(expression).second)
      expression->setSubExpr(signedCharacterAccess(expression->getSubExpr(), 5));
    return true;
  }
  // Attach supported scalar union initializer identity before CodeGen turns it into
  // ordinary stores or a memcpy from compiler constant data. Other constructor/aggregate
  // forms are not inferred from a first read of the resulting bytes.
  bool VisitVarDecl(VarDecl* variable) {
    auto* record = variable->getType()->getAs<RecordType>();
    if (!record || !record->getDecl()->isUnion() || variable->getType()->isDependentType() ||
        !variable->isThisDeclarationADefinition()) return true;
    for (auto* attr : variable->specific_attrs<AnnotateAttr>())
      if (attr->getAnnotation().starts_with("strictcpp.union.init:")) return true;
    auto* initializer = variable->getInit();
    if (auto* cleanup = dyn_cast_or_null<ExprWithCleanups>(initializer)) initializer = cleanup->getSubExpr();
    auto* list = dyn_cast_or_null<InitListExpr>(initializer ? initializer->IgnoreParenImpCasts() : nullptr);
    if (list && list->isSyntacticForm()) list = list->getSemanticForm();
    auto* member = list ? list->getInitializedFieldInUnion() : nullptr;
    if (!member && variable->hasGlobalStorage() && !initializer && !record->getDecl()->field_empty())
      member = *record->getDecl()->field_begin();
    if (!member || !member->getType()->isScalarType() || member->isBitField()) return true;
    auto annotation = "strictcpp.union.init:" + std::to_string(memberIndex(member)) + ":" +
        std::to_string(C.getTypeSizeInChars(variable->getType()).getQuantity()) + ":" + nameOf(variable->getType().getUnqualifiedType());
    variable->addAttr(AnnotateAttr::CreateImplicit(C, annotation, nullptr, 0));
    return true;
  }
  // Selecting a union member produces a capability carrying member/generation identity.
  // Wrap only the base address, preserving the selected member and single evaluation.
  // Record/array members, bitfields and unsupported value categories remain deferred.
  bool VisitMemberExpr(MemberExpr* expression) {
    auto* member = dyn_cast<FieldDecl>(expression->getMemberDecl());
    if (!member || !member->getParent()->isUnion() || !UnionSites.insert(expression).second) return true;
    if (member->getType()->isDependentType()) return true;
    if (!member->getType()->isScalarType() || member->isBitField()) { ++Skipped; return true; }
    auto* base = expression->getBase();
    if (!expression->isArrow() && !base->isLValue()) { ++Skipped; return true; }
    const bool activate = UnionAssignments.count(expression);
    auto unionType = expression->isArrow() ? base->getType()->getPointeeType() : base->getType();
    auto pointerType = C.getPointerType(unionType);
    auto loc = expression->getBeginLoc();
    Expr* address = base;
    if (!expression->isArrow()) address = UnaryOperator::Create(C, base, UO_AddrOf, pointerType,
                           VK_PRValue, OK_Ordinary, loc, false, FPOptionsOverride());
    auto* raw = ImplicitCastExpr::Create(C, C.VoidPtrTy, CK_BitCast, address, nullptr, VK_PRValue, FPOptionsOverride());
    if (!UnionHook) UnionHook = declareHook("__sc_front_union", {C.VoidPtrTy, C.getSizeType(),
        C.getPointerType(C.CharTy.withConst()), C.getSizeType(), C.getSizeType()});
    Expr* args[] = {raw, number(C.getTypeSizeInChars(unionType).getQuantity(), loc),
        label(nameOf(unionType.getUnqualifiedType()), loc), number(memberIndex(member), loc), number(activate, loc)};
    auto* typed = runtimeOnly(address, UnionHook, args, loc);
    Expr* selectedBase = typed;
    if (!expression->isArrow()) selectedBase = UnaryOperator::Create(C, typed, UO_Deref, unionType,
        VK_LValue, OK_Ordinary, loc, false, FPOptionsOverride());
    expression->setBase(selectedBase);
    return true;
  }
  // Annotate emitted free/static definitions with their source function type. Non-static
  // methods require a broader object/member-function ABI model and are not claimed here.
  bool VisitFunctionDecl(FunctionDecl* function) {
    if (!function->doesThisDeclarationHaveABody() || function->getType()->isDependentType()) return true;
    if (auto* method = dyn_cast<CXXMethodDecl>(function); method && !method->isStatic()) return true;
    for (auto* annotation : function->specific_attrs<AnnotateAttr>())
      if (annotation->getAnnotation().starts_with("strictcpp.signature:")) return true;
    function->addAttr(AnnotateAttr::CreateImplicit(C, "strictcpp.signature:" + sourceSignature(function->getType()), nullptr, 0));
    return true;
  }
  // Defer explicit destruction semantics, then preserve source signatures for selected
  // indirect free-function calls. Member calls and direct calls follow separate IR handling.
  bool VisitCallExpr(CallExpr* expression) {
    if ((isa_and_nonnull<CXXDestructorDecl>(expression->getDirectCallee()) ||
         isa<CXXPseudoDestructorExpr>(expression->getCallee()->IgnoreParenImpCasts())) && DeferredSites.insert(expression).second)
      ++Skipped;
    if (expression->getDirectCallee() || isa<CXXMemberCallExpr>(expression) || !Calls.insert(expression).second) return true;
    auto* original = expression->getCallee();
    auto type = original->getType();
    if (!type->isPointerType() || !type->getPointeeType()->isFunctionType() || type->isDependentType()) return true;
    if (!CallHook) {
      auto* linkage = LinkageSpecDecl::Create(C, C.getTranslationUnitDecl(), {}, {}, LinkageSpecLanguageIDs::C, false);
      QualType args[] = {C.VoidPtrTy, C.getPointerType(C.CharTy.withConst())};
      auto info = FunctionProtoType::ExtProtoInfo();
      info.ExceptionSpec.Type = EST_BasicNoexcept;
      auto type = C.getFunctionType(C.VoidPtrTy, args, info);
      CallHook = FunctionDecl::Create(C, linkage, {}, {}, &C.Idents.get("__sc_front_call_target"), type,
                                      C.getTrivialTypeSourceInfo(type), SC_Extern);
      CallHook->setImplicit(); CallHook->setReferenced();
      llvm::SmallVector<ParmVarDecl*> parameters;
      for (auto t : args) parameters.push_back(ParmVarDecl::Create(C, CallHook, {}, {}, nullptr, t,
                                       C.getTrivialTypeSourceInfo(t), SC_None, nullptr));
      CallHook->setParams(parameters); linkage->addDecl(CallHook); C.getTranslationUnitDecl()->addDecl(linkage);
    }
    auto loc = expression->getBeginLoc();
    auto signature = sourceSignature(type->getPointeeType());
    auto array = C.getStringLiteralArrayType(C.CharTy.withConst(), signature.size() + 1);
    auto* literal = StringLiteral::Create(C, signature, StringLiteralKind::Ordinary, false, array, {loc});
    auto* label = ImplicitCastExpr::Create(C, C.getPointerType(C.CharTy.withConst()), CK_ArrayToPointerDecay,
                                         literal, nullptr, VK_PRValue, FPOptionsOverride());
    auto* raw = ImplicitCastExpr::Create(C, C.VoidPtrTy, CK_BitCast, original, nullptr, VK_PRValue, FPOptionsOverride());
    Expr* args[] = {raw, label};
    expression->setCallee(runtimeOnly(original, CallHook, args, loc));
    return true;
  }
  // Construction phases are not completely modeled, even when their ordinary IR memory
  // operations are instrumented. Keep this visible to coverage-rejection policy.
  bool VisitCXXConstructExpr(CXXConstructExpr* expression) {
    if (DeferredSites.insert(expression).second) ++Skipped;
    return true;
  }
  // Dynamic-cast object-model/runtime interactions remain outside complete coverage.
  bool VisitCXXDynamicCastExpr(CXXDynamicCastExpr* expression) {
    if (DeferredSites.insert(expression).second) ++Skipped;
    return true;
  }
  // For a base-to-derived pointer cast, preserve the target layout and cumulative base
  // offset before IR erases the source relationship. The hook returns original base bits;
  // the original cast still performs its normal pointer adjustment.
  bool VisitCXXStaticCastExpr(CXXStaticCastExpr* expression) {
    if (expression->getCastKind() != CK_BaseToDerived || !Casts.insert(expression).second) return true;
    const auto target = expression->getType();
    if (!target->isPointerType() || target->isDependentType()) { ++Skipped; return true; }
    auto pointee = target->getPointeeType();
    auto* record = pointee->getAsCXXRecordDecl();
    if (!record || !record->hasDefinition()) { ++Skipped; return true; }
    std::uint64_t offset = 0;
    auto* current = record;
    for (auto* base : expression->path()) {
      auto* next = base->getType()->getAsCXXRecordDecl();
      offset += C.getASTRecordLayout(current).getBaseClassOffset(next).getQuantity();
      current = next;
    }
    if (!CastHook) {
      auto* linkage = LinkageSpecDecl::Create(C, C.getTranslationUnitDecl(), {}, {}, LinkageSpecLanguageIDs::C, false);
      QualType args[] = {C.VoidPtrTy, C.getSizeType(), C.getPointerType(C.CharTy.withConst()), C.getSizeType()};
      auto info = FunctionProtoType::ExtProtoInfo();
      info.ExceptionSpec.Type = EST_BasicNoexcept;
      auto type = C.getFunctionType(C.VoidPtrTy, args, info);
      CastHook = FunctionDecl::Create(C, linkage, {}, {}, &C.Idents.get("__sc_front_downcast"), type,
                                      C.getTrivialTypeSourceInfo(type), SC_Extern);
      CastHook->setImplicit(); CastHook->setReferenced();
      llvm::SmallVector<ParmVarDecl*> parameters;
      for (auto t : args) parameters.push_back(ParmVarDecl::Create(C, CastHook, {}, {}, nullptr, t,
                                 C.getTrivialTypeSourceInfo(t), SC_None, nullptr));
      CastHook->setParams(parameters); linkage->addDecl(CastHook); C.getTranslationUnitDecl()->addDecl(linkage);
    }
    auto loc = expression->getBeginLoc();
    auto name = nameOf(pointee.getUnqualifiedType());
    auto array = C.getStringLiteralArrayType(C.CharTy.withConst(), name.size() + 1);
    auto* literal = StringLiteral::Create(C, name, StringLiteralKind::Ordinary, false, array, {loc});
    auto* label = ImplicitCastExpr::Create(C, C.getPointerType(C.CharTy.withConst()), CK_ArrayToPointerDecay,
                                          literal, nullptr, VK_PRValue, FPOptionsOverride());
    auto* original = expression->getSubExpr();
    auto* raw = ImplicitCastExpr::Create(C, C.VoidPtrTy, CK_BitCast, original, nullptr, VK_PRValue, FPOptionsOverride());
    auto* size = IntegerLiteral::Create(C, llvm::APInt(C.getTypeSize(C.getSizeType()), C.getTypeSizeInChars(pointee).getQuantity()), C.getSizeType(), loc);
    auto* adjust = IntegerLiteral::Create(C, llvm::APInt(C.getTypeSize(C.getSizeType()), offset), C.getSizeType(), loc);
    Expr* args[] = {raw, size, label, adjust};
    expression->setSubExpr(runtimeOnly(original, CastHook, args, loc));
    return true;
  }
  // An annotation on a mutable field becomes llvm.ptr.annotation on member addresses,
  // allowing a selected writable view within an otherwise const record.
  bool VisitFieldDecl(FieldDecl* field) {
    if (!field->isMutable() || field->getType()->isDependentType()) return true;
    for (auto* annotation : field->specific_attrs<AnnotateAttr>())
      if (annotation->getAnnotation().starts_with("strictcpp.mutable:")) return true;
    field->addAttr(AnnotateAttr::CreateImplicit(C, "strictcpp.mutable:" + nameOf(field->getType()), nullptr, 0));
    return true;
  }
  // Recognize only the reserved global placement operator, not arbitrary overloads with
  // placement arguments. Concrete scalar/record construction is hooked; dependent/array
  // forms are deferred rather than pretending to know their lifetime effects.
  bool VisitCXXNewExpr(CXXNewExpr* expression) {
    if (!expression->getNumPlacementArgs() || !expression->getOperatorNew() ||
        !expression->getOperatorNew()->isReservedGlobalPlacementOperator()) return true;
    if (!Seen.insert(expression).second) return true;
    auto type = expression->getAllocatedType();
    if (type->isDependentType() || expression->isArray()) { ++Skipped; return true; }
    Expr* argument = expression->getPlacementArg(0);
    auto loc = expression->getBeginLoc();
    std::string name = nameOf(type);
    auto array = C.getStringLiteralArrayType(C.CharTy.withConst(), name.size() + 1);
    auto* literal = StringLiteral::Create(C, name, StringLiteralKind::Ordinary, false, array, {loc});
    auto* label = ImplicitCastExpr::Create(C, C.getPointerType(C.CharTy.withConst()), CK_ArrayToPointerDecay,
                                         literal, nullptr, VK_PRValue, FPOptionsOverride());
    auto* size = IntegerLiteral::Create(C, llvm::APInt(C.getTypeSize(C.getSizeType()), C.getTypeSizeInChars(type).getQuantity()), C.getSizeType(), loc);
    Expr* args[] = {argument, size, label};
    auto* invoke = CallExpr::Create(C, functionReference(hook(), loc), args, C.VoidPtrTy,
                                   VK_PRValue, loc, FPOptionsOverride());
    if (!ConstantEvaluation) {
      auto& id = C.Idents.get("__builtin_is_constant_evaluated");
      ConstantEvaluation = cast<FunctionDecl>(CI.getSema().LazilyCreateBuiltin(
          &id, Builtin::BI__builtin_is_constant_evaluated, CI.getSema().getCurScope(), false, loc));
    }
    auto* condition = CallExpr::Create(C, functionReference(ConstantEvaluation, loc), {}, C.BoolTy,
                                      VK_PRValue, loc, FPOptionsOverride());
    expression->getPlacementArgs()[0] = new (C) ConditionalOperator(condition, loc, argument,
        loc, invoke, C.VoidPtrTy, VK_PRValue, OK_Ordinary);
    ++Hooks;
    return true;
  }
  // This exact summary is a driver protocol, not only a progress message. Keep its
  // format synchronized with strictcpp/native_driver.py; missing/ambiguous reports reject builds.
  void report() { llvm::errs() << "strictcpp-front: " << Hooks << " placement hooks, " << Skipped << " semantic sites deferred\n"; }
};
// The strict gate must inspect original source nodes before the mutating visitor runs.
// Top-level callbacks expose annotations early enough for CodeGen; the final traversal
// also finds concrete nodes Clang completed later.
class Consumer : public ASTConsumer {
  LifetimeVisitor Visitor;
  std::unique_ptr<strictcpp::StrictSourceValidator> Gate;
public:
  Consumer(CompilerInstance& ci, bool strict) : Visitor(ci) {
    if (strict) {
      Gate = std::make_unique<strictcpp::StrictSourceValidator>(ci);
      ci.getPreprocessor().addPPCallbacks(std::make_unique<strictcpp::StrictPreprocessorValidator>(ci));
    }
  }
  // A failed admission gate never authorizes instrumentation/linking of that declaration.
  // Clang retains the source diagnostic and returns a compilation failure.
  bool HandleTopLevelDecl(DeclGroupRef group) override {
    for (auto* declaration : group) {
      if (Gate && !Gate->TraverseDecl(declaration)) return true;
      Visitor.TraverseDecl(declaration);
    }
    return true;
  }
  // The final strict acknowledgment is emitted only after source traversal and call-graph
  // checks succeed. The driver requires it in addition to ordinary frontend/IR summaries.
  void HandleTranslationUnit(ASTContext& c) override {
    if (Gate) {
      if (!Gate->TraverseDecl(c.getTranslationUnitDecl()) || !Gate->finish()) return;
      llvm::errs() << "strictcpp-strict: strict-v1 accepted\n";
    }
    Visitor.TraverseDecl(c.getTranslationUnitDecl());
    Visitor.report();
  }
};
// Register before the main CodeGen action so semantic annotations affect emitted IR.
// Only the named strict profile is a supported private plugin argument.
class Action : public PluginASTAction {
  bool Strict = false;
  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance& ci, llvm::StringRef) override {
    return std::make_unique<Consumer>(ci, Strict);
  }
  // Unknown internal modes fail plugin setup instead of silently reverting to compatibility.
  bool ParseArgs(const CompilerInstance&, const std::vector<std::string>& args) override {
    for (const auto& arg : args) {
      if (arg == "strict-v1") Strict = true;
      else return false;
    }
    return true;
  }
  ActionType getActionType() override { return AddBeforeMainAction; }
};
// Clang discovers this registration when the matching driver loads the frontend plugin.
FrontendPluginRegistry::Add<Action> Registration("strictcpp-lifetimes", "preserve object lifetime transitions for Strict C++");
}
