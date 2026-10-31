# Native end-to-end regressions compile original C++ into real instrumented executables.
# Fixture strings are intentionally kept byte-for-byte stable: their file/line diagnostics
# and source-preservation assertions are part of the compiler behavior under test.
"""Native backend regressions. Exercise real generated IR and runtime behavior."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "bin/strict++"


class NativeTests(unittest.TestCase):
    # Isolate every test's source, executable and reports. Cleanup is registered immediately
    # so even a failed compiler assertion does not leave test inputs in the workspace.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="strictcpp-native-test-")
        self.folder = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    # Compile with the native default backend, assert that the source bytes are untouched,
    # then execute with a bounded timeout. Negative cases must fail for the intended diagnostic,
    # not merely return some nonzero status.
    def program(self, source, *, expected=0, diagnostic=None, flags=(), files=None):
        main = self.folder / "main.cpp"
        main.write_text(source)
        before = main.read_bytes()
        for name, contents in (files or {}).items():
            target = self.folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
        binary = self.folder / "program"
        result = subprocess.run([str(DRIVER), "--sc-quiet", *flags, str(main), "-o", str(binary)],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(main.read_bytes(), before)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        if diagnostic:
            self.assertIn("strictcpp: " + diagnostic, result.stderr)
        return result

    # A valid void-pointer round trip must preserve the original object ID and access type.
    def test_void_roundtrip(self):
        self.program("int read(void* p){return *static_cast<int*>(p);} int main(){int n=7; return read(&n)!=7;}")

    # Hosted argv is external entry storage: its strings and null sentinel must be registered
    # explicitly rather than accepted by a general unknown-pointer escape hatch.
    def test_main_arguments(self):
        self.program("int main(int argc,char** argv){return argc<1 || argv[0][0]=='\\0' || argv[argc]!=0;}")

    # The checking runtime is compiled independently as C++17. Older user source standards
    # therefore remain compatibility cases even though strict-v1 itself admits only C++17.
    def test_legacy_language_standards(self):
        for standard in ('c++98','c++11','c++14'):
            with self.subTest(standard=standard):
                self.program("int read(void* p){return *static_cast<int*>(p);}int main(){int n=7;return read(&n)!=7;}",flags=('-std='+standard,))

    # Runtime build flags must not leak requirements into user code; native metadata uses
    # compiler debug/type information rather than user RTTI-enabled wrappers.
    def test_no_rtti_or_exceptions_required_in_user_source(self):
        self.program("int main(){int n=7;return *&n!=7;}",flags=('-fno-rtti','-fno-exceptions'))

    # The check must survive every exposed optimization level and run before a wrong-type
    # load can exploit aliasing assumptions or disappear during ordinary optimization.
    def test_wrong_read_at_all_optimization_levels(self):
        for level in ("-O0", "-O1", "-O2", "-O3", "-Os", "-Oz"):
            with self.subTest(level=level):
                self.program("int main(){int n=7; return *reinterpret_cast<float*>(&n);}",
                             flags=(level,), expected=86, diagnostic="type-mismatch")

    # A store through a wrong access type must be rejected before corrupting the int.
    def test_wrong_write(self):
        self.program("int main(){int n=7; *reinterpret_cast<float*>(&n)=2.0f;}",
                     expected=86, diagnostic="type-mismatch")

    # Precise signed-char access hooks must still permit ordinary access to actual signed-char
    # storage; fixing byte-alias holes must not reject this basic valid program.
    def test_valid_signed_char_pointer(self):
        self.program("int main(){signed char n=7;signed char* p=&n;return *p!=7;}")

    # ABI-lowered aggregate returns must retain the embedded pointer's allocation identity.
    def test_aggregate_pointer_return_for_live_object(self):
        self.program("struct Pair{int* p;long n;};Pair f(){return {new int(7),0};}int main(){Pair r=f();int n=*r.p;delete r.p;return n!=7;}")

    # Force address reuse deterministically with replacement new. An aggregate return must
    # carry the old generation independently from a later allocation's equal pointer bits.
    def test_aggregate_return_keeps_expired_identity_after_address_reuse(self):
        self.program("#include <new>\n#include <cstdlib>\n"
                     "alignas(int) unsigned char storage[sizeof(int)];"
                     "void* operator new(std::size_t n){if(n==sizeof(int))return storage;return std::malloc(n);}"
                     "void operator delete(void* p) noexcept{if(p!=storage)std::free(p);}"
                     "struct Pair{int* p;long n;};Pair f(){return {new int(7),0};}"
                     "int main(){Pair r=f();delete r.p;int* q=new int(9);return *r.p;}",
                     expected=86,diagnostic="expired-object")

    # Static pointer shadows need lazy root resolution without weakening ordinary runtime loads.
    def test_global_pointer_initializer(self):
        self.program("int n=7;int* p=&n;int main(){return *p!=7;}")

    # A pointer-width integer passed through a scalar ABI slot must transport its pointer ID.
    def test_integer_provenance_through_function(self):
        self.program("#include <cstdint>\nstd::uintptr_t f(std::uintptr_t p){return p;}int main(){int n=7;return *reinterpret_cast<int*>(f(reinterpret_cast<std::uintptr_t>(&n)))!=7;}")

    # Native coverage follows emitted IR, so a macro expansion cannot hide a wrong-type load.
    def test_macro(self):
        self.program("#define READ(p) (*(p))\nint main(){int n=7;return READ(reinterpret_cast<float*>(&n));}",
                     expected=86, diagnostic="type-mismatch")

    # The native pass must cover a header-defined access and preserve its header source location.
    def test_header(self):
        result = self.program('#include "read.h"\nint main(){int n=7;return read(&n);}',
                     files={"read.h": "inline int read(void* p){return *static_cast<float*>(p);}"},
                     expected=86, diagnostic="type-mismatch")
        self.assertIn("read.h:1:", result.stderr)

    # Concrete template instantiations emitted from headers must carry the same checks as
    # ordinary source definitions, even though the archived AST backend cannot cover them.
    def test_template_header(self):
        self.program('#include "read.h"\nint main(){int n=7;return read<float>(&n);}',
                     files={"read.h": "template<class T> int read(void* p){return *static_cast<T*>(p);}"},
                     expected=86, diagnostic="type-mismatch")

    # Const initialization is allowed, and only the mutable field gets a writable view afterwards.
    def test_const_and_mutable_initialization(self):
        self.program("struct S{const int x=7; mutable int y=3;}; int main(){const S s; s.y=4; return s.x!=7 || s.y!=4;}")

    # Casting away const does not authorize a later write to initialized const storage.
    def test_const_write(self):
        self.program("int main(){const int n=7; int* p=const_cast<int*>(&n); *p=2;}",
                     expected=86, diagnostic="write-to-const")

    # The checker must diagnose address zero before the actual machine load.
    def test_null(self):
        self.program("int main(){int* p=nullptr; return *p;}", expected=86, diagnostic="null-access")

    # A byte buffer at an offset may be in bounds but still fail the requested type's alignment.
    def test_alignment(self):
        self.program("int main(){alignas(int) unsigned char b[8]{}; return *reinterpret_cast<int*>(b+1);}",
                     expected=86, diagnostic="misaligned-access")

    # A one-past address may be formed, but loading a complete element there must fail.
    def test_bounds(self):
        self.program("int main(){int a[3]{};int* p=a;return p[3];}", expected=86, diagnostic="out-of-bounds")

    # An element pointer into one matrix row must not gain authority over the next row merely
    # because the complete matrix allocation still contains that address.
    def test_row_bounds(self):
        self.program("int main(){int a[2][3]{};int* p=a[0];return p[3];}",
                     expected=86, diagnostic="subobject-out-of-bounds")

    # Multiplying a huge index by element size must reach the overflow check before GEP poison.
    def test_index_multiplication_overflow(self):
        self.program("int main(){int a[2]{};unsigned long long n=1ULL<<62;return a[n];}",
                     expected=86,diagnostic="index-overflow")

    # A mismatched scalar machine signature must fail before indirect invocation.
    def test_indirect_function_signature(self):
        self.program("int f(int){return 0;}int main(){auto p=reinterpret_cast<int(*)(double)>(&f);return p(1.0);}",
                     expected=86,diagnostic="call-type-mismatch")

    # Signature preservation must allow an ordinary compatible indirect free-function call.
    def test_valid_indirect_function(self):
        self.program("int f(int x){return x;}int main(){int(*p)(int)=f;return p(7)!=7;}")

    # A* and B* lower to opaque LLVM pointers. The preserved source signature must reject
    # the incompatible call even when the lowered IR function types look identical.
    def test_function_pointer_pointee_types(self):
        self.program("struct A{};struct B{};int f(A*){return 0;}int main(){B b;auto p=reinterpret_cast<int(*)(B*)>(&f);return p(&b);}",
                     expected=86,diagnostic="call-type-mismatch")

    # Dropping a compatible noexcept specification in a function-pointer conversion is valid;
    # normalizing signatures must not turn it into a false call-type mismatch.
    def test_noexcept_function_pointer_conversion(self):
        self.program("int f(int x) noexcept{return x;}int main(){int(*p)(int)=f;return p(7)!=7;}")

    # Synthetic hooks must be declared noexcept so source noexcept queries and templates
    # continue to observe the original expression's exception behavior.
    def test_noexcept_traits_remain_unchanged(self):
        self.program("int f(int x)noexcept{return x;}template<class F>constexpr bool safe(F fn){return noexcept(fn(7));}"
                     "static_assert(safe(&f));int main(){int(*p)(int)noexcept=f;static_assert(noexcept(p(7)));return p(7)!=7;}")

    # Ordinary indexing from an interior pointer retains the containing array authority;
    # not every GEP should narrow to a single element.
    def test_interior_array_pointer(self):
        self.program("int main(){int a[3]={1,2,3}; int* p=&a[1];return p[1]!=3;}")

    # Debug layout must expose legitimate private/nested storage without requiring public fields.
    def test_private_nested_subobjects(self):
        self.program("class S{int n=7; public:int* ptr(){return &n;}};int main(){S s;return *s.ptr()!=7;}")

    # A nonzero base adjustment must preserve the correct subobject layout and access ID.
    def test_inheritance(self):
        self.program("struct A{int x=1;};struct B{int y=7;};struct D:A,B{};int main(){D d;B* b=&d;return b->y!=7;}")

    # Exercise one supported virtual dispatch/deletion path with optimization enabled.
    # This is a regression example, not exhaustive support for the C++ virtual object model.
    def test_virtual_dispatch_and_deletion(self):
        self.program("struct B{virtual ~B(){} virtual int value()const{return 3;}};"
                     "struct D:B{int n=7;int value()const override{return n;}};"
                     "int main(){B* p=new D;int n=p->value();delete p;return n!=7;}",flags=('-O2',))

    # Final int-field compatibility is insufficient: the enclosing unrelated record identity
    # must also match the access path.
    def test_same_layout_wrong_record(self):
        self.program("struct A{int x;};struct B{int x;};int main(){A a{7};B* b=reinterpret_cast<B*>(&a);return b->x;}",
                     expected=86,diagnostic="type-mismatch")

    # A base object is not a derived object merely because the inherited field has the same
    # layout. Preserve source downcast semantics before lowering erases that relationship.
    def test_invalid_downcast_same_field_layout(self):
        self.program("struct B{int x;};struct D:B{int y;};int main(){B b{7};D* d=static_cast<D*>(&b);return d->x;}",
                     expected=86,diagnostic="type-mismatch")

    # A real derived root must pass a downcast from a secondary base and grant the complete
    # derived extent, including fields outside the original base capability.
    def test_valid_downcast_nonzero_base(self):
        self.program("struct A{int a=1;};struct B{int b=7;};struct D:A,B{int c=3;};int main(){D d;B* b=&d;D* p=static_cast<D*>(b);return p->b!=7 || p->c!=3;}")

    # Null pointer downcasts remain null and must not attempt root-object layout resolution.
    def test_null_downcast(self):
        self.program("struct B{int x;};struct D:B{int y;};int main(){B* p=nullptr;return static_cast<D*>(p)!=nullptr;}")

    # The saved address survives as bits, but lexical lifetime.end must retire its object ID.
    def test_automatic_scope_expiration(self):
        self.program("int main(){int* p;{int n=7;p=&n;}return *p;}",expected=86,diagnostic="expired-object")

    # Returning a pointer cannot extend the callee's automatic storage lifetime.
    def test_returned_stack_pointer(self):
        self.program("int* f(){int n=7;return &n;}int main(){return *f();}",expected=86,diagnostic="expired-object")

    # Frame cleanup must retire automatic storage without retiring an independent returned heap object.
    def test_returned_heap_pointer(self):
        self.program("int* f(){return new int(7);}int main(){int* p=f();int n=*p;delete p;return n!=7;}")

    # The array bound's side effect occurs once and the resulting allocation covers every element.
    def test_heap_array(self):
        self.program("int main(){int n=3;int* p=new int[n++]{};p[2]=7;int r=(p[2]!=7 || n!=4);delete[]p;return r;}")

    # A saved heap pointer keeps its expired ID after deletion; address lookup must not repair it.
    def test_deleted_heap(self):
        self.program("int main(){int* p=new int(7);delete p;return *p;}",expected=86,diagnostic="expired-object")

    # Array-new storage cannot be released through scalar delete.
    def test_delete_kind_mismatch(self):
        self.program("int main(){int* p=new int[2]{};delete p;}",expected=86,diagnostic="delete-kind-mismatch")

    # C allocations and C++ scalar new have distinct deallocation contracts.
    def test_malloc_delete_mismatch(self):
        self.program("#include <cstdlib>\nint main(){auto p=static_cast<int*>(std::malloc(4));delete p;}",expected=86,diagnostic="delete-kind-mismatch")

    # Runtime metadata allocation must bypass user replacement new/delete, while an instrumented
    # user allocator/deallocator may legitimately delegate to malloc/free.
    def test_replaced_new_and_delete_using_malloc(self):
        self.program("#include <new>\n#include <cstdlib>\nvoid* operator new(std::size_t n){return std::malloc(n);}"
                     "void operator delete(void* p) noexcept{std::free(p);}int main(){int* p=new int(7);int n=*p;delete p;return n!=7;}")

    # A reference parameter does not make a null source pointer valid. Optimized code must
    # still reach the null check when the referred-to value is actually loaded.
    def test_null_reference_is_checked(self):
        self.program("int f(int& r){return r;}int main(){int* p=nullptr;return f(*p);}",flags=('-O3',),expected=86,diagnostic="null-access")

    # Selected C allocation stores establish usable typed-slot metadata before later reads.
    def test_c_allocator(self):
        self.program("#include <cstdlib>\nint main(){int* p=static_cast<int*>(std::malloc(sizeof(int)));*p=7;int n=*p;std::free(p);return n!=7;}")

    # Zero-filled C allocation begins initialized, so a value read need not wait for a store.
    def test_calloc(self):
        self.program("#include <cstdlib>\nint main(){int* p=static_cast<int*>(std::calloc(2,sizeof(int)));int n=p[1];std::free(p);return n;}")

    # Successful growth must retain initialization/type metadata for the preserved prefix.
    def test_realloc_preserves_values(self):
        self.program("#include <cstdlib>\nint main(){auto p=static_cast<int*>(std::malloc(4));*p=7;p=static_cast<int*>(std::realloc(p,8));int n=*p;std::free(p);return n!=7;}")

    # Reject the unmodeled platform-dependent realloc(p,0) contract instead of silently
    # assuming whether the original allocation was freed.
    def test_zero_realloc_is_explicitly_rejected(self):
        self.program("#include <cstdlib>\nint main(){void* p=std::malloc(4);p=std::realloc(p,0);}",expected=86,diagnostic="unsupported-zero-realloc")

    # Even an in-place successful realloc creates a fresh identity; previously saved aliases
    # must not be revived by the result's equal address.
    def test_realloc_expires_old_pointer(self):
        self.program("#include <cstdlib>\nint main(){auto p=static_cast<int*>(std::malloc(4));*p=7;int* stale=p;p=static_cast<int*>(std::realloc(p,8));return *stale;}",
                     expected=86,diagnostic="expired-object")

    # Copying pointer bytes must also copy the shadow ID when the complete slot is transferred.
    def test_memcpy_pointer_provenance(self):
        self.program("#include <cstring>\nint main(){int n=7;int* p=&n;int* q;std::memcpy(&q,&p,sizeof(q));return *q!=7;}")

    # A byte copy transports the expired generation, not a new live-address lookup.
    def test_memcpy_does_not_recover_expired_pointer(self):
        self.program("#include <cstring>\nint main(){int* p;{int n=7;p=&n;}int* q;std::memcpy(&q,&p,sizeof(q));return *q;}",
                     expected=86,diagnostic="expired-object")

    # Selected pointer-width integer casts preserve authority through an ordinary round trip.
    def test_pointer_integer_roundtrip(self):
        self.program("#include <cstdint>\nint main(){int n=7;std::uintptr_t bits=reinterpret_cast<std::uintptr_t>(&n);int* p=reinterpret_cast<int*>(bits);return *p!=7;}")

    # The same round trip must not erase expiration when the pointed-to scope has ended.
    def test_expired_integer_roundtrip(self):
        self.program("#include <cstdint>\nint main(){std::uintptr_t bits;{int n=7;bits=reinterpret_cast<std::uintptr_t>(&n);}return *reinterpret_cast<int*>(bits);}",
                     expected=86,diagnostic="expired-object")

    # Initialization is checked before the actual LLVM value load can become undefined data.
    def test_uninitialized(self):
        self.program("int main(){int n;return *&n;}",expected=86,diagnostic="uninitialized-read")

    # Loop/control declarations must register the right activation and remain usable while in scope.
    def test_control_initializer(self):
        self.program("int main(){int sum=0;for(int i=0;i<3;++i){int* p=&i;sum+=*p;}return sum!=3;}")

    # Constant evaluation and runtime execution of the same constexpr function follow different
    # paths; native runtime storage checks must not invalidate its static_assert use.
    def test_constexpr_local_storage_runtime(self):
        self.program("constexpr int f(){int a[2]={3,7};return *&a[1];}static_assert(f()==7);int main(){return f()!=7;}")

    # A supported standard vector path exercises template/header IR, placement hooks and C++ allocation.
    def test_standard_vector(self):
        self.program("#include <vector>\nint main(){std::vector<int> v{1,2,3};v.push_back(4);return v[1]!=2 || v.back()!=4;}")

    # Vector reallocation must retire the old buffer identity even when a source reference remains.
    def test_vector_stale_reference(self):
        self.program("#include <vector>\nint main(){std::vector<int> v{1};int& r=v[0];v.reserve(100);return r;}",
                     expected=86,diagnostic="expired-object")

    # Supported catch control flow must preserve a live caller-owned pointer through the exception runtime.
    def test_exception_control_flow(self):
        self.program("int f(int* p){try{throw 1;}catch(int){return *p;}}int main(){int n=7;return f(&n)!=7;}")

    # Concrete scalar placement construction establishes a typed child in byte backing storage.
    def test_placement_scalar(self):
        self.program("#include <new>\nint main(){alignas(int) unsigned char s[sizeof(int)];int* p=new(s) int(7);return *p!=7;}")

    # Concrete record placement must carry recursive field layout into the new child lifetime.
    def test_placement_record(self):
        self.program("#include <new>\nstruct S{int x;double y;};int main(){alignas(S) unsigned char b[sizeof(S)];S* p=new(b) S{7,2};return p->x!=7;}")

    # Supported same-type non-const replacement forwards old identity to the replacement lifetime.
    def test_transparent_replacement(self):
        self.program("#include <new>\nint main(){alignas(int) unsigned char s[sizeof(int)];int* p=new(s) int(7);int* q=new(s) int(9);return *p!=9 || *q!=9;}")

    # Different-type placement replacement must expire the old identity instead of forwarding it.
    def test_reused_address_different_object(self):
        self.program("#include <new>\nint main(){alignas(double) unsigned char s[sizeof(double)];int* p=new(s) int(7);float* q=new(s) float(2);return *p;}",
                     expected=86,diagnostic="expired-object")

    # A catch block must not see objects from an abandoned child frame as still alive.
    def test_same_thread_exception_unwinds_objects(self):
        self.program("int* p;void f(){int n=7;p=&n;throw 1;}int main(){try{f();}catch(int){}return *p;}",
                     expected=86,diagnostic="expired-object")

    # Independently instrumented objects must share one runtime registry at final link.
    # This compatibility case is deliberately outside strict-v1's source-to-executable policy.
    def test_separate_compilation(self):
        a=self.folder/'a.cpp';b=self.folder/'b.cpp';binary=self.folder/'program'
        a.write_text("int f(void* p){return *static_cast<int*>(p);}")
        b.write_text("int f(void*);int main(){int n=7;return f(&n)!=7;}")
        for source in (a,b):
            r=subprocess.run([str(DRIVER),'--sc-quiet','-c',source.name],cwd=self.folder,capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
        r=subprocess.run([str(DRIVER),'--sc-quiet',str(self.folder/'a.o'),str(self.folder/'b.o'),'-o',str(binary)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(subprocess.run([str(binary)]).returncode,0)

    # Persisted IR and JSON evidence must describe the actual native pipeline selected by default.
    def test_report_and_bitcode(self):
        report=self.folder/'report.json'; emit=self.folder/'ir'
        self.program("int main(){int n=7;return *&n!=7;}",flags=('--sc-report',str(report),'--sc-emit-dir',str(emit)))
        data=json.loads(report.read_text())
        self.assertEqual(data['backend'],'llvm-native')
        self.assertTrue(list(emit.glob('*.strict.bc')))

    # Normalize joined options and emit dependencies for the public object target rather than
    # temporary bitcode, retaining the original source/header paths.
    def test_joined_output_and_make_dependencies(self):
        source=self.folder/'main.cpp';header=self.folder/'value.h';out=self.folder/'main.o';dep=self.folder/'main.d'
        source.write_text('#include "value.h"\nint f(){return VALUE;}')
        header.write_text('#define VALUE 7\n')
        result=subprocess.run([str(DRIVER),'--sc-quiet','-c',str(source),'-o'+str(out),'-MMD','-MF'+str(dep)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(out.is_file())
        contents=dep.read_text()
        self.assertIn(str(source),contents)
        self.assertIn(str(header),contents)
        self.assertNotIn('unit-0.bc',contents)

    # A mistakenly selected output path must be rejected before user source bytes are replaced.
    def test_output_cannot_overwrite_source(self):
        source=self.folder/'main.cpp';contents='int main(){}';source.write_text(contents)
        result=subprocess.run([str(DRIVER),'--sc-quiet',str(source),'-o',str(source)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('overwrite',result.stderr)
        self.assertEqual(source.read_text(),contents)

    # Coverage-only policy accepts this zero-gap example; it is not a proof that every zero-gap
    # program is safe or equivalent to the separate strict source admission gate.
    def test_require_coverage_accepts_supported_closed_program(self):
        self.program("int main(){int n=7;return *&n!=7;}",flags=('--sc-require-coverage',))

    # The coverage-only rejection policy must list and reject an unmodeled foreign symbol,
    # even when ordinary compatibility compilation would link it successfully.
    def test_foreign_calls_are_explicit_and_strict_mode_rejects_them(self):
        source=self.folder/'main.cpp';report=self.folder/'report.json'
        source.write_text('#include <cstdio>\nint main(){std::puts("hello");}')
        result=subprocess.run([str(DRIVER),'--sc-quiet','--sc-require-coverage','--sc-report',str(report),str(source),'-o',str(self.folder/'program')],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('foreign call targets',result.stderr)
        data=json.loads(report.read_text())
        self.assertIn('puts',data['translation_units'][0]['foreign_symbols'])

    # Instrumenting allocator body accesses does not validate its returned extent. Keep that
    # trusted contract visible as a gap so coverage-required builds cannot silently accept it.
    def test_replacement_allocator_contract_is_reported_as_gap(self):
        source=self.folder/'main.cpp';report=self.folder/'report.json'
        source.write_text('#include <new>\n#include <cstdlib>\nvoid* operator new(std::size_t n){return std::malloc(n);}int main(){return 0;}')
        result=subprocess.run([str(DRIVER),'--sc-quiet','--sc-require-coverage','--sc-report',str(report),str(source),'-o',str(self.folder/'program')],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('incomplete coverage',result.stderr)
        self.assertGreater(json.loads(report.read_text())['translation_units'][0]['counts']['known_gaps'],0)


if __name__ == '__main__': unittest.main()
