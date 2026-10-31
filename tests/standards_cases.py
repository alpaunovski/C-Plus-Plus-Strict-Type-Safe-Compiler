"""Curated standards/edge fixtures for the standalone differential audit.

Minimum years describe the source language needed by a fixture, not a claim of
complete conformance. Native and archived AST modes are compared with their own
actual Clang compiler. Undefined-behavior controls are compiled, never executed.
"""
from dataclasses import dataclass
import json


ARMED = 'strictcpp-matrix-armed\n'
COMPATIBILITY = ('native', 'ast')


@dataclass(frozen=True)
class Case:
    # An explicit contract prevents arbitrary nonzero exits from counting as a
    # successful safety check. known profiles remain non-passing audit results.
    name: str
    source: str
    category: str
    since: int = 1998
    expectation: str = 'valid'
    diagnostics: tuple[str, ...] = ()
    profiles: tuple[str, ...] = COMPATIBILITY
    known: tuple[str, ...] = ()
    files: tuple[tuple[str, str], ...] = ()
    filename: str = 'main.cpp'
    armed: bool = False


def valid(name, body, *, declarations='', category='core', **kwargs):
    """Build a defined positive program that must return zero in its control."""
    return Case(name, declarations + '\nint main(){' + body + '}\n', category, **kwargs)


def violation(name, setup, action, diagnostics, *, declarations='', category='safety', **kwargs):
    """Arm immediately before the invalid action using a small POSIX write.

    Seeing this marker distinguishes a check of the intended bad action from a
    false positive during otherwise valid setup. It is a test-harness foreign
    boundary, so these sources are not strict-admission candidates.
    """
    preamble = 'extern "C" long write(int,const void*,unsigned long);\n'
    marker = 'write(2,' + json.dumps(ARMED) + ',' + str(len(ARMED.encode())) + 'UL);'
    source = preamble + declarations + '\nint main(){' + setup + marker + action + '}\n'
    if isinstance(diagnostics, str):
        diagnostics = (diagnostics,)
    return Case(name, source, category, expectation='violation', diagnostics=diagnostics,
                armed=True, **kwargs)


