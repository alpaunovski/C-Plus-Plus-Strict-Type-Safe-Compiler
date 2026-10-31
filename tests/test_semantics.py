# Source-semantic regressions cover distinctions ordinary LLVM IR/TBAA can erase.
# Helpers are borrowed without importing the NativeTests class as a local TestCase,
# which would make unittest discover and run that entire suite a second time.
"""Source distinctions and union lifetime regressions erased by ordinary IR."""
import unittest
import test_native as native


class SemanticTests(unittest.TestCase):
    setUp = native.NativeTests.setUp
    program = native.NativeTests.program

    # Reject signed-char alias reads before their i8/byte-TBAA lowering hides the source distinction.
    def test_signed_char_alias_read(self):
        for optimization in ('-O0', '-O2', '-O3'):
            with self.subTest(optimization=optimization):
                self.program('int main(){int n=0;return *reinterpret_cast<signed char*>(&n);}',
                             flags=(optimization,), expected=86, diagnostic='type-mismatch')

    # A precise source write hook must reject mutation through an unrelated signed-char view.
    def test_signed_char_alias_write(self):
        self.program('int main(){int n=0;*reinterpret_cast<signed char*>(&n)=1;}',
                     expected=86, diagnostic='type-mismatch')

    # Compound assignment must retain both source access type and old-value read requirements.
    def test_signed_char_alias_compound_write(self):
        self.program('int main(){int n=0;*reinterpret_cast<signed char*>(&n)+=1;}',
                     expected=86, diagnostic='type-mismatch')

    # Unary updates need the same precise source information as binary assignment.
    def test_signed_char_alias_increment(self):
        self.program('int main(){int n=0;++*reinterpret_cast<signed char*>(&n);}',
                     expected=86, diagnostic='type-mismatch')

    # An actual signed-char object remains subject to initialization checks despite byte-like IR.
    def test_signed_char_uninitialized_read(self):
        self.program('int main(){signed char n;return n;}', expected=86, diagnostic='uninitialized-read')

    # Read-modify-write must check initialization before recording the result as written.
    def test_signed_char_compound_operation_requires_initialization(self):
        self.program('int main(){signed char n;++n;}', expected=86, diagnostic='uninitialized-read')

    # The source fix must retain valid corresponding signed/unsigned character access.
    def test_signed_char_corresponding_unsigned_type(self):
        self.program('int main(){unsigned char n=7;return *reinterpret_cast<signed char*>(&n)!=7;}')

    # Plain char is a distinct object type; do not infer a signed-char access permission from its size.
    def test_signed_char_is_distinct_from_plain_char(self):
        self.program('int main(){char n=7;return *reinterpret_cast<signed char*>(&n);}',
                     expected=86, diagnostic='type-mismatch')

    # Typed constant branches and noexcept synthetic declarations must preserve compile-time traits.
    def test_signed_char_constexpr_and_noexcept(self):
        self.program('constexpr signed char f(){signed char n=7;return ++n;}'
                     'static_assert(f()==8);int main(){signed char n=7;static_assert(noexcept(++n));return f()!=8;}')

    # The wrapper must not call a side-effecting pointer-producing expression twice.
    def test_signed_char_evaluation_once(self):
        self.program('int calls; signed char n=7; signed char* get(){++calls;return &n;}'
                     'int main(){++*get();return calls!=1 || n!=8;}')

    # Keep original typed function pointers in the constexpr branch; a void-pointer round trip is not allowed there.
    def test_indirect_call_constant_evaluation(self):
        self.program('constexpr int f(int n){return n;}constexpr int g(int(*p)(int)){return p(7);}'
                     'static_assert(g(f)==7);int main(){return g(f)!=7;}')

    # Runtime downcast instrumentation must not invalidate a legal constant-evaluated hierarchy cast.
    def test_downcast_constant_evaluation(self):
        self.program('struct B{int n;};struct D:B{};constexpr int f(){D d{{7}};B* b=&d;return static_cast<D*>(b)->n;}'
                     'static_assert(f()==7);int main(){return f()!=7;}')

    # Supported simple assignment activates the nominated scalar member.
    def test_union_active_scalar(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=7;return u.i!=7;}')

    # Matching allocation bounds and shared address do not make another union member live.
    def test_union_inactive_scalar(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=0;return static_cast<int>(u.f);}',
                     expected=86, diagnostic='union-inactive-member')

    # Two int members need distinct member indexes; type/size comparisons alone cannot detect this violation.
    def test_union_same_type_members_have_distinct_lifetimes(self):
        self.program('union U{int a;int b;};int main(){U u;u.a=7;return u.b;}',
                     expected=86, diagnostic='union-inactive-member')

    # A supported scalar switch starts the new member and permits reading its newly written value.
    def test_union_scalar_switch(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=7;u.f=2;return u.f!=2;}')

    # C++17 assignment evaluates the RHS first: read the old member before the LHS activates the new one.
    def test_union_rhs_precedes_lifetime_switch(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=7;u.f=u.i;return u.f!=7;}')

    # Unlike simple assignment, += needs an already live old value and cannot activate an inactive member.
    def test_union_compound_assignment_does_not_start_lifetime(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=0;u.f+=1;}',
                     expected=86, diagnostic='union-inactive-member')

    # A member capability must support ordinary indirect access while its captured generation stays active.
    def test_union_pointer_to_active_member(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=7;int* p=&u.i;return *p!=7;}')

    # Taking an inactive address is not permission for an arbitrary pointer store to start its lifetime.
    def test_union_pointer_cannot_activate_inactive_member(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=7;float* p=&u.f;*p=2;}',
                     expected=86, diagnostic='union-inactive-member')

    # A pointer to the old member must fail after another member becomes active.
    def test_union_stale_pointer_after_switch(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=7;int* p=&u.i;u.f=2;return *p;}',
                     expected=86, diagnostic='union-inactive-member')

    # Switching back to the same type/address must not revive a capability from the earlier generation.
    def test_union_stale_pointer_after_switch_back(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=7;int* p=&u.i;u.f=2;u.i=9;return *p;}',
                     expected=86, diagnostic='expired-union-member')

    # Bypassing MemberExpr with a raw cast must still encounter the root's active-union layout check.
    def test_union_raw_cast_cannot_read_inactive_type(self):
        self.program('union U{int i;float f;};int main(){U u;u.i=0;return *reinterpret_cast<float*>(&u);}',
                     expected=86, diagnostic='union-inactive-member')

    # Initializer annotations must survive lowering to ordinary stores or copies from compiler constant data.
    def test_union_local_initializer(self):
        self.program('union U{int i;float f;};int main(){U u{7};return u.i!=7;}')

    # Global initialization mode preserves initialized bytes while selecting the correct member.
    def test_union_global_initializer(self):
        self.program('union U{int i;float f;};U u{7};int main(){return u.i!=7;}')

    # Wrapping the union base must evaluate a side-effecting get() call once.
    def test_union_evaluation_once(self):
        self.program('union U{int i;float f;};U u;int calls;U* get(){++calls;return &u;}'
                     'int main(){get()->i=7;return calls!=1 || u.i!=7;}')

    # Union state keys include the actual nested/element address, not only the root allocation.
    def test_union_in_record_and_array(self):
        self.program('union U{int i;float f;};struct S{int n;U u;};'
                     'int main(){S s;s.u.i=7;U a[2];a[1].f=2;return s.u.i!=7 || a[1].f!=2;}')

    # The constant branch must use the original typed member base without a forbidden void-pointer cast.
    def test_union_constexpr(self):
        self.program('union U{int i;float f;};constexpr int f(){U u{7};return u.i;}'
                     'static_assert(f()==7);int main(){return f()!=7;}')

    # A complete supported union copy carries the active member into the destination.
    def test_union_memcpy_preserves_active_member(self):
        self.program('#include <cstring>\nunion U{int i;float f;};'
                     'int main(){U a{7};U b;std::memcpy(&b,&a,sizeof a);return b.i!=7;}')

    # Copying representation bytes does not authorize reading a different inactive member.
    def test_union_memcpy_does_not_activate_another_member(self):
        self.program('#include <cstring>\nunion U{int i;float f;};'
                     'int main(){U a{0};U b;std::memcpy(&b,&a,sizeof a);return b.f;}',
                     expected=86, diagnostic='union-inactive-member')

    # The copy must carry both union member identity and the embedded pointer shadow.
    def test_union_memcpy_preserves_pointer_identity(self):
        self.program('#include <cstring>\nunion U{int* p;long n;};'
                     'int main(){int n=7;U a{&n};U b;std::memcpy(&b,&a,sizeof a);return *b.p!=7;}')

    # Compiler-generated trivial union copies need the same metadata transfer as explicit memcpy.
    def test_union_copy_constructor(self):
        self.program('union U{int i;float f;};int main(){U a{7};U b=a;return b.i!=7;}')

    # Assigning into the member already active preserves its lifetime and existing member pointers.
    def test_union_copy_assignment_preserves_existing_member_lifetime(self):
        self.program('union U{int i;float f;};int main(){U a{7};U b{1};int* p=&b.i;b=a;return *p!=7;}')

    # Pointer-object and explicit-destruction semantics remain deferred; coverage rejection must see
    # them even when all emitted plain loads/stores were instrumented.
    def test_coverage_rejects_unmodeled_source_operations(self):
        import subprocess
        for name, source in {
            'destruction': 'struct X{int n;~X(){}};int main(){X x{7};x.~X();return x.n;}',
            'pointer-storage-type': 'int main(){int n=7;int* p=&n;return *p!=7;}',
        }.items():
            with self.subTest(operation=name):
                main = self.folder / (name + '.cpp')
                main.write_text(source)
                result = subprocess.run([str(native.DRIVER), '--sc-quiet', '--sc-require-coverage',
                                         str(main), '-o', str(self.folder / name)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('deferred frontend sites', result.stderr)


if __name__ == '__main__': unittest.main()
