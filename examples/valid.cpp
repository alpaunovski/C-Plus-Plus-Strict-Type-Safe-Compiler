// Positive experimental-native smoke example. Ordinary C++ pointer syntax is retained
// for a void round trip, stack indexing and scalar new/delete. printf is an explicit
// foreign-library boundary, so this is intentionally not a strict-v1 example.
#include <cstdio>

// A valid cast back to the actual int type must retain the original object's authority.
int read_value(void* opaque) {
  return *static_cast<int*>(opaque);
}

// Expected output: value=42 array=2 heap=7, followed by successful deallocation.
int main() {
  int value = 42;
  void* opaque = &value;
  int items[] = {1, 2, 3};
  int* heap = new int(7);
  std::printf("value=%d array=%d heap=%d\n", read_value(opaque), items[1], *heap);
  delete heap;
}
