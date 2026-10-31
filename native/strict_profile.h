// Admission policy for strict-v1. This checks original source before instrumentation;
// it is intentionally much smaller than C++ and rejects valid but unmodeled programs.
// The allowlists are not a proof that the compiler/runtime implementation is sound.
#pragma once
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/AST/ExprCXX.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include <cstdint>
#include <string>

namespace strictcpp {
using namespace clang;

// Source directives can affect linkage, attributes or generated assembly without
// appearing as ordinary expression nodes. Reject them at preprocessing time rather
// than relying solely on the later AST/IR gap counters.
class StrictPreprocessorValidator final : public PPCallbacks {
  CompilerInstance& CI;
  // Only the physical compiler predefines FileID is trusted. Presumed filenames can be
  // forged by #line; user macro invocations must be checked at their expansion location.
  // The driver also rejects newline macro arguments that could inject predefines directives.
  void reject(SourceLocation loc, llvm::StringRef reason) {
    // Clang's own predefines buffer contains target macro push/pop pragmas.
    // Physical expansion locations keep #line and user _Pragma expansions from
    // being mistaken for that trusted compiler-generated buffer.
    auto& sources = CI.getSourceManager();
    if (sources.getFileID(sources.getExpansionLoc(loc)) == CI.getPreprocessor().getPredefinesFileID()) return;
    auto id = CI.getDiagnostics().getCustomDiagID(DiagnosticsEngine::Error,
        "strictcpp: strict profile rejects %0");
    CI.getDiagnostics().Report(loc, id) << reason;
  }
public:
  explicit StrictPreprocessorValidator(CompilerInstance& ci) : CI(ci) {}
  void PragmaDirective(SourceLocation loc, PragmaIntroducerKind) override { reject(loc, "preprocessor pragmas"); }
  void Ident(SourceLocation loc, llvm::StringRef) override { reject(loc, "identification directives"); }
};

// This gate runs on the original AST, before semantic instrumentation. Every
// admitted node/type/operator is enumerated; new constructs are rejected.
// Every source declaration and statement/expression kind is enumerated. Unknown/new
// AST constructs reach a rejection path rather than being accepted because no known-gap
// counter happened to notice them.
class StrictSourceValidator : public RecursiveASTVisitor<StrictSourceValidator> {
  CompilerInstance& CI;
  ASTContext& C;
  // A source declaration is validated before mutation and then remembered. Final TU
  // traversal must not reinterpret the compiler's subsequently injected hook AST as user code.
  llvm::SmallPtrSet<Decl*, 32> Checked;
  // RAV pre/post callbacks maintain evaluation nesting for standalone mutation checks.
  // This avoids depending on a cached parent map while parsing/mutating top-level declarations.
  llvm::SmallVector<Stmt*, 32> Parents;
  llvm::DenseMap<FunctionDecl*, llvm::SmallVector<FunctionDecl*, 4>> Edges;
  llvm::DenseMap<FunctionDecl*, unsigned> Colors;
  // Canonical declarations make prototype/definition/call edges refer to one function.
  // LocalBytes resets at function boundaries; Nodes bounds source traversal for the whole TU.
  FunctionDecl* Current = nullptr;
  std::uint64_t LocalBytes = 0;
  unsigned Nodes = 0;
  bool Failed = false;