# These fixtures deliberately avoid post-C++98 syntax. The same program can
# therefore be exercised under every canonical ISO/GNU mode without relying on
# the compiler accepting a newer-language extension as a false conformance pass.
CORE = [
    valid('scalar-address', 'int n=7;return *&n!=7;'),
    valid('void-roundtrip', 'int n=7;return read(&n)!=7;', declarations='int read(void* p){return *static_cast<int*>(p);}'),
    valid('corresponding-unsigned-alias', 'int n=7;return *reinterpret_cast<unsigned int*>(&n)!=7u;'),
    valid('unsigned-byte-view', 'int n=1;return reinterpret_cast<unsigned char*>(&n)[0]!=1;'),
    valid('signed-character-object', 'signed char n=7;return *&n!=7;'),
    valid('wide-string-literal', 'const wchar_t* p=L"az";return p[1]!=L\'z\';', category='character-types'),
    valid('negative-interior-index', 'int a[2]={7,9};int* p=a+1;return p[-1]!=7;'),
    valid('one-past-formation', 'int a[2]={7,9};int* p=a+2;return p!=&a[2];'),
    valid('private-subobject', 'S s;return *s.get()!=7;', declarations='class S{int n;public:S():n(7){}int* get(){return &n;}};'),
    valid('secondary-base', 'D d;B* p=&d;return p->b!=7;', declarations='struct A{int a;A():a(1){}};struct B{int b;B():b(7){}};struct D:A,B{};'),
    valid('scalar-new-delete', 'int* p=new int(7);int n=*p;delete p;return n!=7;'),
    valid('zero-array-delete', 'int* p=new int[0];delete[] p;return 0;'),
    valid('destructor-access', '{X x;}return result!=7;', declarations='static int result=0;struct X{int n;X():n(7){}~X(){result=this->n;}};'),
    valid('local-static', 'return f()!=7||f()!=7;', declarations='int f(){static int n=7;return *&n;}'),
    valid('pointer-select-call', 'int a=7,b=9;return *pick(true,&a,&b)!=7||*pick(false,&a,&b)!=9;', declarations='int* pick(bool b,int* x,int* y){return b?x:y;}'),
    valid('aggregate-two-pointer-return', 'int a=7,b=9;Pair r=make(&a,&b);return *r.a!=7||*r.b!=9;', declarations='struct Pair{int* a;int* b;};Pair make(int* a,int* b){Pair r={a,b};return r;}'),
    valid('overlapping-pointer-memmove', 'int a=7,b=9;int* p[2]={&a,&b};std::memmove(p+1,p,sizeof(int*));return *p[1]!=7;', declarations='#include <cstring>'),
    valid('unicode-identifier-path', 'int τιμή=7;return *&τιμή!=7;', filename='δοκιμή with spaces 🐶.cpp'),
    valid('c-allocation', 'int* p=static_cast<int*>(std::malloc(sizeof(int)));if(!p)return 1;*p=7;int n=*p;std::free(p);return n!=7;', declarations='#include <cstdlib>', known=('ast',)),
    violation('wrong-type-read', 'int n=0;', 'return *reinterpret_cast<float*>(&n);', 'type-mismatch'),
    violation('wrong-type-store', 'int n=0;', '*reinterpret_cast<float*>(&n)=1;return 0;', 'type-mismatch'),
    violation('null-read', 'int* p=0;', 'return *p;', 'null-access'),
    violation('misaligned-read', 'int a[2]={0,0};unsigned char* p=reinterpret_cast<unsigned char*>(a);', 'return *reinterpret_cast<int*>(p+1);', 'misaligned-access'),
    violation('one-past-read', 'int a[2]={7,9};int* p=a;', 'return p[2];', 'out-of-bounds'),
    violation('negative-outside-read', 'int a[2]={7,9};int* p=a;', 'return p[-1];', ('out-of-bounds', 'untracked-access', 'type-mismatch')),
    violation('index-scale-overflow', 'int a[2]={0,0};unsigned long n=1UL<<(sizeof(unsigned long)*8-2);', 'return a[n];', 'index-overflow'),
    violation('deleted-pointer', 'int* p=new int(7);delete p;', 'return *p;', 'expired-object'),
    violation('expired-stack', 'int* p=0;{int n=7;p=&n;}', 'return *p;', 'expired-object'),
    violation('zero-array-read', 'int* p=new int[0];', 'return *p;', 'out-of-bounds'),
    violation('field-array-escape', 'S s={{1,2},9};int* p=s.a;', 'return p[2];', ('subobject-out-of-bounds', 'out-of-bounds'), declarations='struct S{int a[2];int next;};', known=('ast',)),
    violation('write-to-const', 'const int n=7;int* p=const_cast<int*>(&n);', '*p=9;return 0;', 'write-to-const'),
    violation('signed-char-alias', 'int n=0;', 'return *reinterpret_cast<signed char*>(&n);', 'type-mismatch'),
    violation('uninitialized-int', 'int n;', 'return n;', 'uninitialized-read', known=('ast',)),
    violation('bad-function-signature', 'int(*p)(double)=reinterpret_cast<int(*)(double)>(&f);', 'return p(1.0);', 'call-type-mismatch', declarations='int f(int){return 0;}', known=('ast',)),
    violation('macro-wrong-read', 'int n=0;', 'return READ(&n);', 'type-mismatch', declarations='#define READ(p) (*reinterpret_cast<float*>(p))', known=('ast',)),
    violation('header-wrong-read', 'int n=0;', 'return read(&n);', 'type-mismatch', declarations='#include "read.h"', files=(('read.h', 'inline int read(void* p){return *static_cast<float*>(p);}\n'),), known=('ast',)),
]


