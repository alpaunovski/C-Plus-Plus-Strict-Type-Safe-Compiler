"""Transform source ranges selected by Clang's semantic AST, never by regex parsing.

This is a bootstrap backend. It deliberately instruments main files only; the
coverage report records constructs that need a native frontend/IR backend.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


# Abort a rewrite when the semantic ranges cannot be composed safely. Producing
# partly transformed C++ would be worse than declining that source construct.
class InstrumentationError(Exception):
    pass


@dataclass(frozen=True)
# A half-open byte range in the original UTF-8 source. Referencing original bytes
# lets nested rewrites preserve spelling and avoids confusing Unicode characters with offsets.
class Slice:
    start: int
    end: int


@dataclass
# Replacement pieces combine literal injected bytes with recursively rendered source
# slices. Slices are essential when one checked expression contains another check site.
class Edit:
    start: int
    end: int
    parts: list[bytes | Slice]


# Compose immutable-source edits without reparsing C++ text. Outer replacements own
# their nested ranges; partially crossing ranges are rejected instead of guessed.
class Editor:
    # Keep raw source immutable and record edits separately. Signature sets deduplicate
    # visits to AST nodes that share source ranges, such as instantiated template forms.
    def __init__(self, source: bytes):
        self.source = source
        self.edits: dict[tuple[int, int], Edit] = {}
        self.signatures: dict[tuple[int, int], set[tuple]] = {}
        self.insertions: dict[int, list[bytes]] = {}

    # Merge wrappers of the same range when one explicitly contains the whole original
    # slice. Two unrelated replacements of one range cannot be ordered safely and are errors.
    def replace(self, start: int, end: int, *parts: str | bytes | Slice):
        edit = Edit(start, end, [p.encode() if isinstance(p, str) else p for p in parts])
        previous = self.edits.get((start, end))
        signature = tuple(edit.parts)
        seen = self.signatures.setdefault((start, end), set())
        if signature in seen:
            return
        seen.add(signature)
        if previous and previous.parts != edit.parts:
            whole = Slice(start, end)
            if whole in previous.parts:
                previous.parts = [piece for part in previous.parts
                                  for piece in (edit.parts if part == whole else [part])]
                return
            if whole in edit.parts:
                edit.parts = [piece for part in edit.parts
                              for piece in (previous.parts if part == whole else [part])]
            else:
                raise InstrumentationError(f"conflicting AST rewrites at byte {start}")
        self.edits[start, end] = edit

    # Insertions are ordered at a byte boundary and deduplicated by exact payload.
    # They are used for guards and generated friend layout descriptions.
    def insert(self, offset: int, text: str):
        values = self.insertions.setdefault(offset, [])
        if text.encode() not in values:
            values.append(text.encode())

    # Select the outermost edits for this range, then recursively render referenced
    # slices. Excluding the currently expanded replacement prevents infinite self-wrapping.
    def render(self, start=0, end=None, excluded=frozenset()) -> bytes:
        if end is None:
            end = len(self.source)
        edits = sorted((e for key, e in self.edits.items()
                        if key not in excluded and start <= e.start < e.end <= end),
                       key=lambda e: (e.start, -e.end))
        outer: list[Edit] = []
        for edit in edits:
            if outer and edit.start < outer[-1].end:
                if edit.end > outer[-1].end:
                    raise InstrumentationError("overlapping AST ranges; refusing unsafe rewrite")
                continue
            outer.append(edit)
        result = bytearray()
        cursor = start
        for edit in outer:
            result.extend(self.raw(cursor, edit.start))
            # Insertions at replacement boundaries belong to the replacement.
            result.extend(b"".join(self.insertions.get(edit.start, [])))
            for part in edit.parts:
                if isinstance(part, Slice):
                    result.extend(self.render(part.start, part.end,
                                              excluded | {(edit.start, edit.end)}))
                else:
                    result.extend(part)
            cursor = edit.end
        result.extend(self.raw(cursor, end))
        return bytes(result)

    # Copy untouched source while applying boundary insertions. This is separate from
    # replacement rendering so insertions inside preserved source are not lost.
    def raw(self, start: int, end: int) -> bytes:
        result = bytearray()
        cursor = start
        for offset in sorted(o for o in self.insertions if start <= o < end):
            result.extend(self.source[cursor:offset])
            result.extend(b"".join(self.insertions[offset]))
            cursor = offset
        result.extend(self.source[cursor:end])
        return bytes(result)


# AST traversal categories encode semantic context: function scopes own guards,
# unevaluated operands must not trigger checks, and transparent wrappers do not
# change whether an underlying access is a read or write.
FUNCTIONS = {"FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl",
             "CXXConversionDecl"}
UNEVALUATED = {"UnaryExprOrTypeTraitExpr", "CXXNoexceptExpr", "TypeTraitExpr",
               "RequiresExpr", "DecltypeType", "StaticAssertDecl"}
TRANSPARENT = {"ParenExpr", "ImplicitCastExpr", "ExprWithCleanups", "MaterializeTemporaryExpr",
               "CXXBindTemporaryExpr", "ConstantExpr"}


# Translate Clang JSON nodes into source edits plus an explicit limitation report.
# Only trustworthy main-file byte ranges are rewritten; macro/header ranges become
# limitations because the bootstrap cannot safely edit their expansion/spelling origins.
class Instrumenter:
    # Precompute byte offsets of line starts once. Diagnostics use the original path and
    # line, while all editing remains in bytes to support non-ASCII identifiers and paths.
    def __init__(self, path: Path, source: bytes):
        self.path = path.resolve()
        self.source = source
        self.editor = Editor(source)
        self.lines = [0] + [i + 1 for i, b in enumerate(source) if b == 10]
        self.counts: Counter[str] = Counter()
        self.notes: dict[tuple[int, str], dict] = {}
        self.seen: set[tuple] = set()
        self.fields: dict[str, dict] = {}

    # Reject expansion, included-file and incomplete locations. Clang end offsets identify
    # the final token start, so tokLen is required to form an exclusive source endpoint.
    def span(self, node: dict) -> Slice | None:
        r = node.get("range", {})
        begin, end = r.get("begin", {}), r.get("end", {})
        if any(k in p for p in (begin, end) for k in ("spellingLoc", "expansionLoc", "includedFrom")):
            return None
        if "offset" not in begin or "offset" not in end:
            return None
        a, b = begin["offset"], end["offset"] + end.get("tokLen", 0)
        if not 0 <= a < b <= len(self.source):
            return None
        return Slice(a, b)

    # Emit a C++ Source initializer using JSON-compatible string escaping for the path.
    # The runtime will report the original source rather than the generated temporary file.
    def location(self, offset: int) -> str:
        return "{ " + json.dumps(str(self.path), ensure_ascii=False) + ", " + str(self.line(offset)) + " }"

    # Binary search maps a byte offset to its one-based source line without repeatedly
    # scanning the beginning of a large translation unit.
    def line(self, offset: int) -> int:
        return bisect_right(self.lines, offset)

    # Deduplicate limitations by expansion offset and message. Macro diagnostics should
    # point at the invocation visible to the user rather than an unrelated definition file.
    def note(self, node: dict, message: str):
        location = node.get("range", {}).get("begin", {})
        location = location.get("expansionLoc", location)
        offset = location.get("offset", 0)
        self.notes[offset, message] = {"line": self.line(offset), "message": message}

    # Count source check sites once even when Clang exposes the same range through more
    # than one semantic node or template instantiation.
    def mark(self, name: str, span: Slice):
        key = name, span.start, span.end
        if key not in self.seen:
            self.counts[name] += 1
            self.seen.add(key)

    @staticmethod
    def children(node: dict):
        # Clang serializes array initializers under array_filler, not inner.
        return node.get("inner", []) + node.get("array_filler", [])

    # Walk outward through transparent AST wrappers to find a write target. An explicit
    # lvalue-to-rvalue conversion ends that search because it represents a read.
    def write_context(self, parents: list[dict], node: dict) -> bool:
        current = node
        for parent in reversed(parents):
            kind = parent.get("kind")
            if kind == "ImplicitCastExpr" and parent.get("castKind") == "LValueToRValue":
                return False
            children = parent.get("inner", [])
            if kind in ("BinaryOperator", "CompoundAssignOperator"):
                return (parent.get("opcode", "").endswith("=") and
                        parent.get("opcode") not in ("==", "!=", "<=", ">=") and
                        bool(children) and children[0] is current)
            if kind == "UnaryOperator":
                return parent.get("opcode") in ("++", "--")
            if kind not in TRANSPARENT and kind != "MemberExpr":
                return False
            current = parent
        return False

    # Forming &*p does not read the pointed-to object. Avoid turning an address-only
    # expression into a dereference check, especially for legal null sentinel expressions.
    def address_only(self, parents: list[dict]) -> bool:
        for parent in reversed(parents):
            if parent.get("kind") in TRANSPARENT:
                continue
            return parent.get("kind") == "UnaryOperator" and parent.get("opcode") == "&"
        return False

    # Traverse semantic nodes, render all queued edits, and add a #line directive for
    # original-source diagnostics. The report deliberately states main-file-only coverage.
    def run(self, ast: dict) -> tuple[bytes, dict]:
        # Main-file nodes have no includedFrom. Explicit file locations, when
        # present, must still agree (e.g. command-line forced includes).
        self.visit(ast, [], False)
        result = self.editor.render()
        result += b"".join(self.editor.insertions.get(len(self.source), []))
        prefix = ("#line 1 " + json.dumps(str(self.path), ensure_ascii=False) + "\n").encode()
        report = {"source": str(self.path), "backend": "clang-json-ast",
                  "scope": "main file only; included headers are not instrumented",
                  "counts": dict(sorted(self.counts.items())),
                  "limitations": list(self.notes.values())}
        return prefix + result, report

    # Apply context-sensitive wrappers only after checking file ownership and evaluation
    # context. The original child nodes remain the traversal source, so injected helper
    # text is never recursively treated as user C++ to instrument.
    def visit(self, node: dict, parents: list[dict], in_function: bool,
              constexpr=False):
        kind = node.get("kind", "")
        if node.get("isImplicit"):
            return
        loc = node.get("loc", {})
        begin = node.get("range", {}).get("begin", {})
        if "includedFrom" in loc or "includedFrom" in begin:
            return
        explicit_file = loc.get("file") or begin.get("file")
        if explicit_file and Path(explicit_file).resolve() != self.path:
            return
        if kind in UNEVALUATED:
            return
        if kind == "LambdaExpr":
            in_function = True
        if kind in FUNCTIONS:
            in_function = True
            constexpr = bool(node.get("constexpr") or node.get("immediate"))
            if constexpr:
                self.note(node, "constexpr function: local storage is not registered; pointer checks remain at runtime")
            if not constexpr:
                body = next((c for c in node.get("inner", []) if c.get("kind") == "CompoundStmt"), None)
                if body and self.span(body):
                    at = self.span(body).start + 1
                    for parm in node.get("inner", []):
                        if parm.get("kind") == "ParmVarDecl" and parm.get("name"):
                            typ = parm.get("type", {}).get("qualType", "")
                            if "&" not in typ:
                                name = parm["name"]
                                self.editor.insert(at, f" ::__strictcpp::Guard __sc_arg_{name}({name}, {self.location(at)}); ")
                                self.mark("parameters", self.span(parm) or self.span(body))
                    parameters = [p for p in node.get("inner", []) if p.get("kind") == "ParmVarDecl"]
                    if (node.get("name") == "main" and len(parameters) >= 2 and
                            all(p.get("name") for p in parameters[:2]) and
                            parameters[1].get("type", {}).get("qualType") == "char **"):
                        self.editor.insert(at, f" ::__strictcpp::arguments({parameters[0]['name']}, {parameters[1]['name']}, {self.location(at)}); ")
        span = self.span(node)
        children = self.children(node)
        parent = parents[-1] if parents else {}

        if kind == "CXXRecordDecl" and node.get("completeDefinition") and span:
            self.record(node, span, in_function)
        if kind == "DeclStmt" and span and not constexpr:
            self.declarations(node, span, parent)
        if kind == "VarDecl" and not in_function and span:
            if parent.get("kind") in ("TranslationUnitDecl", "NamespaceDecl", "LinkageSpecDecl"):
                self.global_variable(node, span, parent)

        write = "true" if self.write_context(parents, node) else "false"
        if kind == "UnaryOperator" and node.get("opcode") == "*" and not self.address_only(parents):
            if span and children and (operand := self.span(children[0])):
                if "(" in node.get("type", {}).get("qualType", ""):
                    self.note(node, "function pointer dereference is not checked")
                else:
                    self.editor.replace(span.start, span.end, "(*::__strictcpp::pointer((", operand,
                                        f"), {self.location(span.start)}, {write}))")
                    self.mark("dereferences", span)
            else:
                self.note(node, "macro-expanded dereference is not instrumented")
        elif kind == "ArraySubscriptExpr" and not self.address_only(parents):
            if span and len(children) == 2 and all(self.span(c) for c in children):
                first, second = map(self.span, children)
                first_type = children[0].get("type", {}).get("qualType", "")
                helper = "indexed" if "*" in first_type or "[" in first_type else "reverse_indexed"
                self.editor.replace(span.start, span.end, f"(::__strictcpp::{helper}((", first,
                                    f"), {self.location(span.start)}, {write}).at((", second, ")))")
                self.mark("subscripts", span)
            else:
                self.note(node, "macro-expanded subscript is not instrumented")
        elif kind == "MemberExpr" and node.get("isArrow"):
            # Wrap the base expression, leaving the selected member untouched.
            if (children and children[0].get("kind") == "CXXThisExpr" and
                    (children[0].get("implicit") or
                     (self.span(children[0]) and self.source[self.span(children[0]).start:self.span(children[0]).end] != b"this"))):
                pass  # An unqualified field has no written pointer to wrap.
            elif any(p.get("kind") == "CXXConstructorDecl" for p in parents):
                self.note(node, "explicit this access during construction is not checked")
            elif span and children and (base := self.span(children[0])):
                self.editor.replace(base.start, base.end, "(::__strictcpp::pointer((", base,
                                    f"), {self.location(span.start)}, false))")
                field = self.fields.get(node.get("referencedMemberDecl"), {})
                if field and not field.get("isBitfield"):
                    self.editor.replace(span.start, span.end, "(::__strictcpp::reference((", span,
                                        f"), {self.location(span.start)}, {write}))")
                self.mark("arrow_bases", span)
            else:
                self.note(node, "macro-expanded member access is not instrumented")
        elif kind in ("CXXReinterpretCastExpr", "CStyleCastExpr") and node.get("valueCategory") in ("lvalue", "xvalue"):
            if span:
                self.editor.replace(span.start, span.end, "(::__strictcpp::reference((", span,
                                    f"), {self.location(span.start)}, {write}))")
                self.mark("reference_casts", span)
        elif kind == "CXXNewExpr":
            self.allocation(node, span, in_function)
        elif kind == "CXXDeleteExpr":
            if span and children and (operand := self.span(children[0])):
                array = bool(node.get("isArray"))
                capture = "[&]" if in_function else "[]"
                delete = "delete[]" if array else "delete"
                self.editor.replace(span.start, span.end, "(::__strictcpp::deleted((", operand,
                                    f"), {capture}(auto* __sc_p) {{ {delete} __sc_p; }}, "
                                    f"{'true' if array else 'false'}, {self.location(span.start)}))")
                self.mark("deletions", span)
            else:
                self.note(node, "macro-expanded delete is not instrumented")
        elif kind == "ImplicitCastExpr" and node.get("castKind") == "ArrayToPointerDecay":
            if children and children[0].get("kind") == "StringLiteral" and span:
                self.editor.replace(span.start, span.end, "(::__strictcpp::literal(", span,
                                    f", {self.location(span.start)}))")
                self.mark("string_literals", span)
        elif kind == "CXXOperatorCallExpr":
            self.note(node, "overloaded operator/library body is not instrumented by this backend")
        elif kind == "CXXForRangeStmt":
            self.note(node, "range-for implicit storage is not registered")
        elif kind in ("GCCAsmStmt", "MSAsmStmt"):
            self.note(node, "inline assembly is outside instrumentation coverage")

        for child in children:
            self.visit(child, parents + [node], in_function, constexpr)

    # Place guards around supported declarations while preserving C++ scope and reverse
    # destruction ordering. Control-statement initializers require a different surrounding
    # scope from ordinary declarations; references/implicit storage are handled conservatively.
    def declarations(self, node: dict, span: Slice, parent: dict):
        variables = [v for v in node.get("inner", []) if v.get("kind") == "VarDecl" and
                     v.get("name") and "&" not in v.get("type", {}).get("qualType", "") and
                     v.get("storageClass") != "extern"]
        if not variables:
            return
        if parent.get("kind") != "CompoundStmt":
            self.note(node, "declaration in a control initializer/body is not registered")
            return
        for var in variables:
            name = var["name"]
            guard = f"__sc_object_{span.start}_{name}"
            storage = "thread_local " if var.get("tls") else ("static " if var.get("storageClass") == "static" else "")
            # Declared first so the guard outlives the object's destructor.
            self.editor.insert(span.start, f" {storage}::__strictcpp::Guard {guard}; ")
            self.editor.insert(span.end, f" {guard}.bind({name}, {self.location(span.start)}); ")
            self.mark("objects", self.span(var) or span)

    # Append registration after the complete global declaration, not after an individual
    # comma-separated declarator. The terminator scan skips strings/comments whose own
    # semicolons would otherwise place injected code inside the declaration.
    def global_variable(self, node: dict, span: Slice, parent: dict):
        if not node.get("name") or (node.get("storageClass") == "extern" and "init" not in node):
            return
        # Multiple declarators share a start and a semicolon. Start scanning
        # after the final AST-selected initializer, so semicolons in later
        # initializers' strings/lambdas cannot be mistaken for the terminator.
        after = span.end
        for sibling in parent.get("inner", []):
            other = self.span(sibling)
            if sibling.get("kind") == "VarDecl" and other and other.start == span.start:
                after = max(after, other.end)
        end = after
        while end < len(self.source):
            if self.source[end:end + 2] == b"/*":
                close = self.source.find(b"*/", end + 2)
                end = len(self.source) if close < 0 else close + 2
            elif self.source[end:end + 2] == b"//":
                close = self.source.find(b"\n", end + 2)
                end = len(self.source) if close < 0 else close + 1
            elif self.source[end:end + 1].isspace():
                end += 1
            else:
                break
        if self.source[end:end + 1] != b";":
            raise InstrumentationError("cannot locate global declaration terminator")
        name = node["name"]
        storage = "thread_local " if node.get("tls") else ""
        self.editor.insert(end + 1, f" {storage}static ::__strictcpp::Guard __sc_global_{span.start}_{name}({name}, {self.location(span.start)}); ")
        self.mark("globals", span)

    # Generate ADL-discovered friend descriptions so private fields and base subobjects
    # can be registered without changing access control for user code. Unsupported local,
    # anonymous, union, reference and bitfield layouts are reported rather than invented.
    def record(self, node: dict, span: Slice, in_function: bool):
        if in_function or not node.get("name") or node.get("tagUsed") == "union":
            self.note(node, "local/anonymous records and active union members have no generated subobject metadata")
            return
        fields = [c for c in node.get("inner", []) if c.get("kind") == "FieldDecl"]
        self.fields.update((field["id"], field) for field in fields)
        commands = []
        for field in fields:
            if not field.get("name") or field.get("isBitfield") or "&" in field.get("type", {}).get("qualType", ""):
                self.note(field, "bitfield/anonymous/reference member metadata is not generated")
                continue
            commands.append(f"__sc_b.add(std::addressof(__sc_p->{field['name']}));")
        name = node["name"]
        for constant in (False, True):
            cv = "const " if constant else ""
            base_commands = []
            for base in node.get("bases", []):
                typ = base.get("type", {}).get("qualType")
                if typ:
                    base_commands.append(f"__sc_b.add(static_cast<{cv}{typ}*>(__sc_p));")
            text = (f" friend void __strictcpp_describe({cv}{name}* __sc_p, ::__strictcpp::Builder& __sc_b) {{ (void)__sc_p; (void)__sc_b; " +
                    " ".join(base_commands + commands) + " } ")
            self.editor.insert(span.end - 1, text)
        self.mark("record_layouts", span)

    # Wrap allocation results and evaluate array bounds exactly once. Placement/nothrow
    # forms and macro ranges lack the lifetime/range support needed by this bootstrap.
    def allocation(self, node: dict, span: Slice | None, in_function: bool):
        if not span:
            self.note(node, "macro-expanded allocation is not instrumented")
            return
        if node.get("isPlacement"):
            self.note(node, "placement/nothrow new needs explicit lifetime support and is not instrumented")
            return
        source = self.location(span.start)
        if not node.get("isArray"):
            self.editor.replace(span.start, span.end, "(::__strictcpp::allocated((", span,
                                f"), {source}))")
        else:
            count = node.get("inner", [None])[0]
            count_span = self.span(count) if count else None
            capture = "[&]" if in_function else "[]"
            if not count_span:
                self.note(node, "array bound has no rewritable source range")
                return
            # For an omitted bound, Clang gives the synthesized count the range
            # of the type name. Preserve new T[]{...} and use its constant count.
            opening = self.source.find(b"[", span.start, span.end)
            if count_span.start < opening:
                number = count.get("value")
                if number is None:
                    self.note(node, "inferred array bound is not supported")
                    return
                self.editor.replace(span.start, span.end, f"(::__strictcpp::allocated_array({capture}([[maybe_unused]] auto __sc_n) {{ return ",
                                    span, f"; }}, {number}, {source}))")
            else:
                self.editor.replace(span.start, span.end, f"(::__strictcpp::allocated_array({capture}(auto __sc_n) {{ return ",
                                    Slice(span.start, count_span.start), "__sc_n",
                                    Slice(count_span.end, span.end), "; }, (", count_span,
                                    f"), {source}))")
        self.mark("allocations", span)
