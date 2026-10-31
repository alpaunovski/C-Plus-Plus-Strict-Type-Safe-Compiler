# Strict-v1 tests protect both sides of the admission boundary: valid subset programs
# must compile, unknown source/configuration must fail before an executable is emitted,
# and admitted runtime violations must still trap for the intended reason.
"""The strict source/driver allowlist must reject every unmodeled case."""
import json
import subprocess
import unittest
from unittest.mock import patch

import test_native as native
from strictcpp import driver
from strictcpp.native_driver import compile_native


class StrictTests(unittest.TestCase):
    setUp = native.NativeTests.setUp

    # Reuse the native harness with strict mode always enabled. A compatibility success
    # must never accidentally stand in for an admitted strict-profile program.
    def program(self, source, *, flags=(), **kwargs):
        return native.NativeTests.program(self, source, flags=('--sc-strict', *flags), **kwargs)

    # Require compile rejection, absence of an executable and unchanged source bytes.
    # The diagnostic assertion distinguishes the expected policy/Clang failure from an unrelated tool error.
    def reject(self, source, *, flags=(), diagnostic='strict profile rejects'):
        path = self.folder / 'main.cpp'
        path.write_text(source)
        output = self.folder / 'program'
        result = subprocess.run([str(native.DRIVER), '--sc-quiet', '--sc-strict', *flags,
                                 str(path), '-o', str(output)], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(diagnostic, result.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(path.read_text(), source)
        return result

    # Taking/dereferencing a temporary address is admitted without permitting stored pointer objects.
    def test_scalar_and_temporary_pointer(self):
        self.program('int main(){int n=7;return *&n!=7;}')

    # Exercise the useful core subset: fixed arrays, unsigned loops, initialization, indexing and accumulation.
    def test_unsigned_array_loop(self):
        self.program('int main(){unsigned a[4]{};unsigned sum=0;'
                     'for(unsigned i=0;i<4u;++i){a[i]=i;sum+=a[i];}return sum!=6u;}')

    # Direct scalar calls use a supported ABI and must not be rejected as foreign or indirect interfaces.
    def test_direct_scalar_function(self):
        self.program('unsigned add(unsigned a,unsigned b){return a+b;}'
                     'int main(){return add(3u,4u)!=7u;}')

    # Internal static data is allowed without exposing a symbol that could interpose a trusted runtime function.
    def test_static_data_has_internal_linkage(self):
        self.program('static unsigned n=7;int main(){return n!=7u;}')

    # Checked temporary offsets and branch-selected identities must remain usable inside the subset.
    def test_pointer_arithmetic_and_selection(self):
        self.program('int main(){unsigned a[3]={3,7,9};unsigned n=7,m=9;bool take=true;'
                     'return *(a+1u)!=7u || *(take?&n:&m)!=7u;}')

    # Unsigned wraparound is defined source behavior; rejecting it would confuse safety with a signed-overflow rule.
    def test_unsigned_wrap_is_defined(self):
        self.program('int main(){unsigned n=~0u;n+=1u;return n!=0u;}')

    # Allow Clang-proven defined signed constant expressions without admitting arbitrary dynamic signed arithmetic.
    def test_proven_signed_constants(self):
        self.program('int main(){const int n=7;return n+1!=8;}')

    # Constant divisor/count proofs admit these dynamic unsigned operations without a zero/count-width hazard.
    def test_safe_division_and_shifts(self):
        self.program('int main(){unsigned n=16u;n/=2u;n<<=1u;return n!=16u;}')

    # Standalone mutations remain valid in loop bodies and control-statement substatements.
    def test_conditional_loop_bodies(self):
        self.program('int main(){unsigned n=0;while(n<7u){++n;if(n==2u)continue;if(n==5u)break;}'
                     'do{--n;}while(n>0u);return n!=0u;}')

    # A small supported user header is allowed; strict mode must not reduce to main-file-only checking.
    def test_header_definitions_are_validated(self):
        self.program('#include "value.h"\nint main(){return value()!=7u;}',
                     files={'value.h': 'inline unsigned value(){return 7u;}'})

    # Admission is not a promise of safe execution: every exposed optimization level must preserve
    # the runtime rejection of this admitted but out-of-bounds access.
    def test_bounds_trap_at_all_optimization_levels(self):
        for level in ('-O0', '-O1', '-O2', '-O3', '-Os', '-Oz'):
            with self.subTest(level=level):
                self.program('int main(){unsigned a[3]{};return a[3];}', flags=(level,),
                             expected=86, diagnostic='out-of-bounds')

    # A listed null conversion may compile, but dereferencing the result must still fail before the load.
    def test_null_access_traps(self):
        self.program('int main(){return *static_cast<unsigned*>(nullptr);}',
                     expected=86, diagnostic='null-access')

    # Source eligibility and dynamic initialization are separate: eligible scalar storage can still trap on read.
    def test_uninitialized_access_traps(self):
        self.program('int main(){int n;return n;}', expected=86, diagnostic='uninitialized-read')

    # Admitting fixed arrays does not grant an element pointer authority over adjacent matrix rows.
    def test_row_bounds_trap(self):
        self.program('int main(){unsigned a[2][3]{};return a[0][3];}',
                     expected=86, diagnostic='subobject-out-of-bounds')

    # The table mixes valid-but-unmodeled C++ with actual unsafe constructs and previous witnesses.
    # Both categories must reject under the explicit allowlist; rejection is the intended compatibility tradeoff.
    def test_unsupported_source_cases_are_rejected(self):
        cases = {
            'stored-pointer': 'int main(){int n=7;int* p=&n;return *p;}',
            'stored-reference': 'int main(){int n=7;int& r=n;return r;}',
            'record': 'struct X{int n;};int main(){X x{7};return x.n;}',
            'union': 'union U{int n;float f;};int main(){U u;u.n=7;return u.n;}',
            'enumeration': 'enum E{a=7};int main(){return a;}',
            'floating': 'int main(){double n=7;return n;}',
            'character': 'int main(){unsigned char n;return n;}',
            'signed-arithmetic': 'int add(int n){return n+1;}int main(){return add(7);}',
            'signed-increment': 'int main(){int n=7;++n;return n;}',
            'signed-negation': 'int neg(int n){return -n;}int main(){return neg(7);}',
            'dynamic-divisor': 'int main(){unsigned n=7;return 1u/n;}',
            'zero-divisor': 'int main(){unsigned n=7;return n/0u;}',
            'dynamic-shift': 'int main(){unsigned n=7;return 1u<<n;}',
            'nested-side-effects': 'int main(){unsigned n=0;return n++ + n++;}',
            'nested-increment': 'int main(){unsigned n=0;return n++;}',
            'nested-assignment': 'int main(){unsigned n=0;return (n=7);}',
            'external-data': 'unsigned n=7;int main(){return n!=7u;}',
            'thread-local': 'int main(){thread_local unsigned n=7;return n;}',
            'external-linkage': 'extern "C" unsigned f(unsigned);int main(){return f(7);}',
            'attributes': '[[noreturn]] void f(){for(;;){}}int main(){f();}',
            'builtin': 'int main(){__builtin_trap();}',
            'assembly': 'int main(){asm("");}',
            'exception': 'int main(){try{throw 1;}catch(int){return 0;}}',
            'goto': 'int main(){goto end;end:return 0;}',
            'allocation': 'int main(){return *new unsigned(7);}',
            'reserved-name': 'unsigned __sc_fake(unsigned n){return n;}int main(){return 0;}',
            'main-arguments': 'int main(int n,char** p){return n;}',
            'recursion': 'unsigned f(unsigned n){if(n==0u)return 0u;return f(n-1u);}int main(){return f(2u);}',
            'pointer-difference': 'int main(){unsigned a[2]{};return &a[1]-&a[0];}',
            'ordered-pointer-comparison': 'int main(){unsigned a=0,b=0;return &a < &b;}',
            'oversize-object': 'int main(){unsigned a[300]{};return 0;}',
            'oversize-frame': 'int main(){unsigned a[200]{};unsigned b[200]{};return 0;}',
            'pragma-linkage': '#pragma weak malloc\nint main(){}',
            'pragma-in-macro': '#define P _Pragma("weak malloc")\nP\nint main(){}',
            'pragma-filename-spoof': '#line 1 "<built-in>"\n#pragma weak malloc\nint main(){}',
            'ident-directive': '#ident "unchecked"\nint main(){}',
            'unevaluated-unknown': 'int main(){return sizeof(double);}',
            'explicit-destruction-witness': 'struct X{int n;~X(){}};int main(){X x{7};x.~X();return x.n;}',
            'pointer-type-witness': 'int main(){char n=0;char* p=&n;signed char* q=*reinterpret_cast<signed char**>(&p);return q==nullptr;}',
            'mutual-recursion': 'unsigned a(unsigned);unsigned b(unsigned n){return a(n);}unsigned a(unsigned n){return b(n);}int main(){return a(1u);}',
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                self.reject(source, diagnostic='unsequenced modifications' if name == 'nested-side-effects'
                            else 'strict profile rejects')

    # Include files receive the original-AST gate too, with their own source location in the diagnostic.
    def test_unknown_code_in_header_is_rejected(self):
        header = self.folder / 'value.h'
        header.write_text('inline int value(){double n=7;return n;}')
        result = self.reject('#include "value.h"\nint main(){return value();}')
        self.assertIn('value.h:1:', result.stderr)

    # A non-void fallthrough must not become unchecked undefined control flow in accepted strict code.
    def test_missing_return_is_an_error(self):
        self.reject('unsigned f(unsigned n){if(n)return n;}int main(){return f(7);}',
                    diagnostic='non-void function')

    # Unknown optimization, target, job, forwarding and macro-injection options cannot widen
    # the accepted source pipeline or inject code into the trusted compiler predefines buffer.
    def test_unknown_driver_configuration_is_rejected(self):
        for flags in (('-Ofast',), ('-std=c++20',), ('-c',), ('-S',), ('-E',),
                      ('-Xclang', '-disable-llvm-passes'), ('-Wa,--defsym,x=0',),
                      ('-mllvm', '-disable-verify'), ('-Wl,unchecked.o',), ('-target', 'x86_64-apple-macosx'),
                      ('-D', 'X=1\n#pragma weak malloc')):
            with self.subTest(flags=flags):
                self.reject('int main(){}', flags=flags, diagnostic='strict-v1 rejects')

    # Selecting the weaker source rewriter cannot satisfy the strict source admission contract.
    def test_ast_backend_is_not_a_strict_fallback(self):
        self.reject('int main(){}', flags=('--sc-backend', 'ast'), diagnostic='requires the native backend')

    # An explicit compatibility weakening option must be incompatible with strict enforcement.
    def test_untracked_storage_is_not_a_strict_fallback(self):
        self.reject('int main(){}', flags=('--sc-allow-untracked',), diagnostic='cannot permit untracked storage')

    # Reject artifact paths before a JSON write or compiler output can replace the source.
    def test_output_and_report_cannot_replace_source(self):
        source = self.folder / 'main.cpp'
        contents = 'int main(){}'
        source.write_text(contents)
        self.reject(contents, flags=('--sc-report', str(source)), diagnostic='must be distinct')

    # The driver appends mandatory return/sequencing diagnostics after user warning controls.
    def test_user_warning_flags_cannot_disable_missing_return_rejection(self):
        self.reject('unsigned f(unsigned n){if(n)return n;}int main(){return f(7);}',
                    flags=('-Wno-return-type', '-Wno-error=return-type'), diagnostic='non-void function')

    # Reports must identify the applied profile and final status; a source rejection is not
    # reported as an accepted zero-gap translation unit.
    def test_accepted_and_rejected_reports_identify_policy(self):
        report = self.folder / 'coverage.json'
        self.program('int main(){int n=7;return *&n!=7;}', flags=('--sc-report', str(report)))
        data = json.loads(report.read_text())
        self.assertEqual(data['mode'], 'strict')
        self.assertEqual(data['status'], 'accepted')
        self.assertEqual(data['translation_units'][0]['source_policy'], 'strict-v1')
        self.assertEqual(data['translation_units'][0]['counts']['known_gaps'], 0)
        # Remove the prior output so rejection must not emit another executable.
        (self.folder / 'program').unlink()
        self.reject('int main(){double n=7;return n;}', flags=('--sc-report', str(report)))
        data = json.loads(report.read_text())
        self.assertEqual(data['status'], 'rejected')
        self.assertEqual(data['profile'], 'strict-v1')

    # Even valid checked source cannot link if the original-AST gate acknowledgment is absent.
    def test_strict_gate_acknowledgment_is_required(self):
        source = self.folder / 'main.cpp'
        source.write_text('int main(){}')
        output = self.folder / 'program'
        options, args = driver.parse(['--sc-strict', '--sc-quiet', str(source), '-o', str(output)])
        real_execute = driver.execute

        # Remove only the successful gate marker while retaining real compilation output. The
        # failure must arise from missing admission evidence rather than a broken compiler subprocess.
        def strip_acknowledgment(command, **kwargs):
            result = real_execute(command, **kwargs)
            if isinstance(result.stderr, str):
                result.stderr = ''.join(line for line in result.stderr.splitlines(keepends=True)
                                        if not line.startswith('strictcpp-strict:'))
            return result

        with patch('strictcpp.driver.execute', side_effect=strip_acknowledgment):
            with self.assertRaisesRegex(driver.DriverError, 'strict source-gate report'):
                compile_native(options, args, self.folder / 'temporary')
        self.assertFalse(output.exists())


if __name__ == '__main__': unittest.main()
