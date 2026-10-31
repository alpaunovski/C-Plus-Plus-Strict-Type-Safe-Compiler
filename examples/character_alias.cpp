// Negative native example: signed char is not a general object-representation access
// type for an int. The source hook must reject this read even though LLVM lowers it
// to a byte access. Expected runtime result: type-mismatch, exit 86.
int main() {
    int value = 0;
    // Casting forms a pointer; the following value access is the operation being checked.
    return *reinterpret_cast<signed char*>(&value);
}
