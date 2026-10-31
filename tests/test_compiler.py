# Regression suite for the explicitly selected, archived JSON-AST source rewriter.
# These cases protect its supported behavior and documented weaker coverage separately
# from native LLVM checks and the strict source-admission profile.
"""End-to-end compiler tests: compile ordinary C++ and execute the result."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "bin" / "strict++"


class CompilerTests(unittest.TestCase):
    # Each test owns a temporary directory, including generated sources and linked artifacts.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="strictcpp-test-")
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    # Always select the AST backend explicitly. Compiler default changes must not silently
    # convert these bootstrap regression cases into native-pass tests.
    def compile(self, code, *flags, name="program.cpp", extra=()):
        source = self.directory / name
        source.write_text(code)
        binary = self.directory / "program"
        result = subprocess.run([str(DRIVER), "--sc-backend", "ast", "--sc-quiet", str(source), *flags,
                                 *map(str, extra), "-o", str(binary)],
                                capture_output=True, text=True)
        return result, binary, source

    # Separate compile failures from runtime failures and require the original source path
    # in negative diagnostics so temporary rewritten filenames do not leak into user errors.
    def run_code(self, code, *flags, status=0, diagnostic=None):
        compilation, binary, source = self.compile(code, *flags)
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        if diagnostic:
            self.assertIn("strictcpp: " + diagnostic, result.stderr)
            self.assertIn(str(source) + ":", result.stderr)
        return result

    # Canonical aliases and a legal void-pointer round trip must not cause a false type mismatch.
    def test_void_round_trip_and_typedef(self):
        self.run_code("using Number=int; int read(void* p){return *static_cast<Number*>(p);} "
                      "int main(){Number n=42; void* p=&n; return read(p)!=42;}")

    # Generated wrappers must diagnose the invalid access before ordinary optimization can
    # exploit the source's aliasing violation.
    def test_type_confusion_at_optimization_levels(self):
        for optimization in ("-O0", "-O2", "-O3"):
            with self.subTest(optimization=optimization):
                self.run_code("int main(){int n=42; void* p=&n; return *static_cast<float*>(p);}",
                              optimization, status=86, diagnostic="type-mismatch")

    # A checked write must fail before changing storage through an unrelated access type.
    def test_write_through_wrong_type(self):
        self.run_code("int main(){int n=42; float* p=reinterpret_cast<float*>(&n); *p=1.0f;}",
                      status=86, diagnostic="type-mismatch")

    # A raw null dereference is diagnosed rather than delegated to an OS fault.
    def test_null(self):
        self.run_code("int main(){int* p=nullptr; return *p;}",
                      status=86, diagnostic="null-access")

    # Bounds alone do not authorize a load at an address incompatible with the requested alignment.
    def test_misalignment(self):
        self.run_code("int main(){alignas(int) char a[16]{}; auto p=reinterpret_cast<int*>(a+1); return *p;}",
                      status=86, diagnostic="misaligned-access")

    # Selected signed/unsigned access aliases and both a[i]/i[a] spellings must remain valid.
    def test_arrays_and_signed_alias(self):
        self.run_code("int main(){int a[3]={1,2,3}; int* p=a; p[1]=8; "
                      "unsigned int* q=reinterpret_cast<unsigned int*>(a); "
                      "return q[1]!=8 || 2[a]!=3;}")

    # RTTI access keys must account for allowed deep qualification differences without copying objects.
    def test_similar_pointer_types_with_deep_const(self):
        self.run_code("int main(){int n=7; int* p=&n; const int* const* q=&p; return **q!=7;}")

    # A saved pointer to a stack array may not read a one-past element.
    def test_stack_array_bounds(self):
        self.run_code("int main(){int a[3]={1,2,3}; int* p=a; return p[3];}",
                      status=86, diagnostic="out-of-bounds")

    # Index scaling must be checked before unsigned address arithmetic wraps.
    def test_index_overflow(self):
        self.run_code("int main(){int a[3]{}; unsigned long long n=~0ULL; return a[n];}",
                      status=86, diagnostic="index-overflow")

    # Recursive region registration must retain legitimate nested-array element access.
    def test_multidimensional_array(self):
        self.run_code("int main(){int a[2][3]={{1,2,3},{4,5,6}}; return a[1][2]!=6;}")

    # Permitted unsigned-char/std::byte representation reads must not be confused with arbitrary typed aliasing.
    def test_byte_access(self):
        self.run_code("#include <cstddef>\nint main(){int n=1; "
                      "auto p=reinterpret_cast<unsigned char*>(&n); "
                      "auto q=reinterpret_cast<std::byte*>(&n); return p[0]!=1 || q[0]!=std::byte{1};}")

    # An lvalue reinterpret cast must validate the referred-to storage, not merely return a new reference type.
    def test_reference_cast(self):
        self.run_code("int main(){int n=1; float& p=reinterpret_cast<float&>(n); return p;}",
                      status=86, diagnostic="type-mismatch")

    # A const_cast changes the access expression, not the underlying object's write permission.
    def test_const_write(self):
        self.run_code("int main(){const int n=1; int* p=const_cast<int*>(&n); *p=2;}",
                      status=86, diagnostic="write-to-const")

    # Basic allocation registration and post-destructor retirement must preserve normal scalar new/delete.
    def test_heap_and_delete(self):
        self.run_code("int main(){int* p=new int(42); int n=*p; delete p; return n!=42;}")

    # The rewritten allocation lambda must use one captured bound value, preserving n++ semantics.
    def test_heap_array_bound_evaluated_once(self):
        self.run_code("int main(){int n=3; int* p=new int[n++]{}; p[2]=7; "
                      "int result=(n!=4 || p[2]!=7); delete[] p; return result;}")

    # Clang's synthesized bound has a different source range; preserve new T[]{...} and register its count.
    def test_inferred_heap_array_bound(self):
        self.run_code("int main(){auto p=new int[]{1,2,3}; int x=p[2]; delete[] p; return x!=3;}")

    # Array construction/registration must retain element layout for multidimensional heap storage.
    def test_multidimensional_heap_array(self):
        self.run_code("int main(){auto p=new int[2][3]{}; p[1][2]=7; "
                      "int x=p[1][2]; delete[] p; return x!=7;}")

    # Retired storage must remain visible to expired-object diagnostics.
    def test_use_after_delete(self):
        self.run_code("int main(){int* p=new int(42); delete p; return *p;}",
                      status=86, diagnostic="expired-object")

    # A second delete must be rejected before invoking the deallocator again.
    def test_double_delete(self):
        self.run_code("int main(){int* p=new int(42); delete p; delete p;}",
                      status=86, diagnostic="double-delete")

    # Access aliasing does not allow mixing scalar and array deletion families.
    def test_delete_kind(self):
        self.run_code("int main(){int* p=new int[2]{}; delete p;}",
                      status=86, diagnostic="delete-kind-mismatch")

    # RAII guards must retire local storage during exception unwinding before the catch reuses its address.
    def test_scope_expiration_and_exception_cleanup(self):
        self.run_code("int main(){int* p=nullptr; try {int n=7; p=&n; throw 1;} "
                      "catch(int){} return *p;}", status=86, diagnostic="expired-object")

    # Generated ADL friends describe private fields and nested objects without public API changes.
    def test_struct_private_and_nested_subobjects(self):
        self.run_code("struct Inner{int n;}; class S{int secret=7; public: Inner a{42}; "
                      "int get(){return secret;}}; int main(){S s; int* p=&s.a.n; "
                      "S* q=&s; return *p!=42 || q->get()!=7;}")

    # Matching a containing allocation is insufficient when the selected field has the wrong access type.
    def test_struct_subobject_type_confusion(self):
        self.run_code("struct S{int n; double d;}; int main(){S s{42,1.0}; "
                      "float* p=reinterpret_cast<float*>(&s.n); return *p;}",
                      status=86, diagnostic="type-mismatch")

    # Base-region descriptions must allow legitimate inheritance access while retaining the complete allocation.
    def test_base_class_and_heap_record(self):
        self.run_code("struct B{int n=7;}; struct D:B{double x=2;}; "
                      "int main(){D* d=new D; B* b=d; int x=b->n; delete d; return x!=7;}")

    # A virtual secondary-base delete may use an adjusted address; validate the described base
    # and still run the actual most-derived destructor.
    def test_polymorphic_delete_through_nonzero_base(self):
        self.run_code("int result=0; struct A{virtual ~A(){} int x=1;}; "
                      "struct B{virtual ~B(){} int n=2;}; struct D:A,B{~D(){result=7;}}; "
                      "int main(){D* p=new D; B* b=p; delete b; return result!=7;}")

    # A corresponding unsigned type may be an allowed read alias while still being an invalid delete type.
    def test_delete_signedness_is_not_an_aliasing_exception(self):
        self.run_code("int main(){auto p=new int(3); auto q=reinterpret_cast<unsigned int*>(p); delete q;}",
                      status=86, diagnostic="delete-type-mismatch")

    # Choose the specific mutable field region over a broader const record region.
    def test_mutable_field_of_const_object(self):
        self.run_code("struct S{mutable int n=7;}; int main(){const S s; const S* p=&s; p->n=8; return p->n!=8;}")

    # Non-mutable fields retain const protection even when the enclosing pointer was cast.
    def test_const_record_field_write(self):
        self.run_code("struct S{int n=7;}; int main(){const S s; S* p=const_cast<S*>(&s); p->n=8;}",
                      status=86, diagnostic="write-to-const")

    # Nested base/member wrappers must compose without evaluating the original chain incorrectly.
    def test_chained_arrow_and_pointer_field(self):
        self.run_code("struct B{int n;}; struct A{B* b;}; int main(){B b{7}; A a{&b}; A* p=&a; return p->b->n!=7;}")

    # Generated friend descriptions must remain warning-clean when a record has no fields.
    def test_empty_record_with_warnings_as_errors(self):
        self.run_code("struct S{}; int main(){S s; S* p=&s; return p==nullptr;}",
                      "-Wall", "-Wextra", "-Werror", "-pedantic")

    # An inferred count can leave a generated lambda parameter unused; injected code must still pass -Werror.
    def test_inferred_array_with_warnings_as_errors(self):
        self.run_code("int main(){auto p=new int[]{1,2}; int n=p[1]; delete[] p; return n!=2;}",
                      "-Wall", "-Wextra", "-Werror", "-pedantic")

    # A signed enum index must preserve its sign when accessing a preceding valid element.
    def test_negative_enum_index(self):
        self.run_code("enum Offset{previous=-1}; int main(){int a[2]={3,4}; int* p=a+1; return p[previous]!=3;}")

    # Repeated template AST ranges must not duplicate or conflict with injected record/access wrappers.
    def test_template_record_and_function(self):
        self.run_code("template<class T> struct Box{T value;}; "
                      "template<class T> T read(T* p){return *p;} "
                      "int main(){Box<int> box{7}; return read(&box.value)!=7;}")

    # Register hosted external argv storage explicitly, including its terminating null slot.
    def test_main_arguments(self):
        self.run_code("int main(int argc,char** argv){return argc<1 || argv[0][0]=='\\0' || argv[argc]!=nullptr;}")

    # Clang byte ranges must remain correct for UTF-8 identifiers and non-ASCII filenames.
    def test_unicode_source_and_path(self):
        result, binary, source = self.compile("// Ελληνικά\nint main(){int τιμή=7; return *&τιμή!=7;}", name="δοκιμή🐶.cpp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.run([str(binary)]).returncode, 0)

    # The original destructor may legally read fields before the delete wrapper retires the allocation.
    def test_heap_destructor_runs_before_retirement(self):
        self.run_code("int result=0; struct S{int n=7; ~S(){result=this->n;}}; "
                      "int main(){auto p=new S; delete p; return result!=7;}")

    # Guard declaration order must keep automatic object metadata live through its destructor.
    def test_stack_destructor_runs_before_retirement(self):
        self.run_code("int result=0; struct S{int n=7; ~S(){result=this->n;}}; "
                      "int main(){{S s;} return result!=7;}")

    # Repeated static access must deduplicate registration and retain a lifetime beyond one call.
    def test_global_and_static_storage(self):
        self.run_code("int global=9; int read(){static int n=4; return *&n;} "
                      "int main(){return *&global!=9 || read()!=4 || read()!=4;}")

    # Locate the declaration's real semicolon rather than semicolons inside a lambda/string/comment.
    def test_multiple_global_declarators(self):
        self.run_code('int a=1, b=[](){const char* p=";"; return p[0]==\';\';}() /* ; */; '
                      'int main(){return *&a!=1 || *&b!=1;}')

    # Two callers binding the same static guard must publish the same deduplicated ID.
    # This narrow registry race regression does not claim general data-race safety.
    def test_concurrent_local_static_registration(self):
        self.run_code("#include <thread>\nint read(){static int n=7; return *&n;} "
                      "int main(){int first=0; int second=0; "
                      "std::thread a([&]{for(int i=0;i<100;++i) first=read();}); "
                      "std::thread b([&]{for(int i=0;i<100;++i) second=read();}); "
                      "a.join(); b.join(); return first!=7 || second!=7;}", "-pthread")

    # Compiler-owned literal storage needs explicit runtime exposure despite having no source declaration guard.
    def test_string_literals(self):
        self.run_code('int main(){const char* p="abc"; return p[1]!=\'b\';}')

    # The same wrapper must leave static_assert evaluation untouched while checking a runtime call.
    def test_constant_evaluation_and_runtime_call(self):
        self.run_code("constexpr int read(const int* p){return *p;} constexpr int n=7; "
                      "static_assert(read(&n)==7); int main(){int x=42; return read(&x)!=42;}")

    # sizeof/noexcept and &*p must not be turned into value reads of a null pointer.
    def test_unevaluated_expressions_and_address_of_dereference(self):
        self.run_code("int main(){int* p=nullptr; int* q=&*p; "
                      "return sizeof(*p)!=sizeof(int) || q!=nullptr || noexcept(*p)!=true;}")

    # The base pointer is evaluated before the index in C++17; p[(p=b,1)] must still use the old base.
    def test_index_evaluation_order(self):
        self.run_code("int main(){int a[2]={3,4}; int b[2]={5,6}; int* p=a; "
                      "int x=p[(p=b,1)]; return x!=4;}")

    # Per-source rewritten objects must link against one registry instance rather than isolated metadata copies.
    def test_separate_compilation_shares_registry(self):
        first = self.directory / "first.cpp"
        second = self.directory / "second.cpp"
        first.write_text("int read(void* p){return *static_cast<int*>(p);}")
        second.write_text("int read(void*); int main(){int x=42; return read(&x)!=42;}")
        for source in (first, second):
            compiled = subprocess.run([str(DRIVER), "--sc-backend", "ast", "--sc-quiet", "-c", source.name],
                                      cwd=self.directory, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
        binary = self.directory / "program"
        linked = subprocess.run([str(DRIVER), "--sc-backend", "ast", "--sc-quiet", str(self.directory / "first.o"),
                                 str(self.directory / "second.o"), "-o", str(binary)], capture_output=True, text=True)
        self.assertEqual(linked.returncode, 0, linked.stderr)
        self.assertEqual(subprocess.run([str(binary)]).returncode, 0)

    # Each temporary rewritten unit must resolve quoted includes beside its own original source.
    def test_multiple_sources_with_conflicting_quoted_header_names(self):
        inputs = []
        for directory, name, value in (("a", "first", 1), ("b", "second", 2)):
            folder = self.directory / directory
            folder.mkdir()
            (folder / "config.h").write_text(f"#define VALUE {value}\n")
            source = folder / "unit.cpp"
            source.write_text(f'#include "config.h"\nint {name}(){{return VALUE;}}')
            inputs.append(source)
        result, binary, _ = self.compile("int first(); int second(); int main(){return first()!=1 || second()!=2;}", extra=inputs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.run([str(binary)]).returncode, 0)

    # Unmodeled C allocation fails by default; only the explicit AST weakening option permits it.
    def test_unknown_storage_fails_closed(self):
        code = "#include <cstdlib>\nint main(){int* p=static_cast<int*>(std::malloc(sizeof(int))); *p=3; std::free(p);}"
        self.run_code(code, status=86, diagnostic="untracked-access")
        self.run_code(code, "--sc-allow-untracked")

    # Source bytes stay unchanged, generated copies are inspectable, and macro limitations remain
    # visible instead of being mistaken for native header/macro coverage.
    def test_source_unchanged_and_coverage_report(self):
        code = "#define READ(p) (*(p))\nint main(){int n=3; return READ(&n)!=3;}"
        report = self.directory / "report.json"
        emit = self.directory / "generated"
        result, binary, source = self.compile(code, "--sc-report", str(report), "--sc-emit-dir", str(emit))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(source.read_text(), code)
        data = json.loads(report.read_text())
        self.assertIn("main file only", data["translation_units"][0]["scope"])
        self.assertTrue(any("macro" in x["message"] for x in data["translation_units"][0]["limitations"]))
        self.assertTrue(list(emit.glob("*.strict.cpp")))

    # An original semantic error should report the user's filename before rewriting is attempted.
    def test_original_clang_diagnostics(self):
        result, _, source = self.compile("int main(){ missing_name; }")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing_name", result.stderr)
        self.assertIn(str(source), result.stderr)

    # Recorded macro ranges without safe source edits must prevent a coverage-required AST build.
    def test_require_main_coverage_rejects_macro_gap(self):
        result, _, _ = self.compile("#define READ(p) (*(p))\nint main(){int n=3; return READ(&n)!=3;}",
                                    "--sc-require-main-coverage")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncovered:", result.stderr)

    # A supported main-file-only example may pass the legacy coverage flag; headers remain outside its claim.
    def test_require_main_coverage_accepts_supported_program(self):
        self.run_code("int main(){int n=3; return *&n!=3;}", "--sc-require-main-coverage")

    # The AST templates require C++17 and must reject older standards with a clear driver error.
    def test_unsupported_standard_is_explicit(self):
        result, _, _ = self.compile("int main(){}", "-std=c++11")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires -std=c++17", result.stderr)


if __name__ == "__main__":
    unittest.main()
