// Negative native example for a saved union-member pointer. The members share an
// address, but assigning floating ends the previously active integer member lifetime.
union Value { int integer; float floating; };

int main() {
    Value value;
    value.integer = 7;
    // Capture the current member generation along with the unchanged pointer bits.
    int* saved = &value.integer;
    value.floating = 2;
    // Expected runtime result: union-inactive-member, exit 86. The shared union allocation
    // remaining live does not make this saved member capability valid.
    return *saved;
}