# Minimum years keep features out of older modes instead of accepting them as
# extensions. Each positive program is compiled AND executed by matching plain
# Clang before its instrumented result can be considered a compatibility defect.
MODERN = [
    valid('nullptr', 'int* p=nullptr;return p!=nullptr;', since=2011, category='language'),
    valid('capturing-lambda', 'int n=7;auto f=[&n](){return n;};return f()!=7;', since=2011, category='language', known=('native',)),
    valid('range-for', 'int a[3]={1,2,3};int n=0;for(int x:a)n+=x;return n!=6;', since=2011, category='language'),
    valid('unique-pointer', 'std::unique_ptr<int> p(new int(7));return *p!=7;', declarations='#include <memory>', since=2011, category='library'),
    valid('initializer-list', 'std::initializer_list<int> x={3,7};return *x.begin()!=3||x.size()!=2;', declarations='#include <initializer_list>', since=2011, category='library'),
    valid('move-only-owner', 'Owner a(new int(7));Owner b(static_cast<Owner&&>(a));return *b.p!=7;', declarations='struct Owner{int* p;explicit Owner(int* q):p(q){}Owner(Owner&& x):p(x.p){x.p=nullptr;}~Owner(){delete p;}Owner(const Owner&)=delete;};', since=2011, category='language'),
    valid('overaligned-stack', 'alignas(64) int n=7;return *&n!=7;', since=2011, category='alignment'),
    valid('utf16-string-literal', 'const char16_t* p=u"az";return p[1]!=u\'z\';', since=2011, category='character-types'),
    valid('utf32-string-literal', 'const char32_t* p=U"az";return p[1]!=U\'z\';', since=2011, category='character-types'),
    valid('generic-lambda', 'auto f=[](auto n){return n+1;};return f(6)!=7;', since=2014, category='language', known=('native',)),
    valid('constexpr-local-array', 'return f()!=7;', declarations='constexpr int f(){int a[2]={3,7};return a[1];}static_assert(f()==7,"");', since=2014, category='constant-evaluation', known=('ast',)),
    valid('variable-template', 'int n=seven<int>;return *&n!=7;', declarations='template<class T>constexpr T seven=T(7);', since=2014, category='language'),
    valid('structured-binding', 'Pair p{7,9};auto [a,b]=p;return a!=7||b!=9;', declarations='struct Pair{int a;int b;};', since=2017, category='language'),
    valid('guaranteed-copy-elision', 'X x=make();return x.n!=7;', declarations='struct X{int n;X():n(7){}X(const X&)=delete;X(X&&)=delete;};X make(){return X{};}', since=2017, category='language'),
    valid('if-initializer', 'if(int n=7;n==7)return 0;return 1;', since=2017, category='language'),
    valid('fold-expression', 'return sum(1,2,4)!=7;', declarations='template<class... T>int sum(T... n){return (n+...);}', since=2017, category='language'),
    valid('if-constexpr', 'return f(7)!=7;', declarations='template<class T>int f(T n){if constexpr(sizeof(T)==sizeof(int))return int(n);else return 0;}', since=2017, category='language'),
    valid('overaligned-new', 'X* p=new X;int n=p->n;delete p;return n!=7;', declarations='struct alignas(64) X{int n=7;};', since=2017, category='alignment'),
    valid('concepts-requires', 'return plus_one(6)!=7;', declarations='template<class T>concept Addable=requires(T n){n+1;};template<Addable T>T plus_one(T n){return n+1;}', since=2020, category='language'),
    valid('consteval', 'int n=f(6);return *&n!=7;', declarations='consteval int f(int n){return n+1;}static_assert(f(6)==7);', since=2020, category='constant-evaluation'),
    valid('constinit', 'return *&n!=7;', declarations='constinit int n=7;', since=2020, category='language'),
    valid('span', 'int a[3]={3,7,9};std::span<int> s(a);return s[1]!=7||s.size()!=3;', declarations='#include <span>', since=2020, category='library'),
    valid('utf8-character-type', 'const char8_t* p=u8"az";return p[1]!=u8\'z\';', since=2020, category='character-types'),
    valid('overlapping-empty-member', 'S s{};s.n=7;return s.n!=7;', declarations='struct E{};struct S{[[no_unique_address]] E e;int n;};', since=2020, category='object-layout'),
    valid('if-consteval', 'int n=7;return f(n)!=7;', declarations='constexpr int f(int n){if consteval{return n+1;}else{return n;}}static_assert(f(6)==7);', since=2023, category='constant-evaluation'),
    valid('explicit-object-parameter', 'X x{7};return x.read()!=7;', declarations='struct X{int n;int read(this const X& self){return self.n;}};', since=2023, category='language'),
    valid('expected', 'std::expected<int,int> x(7);return !x||*x!=7;', declarations='#include <expected>', since=2023, category='library'),
    valid('pack-indexing', 'return pick<1>(3,7,9)!=7;', declarations='template<unsigned I,class... T>constexpr auto pick(T... values){return values...[I];}', since=2026, category='language'),
]


