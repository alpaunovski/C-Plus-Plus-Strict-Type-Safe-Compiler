// Positive native scalar-union examples. Whole-union copies must transfer member and
// pointer metadata, while copying into the member already active preserves its lifetime.
// Broader union semantics remain incomplete and union use still reports coverage gaps.
#include <cstring>

union Value { int integer; float floating; };

int main() {
    Value original{7};
    Value copied;
    // Transfer supported active-member metadata with the complete representation copy.
    std::memcpy(&copied, &original, sizeof(Value));
    if (copied.integer != 7) return 1;
    Value assigned{1};
    int* existing = &assigned.integer;
    // Both source and destination have integer active, so this assignment must not expire
    // the existing pointer to the destination member.
    assigned = original;
    if (*existing != 7) return 1;
    // A simple assignment may activate this supported scalar member; subsequent reads use
    // its new member generation rather than the old integer member.
    copied.floating = 2;
    return copied.floating != 2;
}