  bool reject(SourceLocation loc, llvm::StringRef reason) {
    // Emit the first policy error and propagate failure through RAV. Continuing
    // with a partial source acknowledgment would violate the admission boundary.
    if (!Failed) {
      auto id = CI.getDiagnostics().getCustomDiagID(DiagnosticsEngine::Error,
          "strictcpp: strict profile rejects %0");
      CI.getDiagnostics().Report(loc, id) << reason;
    }
    Failed = true;
    return false;
  }
  // Admit a short builtin vocabulary with known access/ABI behavior. Character types,
  // volatile/atomic objects, floating point and source-defined records/enums stay rejected.
  // Void is permitted only in contexts that explicitly request it.
  bool scalar(QualType type, bool allowVoid = false) const {
    if (type.isNull() || type.isVolatileQualified() || type->isAtomicType()) return false;
    auto* builtin = dyn_cast<BuiltinType>(type.getCanonicalType().getTypePtr());
    if (!builtin) return false;
    switch (builtin->getKind()) {
    case BuiltinType::Void: return allowVoid;
    case BuiltinType::Bool: case BuiltinType::Int: case BuiltinType::UInt:
    case BuiltinType::Long: case BuiltinType::ULong:
    case BuiltinType::LongLong: case BuiltinType::ULongLong: return true;
    default: return false;
    }
  }
  // Recursively admit positive constant arrays of admitted scalar elements, bounded by
  // size. Pointer/reference storage and variable-length arrays never become fallback cases.
  bool object(QualType type) const {
    if (type.isVolatileQualified()) return false;
    if (auto* array = C.getAsConstantArrayType(type))
      return !array->getSize().isZero() && array->getSize().getActiveBits() <= 16 &&
             C.getTypeSizeInChars(type).getQuantity() <= 1024 && object(array->getElementType());
    return scalar(type);
  }
  // Temporary object pointers support address formation/indexing without permitting
  // stored pointer objects. Function expressions are needed for direct callees, whose
  // interfaces and call form are checked separately.
  bool expressionType(QualType type) const {
    if (scalar(type, true) || object(type) || type->isNullPtrType()) return true;
    if (type->isFunctionType()) return true; // Direct callees are checked separately.
    if (type->isPointerType()) {
      auto pointee = type->getPointeeType();
      return object(pointee) || pointee->isFunctionType();
    }
    return false;
  }
  // Accept arithmetic proof only when Clang evaluates an integer without undefined
  // behavior or side effects. A folded value obtained by ignoring UB is not an admission proof.
  bool constant(const Expr* expression, llvm::APSInt& value) const {
    Expr::EvalResult result;
    if (!expression->EvaluateAsInt(result, C, Expr::SE_NoSideEffects) ||
        result.HasUndefinedBehavior || result.HasSideEffects) return false;
    value = result.Val.getInt();
    return true;
  }
  // Mutating subexpressions can introduce sequencing hazards. Admit mutations only as
  // standalone statements or the supported for init/increment/body positions; transparent
  // parentheses do not themselves change that evaluation context.
  bool standalone(Expr* expression) const {
    if (Parents.size() < 2 || Parents.back() != expression) return false;
    const Stmt* child = expression;
    for (std::size_t n = Parents.size() - 1; n; --n) {
      auto* parent = Parents[n - 1];
      if (isa<ParenExpr>(parent)) { child = parent; continue; }
      if (isa<CompoundStmt>(parent)) return true;
      if (auto* loop = dyn_cast<ForStmt>(parent))
        return loop->getInc() == child || loop->getInit() == child || loop->getBody() == child;
      if (auto* branch = dyn_cast<IfStmt>(parent)) return branch->getThen() == child || branch->getElse() == child;
      if (auto* loop = dyn_cast<WhileStmt>(parent)) return loop->getBody() == child;
      if (auto* loop = dyn_cast<DoStmt>(parent)) return loop->getBody() == child;
      return false;
    }
    return false;
  }
  // Three-color DFS rejects direct and mutual recursion. Copy the adjacency list before
  // recursing so DenseMap growth cannot invalidate the parent iteration.
  bool acyclic(FunctionDecl* function) {
    if (Colors[function] == 1) return reject(function->getLocation(), "recursive call graphs");
    if (Colors[function] == 2) return true;
    Colors[function] = 1;
    const auto callees = Edges.lookup(function);
    for (auto* callee : callees) if (!acyclic(callee)) return false;
    Colors[function] = 2;
    return true;
  }
public:
  explicit StrictSourceValidator(CompilerInstance& ci) : CI(ci), C(ci.getASTContext()) {}
  bool failed() const { return Failed; }
  // Skip trusted implicit compiler declarations and already checked source declarations.
  // Preserve the accumulated local-object budget across child VarDecl visits; restoring it
  // there would accidentally turn the per-function budget into a per-variable budget.
  bool TraverseDecl(Decl* declaration) {
    if (Failed || CI.getDiagnostics().hasErrorOccurred()) return false;
    if (!declaration || declaration->isImplicit() || !Checked.insert(declaration).second) return !Failed;
    auto* oldFunction = Current;
    auto oldBytes = LocalBytes;
    auto* function = dyn_cast<FunctionDecl>(declaration);
    if (function) {
      Current = function->getCanonicalDecl();
      LocalBytes = 0;
    }
    bool result = RecursiveASTVisitor::TraverseDecl(declaration);
    Current = oldFunction;
    if (function) LocalBytes = oldBytes;
    return result;
  }
  // Pair these callbacks on the explicit RAV traversal stack; Visit* methods can then
  // inspect the actual enclosing statement when validating mutation placement.
  bool dataTraverseStmtPre(Stmt* statement) { Parents.push_back(statement); return true; }
  bool dataTraverseStmtPost(Stmt*) { Parents.pop_back(); return true; }
  // Declaration kind and attributes are independently gated. Reserved helper identifiers
  // cannot be used by source to impersonate compiler-created runtime markers.
  bool VisitDecl(Decl* declaration) {
    if (auto* named = dyn_cast<NamedDecl>(declaration))
      if (named->getNameAsString().find("__") != std::string::npos)
        return reject(named->getLocation(), "implementation-reserved identifiers");
    if (declaration->hasAttrs()) return reject(declaration->getLocation(), "declaration attributes");
    switch (declaration->getKind()) {
    case Decl::TranslationUnit: case Decl::Function: case Decl::Var: case Decl::ParmVar:
    case Decl::Typedef: case Decl::TypeAlias: case Decl::Empty: return true;
    default: return reject(declaration->getLocation(), declaration->getDeclKindName());
    }
  }
  // Aliases do not provide a route around the object-type allowlist; inspect their
  // underlying canonical object type rather than trusting a familiar-looking alias name.
  bool VisitTypedefNameDecl(TypedefNameDecl* declaration) {
    return object(declaration->getUnderlyingType()) || reject(declaration->getLocation(), "this type alias");
  }
  // Only direct scalar interfaces are admitted. No varargs, overloaded operators,
  // builtins or foreign C entry points are inferred safe from a matching lowered signature.
  bool VisitFunctionDecl(FunctionDecl* function) {
    if (function->isVariadic() || function->isOverloadedOperator() || function->getBuiltinID() ||
        (function->isExternC() && !function->isMain()))
      return reject(function->getLocation(), "this function interface");
    if (!scalar(function->getReturnType(), true) || function->getNumParams() > 8)
      return reject(function->getLocation(), "this function signature");
    if (function->isMain() && function->getNumParams()) return reject(function->getLocation(), "main arguments");
    return true;
  }
  // Reject unsupported storage/types and externally visible data that could interpose
  // runtime symbols. Static initialization must be compile-time constant; automatic
  // objects plus a tracking allowance contribute to the current function budget.
  bool VisitVarDecl(VarDecl* variable) {
    if (!object(variable->getType())) return reject(variable->getLocation(), "this object type (including stored pointers/references)");
    if (variable->getTLSKind() != VarDecl::TLS_None) return reject(variable->getLocation(), "thread-local storage");
    if (variable->hasExternalFormalLinkage()) return reject(variable->getLocation(), "externally visible data objects");
    if (variable->hasGlobalStorage() && variable->hasInit() && !variable->getInit()->isConstantInitializer(C, false))
      return reject(variable->getLocation(), "dynamic static initialization");
    if (Current && !variable->hasGlobalStorage()) {
      LocalBytes += static_cast<std::uint64_t>(C.getTypeSizeInChars(variable->getType()).getQuantity()) + 16;
      if (LocalBytes > 1024) return reject(variable->getLocation(), "more than 1024 bytes of local objects and tracking slots");
    }
    return true;
  }
  // This default-reject switch is the main source admission boundary. The operator,
  // cast and call visitors below add stricter rules to the otherwise admitted AST shapes.
  bool VisitStmt(Stmt* statement) {
    if (++Nodes > 4096) return reject(statement->getBeginLoc(), "more than 4096 source statement/expression nodes");
    switch (statement->getStmtClass()) {
    case Stmt::CompoundStmtClass: case Stmt::DeclStmtClass: case Stmt::ReturnStmtClass:
    case Stmt::IfStmtClass: case Stmt::WhileStmtClass: case Stmt::DoStmtClass: case Stmt::ForStmtClass:
    case Stmt::BreakStmtClass: case Stmt::ContinueStmtClass: case Stmt::NullStmtClass:
    case Stmt::BinaryOperatorClass: case Stmt::CompoundAssignOperatorClass: case Stmt::UnaryOperatorClass:
    case Stmt::IntegerLiteralClass: case Stmt::CXXBoolLiteralExprClass: case Stmt::CXXNullPtrLiteralExprClass:
    case Stmt::ImplicitCastExprClass: case Stmt::CXXStaticCastExprClass: case Stmt::ParenExprClass:
    case Stmt::DeclRefExprClass: case Stmt::ArraySubscriptExprClass: case Stmt::CallExprClass:
    case Stmt::ConditionalOperatorClass: case Stmt::InitListExprClass: case Stmt::ImplicitValueInitExprClass:
    case Stmt::UnaryExprOrTypeTraitExprClass: break;
    default: return reject(statement->getBeginLoc(), statement->getStmtClassName());
    }
    if (auto* expression = dyn_cast<Expr>(statement))
      if (!expressionType(expression->getType())) return reject(expression->getBeginLoc(), "this expression type");
    return true;
  }
  // Only explicit listed conversions are accepted. Reinterpretation, integer-to-pointer,
  // record hierarchy casts and other erased semantic relationships cannot sneak through
  // a generic CastExpr acceptance rule.
  bool VisitCastExpr(CastExpr* cast) {
    switch (cast->getCastKind()) {
    case CK_NoOp: case CK_LValueToRValue: case CK_ArrayToPointerDecay: case CK_FunctionToPointerDecay:
    case CK_NullToPointer: case CK_PointerToBoolean: case CK_IntegralCast: case CK_IntegralToBoolean:
    case CK_ToVoid: return true;
    default: return reject(cast->getBeginLoc(), "this cast");
    }
  }
  // Even unevaluated type queries stay within the supported vocabulary. Some harmless
  // queries are rejected deliberately instead of expanding the profile implicitly.
  bool VisitUnaryExprOrTypeTraitExpr(UnaryExprOrTypeTraitExpr* expression) {
    if (expression->getKind() != UETT_SizeOf && expression->getKind() != UETT_AlignOf)
      return reject(expression->getBeginLoc(), "this type-trait operation");
    return object(expression->getTypeOfArgument()) ||
        reject(expression->getBeginLoc(), "type queries for unsupported object types");
  }
  // Record exact direct-call edges for the final recursion check. Missing definitions
  // remain foreign symbols in the IR report and are rejected by the strict driver.
  bool VisitCallExpr(CallExpr* call) {
    auto* function = call->getDirectCallee();
    if (!function || function->getBuiltinID()) return reject(call->getBeginLoc(), "indirect or builtin calls");
    if (Current) Edges[Current].push_back(function->getCanonicalDecl());
    return true;
  }
  // Unsigned standalone updates are defined at wraparound. Signed negation must be
  // proven constant; other unmodeled unary operators are rejected explicitly.
  bool VisitUnaryOperator(UnaryOperator* operation) {
    auto code = operation->getOpcode();
    if (operation->isIncrementDecrementOp())
      return (operation->getType()->isUnsignedIntegerType() && standalone(operation)) ||
          reject(operation->getBeginLoc(), "signed or nested increment/decrement");
    switch (code) {
    case UO_AddrOf: case UO_Deref: case UO_Plus: case UO_Not: case UO_LNot: return true;
    case UO_Minus: {
      llvm::APSInt value;
      return operation->getType()->isUnsignedIntegerType() || constant(operation, value) ||
          reject(operation->getBeginLoc(), "dynamic signed negation");
    }
    default: return reject(operation->getBeginLoc(), "this unary operator");
    }
  }
  // Gate sequencing, pointer operations and arithmetic independently. Checked GEPs cover
  // supported pointer offsets; pointer differences/ordered comparisons require semantics
  // not supplied by that check and are rejected.
  bool VisitBinaryOperator(BinaryOperator* operation) {
    auto code = operation->getOpcode();
    if (operation->isAssignmentOp() && !standalone(operation))
      return reject(operation->getBeginLoc(), "nested assignment side effects");
    if (operation->isComparisonOp() &&
        (operation->getLHS()->getType()->isPointerType() || operation->getRHS()->getType()->isPointerType()) &&
        code != BO_EQ && code != BO_NE)
      return reject(operation->getBeginLoc(), "ordered pointer comparisons");
    if (code == BO_Assign || operation->isComparisonOp() || code == BO_LAnd || code == BO_LOr || code == BO_Comma)
      return true;
    auto computation = operation->getType();
    if (auto* compound = dyn_cast<CompoundAssignOperator>(operation)) computation = compound->getComputationResultType();
    if (operation->getLHS()->getType()->isPointerType() || operation->getRHS()->getType()->isPointerType())
      return (computation->isPointerType() && object(computation->getPointeeType()) && (code == BO_Add || code == BO_Sub)) ||
          reject(operation->getBeginLoc(), "pointer subtraction or compound pointer arithmetic");
    llvm::APSInt value;
    // A genuinely defined, side-effect-free constant expression is already resolved by
    // the frontend. Dynamic arithmetic instead follows the narrow unsigned/divisor/shift rules.
    if (constant(operation, value)) return true;
    switch (code) {
    case BO_Add: case BO_Sub: case BO_Mul: case BO_AddAssign: case BO_SubAssign: case BO_MulAssign:
      return computation->isUnsignedIntegerType() || reject(operation->getBeginLoc(), "dynamic signed arithmetic");
    case BO_And: case BO_Or: case BO_Xor: case BO_AndAssign: case BO_OrAssign: case BO_XorAssign: return true;
    case BO_Div: case BO_Rem: case BO_DivAssign: case BO_RemAssign:
      if (constant(operation->getRHS(), value) && !value.isZero() &&
          (computation->isUnsignedIntegerType() || !value.isNegative())) return true;
      return reject(operation->getBeginLoc(), "division/remainder without a proven safe constant divisor");
    case BO_Shl: case BO_Shr: case BO_ShlAssign: case BO_ShrAssign:
      if (computation->isUnsignedIntegerType() && constant(operation->getRHS(), value) && !value.isNegative() &&
          value.getActiveBits() <= 8 && value.getZExtValue() < C.getTypeSize(computation)) return true;
      return reject(operation->getBeginLoc(), "unproven shift operands");
    default: return reject(operation->getBeginLoc(), "this binary operator");
    }
  }
  // Do not acknowledge admission after any Clang/preprocessor error. Check the complete
  // canonical call graph only after all top-level declarations/definitions have been seen.
  bool finish() {
    if (Failed || CI.getDiagnostics().hasErrorOccurred()) return false;
    llvm::SmallVector<FunctionDecl*, 16> functions;
    for (auto& edge : Edges) functions.push_back(edge.first);
    for (auto* function : functions) if (!acyclic(function)) return false;
    return true;
  }
};
}
