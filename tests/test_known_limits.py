# These expected failures intentionally document counterexamples in experimental mode.
# Strict-mode tests separately require compilation rejection of the same witnesses.
# When a real fix lands, remove the expectedFailure marker only after reviewing the new behavior.
"""Executable counterexamples to completeness. Expected failures stay visible.

Remove expectedFailure only when the corresponding behavior is implemented;
an unexpected success deliberately makes the suite fail for review.
"""
import unittest
import test_native as native


class KnownLimits(unittest.TestCase):
    setUp = native.NativeTests.setUp
    program = native.NativeTests.program

    # The first post-destruction member read should fail before any later automatic cleanup.
    # Keeping this executable exposes incomplete explicit-lifetime retirement instead of hiding it.
    @unittest.expectedFailure
    def test_explicit_destructor_must_end_object_lifetime(self):
        self.program("struct X{int n;~X(){}};int main(){X x{7};x.~X();return x.n;}",
                     expected=86,diagnostic="expired-object")

    # Reading a char* object through signed char** violates its storage access type even if
    # its loaded pointer is only compared, never dereferenced. Byte-pointee normalization loses this fact.
    @unittest.expectedFailure
    def test_character_pointer_storage_types_must_remain_distinct(self):
        self.program('int main(){char n=0;char* p=&n;'
                     'signed char* q=*reinterpret_cast<signed char**>(&p);return q==nullptr;}',
                     expected=86, diagnostic='type-mismatch')


if __name__ == '__main__': unittest.main()