# Investigative cases are not converted into passing tests when a limitation is
# observed. Explicitly documented cases are labeled known failures; newly missed
# checks/false positives stay ordinary failures in the audit's machine-readable output.
EDGES = [
    violation('explicit-destructor-lifetime', 'X x={7};x.~X();', 'return x.n;', 'expired-object', declarations='struct X{int n;~X(){}};', category='lifetime', known=COMPATIBILITY),
    violation('character-pointer-storage-type', 'char n=0;char* p=&n;', 'signed char* q=*reinterpret_cast<signed char**>(&p);return q==0;', 'type-mismatch', category='type-identity', known=('native',)),
    violation('delete-corresponding-unsigned', 'int* p=new int(7);unsigned int* q=reinterpret_cast<unsigned int*>(p);', 'delete q;return 0;', 'delete-type-mismatch', category='deallocation'),
    violation('uninitialized-unsigned-character', 'unsigned char n;', 'return n;', 'uninitialized-read', category='initialization', known=('ast',)),
    violation('const-bulk-copy', 'const int n=7;int value=9;', 'std::memcpy(const_cast<int*>(&n),&value,sizeof(n));return 0;', 'write-to-const', declarations='#include <cstring>', category='const-bulk-memory'),
    violation('copy-outside-allocation', 'int dst[1]={0};int src[2]={1,2};', 'std::memcpy(dst,src,sizeof(src));return 0;', 'out-of-bounds', declarations='#include <cstring>', category='bulk-memory', known=('ast',)),
    violation('outside-pointer-formation', 'int a[2]={7,9};', 'int* p=a+3;return p==0;', 'out-of-bounds', category='pointer-arithmetic', known=('ast',)),
    violation('scalar-union-inactive', 'U u;u.i=0;', 'return static_cast<int>(u.f);', 'union-inactive-member', declarations='union U{int i;float f;};', category='union', known=('ast',)),
    violation('atomic-one-past-rmw', 'unsigned a[1]={0};unsigned* p=a+1;', '__atomic_fetch_add(p,1u,__ATOMIC_SEQ_CST);return 0;', 'out-of-bounds', since=2011, category='atomic-extension', known=COMPATIBILITY),
    valid('common-initial-sequence', 'U u;u.a.tag=7;u.a.n=2;return u.b.tag!=7;', declarations='struct A{int tag;double n;};struct B{int tag;float n;};union U{A a;B b;};', category='union', known=('native',)),
    valid('nontrivial-array-cookie', 'X* p=new X[3];int n=p[2].n;delete[] p;return n!=7;', declarations='struct X{int n;X():n(7){}~X(){n=0;}};', category='deallocation'),
    valid('integer-xor-roundtrip', 'int n=7;unsigned long bits=reinterpret_cast<unsigned long>(&n);unsigned long mask=0x55;bits^=mask;bits^=mask;return *reinterpret_cast<int*>(bits)!=7;', category='provenance', known=('native',)),
    valid('reference-member', 'int n=7;R r(n);return r.read()!=7;', declarations='struct R{int& n;R(int& x):n(x){}int read(){return n;}};', category='reference'),
    valid('virtual-base-subobject', 'D d;B* b=&d;return b->n!=7;', declarations='struct B{int n;B():n(7){}virtual ~B(){}};struct D:virtual B{};', category='object-layout', known=('native',)),
    violation('expired-reference-member', 'R* r=0;{int n=7;r=new R(n);}', 'return r->read();', 'expired-object', declarations='struct R{int& n;R(int& x):n(x){}int read(){return n;}};', category='reference', known=('ast',)),
    valid('thread-local-worker', 'pthread_t t;if(pthread_create(&t,0,worker,0))return 1;pthread_join(t,0);return result!=7;', declarations='#include <pthread.h>\nthread_local int local=7;static int result=0;void* worker(void*){result=*&local;return 0;}', since=2011, category='thread-local', known=('native',)),
    valid('coroutine-suspend-resume', 'int n=0;{Task task=run(&n);task.h.resume();}return n!=7;', declarations='''#include <coroutine>
struct Task{
 struct promise_type;
 using Handle=std::coroutine_handle<promise_type>;
 Handle h;
 explicit Task(Handle value):h(value){}
 Task(Task&& other):h(other.h){other.h=nullptr;}
 ~Task(){if(h)h.destroy();}
 struct promise_type{
  Task get_return_object(){return Task(Handle::from_promise(*this));}
  std::suspend_always initial_suspend()noexcept{return {};}
  std::suspend_always final_suspend()noexcept{return {};}
  void return_void()noexcept{}
  void unhandled_exception(){__builtin_trap();}
 };
};
Task run(int* out){*out=7;co_return;}
''', since=2020, category='coroutine', known=('native', 'ast')),
]


