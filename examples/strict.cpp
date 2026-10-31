// Positive strict-v1 example: direct scalar calls, small fixed arrays and unsigned
// arithmetic stay within the source allowlist. No stored pointers, external interfaces
// or unproven signed arithmetic are required.
unsigned total(unsigned base) {
    unsigned values[4]{};
    // Standalone unsigned updates and checked indexing are admitted; every iteration
    // initializes a supported array element before the later reads.
    for (unsigned i = 0; i < 4u; ++i) values[i] = base + i;
    unsigned sum = 0;
    for (unsigned i = 0; i < 4u; ++i) sum += values[i];
    return sum;
}

// A successful strict build also requires zero reported gaps and foreign symbols.
// The executable returns zero when the four values sum to 18.
int main() {
    return total(3u) != 18u;
}