# Strict runtime probes cannot use the POSIX arming helper: foreign calls are
# intentionally outside this profile. These tiny setups are already covered by
# the detailed compatibility fixtures and source-admission regression suite.
STRICT = [
    valid('strict-scalar', 'int n=7;return *&n!=7;', profiles=('strict',)),
    valid('strict-array-loop', 'unsigned a[4]{};unsigned sum=0;for(unsigned i=0;i<4u;++i){a[i]=i;sum+=a[i];}return sum!=6u;', profiles=('strict',)),
    valid('strict-direct-call', 'return add(3u,4u)!=7u;', declarations='unsigned add(unsigned a,unsigned b){return a+b;}', profiles=('strict',)),
    valid('strict-unsigned-wrap', 'unsigned n=~0u;n+=1u;return n!=0u;', profiles=('strict',)),
    valid('strict-branch-pointer', 'unsigned a=7,b=9;bool first=true;return *(first?&a:&b)!=7u;', profiles=('strict',)),
    Case('strict-null', 'int main(){return *static_cast<unsigned*>(nullptr);}', 'strict-runtime', expectation='violation', diagnostics=('null-access',), profiles=('strict',)),
    Case('strict-uninitialized', 'int main(){unsigned n;return n;}', 'strict-runtime', expectation='violation', diagnostics=('uninitialized-read',), profiles=('strict',)),
    Case('strict-array-bounds', 'int main(){unsigned a[2]{};return a[2];}', 'strict-runtime', expectation='violation', diagnostics=('out-of-bounds',), profiles=('strict',)),
    Case('strict-row-bounds', 'int main(){unsigned a[2][3]{};return a[0][3];}', 'strict-runtime', expectation='violation', diagnostics=('subobject-out-of-bounds',), profiles=('strict',)),
]

# These are compilation-admission tests, not unsafe executable controls. Even
# when their grammar is legal, the strict profile must decline their semantics.
for name, source in {
    'stored-pointer': 'int main(){int n=7;int* p=&n;return *p;}',
    'stored-reference': 'int main(){int n=7;int& r=n;return r;}',
    'record': 'struct X{int n;};int main(){X x{7};return x.n;}',
    'union': 'union U{int n;float f;};int main(){U u;u.n=7;return u.n;}',
    'character-object': 'int main(){unsigned char n;return n;}',
    'explicit-destruction': 'struct X{int n;~X(){}};int main(){X x{7};x.~X();return x.n;}',
    'pointer-type-alias': 'int main(){char n=0;char* p=&n;signed char* q=*reinterpret_cast<signed char**>(&p);return q==nullptr;}',
    'dynamic-divisor': 'int main(){unsigned n=7;return 1u/n;}',
    'signed-arithmetic': 'int f(int n){return n+1;}int main(){return f(7);}',
    'const-cast': 'int main(){const unsigned n=7;*const_cast<unsigned*>(&n)=9;return 0;}',
    'atomic-builtin': 'int main(){unsigned n=0;return __atomic_fetch_add(&n,1u,__ATOMIC_SEQ_CST);}',
    'nested-mutation': 'int main(){unsigned n=0;return n++ + n++;}',
}.items():
    STRICT.append(Case('strict-reject-' + name, source, 'strict-admission', since=2017,
                       expectation='policy', profiles=('strict',)))


ALL_CASES = CORE + MODERN + EDGES + STRICT
